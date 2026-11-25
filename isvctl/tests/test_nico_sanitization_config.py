# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not use
# this file except in compliance with the License. You may obtain a copy of the
# License at
#
# http://www.apache.org/licenses/LICENSE-2.0

"""Contract tests for the isolated NICo GPU-memory sanitization validation."""

from __future__ import annotations

from pathlib import Path

import yaml

from isvctl.config.merger import merge_yaml_files
from isvctl.config.schema import RunConfig

ISVCTL_ROOT = Path(__file__).resolve().parents[1]
CONFIG = ISVCTL_ROOT / "configs" / "providers" / "nico" / "config" / "sanitization_nvl72.yaml"


def _config() -> dict:
    """Load the planned standalone sanitization configuration."""
    return yaml.safe_load(CONFIG.read_text())


def test_nico_sanitization_config_is_bare_metal_and_read_only() -> None:
    """The config imports the bare-metal contract and owns one test-phase probe."""
    config = _config()

    assert config["import"] == ["../../../suites/bare_metal.yaml"]
    assert config["version"] == "1.0"
    steps = config["commands"]["bare_metal"]["steps"]
    assert len(steps) == 1
    assert steps[0]["name"] == "query_sanitization"
    assert steps[0]["phase"] == "test"
    assert steps[0]["continue_on_failure"] is True
    assert steps[0]["command"] == "python3 ../scripts/sanitization/query_sanitization.py"
    assert all("key_access" not in step["command"] for step in steps)


def test_nico_sanitization_config_wires_only_gpu_memory_sanitization() -> None:
    """The isolated config binds the GPU-memory check to the read-only probe."""
    config = _config()
    validation = config["tests"]["validations"]["gpu_memory_sanitization"]

    assert validation["step"] == "query_sanitization"
    assert set(validation["checks"]) == {"BmGpuMemorySanitizationCheck"}


def test_nico_sanitization_config_merges_canonical_check_metadata() -> None:
    """Merged wiring retains the canonical SEC21-05 identity and bare-metal gate."""
    merged = merge_yaml_files([CONFIG])
    config = RunConfig.model_validate(merged)
    check = config.tests.validations["gpu_memory_sanitization"]["checks"]["BmGpuMemorySanitizationCheck"]

    assert check["test_id"] == "SEC21-05"
    assert check["labels"] == ["bare_metal", "gpu", "offtake", "sanitization", "security"]
