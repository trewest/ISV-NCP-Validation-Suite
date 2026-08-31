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

"""Query OCP Prometheus for storage telemetry metrics.

Supports two modes via --metric-type:
  capacity    — queries kubelet_volume_stats_{capacity,used,available}_bytes
  performance — queries node_disk_{read,written}_bytes_total,
                node_disk_{reads,writes}_completed_total,
                node_disk_{read,write}_time_seconds_total

Both modes use kubectl port-forward to the Prometheus pod for auth-free access.

Outputs JSON consumed by StorageCapacityTelemetryCheck or
StoragePerformanceTelemetryCheck.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid


DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"
KUBECTL = os.environ.get("KUBECTL", "kubectl")

CAPACITY_METRICS = [
    "kubelet_volume_stats_capacity_bytes",
    "kubelet_volume_stats_used_bytes",
    "kubelet_volume_stats_available_bytes",
]
CAPACITY_KINDS = ["total", "used", "free"]
CAPACITY_METRIC_TO_KIND = {
    "kubelet_volume_stats_capacity_bytes": "total",
    "kubelet_volume_stats_used_bytes": "used",
    "kubelet_volume_stats_available_bytes": "free",
}

PERFORMANCE_METRICS = [
    "node_disk_read_bytes_total",
    "node_disk_written_bytes_total",
    "node_disk_reads_completed_total",
    "node_disk_writes_completed_total",
    "node_disk_read_time_seconds_total",
    "node_disk_write_time_seconds_total",
]
PERFORMANCE_KINDS = ["bandwidth", "iops", "latency"]
PERFORMANCE_METRIC_TO_KIND = {
    "node_disk_read_bytes_total": "bandwidth",
    "node_disk_written_bytes_total": "bandwidth",
    "node_disk_reads_completed_total": "iops",
    "node_disk_writes_completed_total": "iops",
    "node_disk_read_time_seconds_total": "latency",
    "node_disk_write_time_seconds_total": "latency",
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
        import urllib.request
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
    import urllib.request
    import urllib.parse
    url = f"http://localhost:{port}/api/v1/query?{urllib.parse.urlencode({'query': query})}"
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Query storage telemetry from OCP Prometheus")
    parser.add_argument("--metric-type", required=True, choices=["capacity", "performance"])
    parser.add_argument("--namespace", help="Filter capacity metrics to this namespace")
    args = parser.parse_args()

    is_capacity = args.metric_type == "capacity"
    metrics = CAPACITY_METRICS if is_capacity else PERFORMANCE_METRICS
    kind_map = CAPACITY_METRIC_TO_KIND if is_capacity else PERFORMANCE_METRIC_TO_KIND
    expected_kinds = CAPACITY_KINDS if is_capacity else PERFORMANCE_KINDS
    kind_field = "capacity_kinds" if is_capacity else "performance_kinds"
    metrics_test = "capacity_metrics_present" if is_capacity else "performance_metrics_present"

    result: dict = {
        "success": False,
        "platform": "observability",
        "test_name": f"storage_{args.metric_type}_telemetry",
        "tests": {},
    }

    if DEMO_MODE:
        demo_probes = {
            "telemetry_source": "ocp-prometheus",
            "metric_names": metrics,
            kind_field: expected_kinds,
            "volumes_checked": 3,
            "sample_count": 6,
            "latest_timestamp": "2026-01-01T00:00:00Z",
        }
        for t in ("telemetry_endpoint_reachable", metrics_test, "samples_recent"):
            result["tests"][t] = {"passed": True, "message": f"demo: {t} ok", "probes": demo_probes}
        result["success"] = True
        print(json.dumps(result, indent=2))
        return 0

    pf_proc = None
    try:
        # TELEMETRY_ENDPOINT_REACHABLE: port-forward to Prometheus pod
        local_port = 19090 + (int(time.time()) % 1000)
        # kubelet and node-exporter metrics live on the platform Prometheus
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

        # METRICS_PRESENT: query each metric
        found_metrics: list[str] = []
        found_kinds: set[str] = set()
        total_samples = 0
        latest_ts = ""
        volumes_checked: set[str] = set()

        for metric in metrics:
            q = metric
            if is_capacity and args.namespace:
                q = f'{metric}{{namespace="{args.namespace}"}}'

            resp = query_prometheus(local_port, q)
            if resp and resp.get("status") == "success":
                results = resp.get("data", {}).get("result", [])
                if results:
                    found_metrics.append(metric)
                    found_kinds.add(kind_map[metric])
                    total_samples += len(results)
                    for r in results:
                        ts = r.get("value", [0])[0]
                        ts_str = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(ts)))
                        if ts_str > latest_ts:
                            latest_ts = ts_str
                        if is_capacity:
                            pvc = r.get("metric", {}).get("persistentvolumeclaim", "")
                            if pvc:
                                volumes_checked.add(pvc)
                        else:
                            device = r.get("metric", {}).get("device", "")
                            if device:
                                volumes_checked.add(device)

        probes = {
            "telemetry_source": "ocp-prometheus",
            "metric_names": found_metrics,
            kind_field: sorted(found_kinds),
            "volumes_checked": len(volumes_checked) if volumes_checked else total_samples,
            "sample_count": total_samples,
            "latest_timestamp": latest_ts,
        }

        missing_kinds = set(expected_kinds) - found_kinds
        if missing_kinds or not found_metrics:
            result["tests"][metrics_test] = {
                "passed": False,
                "message": f"Missing metrics/kinds: found {found_metrics}, kinds {sorted(found_kinds)}, missing kinds {sorted(missing_kinds)}",
                "probes": probes,
            }
            print(json.dumps(result, indent=2))
            return 1

        result["tests"][metrics_test] = {
            "passed": True,
            "message": f"Found {len(found_metrics)} metrics with kinds {sorted(found_kinds)}",
            "probes": probes,
        }

        # SAMPLES_RECENT: check latest timestamp is within last 10 minutes
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
