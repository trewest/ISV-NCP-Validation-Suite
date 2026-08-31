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

"""Query North-South network telemetry from Prometheus.

Verifies that ingress/egress (north-south) network metrics are available
via OCP's built-in Prometheus by querying HAProxy frontend metrics from
the OpenShift router.

Outputs JSON consumed by NorthSouthNetworkTelemetryCheck (OBS06-01):
  success, platform, test_name,
  tests.{telemetry_endpoint_reachable, plane_metrics_present, samples_recent}
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import UTC, datetime

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"
KUBECTL = os.environ.get("KUBECTL", "kubectl")

NS_METRICS = [
    "haproxy_frontend_bytes_in_total",
    "haproxy_frontend_bytes_out_total",
    "haproxy_frontend_http_responses_total",
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


def get_prometheus_url() -> str | None:
    rc, host, _ = run_kubectl(
        "get",
        "route",
        "prometheus-k8s",
        "-n",
        "openshift-monitoring",
        "-o",
        "jsonpath={.spec.host}",
    )
    if rc == 0 and host:
        return f"https://{host}"
    return None


def get_auth_token() -> str | None:
    rc, token, _ = run_kubectl("whoami", "-t")
    if rc == 0 and token:
        return token
    rc, token, _ = run_kubectl(
        "create",
        "token",
        "prometheus-k8s",
        "-n",
        "openshift-monitoring",
        "--duration=300s",
    )
    if rc == 0 and token:
        return token
    return None


def query_prometheus(prom_url: str, token: str, query: str) -> list[dict]:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    url = f"{prom_url}/api/v1/query?query={urllib.parse.quote(query)}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
            data = json.loads(resp.read())
            return data.get("data", {}).get("result", [])
    except Exception as exc:
        sys.stderr.write(f"Prometheus query failed: {exc}\n")
        return []


def main() -> int:
    parser = argparse.ArgumentParser(description="Query N-S network telemetry (OSAC)")
    parser.add_argument("--site-id", default="osac-dev")
    parser.parse_args()

    result: dict = {
        "success": False,
        "platform": "observability",
        "test_name": "north_south_network_telemetry",
        "tests": {},
    }

    if DEMO_MODE:
        result["success"] = True
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        result["tests"] = {
            "telemetry_endpoint_reachable": {
                "passed": True,
                "message": "Prometheus endpoint reachable",
                "probes": {"telemetry_source": "prometheus"},
            },
            "plane_metrics_present": {
                "passed": True,
                "message": f"{len(NS_METRICS)} N-S metrics found",
                "probes": {"metric_names": NS_METRICS},
            },
            "samples_recent": {
                "passed": True,
                "message": "Recent samples available",
                "probes": {"sample_count": 10, "latest_timestamp": now},
            },
        }
        print(json.dumps(result, indent=2))
        return 0

    try:
        prom_url = get_prometheus_url()
        if not prom_url:
            result["tests"]["telemetry_endpoint_reachable"] = {
                "passed": False,
                "message": "Prometheus route not found in openshift-monitoring",
                "probes": {"telemetry_source": ""},
            }
            result["error"] = "No Prometheus route"
            print(json.dumps(result, indent=2))
            return 1

        token = get_auth_token()
        if not token:
            result["tests"]["telemetry_endpoint_reachable"] = {
                "passed": False,
                "message": "Could not obtain auth token for Prometheus",
                "probes": {"telemetry_source": "prometheus"},
            }
            result["error"] = "No auth token"
            print(json.dumps(result, indent=2))
            return 1

        test_results = query_prometheus(prom_url, token, "up{job='prometheus-k8s'}")
        if not test_results:
            result["tests"]["telemetry_endpoint_reachable"] = {
                "passed": False,
                "message": "Prometheus endpoint returned no data for health check",
                "probes": {"telemetry_source": "prometheus"},
            }
            result["error"] = "Prometheus unreachable or auth failed"
            print(json.dumps(result, indent=2))
            return 1

        result["tests"]["telemetry_endpoint_reachable"] = {
            "passed": True,
            "message": f"Prometheus reachable at {prom_url}",
            "probes": {"telemetry_source": "prometheus"},
        }
        sys.stderr.write(f"Prometheus reachable at {prom_url}\n")

        found_metrics = []
        total_samples = 0
        latest_ts = 0.0

        for metric_name in NS_METRICS:
            samples = query_prometheus(prom_url, token, metric_name)
            if samples:
                found_metrics.append(metric_name)
                total_samples += len(samples)
                for s in samples:
                    ts = float(s.get("value", [0])[0])
                    if ts > latest_ts:
                        latest_ts = ts
                sys.stderr.write(f"  {metric_name}: {len(samples)} samples\n")
            else:
                sys.stderr.write(f"  {metric_name}: no samples\n")

        if not found_metrics:
            result["tests"]["plane_metrics_present"] = {
                "passed": False,
                "message": "No N-S network metrics found",
                "probes": {"metric_names": []},
            }
            result["error"] = "No N-S metrics available"
            print(json.dumps(result, indent=2))
            return 1

        result["tests"]["plane_metrics_present"] = {
            "passed": True,
            "message": f"{len(found_metrics)} N-S metrics found",
            "probes": {"metric_names": found_metrics},
        }

        latest_iso = ""
        if latest_ts > 0:
            latest_iso = datetime.fromtimestamp(latest_ts, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

        result["tests"]["samples_recent"] = {
            "passed": total_samples > 0,
            "message": f"{total_samples} recent samples, latest at {latest_iso}",
            "probes": {
                "sample_count": total_samples,
                "latest_timestamp": latest_iso,
            },
        }

        all_passed = all(t.get("passed", False) for t in result["tests"].values())
        result["success"] = all_passed

    except Exception as exc:
        result["error"] = str(exc)
        result["error_type"] = type(exc).__name__

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
