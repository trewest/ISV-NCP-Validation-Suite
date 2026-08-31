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

"""Pre-create namespaces for image registry tests.

Waits for any terminating namespaces to finish before (re)creating them.
This runs as a setup step so the framework knows setup executed, which
is required for teardown to run.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time


DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"
KUBECTL = os.environ.get("KUBECTL", "kubectl")
TERMINATION_TIMEOUT = 120


def run_kubectl(*args: str, timeout: int = 60) -> tuple[int, str, str]:
    cmd = KUBECTL.split() + list(args)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except subprocess.TimeoutExpired:
        return 1, "", f"Command timed out after {timeout}s"
    except Exception as exc:
        return 1, "", str(exc)


def wait_ns_gone(ns: str) -> bool:
    rc, out, _ = run_kubectl("get", "namespace", ns, "-o", "jsonpath={.status.phase}")
    if rc != 0:
        return True
    if out != "Terminating":
        return True
    sys.stderr.write(f"Namespace {ns} is terminating, waiting...\n")
    deadline = time.time() + TERMINATION_TIMEOUT
    while time.time() < deadline:
        rc, _, _ = run_kubectl("get", "namespace", ns)
        if rc != 0:
            sys.stderr.write(f"Namespace {ns} terminated.\n")
            return True
        time.sleep(5)
    sys.stderr.write(f"Namespace {ns} still terminating after {TERMINATION_TIMEOUT}s\n")
    return False


def ensure_namespace(ns: str) -> tuple[bool, str]:
    if not wait_ns_gone(ns):
        return False, f"Namespace {ns} stuck in Terminating state"
    rc, _, _ = run_kubectl("get", "namespace", ns)
    if rc == 0:
        sys.stderr.write(f"Namespace {ns} already exists.\n")
        return True, ""
    rc, _, err = run_kubectl("create", "namespace", ns)
    if rc != 0:
        return False, f"Failed to create namespace {ns}: {err}"
    sys.stderr.write(f"Created namespace {ns}.\n")
    return True, ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Setup namespaces for image registry tests")
    parser.add_argument("--namespaces", nargs="+", default=["isv-image-crud-test", "isv-image-test"])
    args = parser.parse_args()

    result: dict = {
        "success": False,
        "platform": "image_registry",
        "test_name": "setup_namespaces",
        "namespaces": [],
        "errors": [],
    }

    if DEMO_MODE:
        result["success"] = True
        result["namespaces"] = args.namespaces
        print(json.dumps(result, indent=2))
        return 0

    for ns in args.namespaces:
        ok, err = ensure_namespace(ns)
        result["namespaces"].append(ns)
        if not ok:
            result["errors"].append(err)

    result["success"] = len(result["errors"]) == 0
    if result["errors"]:
        result["error"] = "; ".join(result["errors"])

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
