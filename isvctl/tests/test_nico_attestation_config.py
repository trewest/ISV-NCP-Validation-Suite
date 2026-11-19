# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not use
# this file except in compliance with the License. You may obtain a copy of the
# License at
#
# http://www.apache.org/licenses/LICENSE-2.0

"""Contract tests for the read-only NICo attestation configuration."""

from __future__ import annotations

from pathlib import Path

import yaml

ISVCTL_ROOT = Path(__file__).resolve().parents[1]
CONFIG = ISVCTL_ROOT / "configs" / "providers" / "nico" / "config" / "attestation_nvl72.yaml"


def _config() -> dict:
    """Load the planned NICo attestation configuration."""
    return yaml.safe_load(CONFIG.read_text())


def test_nico_attestation_config_imports_bare_metal_contract() -> None:
    """Attestation checks use the canonical bare-metal suite definition."""
    config = _config()

    assert config["import"] == ["../../../suites/bare_metal.yaml"]
    assert config["version"] == "1.0"


def test_nico_attestation_config_contains_only_read_only_query_step() -> None:
    """The attestation slice must not include lifecycle or mutation commands."""
    steps = _config()["commands"]["bare_metal"]["steps"]

    assert len(steps) == 1
    assert steps[0]["name"] == "query_attestation"
    assert steps[0]["phase"] == "test"
    assert steps[0]["continue_on_failure"] is True
    assert steps[0]["command"] == "python3 ../scripts/attestation/query_attestation.py"


def test_nico_attestation_config_wires_nonce_and_firmware_checks() -> None:
    """SEC22-01 and CNP09-02 consume the shared attestation query."""
    validations = _config()["tests"]["validations"]

    assert validations["nonce_attestation"] == {
        "step": "query_attestation",
        "checks": {"BmNonceAttestationCheck": {}},
    }
    assert validations["firmware_attestation"] == {
        "step": "query_attestation",
        "checks": {"BmFirmwareAttestationCheck": {}},
    }
