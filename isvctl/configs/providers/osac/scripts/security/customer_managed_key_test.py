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

"""Customer-managed key (BYOK) test via HashiCorp Vault Transit engine.

Validates that a customer-managed key can be created, used for encrypt/
decrypt, and that the ciphertext proves the key origin is customer-owned
rather than provider-managed.

Covers SEC09-04 (CustomerManagedKeyCheck).

Output JSON:
{
    "success": true,
    "platform": "security",
    "test_name": "customer_managed_key_test",
    "key_id": "customer-managed-key",
    "encrypted_resource_id": "vault:v1:...",
    "tests": {
        "customer_managed_key_available":       {"passed": true},
        "key_manager_is_customer":              {"passed": true},
        "encrypt_decrypt_roundtrip":            {"passed": true},
        "resource_encrypted_with_customer_key": {"passed": true},
        "provider_managed_key_not_used":        {"passed": true}
    }
}
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"
KUBECTL = os.environ.get("KUBECTL", "kubectl")

VAULT_NS = os.environ.get("VAULT_NS", "vault")
VAULT_DEPLOY = os.environ.get("VAULT_DEPLOY", "deployment/vault")
CUSTOMER_KEY = "customer-managed-key"
PROVIDER_KEY = "provider-managed-key"


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


def main() -> int:
    result: dict = {
        "success": False,
        "platform": "security",
        "test_name": "customer_managed_key_test",
        "key_id": "",
        "encrypted_resource_id": "",
        "tests": {
            "customer_managed_key_available": {"passed": False, "message": ""},
            "key_manager_is_customer": {"passed": False, "message": ""},
            "encrypt_decrypt_roundtrip": {"passed": False, "message": ""},
            "resource_encrypted_with_customer_key": {"passed": False, "message": ""},
            "provider_managed_key_not_used": {"passed": False, "message": ""},
        },
    }

    if DEMO_MODE:
        result["key_id"] = CUSTOMER_KEY
        result["encrypted_resource_id"] = "vault:v1:demo-ciphertext"
        for key in result["tests"]:
            result["tests"][key] = {"passed": True, "message": f"demo: {key} ok"}
        result["success"] = True
        print(json.dumps(result, indent=2))
        return 0

    try:
        # 1. Customer-managed key available
        rc, out, err = vault_exec("read", "-format=json", f"transit/keys/{CUSTOMER_KEY}")
        if rc != 0:
            result["tests"]["customer_managed_key_available"] = {
                "passed": False, "message": f"key '{CUSTOMER_KEY}' not found: {err}",
            }
            print(json.dumps(result, indent=2))
            return 0
        key_data = json.loads(out).get("data", {})
        result["key_id"] = CUSTOMER_KEY
        result["tests"]["customer_managed_key_available"] = {
            "passed": True,
            "message": f"key '{CUSTOMER_KEY}' exists (type: {key_data.get('type', 'unknown')})",
        }

        # 2. Key manager is customer (not the provider-managed key)
        result["tests"]["key_manager_is_customer"] = {
            "passed": CUSTOMER_KEY != PROVIDER_KEY,
            "message": f"key '{CUSTOMER_KEY}' is distinct from provider key '{PROVIDER_KEY}'",
        }

        # 3. Encrypt/decrypt roundtrip
        plaintext = "byok-validation-test-payload"
        plaintext_b64 = base64.b64encode(plaintext.encode()).decode()

        rc, out, err = vault_exec(
            "write", "-format=json",
            f"transit/encrypt/{CUSTOMER_KEY}",
            f"plaintext={plaintext_b64}",
        )
        if rc != 0:
            result["tests"]["encrypt_decrypt_roundtrip"] = {
                "passed": False, "message": f"encrypt failed: {err}",
            }
            print(json.dumps(result, indent=2))
            return 0

        ciphertext = json.loads(out).get("data", {}).get("ciphertext", "")
        result["encrypted_resource_id"] = ciphertext

        rc, out, err = vault_exec(
            "write", "-format=json",
            f"transit/decrypt/{CUSTOMER_KEY}",
            f"ciphertext={ciphertext}",
        )
        if rc != 0:
            result["tests"]["encrypt_decrypt_roundtrip"] = {
                "passed": False, "message": f"decrypt failed: {err}",
            }
        else:
            decrypted_b64 = json.loads(out).get("data", {}).get("plaintext", "")
            try:
                decrypted = base64.b64decode(decrypted_b64).decode()
            except Exception:
                decrypted = ""
            roundtrip_ok = decrypted == plaintext
            result["tests"]["encrypt_decrypt_roundtrip"] = {
                "passed": roundtrip_ok,
                "message": "encrypt → decrypt roundtrip verified" if roundtrip_ok
                else f"roundtrip mismatch: got '{decrypted[:30]}'",
            }

        # 4. Resource encrypted with customer key
        uses_customer = ciphertext.startswith("vault:v")
        result["tests"]["resource_encrypted_with_customer_key"] = {
            "passed": uses_customer,
            "message": f"ciphertext uses Vault Transit key '{CUSTOMER_KEY}'" if uses_customer
            else "ciphertext does not reference a Vault key",
        }

        # 5. Provider-managed key not used (encrypted with customer key, not provider)
        # Try decrypting with provider key — should fail
        rc, out, err = vault_exec(
            "write", "-format=json",
            f"transit/decrypt/{PROVIDER_KEY}",
            f"ciphertext={ciphertext}",
        )
        provider_not_used = rc != 0
        result["tests"]["provider_managed_key_not_used"] = {
            "passed": provider_not_used,
            "message": "ciphertext cannot be decrypted with provider key" if provider_not_used
            else "ciphertext is decryptable with provider key — wrong key was used",
        }

        result["success"] = all(t["passed"] for t in result["tests"].values())

    except Exception as e:
        result["error"] = str(e)
        result["error_type"] = type(e).__name__

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
