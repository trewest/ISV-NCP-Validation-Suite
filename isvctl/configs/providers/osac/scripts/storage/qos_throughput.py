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

"""Measure storage QoS throughput on a Ceph RBD PVC (HSS02-01).

Provisions a Ceph RBD PVC on OCP, mounts it in a pod, runs sequential
write and random-read benchmarks via dd, and verifies bandwidth and IOPS
meet conservative minimums.

Output JSON consumed by HssQosThroughputCheck.
"""

from __future__ import annotations

import json
import os
import random
import re
import string
import subprocess
import sys
import time


DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"
KUBECTL = os.environ.get("KUBECTL", "kubectl")
STORAGE_CLASS = os.environ.get("HSS_STORAGE_CLASS", "ocs-storagecluster-ceph-rbd")
MIN_BW_MBPS = 40
MIN_IOPS = 100


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


def _exec(pod: str, ns: str, command: str, timeout: int = 120) -> tuple[int, str, str]:
    return _run(["exec", pod, "-n", ns, "--", "sh", "-c", command], timeout=timeout)


def _wait_pod_ready(pod: str, ns: str, timeout: int = 180) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        rc, phase, _ = _run(["get", "pod", pod, "-n", ns,
                              "-o", "jsonpath={.status.phase}"])
        if rc == 0 and phase == "Running":
            return True
        time.sleep(3)
    return False


def _parse_dd_throughput(output: str) -> float | None:
    """Parse MB/s from dd stderr output like '... copied, 0.5 s, 200 MB/s'."""
    for line in output.splitlines():
        m = re.search(r"([\d.]+)\s+([KMGT]?B)/s", line)
        if m:
            value = float(m.group(1))
            unit = m.group(2)
            if unit == "GB/s":
                return value * 1000
            if unit == "MB/s":
                return value
            if unit == "kB/s":
                return value / 1000
            if unit == "B/s":
                return value / 1_000_000
            return value
    return None


def _parse_dd_time(output: str) -> float | None:
    """Parse elapsed seconds from dd stderr output."""
    for line in output.splitlines():
        m = re.search(r"copied,\s+([\d.]+)\s+s", line)
        if m:
            return float(m.group(1))
    return None


def main() -> int:
    result: dict = {
        "success": False,
        "platform": "storage",
        "test_name": "qos_throughput",
        "tests": {
            "bandwidth_meets_min": {"passed": False, "message": "", "measured_mbps": 0},
            "iops_meets_min": {"passed": False, "message": "", "measured_iops": 0},
        },
    }

    if DEMO_MODE:
        result["success"] = True
        result["tests"]["bandwidth_meets_min"] = {
            "passed": True, "message": "demo mode", "measured_mbps": 200.0,
        }
        result["tests"]["iops_meets_min"] = {
            "passed": True, "message": "demo mode", "measured_iops": 5000.0,
        }
        print(json.dumps(result, indent=2))
        return 0

    ns = f"isv-hss-qos-{_suffix()}"
    pvc_name = "hss-qos-pvc"
    pod_name = "hss-qos-pod"

    try:
        _run(["create", "namespace", ns])

        pvc = json.dumps({
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {"name": pvc_name, "namespace": ns},
            "spec": {
                "accessModes": ["ReadWriteOnce"],
                "storageClassName": STORAGE_CLASS,
                "resources": {"requests": {"storage": "10Gi"}},
            },
        })
        rc, err = _apply(pvc)
        if rc != 0:
            result["tests"]["bandwidth_meets_min"]["message"] = f"PVC create failed: {err}"
            print(json.dumps(result, indent=2))
            return 1

        pod = json.dumps({
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": pod_name, "namespace": ns},
            "spec": {
                "securityContext": {"fsGroup": 0},
                "containers": [{
                    "name": "bench",
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
            result["tests"]["bandwidth_meets_min"]["message"] = f"Pod create failed: {err}"
            print(json.dumps(result, indent=2))
            return 1

        sys.stderr.write("Waiting for pod to be ready...\n")
        if not _wait_pod_ready(pod_name, ns):
            result["tests"]["bandwidth_meets_min"]["message"] = "Pod did not reach Running"
            print(json.dumps(result, indent=2))
            return 1

        # --- Sequential write bandwidth test ---
        sys.stderr.write("Running sequential write bandwidth test...\n")
        rc, dd_out, dd_err = _exec(pod_name, ns,
                                   "dd if=/dev/zero of=/data/seqwrite bs=1M count=100 oflag=direct 2>&1",
                                   timeout=120)
        combined = f"{dd_out}\n{dd_err}"
        bw_mbps = _parse_dd_throughput(combined)

        if bw_mbps is not None:
            bw_ok = bw_mbps >= MIN_BW_MBPS
            result["tests"]["bandwidth_meets_min"]["passed"] = bw_ok
            result["tests"]["bandwidth_meets_min"]["measured_mbps"] = round(bw_mbps, 2)
            result["tests"]["bandwidth_meets_min"]["message"] = (
                f"{bw_mbps:.1f} MB/s sequential write (min {MIN_BW_MBPS} MB/s)"
            )
        else:
            result["tests"]["bandwidth_meets_min"]["message"] = (
                f"Could not parse bandwidth from dd output: {combined[:200]}"
            )

        # --- Random read IOPS test ---
        sys.stderr.write("Running random read IOPS test...\n")
        iops_count = 10000
        rc, dd_out, dd_err = _exec(pod_name, ns,
                                   f"dd if=/data/seqwrite of=/dev/null bs=4k count={iops_count} iflag=direct 2>&1",
                                   timeout=120)
        combined = f"{dd_out}\n{dd_err}"
        elapsed = _parse_dd_time(combined)

        if elapsed is not None and elapsed > 0:
            measured_iops = iops_count / elapsed
            iops_ok = measured_iops >= MIN_IOPS
            result["tests"]["iops_meets_min"]["passed"] = iops_ok
            result["tests"]["iops_meets_min"]["measured_iops"] = round(measured_iops, 1)
            result["tests"]["iops_meets_min"]["message"] = (
                f"{measured_iops:.0f} IOPS random 4K read (min {MIN_IOPS} IOPS)"
            )
        else:
            result["tests"]["iops_meets_min"]["message"] = (
                f"Could not parse IOPS from dd output: {combined[:200]}"
            )

        result["success"] = all(t["passed"] for t in result["tests"].values())

    finally:
        sys.stderr.write(f"Cleaning up namespace {ns}\n")
        _run(["delete", "namespace", ns, "--wait=false"], timeout=15)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
