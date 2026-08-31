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

"""Query governance metrics from K8s node state.

Maps K8s node conditions and scheduling state to the provider-neutral
GovernanceMetricsCheck JSON format: Delivered / Healthy / Reserved / Active
counts for nodes and GPUs.

Outputs JSON consumed by GovernanceMetricsCheck (CAP01-01):
  success, platform, metrics.{delivered,healthy,reserved,active}.{nodes,gpus}
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"
KUBECTL = os.environ.get("KUBECTL", "kubectl")


def run_kubectl(*args: str, timeout: int = 60) -> tuple[int, str, str]:
    cmd = KUBECTL.split() + list(args)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except subprocess.TimeoutExpired:
        return 1, "", f"Command timed out after {timeout}s"
    except Exception as exc:
        return 1, "", str(exc)


def main() -> int:
    parser = argparse.ArgumentParser(description="Query governance metrics (OSAC)")
    parser.add_argument("--site-id", default="osac-dev")
    parser.parse_args()

    result: dict = {
        "success": False,
        "platform": "bare_metal",
        "test_name": "query_governance_metrics",
        "metrics": {},
    }

    if DEMO_MODE:
        result["success"] = True
        result["metrics"] = {
            "delivered": {"nodes": 6, "gpus": 0},
            "healthy": {"nodes": 6, "gpus": 0},
            "reserved": {"nodes": 6, "gpus": 0},
            "active": {"nodes": 6, "gpus": 0},
        }
        print(json.dumps(result, indent=2))
        return 0

    try:
        rc, nodes_json, err = run_kubectl("get", "nodes", "-o", "json")
        if rc != 0 or not nodes_json:
            result["error"] = f"Failed to get nodes: {err}"
            print(json.dumps(result, indent=2))
            return 1

        nodes = json.loads(nodes_json).get("items", [])
        if not nodes:
            result["error"] = "No nodes found"
            print(json.dumps(result, indent=2))
            return 1

        total = len(nodes)
        ready = 0
        schedulable = 0
        gpu_total = 0
        gpu_ready = 0

        for node in nodes:
            conditions = node.get("status", {}).get("conditions", [])
            capacity = node.get("status", {}).get("capacity", {})
            spec = node.get("spec", {})

            is_ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions)
            is_schedulable = not spec.get("unschedulable", False)

            gpu_count = 0
            gpu_str = capacity.get("nvidia.com/gpu", "0")
            try:
                gpu_count = int(gpu_str)
            except (ValueError, TypeError):
                pass

            if is_ready:
                ready += 1
                if gpu_count > 0:
                    gpu_ready += gpu_count
            if is_schedulable and is_ready:
                schedulable += 1
            if gpu_count > 0:
                gpu_total += gpu_count

        result["metrics"] = {
            "delivered": {"nodes": total, "gpus": gpu_total},
            "healthy": {"nodes": ready, "gpus": gpu_ready},
            "reserved": {"nodes": schedulable, "gpus": gpu_ready},
            "active": {"nodes": schedulable, "gpus": gpu_ready},
        }
        result["success"] = True

    except Exception as exc:
        result["error"] = str(exc)
        result["error_type"] = type(exc).__name__

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
