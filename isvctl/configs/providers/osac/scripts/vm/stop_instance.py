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

"""Stop a ComputeInstance on OSAC.

OSAC ComputeInstances are backed by KubeVirt VirtualMachines. The
fulfillment API does not expose explicit stop/start operations, so this
script uses ``kubectl`` to patch the underlying VirtualMachine's
``spec.running`` to ``false`` and waits for the VMI to disappear
(indicating the VM has stopped).

The ComputeInstance's fulfillment state will transition to
``COMPUTE_INSTANCE_STATE_STOPPED`` once the osac-operator feedback
controller picks up the KubeVirt status change.

Outputs the JSON contract consumed by ``InstanceStopCheck``.
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
STOP_TIMEOUT = 300


def main() -> int:
    parser = argparse.ArgumentParser(description="Stop ComputeInstance (OSAC)")
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
        "stop_initiated": False,
    }

    if DEMO_MODE:
        result["state"] = "stopped"
        result["stop_initiated"] = True
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

        # Stop the VM by setting runStrategy to Halted
        patch = subprocess.run(
            [
                kubectl,
                "patch",
                "virtualmachine",
                vm_name,
                "-n",
                vm_ns,
                "--type",
                "merge",
                "-p",
                '{"spec":{"runStrategy":"Halted"}}',
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if patch.returncode != 0:
            result["error"] = f"kubectl patch VM failed: {patch.stderr.strip()}"
            print(json.dumps(result, indent=2))
            return 1

        result["stop_initiated"] = True

        # Wait for the fulfillment API to reflect STOPPED state
        client = FulfillmentClient(config, args.sa_token)
        deadline = time.time() + STOP_TIMEOUT
        while time.time() < deadline:
            gs, gb = client.get_compute_instance(args.instance_id, token=args.sa_token)
            if gs == 200 and isinstance(gb, dict):
                raw_state = gb.get("status", {}).get("state", "")
                state = raw_state.lower().removeprefix("compute_instance_state_")
                if state == "stopped":
                    result["state"] = "stopped"
                    result["success"] = True
                    break
            time.sleep(5)

        if not result["success"]:
            # Fall back to checking VMI disappearance (VMI goes away when stopped)
            vmi_probe = subprocess.run(
                [
                    kubectl,
                    "get",
                    "virtualmachineinstance",
                    vm_name,
                    "-n",
                    vm_ns,
                    "--no-headers",
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if vmi_probe.returncode != 0 or not vmi_probe.stdout.strip():
                # VMI is gone, the VM is stopped
                result["state"] = "stopped"
                result["success"] = True
            else:
                result["error"] = f"ComputeInstance {args.instance_id} did not reach stopped state within {STOP_TIMEOUT}s"

    except Exception as exc:
        result["error"] = str(exc)
        result["error_type"] = type(exc).__name__

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
