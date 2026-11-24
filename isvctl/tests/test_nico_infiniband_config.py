# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not use
# this file except in compliance with the License. You may obtain a copy of the
# License at
#
# http://www.apache.org/licenses/LICENSE-2.0

"""Contract tests for the read-only NICo InfiniBand configuration."""

from __future__ import annotations

from pathlib import Path

import yaml

ISVCTL_ROOT = Path(__file__).resolve().parents[1]
CONFIG = ISVCTL_ROOT / "configs" / "providers" / "nico" / "config" / "infiniband_nvl72.yaml"


def _config() -> dict:
    """Load the planned NICo InfiniBand configuration."""
    return yaml.safe_load(CONFIG.read_text())


def test_nico_infiniband_config_imports_bare_metal_contract() -> None:
    """InfiniBand checks use the canonical bare-metal suite definition."""
    config = _config()

    assert config["import"] == ["../../../suites/bare_metal.yaml"]
    assert config["version"] == "1.0"


def test_nico_infiniband_config_contains_only_read_only_fabric_queries() -> None:
    """The fabric slice must not include lifecycle or mutating operations."""
    steps = _config()["commands"]["bare_metal"]["steps"]
    by_name = {step["name"]: step for step in steps}

    assert set(by_name) == {"query_ib_tenant_isolation", "query_ib_keys"}
    assert all(step["phase"] == "test" for step in steps)
    assert all(step["continue_on_failure"] is True for step in steps)
    assert by_name["query_ib_tenant_isolation"]["command"] == (
        "python3 ../scripts/infiniband/query_ib_tenant_isolation.py"
    )
    assert by_name["query_ib_keys"]["command"] == "python3 ../scripts/infiniband/query_ib_keys.py"


def test_nico_infiniband_config_wires_tenant_isolation_check() -> None:
    """SDN04-04 consumes the NICo partition query."""
    validation = _config()["tests"]["validations"]["ib_tenant_isolation"]

    assert validation["step"] == "query_ib_tenant_isolation"
    assert validation["checks"]["IbTenantIsolationCheck"]["min_partitions"] == 1


def test_nico_infiniband_config_wires_observable_security_keys() -> None:
    """SDN04-05 gates on P_Key and the UFM Management Key evidence."""
    validation = _config()["tests"]["validations"]["ib_keys_configured"]

    assert validation["step"] == "query_ib_keys"
    assert validation["checks"]["IbKeysConfiguredCheck"]["required_keys"] == ["p_key", "management_key"]
