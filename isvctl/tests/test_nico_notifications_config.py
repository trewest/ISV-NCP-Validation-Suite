# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not use
# this file except in compliance with the License. You may obtain a copy of the
# License at
#
# http://www.apache.org/licenses/LICENSE-2.0

"""Contract tests for the read-only NICo notification configurations."""

from __future__ import annotations

from pathlib import Path

import yaml

ISVCTL_ROOT = Path(__file__).resolve().parents[1]
CONFIG = ISVCTL_ROOT / "configs" / "providers" / "nico" / "config" / "notifications_nvl72.yaml"


def _config() -> dict:
    """Load the planned NICo notification configuration."""
    return yaml.safe_load(CONFIG.read_text())


def test_nico_notifications_config_imports_bare_metal_contract() -> None:
    """Notification checks use the canonical bare-metal suite definition."""
    config = _config()

    assert config["import"] == ["../../../suites/bare_metal.yaml"]
    assert config["version"] == "1.0"


def test_nico_notifications_config_contains_only_read_only_gap_queries() -> None:
    """The notification slice must not contain lifecycle or mutation commands."""
    steps = _config()["commands"]["bare_metal"]["steps"]
    by_name = {step["name"]: step for step in steps}
    expected_gaps = {
        "query_retirement_notices": "BFX02-02",
        "query_planned_notifications": "BFX05-01",
        "query_failure_notifications": "BFX06-01",
    }

    assert set(by_name) == set(expected_gaps)
    for name, gap in expected_gaps.items():
        assert by_name[name]["phase"] == "test"
        assert by_name[name]["continue_on_failure"] is True
        assert by_name[name]["command"] == "python3 ../scripts/breakfix/gap_stub.py"
        assert by_name[name]["args"][-2:] == ["--gap", gap]


def test_nico_notifications_config_wires_all_three_checks() -> None:
    """BFX02-02, BFX05-01, and BFX06-01 consume their gap payloads."""
    validations = _config()["tests"]["validations"]

    assert validations["retirement_notices"] == {
        "step": "query_retirement_notices",
        "checks": {"RetirementNoticesCheck": {}},
    }
    assert validations["planned_notifications"] == {
        "step": "query_planned_notifications",
        "checks": {"PlannedMaintenanceNotificationCheck": {}},
    }
    assert validations["failure_notifications"] == {
        "step": "query_failure_notifications",
        "checks": {"FailureNotificationCheck": {}},
    }
