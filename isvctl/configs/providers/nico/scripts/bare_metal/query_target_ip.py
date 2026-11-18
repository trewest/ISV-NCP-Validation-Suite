#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not use
# this file except in compliance with the License. You may obtain a copy of the
# License at
#
# http://www.apache.org/licenses/LICENSE-2.0

"""Query the target machine's primary Admin-network IP."""

from __future__ import annotations

import os
from typing import Any

from common import NicoCommandError, emit, env_value, nico_json


def _records(value: Any) -> list[dict[str, Any]]:
    """Return interface records from either a bare list or wrapped response."""
    if isinstance(value, list):
        return [record for record in value if isinstance(record, dict)]
    if isinstance(value, dict):
        for key in ("interfaces", "machineInterfaces", "items"):
            if isinstance(value.get(key), list):
                return [record for record in value[key] if isinstance(record, dict)]
    return []


def main() -> int:
    """Emit only the target host's primary interface addresses."""
    result: dict[str, Any] = {
        "success": False,
        "platform": "nico",
        "site_id": os.environ.get("NICO_SITE_ID", "nvl72"),
        "test_name": "query_target_ip",
        "hosts_checked": 0,
        "hosts": [],
    }
    try:
        machine_id = env_value("NICO_MACHINE_ID")
        interfaces = _records(nico_json("machine-interfaces", "show", "--extended"))
        scoped = [
            item for item in interfaces if str(item.get("machine_id") or item.get("machineId") or "") == machine_id
        ]
        primary = [item for item in scoped if item.get("is_primary") or item.get("isPrimary")]
        selected = primary or scoped[:1]
        addresses: list[str] = []
        for interface in selected:
            values = interface.get("ip_addresses") or interface.get("ipAddresses") or interface.get("address") or []
            if isinstance(values, str):
                values = [values]
            for value in values:
                if isinstance(value, str) and value and value not in addresses:
                    addresses.append(value)

        result.update(
            {
                "success": True,
                "hosts_checked": 1,
                "hosts": [
                    {
                        "host_id": machine_id,
                        "hw_sku_device_type": "compute",
                        "primary_ip_addresses": addresses,
                    }
                ],
            }
        )
    except NicoCommandError as exc:
        result["error"] = str(exc)
        result["error_type"] = type(exc).__name__

    return emit(result)


if __name__ == "__main__":
    raise SystemExit(main())
