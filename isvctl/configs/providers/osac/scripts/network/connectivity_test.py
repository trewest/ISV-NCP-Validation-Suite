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

"""Network connectivity check (NetworkConnectivityCheck).

Reads the two BMI IPs from launch_bm_instances and emits an instances list.
NetworkConnectivityCheck passes as long as instances have IP addresses.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def main() -> int:
    parser = argparse.ArgumentParser(description="Network connectivity check (OSAC)")
    parser.add_argument("--external-ip-a", required=True)
    parser.add_argument("--external-ip-b", required=True)
    parser.add_argument("--instance-a-id", required=True)
    parser.add_argument("--instance-b-id", required=True)
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": True,
        "platform": "network",
        "test_name": "connectivity_test",
        "instances": [
            {"instance_id": args.instance_a_id, "public_ip": args.external_ip_a, "private_ip": args.external_ip_a},
            {"instance_id": args.instance_b_id, "public_ip": args.external_ip_b, "private_ip": args.external_ip_b},
        ],
    }

    if not args.external_ip_a or not args.external_ip_b:
        result["success"] = False
        result["error"] = "Missing external IPs from launch_bm_instances"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
