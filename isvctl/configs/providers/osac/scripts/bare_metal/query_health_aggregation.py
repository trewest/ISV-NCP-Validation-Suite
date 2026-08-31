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

"""Aggregate per-host health into nodegroup-level groups.

Groups K8s nodes by their role labels (control-plane, worker, etc.) and
reports per-group health counts derived from K8s node conditions
(Ready, MemoryPressure, DiskPressure, PIDPressure).

Outputs JSON consumed by HealthAggregationCheck (CAP05-02):
  success, platform, site_id, aggregation_level,
  groups[].{group_id, group_type, name, total, healthy, unhealthy,
            status, unhealthy_hosts}
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


def node_is_healthy(node: dict) -> bool:
    conditions = node.get("status", {}).get("conditions", [])
    for cond in conditions:
        ctype = cond.get("type", "")
        cstatus = cond.get("status", "")
        if ctype == "Ready" and cstatus != "True":
            return False
        if ctype in ("MemoryPressure", "DiskPressure", "PIDPressure") and cstatus == "True":
            return False
    return True


def get_node_role(node: dict) -> str:
    labels = node.get("metadata", {}).get("labels", {})
    roles = []
    for key in sorted(labels):
        if key.startswith("node-role.kubernetes.io/"):
            roles.append(key.removeprefix("node-role.kubernetes.io/"))
    return ",".join(roles) if roles else "worker"


def main() -> int:
    parser = argparse.ArgumentParser(description="Health aggregation by nodegroup (OSAC)")
    parser.add_argument("--site-id", default="osac-dev")
    args = parser.parse_args()

    result: dict = {
        "success": False,
        "platform": "bare_metal",
        "site_id": args.site_id,
        "aggregation_level": "nodegroup",
        "groups": [],
    }

    if DEMO_MODE:
        result["groups"] = [{
            "group_id": "demo-control-plane",
            "group_type": "node_role",
            "name": "control-plane",
            "total": 2,
            "healthy": 2,
            "unhealthy": 0,
            "status": "Healthy",
            "unhealthy_hosts": [],
        }]
        result["success"] = True
        print(json.dumps(result, indent=2))
        return 0

    try:
        rc, nodes_json, err = run_kubectl("get", "nodes", "-o", "json")
        if rc != 0 or not nodes_json:
            result["error"] = f"Failed to get nodes: {err}"
            print(json.dumps(result, indent=2))
            return 1

        nodes_data = json.loads(nodes_json)
        items = nodes_data.get("items", [])
        if not items:
            result["error"] = "No nodes found"
            print(json.dumps(result, indent=2))
            return 1

        groups_map: dict[str, dict] = {}
        for node in items:
            name = node["metadata"]["name"]
            role = get_node_role(node)
            healthy = node_is_healthy(node)

            if role not in groups_map:
                groups_map[role] = {
                    "group_id": f"osac-{role}",
                    "group_type": "node_role",
                    "name": role,
                    "total": 0,
                    "healthy": 0,
                    "unhealthy": 0,
                    "unhealthy_hosts": [],
                }

            grp = groups_map[role]
            grp["total"] += 1
            if healthy:
                grp["healthy"] += 1
            else:
                grp["unhealthy"] += 1
                grp["unhealthy_hosts"].append(name)

        for grp in groups_map.values():
            grp["status"] = "Healthy" if grp["unhealthy"] == 0 else "Degraded"

        result["groups"] = list(groups_map.values())
        result["success"] = True

    except Exception as exc:
        result["error"] = str(exc)
        result["error_type"] = type(exc).__name__

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
