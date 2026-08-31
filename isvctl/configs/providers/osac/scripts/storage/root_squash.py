#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""NFS root-squash toggle test for OSAC.

Validates root_squash can be enabled and disabled at runtime on the NFS
server, and that the setting takes effect: root writes from a client pod
are squashed to anonymous UID when enabled, and retained as UID 0 when
disabled.

Uses a privileged pod that mounts NFS internally via NFSv4 to the
pseudo-root (/exports on the server, exported as / with fsid=0).
A single pod is kept running for both tests to avoid repeated
nfs-utils install overhead.

Covers HSS13-01.

Output JSON:
{
    "success": true,
    "platform": "storage",
    "test_name": "root_squash",
    "tests": {
        "enable_root_squash":  {"passed": true, "message": "..."},
        "root_squashed":       {"passed": true, "message": "..."},
        "disable_root_squash": {"passed": true, "message": "..."},
        "root_unsquashed":     {"passed": true, "message": "..."}
    }
}
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"
KUBECTL = os.environ.get("KUBECTL", "kubectl")

NFS_SERVER_NS = os.environ.get("NFS_SERVER_NS", "nfs-system")
NFS_SERVER_DEPLOY = os.environ.get("NFS_SERVER_DEPLOY", "deployment/nfs-server")
NFS_SERVER_LABEL = os.environ.get("NFS_SERVER_LABEL", "app=nfs-server")
NFS_EXPORT_PATH = os.environ.get("NFS_EXPORT_PATH", "/exports")
TEST_NS = "isvtest-rootsquash"
CLIENT_POD = "rootsquash-client"
SETUP_TIMEOUT = 180


def run_kubectl(*args: str, stdin: str | None = None, timeout: int = 60) -> tuple[int, str, str]:
    cmd = KUBECTL.split() + list(args)
    try:
        proc = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except subprocess.TimeoutExpired:
        return 1, "", f"kubectl timed out after {timeout}s"
    except Exception as exc:
        return 1, "", str(exc)


def get_nfs_pod_ip() -> str | None:
    rc, ip, _ = run_kubectl(
        "get", "pod", "-n", NFS_SERVER_NS, "-l", NFS_SERVER_LABEL,
        "-o", "jsonpath={.items[0].status.podIP}",
    )
    if rc == 0 and ip:
        return ip
    return None


def ensure_namespace() -> bool:
    # Wait for any previous terminating namespace to be fully gone
    deadline = time.time() + 120
    while time.time() < deadline:
        rc, phase, _ = run_kubectl(
            "get", "namespace", TEST_NS,
            "-o", "jsonpath={.status.phase}",
        )
        if rc != 0:
            break  # namespace doesn't exist — ready to create
        if phase == "Terminating":
            time.sleep(3)
            continue
        # Namespace exists and is Active — delete it first
        run_kubectl("delete", "namespace", TEST_NS, "--wait=true", timeout=90)
        break
    rc, ns_yaml, _ = run_kubectl("create", "namespace", TEST_NS, "--dry-run=client", "-o", "yaml")
    if rc != 0:
        return False
    rc, _, _ = run_kubectl("apply", "-f", "-", stdin=ns_yaml)
    if rc != 0:
        return False
    subprocess.run(
        ["oc", "adm", "policy", "add-scc-to-user", "privileged",
         "-z", "default", "-n", TEST_NS],
        capture_output=True, text=True, timeout=30,
    )
    return True


def cleanup_namespace() -> None:
    run_kubectl("delete", "namespace", TEST_NS, "--ignore-not-found", "--wait=true", timeout=120)


def nfs_exec(*cmd_parts: str, timeout: int = 30) -> tuple[int, str, str]:
    return run_kubectl(
        "exec", "-n", NFS_SERVER_NS, NFS_SERVER_DEPLOY, "--", *cmd_parts,
        timeout=timeout,
    )


def create_client_pod(nfs_ip: str) -> tuple[bool, str]:
    """Create a privileged pod that installs nfs-utils, mounts NFS, then sleeps."""
    manifest = json.dumps({
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": CLIENT_POD, "namespace": TEST_NS},
        "spec": {
            "restartPolicy": "Never",
            "containers": [{
                "name": "client",
                "image": "fedora:latest",
                "command": [
                    "sh", "-c",
                    "dnf install -y nfs-utils > /dev/null 2>&1 && "
                    f"mount -t nfs4 {nfs_ip}:/ /mnt && "
                    "touch /mnt/.ready && "
                    "sleep 600",
                ],
                "securityContext": {"privileged": True, "runAsUser": 0},
            }],
        },
    })
    rc, _, err = run_kubectl("apply", "-f", "-", stdin=manifest)
    if rc != 0:
        return False, f"pod apply failed: {err}"

    deadline = time.time() + SETUP_TIMEOUT
    while time.time() < deadline:
        rc, phase, _ = run_kubectl(
            "get", "pod", CLIENT_POD, "-n", TEST_NS,
            "-o", "jsonpath={.status.phase}",
        )
        if phase == "Failed":
            _, logs, _ = run_kubectl("logs", CLIENT_POD, "-n", TEST_NS)
            return False, f"pod failed: {logs}"
        if phase == "Running":
            rc2, _, _ = run_kubectl(
                "exec", CLIENT_POD, "-n", TEST_NS, "--",
                "test", "-f", "/mnt/.ready",
            )
            if rc2 == 0:
                return True, "client pod ready with NFS mounted"
        time.sleep(3)
    return False, "client pod setup timed out"


