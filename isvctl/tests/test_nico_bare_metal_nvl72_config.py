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
    "query_target_serials",
    "query_target_ip",
    "query_target_health",
    "query_target_topology",
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
    """The host probe runs the four-GPU check without requiring CUDA toolkit."""
    validations = _config()["tests"]["validations"]

    gpu = validations["gpu"]
    assert gpu["step"] == "nvl72_host_probe"
    assert gpu["checks"]["BmGpusPresentCheck"]["expected_gpus"] == 4

    assert "BmGpuDriverCheck" in _config()["tests"]["exclude"]["tests"]


def test_nico_nvl72_config_wires_read_only_platform_checks() -> None:
    """DPU/OOB/telemetry checks use dedicated read-only provider steps."""
    steps = {step["name"]: step for step in _config()["commands"]["bare_metal"]["steps"]}
    validations = _config()["tests"]["validations"]

    assert steps["query_switch_firmware"]["command"] == "python3 ../scripts/breakfix/query_switch_firmware.py"
    assert validations["dpu_health"]["step"] == "check_dpu_health"
    assert validations["dpu_health"]["checks"]["BmDpuHealthCheck"]["expected_dpu_count"] == 1
    assert validations["dpu_health"]["checks"]["BmDpuHealthCheck"]["require_heartbeat"] is True
    assert validations["oob_failure_detection"]["step"] == "query_oob_health"
    assert validations["switch_firmware"]["step"] == "query_switch_firmware"
    assert validations["bmc_kernel_logs"]["step"] == "query_bmc_kernel_logs"
    assert validations["node_health_agents"]["step"] == "query_node_health_agents"
    assert "OobFailureDetectionCheck" in validations["oob_failure_detection"]["checks"]
    assert "OobFailureDetectionCheck" not in _config()["tests"]["exclude"]["tests"]
    assert "FailureDomainObservabilityCheck" in _config()["tests"]["exclude"]["tests"]


def test_nico_nvl72_config_wires_target_inventory_checks() -> None:
    """Inventory checks must query only the explicitly selected target machine."""
    validations = _config()["tests"]["validations"]

    assert validations["hardware_serials"]["step"] == "query_target_serials"
    assert validations["hardware_serials"]["checks"]["BmHardwareSerialCheck"]["min_machines"] == 1

    assert validations["stable_storage_ips"]["step"] == "query_target_ip"
    assert validations["stable_storage_ips"]["checks"]["StableStorageNodeIpCheck"] == {
        "min_hosts": 1,
        "storage_only": False,
    }

    assert validations["host_health"]["step"] == "query_target_health"
    assert validations["host_health"]["checks"]["HostHealthCheck"]["require_report"] is True

    assert validations["failure_domain_observability"]["step"] == "query_target_topology"
    assert validations["failure_domain_observability"]["checks"]["FailureDomainObservabilityCheck"] == {
        "min_hosts": 1,
        "min_failure_domains": 1,
        "require_all_hosts_mapped": True,
    }


def test_nico_nvl72_dpu_overlay_check_is_deferred_without_active_instance() -> None:
    """DPU overlay validation must remain unconfigured for an unassigned host."""
    step_names = {step["name"] for step in _config()["commands"]["bare_metal"]["steps"]}

    assert "check_dpu_network" not in step_names
