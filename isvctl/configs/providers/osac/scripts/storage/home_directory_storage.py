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

"""Home-directory storage test for OSAC.

Validates filesystem quotas (ext4 usrquota/grpquota), per-UID/GID usage
accounting, and NFSv4 shared-storage availability — all against the
in-cluster NFS server.

Covers DIR01-01, DIR01-02, DIR02-01.

Output JSON:
{
    "success": true,
    "platform": "storage",
    "test_name": "home_directory_storage",
    "tests": {
        "filesystem_quota_configured": {"passed": true, "message": "..."},
        "filesystem_quota_updated":    {"passed": true, "message": "..."},
        "filesystem_quota_enforced":   {"passed": true, "message": "..."},
        "uid_usage_accounted":         {"passed": true, "message": "..."},
        "gid_usage_accounted":         {"passed": true, "message": "..."},
        "identity_usage_isolated":     {"passed": true, "message": "..."},
        "nfsv4_mounted":               {"passed": true, "message": "..."},
        "nfs_read_write":              {"passed": true, "message": "..."},
        "nfs_shared_visibility":       {"passed": true, "message": "..."}
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
TEST_NS = "isvtest-homedir"
CLIENT_POD_A = "homedir-client-a"
CLIENT_POD_B = "homedir-client-b"
SETUP_TIMEOUT = 180

TEST_UID = 5000
TEST_UID2 = 5001
TEST_GID = 6000


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


def create_client_pod(name: str, nfs_ip: str) -> tuple[bool, str]:
    manifest = json.dumps({
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "namespace": TEST_NS},
        "spec": {
            "restartPolicy": "Never",
            "containers": [{
                "name": "client",
                "image": "fedora:latest",
                "command": [
                    "sh", "-c",
                    "dnf install -y nfs-utils > /dev/null 2>&1 && "
                    f"mount -t nfs4 {nfs_ip}:/ /mnt && "
                    f"touch /mnt/.ready-{name} && "
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
            "get", "pod", name, "-n", TEST_NS,
            "-o", "jsonpath={.status.phase}",
        )
        if phase == "Failed":
            _, logs, _ = run_kubectl("logs", name, "-n", TEST_NS)
            return False, f"pod failed: {logs}"
        if phase == "Running":
            rc2, _, _ = run_kubectl(
                "exec", name, "-n", TEST_NS, "--",
                "test", "-f", f"/mnt/.ready-{name}",
            )
            if rc2 == 0:
                return True, f"{name} ready with NFS mounted"
        time.sleep(3)
    return False, f"{name} setup timed out"


def pod_exec(pod: str, *cmd_parts: str, timeout: int = 30) -> tuple[int, str, str]:
    return run_kubectl(
        "exec", pod, "-n", TEST_NS, "--", *cmd_parts,
        timeout=timeout,
    )


def enable_quotas() -> tuple[bool, str]:
    """Enable ext4 usrquota/grpquota on the NFS server's /exports."""
    rc, mount_out, _ = nfs_exec("mount")
    if rc != 0:
        return False, "cannot read mounts on NFS server"
    device = None
    for line in mount_out.splitlines():
        if f" {NFS_EXPORT_PATH} " in line or f" on {NFS_EXPORT_PATH} " in line:
            device = line.split()[0]
            break
    if not device:
        return False, f"{NFS_EXPORT_PATH} mount not found on NFS server"

    rc, _, err = nfs_exec(
        "sh", "-c",
        f"mount -o remount,usrquota,grpquota {NFS_EXPORT_PATH}",
        timeout=30,
    )
    if rc != 0:
        return False, f"remount with quotas failed: {err}"

    nfs_exec("quotacheck", "-cugm", NFS_EXPORT_PATH, timeout=60)
    rc, _, err = nfs_exec("quotaon", NFS_EXPORT_PATH, timeout=15)
    if rc != 0 and "already on" not in err.lower():
        return False, f"quotaon failed: {err}"

    return True, f"quotas enabled on {NFS_EXPORT_PATH} ({device})"


def disable_quotas() -> None:
    """Best-effort disable quotas and remount without quota options."""
    nfs_exec("quotaoff", NFS_EXPORT_PATH, timeout=15)
    nfs_exec("sh", "-c", f"mount -o remount {NFS_EXPORT_PATH}", timeout=15)


