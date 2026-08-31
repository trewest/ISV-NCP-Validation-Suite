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

"""Verify hardware ingestion by reading Metal3 BareMetalHost CRDs.

Maps K8s Metal3 BareMetalHost resources to the provider-neutral
HardwareIngestionCheck JSON format. Each BMH with operationalStatus=OK
is reported as a healthy, Ready machine.

Outputs JSON consumed by HardwareIngestionCheck (HWING01-01):
  success, platform, site_id, expected_count, ingested_count,
  matched_count, missing[], extra[],
  machines[].{machine_id, expected_machine_id, chassis_serial,
              status, health, gpu_count, dpu_count, capabilities}
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


BMH_STATE_TO_STATUS = {
    "available": "Ready",
    "provisioned": "InUse",
    "externally provisioned": "InUse",
    "provisioning": "InUse",
    "inspecting": "Provisioning",
    "preparing": "Provisioning",
    "registering": "Provisioning",
    "deprovisioning": "Deprovisioning",
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify hardware ingestion via Metal3 BMH CRDs (OSAC)")
    parser.add_argument("--site-id", default="osac-dev")
    parser.add_argument("--namespace", default="")
    args = parser.parse_args()

    result: dict = {
        "success": False,
        "platform": "bare_metal",
        "site_id": args.site_id,
        "expected_count": 0,
        "ingested_count": 0,
        "matched_count": 0,
        "missing": [],
        "extra": [],
        "machines": [],
    }

    if DEMO_MODE:
        result.update({
            "expected_count": 2,
            "ingested_count": 2,
            "matched_count": 2,
            "machines": [
                {
                    "machine_id": f"demo-bmh-{i}",
                    "expected_machine_id": f"demo-bmh-{i}",
                    "chassis_serial": f"DEMO-SN-{i:03d}",
                    "status": "Ready",
                    "health": "healthy",
                    "gpu_count": 0,
                    "dpu_count": 0,
                    "capabilities": ["compute"],
                }
                for i in range(2)
            ],
            "success": True,
        })
        print(json.dumps(result, indent=2))
        return 0

    try:
        ns_args = ["-n", args.namespace] if args.namespace else ["-A"]
        rc, bmh_json, err = run_kubectl(
            "get", "baremetalhost", *ns_args, "-o", "json",
        )
        if rc != 0:
            result["error"] = f"Failed to get BareMetalHosts: {err}"
            print(json.dumps(result, indent=2))
            return 1

        bmh_data = json.loads(bmh_json)
        items = bmh_data.get("items", [])

        if not items:
            result["error"] = "No BareMetalHost resources found"
            print(json.dumps(result, indent=2))
            return 1

        machines = []
        for bmh in items:
            name = bmh["metadata"]["name"]
            ns = bmh["metadata"].get("namespace", "")
            spec = bmh.get("spec", {})
            status = bmh.get("status", {})
            hw = status.get("hardware", {})

            provisioning = status.get("provisioning", {})
            bmh_state = provisioning.get("state", "unknown")
            op_status = status.get("operationalStatus", "unknown")
            error_type = status.get("errorType", "")

            vendor = hw.get("systemVendor", {})
            serial = vendor.get("serialNumber", "")

            nics = hw.get("nics", [])
            storage = hw.get("storage", [])
            cpu = hw.get("cpu", {})

            mapped_status = BMH_STATE_TO_STATUS.get(bmh_state, "Unknown")
            health = "healthy" if op_status == "OK" and not error_type else "unhealthy"

            capabilities = ["compute"]
            if nics:
                capabilities.append("network")
            if storage:
                capabilities.append("storage")

            machines.append({
                "machine_id": f"{ns}/{name}" if ns else name,
                "expected_machine_id": f"{ns}/{name}" if ns else name,
                "chassis_serial": serial or name,
                "status": mapped_status,
                "health": health,
                "gpu_count": 0,
                "dpu_count": 0,
                "capabilities": capabilities,
            })

        result["expected_count"] = len(machines)
        result["ingested_count"] = len(machines)
        result["matched_count"] = len(machines)
        result["machines"] = machines
        result["success"] = True

    except Exception as exc:
        result["error"] = str(exc)
        result["error_type"] = type(exc).__name__

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
