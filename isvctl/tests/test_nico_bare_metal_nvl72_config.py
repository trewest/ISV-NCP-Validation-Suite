# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not use
# this file except in compliance with the License. You may obtain a copy of the
# License at
#
# http://www.apache.org/licenses/LICENSE-2.0

"""Contract tests for the read-only NICo NVL72 bare-metal configuration."""

from __future__ import annotations

from pathlib import Path

import yaml

ISVCTL_ROOT = Path(__file__).resolve().parents[1]
CONFIG = ISVCTL_ROOT / "configs" / "providers" / "nico" / "config" / "bare_metal_nvl72.yaml"

EXPECTED_STEPS = {
    "nvl72_host_probe",
    "check_dpu_health",
    "query_oob_health",
    "query_switch_firmware",
    "query_bmc_kernel_logs",
    "query_node_health_agents",
}


def _config() -> dict:
    """Load the planned NICo NVL72 provider configuration."""
    return yaml.safe_load(CONFIG.read_text())


def test_nico_nvl72_config_imports_canonical_bare_metal_contract() -> None:
    """The provider must inherit the provider-agnostic bare-metal suite."""
    config = _config()

    assert config["import"] == ["../../../suites/bare_metal.yaml"]
    assert config["version"] == "1.0"


def test_nico_nvl72_config_contains_only_test_phase_steps() -> None:
    """The first NVL72 slice must not launch, reboot, or tear down a host."""
    steps = _config()["commands"]["bare_metal"]["steps"]
    step_names = {step["name"] for step in steps}

    assert step_names == EXPECTED_STEPS
    assert all(step["phase"] == "test" for step in steps)
    assert not {"launch_instance", "reboot_instance", "power_cycle_instance", "teardown"} & step_names


def test_nico_nvl72_host_probe_wires_four_gpu_and_driver_checks() -> None:
    """Host checks use the NVL72 SSH probe and the observed GB300 baseline."""
    validations = _config()["tests"]["validations"]

    gpu = validations["gpu"]
    assert gpu["step"] == "nvl72_host_probe"
    assert gpu["checks"]["BmGpusPresentCheck"]["expected_gpus"] == 4

    driver = validations["driver_info"]
    assert driver["step"] == "nvl72_host_probe"
    assert driver["checks"]["BmGpuDriverCheck"]["expected_driver_version"] == "615.71.10"


def test_nico_nvl72_config_wires_read_only_platform_checks() -> None:
    """DPU/OOB/telemetry checks use dedicated read-only provider steps."""
    validations = _config()["tests"]["validations"]

    assert validations["dpu_health"]["step"] == "check_dpu_health"
    assert validations["dpu_health"]["checks"]["BmDpuHealthCheck"]["expected_dpu_count"] == 1
    assert validations["dpu_health"]["checks"]["BmDpuHealthCheck"]["require_heartbeat"] is True
    assert validations["oob_failure_detection"]["step"] == "query_oob_health"
    assert validations["switch_firmware"]["step"] == "query_switch_firmware"
    assert validations["bmc_kernel_logs"]["step"] == "query_bmc_kernel_logs"
    assert validations["node_health_agents"]["step"] == "query_node_health_agents"


def test_nico_nvl72_dpu_overlay_check_is_deferred_without_active_instance() -> None:
    """DPU overlay validation must remain unconfigured for an unassigned host."""
    step_names = {step["name"] for step in _config()["commands"]["bare_metal"]["steps"]}

    assert "check_dpu_network" not in step_names
