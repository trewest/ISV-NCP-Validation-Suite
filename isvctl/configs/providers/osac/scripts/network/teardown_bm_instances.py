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

"""Delete the two BareMetalInstances launched by launch_bm_instances."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.osac_client import FulfillmentClient, get_admin_token, get_env_config

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"
DELETE_TIMEOUT = 900


def _wait_deleted(client: FulfillmentClient, bmi_id: str, timeout: int) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        s, _ = client.get_bare_metal_instance(bmi_id)
        if s in (404, 410):
            return True
        time.sleep(10)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Teardown BMIs from network tests (OSAC)")
    parser.add_argument("--instance-a-id", required=True)
    parser.add_argument("--instance-b-id", required=True)
    parser.add_argument("--tenant-namespace", required=True)
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "test_name": "teardown_bm_instances",
        "deleted_a": False,
        "deleted_b": False,
    }

    if DEMO_MODE:
        result.update({"success": True, "deleted_a": True, "deleted_b": True})
        print(json.dumps(result, indent=2))
        return 0

    try:
        config = get_env_config(require_admin=True)
        token = get_admin_token(config)
        client = FulfillmentClient(config, token)

        # Issue both deletes first, then wait for both — avoids sequential timeout doubling.
        pending = []
        for attr, bmi_id in [("deleted_a", args.instance_a_id), ("deleted_b", args.instance_b_id)]:
            if not bmi_id:
                result[attr] = True
                continue
            s, _ = client.delete_bare_metal_instance(bmi_id)
            if s in (200, 202, 204, 404):
                pending.append((attr, bmi_id))
            else:
                result["error"] = f"DELETE {bmi_id} HTTP {s}"

        deadline = time.time() + DELETE_TIMEOUT
        for attr, bmi_id in pending:
            remaining = max(0, deadline - time.time())
            result[attr] = _wait_deleted(client, bmi_id, int(remaining))

        result["success"] = result["deleted_a"] and result["deleted_b"]

    except Exception as exc:
        result["error"] = str(exc)
        result["error_type"] = type(exc).__name__

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
