#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not use
# this file except in compliance with the License. You may obtain a copy of the
# License at
#
# http://www.apache.org/licenses/LICENSE-2.0

"""Query the target machine's rack/failure-domain placement."""

from __future__ import annotations

import os
from typing import Any

from common import NicoCommandError, emit, env_value, nico_json


def _first_string(mapping: dict[str, Any], *keys: str) -> str:
    """Return the first non-empty string field from a mapping."""
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def main() -> int:
    """Emit target placement as one provider-neutral failure domain."""
    result: dict[str, Any] = {
        "success": False,
        "platform": "nico",
        "site_id": os.environ.get("NICO_SITE_ID", "nvl72"),
        "test_name": "query_target_topology",
        "hosts_checked": 0,
        "hosts": [],
    }
    try:
        machine_id = env_value("NICO_MACHINE_ID")
        machine = nico_json("machine", "show", "--extended", machine_id)
        status = machine.get("status") if isinstance(machine.get("status"), dict) else {}
        placement = (
            machine.get("placement_in_rack")
            or machine.get("placementInRack")
            or status.get("placement_in_rack")
            or status.get("placementInRack")
            or {}
        )
        metadata = machine.get("metadata") or {}
        labels = metadata.get("labels") if isinstance(metadata, dict) else {}
        labels = labels if isinstance(labels, dict) else {}
        failure_domain = _first_string(
            placement,
            "rack_id",
            "rackId",
            "rack",
            "enclosure",
            "enclosure_id",
            "enclosureId",
        ) or _first_string(labels, "rack", "rack_id", "failure_domain", "failure-domain")
        result.update(
            {
                "success": True,
                "hosts_checked": 1,
                "hosts": [{"host_id": machine_id, "failure_domain": failure_domain}],
            }
        )
    except NicoCommandError as exc:
        result["error"] = str(exc)
        result["error_type"] = type(exc).__name__

    return emit(result)


if __name__ == "__main__":
    raise SystemExit(main())
