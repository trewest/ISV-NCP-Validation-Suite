#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not use
# this file except in compliance with the License. You may obtain a copy of the
# License at
#
# http://www.apache.org/licenses/LICENSE-2.0

"""Project target-machine discovery data into stable hardware identifiers."""

from __future__ import annotations

import os
from typing import Any

from common import NicoCommandError, emit, env_value, nico_json


def _first_string(mapping: dict[str, Any], *keys: str) -> str:
    """Return the first non-empty string value from a mapping."""
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _identifiers(items: list[Any], *keys: str) -> list[str]:
    """Collect non-empty identifiers from a list of discovery records."""
    values: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        value = _first_string(item, *keys)
        if not value and isinstance(item.get("platform_info"), dict):
            value = _first_string(item["platform_info"], *keys)
        if value and value not in values:
            values.append(value)
    return values


def main() -> int:
    """Query target hardware inventory without running a hardware workload."""
    result: dict[str, Any] = {
        "success": False,
        "platform": "nico",
        "site_id": os.environ.get("NICO_SITE_ID", "nvl72"),
        "test_name": "query_target_serials",
        "machines_checked": 0,
        "machines": [],
    }
    try:
        machine_id = env_value("NICO_MACHINE_ID")
        machine = nico_json("machine", "show", "--extended", machine_id)
        status = machine.get("status", {})
        discovery = status.get("discovery_info", {})
        dmi = discovery.get("dmi_data", {})
        gpus = discovery.get("gpus") or []
        nics = discovery.get("network_interfaces") or []
        cpus = discovery.get("cpu_info") or []

        components = {
            "chassis": {
                "present": bool(_first_string(dmi, "chassis_serial", "product_serial")),
                "identifiers": [value for value in [_first_string(dmi, "chassis_serial", "product_serial")] if value],
            },
            "baseboard": {
                "present": bool(_first_string(dmi, "board_serial", "board_name")),
                "identifiers": [value for value in [_first_string(dmi, "board_serial", "board_name")] if value],
            },
            "cpu": {
                "present": bool(cpus),
                "identifiers": _identifiers(cpus, "model", "name", "serial"),
            },
            "gpu": {
                "present": bool(gpus),
                "identifiers": _identifiers(
                    gpus,
                    "uuid",
                    "gpu_uuid",
                    "serial",
                    "fabric_guid",
                ),
            },
            "nic": {
                "present": bool(nics),
                "identifiers": _identifiers(nics, "mac_address", "macAddress"),
            },
        }

        result.update(
            {
                "success": True,
                "machines_checked": 1,
                "machines": [{"machine_id": machine_id, "components": components}],
            }
        )
    except NicoCommandError as exc:
        result["error"] = str(exc)
        result["error_type"] = type(exc).__name__

    return emit(result)


if __name__ == "__main__":
    raise SystemExit(main())
