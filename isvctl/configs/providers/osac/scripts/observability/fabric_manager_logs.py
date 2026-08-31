#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""OSAC Fabric Manager log availability probe on OCP.

Queries Fabric Manager logs from the GPU Operator's driver pod
(where nv-fabricmanager runs) via kubectl exec, or from dedicated
nvidia-fabricmanager pods if they exist.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"

TESTS = [
    "log_endpoint_reachable",
    "log_source_present",
    "log_entries_queryable",
]

GPU_OPERATOR_NS = "nvidia-gpu-operator"


def _kubectl(args: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    cmd = os.environ.get("KUBECTL", "kubectl").split() + args
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _get_fm_logs_from_driver_pod() -> tuple[list[str], str]:
    """Get FM logs from the driver daemonset pod's /var/log/fabricmanager.log."""
    r = _kubectl([
        "get", "pods", "-n", GPU_OPERATOR_NS,
        "-l", "app=nvidia-driver-daemonset",
        "--field-selector=status.phase=Running",
        "-o", "jsonpath={range .items[*]}{.metadata.name}{'\\n'}{end}",
    ])
    if r.returncode != 0 or not r.stdout.strip():
        r = _kubectl([
            "get", "pods", "-n", GPU_OPERATOR_NS,
            "--field-selector=status.phase=Running",
            "-o", "jsonpath={range .items[*]}{.metadata.name}{'\\n'}{end}",
        ])
        if r.returncode != 0:
            return [], ""
        pods = [p for p in r.stdout.strip().split("\n") if "driver" in p.lower()]
    else:
        pods = [p for p in r.stdout.strip().split("\n") if p]

    for pod in pods:
        for container in ["nvidia-driver-ctr", ""]:
            args = ["exec", pod, "-n", GPU_OPERATOR_NS]
            if container:
                args += ["-c", container]
            args += ["--", "tail", "-100", "/var/log/fabricmanager.log"]
            r = _kubectl(args, timeout=15)
            if r.returncode == 0 and r.stdout.strip():
                lines = [ln for ln in r.stdout.strip().split("\n") if ln.strip()]
                return lines, f"driver pod {pod}"
    return [], ""


def _get_fm_logs_from_fm_pods() -> tuple[list[str], str]:
    """Get FM logs from dedicated nvidia-fabricmanager pods."""
    r = _kubectl([
        "get", "pods", "-n", GPU_OPERATOR_NS,
        "-l", "app=nvidia-fabricmanager",
        "-o", "jsonpath={range .items[*]}{.metadata.name}{'\\n'}{end}",
    ])
    if r.returncode != 0 or not r.stdout.strip():
        return [], ""

    pods = [p for p in r.stdout.strip().split("\n") if p]
    all_lines: list[str] = []
    for pod in pods:
        r = _kubectl(["logs", pod, "-n", GPU_OPERATOR_NS, "--tail=100"], timeout=15)
        if r.returncode == 0:
            all_lines.extend(ln for ln in r.stdout.strip().split("\n") if ln.strip())
    return all_lines, f"{len(pods)} fabricmanager pod(s)"


def main() -> int:
    result: dict[str, Any] = {
        "success": False,
        "platform": "observability",
        "test_name": "fabric_manager_logs",
        "tests": {t: {"passed": False} for t in TESTS},
    }

    if DEMO_MODE:
        probes = {
            "log_source": "nvidia-fabricmanager (driver pod)",
            "log_endpoints_checked": 1,
            "entry_count": 48,
            "latest_timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        for t in TESTS:
            result["tests"][t] = {"passed": True, "probes": probes}
        result["success"] = True
        print(json.dumps(result, indent=2))
        return 0

    lines, source = _get_fm_logs_from_fm_pods()
    if not lines:
        lines, source = _get_fm_logs_from_driver_pod()

    probes: dict[str, Any] = {
        "log_source": f"nvidia-fabricmanager ({source})" if source else "nvidia-fabricmanager",
        "log_endpoints_checked": 1 if source else 0,
        "entry_count": len(lines),
        "latest_timestamp": "",
    }

    if not lines:
        result["tests"]["log_endpoint_reachable"] = {
            "passed": False,
            "message": "No Fabric Manager logs found — NVSwitch hardware may not be present",
        }
        result["error"] = "No Fabric Manager logs found in GPU Operator namespace"
        print(json.dumps(result, indent=2))
        return 1

    result["tests"]["log_endpoint_reachable"] = {
        "passed": True,
        "message": f"Fabric Manager logs found via {source}",
    }

    latest_ts = ""
    for line in reversed(lines):
        for part in line.split():
            try:
                dt = datetime.fromisoformat(part.rstrip("Z"))
                ts_str = dt.replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                if ts_str > latest_ts:
                    latest_ts = ts_str
                break
            except (ValueError, TypeError):
                continue
        if latest_ts:
            break

    probes["latest_timestamp"] = latest_ts

    result["tests"]["log_source_present"] = {"passed": True, "probes": probes}
    result["tests"]["log_entries_queryable"] = {"passed": True, "probes": probes}
    result["success"] = True

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
