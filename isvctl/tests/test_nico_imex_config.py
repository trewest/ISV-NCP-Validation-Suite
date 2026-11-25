# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not use
# this file except in compliance with the License. You may obtain a copy of the
# License at
#
# http://www.apache.org/licenses/LICENSE-2.0

"""Contract tests for the opt-in NICo IMEX service smoke validation."""

from __future__ import annotations

from pathlib import Path

import yaml

from isvctl.config.merger import merge_yaml_files
from isvctl.config.schema import RunConfig

ISVCTL_ROOT = Path(__file__).resolve().parents[1]
CONFIG = ISVCTL_ROOT / "configs" / "providers" / "nico" / "config" / "imex_nvl72.yaml"


def _config() -> dict:
    """Load the planned NICo IMEX smoke configuration."""
    return yaml.safe_load(CONFIG.read_text())


def test_nico_imex_config_imports_network_contract_and_bare_metal_context() -> None:
    """The smoke check uses the canonical network contract on bare metal."""
    config = _config()

    assert config["import"] == ["../../../suites/network.yaml"]
    assert config["version"] == "1.0"
    assert config["tests"]["capability"] == "bare_metal"


def test_nico_imex_config_has_one_read_only_ssh_probe() -> None:
    """The smoke config only probes pre-existing hosts in the test phase."""
    config = _config()
    steps = config["commands"]["bare_metal"]["steps"]

    assert len(steps) == 1
    assert steps[0]["name"] == "imex_service"
    assert steps[0]["phase"] == "test"
    assert steps[0]["command"] == "python3 ../scripts/network/imex_service.py"
    assert steps[0]["args"] == [
        "--node-ids={{imex_hosts}}",
        "--key-file={{imex_ssh_key_file}}",
        "--ssh-user={{imex_ssh_user}}",
    ]
    assert set(config["tests"]["settings"]) == {
        "imex_hosts",
        "imex_ssh_key_file",
        "imex_ssh_user",
    }


def test_nico_imex_config_targets_service_presence_check() -> None:
    """The smoke config selects the canonical IMEX service-presence check."""
    config = _config()
    validation = config["tests"]["validations"]["fabric_topology"]

    assert validation["checks"]["ImexServicePresenceCheck"] == {
        "step": "imex_service",
        "require_running": True,
    }


def test_nico_imex_config_merges_canonical_check_metadata() -> None:
    """Provider wiring retains the canonical SDN17-01 identity and capability gate."""
    merged = merge_yaml_files([CONFIG])
    config = RunConfig.model_validate(merged)
    check = config.tests.validations["fabric_topology"]["checks"]["ImexServicePresenceCheck"]

    assert check["test_id"] == "SDN17-01"
    assert check["labels"] == ["network"]
    assert check["requires"] == ["vm", "bare_metal"]
    assert check["step"] == "imex_service"
    assert check["require_running"] is True
