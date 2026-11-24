# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not use
# this file except in compliance with the License. You may obtain a copy of the
# License at
#
# http://www.apache.org/licenses/LICENSE-2.0

"""Contract tests for the runtime-guarded NICo VPC peering validation."""

from __future__ import annotations

from pathlib import Path

import yaml

ISVCTL_ROOT = Path(__file__).resolve().parents[1]
CONFIG = ISVCTL_ROOT / "configs" / "providers" / "nico" / "config" / "vpc_peering_nvl72.yaml"


def _config() -> dict:
    """Load the planned NICo VPC peering configuration."""
    return yaml.safe_load(CONFIG.read_text())


def test_nico_vpc_peering_config_imports_network_contract() -> None:
    """VPC peering uses the canonical network suite definition."""
    config = _config()

    assert config["import"] == ["../../../suites/network.yaml"]
    assert config["version"] == "1.0"


def test_nico_vpc_peering_config_contains_one_guarded_mutation_probe() -> None:
    """The peering probe owns setup and cleanup behind an explicit guard."""
    steps = _config()["commands"]["network"]["steps"]

    assert len(steps) == 1
    assert steps[0]["name"] == "query_vpc_peering"
    assert steps[0]["phase"] == "test"
    assert steps[0]["continue_on_failure"] is True
    assert steps[0]["command"] == "python3 ../scripts/network/query_vpc_peering.py"
    assert "NICO_ALLOW_VPC_PEERING_MUTATION=1" in _config()["tests"]["description"]


def test_nico_vpc_peering_config_wires_sdn07_check() -> None:
    """SDN07-01 consumes the provider-neutral peering lifecycle output."""
    validation = _config()["tests"]["validations"]["vpc_peering"]

    assert validation == {
        "step": "query_vpc_peering",
        "checks": {"VpcPeeringCheck": {}},
    }
