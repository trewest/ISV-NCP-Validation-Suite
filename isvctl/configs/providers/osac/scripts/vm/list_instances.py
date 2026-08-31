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

"""List ComputeInstances visible to the tenant SA token.

Outputs the JSON contract consumed by ``InstanceListCheck`` (via
``VmListedCheck``).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.osac_client import FulfillmentClient, get_env_config

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    parser = argparse.ArgumentParser(description="List ComputeInstances (OSAC)")
    parser.add_argument("--vpc-id", required=True)
    parser.add_argument("--instance-id", default="")
    parser.add_argument("--region", required=True)
    parser.add_argument("--sa-token", required=True)
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "vm",
        "instances": [],
        "count": 0,
        "found_target": False,
        "target_instance": args.instance_id,
    }

    if DEMO_MODE:
        target = args.instance_id or "demo-vm-0001"
        result["instances"] = [
            {
                "instance_id": target,
                "state": "running",
                "vpc_id": args.vpc_id,
            }
        ]
        result["count"] = 1
        result["found_target"] = True
        result["target_instance"] = target
        result["success"] = True
        print(json.dumps(result, indent=2))
        return 0

    try:
        config = get_env_config(require_admin=False)
        client = FulfillmentClient(config, args.sa_token)

        status, body = client.list_compute_instances(token=args.sa_token)
        if status != 200:
            result["error"] = f"list ComputeInstances failed (HTTP {status}): {body}"
            print(json.dumps(result, indent=2))
            return 1

        items = body.get("items", []) if isinstance(body, dict) else []
        for item in items:
            raw_state = item.get("status", {}).get("state", "")
            state = raw_state.lower().removeprefix("compute_instance_state_")
            iid = item.get("id", "")
            # Derive vpc_id from the first network attachment's subnet
            attachments = item.get("spec", {}).get("network_attachments", [])
            vpc = attachments[0].get("subnet", {}).get("id", "") if attachments else ""
            result["instances"].append({
                "instance_id": iid,
                "state": state,
                "vpc_id": vpc or args.vpc_id,
            })

        result["count"] = len(result["instances"])
        if args.instance_id:
            result["found_target"] = any(i["instance_id"] == args.instance_id for i in result["instances"])
        result["success"] = True

    except Exception as exc:
        result["error"] = str(exc)
        result["error_type"] = type(exc).__name__

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