def ensure_test_users() -> None:
    """Create test users on the NFS server for quota attribution."""
    for uid, name in [(TEST_UID, "isvquota1"), (TEST_UID2, "isvquota2")]:
        nfs_exec(
            "sh", "-c",
            f"id {uid} >/dev/null 2>&1 || useradd -u {uid} -M -s /bin/sh {name} 2>/dev/null || true",
        )
    nfs_exec(
        "sh", "-c",
        f"groupadd -g {TEST_GID} isvgrp1 2>/dev/null || true",
    )


def cleanup_test_files(tag: str) -> None:
    nfs_exec("rm", "-rf", f"{NFS_EXPORT_PATH}/homedir-test-{tag}")
    nfs_exec("rm", "-f", f"{NFS_EXPORT_PATH}/.ready-{CLIENT_POD_A}")
    nfs_exec("rm", "-f", f"{NFS_EXPORT_PATH}/.ready-{CLIENT_POD_B}")
    nfs_exec("sh", "-c", f"setquota -u {TEST_UID} 0 0 0 0 {NFS_EXPORT_PATH} 2>/dev/null || true")
    nfs_exec("sh", "-c", f"setquota -u {TEST_UID2} 0 0 0 0 {NFS_EXPORT_PATH} 2>/dev/null || true")
    nfs_exec("sh", "-c", f"setquota -g {TEST_GID} 0 0 0 0 {NFS_EXPORT_PATH} 2>/dev/null || true")


