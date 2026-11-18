# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not use
# this file except in compliance with the License. You may obtain a copy of the
# License at
#
# http://www.apache.org/licenses/LICENSE-2.0

"""Contract tests for the read-only NICo governance validation configuration."""

from __future__ import annotations

from pathlib import Path

import yaml

ISVCTL_ROOT = Path(__file__).resolve().parents[1]
CONFIG = ISVCTL_ROOT / "configs" / "providers" / "nico" / "config" / "governance_nvl72.yaml"


def _config() -> dict:
    """Load the planned NICo governance configuration."""
    return yaml.safe_load(CONFIG.read_text())


def test_nico_governance_config_imports_bare_metal_contract() -> None:
    """Governance checks use the canonical provider-neutral suite definitions."""
    config = _config()

    assert config["import"] == ["../../../suites/bare_metal.yaml"]
    assert config["version"] == "1.0"


def test_nico_governance_config_wires_only_read_only_site_queries() -> None:
    """The governance slice must not contain host lifecycle or mutation steps."""
    steps = _config()["commands"]["bare_metal"]["steps"]
    by_name = {step["name"]: step for step in steps}

    assert set(by_name) == {"query_governance_metrics", "query_fleet_inventory", "query_resource_discovery"}
    assert all(step["phase"] == "test" for step in steps)
    assert by_name["query_fleet_inventory"]["command"] == "python3 ../scripts/governance/query_fleet_inventory.py"
    assert by_name["query_governance_metrics"]["command"] == "python3 ../scripts/governance/query_metrics.py"
    assert by_name["query_resource_discovery"]["command"] == "python3 ../scripts/governance/query_resource_discovery.py"
    assert "--polls" in by_name["query_resource_discovery"]["args"]
    assert "2" in by_name["query_resource_discovery"]["args"]


def test_nico_governance_config_wires_fleet_and_resource_discovery_checks() -> None:
    """Both CAP02 and CAP03 checks must consume their matching query steps."""
    validations = _config()["tests"]["validations"]

    assert validations["fleet_inventory"]["step"] == "query_fleet_inventory"
    assert validations["fleet_inventory"]["checks"]["FleetManagementApiCheck"]["min_nodes"] == 1
    assert validations["resource_discovery"]["step"] == "query_resource_discovery"
    assert validations["resource_discovery"]["checks"]["ResourceDiscoveryApiCheck"] == {
        "min_resources": 1,
        "min_polls": 2,
    }


def test_nico_governance_config_wires_governance_metrics() -> None:
    """CAP01-01 consumes the read-only node/GPU governance count query."""
    validation = _config()["tests"]["validations"]["governance_metrics"]

    assert validation["step"] == "query_governance_metrics"
    assert validation["checks"]["GovernanceMetricsCheck"]["min_delivered_nodes"] == 1
