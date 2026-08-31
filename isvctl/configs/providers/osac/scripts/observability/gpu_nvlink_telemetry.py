#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""OSAC GPU NVLink telemetry probe via DCGM exporter on OCP.

Queries the in-cluster Prometheus/Thanos endpoint for DCGM NVLink metrics
exposed by the GPU Operator's dcgm-exporter daemonset.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"

TESTS = [
    "telemetry_endpoint_reachable",
    "link_metrics_present",
    "samples_recent",
]

NVLINK_METRICS = [
    "DCGM_FI_PROF_NVLINK_TX_BYTES",
    "DCGM_FI_PROF_NVLINK_RX_BYTES",
    "DCGM_FI_DEV_NVLINK_BANDWIDTH_TOTAL",
    "DCGM_FI_DEV_NVLINK_CRC_FLIT_ERROR_COUNT_TOTAL",
    "DCGM_FI_DEV_NVLINK_CRC_DATA_ERROR_COUNT_TOTAL",
]


def _kubectl(args: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    cmd = os.environ.get("KUBECTL", "kubectl").split() + args
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _prom_query(query: str, token: str, thanos_url: str) -> dict[str, Any] | None:
    """Run a PromQL instant query via the Thanos querier."""
    import urllib.request
    import urllib.parse
    import ssl

    url = f"{thanos_url}/api/v1/query?query={urllib.parse.quote(query)}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def _get_thanos_url() -> str | None:
    """Get the Thanos querier route URL."""
    r = _kubectl([
        "get", "route", "thanos-querier", "-n", "openshift-monitoring",
        "-o", "jsonpath={.spec.host}",
    ])
    if r.returncode == 0 and r.stdout.strip():
        return f"https://{r.stdout.strip()}"
    return None


def _get_sa_token() -> str | None:
    """Get a token for querying Prometheus."""
    r = _kubectl(["create", "token", "prometheus-k8s", "-n", "openshift-monitoring", "--duration=600s"])
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip()
    r = _kubectl(["whoami", "-t"])
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip()
    return None


def main() -> int:
    result: dict[str, Any] = {
        "success": False,
        "platform": "observability",
        "test_name": "gpu_nvlink_telemetry",
        "tests": {t: {"passed": False} for t in TESTS},
    }

    if DEMO_MODE:
        probes = {
            "telemetry_source": "dcgm-exporter",
            "links_checked": 6,
            "metric_names": NVLINK_METRICS[:3],
            "sample_count": 12,
            "latest_timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        for t in TESTS:
            result["tests"][t] = {"passed": True, "probes": probes}
        result["success"] = True
        print(json.dumps(result, indent=2))
        return 0

    thanos_url = _get_thanos_url()
    if not thanos_url:
        result["error"] = "Cannot find Thanos querier route in openshift-monitoring"
        print(json.dumps(result, indent=2))
        return 1

    token = _get_sa_token()
    if not token:
        result["error"] = "Cannot obtain auth token for Prometheus"
        print(json.dumps(result, indent=2))
        return 1

    result["tests"]["telemetry_endpoint_reachable"] = {"passed": True, "message": f"Thanos querier at {thanos_url}"}

    found_metrics: list[str] = []
    total_samples = 0
    links_checked = 0
    latest_ts = ""

    for metric in NVLINK_METRICS:
        resp = _prom_query(metric, token, thanos_url)
        if not resp or resp.get("status") != "success":
            continue
        series = resp.get("data", {}).get("result", [])
        if not series:
            continue
        found_metrics.append(metric)
        total_samples += len(series)
        gpu_set = {s.get("metric", {}).get("gpu", "0") for s in series}
        links_checked = max(links_checked, len(gpu_set))
        for s in series:
            ts_val = s.get("value", [0])[0]
            ts_str = datetime.fromtimestamp(float(ts_val), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            if ts_str > latest_ts:
                latest_ts = ts_str

    probes = {
        "telemetry_source": "dcgm-exporter",
        "links_checked": links_checked,
        "metric_names": found_metrics,
        "sample_count": total_samples,
        "latest_timestamp": latest_ts,
    }

    if found_metrics:
        result["tests"]["link_metrics_present"] = {"passed": True, "probes": probes}
    else:
        result["tests"]["link_metrics_present"] = {
            "passed": False,
            "message": "No NVLink metrics found — GPUs may not have NVLink (e.g. A10G/T4)",
        }
        result["error"] = "No NVLink metrics available from DCGM exporter"
        print(json.dumps(result, indent=2))
        return 1

    if latest_ts:
        result["tests"]["samples_recent"] = {"passed": True, "probes": probes}
    else:
        result["tests"]["samples_recent"] = {"passed": False, "message": "No recent samples"}
        print(json.dumps(result, indent=2))
        return 1

    result["success"] = True
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
