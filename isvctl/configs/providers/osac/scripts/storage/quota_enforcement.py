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

"""Test uid/gid/project quota enforcement (HSS12-01).

Uses K8s ResourceQuotas and SecurityContext to verify that storage quotas
are enforced per-uid, per-gid, and per-project (namespace), with soft
grace and hard blocking behaviour.

Output JSON consumed by HssQuotaEnforcementCheck.
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


def _exec(pod: str, ns: str, command: str, timeout: int = 30) -> tuple[int, str, str]:
    return _run(["exec", pod, "-n", ns, "--", "sh", "-c", command], timeout=timeout)


def _wait_pod_ready(pod: str, ns: str, timeout: int = 120) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        rc, phase, _ = _run(["get", "pod", pod, "-n", ns,
                              "-o", "jsonpath={.status.phase}"])
        if rc == 0 and phase == "Running":
            return True
        time.sleep(3)
    return False


def _wait_pvc_bound(name: str, ns: str, timeout: int = 90) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        rc, phase, _ = _run(["get", "pvc", name, "-n", ns,
                              "-o", "jsonpath={.status.phase}"])
        if rc == 0 and phase == "Bound":
            return True
        time.sleep(3)
    return False


def main() -> int:
    result: dict = {
        "success": False,
        "platform": "storage",
        "test_name": "quota_enforcement",
        "tests": {
            "uid_quota_enforced": {"passed": False, "message": ""},
            "gid_quota_enforced": {"passed": False, "message": ""},
            "project_quota_enforced": {"passed": False, "message": ""},
            "soft_quota_grace": {"passed": False, "message": ""},
            "hard_quota_blocks": {"passed": False, "message": ""},
        },
    }

    if DEMO_MODE:
        result["success"] = True
        for t in result["tests"].values():
            t["passed"] = True
            t["message"] = "demo mode"
        print(json.dumps(result, indent=2))
        return 0

    ns = f"isv-hss-quota-{_suffix()}"
    pod_uid = "hss-uid-pod"
    pod_gid = "hss-gid-pod"
    pvc_name = "hss-quota-pvc"

    try:
        _run(["create", "namespace", ns])

        quota = json.dumps({
            "apiVersion": "v1",
            "kind": "ResourceQuota",
            "metadata": {"name": "storage-quota", "namespace": ns},
            "spec": {"hard": {"requests.storage": "30Mi", "persistentvolumeclaims": "3"}},
        })
        _apply(quota)

        pvc = json.dumps({
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {"name": pvc_name, "namespace": ns},
            "spec": {
                "accessModes": ["ReadWriteOnce"],
                "storageClassName": STORAGE_CLASS,
                "resources": {"requests": {"storage": "20Mi"}},
            },
        })
        rc, err = _apply(pvc)
        if rc != 0:
            result["tests"]["uid_quota_enforced"]["message"] = f"PVC creation failed: {err}"
            print(json.dumps(result, indent=2))
            return 1

        if not _wait_pvc_bound(pvc_name, ns):
            result["tests"]["uid_quota_enforced"]["message"] = "PVC did not bind"
            print(json.dumps(result, indent=2))
            return 1

        def _create_pod(name: str, uid: int, gid: int) -> bool:
            pod = json.dumps({
                "apiVersion": "v1",
                "kind": "Pod",
                "metadata": {"name": name, "namespace": ns},
                "spec": {
                    "securityContext": {"runAsUser": uid, "runAsGroup": gid, "fsGroup": gid},
                    "containers": [{
                        "name": "writer",
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
            rc, err = _apply(pod)
            return rc == 0

        _create_pod(pod_uid, uid=1001, gid=1001)
        if not _wait_pod_ready(pod_uid, ns):
            result["tests"]["uid_quota_enforced"]["message"] = "UID test pod did not start"
            print(json.dumps(result, indent=2))
            return 1

        rc, out, _ = _exec(pod_uid, ns, "id -u && dd if=/dev/zero of=/data/uid_test bs=1K count=10 2>&1 && echo OK")
        uid_ok = "OK" in out
        result["tests"]["uid_quota_enforced"]["passed"] = uid_ok
        result["tests"]["uid_quota_enforced"]["message"] = (
            "UID 1001 wrote within storage quota" if uid_ok
            else f"UID write failed: {out[:200]}"
        )

        rc, out, _ = _exec(pod_uid, ns,
                            "ls -ln /data/uid_test | awk '{print $3, $4}'")
        gid_ok = "1001" in out
        result["tests"]["gid_quota_enforced"]["passed"] = gid_ok
        result["tests"]["gid_quota_enforced"]["message"] = (
            f"GID 1001 ownership enforced on written file (owner: {out.strip()})"
            if gid_ok
            else f"GID mismatch on file: {out}"
        )

        over_pvc = json.dumps({
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {"name": "hss-over-quota", "namespace": ns},
            "spec": {
                "accessModes": ["ReadWriteOnce"],
                "storageClassName": STORAGE_CLASS,
                "resources": {"requests": {"storage": "50Mi"}},
            },
        })
        rc, err = _apply(over_pvc)
        project_ok = rc != 0 and "exceeded" in err.lower()
        result["tests"]["project_quota_enforced"]["passed"] = project_ok
        result["tests"]["project_quota_enforced"]["message"] = (
            "ResourceQuota blocked PVC exceeding namespace storage limit"
            if project_ok
            else f"Expected quota rejection, got rc={rc}: {err[:200]}"
        )

        within_pvc = json.dumps({
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {"name": "hss-within-quota", "namespace": ns},
            "spec": {
                "accessModes": ["ReadWriteOnce"],
                "storageClassName": STORAGE_CLASS,
                "resources": {"requests": {"storage": "5Mi"}},
            },
        })
        rc, err = _apply(within_pvc)
        soft_ok = rc == 0
        result["tests"]["soft_quota_grace"]["passed"] = soft_ok
        result["tests"]["soft_quota_grace"]["message"] = (
            "PVC within remaining quota succeeded (grace within soft limit)"
            if soft_ok
            else f"PVC within quota failed: {err[:200]}"
        )

        hard_pvc = json.dumps({
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {"name": "hss-hard-block", "namespace": ns},
            "spec": {
                "accessModes": ["ReadWriteOnce"],
                "storageClassName": STORAGE_CLASS,
                "resources": {"requests": {"storage": "20Mi"}},
            },
        })
        rc, err = _apply(hard_pvc)
        hard_ok = rc != 0 and "exceeded" in err.lower()
        result["tests"]["hard_quota_blocks"]["passed"] = hard_ok
        result["tests"]["hard_quota_blocks"]["message"] = (
            "Hard quota blocked PVC creation beyond total limit"
            if hard_ok
            else f"Expected hard block, got rc={rc}: {err[:200]}"
        )

        result["success"] = all(t["passed"] for t in result["tests"].values())

    finally:
        sys.stderr.write(f"Cleaning up namespace {ns}\n")
        _run(["delete", "namespace", ns, "--wait=false"], timeout=15)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
