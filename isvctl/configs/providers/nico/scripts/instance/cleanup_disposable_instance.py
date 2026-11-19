#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not use
# this file except in compliance with the License. You may obtain a copy of the
# License at
#
# http://www.apache.org/licenses/LICENSE-2.0

"""Best-effort cleanup for a runtime-created disposable NICo instance."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.nico_client import NicoAuthError, forge_delete, forge_get, resolve_auth

CREATE_GUARD = "NICO_ALLOW_DISPOSABLE_INSTANCE_CREATE"
RELEASE_GUARD = "NICO_ALLOW_RELEASE_FOR_REPAIR"
POLL_INTERVAL_SECONDS = 5
POLL_TIMEOUT_SECONDS = 300

REPAIR_ISSUE = {
    "category": "Hardware",
    "summary": "Validation suite disposable instance cleanup",
    "details": "Cleanup for a runtime-created disposable instance used by ReturnNodeMaintenanceCheck.",
}


def _env(name: str) -> str:
    """Return a trimmed environment value."""
    return os.environ.get(name, "").strip()


def _get_instance(org: str, instance_id: str, token: str, *, base_url: str) -> dict[str, Any]:
    """Return an instance or an empty object when it has already disappeared."""
    try:
        return forge_get(org, f"instance/{instance_id}", token, base_url=base_url)
    except HTTPError as exc:
        if exc.code == 404:
            return {}
        raise


def _wait_deleted(org: str, instance_id: str, token: str, *, base_url: str) -> bool:
    """Wait for the instance to disappear after cleanup."""
    deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
    while True:
        if not _get_instance(org, instance_id, token, base_url=base_url):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL_INTERVAL_SECONDS)


def main() -> int:
    """Release the runtime-created instance if it still exists."""
    parser = argparse.ArgumentParser(description="Clean up a disposable NICo instance")
    parser.add_argument("--org", required=True)
    parser.add_argument("--site-id", required=True)
    parser.add_argument("--api-base", required=True)
    parser.add_argument("--instance-id", default="")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": True,
        "platform": "nico",
        "site_id": args.site_id,
        "instance_id": args.instance_id,
        "deleted": False,
    }

    if not args.instance_id:
        result["skipped"] = True
        result["skip_reason"] = "no disposable instance id was produced"
        print(json.dumps(result, indent=2))
        return 0
    if _env(CREATE_GUARD) != "1" or _env(RELEASE_GUARD) != "1":
        result["skipped"] = True
        result["skip_reason"] = f"cleanup requires {CREATE_GUARD}=1 and {RELEASE_GUARD}=1"
        print(json.dumps(result, indent=2))
        return 0

    try:
        auth = resolve_auth()
        if not _get_instance(args.org, args.instance_id, auth.token, base_url=args.api_base):
            result["skipped"] = True
            result["skip_reason"] = "disposable instance is already deleted"
        else:
            forge_delete(
                args.org,
                f"instance/{args.instance_id}",
                auth.token,
                base_url=args.api_base,
                body={"machineHealthIssue": dict(REPAIR_ISSUE)},
            )
            result["deleted"] = _wait_deleted(args.org, args.instance_id, auth.token, base_url=args.api_base)
            if not result["deleted"]:
                result["success"] = False
                result["error"] = "cleanup was accepted but the disposable instance still exists"
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
