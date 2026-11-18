#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not use
# this file except in compliance with the License. You may obtain a copy of the
# License at
#
# http://www.apache.org/licenses/LICENSE-2.0

"""Check the host's GPU health-monitoring process over SSH."""

from __future__ import annotations

import os
from typing import Any

from common import NicoCommandError, emit, env_value, ssh_command


def main() -> int:
    """Query DCGM/nv-hostengine process state without changing the host."""
    result: dict[str, Any] = {
        "success": False,
        "platform": "nico",
        "test_name": "query_node_health_agents",
        "agents_observable": True,
        "nodes_expected": 1,
        "agents": [],
    }
    try:
        host = env_value("NVL72_HOST")
        user = os.environ.get("NVL72_SSH_USER", "ubuntu")
        key_file = env_value("NVL72_SSH_KEY_FILE")
        code, stdout, stderr = ssh_command(
            "(systemctl is-active --quiet nvidia-dcgm || "
            "systemctl is-active --quiet dcgm || "
            "pgrep -x nv-hostengine >/dev/null) && printf active || printf inactive",
            host=host,
            user=user,
            key_file=key_file,
        )
        running = code == 0 and stdout.strip() == "active"
        result.update(
            {
                "success": True,
                "agents": [{"node_id": host, "agent_name": "NVIDIA DCGM", "running": running}],
            }
        )
        if code != 0:
            result["error"] = stderr or "health-agent query failed"
    except NicoCommandError as exc:
        result["error"] = str(exc)
        result["error_type"] = type(exc).__name__

    return emit(result)


if __name__ == "__main__":
    raise SystemExit(main())
