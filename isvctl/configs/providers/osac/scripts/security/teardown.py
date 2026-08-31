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

"""Security test teardown for OSAC.

Cleans up any residual resources created during the security test phase.
Individual test scripts perform their own best-effort cleanup, so this
teardown handles any resources that survived.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from typing import Any

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def _kubectl() -> str:
    path = shutil.which("kubectl") or shutil.which("oc")
    if not path:
        raise RuntimeError("Neither kubectl nor oc found on PATH")
    return path


def _cleanup_tenants(namespace: str) -> list[str]:
    """Delete any leftover ISV test tenants.

    Removes finalizers before deleting to avoid blocking on stuck
    controllers (e.g. storage controller without a configured backend).
    """
    errors: list[str] = []
    kctl = _kubectl()
    try:
        cmd = [
            kctl,
            "get",
            "tenants.osac.openshift.io",
            "-n",
            namespace,
            "-o",
            "jsonpath={.items[*].metadata.name}",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0 and result.stdout.strip():
            for name in result.stdout.strip().split():
                if name.startswith("isv-"):
                    try:
                        # Remove finalizers first to avoid hanging on
                        # controllers that cannot process deletion.
                        # Use JSON patch (not merge) — merge patch may be
                        # ignored on objects with a deletionTimestamp.
                        subprocess.run(
                            [
                                kctl,
                                "patch",
                                f"tenants.osac.openshift.io/{name}",
                                "-n",
                                namespace,
                                "--type",
                                "json",
                                "-p",
                                '[{"op":"remove","path":"/metadata/finalizers"}]',
                            ],
                            capture_output=True,
                            text=True,
                            timeout=10,
                        )
                        subprocess.run(
                            [
                                kctl,
                                "delete",
                                "tenants.osac.openshift.io",
                                name,
                                "-n",
                                namespace,
                                "--ignore-not-found",
                                "--wait=false",
                            ],
                            capture_output=True,
                            text=True,
                            timeout=15,
                        )
                    except Exception as e:
                        errors.append(f"tenant {name}: {e}")
    except Exception as e:
        errors.append(f"tenant listing: {e}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Security test teardown (OSAC)")
    parser.add_argument("--region", default="osac-default")
    parser.add_argument("--namespace", default="osac-e2e-ci")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": True,
        "platform": "security",
        "test_name": "teardown",
    }

    if DEMO_MODE:
        print(json.dumps(result, indent=2))
        return 0

    cleanup_errors: list[str] = []

    tenant_errors = _cleanup_tenants(args.namespace)
    cleanup_errors.extend(tenant_errors)

    if cleanup_errors:
        result["cleanup_errors"] = cleanup_errors

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