def main() -> int:
    tag = uuid.uuid4().hex[:8]
    result: dict = {
        "success": False,
        "platform": "storage",
        "test_name": "home_directory_storage",
        "tests": {
            "filesystem_quota_configured": {"passed": False, "message": ""},
            "filesystem_quota_updated": {"passed": False, "message": ""},
            "filesystem_quota_enforced": {"passed": False, "message": ""},
            "uid_usage_accounted": {"passed": False, "message": ""},
            "gid_usage_accounted": {"passed": False, "message": ""},
            "identity_usage_isolated": {"passed": False, "message": ""},
            "nfsv4_mounted": {"passed": False, "message": ""},
            "nfs_read_write": {"passed": False, "message": ""},
            "nfs_shared_visibility": {"passed": False, "message": ""},
        },
    }

    if DEMO_MODE:
        for key in result["tests"]:
            result["tests"][key] = {"passed": True, "message": f"demo: {key} ok"}
        result["success"] = True
        print(json.dumps(result, indent=2))
        return 0

    testdir = f"homedir-test-{tag}"
    server_testdir = f"{NFS_EXPORT_PATH}/{testdir}"

    try:
        if not ensure_namespace():
            result["error"] = "could not create test namespace"
            print(json.dumps(result, indent=2))
            return 0

        nfs_ip = get_nfs_pod_ip()
        if not nfs_ip:
            result["error"] = "could not resolve NFS server pod IP"
            print(json.dumps(result, indent=2))
            return 0

        # Ensure no_root_squash so client UID/GID attribution is preserved
        nfs_exec(
            "sh", "-c",
            f"exportfs -u '*:{NFS_EXPORT_PATH}' 2>/dev/null; "
            f"exportfs -o rw,fsid=0,insecure,no_root_squash '*:{NFS_EXPORT_PATH}'",
        )

        nfs_exec("mkdir", "-p", server_testdir)
        nfs_exec("chmod", "1777", server_testdir)

        # Create both client pods in parallel
        ok_a, msg_a = create_client_pod(CLIENT_POD_A, nfs_ip)
        if not ok_a:
            result["error"] = f"pod A: {msg_a}"
            print(json.dumps(result, indent=2))
            return 0

        ok_b, msg_b = create_client_pod(CLIENT_POD_B, nfs_ip)
        if not ok_b:
            result["error"] = f"pod B: {msg_b}"
            print(json.dumps(result, indent=2))
            return 0

        # ═══ NFS availability tests (DIR02-01) ═══

        # --- nfsv4_mounted ---
        rc, mount_out, _ = pod_exec(CLIENT_POD_A, "sh", "-c", "mount | grep /mnt")
        if rc == 0 and "nfs" in mount_out.lower():
            nfs_type = "nfs4" if "nfs4" in mount_out.lower() else "nfs"
            result["tests"]["nfsv4_mounted"] = {
                "passed": True,
                "message": f"NFSv4 mounted at /mnt ({nfs_type})",
            }
        else:
            result["tests"]["nfsv4_mounted"] = {
                "passed": False,
                "message": f"NFS mount not found on /mnt: {mount_out}",
            }
            print(json.dumps(result, indent=2))
            return 0

        # --- nfs_read_write ---
        test_content = f"rw-test-{tag}"
        rc, _, err = pod_exec(
            CLIENT_POD_A, "sh", "-c",
            f"echo '{test_content}' > /mnt/{testdir}/rw-test.txt",
        )
        if rc != 0:
            result["tests"]["nfs_read_write"] = {
                "passed": False,
                "message": f"write failed: {err}",
            }
        else:
            rc, read_out, _ = pod_exec(
                CLIENT_POD_A, "cat", f"/mnt/{testdir}/rw-test.txt",
            )
            if rc == 0 and test_content in read_out:
                result["tests"]["nfs_read_write"] = {
                    "passed": True,
                    "message": "file written and read back successfully via NFSv4",
                }
            else:
                result["tests"]["nfs_read_write"] = {
                    "passed": False,
                    "message": f"read-back mismatch: expected '{test_content}', got '{read_out}'",
                }

        # --- nfs_shared_visibility ---
        rc, read_out, _ = pod_exec(
            CLIENT_POD_B, "cat", f"/mnt/{testdir}/rw-test.txt",
        )
        if rc == 0 and test_content in read_out:
            result["tests"]["nfs_shared_visibility"] = {
                "passed": True,
                "message": "file written by pod A is visible from pod B on the same NFS share",
            }
        else:
            result["tests"]["nfs_shared_visibility"] = {
                "passed": False,
                "message": f"pod B cannot see pod A's file: rc={rc} out={read_out}",
            }

        # ═══ Quota tests (DIR01-01) ═══

        ok, msg = enable_quotas()
        if not ok:
            result["tests"]["filesystem_quota_configured"]["message"] = msg
            result["tests"]["filesystem_quota_updated"]["message"] = "skipped: quotas not enabled"
            result["tests"]["filesystem_quota_enforced"]["message"] = "skipped: quotas not enabled"
            result["tests"]["uid_usage_accounted"]["message"] = "skipped: quotas not enabled"
            result["tests"]["gid_usage_accounted"]["message"] = "skipped: quotas not enabled"
            result["tests"]["identity_usage_isolated"]["message"] = "skipped: quotas not enabled"
            result["success"] = all(t["passed"] for t in result["tests"].values())
            print(json.dumps(result, indent=2))
            return 0

        ensure_test_users()

        # --- filesystem_quota_configured ---
        # Set 4MB hard limit for TEST_UID, then write a small file AS that UID
        # so repquota shows the entry (repquota only lists UIDs with usage or limits)
        rc, _, err = nfs_exec(
            "setquota", "-u", str(TEST_UID), "0", "4096", "0", "0", NFS_EXPORT_PATH,
        )
        if rc != 0:
            result["tests"]["filesystem_quota_configured"] = {
                "passed": False,
                "message": f"setquota failed: {err}",
            }
        else:
            # Write a tiny file as TEST_UID so repquota shows the entry
            nfs_exec(
                "sh", "-c",
                f"su -s /bin/sh isvquota1 -c '"
                f"dd if=/dev/zero of={server_testdir}/quota-probe.dat bs=1K count=4 2>/dev/null"
                f"'",
                timeout=15,
            )
            rc, repq_out, _ = nfs_exec("repquota", "-nup", NFS_EXPORT_PATH)
            if rc == 0 and str(TEST_UID) in repq_out and "4096" in repq_out:
                result["tests"]["filesystem_quota_configured"] = {
                    "passed": True,
                    "message": f"quota configured for UID {TEST_UID}: 4MB hard limit, visible in repquota",
                }
            else:
                result["tests"]["filesystem_quota_configured"] = {
                    "passed": False,
                    "message": f"quota set but not visible in repquota: {repq_out[:300]}",
                }

        # --- filesystem_quota_updated ---
        # Update to 8MB hard limit and verify
        rc, _, err = nfs_exec(
            "setquota", "-u", str(TEST_UID), "0", "8192", "0", "0", NFS_EXPORT_PATH,
        )
        if rc != 0:
            result["tests"]["filesystem_quota_updated"] = {
                "passed": False,
                "message": f"setquota update failed: {err}",
            }
        else:
            rc, repq_out, _ = nfs_exec("repquota", "-nup", NFS_EXPORT_PATH)
            if rc == 0 and "8192" in repq_out:
                result["tests"]["filesystem_quota_updated"] = {
                    "passed": True,
                    "message": f"quota updated for UID {TEST_UID}: 8MB hard limit",
                }
            else:
                result["tests"]["filesystem_quota_updated"] = {
                    "passed": False,
                    "message": f"quota update not reflected in repquota: {repq_out[:300]}",
                }

        # --- filesystem_quota_enforced ---
        # Remove probe file from configured test so its blocks don't count against the 1MB limit
        nfs_exec("sh", "-c", f"rm -f {server_testdir}/quota-probe.dat")
        # Set tight 1MB hard limit, then try to write 2MB AS TEST_UID
        nfs_exec("setquota", "-u", str(TEST_UID), "0", "1024", "0", "0", NFS_EXPORT_PATH)

        rc, dd_out, dd_err = nfs_exec(
            "sh", "-c",
            f"su -s /bin/sh isvquota1 -c '"
            f"dd if=/dev/zero of={server_testdir}/quota-exceed.dat bs=1K count=2048 2>&1"
            f"'",
            timeout=30,
        )
        # dd should fail or produce a short write when quota is exceeded
        combined = f"{dd_out} {dd_err}".lower()
        quota_hit = "quota" in combined or "no space" in combined or "exceeded" in combined

        if not quota_hit:
            # Check file size — should be < 2MB if enforcement worked
            rc2, size_out, _ = nfs_exec("stat", "-c", "%s", f"{server_testdir}/quota-exceed.dat")
            if rc2 == 0:
                try:
                    size = int(size_out)
                    if size < 2048 * 1024:
                        quota_hit = True
                except ValueError:
                    pass

        if not quota_hit:
            # Check repquota for over-limit marker
            rc3, repq_out, _ = nfs_exec("repquota", "-nup", NFS_EXPORT_PATH)
            if rc3 == 0:
                for line in repq_out.splitlines():
                    if str(TEST_UID) in line and ("+" in line or "*" in line):
                        quota_hit = True
                        break

        if quota_hit:
            result["tests"]["filesystem_quota_enforced"] = {
                "passed": True,
                "message": f"quota enforcement verified: writes by UID {TEST_UID} capped at 1MB hard limit",
            }
        else:
            result["tests"]["filesystem_quota_enforced"] = {
                "passed": False,
                "message": f"quota enforcement not detected: dd_out={dd_out[:200]} dd_err={dd_err[:200]}",
            }

        # ═══ Usage accounting tests (DIR01-02) ═══

        # Reset quotas to generous limits for accounting tests
        nfs_exec("setquota", "-u", str(TEST_UID), "0", "102400", "0", "0", NFS_EXPORT_PATH)
        nfs_exec("setquota", "-u", str(TEST_UID2), "0", "102400", "0", "0", NFS_EXPORT_PATH)
        nfs_exec("setquota", "-g", str(TEST_GID), "0", "102400", "0", "0", NFS_EXPORT_PATH)

        # Clean up any previous test files for this UID
        nfs_exec("sh", "-c", f"rm -rf {server_testdir}/uid-test {server_testdir}/gid-test {server_testdir}/quota-exceed.dat")
        nfs_exec("mkdir", "-p", f"{server_testdir}/uid-test", f"{server_testdir}/gid-test")
        nfs_exec("chmod", "1777", f"{server_testdir}/uid-test", f"{server_testdir}/gid-test")

        # --- uid_usage_accounted ---
        # Write file AS TEST_UID so block allocation is attributed correctly
        nfs_exec(
            "sh", "-c",
            f"su -s /bin/sh isvquota1 -c '"
            f"dd if=/dev/zero of={server_testdir}/uid-test/file1.dat bs=1K count=512 2>/dev/null"
            f"'",
            timeout=15,
        )
        rc, repq_out, _ = nfs_exec("repquota", "-nup", NFS_EXPORT_PATH)
        uid_found = False
        uid1_usage = 0
        if rc == 0:
            for line in repq_out.splitlines():
                if str(TEST_UID) in line:
                    parts = line.split()
                    for p in parts:
                        try:
                            val = int(p)
                            if val > 0:
                                uid_found = True
                                uid1_usage = val
                                break
                        except ValueError:
                            continue
                    break
        if uid_found:
            result["tests"]["uid_usage_accounted"] = {
                "passed": True,
                "message": f"storage usage for UID {TEST_UID} tracked: {uid1_usage}K in quota accounting",
            }
        else:
            result["tests"]["uid_usage_accounted"] = {
                "passed": False,
                "message": f"UID {TEST_UID} usage not found in repquota: {repq_out[:300]}",
            }

        # --- gid_usage_accounted ---
        # Temporarily set isvquota1's primary group to TEST_GID so writes
        # are attributed to that GID in quota accounting
        nfs_exec("usermod", "-g", str(TEST_GID), "isvquota1")
        nfs_exec(
            "sh", "-c",
            f"su -s /bin/sh isvquota1 -c '"
            f"dd if=/dev/zero of={server_testdir}/gid-test/file1.dat bs=1K count=512 2>/dev/null"
            f"'",
            timeout=15,
        )
        # Restore primary group
        nfs_exec("usermod", "-g", str(TEST_UID), "isvquota1")
        rc, repq_out, _ = nfs_exec("repquota", "-ngp", NFS_EXPORT_PATH)
        gid_found = False
        gid_usage = 0
        if rc == 0:
            for line in repq_out.splitlines():
                if str(TEST_GID) in line:
                    parts = line.split()
                    for p in parts:
                        try:
                            val = int(p)
                            if val > 0:
                                gid_found = True
                                gid_usage = val
                                break
                        except ValueError:
                            continue
                    break
        if gid_found:
            result["tests"]["gid_usage_accounted"] = {
                "passed": True,
                "message": f"storage usage for GID {TEST_GID} tracked: {gid_usage}K in quota accounting",
            }
        else:
            result["tests"]["gid_usage_accounted"] = {
                "passed": False,
                "message": f"GID {TEST_GID} usage not found in repquota: {repq_out[:300]}",
            }

        # --- identity_usage_isolated ---
        # Write file AS TEST_UID2 (different UID) and verify separate tracking
        nfs_exec(
            "sh", "-c",
            f"su -s /bin/sh isvquota2 -c '"
            f"dd if=/dev/zero of={server_testdir}/uid-test/file2.dat bs=1K count=256 2>/dev/null"
            f"'",
            timeout=15,
        )
        rc, repq_out, _ = nfs_exec("repquota", "-nup", NFS_EXPORT_PATH)
        uid1_usage = 0
        uid2_usage = 0
        if rc == 0:
            for line in repq_out.splitlines():
                if str(TEST_UID) in line and str(TEST_UID2) not in line:
                    parts = line.split()
                    for p in parts:
                        try:
                            val = int(p)
                            if val > 0:
                                uid1_usage = val
                                break
                        except ValueError:
                            continue
                elif str(TEST_UID2) in line:
                    parts = line.split()
                    for p in parts:
                        try:
                            val = int(p)
                            if val > 0:
                                uid2_usage = val
                                break
                        except ValueError:
                            continue

        if uid1_usage > 0 and uid2_usage > 0:
            result["tests"]["identity_usage_isolated"] = {
                "passed": True,
                "message": (
                    f"usage tracked independently: UID {TEST_UID}={uid1_usage}K, "
                    f"UID {TEST_UID2}={uid2_usage}K"
                ),
            }
        else:
            result["tests"]["identity_usage_isolated"] = {
                "passed": False,
                "message": (
                    f"identity isolation not verified: "
                    f"UID {TEST_UID}={uid1_usage}K, UID {TEST_UID2}={uid2_usage}K"
                ),
            }

        result["success"] = all(t["passed"] for t in result["tests"].values())

    except Exception as e:
        result["error"] = str(e)
        result["error_type"] = type(e).__name__
    finally:
        disable_quotas()
        cleanup_test_files(tag)
        cleanup_namespace()

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
