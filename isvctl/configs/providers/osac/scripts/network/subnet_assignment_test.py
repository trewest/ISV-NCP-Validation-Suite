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

"""Subnet assignment test for OSAC (SDN01-06).

Verifies that the VPC under test contains the expected subnet by
reading the subnet from the fulfillment API and confirming its
virtual_network reference matches the target VPC.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.osac_client import FulfillmentClient, create_sa_token, get_env_config

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    parser = argparse.ArgumentParser(description="Subnet assignment test (OSAC)")
    parser.add_argument("--region", required=True)
    parser.add_argument("--tenant-namespace", required=True)
    parser.add_argument("--target-vpc-id", required=True)
    parser.add_argument("--target-subnet-id", required=True)
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "test_name": "subnet_assignment",
        "vpc_id": args.target_vpc_id,
        "subnet_id": args.target_subnet_id,
        "tests": {
            "subnet_assigned": {"passed": False},
        },
    }

    if DEMO_MODE:
        result["tests"]["subnet_assigned"] = {
            "passed": True,
            "message": f"Subnet {args.target_subnet_id} belongs to VPC {args.target_vpc_id}",
        }
        result["success"] = True
        print(json.dumps(result, indent=2))
        return 0

    try:
        config = get_env_config(require_admin=False)
        token, _ttl = create_sa_token(args.tenant_namespace, "default")
        client = FulfillmentClient(config, token)

        status, body = client.get_subnet(args.target_subnet_id)
        if status != 200:
            result["tests"]["subnet_assigned"] = {
                "passed": False,
                "error": f"GET subnet failed (HTTP {status}): {body}",
            }
            print(json.dumps(result, indent=2))
            return 1

        vnet_ref = body.get("spec", {}).get("virtual_network", {})
        actual_vpc_id = vnet_ref.get("id", "") if isinstance(vnet_ref, dict) else str(vnet_ref)

        if actual_vpc_id == args.target_vpc_id:
            result["tests"]["subnet_assigned"] = {
                "passed": True,
                "message": f"Subnet {args.target_subnet_id} belongs to VPC {args.target_vpc_id}",
            }
        else:
            result["tests"]["subnet_assigned"] = {
                "passed": False,
                "error": (
                    f"Subnet {args.target_subnet_id} belongs to VPC {actual_vpc_id}, expected {args.target_vpc_id}"
                ),
            }

        result["success"] = all(t.get("passed") for t in result["tests"].values())

    except Exception as exc:
        result["error"] = str(exc)
        result["error_type"] = type(exc).__name__

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
