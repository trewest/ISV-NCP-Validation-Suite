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

"""Teardown storage test resources by deleting the ephemeral namespace."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys


DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"
KUBECTL = os.environ.get("KUBECTL", "kubectl")


def main() -> int:
    parser = argparse.ArgumentParser(description="Teardown storage namespace (OSAC)")
    parser.add_argument("--namespace", required=True)
    args = parser.parse_args()

    result = {
        "success": False,
        "platform": "storage",
        "test_name": "teardown_storage",
    }

    if DEMO_MODE:
        result["success"] = True
        print(json.dumps(result, indent=2))
        return 0

    try:
        cmd = KUBECTL.split() + [
            "delete", "namespace", args.namespace,
            "--wait=false", "--ignore-not-found=true",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if proc.returncode == 0:
            result["success"] = True
        else:
            result["error"] = proc.stderr.strip()
    except Exception as exc:
        result["error"] = str(exc)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
