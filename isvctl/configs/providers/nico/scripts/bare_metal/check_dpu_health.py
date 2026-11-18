#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not use
# this file except in compliance with the License. You may obtain a copy of the
# License at
#
# http://www.apache.org/licenses/LICENSE-2.0

"""Collect read-only DPU health and agent-heartbeat evidence from NICo."""

from __future__ import annotations

import os
from typing import Any

from common import NicoCommandError, emit, env_value, nico_json, ssh_command


def _find_dpu(records: Any, dpu_id: str) -> dict[str, Any] | None:
    """Find one DPU status record by its machine identifier."""
    if not isinstance(records, list):
        return None
    for record in records:
        if isinstance(record, dict) and str(record.get("id", "")) == dpu_id:
            return record
    return None


def main() -> int:
    """Query one target DPU without changing its state or configuration."""
    result: dict[str, Any] = {
        "success": False,
        "platform": "nico",
        "site_id": os.environ.get("NICO_SITE_ID", "nvl72"),
        "test_name": "check_dpu_health",
        "machines_checked": 0,
        "machines": [],
    }
    try:
        machine_id = env_value("NICO_MACHINE_ID")
        dpu_id = env_value("NICO_DPU_MACHINE_ID")
        dpu_host = env_value("NVL72_DPU_HOST")
        dpu_user = os.environ.get("NVL72_DPU_SSH_USER", "ubuntu")
        dpu_key = env_value("NVL72_DPU_SSH_KEY_FILE")

        statuses = nico_json("dpu", "status", "--extended")
        status = _find_dpu(statuses, dpu_id)
        if status is None:
            raise NicoCommandError(f"DPU {dpu_id} was not returned by dpu status")

        code, stdout, stderr = ssh_command(
            "systemctl is-active --quiet forge-dpu-agent && printf active",
            host=dpu_host,
            user=dpu_user,
            key_file=dpu_key,
        )
        heartbeat = code == 0 and stdout.strip() == "active"
        healthy = str(status.get("healthy", "")).lower() in {"yes", "true"}
        alerts = [] if healthy else [{"id": "DpuHealth", "target": dpu_id, "message": status.get("healthy", "unknown")}]
        machine = {
            "machine_id": machine_id,
            "status": status.get("state", "unknown"),
            "dpu_count": 1,
            "dpu_capability": {"dpu_id": dpu_id, "dpu_type": status.get("dpu_type", "")},
            "health_summary": "healthy" if healthy else "unhealthy",
            "health_successes": ["DpuHealth"] + (["DpuAgentHeartbeat"] if heartbeat else []),
            "health_alerts": alerts,
            "dpu_agent_heartbeat": heartbeat,
        }
        result.update({"success": healthy and heartbeat, "machines_checked": 1, "machines": [machine]})
        if not heartbeat:
            result["error"] = stderr or "forge-dpu-agent is not active"
    except NicoCommandError as exc:
        result["error"] = str(exc)
        result["error_type"] = type(exc).__name__

    return emit(result)


if __name__ == "__main__":
    raise SystemExit(main())
