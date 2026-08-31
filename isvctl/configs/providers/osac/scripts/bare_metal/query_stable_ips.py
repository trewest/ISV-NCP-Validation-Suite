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

"""Query stable admin IPs from K8s node addresses.

Maps OCP/K8s node InternalIP addresses to the provider-neutral
StableStorageNodeIpCheck JSON format. Each node's InternalIP is
reported as its primary IP.

Outputs JSON consumed by StableStorageNodeIpCheck (STG03-01):
  success, platform, site_id, hosts_checked,
  hosts[].{host_id, hw_sku_device_type, primary_ip_addresses}
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
    parser = argparse.ArgumentParser(description="Query stable admin IPs (OSAC)")
    parser.add_argument("--site-id", default="osac-dev")
    args = parser.parse_args()

    result: dict = {
        "success": False,
        "platform": "bare_metal",
        "site_id": args.site_id,
        "test_name": "query_stable_ips",
        "hosts_checked": 0,
        "hosts": [],
    }

    if DEMO_MODE:
        result["success"] = True
        result["hosts_checked"] = 1
        result["hosts"] = [
            {
                "host_id": "demo-node-0",
                "hw_sku_device_type": "kvm-vm",
                "primary_ip_addresses": ["192.168.1.10"],
            }
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
            addresses = node.get("status", {}).get("addresses", [])

            ips = [addr["address"] for addr in addresses if addr.get("type") == "InternalIP"]

            instance_type = labels.get(
                "node.kubernetes.io/instance-type",
                labels.get("beta.kubernetes.io/instance-type", "baremetal"),
            )

            hosts.append(
                {
                    "host_id": name,
                    "hw_sku_device_type": instance_type,
                    "primary_ip_addresses": ips,
                }
            )

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
