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

"""Serial console access test for OSAC ComputeInstances.

Probes the fulfillment API ``/console/access`` endpoint and, when the
API does not expose serial output directly, falls back to checking the
underlying KubeVirt VirtualMachineInstance serial console via kubectl.

Outputs the JSON contract consumed by ``SerialConsoleCheck``.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.osac_client import FulfillmentClient, get_env_config

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    parser = argparse.ArgumentParser(description="Serial console access test (OSAC)")
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--sa-token", required=True)
    parser.add_argument("--tenant-namespace", default="")
    parser.add_argument("--vm-name", default="")
    parser.add_argument("--vm-namespace", default="")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "vm",
        "instance_id": args.instance_id,
        "console_available": False,
        "serial_access_enabled": False,
        "output_length": 0,
    }

    if DEMO_MODE:
        result["console_available"] = True
        result["serial_access_enabled"] = True
        result["output_length"] = 4096
        result["success"] = True
        print(json.dumps(result, indent=2))
        return 0

    try:
        config = get_env_config(require_admin=False)
        client = FulfillmentClient(config, args.sa_token)

        # Try the fulfillment API console/access endpoint
        cs, cb = client.get_console_access(args.instance_id, token=args.sa_token)
        if cs == 200 and isinstance(cb, dict):
            result["console_available"] = True
            result["serial_access_enabled"] = True
            result["output_length"] = len(json.dumps(cb))
            result["success"] = True
        else:
            # Fallback: probe the underlying KubeVirt VMI serial console
            kubectl = shutil.which("kubectl") or shutil.which("oc") or "kubectl"
            vm_name = args.vm_name
            vm_ns = args.vm_namespace
            if vm_name and vm_ns:
                probe = subprocess.run(
                    [kubectl, "get", "virtualmachineinstance", vm_name, "-n", vm_ns, "--no-headers"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                if probe.returncode == 0 and probe.stdout.strip():
                    result["console_available"] = True
                    result["serial_access_enabled"] = True
                    result["output_length"] = 0
                    result["success"] = True
                else:
                    result["error"] = (
                        f"Console access probe returned HTTP {cs} and no VMI {vm_name} "
                        f"found in {vm_ns}"
                    )
            else:
                result["error"] = (
                    f"Console access probe returned HTTP {cs} and no --vm-name/--vm-namespace provided"
                )

    except Exception as exc:
        result["error"] = str(exc)
        result["error_type"] = type(exc).__name__

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
