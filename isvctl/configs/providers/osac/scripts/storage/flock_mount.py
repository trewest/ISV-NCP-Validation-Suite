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

"""NFS flock mount test for OSAC.

Validates that an NFS-mounted filesystem supports POSIX advisory file
locking via flock(2): exclusive locks, shared locks, and lock contention.

Uses a privileged pod that mounts NFS internally via NFSv4 to the
pseudo-root (/exports on the server, exported as / with fsid=0).

Covers HSS14-01.

Output JSON:
{
    "success": true,
    "platform": "storage",
    "test_name": "flock_mount",
    "tests": {
        "mounted_with_flock":  {"passed": true, "message": "..."},
        "flock_exclusive":     {"passed": true, "message": "..."},
        "flock_shared":        {"passed": true, "message": "..."},
        "flock_contention":    {"passed": true, "message": "..."}
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
TEST_NS = "isvtest-flockmount"
CLIENT_POD = "flock-client"
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
    deadline = time.time() + 120
    while time.time() < deadline:
        rc, phase, _ = run_kubectl(
            "get", "namespace", TEST_NS,
            "-o", "jsonpath={.status.phase}",
        )
        if rc != 0:
            break
        if phase == "Terminating":
            time.sleep(3)
            continue
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
                    "touch /mnt/.ready-flock && "
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
                "test", "-f", "/mnt/.ready-flock",
            )
            if rc2 == 0:
                return True, "client pod ready with NFS mounted"
        time.sleep(3)
    return False, "client pod setup timed out"


def pod_exec(*cmd_parts: str, timeout: int = 30) -> tuple[int, str, str]:
    return run_kubectl(
        "exec", CLIENT_POD, "-n", TEST_NS, "--", *cmd_parts,
        timeout=timeout,
    )


def cleanup_test_files() -> None:
    nfs_exec("rm", "-f", f"{NFS_EXPORT_PATH}/.ready-flock")
    nfs_exec("rm", "-f", f"{NFS_EXPORT_PATH}/.flock-testfile")
    nfs_exec("rm", "-f", f"{NFS_EXPORT_PATH}/.flock-holder")


def main() -> int:
    tag = uuid.uuid4().hex[:8]
    result: dict = {
        "success": False,
        "platform": "storage",
        "test_name": "flock_mount",
        "tests": {
            "mounted_with_flock": {"passed": False, "message": ""},
            "flock_exclusive": {"passed": False, "message": ""},
            "flock_shared": {"passed": False, "message": ""},
            "flock_contention": {"passed": False, "message": ""},
        },
    }

    if DEMO_MODE:
        for key in result["tests"]:
            result["tests"][key] = {"passed": True, "message": f"demo: {key} ok"}
        result["success"] = True
        print(json.dumps(result, indent=2))
        return 0

    lockfile = f"/mnt/.flock-testfile-{tag}"

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

        # --- 1. Verify NFS is mounted and flock works at all ---
        rc, mount_out, _ = pod_exec("sh", "-c", "mount | grep /mnt")
        if rc != 0 or "nfs" not in mount_out.lower():
            result["tests"]["mounted_with_flock"] = {
                "passed": False,
                "message": f"NFS mount not found on /mnt: {mount_out}",
            }
            print(json.dumps(result, indent=2))
            return 0

        rc, _, err = pod_exec("sh", "-c", f"touch {lockfile} && flock -x -w 5 {lockfile} echo ok")
        if rc != 0:
            result["tests"]["mounted_with_flock"] = {
                "passed": False,
                "message": f"flock not supported on NFS mount: {err}",
            }
            print(json.dumps(result, indent=2))
            return 0

        result["tests"]["mounted_with_flock"] = {
            "passed": True,
            "message": f"NFS mounted at /mnt ({mount_out.split()[4] if len(mount_out.split()) > 4 else 'nfs'}) with flock support",
        }

        # --- 2. Exclusive lock ---
        rc, out, err = pod_exec(
            "sh", "-c",
            f"flock -x -w 5 {lockfile} sh -c 'echo EXCLUSIVE_OK'",
        )
        if rc == 0 and "EXCLUSIVE_OK" in out:
            result["tests"]["flock_exclusive"] = {
                "passed": True,
                "message": "exclusive lock acquired and released successfully",
            }
        else:
            result["tests"]["flock_exclusive"] = {
                "passed": False,
                "message": f"exclusive lock failed: rc={rc} out={out} err={err}",
            }

        # --- 3. Shared lock ---
        rc, out, err = pod_exec(
            "sh", "-c",
            f"flock -s -w 5 {lockfile} sh -c 'echo SHARED_OK'",
        )
        if rc == 0 and "SHARED_OK" in out:
            result["tests"]["flock_shared"] = {
                "passed": True,
                "message": "shared lock acquired and released successfully",
            }
        else:
            result["tests"]["flock_shared"] = {
                "passed": False,
                "message": f"shared lock failed: rc={rc} out={out} err={err}",
            }

        # --- 4. Contention: hold exclusive, try another exclusive ---
        rc, out, err = pod_exec(
            "sh", "-c",
            f"flock -x {lockfile} sh -c 'touch /mnt/.flock-holder; sleep 10' & "
            "BGPID=$!; "
            "for i in $(seq 1 20); do test -f /mnt/.flock-holder && break; sleep 0.5; done; "
            f"flock -x -w 2 {lockfile} echo CONTENTION_FAIL 2>/dev/null; "
            "RC=$?; "
            "kill $BGPID 2>/dev/null; wait $BGPID 2>/dev/null; "
            'if [ "$RC" -ne 0 ]; then echo CONTENTION_ENFORCED; else echo CONTENTION_NOT_ENFORCED; fi',
            timeout=45,
        )
        if "CONTENTION_ENFORCED" in out:
            result["tests"]["flock_contention"] = {
                "passed": True,
                "message": "exclusive lock contention enforced (second lock timed out while first held)",
            }
        elif "CONTENTION_NOT_ENFORCED" in out:
            result["tests"]["flock_contention"] = {
                "passed": False,
                "message": "contention NOT enforced — second exclusive lock acquired while first was held",
            }
        else:
            result["tests"]["flock_contention"] = {
                "passed": False,
                "message": f"contention test inconclusive: rc={rc} out={out} err={err}",
            }

        result["success"] = all(t["passed"] for t in result["tests"].values())

    except Exception as e:
        result["error"] = str(e)
        result["error_type"] = type(e).__name__
    finally:
        cleanup_test_files()
        cleanup_namespace()

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
