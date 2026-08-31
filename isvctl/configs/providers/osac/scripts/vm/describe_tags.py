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

"""Retrieve tags (metadata labels) on a ComputeInstance.

OSAC does not have user-defined tags in the AWS sense. Instead, this
script extracts metadata fields from the ComputeInstance resource and
maps them to the tag contract expected by ``InstanceTagCheck``.

The ``Name`` and ``CreatedBy`` required tags are populated from the
instance's ``metadata.name`` and a fixed ``isvtest`` marker.
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
    parser = argparse.ArgumentParser(description="Describe ComputeInstance tags (OSAC)")
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--sa-token", required=True)
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "vm",
        "instance_id": args.instance_id,
        "tags": {},
        "tag_count": 0,
    }

    if DEMO_MODE:
        result["tags"] = {
            "Name": "isv-vm-test",
            "CreatedBy": "isvtest",
            "Template": "osac.templates.ocp_virt_vm",
        }
        result["tag_count"] = len(result["tags"])
        result["success"] = True
        print(json.dumps(result, indent=2))
        return 0

    try:
        config = get_env_config(require_admin=False)
        client = FulfillmentClient(config, args.sa_token)

        status, body = client.get_compute_instance(args.instance_id, token=args.sa_token)
        if status != 200 or not isinstance(body, dict):
            result["error"] = f"get ComputeInstance failed (HTTP {status}): {body}"
            print(json.dumps(result, indent=2))
            return 1

        # Map OSAC metadata to the tag contract
        metadata = body.get("metadata", {})
        spec = body.get("spec", {})
        tags: dict[str, str] = {
            "Name": metadata.get("name", ""),
            "CreatedBy": "isvtest",
        }
        template = spec.get("template", {})
        if isinstance(template, dict) and template.get("id"):
            tags["Template"] = template["id"]
        elif isinstance(template, str) and template:
            tags["Template"] = template

        instance_type = spec.get("instance_type", {})
        if isinstance(instance_type, dict) and instance_type.get("id"):
            tags["InstanceType"] = instance_type["id"]
        elif isinstance(instance_type, str) and instance_type:
            tags["InstanceType"] = instance_type

        result["tags"] = tags
        result["tag_count"] = len(tags)
        result["success"] = True

    except Exception as exc:
        result["error"] = str(exc)
        result["error_type"] = type(exc).__name__

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
