#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Provider-hidden observability probes for OSAC on OCP.

Emits provider_hidden=true for observability checks where the underlying
hardware plane (BMC, physical switch, InfiniBand subnet manager, UFM) is
not exposed to tenants in an OCP-based cloud.  OCP manages these planes
internally; tenants interact only with Kubernetes and fulfillment APIs.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

ASPECT_SUBTESTS: dict[str, dict[str, Any]] = {
    "bmc_sel_logs": {
        "subtests": [
            "sel_log_endpoint_reachable",
            "sel_log_source_present",
            "sel_entries_queryable",
        ],
        "message": (
            "BMC SEL logs are provider-managed in OCP; "
            "the BMC/IPMI plane is not exposed to tenants"
        ),
    },
    "bmc_gpu_telemetry": {
        "subtests": [
            "telemetry_endpoint_reachable",
            "gpu_metrics_present",
            "host_os_gap_identified",
            "telemetry_samples_recent",
        ],
        "message": (
            "BMC GPU telemetry is provider-managed in OCP; "
            "GPU metrics are collected via DCGM exporter at the host OS level"
        ),
    },
    "switch_nvlink_telemetry": {
        "subtests": [
            "telemetry_endpoint_reachable",
            "port_metrics_present",
            "samples_recent",
        ],
        "message": (
            "NVSwitch port-side telemetry is provider-managed; "
            "switch-side metrics are collected by DCGM exporter from the GPU host"
        ),
    },
    "subnet_manager_logs": {
        "subtests": [
            "log_endpoint_reachable",
            "log_source_present",
            "log_entries_queryable",
        ],
        "message": (
            "No InfiniBand fabric deployed; subnet manager logs "
            "are not applicable to Ethernet-only OCP clusters"
        ),
    },
    "ufm_event_logs": {
        "subtests": [
            "event_log_endpoint_reachable",
            "event_log_source_present",
            "event_entries_queryable",
        ],
        "message": (
            "No UFM deployed; UFM event logs are not applicable "
            "to Ethernet-only OCP clusters"
        ),
    },
    "general_switch_logs": {
        "subtests": [
            "log_endpoint_reachable",
            "switch_log_source_present",
            "entries_queryable",
        ],
        "message": (
            "OVN-Kubernetes is the SDN in OCP; "
            "physical switch management logs are provider-owned"
        ),
    },
    "switch_syslogs": {
        "subtests": [
            "syslog_endpoint_reachable",
            "switch_syslog_source_present",
            "entries_recent",
        ],
        "message": (
            "OVN-Kubernetes is the SDN in OCP; "
            "physical switch syslog is provider-managed"
        ),
    },
    "switch_kernel_logs": {
        "subtests": [
            "log_endpoint_reachable",
            "kernel_log_source_present",
            "entries_queryable",
        ],
        "message": (
            "OVN-Kubernetes is the SDN in OCP; "
            "physical switch kernel logs are provider-managed"
        ),
    },
}


def _provider_hidden(test_name: str, message: str) -> dict[str, Any]:
    return {
        "passed": True,
        "provider_hidden": True,
        "message": f"{test_name}: {message}",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--aspect", required=True, choices=sorted(ASPECT_SUBTESTS))
    args = parser.parse_args()

    spec = ASPECT_SUBTESTS[args.aspect]
    message = spec["message"]

    result: dict[str, Any] = {
        "success": True,
        "platform": "observability",
        "test_name": args.aspect,
        "tests": {
            name: _provider_hidden(name, message)
            for name in spec["subtests"]
        },
    }

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
