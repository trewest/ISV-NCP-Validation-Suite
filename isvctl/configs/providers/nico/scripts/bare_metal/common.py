# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not use
# this file except in compliance with the License. You may obtain a copy of the
# License at
#
# http://www.apache.org/licenses/LICENSE-2.0

"""Shared read-only helpers for the NICo NVL72 provider scripts."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path
from typing import Any


class NicoCommandError(RuntimeError):
    """Raised when a read-only NICo or SSH command cannot be completed."""


def run_process(command: list[str], *, timeout: int = 120) -> tuple[int, str, str]:
    """Run a command without exposing its arguments or output on the console."""
    try:
        process = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        return 124, "", f"command timed out after {timeout}s: {exc.cmd[0]}"
    except OSError as exc:
        return 127, "", str(exc)
    return process.returncode, process.stdout.strip(), process.stderr.strip()


def nico_json(*args: str, timeout: int = 120) -> Any:
    """Run ``nico-admin-cli`` with JSON output and decode its response."""
    executable = shlex.split(os.environ.get("NICO_ADMIN_CLI", "nico-admin-cli"))
    command = executable + ["-f", "json", *args]
    return_code, stdout, stderr = run_process(command, timeout=timeout)
    if return_code != 0:
        raise NicoCommandError(stderr or f"nico-admin-cli exited with {return_code}")
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise NicoCommandError(f"nico-admin-cli returned non-JSON output: {exc}") from exc


def env_value(name: str, default: str | None = None) -> str:
    """Return a non-empty environment value or raise a concise configuration error."""
    value = os.environ.get(name, default)
    if not value:
        raise NicoCommandError(f"missing required environment variable: {name}")
    return value


def ssh_command(
    command: str,
    *,
    host: str,
    user: str,
    key_file: str,
    timeout: int = 60,
) -> tuple[int, str, str]:
    """Run a non-interactive SSH command using a local private key."""
    key_path = Path(key_file).expanduser()
    if not key_path.is_file():
        return 2, "", f"SSH key file not found: {key_path}"

    ssh = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=15",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-i",
        str(key_path),
        f"{user}@{host}",
        command,
    ]
    return run_process(ssh, timeout=timeout)


def emit(result: dict[str, Any]) -> int:
    """Print one structured result and return a process status."""
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("success") else 1


def skipped(test_name: str, reason: str) -> int:
    """Emit a structured skip for an unavailable optional telemetry surface."""
    return emit(
        {
            "success": True,
            "skipped": True,
            "skip_reason": reason,
            "platform": "nico",
            "test_name": test_name,
        }
    )
