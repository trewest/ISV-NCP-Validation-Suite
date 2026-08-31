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

"""Localized DNS test for OSAC/OCP (SDN06-01).

Validates that OCP provides localized DNS resolution within the cluster:
1. Creates a namespace (VPC analog with auto-provisioned DNS)
2. Deploys a target pod and a ClusterIP Service (hosted zone + record)
3. Verifies DNS settings (Service ClusterIP assigned)
4. Resolves the Service FQDN from a test pod via nslookup

Maps cloud-provider DNS concepts to OCP's CoreDNS + K8s Services:
- VPC with DNS → Namespace (auto-DNS via CoreDNS)
- Hosted Zone → <namespace>.svc.cluster.local domain
- DNS Record → ClusterIP Service → A record
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"
KUBECTL = os.environ.get("KUBECTL", "kubectl").split()
SUFFIX = f"{int(time.time()) % 0xFFFF:04x}"
NAMESPACE = f"isv-dns-test-{SUFFIX}"
SVC_NAME = "test-svc"
TARGET_POD = "dns-target"
PROBE_POD = "dns-probe"


def run_kubectl(*args: str, timeout: int = 30) -> tuple[int, str, str]:
    cmd = KUBECTL + list(args)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except subprocess.TimeoutExpired:
        return 1, "", f"Command timed out after {timeout}s"


def cleanup() -> None:
    sys.stderr.write(f"Cleaning up namespace {NAMESPACE}...\n")
    run_kubectl("delete", "namespace", NAMESPACE, "--ignore-not-found", "--wait=false", timeout=30)


def wait_pod_ready(name: str, timeout_s: int = 90) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        rc, out, _ = run_kubectl(
            "get", "pod", name, "-n", NAMESPACE,
            "-o", "jsonpath={.status.phase}",
        )
        if rc == 0 and out == "Running":
            return True
        time.sleep(3)
    return False


def main() -> int:
    result: dict = {
        "success": False,
        "platform": "network",
        "test_name": "dns_test",
        "tests": {
            "create_vpc_with_dns": {"passed": False, "message": ""},
            "create_hosted_zone": {"passed": False, "message": ""},
            "create_dns_record": {"passed": False, "message": "", "fqdn": ""},
            "verify_dns_settings": {"passed": False, "message": ""},
            "resolve_record": {"passed": False, "message": "", "resolved_ip": ""},
        },
    }

    if DEMO_MODE:
        fqdn = f"{SVC_NAME}.{NAMESPACE}.svc.cluster.local"
        for t in result["tests"].values():
            t["passed"] = True
            t["message"] = "demo"
        result["tests"]["create_dns_record"]["fqdn"] = fqdn
        result["tests"]["resolve_record"]["resolved_ip"] = "10.96.0.42"
        result["success"] = True
        print(json.dumps(result, indent=2))
        return 0

    try:
        # 1. Create namespace (VPC with DNS)
        rc, _, err = run_kubectl("create", "namespace", NAMESPACE)
        if rc != 0:
            result["tests"]["create_vpc_with_dns"]["message"] = f"Failed to create namespace: {err}"
            print(json.dumps(result, indent=2))
            return 1
        result["tests"]["create_vpc_with_dns"]["passed"] = True
        result["tests"]["create_vpc_with_dns"]["message"] = f"Namespace {NAMESPACE} created with auto-DNS"
        sys.stderr.write(f"Created namespace {NAMESPACE}\n")

        # 2. Create target pod + headless service (hosted zone)
        pod_manifest = json.dumps({
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": TARGET_POD, "labels": {"app": "dns-target"}},
            "spec": {
                "containers": [{
                    "name": "target",
                    "image": "registry.access.redhat.com/ubi9/ubi-minimal:latest",
                    "command": ["sleep", "3600"],
                }],
            },
        })
        proc = subprocess.run(
            KUBECTL + ["apply", "-n", NAMESPACE, "-f", "-"],
            input=pod_manifest, capture_output=True, text=True, timeout=15,
        )
        if proc.returncode != 0:
            result["tests"]["create_hosted_zone"]["message"] = f"Failed to create target pod: {proc.stderr}"
            print(json.dumps(result, indent=2))
            return 1

        # Create headless service for the hosted zone
        headless_svc = json.dumps({
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": "dns-zone"},
            "spec": {
                "clusterIP": "None",
                "selector": {"app": "dns-target"},
                "ports": [{"port": 80, "targetPort": 80}],
            },
        })
        proc = subprocess.run(
            KUBECTL + ["apply", "-n", NAMESPACE, "-f", "-"],
            input=headless_svc, capture_output=True, text=True, timeout=15,
        )
        if proc.returncode != 0:
            result["tests"]["create_hosted_zone"]["message"] = f"Failed to create headless service: {proc.stderr}"
            print(json.dumps(result, indent=2))
            return 1

        result["tests"]["create_hosted_zone"]["passed"] = True
        result["tests"]["create_hosted_zone"]["message"] = (
            f"Hosted zone {NAMESPACE}.svc.cluster.local configured via headless Service"
        )
        sys.stderr.write("Created hosted zone (headless service)\n")

        # Wait for target pod
        if not wait_pod_ready(TARGET_POD):
            result["tests"]["create_hosted_zone"]["passed"] = False
            result["tests"]["create_hosted_zone"]["message"] = "Target pod did not reach Running"
            print(json.dumps(result, indent=2))
            return 1

        # 3. Create ClusterIP service (DNS record)
        fqdn = f"{SVC_NAME}.{NAMESPACE}.svc.cluster.local"
        clusterip_svc = json.dumps({
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": SVC_NAME},
            "spec": {
                "selector": {"app": "dns-target"},
                "ports": [{"port": 80, "targetPort": 80}],
                "type": "ClusterIP",
            },
        })
        proc = subprocess.run(
            KUBECTL + ["apply", "-n", NAMESPACE, "-f", "-"],
            input=clusterip_svc, capture_output=True, text=True, timeout=15,
        )
        if proc.returncode != 0:
            result["tests"]["create_dns_record"]["message"] = f"Failed to create service: {proc.stderr}"
            print(json.dumps(result, indent=2))
            return 1

        result["tests"]["create_dns_record"]["passed"] = True
        result["tests"]["create_dns_record"]["fqdn"] = fqdn
        result["tests"]["create_dns_record"]["message"] = f"DNS record created: {fqdn}"
        sys.stderr.write(f"Created DNS record: {fqdn}\n")

        # 4. Verify DNS settings (Service has ClusterIP)
        rc, cluster_ip, _ = run_kubectl(
            "get", "svc", SVC_NAME, "-n", NAMESPACE,
            "-o", "jsonpath={.spec.clusterIP}",
        )
        if rc != 0 or not cluster_ip:
            result["tests"]["verify_dns_settings"]["message"] = "Service has no ClusterIP"
            print(json.dumps(result, indent=2))
            return 1

        result["tests"]["verify_dns_settings"]["passed"] = True
        result["tests"]["verify_dns_settings"]["message"] = (
            f"DNS settings verified: {SVC_NAME} → {cluster_ip}"
        )
        sys.stderr.write(f"Verified DNS settings: ClusterIP={cluster_ip}\n")

        # 5. Resolve record from a test pod
        probe_manifest = json.dumps({
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": PROBE_POD},
            "spec": {
                "containers": [{
                    "name": "probe",
                    "image": "registry.access.redhat.com/ubi9/ubi-minimal:latest",
                    "command": ["sleep", "3600"],
                }],
            },
        })
        proc = subprocess.run(
            KUBECTL + ["apply", "-n", NAMESPACE, "-f", "-"],
            input=probe_manifest, capture_output=True, text=True, timeout=15,
        )
        if proc.returncode != 0:
            result["tests"]["resolve_record"]["message"] = f"Failed to create probe pod: {proc.stderr}"
            print(json.dumps(result, indent=2))
            return 1

        if not wait_pod_ready(PROBE_POD):
            result["tests"]["resolve_record"]["message"] = "Probe pod did not reach Running"
            print(json.dumps(result, indent=2))
            return 1

        # Resolve using getent (available in ubi-minimal) or /etc/hosts via python
        # Try getent first, fall back to python socket
        rc, resolved, _ = run_kubectl(
            "exec", PROBE_POD, "-n", NAMESPACE, "--",
            "python3", "-c",
            f"import socket; print(socket.gethostbyname('{fqdn}'))",
            timeout=15,
        )
        if rc != 0 or not resolved:
            # Fallback: try with cat /etc/resolv.conf to verify DNS is configured,
            # then use a shell-based approach
            rc, resolved, _ = run_kubectl(
                "exec", PROBE_POD, "-n", NAMESPACE, "--",
                "sh", "-c",
                f"getent hosts {fqdn} | awk '{{print $1}}'",
                timeout=15,
            )

        if rc != 0 or not resolved:
            result["tests"]["resolve_record"]["message"] = f"DNS resolution failed for {fqdn}"
            print(json.dumps(result, indent=2))
            return 1

        resolved_ip = resolved.strip().split("\n")[0]
        if resolved_ip != cluster_ip:
            result["tests"]["resolve_record"]["message"] = (
                f"Resolved IP {resolved_ip} != Service ClusterIP {cluster_ip}"
            )
            print(json.dumps(result, indent=2))
            return 1

        result["tests"]["resolve_record"]["passed"] = True
        result["tests"]["resolve_record"]["resolved_ip"] = resolved_ip
        result["tests"]["resolve_record"]["message"] = (
            f"DNS resolution: {fqdn} → {resolved_ip}"
        )
        sys.stderr.write(f"Resolved {fqdn} → {resolved_ip}\n")

        result["success"] = True
        print(json.dumps(result, indent=2))
        return 0

    except Exception as exc:
        result["error"] = str(exc)
        result["error_type"] = type(exc).__name__
        print(json.dumps(result, indent=2))
        return 1
    finally:
        cleanup()


if __name__ == "__main__":
    sys.exit(main())
