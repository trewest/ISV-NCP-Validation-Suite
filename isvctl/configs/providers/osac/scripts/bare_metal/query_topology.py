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

"""Query failure-domain topology from K8s node labels.

Maps OCP/K8s node topology labels to the provider-neutral
FailureDomainObservabilityCheck JSON format. Tries well-known topology
labels first; falls back to the node hostname as a best-effort domain.

Outputs JSON consumed by FailureDomainObservabilityCheck (STG05-01):
  success, platform, site_id, hosts_checked,
  hosts[].{host_id, failure_domain}
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"
KUBECTL = os.environ.get("KUBECTL", "kubectl")

ZONE_LABELS = [
    "topology.kubernetes.io/zone",
    "failure-domain.beta.kubernetes.io/zone",
    "topology.kubernetes.io/region",
]


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
    parser = argparse.ArgumentParser(description="Query failure-domain topology (OSAC)")
    parser.add_argument("--site-id", default="osac-dev")
    args = parser.parse_args()

    result: dict = {
        "success": False,
        "platform": "bare_metal",
        "site_id": args.site_id,
        "test_name": "query_topology",
        "hosts_checked": 0,
        "hosts": [],
    }

    if DEMO_MODE:
        result["success"] = True
        result["hosts_checked"] = 2
        result["hosts"] = [
            {"host_id": "demo-node-0", "failure_domain": "rack-a"},
            {"host_id": "demo-node-1", "failure_domain": "rack-b"},
        ]
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

        hosts = []
        for node in nodes:
            name = node["metadata"]["name"]
            labels = node["metadata"].get("labels", {})

            domain = ""
            for label_key in ZONE_LABELS:
                if labels.get(label_key):
                    domain = labels[label_key]
                    break

            if not domain:
                domain = name

            hosts.append({"host_id": name, "failure_domain": domain})

        result["hosts_checked"] = len(hosts)
        result["hosts"] = hosts
        result["success"] = True

    except Exception as exc:
        result["error"] = str(exc)
        result["error_type"] = type(exc).__name__

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
