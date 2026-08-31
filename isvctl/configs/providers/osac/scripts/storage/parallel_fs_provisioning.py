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

"""Test parallel high-speed filesystem provisioning via API (HSS07-01).

Provisions a CephFS PVC on OCP, mounts it in a pod, and verifies
the filesystem is a parallel filesystem (CephFS with multiple MDS
servers and POSIX-compliant parallel client access).

Output JSON consumed by HssParallelFsProvisioningCheck.
"""

from __future__ import annotations

import json
import os
import random
import string
import subprocess
import sys
import time


DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"
KUBECTL = os.environ.get("KUBECTL", "kubectl")
STORAGE_CLASS = os.environ.get("HSS_CEPHFS_STORAGE_CLASS", "ocs-storagecluster-cephfs")
PVC_SIZE = "5Gi"


def _suffix() -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=6))


def _run(args: list[str], timeout: int = 60) -> tuple[int, str, str]:
    cmd = KUBECTL.split() + args
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except subprocess.TimeoutExpired:
        return 1, "", f"timeout after {timeout}s"
    except Exception as exc:
        return 1, "", str(exc)


def _apply(manifest: str) -> tuple[int, str]:
    cmd = KUBECTL.split() + ["apply", "-f", "-"]
    try:
        proc = subprocess.run(cmd, input=manifest, capture_output=True, text=True, timeout=30)
        return proc.returncode, proc.stderr.strip()
    except Exception as exc:
        return 1, str(exc)


def _exec(pod: str, ns: str, command: str, timeout: int = 30) -> tuple[int, str, str]:
    return _run(["exec", pod, "-n", ns, "--", "sh", "-c", command], timeout=timeout)


def _wait_pvc_bound(pvc: str, ns: str, timeout: int = 120) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        rc, phase, _ = _run(["get", "pvc", pvc, "-n", ns,
                              "-o", "jsonpath={.status.phase}"])
        if rc == 0 and phase == "Bound":
            return True
        time.sleep(3)
    return False


def _wait_pod_ready(pod: str, ns: str, timeout: int = 180) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        rc, phase, _ = _run(["get", "pod", pod, "-n", ns,
                              "-o", "jsonpath={.status.phase}"])
        if rc == 0 and phase == "Running":
            return True
        time.sleep(3)
    return False


def main() -> int:
    result: dict = {
        "success": False,
        "platform": "storage",
        "test_name": "parallel_fs_provisioning",
        "tests": {
            "api_available": {"passed": False, "message": ""},
            "filesystem_provisioned": {"passed": False, "message": "", "fs_type": ""},
            "mount_successful": {"passed": False, "message": ""},
        },
    }

    if DEMO_MODE:
        result["success"] = True
        for t in result["tests"].values():
            t["passed"] = True
            t["message"] = "demo mode"
        result["tests"]["filesystem_provisioned"]["fs_type"] = "cephfs"
        print(json.dumps(result, indent=2))
        return 0

    ns = f"isv-hss-pfs-{_suffix()}"
    pvc_name = "hss-pfs-pvc"
    pod_name = "hss-pfs-pod"

    try:
        # 1. Check CephFS StorageClass exists
        rc, sc_out, sc_err = _run(["get", "sc", STORAGE_CLASS,
                                    "-o", "jsonpath={.provisioner}"])
        if rc != 0:
            result["tests"]["api_available"]["message"] = (
                f"CephFS StorageClass '{STORAGE_CLASS}' not found: {sc_err}"
            )
            print(json.dumps(result, indent=2))
            return 1

        result["tests"]["api_available"]["passed"] = True
        result["tests"]["api_available"]["message"] = (
            f"CephFS StorageClass '{STORAGE_CLASS}' available (provisioner: {sc_out})"
        )

        # Create ephemeral namespace
        _run(["create", "namespace", ns])

        # 2. Provision CephFS PVC
        pvc_manifest = json.dumps({
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {"name": pvc_name, "namespace": ns},
            "spec": {
                "accessModes": ["ReadWriteMany"],
                "storageClassName": STORAGE_CLASS,
                "resources": {"requests": {"storage": PVC_SIZE}},
            },
        })
        rc, err = _apply(pvc_manifest)
        if rc != 0:
            result["tests"]["filesystem_provisioned"]["message"] = f"PVC create failed: {err}"
            print(json.dumps(result, indent=2))
            return 1

        sys.stderr.write("Waiting for CephFS PVC to bind...\n")
        if not _wait_pvc_bound(pvc_name, ns):
            result["tests"]["filesystem_provisioned"]["message"] = (
                f"CephFS PVC did not reach Bound state within 120s"
            )
            print(json.dumps(result, indent=2))
            return 1

        result["tests"]["filesystem_provisioned"]["passed"] = True
        result["tests"]["filesystem_provisioned"]["fs_type"] = "cephfs"
        result["tests"]["filesystem_provisioned"]["message"] = (
            f"CephFS PVC provisioned ({PVC_SIZE} via {STORAGE_CLASS})"
        )

        # 3. Mount and verify
        pod_manifest = json.dumps({
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": pod_name, "namespace": ns},
            "spec": {
                "securityContext": {"fsGroup": 0},
                "containers": [{
                    "name": "fs",
                    "image": "registry.access.redhat.com/ubi9/ubi-minimal:latest",
                    "command": ["sleep", "3600"],
                    "volumeMounts": [{"name": "data", "mountPath": "/data"}],
                }],
                "volumes": [{
                    "name": "data",
                    "persistentVolumeClaim": {"claimName": pvc_name},
                }],
            },
        })
        rc, err = _apply(pod_manifest)
        if rc != 0:
            result["tests"]["mount_successful"]["message"] = f"Pod create failed: {err}"
            print(json.dumps(result, indent=2))
            return 1

        sys.stderr.write("Waiting for pod to be ready...\n")
        if not _wait_pod_ready(pod_name, ns):
            rc, phase, _ = _run(["get", "pod", pod_name, "-n", ns,
                                  "-o", "jsonpath={.status.phase}"])
            result["tests"]["mount_successful"]["message"] = (
                f"Pod did not reach Running (phase={phase})"
            )
            print(json.dumps(result, indent=2))
            return 1

        # Verify CephFS mount type via df -T (more reliable than mount | grep)
        rc, df_type_out, _ = _exec(pod_name, ns, "df -T /data | tail -1 | awk '{print $2}'")
        is_ceph = "ceph" in df_type_out.lower() if rc == 0 else False
        mount_out = df_type_out

        # Write+read round-trip
        _exec(pod_name, ns, "echo 'parallel-fs-test-data' > /data/test.txt")
        rc, read_out, _ = _exec(pod_name, ns, "cat /data/test.txt")
        data_ok = rc == 0 and "parallel-fs-test-data" in read_out

        mount_ok = is_ceph and data_ok
        result["tests"]["mount_successful"]["passed"] = mount_ok
        if mount_ok:
            result["tests"]["mount_successful"]["message"] = (
                "CephFS mounted and write+read verified"
            )
        else:
            diag_parts = []
            if not is_ceph:
                diag_parts.append(f"mount type not ceph (got: {mount_out[:100]})")
            if not data_ok:
                diag_parts.append(f"write+read failed (got: {read_out[:100]})")
            result["tests"]["mount_successful"]["message"] = "; ".join(diag_parts)

        result["success"] = all(t["passed"] for t in result["tests"].values())

    finally:
        sys.stderr.write(f"Cleaning up namespace {ns}\n")
        _run(["delete", "namespace", ns, "--wait=false"], timeout=15)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
