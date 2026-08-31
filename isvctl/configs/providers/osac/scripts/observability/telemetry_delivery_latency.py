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

"""Measure telemetry delivery latency via OCP Prometheus + ServiceMonitor.

Deploys a lightweight metric exporter pod that emits a custom gauge with a
creation timestamp, creates a ServiceMonitor so OCP Prometheus scrapes it,
then queries Thanos for the metric and calculates the delivery latency
(time from metric creation to first query result).

Outputs JSON consumed by TelemetryDeliveryLatencyCheck.
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


def run_kubectl(*args: str, stdin: str | None = None, timeout: int = 60) -> tuple[int, str, str]:
    cmd = KUBECTL.split() + list(args)
    try:
        proc = subprocess.run(
            cmd,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except subprocess.TimeoutExpired:
        return 1, "", f"Command timed out after {timeout}s"
    except Exception as exc:
        return 1, "", str(exc)


def start_port_forward(resource: str, namespace: str, remote_port: int, local_port: int) -> subprocess.Popen | None:
    """Start kubectl port-forward in background and wait until it's ready."""
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


def query_prometheus_local(port: int, query: str) -> dict | None:
    """Query Prometheus via localhost port-forward (plain HTTP, no auth)."""
    import urllib.request
    import urllib.parse

    url = f"http://localhost:{port}/api/v1/query?{urllib.parse.urlencode({'query': query})}"
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Measure telemetry delivery latency (OSAC)")
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--max-delivery-seconds", type=int, default=600)
    parser.add_argument("--poll-timeout", type=int, default=300)
    args = parser.parse_args()

    suffix = uuid.uuid4().hex[:6]
    metric_name = f"isv_telemetry_probe_{suffix}"
    app_label = f"telemetry-probe-{suffix}"

    result: dict = {
        "success": False,
        "platform": "observability",
        "test_name": "telemetry_delivery_latency",
        "tests": {},
    }

    if DEMO_MODE:
        for check_name in ("telemetry_endpoint_reachable", "delivery_sample_present", "delivery_within_threshold"):
            result["tests"][check_name] = {
                "passed": True,
                "message": f"demo: {check_name} ok",
                "probes": {
                    "telemetry_source": "ocp-prometheus",
                    "observed_delivery_seconds": 15,
                    "max_delivery_seconds": args.max_delivery_seconds,
                },
            }
        result["success"] = True
        print(json.dumps(result, indent=2))
        return 0

    creation_time = int(time.time())
    pf_proc = None

    try:
        # Build the exporter script as a standalone Python file
        exporter_script = (
            "import http.server\n"
            "import os\n"
            "\n"
            "METRIC = os.environ['METRIC_NAME']\n"
            "CREATED = os.environ['CREATED_TS']\n"
            "\n"
            "class Handler(http.server.BaseHTTPRequestHandler):\n"
            "    def do_GET(self):\n"
            '        if self.path == "/metrics":\n'
            "            body = (\n"
            '                "# HELP " + METRIC + " ISV telemetry delivery probe\\n"\n'
            '                "# TYPE " + METRIC + " gauge\\n"\n'
            '                + METRIC + "{created=\\"" + CREATED + "\\"} " + CREATED + "\\n"\n'
            "            )\n"
            "            self.send_response(200)\n"
            '            self.send_header("Content-Type", "text/plain")\n'
            "            self.end_headers()\n"
            "            self.wfile.write(body.encode())\n"
            '        elif self.path == "/healthz":\n'
            "            self.send_response(200)\n"
            "            self.end_headers()\n"
            '            self.wfile.write(b"ok")\n'
            "        else:\n"
            "            self.send_response(404)\n"
            "            self.end_headers()\n"
            "    def log_message(self, *a):\n"
            "        pass\n"
            "\n"
            'http.server.HTTPServer(("", 9090), Handler).serve_forever()\n'
        )

        # Create ConfigMap with the exporter script
        rc, _, err = run_kubectl(
            "create", "configmap", f"{app_label}-script",
            "-n", args.namespace,
            f"--from-literal=exporter.py={exporter_script}",
        )
        if rc != 0:
            result["error"] = f"Failed to create exporter ConfigMap: {err}"
            print(json.dumps(result, indent=2))
            return 1

        # Deploy pod + service + ServiceMonitor
        manifests = f"""\
apiVersion: v1
kind: Pod
metadata:
  name: {app_label}
  namespace: {args.namespace}
  labels:
    app: {app_label}
spec:
  containers:
    - name: exporter
      image: python:3.11-slim
      command: ["python3", "/scripts/exporter.py"]
      env:
        - name: METRIC_NAME
          value: "{metric_name}"
        - name: CREATED_TS
          value: "{creation_time}"
      ports:
        - containerPort: 9090
          name: metrics
      readinessProbe:
        httpGet:
          path: /healthz
          port: 9090
        initialDelaySeconds: 2
        periodSeconds: 3
      volumeMounts:
        - name: script
          mountPath: /scripts
          readOnly: true
  volumes:
    - name: script
      configMap:
        name: {app_label}-script
  terminationGracePeriodSeconds: 0
---
apiVersion: v1
kind: Service
metadata:
  name: {app_label}
  namespace: {args.namespace}
  labels:
    app: {app_label}
spec:
  selector:
    app: {app_label}
  ports:
    - port: 9090
      targetPort: 9090
      name: metrics
---
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: {app_label}
  namespace: {args.namespace}
spec:
  selector:
    matchLabels:
      app: {app_label}
  endpoints:
    - port: metrics
      interval: "15s"
"""
        rc, _, err = run_kubectl("apply", "-f", "-", stdin=manifests)
        if rc != 0:
            result["error"] = f"Failed to deploy exporter: {err}"
            print(json.dumps(result, indent=2))
            return 1

        # Wait for exporter pod to be ready
        rc, _, err = run_kubectl(
            "wait", "--for=condition=Ready", f"pod/{app_label}",
            "-n", args.namespace, "--timeout=120s",
            timeout=140,
        )
        if rc != 0:
            result["error"] = f"Exporter pod not ready: {err}"
            print(json.dumps(result, indent=2))
            return 1

        # TELEMETRY_ENDPOINT_REACHABLE: port-forward to user-workload Prometheus
        # User ServiceMonitors are scraped by prometheus-user-workload, not prometheus-k8s
        local_port = 19090 + (creation_time % 1000)
        prom_targets = [
            ("pod/prometheus-user-workload-0", "openshift-user-workload-monitoring"),
            ("pod/prometheus-k8s-0", "openshift-monitoring"),
        ]
        prom_source = ""
        for target, ns in prom_targets:
            pf_proc = start_port_forward(target, ns, 9090, local_port)
            if pf_proc is not None:
                test_resp = query_prometheus_local(local_port, "up")
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
            "message": f"Prometheus reachable via port-forward to {prom_source}",
            "probes": {"telemetry_source": "ocp-prometheus"},
        }

        # DELIVERY_SAMPLE_PRESENT: poll for the custom metric
        query = f'{metric_name}{{created="{creation_time}"}}'
        deadline = time.time() + args.poll_timeout
        delivery_time = None
        while time.time() < deadline:
            resp = query_prometheus_local(local_port, query)
            if resp and resp.get("status") == "success":
                results = resp.get("data", {}).get("result", [])
                if results:
                    delivery_time = int(time.time())
                    break
            time.sleep(10)

        if delivery_time is None:
            result["tests"]["delivery_sample_present"] = {
                "passed": False,
                "message": f"Metric {metric_name} not found after {args.poll_timeout}s",
                "probes": {
                    "telemetry_source": "ocp-prometheus",
                    "observed_delivery_seconds": args.poll_timeout,
                    "max_delivery_seconds": args.max_delivery_seconds,
                },
            }
            print(json.dumps(result, indent=2))
            return 1

        observed_latency = delivery_time - creation_time
        probes = {
            "telemetry_source": "ocp-prometheus",
            "observed_delivery_seconds": observed_latency,
            "max_delivery_seconds": args.max_delivery_seconds,
        }

        result["tests"]["delivery_sample_present"] = {
            "passed": True,
            "message": f"Metric {metric_name} delivered in {observed_latency}s",
            "probes": probes,
        }

        # DELIVERY_WITHIN_THRESHOLD
        within = observed_latency <= args.max_delivery_seconds
        result["tests"]["delivery_within_threshold"] = {
            "passed": within,
            "message": (
                f"Latency {observed_latency}s within {args.max_delivery_seconds}s threshold"
                if within
                else f"Latency {observed_latency}s exceeds {args.max_delivery_seconds}s threshold"
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
