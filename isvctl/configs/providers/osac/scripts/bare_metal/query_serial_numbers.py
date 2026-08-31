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

"""Query hardware serial numbers from K8s nodes and BareMetalHost CRDs.

Combines node status (systemUUID, machineID) with BareMetalHost hardware
inventory (NIC MACs, CPU model) to produce per-machine component identifiers
for break/fix tracking.

Outputs JSON consumed by BmHardwareSerialCheck (BFX03-01):
  success, platform, site_id, machines_checked,
  machines[].{machine_id, components.{chassis,baseboard,cpu,gpu,nic}}
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"
KUBECTL = os.environ.get("KUBECTL", "kubectl")
BMH_NAMESPACE = os.environ.get("BMH_NAMESPACE", "openshift-machine-api")


def run_kubectl(*args: str, timeout: int = 60) -> tuple[int, str, str]:
    cmd = KUBECTL.split() + list(args)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except subprocess.TimeoutExpired:
        return 1, "", f"Command timed out after {timeout}s"
    except Exception as exc:
        return 1, "", str(exc)


def get_bmh_hardware() -> dict[str, dict]:
    """Return {bmh_name: hardware_dict} for all BareMetalHosts."""
    rc, out, _ = run_kubectl(
        "get",
        "baremetalhost",
        "-n",
        BMH_NAMESPACE,
        "-o",
        "json",
    )
    if rc != 0 or not out:
        return {}
    try:
        items = json.loads(out).get("items", [])
    except json.JSONDecodeError:
        return {}
    result = {}
    for item in items:
        name = item["metadata"]["name"]
        hw = item.get("status", {}).get("hardware", {})
        consumer = item.get("spec", {}).get("consumerRef", {})
        result[name] = {"hw": hw, "consumer": consumer}
    return result


def match_node_to_bmh(node_name: str, bmh_data: dict[str, dict]) -> dict | None:
    """Match a node to its BMH by consumer ref chain or name pattern."""
    for bmh_name, data in bmh_data.items():
        consumer = data.get("consumer", {})
        consumer_name = consumer.get("name", "")
        if node_name in consumer_name or consumer_name in node_name:
            return data.get("hw", {})
    for bmh_name, data in bmh_data.items():
        if node_name.replace("master-", "master-0-").replace("worker-", "worker-0-") == bmh_name:
            return data.get("hw", {})
        if bmh_name.startswith(node_name.split("-")[0]):
            pass
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Query hardware serial numbers (OSAC)")
    parser.add_argument("--site-id", default="osac-dev")
    args = parser.parse_args()

    result: dict = {
        "success": False,
        "platform": "bare_metal",
        "site_id": args.site_id,
        "test_name": "query_serial_numbers",
        "machines_checked": 0,
        "machines": [],
    }

    if DEMO_MODE:
        result["success"] = True
        result["machines_checked"] = 1
        result["machines"] = [
            {
                "machine_id": "demo-node-0",
                "components": {
                    "chassis": {"present": True, "identifiers": ["DEMO-UUID-001"]},
                    "baseboard": {"present": True, "identifiers": ["DEMO-MID-001"]},
                    "cpu": {"present": True, "identifiers": ["Intel Xeon Demo"]},
                    "gpu": {"present": False, "identifiers": []},
                    "nic": {"present": True, "identifiers": ["00:11:22:33:44:55"]},
                },
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

        bmh_data = get_bmh_hardware()
        sys.stderr.write(f"Found {len(bmh_data)} BareMetalHosts\n")

        machines = []
        for node in nodes:
            name = node["metadata"]["name"]
            node_info = node.get("status", {}).get("nodeInfo", {})
            capacity = node.get("status", {}).get("capacity", {})

            system_uuid = node_info.get("systemUUID", "")
            machine_id = node_info.get("machineID", "")

            bmh_hw = match_node_to_bmh(name, bmh_data)

            nic_macs = []
            cpu_model = ""
            if bmh_hw:
                nics = bmh_hw.get("nics", [])
                nic_macs = [n.get("mac", "") for n in nics if n.get("mac")]
                cpu_model = bmh_hw.get("cpu", {}).get("model", "")

            if not cpu_model:
                cpu_model = node_info.get("architecture", "unknown")

            gpu_count = 0
            try:
                gpu_count = int(capacity.get("nvidia.com/gpu", "0"))
            except (ValueError, TypeError):
                pass

            machine_entry = {
                "machine_id": name,
                "components": {
                    "chassis": {
                        "present": bool(system_uuid),
                        "identifiers": [system_uuid] if system_uuid else [],
                    },
                    "baseboard": {
                        "present": bool(machine_id),
                        "identifiers": [machine_id] if machine_id else [],
                    },
                    "cpu": {
                        "present": bool(cpu_model),
                        "identifiers": [cpu_model] if cpu_model else [],
                    },
                    "gpu": {
                        "present": gpu_count > 0,
                        "identifiers": [],
                    },
                    "nic": {
                        "present": bool(nic_macs),
                        "identifiers": nic_macs,
                    },
                },
            }
            machines.append(machine_entry)
            sys.stderr.write(
                f"  {name}: chassis={bool(system_uuid)} baseboard={bool(machine_id)} "
                f"cpu={bool(cpu_model)} nic={len(nic_macs)}\n"
            )

        result["machines_checked"] = len(machines)
        result["machines"] = machines
        result["success"] = True

    except Exception as exc:
        result["error"] = str(exc)
        result["error_type"] = type(exc).__name__

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
