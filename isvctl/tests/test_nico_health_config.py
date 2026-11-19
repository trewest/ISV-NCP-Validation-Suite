# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not use
# this file except in compliance with the License. You may obtain a copy of the
# License at
#
# http://www.apache.org/licenses/LICENSE-2.0

"""Contract tests for the read-only NICo health aggregation configuration."""

from __future__ import annotations

from pathlib import Path

import yaml

ISVCTL_ROOT = Path(__file__).resolve().parents[1]
CONFIG = ISVCTL_ROOT / "configs" / "providers" / "nico" / "config" / "health_nvl72.yaml"


def _config() -> dict:
    """Load the planned NICo health aggregation configuration."""
    return yaml.safe_load(CONFIG.read_text())


def test_nico_health_config_imports_bare_metal_contract() -> None:
    """Health aggregation uses the canonical bare-metal suite definition."""
    config = _config()

    assert config["import"] == ["../../../suites/bare_metal.yaml"]
    assert config["version"] == "1.0"


def test_nico_health_config_contains_only_read_only_aggregation_step() -> None:
    """The health slice must not include host lifecycle or mutation commands."""
    steps = _config()["commands"]["bare_metal"]["steps"]

    assert len(steps) == 1
    assert steps[0]["name"] == "query_health_aggregation"
    assert steps[0]["phase"] == "test"
    assert steps[0]["continue_on_failure"] is True
    assert steps[0]["command"] == "python3 ../scripts/health/query_health_aggregation.py"


def test_nico_health_config_wires_health_aggregation_check() -> None:
    """CAP05-02 consumes the matching NICo health aggregation query."""
    validation = _config()["tests"]["validations"]["health_aggregation"]

    assert validation["step"] == "query_health_aggregation"
    assert validation["checks"]["HealthAggregationCheck"]["min_groups"] == 1
