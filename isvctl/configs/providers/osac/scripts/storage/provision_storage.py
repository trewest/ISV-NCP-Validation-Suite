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

"""Test storage provisioning via K8s CSI API (HSS01-01).

Proves that a volume can be dynamically provisioned via the CSI StorageClass
API on OCP with Ceph storage. Creates a PVC, waits for it to bind, and
verifies the provisioned capacity matches the request.

Output JSON consumed by HssStorageProvisioningCheck.
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
STORAGE_CLASS = os.environ.get("HSS_STORAGE_CLASS", "ocs-storagecluster-ceph-rbd")
CAPACITY_GI = int(os.environ.get("HSS_CAPACITY_GI", "10"))


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


def _apply(manifest: str, timeout: int = 30) -> tuple[int, str]:
    cmd = KUBECTL.split() + ["apply", "-f", "-"]
    try:
        proc = subprocess.run(cmd, input=manifest, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stderr.strip()
    except Exception as exc:
        return 1, str(exc)


def main() -> int:
    result: dict = {
        "success": False,
        "platform": "storage",
        "test_name": "provision_storage",
        "tests": {
            "api_available": {"passed": False, "message": ""},
            "provisioned": {"passed": False, "message": ""},
            "capacity_matches": {"passed": False, "message": "", "capacity_gib": 0},
        },
    }

    if DEMO_MODE:
        result["success"] = True
        for t in result["tests"].values():
            t["passed"] = True
            t["message"] = "demo mode"
        result["tests"]["capacity_matches"]["capacity_gib"] = CAPACITY_GI
        print(json.dumps(result, indent=2))
        return 0

    ns = f"isv-hss-prov-{_suffix()}"
    pvc_name = "hss-test-pvc"

    try:
        _run(["create", "namespace", ns])

        rc, out, err = _run(["get", "sc", STORAGE_CLASS, "-o", "jsonpath={.provisioner}"])
        if rc != 0:
            result["tests"]["api_available"]["message"] = f"StorageClass {STORAGE_CLASS} not found: {err}"
            print(json.dumps(result, indent=2))
            return 1
        result["tests"]["api_available"]["passed"] = True
        result["tests"]["api_available"]["message"] = f"StorageClass {STORAGE_CLASS} available (provisioner: {out})"

        pvc_manifest = json.dumps({
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {"name": pvc_name, "namespace": ns},
            "spec": {
                "accessModes": ["ReadWriteOnce"],
                "storageClassName": STORAGE_CLASS,
                "resources": {"requests": {"storage": f"{CAPACITY_GI}Gi"}},
            },
        })
        rc, err = _apply(pvc_manifest)
        if rc != 0:
            result["tests"]["provisioned"]["message"] = f"PVC creation failed: {err}"
            print(json.dumps(result, indent=2))
            return 1

        deadline = time.time() + 90
        bound = False
        while time.time() < deadline:
            rc, phase, _ = _run(["get", "pvc", pvc_name, "-n", ns,
                                  "-o", "jsonpath={.status.phase}"])
            if rc == 0 and phase == "Bound":
                bound = True
                break
            time.sleep(3)

        if not bound:
            result["tests"]["provisioned"]["message"] = "PVC did not reach Bound within 90s"
            print(json.dumps(result, indent=2))
            return 1
        result["tests"]["provisioned"]["passed"] = True
        result["tests"]["provisioned"]["message"] = f"PVC {pvc_name} dynamically provisioned and Bound"

        rc, cap_str, _ = _run(["get", "pvc", pvc_name, "-n", ns,
                                "-o", "jsonpath={.status.capacity.storage}"])
        if rc == 0 and cap_str:
            cap_gi = int(cap_str.replace("Gi", "")) if "Gi" in cap_str else 0
            matches = cap_gi == CAPACITY_GI
            result["tests"]["capacity_matches"]["passed"] = matches
            result["tests"]["capacity_matches"]["capacity_gib"] = cap_gi
            result["tests"]["capacity_matches"]["message"] = (
                f"Capacity {cap_gi}Gi matches request {CAPACITY_GI}Gi"
                if matches
                else f"Capacity mismatch: got {cap_gi}Gi, requested {CAPACITY_GI}Gi"
            )
        else:
            result["tests"]["capacity_matches"]["message"] = "Could not read PVC capacity"

        result["success"] = all(t["passed"] for t in result["tests"].values())

    finally:
        sys.stderr.write(f"Cleaning up namespace {ns}\n")
        _run(["delete", "namespace", ns, "--wait=false"], timeout=15)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
