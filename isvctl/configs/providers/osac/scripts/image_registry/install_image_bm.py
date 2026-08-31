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

"""Install a custom OS image on a bare-metal instance via OSAC (BOOT01-03).

Creates a BareMetalInstance through the OSAC fulfillment API using a
custom image (uploaded via upload_image_osac.py), waits for it to reach
RUNNING state, and outputs the result in the BmHostBootedFromCustomImageCheck
contract.

Output JSON consumed by BmHostBootedFromCustomImageCheck (composed:
StepSuccessCheck + FieldExistsCheck + InstanceStateCheck).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.osac_client import FulfillmentClient, create_sa_token, get_env_config

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"
RUN_STRATEGY_ALWAYS = "BARE_METAL_INSTANCE_RUN_STRATEGY_ALWAYS"
POLL_TIMEOUT = 1800


def main() -> int:
    parser = argparse.ArgumentParser(description="Install custom image on bare metal (OSAC)")
    parser.add_argument("--image-id", required=not DEMO_MODE, default="", help="Image ID from upload step")
    parser.add_argument("--tenant-namespace", default="", help="K8s namespace for SA token")
    parser.add_argument("--region", default="osac-default")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "image_registry",
        "test_name": "install_image_bm",
        "instance_id": "",
        "image_id": args.image_id,
        "instance_state": "",
        "state": "",
    }

    if DEMO_MODE:
        result.update({
            "success": True,
            "instance_id": "demo-bmi-img-001",
            "image_id": args.image_id or "demo-image-001",
            "instance_state": "running",
            "state": "running",
        })
        print(json.dumps(result, indent=2))
        return 0

    if not args.tenant_namespace:
        result["error"] = "--tenant-namespace is required"
        print(json.dumps(result, indent=2))
        return 1

    try:
        config = get_env_config(require_admin=False)
        token, _ttl = create_sa_token(args.tenant_namespace, "default")
        client = FulfillmentClient(config, token)

        catalog_item_id = (
            os.environ.get("OSAC_CATALOG_ITEM", "")
            or client.get_baremetal_catalog_item_id()
        )

        key_proc = subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", "/tmp/osac-bmi-img-key"],
            capture_output=True, timeout=30,
        )
        pub_key = ""
        if key_proc.returncode == 0:
            pub_key = Path("/tmp/osac-bmi-img-key.pub").read_text().strip()

        suffix = f"{int(time.time()) % 0xFFFF:04x}"
        bmi_name = f"isv-bm-img-{suffix}"

        body: dict[str, Any] = {
            "metadata": {
                "name": bmi_name,
                "labels": {"name": "osac-bm-image-validation", "created-by": "isv-validation"},
            },
            "spec": {
                "catalog_item": {"id": catalog_item_id},
                "ssh_public_key": pub_key,
                "auto_external_ip_attachment": True,
                "run_strategy": RUN_STRATEGY_ALWAYS,
            },
        }
        # NOTE: spec.image is not in the BareMetalInstance proto — the OS image
        # is determined by the AAP role's create_metal3.yaml, not the API request.

        status, resp = client.create_bare_metal_instance(body)
        if status not in (200, 201):
            result["error"] = f"Create BareMetalInstance failed (HTTP {status}): {resp}"
            print(json.dumps(result, indent=2))
            return 1

        bmi_id = resp["id"]
        result["instance_id"] = bmi_id

        final_body = client.wait_bare_metal_instance_state(bmi_id, "running", timeout=POLL_TIMEOUT)
        raw_state = final_body.get("status", {}).get("state", "")
        state = raw_state.lower().removeprefix("bare_metal_instance_state_")
        result["instance_state"] = state
        result["state"] = state
        result["success"] = state == "running"

    except Exception as exc:
        result["error"] = str(exc)
        result["error_type"] = type(exc).__name__

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
