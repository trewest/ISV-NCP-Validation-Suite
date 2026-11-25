# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not use
# this file except in compliance with the License. You may obtain a copy of the
# License at
#
# http://www.apache.org/licenses/LICENSE-2.0

"""Tests for the read-only NICo IMEX service probe."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

NICO_SCRIPTS = Path(__file__).resolve().parents[3] / "configs" / "providers" / "nico" / "scripts"


def _load_script() -> ModuleType:
    """Load the IMEX probe as a module."""
    path = NICO_SCRIPTS / "network" / "imex_service.py"
    spec = importlib.util.spec_from_file_location("test_nico_imex_service", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["test_nico_imex_service"] = module
    spec.loader.exec_module(module)
    return module


def _probe_output(*, active: str = "active", nodes_config: str = "yes") -> str:
    """Build stable key/value output from a remote host probe."""
    return "\n".join(
        [
            "DAEMON=yes",
            "CONTROL=yes",
            "LOAD=loaded",
            "BOOT=enabled",
            f"ACTIVE={active}",
            "CONFIG=yes",
            f"NODES_CONFIG={nodes_config}",
        ]
    )


def test_probe_command_is_read_only() -> None:
    """The remote command only reads binaries, systemd state, and config files."""
    module = _load_script()
    command = module._probe_command()

    assert "systemctl restart" not in command
    assert "systemctl stop" not in command
    assert "systemctl start" not in command
    assert "tee" not in command
    assert "rm " not in command


def test_parse_probe_reports_active_one_node_evidence() -> None:
    """A healthy one-node host is represented in the validation contract."""
    module = _load_script()

    result = module._parse_probe(_probe_output())

    assert result == {
        "service_present": True,
        "control_tooling_present": True,
        "service_registration": "loaded",
        "service_running": True,
        "boot_disposition": "enabled",
        "config_present": True,
        "nodes_config_present": True,
    }


def test_parse_probe_reports_stopped_service_without_claiming_presence_failure() -> None:
    """The probe reports state; the validation decides whether active is required."""
    module = _load_script()

    result = module._parse_probe(_probe_output(active="failed", nodes_config="no"))

    assert result["service_present"] is True
    assert result["service_registration"] == "loaded"
    assert result["service_running"] is False
    assert result["nodes_config_present"] is False


def test_unconfigured_probe_is_a_structured_skip(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Omitting optional smoke inputs does not attempt SSH or mutate a host."""
    module = _load_script()
    monkeypatch.setattr(sys, "argv", ["imex_service.py"])

    assert module.main() == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["success"] is True
    assert payload["skipped"] is True
    assert "not configured" in payload["skip_reason"]


def test_probe_emits_structured_one_node_result(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A reachable host yields the fields consumed by ImexServicePresenceCheck."""
    module = _load_script()
    monkeypatch.setattr(module, "ssh_command", lambda *args, **kwargs: (0, _probe_output(), ""))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "imex_service.py",
            "--node-ids",
            "10.180.64.2",
            "--key-file",
            "/tmp/test-key",
        ],
    )

    assert module.main() == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["success"] is True
    assert payload["nodes_checked"] == 1
    assert payload["nodes_validated"] == 1
    assert payload["nodes"] == [
        {
            "node_id": "10.180.64.2",
            "in_nvlink_allocation": True,
            "service_present": True,
            "control_tooling_present": True,
            "service_registration": "loaded",
            "service_running": True,
            "boot_disposition": "enabled",
            "config_present": True,
            "nodes_config_present": True,
        }
    ]
