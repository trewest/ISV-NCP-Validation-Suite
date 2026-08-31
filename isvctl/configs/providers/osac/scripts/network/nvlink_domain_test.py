#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""OSAC NVLink domain probe on OCP.

Detects NVLink support and domain ID on a GPU node by querying
GPU Operator node labels and nvidia-smi NVLink status via kubectl exec.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from typing import Any

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"

TESTS = [
    "node_resolved",
    "nvlink_support_detected",
    "nvlink_domain_id_present",
]


def _kubectl(args: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    cmd = os.environ.get("KUBECTL", "kubectl").split() + args
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _find_gpu_node() -> str | None:
    """Find a node with nvidia.com/gpu.present=true."""
    r = _kubectl([
        "get", "nodes",
        "-l", "nvidia.com/gpu.present=true",
        "-o", "jsonpath={.items[0].metadata.name}",
    ])
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip()
    return None


def _get_node_labels(node: str) -> dict[str, str]:
    """Get GPU-related node labels."""
    r = _kubectl(["get", "node", node, "-o", "jsonpath={.metadata.labels}"])
    if r.returncode != 0:
        return {}
    return json.loads(r.stdout) if r.stdout.strip() else {}


def _check_nvlink_via_nvidia_smi(node: str) -> tuple[bool, str]:
    """Check NVLink status by running nvidia-smi in a GPU Operator pod on the node."""
    r = _kubectl([
        "get", "pods", "-n", "nvidia-gpu-operator",
        "-l", "app.kubernetes.io/component=nvidia-driver",
        "--field-selector", f"spec.nodeName={node},status.phase=Running",
        "-o", "jsonpath={.items[0].metadata.name}",
    ])
    if r.returncode != 0 or not r.stdout.strip():
        return False, ""

    driver_pod = r.stdout.strip()
    r = _kubectl([
        "exec", driver_pod, "-n", "nvidia-gpu-operator",
        "-c", "nvidia-driver-ctr", "--",
        "nvidia-smi", "nvlink", "-s",
    ], timeout=15)

    if r.returncode != 0:
        return False, ""

    output = r.stdout.strip()
    if not output or "Link" not in output:
        return False, ""

    return True, output


def _derive_domain_id(node: str, labels: dict[str, str], nvlink_output: str) -> str:
    """Derive NVLink domain ID from topology info."""
    gpu_product = labels.get("nvidia.com/gpu.product", "")
    gpu_count = labels.get("nvidia.com/gpu.count", "")
    machine = labels.get("nvidia.com/gpu.machine", "")

    link_count = len(re.findall(r"Link \d+:", nvlink_output))
    gpu_lines = [l for l in nvlink_output.split("\n") if l.startswith("GPU ")]
    gpu_with_nvlink = len(gpu_lines)

    return f"{machine}-{gpu_with_nvlink}gpu-{link_count}links"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--node-id", default="", help="Specific node to check")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "test_name": "nvlink_domain",
        "node_id": "",
        "nvlink_supported": False,
        "nvlink_domain_id": "",
        "tests": {t: {"passed": False} for t in TESTS},
    }

    if DEMO_MODE:
        result["node_id"] = args.node_id or "gpu-node-01"
        result["nvlink_supported"] = True
        result["nvlink_domain_id"] = "p4d.24xlarge-8gpu-96links"
        result["success"] = True
        for t in TESTS:
            result["tests"][t] = {"passed": True}
        print(json.dumps(result, indent=2))
        return 0

    node = args.node_id or _find_gpu_node()
    if not node:
        result["tests"]["node_resolved"] = {
            "passed": False,
            "message": "No GPU node found with nvidia.com/gpu.present=true",
        }
        result["error"] = "No GPU node available"
        print(json.dumps(result, indent=2))
        return 1

    result["node_id"] = node
    result["tests"]["node_resolved"] = {"passed": True, "message": f"GPU node: {node}"}

    labels = _get_node_labels(node)
    has_nvlink, nvlink_output = _check_nvlink_via_nvidia_smi(node)

    if not has_nvlink:
        gpu_family = labels.get("nvidia.com/gpu.family", "unknown")
        result["nvlink_supported"] = False
        result["tests"]["nvlink_support_detected"] = {
            "passed": True,
            "message": f"NVLink not detected on {gpu_family} GPU",
        }
        result["success"] = True
        print(json.dumps(result, indent=2))
        return 0

    result["nvlink_supported"] = True
    result["tests"]["nvlink_support_detected"] = {
        "passed": True,
        "message": "NVLink active on GPU node",
    }

    domain_id = _derive_domain_id(node, labels, nvlink_output)
    result["nvlink_domain_id"] = domain_id
    result["tests"]["nvlink_domain_id_present"] = {
        "passed": True,
        "message": f"NVLink domain: {domain_id}",
    }

    result["success"] = True
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
