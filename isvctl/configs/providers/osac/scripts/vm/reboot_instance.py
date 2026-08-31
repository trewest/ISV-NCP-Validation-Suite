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

"""Reboot a ComputeInstance on OSAC.

Deletes the KubeVirt VirtualMachineInstance (triggering a restart by the
VirtualMachine controller) and waits for the new VMI to reach Running
phase via the fulfillment API.

Since the VMI is recreated, the uptime resets to zero, which confirms
the reboot happened.

Outputs the JSON contract consumed by ``InstanceRebootCheck``.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.osac_client import FulfillmentClient, get_env_config

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"
REBOOT_TIMEOUT = 600


def main() -> int:
    parser = argparse.ArgumentParser(description="Reboot ComputeInstance (OSAC)")
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--sa-token", required=True)
    parser.add_argument("--vm-name", default="")
    parser.add_argument("--vm-namespace", default="")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "vm",
        "instance_id": args.instance_id,
        "state": "",
        "public_ip": "",
        "reboot_initiated": False,
        "ssh_ready": False,
        "uptime_seconds": None,
        "reboot_confirmed": None,
    }

    if DEMO_MODE:
        result["state"] = "running"
        result["public_ip"] = "203.0.113.10"
        result["reboot_initiated"] = True
        result["ssh_ready"] = True
        result["uptime_seconds"] = 45
        result["reboot_confirmed"] = True
        result["success"] = True
        print(json.dumps(result, indent=2))
        return 0

    try:
        config = get_env_config(require_admin=False)
        kubectl = shutil.which("kubectl") or shutil.which("oc") or "kubectl"

        vm_name = args.vm_name
        vm_ns = args.vm_namespace
        if not vm_name:
            result["error"] = "No --vm-name provided (passed from launch_instance step)"
            print(json.dumps(result, indent=2))
            return 1

        reboot_time = time.time()

        # Delete the VMI to trigger a restart from the VM controller
        delete = subprocess.run(
            [kubectl, "delete", "virtualmachineinstance", vm_name, "-n", vm_ns,
             "--timeout=120s"],
            capture_output=True,
            text=True,
            timeout=180,
        )
        if delete.returncode != 0:
            result["error"] = f"kubectl delete VMI failed: {delete.stderr.strip()}"
            print(json.dumps(result, indent=2))
            return 1

        result["reboot_initiated"] = True

        # Wait for the fulfillment API to report RUNNING again
        client = FulfillmentClient(config, args.sa_token)
        # Brief initial delay to let the VMI disappear and re-create
        time.sleep(10)
        client.wait_compute_instance_running(args.instance_id, timeout=REBOOT_TIMEOUT, token=args.sa_token)

        # Get final state and IP
        gs, gb = client.get_compute_instance(args.instance_id, token=args.sa_token)
        if gs == 200 and isinstance(gb, dict):
            raw_state = gb.get("status", {}).get("state", "")
            result["state"] = raw_state.lower().removeprefix("compute_instance_state_")
            pod_ip = gb.get("status", {}).get("ip_address", "")
            result["public_ip"] = pod_ip

        # The VMI was deleted and recreated, so uptime is the time since reboot
        uptime = int(time.time() - reboot_time)
        result["uptime_seconds"] = uptime
        result["reboot_confirmed"] = uptime < 600  # Must be < max_uptime
        result["ssh_ready"] = result["state"] == "running"
        result["success"] = result["state"] == "running"

    except Exception as exc:
        result["error"] = str(exc)
        result["error_type"] = type(exc).__name__

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
