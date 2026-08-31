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

"""Query OCP Prometheus for network telemetry metrics.

Supports three modes via --telemetry-type:
  management — queries ovs_vswitchd_* metrics (OVN-Kubernetes management plane)
  east-west  — queries container_network_* metrics (pod-to-pod traffic)
  host-nic   — queries node_network_* metrics (per-NIC interface stats)

Uses kubectl port-forward to the Prometheus pod for auth-free access.

Outputs JSON consumed by ManagementNetworkTelemetryCheck,
EastWestNetworkTelemetryCheck, or HostNicNetworkTelemetryCheck.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request


DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"
KUBECTL = os.environ.get("KUBECTL", "kubectl")

MANAGEMENT_METRICS = [
    "ovs_vswitchd_interface_rx_dropped_total",
    "ovs_vswitchd_interface_tx_dropped_total",
    "ovs_vswitchd_interface_rx_errors_total",
    "ovs_vswitchd_interface_tx_errors_total",
    "ovs_vswitchd_bridge_flows_total",
    "ovs_vswitchd_dp_packets_total",
]

EAST_WEST_METRICS = [
    "container_network_receive_bytes_total",
    "container_network_transmit_bytes_total",
    "container_network_receive_packets_total",
    "container_network_transmit_packets_total",
    "container_network_receive_errors_total",
    "container_network_transmit_errors_total",
]

HOST_NIC_METRICS = [
    "node_network_receive_bytes_total",
    "node_network_transmit_bytes_total",
    "node_network_receive_packets_total",
    "node_network_transmit_packets_total",
    "node_network_receive_errs_total",
    "node_network_transmit_errs_total",
]

TYPE_CONFIG = {
    "management": {
        "metrics": MANAGEMENT_METRICS,
        "metrics_test": "plane_metrics_present",
        "count_field": None,
        "count_label": None,
        "test_name": "management_network_telemetry",
    },
    "east-west": {
        "metrics": EAST_WEST_METRICS,
        "metrics_test": "plane_metrics_present",
        "count_field": None,
        "count_label": None,
        "test_name": "east_west_network_telemetry",
    },
    "host-nic": {
        "metrics": HOST_NIC_METRICS,
        "metrics_test": "nic_metrics_present",
        "count_field": "nics_checked",
        "count_label": "device",
        "test_name": "host_nic_network_telemetry",
    },
}


def run_kubectl(*args: str, timeout: int = 60) -> tuple[int, str, str]:
    cmd = KUBECTL.split() + list(args)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except subprocess.TimeoutExpired:
        return 1, "", f"Command timed out after {timeout}s"
    except Exception as exc:
        return 1, "", str(exc)


def start_port_forward(resource: str, namespace: str, remote_port: int, local_port: int) -> subprocess.Popen | None:
    cmd = KUBECTL.split() + [
        "port-forward", resource, f"{local_port}:{remote_port}",
        "-n", namespace,
    ]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.time() + 15
        while time.time() < deadline:
            if proc.poll() is not None:
                return None
            try:
                urllib.request.urlopen(f"http://localhost:{local_port}/-/ready", timeout=2)
                return proc
            except Exception:
                time.sleep(0.5)
        return proc
    except Exception:
        return None


def query_prometheus(port: int, query: str) -> dict | None:
    url = f"http://localhost:{port}/api/v1/query?{urllib.parse.urlencode({'query': query})}"
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Query network telemetry from OCP Prometheus")
    parser.add_argument("--telemetry-type", required=True, choices=["management", "east-west", "host-nic"])
    args = parser.parse_args()

    cfg = TYPE_CONFIG[args.telemetry_type]
    metrics = cfg["metrics"]
    metrics_test = cfg["metrics_test"]
    count_field = cfg["count_field"]
    count_label = cfg["count_label"]

    result: dict = {
        "success": False,
        "platform": "observability",
        "test_name": cfg["test_name"],
        "tests": {},
    }

    if DEMO_MODE:
        demo_probes: dict = {
            "telemetry_source": "ocp-prometheus",
            "metric_names": metrics,
            "sample_count": 42,
            "latest_timestamp": "2026-01-01T00:00:00Z",
        }
        if count_field:
            demo_probes[count_field] = 4
        for t in ("telemetry_endpoint_reachable", metrics_test, "samples_recent"):
            result["tests"][t] = {"passed": True, "message": f"demo: {t} ok", "probes": demo_probes}
        result["success"] = True
        print(json.dumps(result, indent=2))
        return 0

    pf_proc = None
    try:
        local_port = 19090 + (int(time.time()) % 1000)
        prom_targets = [
            ("pod/prometheus-k8s-0", "openshift-monitoring"),
            ("pod/prometheus-user-workload-0", "openshift-user-workload-monitoring"),
        ]
        prom_source = ""
        for target, ns in prom_targets:
            pf_proc = start_port_forward(target, ns, 9090, local_port)
            if pf_proc is not None:
                test_resp = query_prometheus(local_port, "up")
                if test_resp and test_resp.get("status") == "success":
                    prom_source = f"{target} in {ns}"
                    break
                pf_proc.terminate()
                pf_proc = None

        if pf_proc is None:
            result["tests"]["telemetry_endpoint_reachable"] = {
                "passed": False,
                "message": "Failed to connect to any Prometheus instance",
                "probes": {"telemetry_source": "ocp-prometheus"},
            }
            print(json.dumps(result, indent=2))
            return 1

        result["tests"]["telemetry_endpoint_reachable"] = {
            "passed": True,
            "message": f"Prometheus reachable via {prom_source}",
            "probes": {"telemetry_source": "ocp-prometheus"},
        }

        found_metrics: list[str] = []
        total_samples = 0
        latest_ts = ""
        unique_entities: set[str] = set()

        for metric in metrics:
            resp = query_prometheus(local_port, metric)
            if resp and resp.get("status") == "success":
                results = resp.get("data", {}).get("result", [])
                if results:
                    found_metrics.append(metric)
                    total_samples += len(results)
                    for r in results:
                        ts = r.get("value", [0])[0]
                        ts_str = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(ts)))
                        if ts_str > latest_ts:
                            latest_ts = ts_str
                        if count_label:
                            entity = r.get("metric", {}).get(count_label, "")
                            if entity:
                                unique_entities.add(entity)

        probes: dict = {
            "telemetry_source": "ocp-prometheus",
            "metric_names": found_metrics,
            "sample_count": total_samples,
            "latest_timestamp": latest_ts,
        }
        if count_field:
            probes[count_field] = len(unique_entities) if unique_entities else total_samples

        if not found_metrics:
            result["tests"][metrics_test] = {
                "passed": False,
                "message": f"No metrics found for {args.telemetry_type} telemetry",
                "probes": probes,
            }
            print(json.dumps(result, indent=2))
            return 1

        result["tests"][metrics_test] = {
            "passed": True,
            "message": f"Found {len(found_metrics)} metrics with {total_samples} samples",
            "probes": probes,
        }

        if latest_ts:
            latest_epoch = time.mktime(time.strptime(latest_ts, "%Y-%m-%dT%H:%M:%SZ"))
            age_s = time.time() - latest_epoch
            recent = age_s < 600
        else:
            recent = False
            age_s = -1

        result["tests"]["samples_recent"] = {
            "passed": recent,
            "message": (
                f"Latest sample {int(age_s)}s old (within 600s)"
                if recent
                else f"Latest sample too old ({int(age_s)}s) or missing"
            ),
            "probes": probes,
        }

        result["success"] = all(t["passed"] for t in result["tests"].values())

    except Exception as exc:
        result["error"] = str(exc)
        result["error_type"] = type(exc).__name__
    finally:
        if pf_proc is not None:
            pf_proc.terminate()
            pf_proc.wait(timeout=5)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
