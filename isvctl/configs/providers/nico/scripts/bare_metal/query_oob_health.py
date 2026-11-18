#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not use
# this file except in compliance with the License. You may obtain a copy of the
# License at
#
# http://www.apache.org/licenses/LICENSE-2.0

"""Query the target machine's read-only NICo health report."""

from __future__ import annotations

import os
from typing import Any

from common import NicoCommandError, emit, env_value, nico_json


def _probe_ids(health: dict[str, Any]) -> list[str]:
    """Extract probe identifiers when the deployed health API exposes them."""
    for key in ("probe_ids", "probes", "health_sources"):
        value = health.get(key)
        if isinstance(value, list):
            return [str(item) for item in value if isinstance(item, str)]
        if isinstance(value, dict):
            return [str(item) for item in value]
    return []


def main() -> int:
    """Emit OOB health evidence without mutating BMC or machine state."""
    result: dict[str, Any] = {
        "success": False,
        "platform": "nico",
        "site_id": os.environ.get("NICO_SITE_ID", "nvl72"),
        "test_name": "query_oob_health",
        "hosts_checked": 0,
        "hosts": [],
    }
    try:
        machine_id = env_value("NICO_MACHINE_ID")
        machine = nico_json("machine", "show", "--extended", machine_id)
        status = machine.get("status", {}) if isinstance(machine, dict) else {}
        health = status.get("health")
        if not isinstance(health, dict):
            raise NicoCommandError("NICo machine response did not include a health report")

        alerts = health.get("alerts") or []
        probes = _probe_ids(health)
        categories: dict[str, dict[str, Any]] = {}
        raw_categories = health.get("failure_categories")
        if isinstance(raw_categories, dict):
            categories = raw_categories

        host = {
            "host_id": machine_id,
            "oob_health_present": True,
            "bmc_probe_ids": probes,
            "failure_categories": categories,
            "alerts": alerts,
        }
        result.update({"success": True, "hosts_checked": 1, "hosts": [host]})
    except NicoCommandError as exc:
        result["error"] = str(exc)
        result["error_type"] = type(exc).__name__

    return emit(result)


if __name__ == "__main__":
    raise SystemExit(main())
