# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not use
# this file except in compliance with the License. You may obtain a copy of the
# License at
#
# http://www.apache.org/licenses/LICENSE-2.0

"""Contract tests for the explicitly armed NICo node-return mutation config."""

from __future__ import annotations

from pathlib import Path

import yaml

ISVCTL_ROOT = Path(__file__).resolve().parents[1]
CONFIG = ISVCTL_ROOT / "configs" / "providers" / "nico" / "config" / "return_node_maintenance_nvl72.yaml"


def _config() -> dict:
    """Load the planned runtime-driven node-return configuration."""
    return yaml.safe_load(CONFIG.read_text())


def test_nico_return_node_maintenance_config_imports_bare_metal_contract() -> None:
    """The mutation check uses the canonical bare-metal suite definition."""
    config = _config()

    assert config["import"] == ["../../../suites/bare_metal.yaml"]
    assert config["version"] == "1.0"


def test_nico_return_node_maintenance_config_targets_only_runtime_instance() -> None:
    """The config must never discover or hard-code a victim instance or machine."""
    steps = _config()["commands"]["bare_metal"]["steps"]

    assert len(steps) == 1
    assert steps[0]["name"] == "return_node_maintenance"
    assert steps[0]["phase"] == "test"
    assert steps[0]["continue_on_failure"] is True
    assert steps[0]["command"] == "python3 ../scripts/breakfix/return_node_maintenance.py"
    assert "--instance-id={{instance_id}}" in steps[0]["args"]
    assert "NICO_ALLOW_RELEASE_FOR_REPAIR" not in steps[0]["args"]


def test_nico_return_node_maintenance_config_wires_check_and_runtime_guard() -> None:
    """BFX01-02 uses the named runtime instance and explicit environment guard."""
    config = _config()
    validation = config["tests"]["validations"]["return_node_maintenance"]

    assert config["tests"]["settings"]["instance_id"] == "{{env.NICO_INSTANCE_ID}}"
    assert validation == {
        "step": "return_node_maintenance",
        "checks": {"ReturnNodeMaintenanceCheck": {}},
    }
    assert "NICO_ALLOW_RELEASE_FOR_REPAIR=1" in config["tests"]["description"]
