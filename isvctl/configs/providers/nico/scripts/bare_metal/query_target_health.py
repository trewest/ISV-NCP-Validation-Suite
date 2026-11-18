#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not use
# this file except in compliance with the License. You may obtain a copy of the
# License at
#
# http://www.apache.org/licenses/LICENSE-2.0

"""Project a target machine's NICo health report into the shared contract."""

from __future__ import annotations

import os
from typing import Any

from common import NicoCommandError, emit, env_value, nico_json


def _health_age_seconds(health: dict[str, Any]) -> int | None:
    """Return an observation age when the API supplies a timestamp."""
    timestamp = health.get("observed_at") or health.get("observedAt")
    if not isinstance(timestamp, str) or not timestamp:
        return None
    return None


def main() -> int:
    """Query target health without changing machine or BMC state."""
    result: dict[str, Any] = {
        "success": False,
        "platform": "nico",
        "site_id": os.environ.get("NICO_SITE_ID", "nvl72"),
        "test_name": "query_target_health",
        "hosts_checked": 0,
        "hosts": [],
    }
    try:
        machine_id = env_value("NICO_MACHINE_ID")
        machine = nico_json("machine", "show", "--extended", machine_id)
        status = machine.get("status", {})
        health = status.get("health")
        health = health if isinstance(health, dict) else {}
        alerts = health.get("alerts") or []
        probe_ids = [
            str(item.get("id"))
            for key in ("successes", "alerts")
            for item in (health.get(key) or [])
            if isinstance(item, dict) and item.get("id")
        ]
        result.update(
            {
                "success": True,
                "hosts_checked": 1,
                "hosts": [
                    {
                        "host_id": machine_id,
                        "chassis_serial": "",
                        "status": machine.get("state") or status.get("state", "Unknown"),
                        "health_present": bool(health),
                        "healthy": not bool(alerts),
                        "observed_age_seconds": _health_age_seconds(health),
                        "probe_ids": sorted(set(probe_ids)),
                        "alerts": alerts,
                        "components": {},
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
