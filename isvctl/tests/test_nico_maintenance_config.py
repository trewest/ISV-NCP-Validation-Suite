# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not use
# this file except in compliance with the License. You may obtain a copy of the
# License at
#
# http://www.apache.org/licenses/LICENSE-2.0

"""Contract tests for the read-only NICo maintenance observability configuration."""

from __future__ import annotations

from pathlib import Path

import yaml

ISVCTL_ROOT = Path(__file__).resolve().parents[1]
CONFIG = ISVCTL_ROOT / "configs" / "providers" / "nico" / "config" / "maintenance_nvl72.yaml"


def _config() -> dict:
    """Load the planned NICo maintenance configuration."""
    return yaml.safe_load(CONFIG.read_text())


def test_nico_maintenance_config_imports_bare_metal_contract() -> None:
    """Maintenance observability uses the canonical bare-metal checks."""
    config = _config()

    assert config["import"] == ["../../../suites/bare_metal.yaml"]
    assert config["version"] == "1.0"


def test_nico_maintenance_config_contains_only_read_only_query_steps() -> None:
    """The slice must not report, release, replace, or repair a machine."""
    steps = _config()["commands"]["bare_metal"]["steps"]
    by_name = {step["name"]: step for step in steps}

    assert set(by_name) == {"query_maintenance_events", "query_repair_history"}
    assert all(step["phase"] == "test" for step in steps)
    assert all(step["continue_on_failure"] is True for step in steps)
    assert by_name["query_maintenance_events"]["command"] == "python3 ../scripts/breakfix/query_maintenance_events.py"
    assert by_name["query_repair_history"]["command"] == "python3 ../scripts/breakfix/query_repair_history.py"


def test_nico_maintenance_config_wires_event_and_history_validations() -> None:
    """The two query steps feed the matching provider-neutral validations."""
    validations = _config()["tests"]["validations"]

    assert validations["maintenance_events"]["step"] == "query_maintenance_events"
    assert validations["maintenance_events"]["checks"] == {"MaintenanceEventsCheck": {}}
    assert validations["repair_history"]["step"] == "query_repair_history"
    assert validations["repair_history"]["checks"] == {"RepairHistoryCheck": {}}
