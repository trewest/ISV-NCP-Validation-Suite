# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not use
# this file except in compliance with the License. You may obtain a copy of the
# License at
#
# http://www.apache.org/licenses/LICENSE-2.0

"""Contract tests for the runtime-driven disposable NICo lifecycle config."""

from __future__ import annotations

from pathlib import Path

import yaml

ISVCTL_ROOT = Path(__file__).resolve().parents[1]
CONFIG = ISVCTL_ROOT / "configs" / "providers" / "nico" / "config" / "disposable_return_node_nvl72.yaml"


def _config() -> dict:
    """Load the planned disposable-instance lifecycle configuration."""
    return yaml.safe_load(CONFIG.read_text())


def test_nico_disposable_return_config_imports_bare_metal_contract() -> None:
    """The lifecycle config uses the canonical bare-metal suite definition."""
    config = _config()

    assert config["import"] == ["../../../suites/bare_metal.yaml"]
    assert config["version"] == "1.0"


def test_nico_disposable_return_config_has_guarded_setup_test_teardown() -> None:
    """Create, release, and cleanup are runtime-driven and explicitly armed."""
    config = _config()
    steps = {step["name"]: step for step in config["commands"]["bare_metal"]["steps"]}

    assert set(steps) == {"create_disposable_instance", "return_node_maintenance", "cleanup_disposable_instance"}
    assert steps["create_disposable_instance"]["phase"] == "setup"
    assert steps["return_node_maintenance"]["phase"] == "test"
    assert steps["cleanup_disposable_instance"]["phase"] == "teardown"
    assert steps["create_disposable_instance"]["command"] == (
        "python3 ../scripts/instance/create_disposable_instance.py"
    )
    assert steps["cleanup_disposable_instance"]["command"] == (
        "python3 ../scripts/instance/cleanup_disposable_instance.py"
    )
    assert "--instance-id={{steps.create_disposable_instance.instance_id}}" in steps["return_node_maintenance"]["args"]
    assert (
        "--instance-id={{steps.create_disposable_instance.instance_id}}" in steps["cleanup_disposable_instance"]["args"]
    )


def test_nico_disposable_return_config_wires_return_validation() -> None:
    """BFX01-02 consumes the instance created by setup, not a hard-coded ID."""
    config = _config()
    validation = config["tests"]["validations"]["return_node_maintenance"]

    assert validation == {
        "step": "return_node_maintenance",
        "checks": {"ReturnNodeMaintenanceCheck": {}},
    }
    description = config["tests"]["description"]
    assert "NICO_ALLOW_DISPOSABLE_INSTANCE_CREATE=1" in description
    assert "NICO_ALLOW_RELEASE_FOR_REPAIR=1" in description
