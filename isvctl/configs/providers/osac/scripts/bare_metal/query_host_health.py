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

"""Query per-host health via K8s node conditions and Prometheus alerts.

Maps OCP node conditions (Ready, MemoryPressure, DiskPressure, PIDPressure)
and any firing Prometheus alerts targeting the node into the provider-neutral
HostHealthCheck JSON format.

Outputs JSON consumed by HostHealthCheck (CAP05-01):
  success, platform, site_id, hosts_checked, hosts[].{host_id, chassis_serial,
  status, health_present, healthy, observed_age_seconds, probe_ids, alerts,
  components}
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
import ssl


DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"
KUBECTL = os.environ.get("KUBECTL", "kubectl")


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
        "get", "route", "prometheus-k8s", "-n", "openshift-monitoring",
        "-o", "jsonpath={.spec.host}",
    )
    if rc == 0 and host:
        return f"https://{host}"
    return None


def get_prometheus_token() -> str | None:
    rc, token, _ = run_kubectl(
        "create", "token", "prometheus-k8s",
        "-n", "openshift-monitoring", "--duration=300s",
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
    except Exception:
        return []


import urllib.parse  # noqa: E402


def get_firing_alerts(prom_url: str, token: str) -> list[dict]:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    url = f"{prom_url}/api/v1/alerts"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
            data = json.loads(resp.read())
            return data.get("data", {}).get("alerts", [])
    except Exception:
        return []


def node_alerts(all_alerts: list[dict], node_name: str) -> list[dict]:
    matched = []
    for alert in all_alerts:
        if alert.get("state") != "firing":
            continue
        labels = alert.get("labels", {})
        if labels.get("node") == node_name or labels.get("instance", "").startswith(node_name):
            severity = labels.get("severity", "warning")
            matched.append({
                "id": labels.get("alertname", "unknown"),
                "target": node_name,
                "message": alert.get("annotations", {}).get("summary", labels.get("alertname", "")),
                "classifications": [severity],
            })
    return matched


def parse_condition_time(ts_str: str) -> float | None:
    try:
        from datetime import datetime, timezone
        for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z"):
            try:
                dt = datetime.strptime(ts_str, fmt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.timestamp()
            except ValueError:
                continue
    except Exception:
        pass
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Query per-host health via K8s + Prometheus (OSAC)")
    parser.add_argument("--site-id", default="osac-dev")
    args = parser.parse_args()

    result: dict = {
        "success": False,
        "platform": "bare_metal",
        "site_id": args.site_id,
        "test_name": "query_host_health",
        "hosts_checked": 0,
        "hosts": [],
    }

    if DEMO_MODE:
        result["hosts_checked"] = 1
        result["hosts"] = [{
            "host_id": "demo-node-0",
            "chassis_serial": "DEMO-SN-001",
            "status": "Ready",
            "health_present": True,
            "healthy": True,
            "observed_age_seconds": 5,
            "probe_ids": ["NodeCondition", "PrometheusAlerts"],
            "alerts": [],
            "components": {
                "cpu": {"status": "ok", "cores": 8},
                "memory": {"status": "ok", "total_gi": 32},
                "disk": {"status": "ok"},
            },
        }]
        result["success"] = True
        print(json.dumps(result, indent=2))
        return 0

    try:
        rc, nodes_json, err = run_kubectl("get", "nodes", "-o", "json")
        if rc != 0 or not nodes_json:
            result["error"] = f"Failed to get nodes: {err}"
            print(json.dumps(result, indent=2))
            return 1

        nodes_data = json.loads(nodes_json)
        items = nodes_data.get("items", [])
        if not items:
            result["error"] = "No nodes found"
            print(json.dumps(result, indent=2))
            return 1

        prom_url = get_prometheus_url()
        prom_token = get_prometheus_token() if prom_url else None
        all_alerts = get_firing_alerts(prom_url, prom_token) if prom_url and prom_token else []

        now = time.time()
        hosts = []

        for node in items:
            name = node["metadata"]["name"]
            conditions = node.get("status", {}).get("conditions", [])
            capacity = node.get("status", {}).get("capacity", {})

            ready = False
            issues = []
            probe_ids = ["NodeCondition"]
            latest_ts = None

            for cond in conditions:
                cond_type = cond.get("type", "")
                cond_status = cond.get("status", "")
                ts_str = cond.get("lastTransitionTime", "")

                ts = parse_condition_time(ts_str)
                if ts is not None and (latest_ts is None or ts > latest_ts):
                    latest_ts = ts

                if cond_type == "Ready":
                    ready = cond_status == "True"
                elif cond_type in ("MemoryPressure", "DiskPressure", "PIDPressure"):
                    if cond_status == "True":
                        issues.append(cond_type)

            alerts = []
            if prom_url and prom_token:
                probe_ids.append("PrometheusAlerts")
                alerts = node_alerts(all_alerts, name)

            observed_age = int(now - latest_ts) if latest_ts else None
            status = "Ready" if ready else "NotReady"
            healthy = ready and not issues and not alerts

            cpu_cores = capacity.get("cpu", "0")
            mem_ki = capacity.get("memory", "0Ki").rstrip("Ki")
            try:
                mem_gi = round(int(mem_ki) / (1024 * 1024), 1)
            except (ValueError, TypeError):
                mem_gi = 0

            host_entry = {
                "host_id": name,
                "chassis_serial": "",
                "status": status,
                "health_present": True,
                "healthy": healthy,
                "observed_age_seconds": observed_age,
                "probe_ids": probe_ids,
                "alerts": alerts + [
                    {
                        "id": issue,
                        "target": name,
                        "message": f"Node condition {issue} is True",
                        "classifications": ["NodePressure"],
                    }
                    for issue in issues
                ],
                "components": {
                    "cpu": {"status": "ok", "cores": int(cpu_cores) if cpu_cores.isdigit() else 0},
                    "memory": {
                        "status": "ok" if "MemoryPressure" not in issues else "pressure",
                        "total_gi": mem_gi,
                    },
                    "disk": {
                        "status": "ok" if "DiskPressure" not in issues else "pressure",
                    },
                },
            }
            hosts.append(host_entry)

        result["hosts_checked"] = len(hosts)
        result["hosts"] = hosts
        result["success"] = True

    except Exception as exc:
        result["error"] = str(exc)
        result["error_type"] = type(exc).__name__

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
