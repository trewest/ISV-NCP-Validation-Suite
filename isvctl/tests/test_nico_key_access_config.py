# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not use
# this file except in compliance with the License. You may obtain a copy of the
# License at
#
# http://www.apache.org/licenses/LICENSE-2.0

"""Contract tests for the read-only NICo specified-key access configuration."""

from __future__ import annotations

from pathlib import Path

import yaml

ISVCTL_ROOT = Path(__file__).resolve().parents[1]
CONFIG = ISVCTL_ROOT / "configs" / "providers" / "nico" / "config" / "key_access_nvl72.yaml"


def _config() -> dict:
    """Load the planned NICo specified-key access configuration."""
    return yaml.safe_load(CONFIG.read_text())


def test_nico_key_access_config_imports_bare_metal_contract() -> None:
    """Key-access validation uses the canonical bare-metal suite definition."""
    config = _config()

    assert config["import"] == ["../../../suites/bare_metal.yaml"]
    assert config["version"] == "1.0"


def test_nico_key_access_config_is_strictly_read_only() -> None:
    """The provider must not provision or remove a throwaway key."""
    steps = _config()["commands"]["bare_metal"]["steps"]

    assert len(steps) == 1
    assert steps[0]["name"] == "query_key_access"
    assert steps[0]["phase"] == "test"
    assert steps[0]["continue_on_failure"] is True
    assert steps[0]["command"] == "python3 ../scripts/auth/query_key_access.py"
    assert "--no-provision" in steps[0]["args"]


def test_nico_key_access_config_wires_component_key_access_check() -> None:
    """AUTH03-01 consumes the read-only specified-key access query."""
    validation = _config()["tests"]["validations"]["key_access"]

    assert validation["step"] == "query_key_access"
    assert validation["checks"]["BmComponentKeyAccessCheck"]["min_accessible_targets"] == 1
