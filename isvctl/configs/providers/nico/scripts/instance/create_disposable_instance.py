#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not use
# this file except in compliance with the License. You may obtain a copy of the
# License at
#
# http://www.apache.org/licenses/LICENSE-2.0

"""Create and wait for a runtime-configured disposable NICo instance.

This is an explicitly armed mutation step. It never discovers a target and does
not contain site-specific IDs. The instance request is assembled from
``NICO_TEST_*`` environment variables and the resulting instance ID is emitted
for later test/teardown steps.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.nico_client import NicoAuthError, forge_get, forge_post, resolve_auth

CREATE_GUARD = "NICO_ALLOW_DISPOSABLE_INSTANCE_CREATE"
RELEASE_GUARD = "NICO_ALLOW_RELEASE_FOR_REPAIR"
DEFAULT_TIMEOUT_SECONDS = 600
POLL_INTERVAL_SECONDS = 5


def _env(name: str) -> str:
    """Return a trimmed environment value."""
    return os.environ.get(name, "").strip()


def _guards_enabled() -> bool:
    """Require independent approval for creation and destructive release."""
    return _env(CREATE_GUARD) == "1" and _env(RELEASE_GUARD) == "1"


def _json_env(name: str, default: Any) -> Any:
    """Decode an optional JSON environment value."""
    value = _env(name)
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} must contain valid JSON: {exc.msg}") from exc


def build_instance_request() -> dict[str, Any]:
    """Build an InstanceCreateRequest from runtime-only environment values."""
    tenant_id = _env("NICO_TEST_TENANT_ID")
    vpc_id = _env("NICO_TEST_VPC_ID")
    machine_id = _env("NICO_TEST_MACHINE_ID")
    instance_type_id = _env("NICO_TEST_INSTANCE_TYPE_ID")

    missing = [name for name, value in (("NICO_TEST_TENANT_ID", tenant_id), ("NICO_TEST_VPC_ID", vpc_id)) if not value]
    if not machine_id and not instance_type_id:
        missing.append("NICO_TEST_MACHINE_ID or NICO_TEST_INSTANCE_TYPE_ID")
    if missing:
        raise ValueError(f"missing required disposable-instance setting(s): {', '.join(missing)}")

    request: dict[str, Any] = {
        "name": _env("NICO_TEST_INSTANCE_NAME") or f"isvtest-disposable-{uuid.uuid4().hex[:8]}",
        "tenantId": tenant_id,
        "vpcId": vpc_id,
    }
    if machine_id:
        request["machineId"] = machine_id
    else:
        request["instanceTypeId"] = instance_type_id

    operating_system_id = _env("NICO_TEST_OPERATING_SYSTEM_ID")
    if operating_system_id:
        request["operatingSystemId"] = operating_system_id

    ssh_key_group_ids = _json_env("NICO_TEST_SSH_KEY_GROUP_IDS", [])
    if not isinstance(ssh_key_group_ids, list) or not all(isinstance(item, str) and item for item in ssh_key_group_ids):
        raise ValueError("NICO_TEST_SSH_KEY_GROUP_IDS must be a JSON array of non-empty strings")
    if ssh_key_group_ids:
        request["sshKeyGroupIds"] = ssh_key_group_ids

    interfaces = _json_env("NICO_TEST_INTERFACES_JSON", None)
    if interfaces is not None:
        if not isinstance(interfaces, list):
            raise ValueError("NICO_TEST_INTERFACES_JSON must contain a JSON array")
        request["interfaces"] = interfaces
    elif _env("NICO_TEST_AUTO_NETWORK").lower() in {"1", "true", "yes"}:
        request["autoNetwork"] = True
    else:
        vpc_prefix_id = _env("NICO_TEST_VPC_PREFIX_ID")
        if not vpc_prefix_id:
            raise ValueError("set NICO_TEST_VPC_PREFIX_ID, NICO_TEST_INTERFACES_JSON, or NICO_TEST_AUTO_NETWORK=1")
        request["interfaces"] = [{"vpcPrefixId": vpc_prefix_id, "isPhysical": True}]

    return request


def _wait_for_machine(
    org: str,
    instance_id: str,
    token: str,
    *,
    base_url: str,
    timeout_seconds: int,
) -> dict[str, Any]:
    """Wait until NICo assigns a machine to the new instance."""
    deadline = time.monotonic() + timeout_seconds
    while True:
        instance = forge_get(org, f"instance/{instance_id}", token, base_url=base_url)
        status = instance.get("status", "Unknown")
        if instance.get("machineId"):
            return instance
        if status == "Error":
            raise RuntimeError(f"disposable instance {instance_id} entered Error state")
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"disposable instance {instance_id} was not assigned a machine within {timeout_seconds}s"
            )
        time.sleep(POLL_INTERVAL_SECONDS)


def main() -> int:
    """Create a guarded disposable instance and emit its runtime IDs."""
    parser = argparse.ArgumentParser(description="Create a guarded disposable NICo instance")
    parser.add_argument("--org", required=True)
    parser.add_argument("--site-id", required=True)
    parser.add_argument("--api-base", required=True)
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": True,
        "platform": "nico",
        "site_id": args.site_id,
        "created": False,
        "instance_id": "",
        "machine_id": "",
    }

    if not _guards_enabled():
        result["skipped"] = True
        result["skip_reason"] = (
            f"instance creation is disabled; set {CREATE_GUARD}=1 and {RELEASE_GUARD}=1 "
            "only for a disposable lifecycle test"
        )
        print(json.dumps(result, indent=2))
        return 0

    try:
        auth = resolve_auth()
        request = build_instance_request()
        instance = forge_post(args.org, "instance", auth.token, base_url=args.api_base, body=request)
        instance_id = instance.get("id")
        if not isinstance(instance_id, str) or not instance_id:
            raise RuntimeError("NICo instance create returned no instance id")

        result["created"] = True
        result["instance_id"] = instance_id
        current = _wait_for_machine(
            args.org,
            instance_id,
            auth.token,
            base_url=args.api_base,
            timeout_seconds=int(_env("NICO_TEST_INSTANCE_TIMEOUT") or DEFAULT_TIMEOUT_SECONDS),
        )
        result["machine_id"] = current.get("machineId", "")
        result["status"] = current.get("status", "Unknown")
    except NicoAuthError as exc:
        result["success"] = False
        result["error_type"] = "auth"
        result["error"] = str(exc)
    except Exception as exc:
        result["success"] = False
        result["error"] = f"{type(exc).__name__}: {exc}"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
