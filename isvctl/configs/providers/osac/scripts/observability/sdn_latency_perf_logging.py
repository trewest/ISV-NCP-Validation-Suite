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

"""Query OCP Prometheus for SDN latency/performance metrics.

Probes OVN-Kubernetes and OVS performance metrics (latency, packet
counts) via kubectl port-forward to the cluster Prometheus instance.

Outputs JSON consumed by SdnLatencyPerfLoggingCheck (SDN09-02).
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

PERFORMANCE_METRICS = [
    "ovs_vswitchd_interface_rx_dropped_total",
    "ovs_vswitchd_interface_tx_dropped_total",
    "ovs_vswitchd_interface_rx_errors_total",
    "ovs_vswitchd_interface_tx_errors_total",
    "ovs_vswitchd_bridge_flows_total",
    "ovs_vswitchd_dp_packets_total",
    "ovnkube_controller_pod_creation_latency_seconds_count",
    "ovnkube_controller_pod_creation_latency_seconds_sum",
]

PACKET_METRICS = [
    "container_network_receive_packets_total",
    "container_network_transmit_packets_total",
    "container_network_receive_bytes_total",
    "container_network_transmit_bytes_total",
    "container_network_receive_errors_total",
    "container_network_transmit_errors_total",
]


def run_kubectl(*args: str, timeout: int = 60) -> tuple[int, str, str]:
    cmd = KUBECTL.split() + list(args)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except subprocess.TimeoutExpired:
        return 1, "", f"Command timed out after {timeout}s"
    except Exception as exc:
        return 1, "", str(exc)


def start_port_forward(
    resource: str, namespace: str, remote_port: int, local_port: int
) -> subprocess.Popen | None:
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


def _check_metrics(port: int, metrics: list[str]) -> tuple[list[str], int, str]:
    """Query a list of metrics and return (found_names, total_samples, latest_ts)."""
    found: list[str] = []
    total_samples = 0
    latest_ts = ""
    for metric in metrics:
        resp = query_prometheus(port, metric)
        if resp and resp.get("status") == "success":
            results = resp.get("data", {}).get("result", [])
            if results:
                found.append(metric)
                total_samples += len(results)
                for r in results:
                    ts = r.get("value", [0])[0]
                    ts_str = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(ts)))
                    if ts_str > latest_ts:
                        latest_ts = ts_str
    return found, total_samples, latest_ts


def main() -> int:
    parser = argparse.ArgumentParser(description="SDN latency/performance logging via OCP Prometheus")
    parser.add_argument("--sample-window-seconds", type=int, default=600)
    args = parser.parse_args()

    result: dict = {
        "success": False,
        "platform": "network",
        "test_name": "sdn_latency_perf_logging",
        "tests": {},
        "telemetry_namespace": "",
        "sample_window_seconds": args.sample_window_seconds,
        "probe_resource_id": "",
    }

    if DEMO_MODE:
        result["telemetry_namespace"] = "openshift-monitoring"
        result["probe_resource_id"] = "prometheus-k8s-0"
        for t in ("metrics_endpoint_reachable", "performance_metric_present",
                   "packet_metric_present", "samples_recent"):
            result["tests"][t] = {"passed": True, "message": f"demo: {t} ok"}
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
        prom_ns = ""
        for target, ns in prom_targets:
            pf_proc = start_port_forward(target, ns, 9090, local_port)
            if pf_proc is not None:
                test_resp = query_prometheus(local_port, "up")
                if test_resp and test_resp.get("status") == "success":
                    prom_source = target
                    prom_ns = ns
                    break
                pf_proc.terminate()
                pf_proc = None

        if pf_proc is None:
            result["tests"]["metrics_endpoint_reachable"] = {
                "passed": False,
                "message": "Failed to connect to any Prometheus instance",
            }
            print(json.dumps(result, indent=2))
            return 1

        result["telemetry_namespace"] = prom_ns
        result["probe_resource_id"] = prom_source

        result["tests"]["metrics_endpoint_reachable"] = {
            "passed": True,
            "message": f"Prometheus reachable via {prom_source} in {prom_ns}",
        }

        perf_found, perf_samples, perf_ts = _check_metrics(local_port, PERFORMANCE_METRICS)
        result["tests"]["performance_metric_present"] = {
            "passed": len(perf_found) > 0,
            "message": (
                f"Found {len(perf_found)} performance metrics with {perf_samples} samples"
                if perf_found
                else "No OVS/OVN performance metrics found"
            ),
        }

        pkt_found, pkt_samples, pkt_ts = _check_metrics(local_port, PACKET_METRICS)
        result["tests"]["packet_metric_present"] = {
            "passed": len(pkt_found) > 0,
            "message": (
                f"Found {len(pkt_found)} packet metrics with {pkt_samples} samples"
                if pkt_found
                else "No container network packet metrics found"
            ),
        }

        latest_ts = max(perf_ts, pkt_ts) if perf_ts or pkt_ts else ""
        if latest_ts:
            latest_epoch = time.mktime(time.strptime(latest_ts, "%Y-%m-%dT%H:%M:%SZ"))
            age_s = time.time() - latest_epoch
            recent = age_s < args.sample_window_seconds
        else:
            recent = False
            age_s = -1

        result["tests"]["samples_recent"] = {
            "passed": recent,
            "message": (
                f"Latest sample {int(age_s)}s old (within {args.sample_window_seconds}s)"
                if recent
                else f"No recent samples (age: {int(age_s)}s, threshold: {args.sample_window_seconds}s)"
            ),
        }

        result["success"] = all(t["passed"] for t in result["tests"].values())

    except Exception as exc:
        result["error"] = str(exc)
        result["error_type"] = type(exc).__name__
    finally:
        if pf_proc is not None:
            pf_proc.terminate()
            try:
                pf_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pf_proc.kill()

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
