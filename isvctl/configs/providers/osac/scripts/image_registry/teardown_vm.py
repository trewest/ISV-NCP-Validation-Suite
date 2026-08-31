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

"""Clean up VM, Service, and namespace created by launch_custom_image_vm.py."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys


DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"
KUBECTL = os.environ.get("KUBECTL", "kubectl")


def run_kubectl(*args: str, timeout: int = 120) -> tuple[int, str, str]:
    cmd = KUBECTL.split() + list(args)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except subprocess.TimeoutExpired:
        return 1, "", f"Command timed out after {timeout}s"
    except Exception as exc:
        return 1, "", str(exc)


def main() -> int:
    parser = argparse.ArgumentParser(description="Teardown custom image VM (OSAC)")
    parser.add_argument("--namespace", default="isv-image-test")
    parser.add_argument("--vm-name", default="isv-custom-image-vm")
    args = parser.parse_args()

    result: dict = {
        "success": False,
        "platform": "image_registry",
        "test_name": "teardown_vm",
        "cleanup_errors": [],
    }

    if DEMO_MODE:
        result["success"] = True
        print(json.dumps(result, indent=2))
        return 0

    rc, _, err = run_kubectl(
        "delete", "svc", f"{args.vm_name}-ssh", "-n", args.namespace,
        "--ignore-not-found=true",
    )
    if rc != 0:
        result["cleanup_errors"].append(f"Service delete: {err}")

    rc, _, err = run_kubectl(
        "delete", "vm", args.vm_name, "-n", args.namespace,
        "--wait=true", "--ignore-not-found=true",
    )
    if rc != 0:
        result["cleanup_errors"].append(f"VM delete: {err}")

    rc, _, err = run_kubectl(
        "delete", "namespace", args.namespace,
        "--wait=false", "--ignore-not-found=true",
    )
    if rc != 0:
        result["cleanup_errors"].append(f"Namespace delete: {err}")

    result["success"] = len(result["cleanup_errors"]) == 0
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
