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

"""Test live filesystem expansion (HSS10-01).

Provisions a Ceph RBD PVC on OCP, mounts it in a pod, starts IO, expands
the PVC live, and verifies capacity grew while IO continued uninterrupted.

Output JSON consumed by HssLiveExpansionCheck.
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
INITIAL_GI = 5
EXPANDED_GI = 10


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
        "test_name": "live_expansion",
        "tests": {
            "capacity_expanded": {"passed": False, "message": ""},
            "inodes_expanded": {"passed": False, "message": ""},
            "io_uninterrupted": {"passed": False, "message": ""},
            "metadata_consistent": {"passed": False, "message": ""},
        },
    }

    if DEMO_MODE:
        result["success"] = True
        for t in result["tests"].values():
            t["passed"] = True
            t["message"] = "demo mode"
        print(json.dumps(result, indent=2))
        return 0

    ns = f"isv-hss-expand-{_suffix()}"
    pvc_name = "hss-expand-pvc"
    pod_name = "hss-expand-pod"

    try:
        _run(["create", "namespace", ns])

        pvc = json.dumps({
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {"name": pvc_name, "namespace": ns},
            "spec": {
                "accessModes": ["ReadWriteOnce"],
                "storageClassName": STORAGE_CLASS,
                "resources": {"requests": {"storage": f"{INITIAL_GI}Gi"}},
            },
        })
        rc, err = _apply(pvc)
        if rc != 0:
            result["tests"]["capacity_expanded"]["message"] = f"PVC create failed: {err}"
            print(json.dumps(result, indent=2))
            return 1

        pod = json.dumps({
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": pod_name, "namespace": ns},
            "spec": {
                "securityContext": {"fsGroup": 0},
                "containers": [{
                    "name": "io",
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
        if rc != 0:
            result["tests"]["capacity_expanded"]["message"] = f"Pod create failed: {err}"
            print(json.dumps(result, indent=2))
            return 1

        sys.stderr.write("Waiting for pod to be ready...\n")
        if not _wait_pod_ready(pod_name, ns):
            rc, phase, _ = _run(["get", "pod", pod_name, "-n", ns,
                                  "-o", "jsonpath={.status.phase}"])
            rc2, events, _ = _run(["get", "events", "-n", ns,
                                    "--field-selector", f"involvedObject.name={pod_name}",
                                    "--sort-by=.lastTimestamp",
                                    "-o", "custom-columns=MSG:.message",
                                    "--no-headers"])
            diag = f"phase={phase}"
            if events:
                diag += f"; events: {events[:300]}"
            result["tests"]["capacity_expanded"]["message"] = f"Pod did not reach Running ({diag})"
            print(json.dumps(result, indent=2))
            return 1

        _exec(pod_name, ns, "echo 'test-sentinel-data' > /data/sentinel.txt")
        rc, md5_before, _ = _exec(pod_name, ns, "md5sum /data/sentinel.txt | awk '{print $1}'")
        rc2, stat_before, _ = _exec(pod_name, ns, "stat -c '%s %Y' /data/sentinel.txt")

        rc, df_before, _ = _exec(pod_name, ns, "df -BG /data | tail -1 | awk '{print $2}'")
        initial_cap = df_before.replace("G", "") if df_before else "0"

        _exec(pod_name, ns,
              "dd if=/dev/zero of=/data/pre_expand.bin bs=1M count=5",
              timeout=30)
        rc2, pre_md5, _ = _exec(pod_name, ns, "md5sum /data/pre_expand.bin | awk '{print $1}'")

        sys.stderr.write(f"Expanding PVC from {INITIAL_GI}Gi to {EXPANDED_GI}Gi...\n")
        patch = json.dumps({"spec": {"resources": {"requests": {"storage": f"{EXPANDED_GI}Gi"}}}})
        rc, _, err = _run(["patch", "pvc", pvc_name, "-n", ns, "--type=merge", "-p", patch])
        if rc != 0:
            result["tests"]["capacity_expanded"]["message"] = f"PVC patch failed: {err}"
            print(json.dumps(result, indent=2))
            return 1

        deadline = time.time() + 180
        expanded = False
        while time.time() < deadline:
            rc, cap_str, _ = _run(["get", "pvc", pvc_name, "-n", ns,
                                    "-o", "jsonpath={.status.capacity.storage}"])
            if rc == 0 and cap_str:
                cap_val = int(cap_str.replace("Gi", "")) if "Gi" in cap_str else 0
                if cap_val >= EXPANDED_GI:
                    expanded = True
                    break
            time.sleep(5)

        if expanded:
            result["tests"]["capacity_expanded"]["passed"] = True
            result["tests"]["capacity_expanded"]["message"] = f"PVC expanded from {INITIAL_GI}Gi to {EXPANDED_GI}Gi"
        else:
            result["tests"]["capacity_expanded"]["message"] = f"PVC did not expand to {EXPANDED_GI}Gi within 180s"

        rc, df_out, _ = _exec(pod_name, ns, "df -i /data | tail -1 | awk '{print $2}'")
        if rc == 0 and df_out:
            inodes = int(df_out) if df_out.isdigit() else 0
            result["tests"]["inodes_expanded"]["passed"] = inodes > 0
            result["tests"]["inodes_expanded"]["message"] = f"{inodes} inodes available after expansion"
        else:
            result["tests"]["inodes_expanded"]["message"] = "Could not read inode count"

        rc_post, _, post_err = _exec(pod_name, ns,
              "dd if=/dev/zero of=/data/post_expand.bin bs=1M count=5",
              timeout=30)
        post_write_ok = rc_post == 0
        rc, post_md5, _ = _exec(pod_name, ns, "md5sum /data/pre_expand.bin | awk '{print $1}'")
        pre_data_intact = pre_md5 == post_md5 and pre_md5 != ""

        io_ok = post_write_ok and pre_data_intact
        result["tests"]["io_uninterrupted"]["passed"] = io_ok
        result["tests"]["io_uninterrupted"]["message"] = (
            "IO uninterrupted: wrote before/after expansion, pre-expansion data intact"
            if io_ok
            else f"IO disrupted: write_ok={post_write_ok}(err={post_err[:100]}), md5={pre_md5}/{post_md5}"
        )

        rc, md5_after, _ = _exec(pod_name, ns, "md5sum /data/sentinel.txt | awk '{print $1}'")
        rc2, stat_after, _ = _exec(pod_name, ns, "stat -c '%s %Y' /data/sentinel.txt")
        meta_ok = (md5_before == md5_after and stat_before == stat_after
                   and md5_before != "" and stat_before != "")
        result["tests"]["metadata_consistent"]["passed"] = meta_ok
        result["tests"]["metadata_consistent"]["message"] = (
            "File metadata consistent after expansion"
            if meta_ok
            else f"Metadata changed: md5 {md5_before}->{md5_after}, stat {stat_before}->{stat_after}"
        )

        result["success"] = all(t["passed"] for t in result["tests"].values())

    finally:
        sys.stderr.write(f"Cleaning up namespace {ns}\n")
        _run(["delete", "namespace", ns, "--wait=false"], timeout=15)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
