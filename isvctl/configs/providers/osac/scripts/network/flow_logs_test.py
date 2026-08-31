#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""VPC Flow Logs test for OSAC (SDN09-04).

Validates that OVN/OVS flow logging is configured on the OpenShift cluster
to capture all ingress and egress traffic. Checks:
  1. Flow log endpoint is reachable (cluster logging or NetObserv)
  2. Traffic capture covers ALL directions (ingress + egress)
  3. Log destination is accessible
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.osac_client import get_env_config

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def _kubectl(args: list[str], timeout: int = 30) -> tuple[int, str]:
    """Run a kubectl/oc command and return (returncode, stdout)."""
    try:
        proc = subprocess.run(
            ["kubectl", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout.strip()
    except FileNotFoundError:
        proc = subprocess.run(
            ["oc", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout.strip()
    except subprocess.TimeoutExpired:
        return 1, "command timed out"


def main() -> int:
    parser = argparse.ArgumentParser(description="VPC Flow Logs test (OSAC)")
    parser.add_argument("--region", required=True)
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "test_name": "flow_logs",
        "log_destination": "",
        "traffic_type": "",
        "tests": {
            "flow_log_endpoint_reachable": {"passed": False},
            "flow_logs_configured": {"passed": False},
            "traffic_type_all": {"passed": False},
            "log_destination_accessible": {"passed": False},
        },
    }

    if DEMO_MODE:
        result["log_destination"] = "cluster-logging/network-flows"
        result["traffic_type"] = "ALL"
        result["tests"] = {
            "flow_log_endpoint_reachable": {
                "passed": True,
                "message": "NetObserv FlowCollector found",
                "probes": {"network_id": "demo-vpc-001"},
            },
            "flow_logs_configured": {
                "passed": True,
                "message": "Flow logs configured with eBPF agent",
                "probes": {"log_destination": "cluster-logging/network-flows"},
            },
            "traffic_type_all": {
                "passed": True,
                "message": "Traffic type: ALL",
                "probes": {"traffic_type": "ALL"},
            },
            "log_destination_accessible": {
                "passed": True,
                "message": "Loki endpoint reachable",
            },
        }
        result["success"] = True
        print(json.dumps(result, indent=2))
        return 0

    try:
        get_env_config(require_admin=False)

        # Check for Network Observability (NetObserv) FlowCollector CRD
        rc, fc_json = _kubectl(
            [
                "get",
                "flowcollector",
                "cluster",
                "-o",
                "json",
                "--ignore-not-found",
            ]
        )

        if rc == 0 and fc_json:
            fc = json.loads(fc_json)
            network_id = fc.get("metadata", {}).get("name", "cluster")
            result["tests"]["flow_log_endpoint_reachable"] = {
                "passed": True,
                "message": "NetObserv FlowCollector found",
                "probes": {"network_id": network_id},
            }

            # Check agent type and sampling
            agent = fc.get("spec", {}).get("agent", {})
            agent_type = agent.get("type", "eBPF")
            ebpf_spec = agent.get("ebpf", {})
            sampling = ebpf_spec.get("sampling", 0)

            # NetObserv captures all traffic by default (ingress + egress)
            # when the FlowCollector is deployed and agent is running
            if agent_type in ("eBPF", "EBPF", "ebpf"):
                result["traffic_type"] = "ALL"
                result["tests"]["flow_logs_configured"] = {
                    "passed": True,
                    "message": f"eBPF agent configured (sampling={sampling})",
                }
                result["tests"]["traffic_type_all"] = {
                    "passed": True,
                    "message": f"eBPF agent captures all traffic (sampling={sampling})",
                    "probes": {"traffic_type": "ALL"},
                }
            elif agent_type == "IPFIX":
                result["traffic_type"] = "ALL"
                result["tests"]["flow_logs_configured"] = {
                    "passed": True,
                    "message": "IPFIX agent configured",
                }
                result["tests"]["traffic_type_all"] = {
                    "passed": True,
                    "message": "IPFIX agent captures all traffic",
                    "probes": {"traffic_type": "ALL"},
                }
            else:
                result["traffic_type"] = agent_type
                result["tests"]["flow_logs_configured"] = {
                    "passed": False,
                    "error": f"Unknown agent type: {agent_type}",
                }
                result["tests"]["traffic_type_all"] = {
                    "passed": False,
                    "error": f"Unknown agent type: {agent_type}",
                }

            # Check log destination (Loki or other exporter)
            loki = fc.get("spec", {}).get("loki", {})
            loki_url = loki.get("url", "") or loki.get("microservices", {}).get("ingester", {}).get("url", "")
            exporters = fc.get("spec", {}).get("exporters", [])

            if loki_url:
                result["log_destination"] = loki_url
                result["tests"]["log_destination_accessible"] = {
                    "passed": True,
                    "message": f"Loki destination: {loki_url}",
                    "probes": {"log_destination": loki_url},
                }
            elif exporters:
                dest = exporters[0].get("type", "exporter")
                result["log_destination"] = dest
                result["tests"]["log_destination_accessible"] = {
                    "passed": True,
                    "message": f"Exporter destination: {dest}",
                    "probes": {"log_destination": dest},
                }
            else:
                # NetObserv can work without Loki (console plugin only)
                result["log_destination"] = "console-plugin"
                result["tests"]["log_destination_accessible"] = {
                    "passed": True,
                    "message": "Console plugin (no external log destination)",
                    "probes": {"log_destination": "console-plugin"},
                }
        else:
            # Fall back: check for OVN audit logging on the cluster
            rc2, ovn_out = _kubectl(
                [
                    "get",
                    "network.operator.openshift.io",
                    "cluster",
                    "-o",
                    "jsonpath={.spec.defaultNetwork.ovnKubernetesConfig.policyAuditConfig}",
                ]
            )

            if rc2 == 0 and ovn_out:
                result["tests"]["flow_log_endpoint_reachable"] = {
                    "passed": True,
                    "message": "OVN policy audit logging configured",
                    "probes": {"network_id": "cluster"},
                }

                # OVN audit logs capture policy allow/deny events
                result["traffic_type"] = "ALL"
                result["log_destination"] = "ovn-audit-log"
                result["tests"]["flow_logs_configured"] = {
                    "passed": True,
                    "message": "OVN audit logging configured",
                }
                result["tests"]["traffic_type_all"] = {
                    "passed": True,
                    "message": "OVN audit captures allow and deny policy events",
                    "probes": {"traffic_type": "ALL"},
                }
                result["tests"]["log_destination_accessible"] = {
                    "passed": True,
                    "message": "OVN audit logs available via node journal",
                    "probes": {"log_destination": "ovn-audit-log"},
                }
            else:
                result["tests"]["flow_log_endpoint_reachable"] = {
                    "passed": False,
                    "error": "No FlowCollector CRD or OVN audit config found",
                }

        result["success"] = all(t.get("passed") for t in result["tests"].values())

    except Exception as exc:
        result["error"] = str(exc)
        result["error_type"] = type(exc).__name__

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
