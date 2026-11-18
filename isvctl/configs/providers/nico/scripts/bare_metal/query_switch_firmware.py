#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not use
# this file except in compliance with the License. You may obtain a copy of the
# License at
#
# http://www.apache.org/licenses/LICENSE-2.0

"""Query compute-tray firmware versions through the NICo admin API."""

from __future__ import annotations

import os
from typing import Any

from common import NicoCommandError, emit, env_value, nico_json


def _walk(value: Any) -> list[dict[str, Any]]:
    """Return all nested JSON objects from a response."""
    objects: list[dict[str, Any]] = []
    if isinstance(value, dict):
        objects.append(value)
        for child in value.values():
            objects.extend(_walk(child))
    elif isinstance(value, list):
        for child in value:
            objects.extend(_walk(child))
    return objects


def _records(response: Any) -> list[dict[str, str]]:
    """Extract identifiable component/version pairs from the CLI response."""
    records: list[dict[str, str]] = []
    for obj in _walk(response):
        component = next(
            (str(obj[key]) for key in ("component", "component_name", "componentName", "name", "id") if obj.get(key)),
            "",
        )
        version = next(
            (
                str(obj[key])
                for key in ("firmware_version", "firmwareVersion", "version", "current_version", "currentVersion")
                if obj.get(key)
            ),
            "",
        )
        if not version and isinstance(obj.get("versions"), list):
            version = ", ".join(str(item) for item in obj["versions"] if item)
        if component and version:
            record = {"tray_id": component, "firmware_version": version}
            if record not in records:
                records.append(record)
    return records


def main() -> int:
    """Query firmware only; never queue an update."""
    result: dict[str, Any] = {
        "success": False,
        "platform": "nico",
        "site_id": os.environ.get("NICO_SITE_ID", "nvl72"),
        "test_name": "query_switch_firmware",
        "trays": [],
    }
    try:
        machine_id = env_value("NICO_MACHINE_ID")
        response = nico_json(
            "component-manager",
            "get-firmware-versions",
            "compute-tray",
            "--machine-id",
            machine_id,
            "--extended",
        )
        trays = _records(response)
        if not trays:
            raise NicoCommandError("NICo returned no identifiable compute-tray firmware records")
        result.update({"success": True, "trays": trays})
    except NicoCommandError as exc:
        result["error"] = str(exc)
        result["error_type"] = type(exc).__name__

    return emit(result)


if __name__ == "__main__":
    raise SystemExit(main())
