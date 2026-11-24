#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not use
# this file except in compliance with the License. You may obtain a copy of the
# License at
#
# http://www.apache.org/licenses/LICENSE-2.0

"""Exercise NICo VPC peering with disposable VPCs (SDN07-01).

The probe creates two empty VPCs, creates a same-tenant peering, waits for the
peering to become Ready, and removes the peering and VPCs in a finally block.
NICo automatically exchanges routes when a peering becomes active, but the
current REST API exposes no separate route-add/read operation. The route portion
is therefore reported as an explicit skipped subtest rather than guessed.
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
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.nico_client import NicoAuthError, forge_delete, forge_get, forge_post, resolve_auth

MUTATION_GUARD = "NICO_ALLOW_VPC_PEERING_MUTATION"
DEFAULT_TIMEOUT_SECONDS = 600
POLL_INTERVAL_SECONDS = 5
ROUTE_GAP = "Current NICo REST API exposes peering lifecycle/status but no separate route-add or route-observation API"


def _env(name: str) -> str:
    """Return a trimmed environment value."""
    return os.environ.get(name, "").strip()


def _timeout_seconds() -> int:
    """Return the bounded peering workflow timeout."""
    raw = _env("NICO_TEST_PEERING_TIMEOUT")
    if not raw:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS
    return value if value > 0 else DEFAULT_TIMEOUT_SECONDS


def _wait_status(
    org: str,
    path: str,
    token: str,
    *,
    base_url: str,
    wanted: set[str],
    timeout_seconds: int,
) -> dict[str, Any]:
    """Poll a NICo resource until it reaches one of the requested states."""
    deadline = time.monotonic() + timeout_seconds
    while True:
        resource = forge_get(org, path, token, base_url=base_url)
        if resource.get("status") in wanted:
            return resource
        if resource.get("status") == "Error":
            raise RuntimeError(f"{path} entered Error state")
        if time.monotonic() >= deadline:
            raise TimeoutError(f"{path} did not reach {sorted(wanted)} within {timeout_seconds}s")
        time.sleep(POLL_INTERVAL_SECONDS)


def _wait_deleted(org: str, path: str, token: str, *, base_url: str, timeout_seconds: int) -> bool:
    """Poll until a NICo resource returns 404."""
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            forge_get(org, path, token, base_url=base_url)
        except HTTPError as exc:
            if exc.code == 404:
                return True
            raise
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL_INTERVAL_SECONDS)


def _set_test(result: dict[str, Any], name: str, *, passed: bool, message: str = "", skipped: bool = False) -> None:
    """Set one provider-neutral VPC peering subtest."""
    entry: dict[str, Any] = {"passed": passed}
    if message:
        entry["message"] = message
    if skipped:
        entry["skipped"] = True
        entry["skip_reason"] = message
    result.setdefault("tests", {})[name] = entry


def _skip_tests(result: dict[str, Any], reason: str) -> None:
    """Populate all required peering tests as structured skips."""
    for name in ("create_vpc_a", "create_vpc_b", "create_peering", "accept_peering", "add_routes", "peering_active"):
        _set_test(result, name, passed=False, message=reason, skipped=True)


def _peering_disabled_error(exc: HTTPError) -> bool:
    """Return whether NICo rejected peering because the site policy disables it."""
    return "VPC peering is disabled at this site" in str(exc)


def main() -> int:
    """Run the guarded disposable VPC peering lifecycle."""
    parser = argparse.ArgumentParser(description="Exercise NICo VPC peering with disposable VPCs")
    parser.add_argument("--org", required=True)
    parser.add_argument("--site-id", required=True)
    parser.add_argument("--api-base", required=True)
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": True,
        "platform": "nico",
        "site_id": args.site_id,
        "tests": {},
        "cleanup_errors": [],
    }
    vpc_ids: list[str] = []
    peering_id = ""
    token = ""
    timeout_seconds = _timeout_seconds()

    if _env(MUTATION_GUARD) != "1":
        reason = f"VPC peering mutation disabled; set {MUTATION_GUARD}=1 for an explicitly approved run"
        result["skipped"] = True
        result["skip_reason"] = reason
        _skip_tests(result, reason)
        print(json.dumps(result, indent=2))
        return 0

    try:
        auth = resolve_auth()
        token = auth.token
        network_type = _env("NICO_TEST_PEERING_NETWORK_TYPE") or "FNN"
        name_prefix = _env("NICO_TEST_PEERING_NAME") or f"isvtest-peering-{uuid.uuid4().hex[:8]}"

        vpc_a = forge_post(
            args.org,
            "vpc",
            token,
            base_url=args.api_base,
            body={
                "id": str(uuid.uuid4()),
                "name": f"{name_prefix}-a",
                "siteId": args.site_id,
                "networkVirtualizationType": network_type,
            },
        )
        vpc_a_id = vpc_a.get("id")
        if not isinstance(vpc_a_id, str) or not vpc_a_id:
            raise RuntimeError("first disposable VPC create returned no id")
        vpc_ids.append(vpc_a_id)
        _wait_status(
            args.org,
            f"vpc/{vpc_a_id}",
            token,
            base_url=args.api_base,
            wanted={"Ready"},
            timeout_seconds=timeout_seconds,
        )
        result["vpc_a"] = {"id": vpc_a_id, "name": vpc_a.get("name", f"{name_prefix}-a")}
        _set_test(result, "create_vpc_a", passed=True, message=f"VPC {vpc_a_id} is Ready")

        vpc_b = forge_post(
            args.org,
            "vpc",
            token,
            base_url=args.api_base,
            body={
                "id": str(uuid.uuid4()),
                "name": f"{name_prefix}-b",
                "siteId": args.site_id,
                "networkVirtualizationType": network_type,
            },
        )
        vpc_b_id = vpc_b.get("id")
        if not isinstance(vpc_b_id, str) or not vpc_b_id:
            raise RuntimeError("second disposable VPC create returned no id")
        vpc_ids.append(vpc_b_id)
        _wait_status(
            args.org,
            f"vpc/{vpc_b_id}",
            token,
            base_url=args.api_base,
            wanted={"Ready"},
            timeout_seconds=timeout_seconds,
        )
        result["vpc_b"] = {"id": vpc_b_id, "name": vpc_b.get("name", f"{name_prefix}-b")}
        _set_test(result, "create_vpc_b", passed=True, message=f"VPC {vpc_b_id} is Ready")

        peering = forge_post(
            args.org,
            "vpc-peering",
            token,
            base_url=args.api_base,
            body={"vpc1Id": vpc_a_id, "vpc2Id": vpc_b_id, "siteId": args.site_id},
        )
        peering_id = peering.get("id") or ""
        if not peering_id:
            raise RuntimeError("VPC peering create returned no id")
        peering = _wait_status(
            args.org,
            f"vpc-peering/{peering_id}",
            token,
            base_url=args.api_base,
            wanted={"Ready"},
            timeout_seconds=timeout_seconds,
        )
        result["peering_id"] = peering_id
        _set_test(result, "create_peering", passed=True, message=f"Peering {peering_id} created")
        _set_test(
            result,
            "accept_peering",
            passed=True,
            message="NICo peering reached Ready without a separate accept operation",
        )
        _set_test(result, "peering_active", passed=True, message=f"Peering {peering_id} is Ready")
        _set_test(result, "add_routes", passed=False, message=ROUTE_GAP, skipped=True)
    except NicoAuthError as exc:
        result["success"] = False
        result["error_type"] = "auth"
        result["error"] = str(exc)
    except HTTPError as exc:
        if _peering_disabled_error(exc):
            reason = "VPC peering is disabled at this NICo site (site policy gap)"
            result["success"] = True
            result["skipped"] = True
            result["skip_reason"] = reason
            _skip_tests(result, reason)
        else:
            result["success"] = False
            result["error"] = f"HTTPError: {exc}"
    except Exception as exc:
        result["success"] = False
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if token:
            if peering_id:
                try:
                    forge_delete(args.org, f"vpc-peering/{peering_id}", token, base_url=args.api_base)
                    if not _wait_deleted(
                        args.org,
                        f"vpc-peering/{peering_id}",
                        token,
                        base_url=args.api_base,
                        timeout_seconds=timeout_seconds,
                    ):
                        result["cleanup_errors"].append(f"peering {peering_id} did not disappear")
                except Exception as exc:
                    result["cleanup_errors"].append(f"peering {peering_id}: {type(exc).__name__}: {exc}")

            for vpc_id in reversed(vpc_ids):
                try:
                    forge_delete(args.org, f"vpc/{vpc_id}", token, base_url=args.api_base)
                    if not _wait_deleted(
                        args.org, f"vpc/{vpc_id}", token, base_url=args.api_base, timeout_seconds=timeout_seconds
                    ):
                        result["cleanup_errors"].append(f"VPC {vpc_id} did not disappear")
                except Exception as exc:
                    result["cleanup_errors"].append(f"VPC {vpc_id}: {type(exc).__name__}: {exc}")

        if result["cleanup_errors"]:
            result["success"] = False

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
