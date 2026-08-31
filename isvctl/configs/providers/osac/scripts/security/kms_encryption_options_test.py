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

"""KMS encryption options test via HashiCorp Vault Transit engine.

Verifies that both provider-managed and customer-managed KMS options are
available. Vault Transit engine provides a key management service where
the provider-managed key represents the platform default and the
customer-managed key represents a BYOK scenario.

Covers SEC09-02 (KmsEncryptionOptionCheck).

Output JSON:
{
    "success": true,
    "platform": "security",
    "test_name": "kms_encryption_options_test",
    "provider_managed_key_id": "provider-managed-key",
    "customer_managed_key_id": "customer-managed-key",
    "tests": {
        "provider_managed_key_available": {"passed": true, "message": "..."},
        "customer_managed_key_available": {"passed": true, "message": "..."},
        "both_options_supported":         {"passed": true, "message": "..."}
    }
}
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"
KUBECTL = os.environ.get("KUBECTL", "kubectl")

VAULT_NS = os.environ.get("VAULT_NS", "vault")
VAULT_DEPLOY = os.environ.get("VAULT_DEPLOY", "deployment/vault")
PROVIDER_KEY = "provider-managed-key"
CUSTOMER_KEY = "customer-managed-key"


def run_kubectl(*args: str, timeout: int = 60) -> tuple[int, str, str]:
    cmd = KUBECTL.split() + list(args)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except subprocess.TimeoutExpired:
        return 1, "", f"kubectl timed out after {timeout}s"
    except Exception as exc:
        return 1, "", str(exc)


def vault_exec(*vault_args: str, timeout: int = 30) -> tuple[int, str, str]:
    return run_kubectl(
        "exec", "-n", VAULT_NS, VAULT_DEPLOY, "--", "vault", *vault_args,
        timeout=timeout,
    )


def key_exists(key_name: str) -> tuple[bool, str]:
    rc, out, err = vault_exec("read", "-format=json", f"transit/keys/{key_name}")
    if rc != 0:
        return False, err
    try:
        data = json.loads(out)
        key_type = data.get("data", {}).get("type", "unknown")
        return True, f"key '{key_name}' exists (type: {key_type})"
    except json.JSONDecodeError:
        return False, f"unexpected vault output: {out[:100]}"


def main() -> int:
    result: dict = {
        "success": False,
        "platform": "security",
        "test_name": "kms_encryption_options_test",
        "provider_managed_key_id": "",
        "customer_managed_key_id": "",
        "tests": {
            "provider_managed_key_available": {"passed": False, "message": ""},
            "customer_managed_key_available": {"passed": False, "message": ""},
            "both_options_supported": {"passed": False, "message": ""},
        },
    }

    if DEMO_MODE:
        result["provider_managed_key_id"] = PROVIDER_KEY
        result["customer_managed_key_id"] = CUSTOMER_KEY
        for key in result["tests"]:
            result["tests"][key] = {"passed": True, "message": f"demo: {key} ok"}
        result["success"] = True
        print(json.dumps(result, indent=2))
        return 0

    try:
        # Check provider-managed key
        ok, msg = key_exists(PROVIDER_KEY)
        result["tests"]["provider_managed_key_available"] = {"passed": ok, "message": msg}
        if ok:
            result["provider_managed_key_id"] = PROVIDER_KEY

        # Check customer-managed key
        ok, msg = key_exists(CUSTOMER_KEY)
        result["tests"]["customer_managed_key_available"] = {"passed": ok, "message": msg}
        if ok:
            result["customer_managed_key_id"] = CUSTOMER_KEY

        # Both must be available
        both = (
            result["tests"]["provider_managed_key_available"]["passed"]
            and result["tests"]["customer_managed_key_available"]["passed"]
        )
        result["tests"]["both_options_supported"] = {
            "passed": both,
            "message": "both provider and customer managed keys available" if both
            else "one or both key types unavailable",
        }

        result["success"] = all(t["passed"] for t in result["tests"].values())

    except Exception as e:
        result["error"] = str(e)
        result["error_type"] = type(e).__name__

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
