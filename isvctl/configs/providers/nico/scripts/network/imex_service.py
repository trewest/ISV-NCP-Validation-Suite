#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not use
# this file except in compliance with the License. You may obtain a copy of the
# License at
#
# http://www.apache.org/licenses/LICENSE-2.0

"""Read-only IMEX service and tooling probe for NICo hosts."""

from __future__ import annotations

import argparse
import importlib.util
import os
import shlex
from pathlib import Path
from typing import Any

DEFAULT_SSH_USER = "ubuntu"
DEFAULT_TIMEOUT_SECONDS = 30
SERVICE = "nvidia-imex.service"
DAEMON = "nvidia-imex"
CONTROL = "nvidia-imex-ctl"


def _load_helpers() -> tuple[Any, Any]:
    """Load NICo helpers by path so generic ``common`` modules cannot collide."""
    path = Path(__file__).resolve().parents[1] / "bare_metal" / "common.py"
    spec = importlib.util.spec_from_file_location("nico_bare_metal_common_for_imex", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load NICo helper module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.emit, module.ssh_command


emit, ssh_command = _load_helpers()


def _hosts(raw: str) -> list[str]:
    """Parse a comma-separated host list."""
    return [host.strip() for host in raw.split(",") if host.strip()]


def _probe_command() -> str:
    """Build a read-only shell probe with stable key/value output."""
    service = shlex.quote(SERVICE)
    daemon = shlex.quote(DAEMON)
    control = shlex.quote(CONTROL)
    return " ".join(
        [
            f"echo DAEMON=$(command -v {daemon} >/dev/null 2>&1 && echo yes || echo no);",
            f"echo CONTROL=$(command -v {control} >/dev/null 2>&1 && ({control} --version >/dev/null 2>&1 && echo yes || echo present_not_invocable) || echo no);",
            f"echo LOAD=$(systemctl show {service} -p LoadState --value 2>/dev/null);",
            f"echo BOOT=$(systemctl show {service} -p UnitFileState --value 2>/dev/null);",
            f"echo ACTIVE=$(systemctl is-active {service} 2>/dev/null || true);",
            "echo CONFIG=$(test -r /etc/nvidia-imex/config.cfg && echo yes || echo no);",
            "echo NODES_CONFIG=$(test -r /etc/nvidia-imex/nodes_config.cfg && echo yes || echo no);",
        ]
    )


def _parse_probe(output: str) -> dict[str, Any]:
    """Normalize probe output into the validation contract."""
    fields: dict[str, str] = {}
    for line in output.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            fields[key.strip()] = value.strip()

    load_state = fields.get("LOAD", "")
    registration = {
        "loaded": "loaded",
        "masked": "masked",
        "not-found": "not_found",
    }.get(load_state, "error")
    boot = fields.get("BOOT", "")
    boot_disposition = boot if boot in {"enabled", "disabled", "static"} else "none"

    return {
        "service_present": fields.get("DAEMON") == "yes",
        "control_tooling_present": fields.get("CONTROL") == "yes",
        "service_registration": registration,
        "service_running": fields.get("ACTIVE") == "active",
        "boot_disposition": boot_disposition,
        "config_present": fields.get("CONFIG") == "yes",
        "nodes_config_present": fields.get("NODES_CONFIG") == "yes",
    }


def main() -> int:
    """Probe configured IMEX hosts without changing their state."""
    parser = argparse.ArgumentParser(description="Read-only NICo IMEX service probe")
    parser.add_argument("--node-ids", default=os.environ.get("NICO_IMEX_HOSTS", ""))
    parser.add_argument("--key-file", default=os.environ.get("NICO_IMEX_SSH_KEY_FILE", ""))
    parser.add_argument("--ssh-user", default=os.environ.get("NICO_IMEX_SSH_USER", DEFAULT_SSH_USER))
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    args = parser.parse_args()

    hosts = _hosts(args.node_ids)
    result: dict[str, Any] = {
        "success": False,
        "platform": "nico",
        "test_name": "imex_service",
        "nodes": [],
        "nodes_checked": len(hosts),
        "nodes_validated": 0,
    }

    if not hosts and not args.key_file:
        result.update(
            {
                "success": True,
                "skipped": True,
                "skip_reason": "IMEX hosts and SSH key are not configured for this run",
            }
        )
        return emit(result)
    if not hosts:
        result["error"] = "--node-ids or NICO_IMEX_HOSTS is required"
        return emit(result)
    if not args.key_file:
        result["error"] = "--key-file or NICO_IMEX_SSH_KEY_FILE is required"
        return emit(result)

    probe = _probe_command()
    for host in hosts:
        code, stdout, stderr = ssh_command(
            probe,
            host=host,
            user=args.ssh_user,
            key_file=args.key_file,
            timeout=args.timeout,
        )
        if code != 0:
            result["nodes"].append(
                {
                    "node_id": host,
                    "in_nvlink_allocation": True,
                    "service_present": False,
                    "control_tooling_present": False,
                    "service_registration": "error",
                    "service_running": False,
                    "boot_disposition": "unknown",
                    "error": stderr or f"SSH probe exited with {code}",
                }
            )
            continue

        result["nodes"].append({"node_id": host, "in_nvlink_allocation": True, **_parse_probe(stdout)})
        result["nodes_validated"] += 1

    # A reachable host produces structured evidence even when the validation
    # fails because IMEX is absent, stopped, or unconfigured.
    result["success"] = result["nodes_validated"] > 0
    if not result["success"]:
        result["error"] = "No IMEX host responded to the SSH probe"
    return emit(result)


if __name__ == "__main__":
    raise SystemExit(main())
