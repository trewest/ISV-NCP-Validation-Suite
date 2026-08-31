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

"""SDN hardware fault logging test for OSAC/OCP (SDN09-01).

Validates that the OCP monitoring stack (Prometheus + AlertManager) is
operational and captures network hardware fault events. Maps OCP's
built-in monitoring to the provider-neutral SDN fault logging contract.

Checks:
- logging_endpoint_reachable: Prometheus/Thanos pods running, API queryable
- fault_event_source_queryable: network-related metrics present in Prometheus
- log_destination_configured: AlertManager running with alert routes
- event_schema_valid: queried metrics have expected label structure
"""

from __future__ import annotations

import json
import os
import ssl
import subprocess
import sys
import urllib.request

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"
KUBECTL = os.environ.get("KUBECTL", "kubectl").split()
MONITORING_NS = "openshift-monitoring"

NETWORK_METRICS = [
    "node_network_receive_errs_total",
    "node_network_transmit_errs_total",
    "node_network_receive_drop_total",
    "node_network_transmit_drop_total",
]


def run_kubectl(*args: str, timeout: int = 30) -> tuple[int, str, str]:
    cmd = KUBECTL + list(args)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except subprocess.TimeoutExpired:
        return 1, "", f"Command timed out after {timeout}s"


def _get_pods_status(label: str) -> list[dict]:
    rc, out, _ = run_kubectl(
        "get", "pod", "-n", MONITORING_NS, "-l", label,
        "-o", "jsonpath={range .items[*]}{.metadata.name},{.status.phase}{\"\\n\"}{end}",
    )
    if rc != 0 or not out:
        return []
    pods = []
    for line in out.strip().split("\n"):
        parts = line.split(",", 1)
        if len(parts) == 2:
            pods.append({"name": parts[0], "phase": parts[1]})
    return pods


def _get_thanos_route() -> str:
    rc, host, _ = run_kubectl(
        "get", "route", "thanos-querier", "-n", MONITORING_NS,
        "-o", "jsonpath={.spec.host}",
    )
    return host if rc == 0 else ""


def _get_bearer_token() -> str:
    rc, token, _ = run_kubectl(
        "create", "token", "prometheus-k8s", "-n", MONITORING_NS,
        "--duration=600s",
    )
    if rc == 0 and token:
        return token
    rc, token, _ = run_kubectl("whoami", "--token", timeout=5)
    rc2, token2, _ = run_kubectl(
        "config", "view", "--raw", "-o",
        "jsonpath={.users[0].user.token}",
    )
    if rc2 == 0 and token2:
        return token2
    sa_rc, sa_token, _ = run_kubectl(
        "create", "token", "default", "-n", MONITORING_NS,
        "--duration=600s",
    )
    return sa_token if sa_rc == 0 else ""


def _query_prometheus(route: str, token: str, query: str) -> dict | None:
    url = f"https://{route}/api/v1/query?query={urllib.request.quote(query)}"
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except Exception as exc:
        sys.stderr.write(f"Prometheus query failed: {exc}\n")
        return None


def _get_alertmanager_config(token: str) -> dict | None:
    rc, am_host, _ = run_kubectl(
        "get", "route", "alertmanager-main", "-n", MONITORING_NS,
        "-o", "jsonpath={.spec.host}",
    )
    if rc != 0 or not am_host:
        return None
    url = f"https://{am_host}/api/v2/status"
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except Exception as exc:
        sys.stderr.write(f"AlertManager query failed: {exc}\n")
        return None


