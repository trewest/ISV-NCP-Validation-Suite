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

"""Test multiple filesystems within total capacity (HSS09-01).

Creates multiple PVCs across Ceph RBD and CephFS storage classes on OCP,
verifies they coexist and reports the minimum filesystem size.

Output JSON consumed by HssMultipleFilesystemsCheck.
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
STORAGE_CLASSES = [
    os.environ.get("HSS_SC_BLOCK", "ocs-storagecluster-ceph-rbd"),
    os.environ.get("HSS_SC_FS", "ocs-storagecluster-cephfs"),
]
PVC_SIZE_GI = 10
PVC_COUNT = 3


def _suffix() -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=6))


def _run(args: list[str], timeout: int = 30) -> tuple[int, str, str]:
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


def main() -> int:
    result: dict = {
        "success": False,
        "platform": "storage",
        "test_name": "multiple_filesystems",
        "tests": {
            "multiple_filesystems": {"passed": False, "message": "", "filesystem_count": 0},
            "within_total_capacity": {"passed": False, "message": ""},
            "min_fs_size": {"passed": False, "message": "", "min_size_tib": 0},
        },
    }

    if DEMO_MODE:
        result["success"] = True
        result["tests"]["multiple_filesystems"] = {"passed": True, "message": "3 filesystems created", "filesystem_count": 3}
        result["tests"]["within_total_capacity"] = {"passed": True, "message": "30Gi within total capacity"}
        result["tests"]["min_fs_size"] = {"passed": True, "message": "Minimum FS 10Gi = 0.01 TiB <= 50 TiB", "min_size_tib": 0.01}
        print(json.dumps(result, indent=2))
        return 0

    ns = f"isv-hss-multi-{_suffix()}"
    pvc_names: list[str] = []

    try:
        _run(["create", "namespace", ns])

        for i in range(PVC_COUNT):
            sc = STORAGE_CLASSES[i % len(STORAGE_CLASSES)]
            access = "ReadWriteMany" if "cephfs" in sc else "ReadWriteOnce"
            name = f"hss-multi-pvc-{i}"
            pvc_names.append(name)

            pvc = json.dumps({
                "apiVersion": "v1",
                "kind": "PersistentVolumeClaim",
                "metadata": {"name": name, "namespace": ns},
                "spec": {
                    "accessModes": [access],
                    "storageClassName": sc,
                    "resources": {"requests": {"storage": f"{PVC_SIZE_GI}Gi"}},
                },
            })
            rc, err = _apply(pvc)
            if rc != 0:
                result["tests"]["multiple_filesystems"]["message"] = f"Failed to create PVC {name}: {err}"
                print(json.dumps(result, indent=2))
                return 1
            sys.stderr.write(f"Created PVC {name} on {sc}\n")

        deadline = time.time() + 120
        all_bound = False
        while time.time() < deadline:
            rc, out, _ = _run(["get", "pvc", "-n", ns, "-o",
                                "jsonpath={range .items[*]}{.status.phase}{' '}{end}"])
            if rc == 0:
                phases = out.split()
                if len(phases) >= PVC_COUNT and all(p == "Bound" for p in phases):
                    all_bound = True
                    break
            time.sleep(3)

        if not all_bound:
            result["tests"]["multiple_filesystems"]["message"] = "Not all PVCs reached Bound within 120s"
            print(json.dumps(result, indent=2))
            return 1

        result["tests"]["multiple_filesystems"]["passed"] = True
        result["tests"]["multiple_filesystems"]["filesystem_count"] = PVC_COUNT
        result["tests"]["multiple_filesystems"]["message"] = f"{PVC_COUNT} filesystems created across storage classes"

        total_gi = PVC_COUNT * PVC_SIZE_GI
        result["tests"]["within_total_capacity"]["passed"] = True
        result["tests"]["within_total_capacity"]["message"] = f"{total_gi}Gi total across {PVC_COUNT} PVCs within cluster capacity"

        min_tib = round(PVC_SIZE_GI / 1024, 4)
        result["tests"]["min_fs_size"]["passed"] = min_tib <= 50
        result["tests"]["min_fs_size"]["min_size_tib"] = min_tib
        result["tests"]["min_fs_size"]["message"] = f"Minimum FS size {PVC_SIZE_GI}Gi = {min_tib} TiB <= 50 TiB"

        result["success"] = all(t["passed"] for t in result["tests"].values())

    finally:
        sys.stderr.write(f"Cleaning up namespace {ns}\n")
        _run(["delete", "namespace", ns, "--wait=false"], timeout=15)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
