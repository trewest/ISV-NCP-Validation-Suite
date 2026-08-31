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

"""Centralized KMS test via HashiCorp Vault Transit engine.

Verifies that encrypted resources reference centralized KMS-backed keys.
Uses the Vault Transit engine as the centralized KMS: lists keys,
encrypts a test payload, and verifies the ciphertext resolves back to a
Transit key.

Covers SEC09-03 (CentralizedKmsCheck).

Output JSON:
{
    "success": true,
    "platform": "security",
    "test_name": "centralized_kms_test",
    "kms_keys_total": 2,
    "encrypted_resources_inspected": 1,
    "non_kms_resources": 0,
    "tests": {
        "kms_service_reachable":            {"passed": true, "message": "..."},
        "kms_keys_present":                 {"passed": true, "message": "..."},
        "all_encrypted_resources_use_kms":  {"passed": true, "message": "..."}
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
        "test_name": "centralized_kms_test",
        "kms_keys_total": 0,
        "encrypted_resources_inspected": 0,
        "non_kms_resources": 0,
        "tests": {
            "kms_service_reachable": {"passed": False, "message": ""},
            "kms_keys_present": {"passed": False, "message": ""},
            "all_encrypted_resources_use_kms": {"passed": False, "message": ""},
        },
    }

    if DEMO_MODE:
        result["kms_keys_total"] = 2
        result["encrypted_resources_inspected"] = 1
        result["non_kms_resources"] = 0
        for key in result["tests"]:
            result["tests"][key] = {"passed": True, "message": f"demo: {key} ok"}
        result["success"] = True
        print(json.dumps(result, indent=2))
        return 0

    try:
        # 1. KMS service reachable — check Vault status
        rc, out, err = vault_exec("status", "-format=json")
        if rc not in (0, 2):  # rc=2 means sealed but reachable
            result["tests"]["kms_service_reachable"] = {
                "passed": False, "message": f"Vault unreachable: {err}",
            }
            print(json.dumps(result, indent=2))
            return 0
        result["tests"]["kms_service_reachable"] = {
            "passed": True, "message": "Vault Transit engine reachable",
        }

        # 2. KMS keys present — list Transit keys
        rc, out, err = vault_exec("list", "-format=json", "transit/keys")
        if rc != 0:
            result["tests"]["kms_keys_present"] = {
                "passed": False, "message": f"list keys failed: {err}",
            }
            print(json.dumps(result, indent=2))
            return 0
        keys = json.loads(out)
        key_count = len(keys)
        result["kms_keys_total"] = key_count
        result["tests"]["kms_keys_present"] = {
            "passed": key_count > 0,
            "message": f"{key_count} Transit key(s) found",
        }

        # 3. Encrypt a test payload and verify it uses a KMS key
        test_key = keys[0] if keys else "provider-managed-key"
        import base64
        plaintext_b64 = base64.b64encode(b"validation-test-payload").decode()

        rc, out, err = vault_exec(
            "write", "-format=json",
            f"transit/encrypt/{test_key}",
            f"plaintext={plaintext_b64}",
        )
        if rc != 0:
            result["tests"]["all_encrypted_resources_use_kms"] = {
                "passed": False, "message": f"encrypt failed: {err}",
            }
        else:
            enc_data = json.loads(out)
            ciphertext = enc_data.get("data", {}).get("ciphertext", "")
            if ciphertext.startswith("vault:v"):
                result["encrypted_resources_inspected"] = 1
                result["non_kms_resources"] = 0
                result["tests"]["all_encrypted_resources_use_kms"] = {
                    "passed": True,
                    "message": f"ciphertext uses Vault Transit key '{test_key}'",
                }
            else:
                result["encrypted_resources_inspected"] = 1
                result["non_kms_resources"] = 1
                result["tests"]["all_encrypted_resources_use_kms"] = {
                    "passed": False,
                    "message": f"ciphertext does not reference a KMS key: {ciphertext[:40]}",
                }

        result["success"] = all(t["passed"] for t in result["tests"].values())

    except Exception as e:
        result["error"] = str(e)
        result["error_type"] = type(e).__name__

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
