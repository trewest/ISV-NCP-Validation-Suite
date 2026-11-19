# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not use
# this file except in compliance with the License. You may obtain a copy of the
# License at
#
# http://www.apache.org/licenses/LICENSE-2.0

"""Contract tests for the read-only NICo hardware-ingestion configuration."""

from __future__ import annotations

from pathlib import Path

import yaml

ISVCTL_ROOT = Path(__file__).resolve().parents[1]
CONFIG = ISVCTL_ROOT / "configs" / "providers" / "nico" / "config" / "ingestion_nvl72.yaml"


def _config() -> dict:
    """Load the planned NICo ingestion configuration."""
    return yaml.safe_load(CONFIG.read_text())


def test_nico_ingestion_config_imports_bare_metal_contract() -> None:
    """Hardware ingestion uses the canonical bare-metal suite definition."""
    config = _config()

    assert config["import"] == ["../../../suites/bare_metal.yaml"]
    assert config["version"] == "1.0"


def test_nico_ingestion_config_contains_only_read_only_query_step() -> None:
    """The ingestion slice must not contain lifecycle or repair operations."""
    steps = _config()["commands"]["bare_metal"]["steps"]

    assert len(steps) == 1
    assert steps[0]["name"] == "verify_ingestion"
    assert steps[0]["phase"] == "test"
    assert steps[0]["continue_on_failure"] is True
    assert steps[0]["command"] == "python3 ../scripts/hardware_ingestion/verify_ingestion.py"


def test_nico_ingestion_config_wires_hardware_ingestion_check() -> None:
    """HWING01-01 must require healthy Ready/InUse discovered machines."""
    validation = _config()["tests"]["validations"]["ingestion_check"]

    assert validation["step"] == "verify_ingestion"
    assert validation["checks"]["HardwareIngestionCheck"] == {
        "min_machines": 1,
        "expected_status": ["Ready", "InUse"],
        "require_healthy": True,
    }