def client_touch(filename: str) -> tuple[bool, str]:
    """Create a file on NFS via the client pod."""
    rc, _, err = run_kubectl(
        "exec", CLIENT_POD, "-n", TEST_NS, "--",
        "touch", f"/mnt/{filename}",
        timeout=15,
    )
    if rc != 0:
        return False, f"touch failed: {err}"
    return True, ""


def toggle_root_squash(enable: bool) -> tuple[bool, str]:
    squash_opt = "root_squash" if enable else "no_root_squash"
    unexport_cmd = f"exportfs -u '*:{NFS_EXPORT_PATH}'"
    reexport_cmd = f"exportfs -o rw,fsid=0,insecure,{squash_opt} '*:{NFS_EXPORT_PATH}'"
    rc, _, err = nfs_exec("sh", "-c", f"{unexport_cmd} && {reexport_cmd}")
    if rc != 0:
        return False, f"exportfs toggle failed: {err}"
    rc, out, _ = nfs_exec("exportfs", "-v")
    if rc != 0:
        return False, "exportfs -v failed"
    for line in out.splitlines():
        if NFS_EXPORT_PATH in line:
            if squash_opt in line:
                return True, f"{NFS_EXPORT_PATH} now exported with {squash_opt}"
    return False, f"{NFS_EXPORT_PATH} export not found with '{squash_opt}' after toggle"


def check_file_uid(filename: str) -> tuple[bool, int, str]:
    filepath = f"{NFS_EXPORT_PATH}/{filename}"
    rc, out, err = nfs_exec("stat", "-c", "%u", filepath)
    if rc != 0:
        return False, -1, f"stat failed: {err}"
    try:
        uid = int(out)
    except ValueError:
        return False, -1, f"unexpected stat output: {out}"
    return True, uid, ""


def cleanup_test_files(*filenames: str) -> None:
    for f in filenames:
        nfs_exec("rm", "-f", f"{NFS_EXPORT_PATH}/{f}")
    nfs_exec("rm", "-f", f"{NFS_EXPORT_PATH}/.ready")
    nfs_exec("chmod", "755", NFS_EXPORT_PATH)


def main() -> int:
    result: dict = {
        "success": False,
        "platform": "storage",
        "test_name": "root_squash",
        "tests": {
            "enable_root_squash": {"passed": False, "message": ""},
            "root_squashed": {"passed": False, "message": ""},
            "disable_root_squash": {"passed": False, "message": ""},
            "root_unsquashed": {"passed": False, "message": ""},
        },
    }

    if DEMO_MODE:
        for key in result["tests"]:
            result["tests"][key] = {"passed": True, "message": f"demo: {key} ok"}
        result["success"] = True
        print(json.dumps(result, indent=2))
        return 0

    squash_file = f"squash-{uuid.uuid4().hex[:8]}"
    unsquash_file = f"unsquash-{uuid.uuid4().hex[:8]}"

    try:
        ensure_namespace()
        nfs_ip = get_nfs_pod_ip()
        if not nfs_ip:
            result["error"] = "could not resolve NFS server pod IP"
            print(json.dumps(result, indent=2))
            return 0

        ok, msg = create_client_pod(nfs_ip)
        if not ok:
            result["error"] = msg
            print(json.dumps(result, indent=2))
            return 0

        # Make export writable so squashed nobody user can create files
        nfs_exec("chmod", "1777", NFS_EXPORT_PATH)

        # --- 1. Enable root_squash ---
        ok, msg = toggle_root_squash(enable=True)
        result["tests"]["enable_root_squash"] = {"passed": ok, "message": msg}
        if not ok:
            print(json.dumps(result, indent=2))
            return 0

        # --- 2. Verify root is squashed ---
        ok, msg = client_touch(squash_file)
        if not ok:
            result["tests"]["root_squashed"] = {"passed": False, "message": msg}
        else:
            ok, uid, err = check_file_uid(squash_file)
            if not ok:
                result["tests"]["root_squashed"] = {"passed": False, "message": err}
            elif uid == 0:
                result["tests"]["root_squashed"] = {
                    "passed": False,
                    "message": "file UID is 0 (root) — root_squash not in effect",
                }
            else:
                result["tests"]["root_squashed"] = {
                    "passed": True,
                    "message": f"file UID is {uid} (squashed from root)",
                }

        # --- 3. Disable root_squash ---
        ok, msg = toggle_root_squash(enable=False)
        result["tests"]["disable_root_squash"] = {"passed": ok, "message": msg}
        if not ok:
            print(json.dumps(result, indent=2))
            return 0

        # --- 4. Verify root is NOT squashed ---
        ok, msg = client_touch(unsquash_file)
        if not ok:
            result["tests"]["root_unsquashed"] = {"passed": False, "message": msg}
        else:
            ok, uid, err = check_file_uid(unsquash_file)
            if not ok:
                result["tests"]["root_unsquashed"] = {"passed": False, "message": err}
            elif uid != 0:
                result["tests"]["root_unsquashed"] = {
                    "passed": False,
                    "message": f"file UID is {uid} — expected 0 (root) with no_root_squash",
                }
            else:
                result["tests"]["root_unsquashed"] = {
                    "passed": True,
                    "message": "file UID is 0 (root retained with no_root_squash)",
                }

        result["success"] = all(t["passed"] for t in result["tests"].values())

    except Exception as e:
        result["error"] = str(e)
        result["error_type"] = type(e).__name__
    finally:
        cleanup_test_files(squash_file, unsquash_file)
        cleanup_namespace()

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