def main() -> int:
    result: dict = {
        "success": False,
        "platform": "network",
        "test_name": "sdn_hardware_fault_logging",
        "tests": {
            "logging_endpoint_reachable": {"passed": False, "message": ""},
            "fault_event_source_queryable": {"passed": False, "message": ""},
            "log_destination_configured": {"passed": False, "message": ""},
            "event_schema_valid": {"passed": False, "message": ""},
        },
        "log_destination": "",
        "recent_event_count": 0,
    }

    if DEMO_MODE:
        result["success"] = True
        for t in result["tests"].values():
            t["passed"] = True
            t["message"] = "demo mode"
        result["log_destination"] = "prometheus/alertmanager"
        result["recent_event_count"] = 42
        print(json.dumps(result, indent=2))
        return 0

    try:
        # --- Check 1: logging_endpoint_reachable ---
        prom_pods = _get_pods_status("app.kubernetes.io/name=prometheus")
        am_pods = _get_pods_status("app.kubernetes.io/name=alertmanager")
        thanos_pods = _get_pods_status("app.kubernetes.io/name=thanos-query")

        prom_running = [p for p in prom_pods if p["phase"] == "Running"]
        am_running = [p for p in am_pods if p["phase"] == "Running"]

        if not prom_running:
            prom_pods = _get_pods_status("app=prometheus")
            prom_running = [p for p in prom_pods if p["phase"] == "Running"]
        if not am_running:
            am_pods = _get_pods_status("app=alertmanager")
            am_running = [p for p in am_pods if p["phase"] == "Running"]

        route = _get_thanos_route()
        token = _get_bearer_token()

        api_reachable = False
        if route and token:
            prom_resp = _query_prometheus(route, token, "up")
            api_reachable = (
                prom_resp is not None
                and prom_resp.get("status") == "success"
            )

        endpoint_ok = len(prom_running) > 0 and api_reachable
        result["tests"]["logging_endpoint_reachable"]["passed"] = endpoint_ok
        result["tests"]["logging_endpoint_reachable"]["message"] = (
            f"{len(prom_running)} Prometheus pod(s), "
            f"{len(am_running)} AlertManager pod(s), "
            f"API {'reachable' if api_reachable else 'unreachable'}"
        )

        if not endpoint_ok:
            result["tests"]["logging_endpoint_reachable"]["message"] += (
                f" (route={'present' if route else 'missing'}, "
                f"token={'present' if token else 'missing'})"
            )
            print(json.dumps(result, indent=2))
            return 1

        # --- Check 2: fault_event_source_queryable ---
        total_event_count = 0
        found_metrics: list[str] = []

        for metric in NETWORK_METRICS:
            resp = _query_prometheus(route, token, metric)
            if resp and resp.get("status") == "success":
                results = resp.get("data", {}).get("result", [])
                if results:
                    found_metrics.append(metric)
                    total_event_count += len(results)

        fault_queryable = len(found_metrics) > 0
        result["tests"]["fault_event_source_queryable"]["passed"] = fault_queryable
        result["tests"]["fault_event_source_queryable"]["message"] = (
            f"{len(found_metrics)}/{len(NETWORK_METRICS)} network fault metrics present: "
            f"{', '.join(found_metrics)}"
            if fault_queryable
            else "No network fault metrics found in Prometheus"
        )
        result["recent_event_count"] = total_event_count

        # --- Check 3: log_destination_configured ---
        am_status = _get_alertmanager_config(token)
        am_config_ok = am_status is not None

        rc, rules_out, _ = run_kubectl(
            "get", "prometheusrule", "-n", MONITORING_NS,
            "--no-headers", "-o", "custom-columns=NAME:.metadata.name",
        )
        rule_count = len(rules_out.strip().split("\n")) if rc == 0 and rules_out.strip() else 0

        log_dest_ok = am_config_ok and rule_count > 0
        result["tests"]["log_destination_configured"]["passed"] = log_dest_ok
        log_dest = f"AlertManager in {MONITORING_NS}"
        result["log_destination"] = log_dest
        result["tests"]["log_destination_configured"]["message"] = (
            f"{log_dest} ({'operational' if am_config_ok else 'unreachable'}), "
            f"{rule_count} PrometheusRule(s) configured"
        )

        # --- Check 4: event_schema_valid ---
        schema_ok = False
        if found_metrics:
            resp = _query_prometheus(route, token, found_metrics[0])
            if resp and resp.get("status") == "success":
                results = resp.get("data", {}).get("result", [])
                if results:
                    sample = results[0]
                    metric_labels = sample.get("metric", {})
                    required_labels = {"__name__", "instance", "job"}
                    present = required_labels & set(metric_labels.keys())
                    schema_ok = len(present) == len(required_labels)
                    result["tests"]["event_schema_valid"]["message"] = (
                        f"Labels present: {sorted(metric_labels.keys())} "
                        f"(required {sorted(required_labels)} "
                        f"{'all found' if schema_ok else f'missing {sorted(required_labels - present)}'})"
                    )

        if not schema_ok and not found_metrics:
            result["tests"]["event_schema_valid"]["message"] = (
                "Cannot validate schema: no network fault metrics available"
            )

        result["tests"]["event_schema_valid"]["passed"] = schema_ok

        result["success"] = all(t["passed"] for t in result["tests"].values())
        print(json.dumps(result, indent=2))
        return 0 if result["success"] else 1

    except Exception as exc:
        result["error"] = str(exc)
        result["error_type"] = type(exc).__name__
        print(json.dumps(result, indent=2))
        return 1


if __name__ == "__main__":
    sys.exit(main())
