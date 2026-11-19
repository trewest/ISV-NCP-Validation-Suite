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

"""Run disposable networking probes against an explicitly configured Kubernetes cluster."""

import argparse
import http.client
import ipaddress
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

PYTHON_IMAGE = "python:3.12-alpine"
COREDNS_IMAGE = "registry.k8s.io/coredns/coredns:v1.12.3"
HTTP_SERVER = """import http.server, os
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(os.environ['RESPONSE'].encode())
http.server.HTTPServer(('0.0.0.0', 8080), Handler).serve_forever()
"""
HTTP_CLIENT = """import json, sys, urllib.request
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
result = []
for ip in json.loads(sys.argv[1]):
    host = '[' + ip + ']' if ':' in ip else ip
    with opener.open('http://' + host + '/', timeout=10) as response:
        result.append({'address': ip, 'body': response.read(4096).decode()})
print(json.dumps(result))
"""
RESOLVE = "import json,socket,sys; print(json.dumps(sorted({r[4][0] for r in socket.getaddrinfo(sys.argv[1], None, socket.AF_UNSPEC, socket.SOCK_STREAM)})))"


class PrerequisiteMissing(Exception):
    """A required component/configuration is absent before test resources are created."""


class Probe:
    """Use bounded kubectl commands and register cleanup only for owned fixtures."""

    def __init__(self, kubectl: list[str], settings: dict[str, Any]) -> None:
        """Allocate a unique namespace without changing kubeconfig or current context."""
        self.kubectl = kubectl
        self.settings = settings
        self.namespace = "isv-net-" + uuid.uuid4().hex[:12]
        self.timeout = settings.get("timeout", 180)
        if type(self.timeout) is not int or not 10 <= self.timeout <= 600:
            raise ValueError("timeout must be an integer between 10 and 600 seconds")
        self.callbacks: list[tuple[str, Callable[[], None]]] = []
        self.output: dict[str, Any] = {}

    def command(self, args: list[str], payload: Any = None, timeout: int = 45) -> str:
        """Execute argv without shell interpolation; preserve failures as failures."""
        result = subprocess.run(
            [*self.kubectl, *args],
            input=json.dumps(payload) if payload is not None else None,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or f"kubectl exited {result.returncode}")
        return result.stdout

    def get(self, kind: str, name: str = "", namespace: str = "") -> dict[str, Any]:
        """Read JSON, distinguishing missing objects from API/authorization errors."""
        args = ["get", kind]
        if name:
            args += [name, "--ignore-not-found"]
        if namespace:
            args += ["-n", namespace]
        raw = self.command([*args, "-o", "json", "--request-timeout=30s"])
        return json.loads(raw) if raw.strip() else {}

    def create(self, obj: dict[str, Any]) -> None:
        """Create only within this probe's namespace; never apply over existing resources."""
        self.command(["create", "-f", "-"], obj)

    def begin(self) -> None:
        """Create and register a unique namespace after prerequisites are checked."""
        self.command(["create", "namespace", self.namespace])
        self.callbacks.append((f"namespace {self.namespace}", self.delete_namespace))

    def delete_namespace(self) -> None:
        """Wait for finalizers so deleting Service objects also cleans up load balancers."""
        self.command(["delete", "namespace", self.namespace, "--ignore-not-found", "--timeout=600s"], timeout=615)

    def pod(self, name: str, image: str, command: list[str], **spec: Any) -> None:
        """Launch a bounded disposable pod and wait for readiness."""
        container = {
            "name": name,
            "image": image,
            "command": command,
            "resources": {"requests": {"cpu": "50m", "memory": "64Mi"}, "limits": {"cpu": "500m", "memory": "256Mi"}},
        }
        container.update(spec.pop("container", {}))
        self.create(
            {
                "apiVersion": "v1",
                "kind": "Pod",
                "metadata": {"name": name, "namespace": self.namespace, "labels": {"app": name}},
                "spec": {"restartPolicy": "Never", "activeDeadlineSeconds": 3600, "containers": [container], **spec},
            }
        )
        self.command(
            ["wait", "-n", self.namespace, f"pod/{name}", "--for=condition=Ready", f"--timeout={self.timeout}s"],
            timeout=self.timeout + 15,
        )

    def client(self) -> None:
        """Create a pod using the cluster's default DNS and private network path."""
        self.pod(
            "client", self.settings.get("python_image", PYTHON_IMAGE), ["python", "-c", "import time; time.sleep(3600)"]
        )

    def python(self, code: str, *args: str) -> Any:
        """Execute Python inside the client pod and return its JSON result."""
        raw = self.command(["exec", "-n", self.namespace, "client", "--", "python", "-c", code, *args], timeout=60)
        return json.loads(raw)

    def service(self, name: str, selector: str, port: int, target: int, **spec: Any) -> dict[str, Any]:
        """Create a fresh Service and observe the API-assigned fields."""
        annotations = spec.pop("annotations", {})
        protocol = spec.pop("protocol", "TCP")
        self.create(
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {"name": name, "namespace": self.namespace, "annotations": annotations},
                "spec": {
                    "selector": {"app": selector},
                    "ports": [{"port": port, "targetPort": target, "protocol": protocol}],
                    **spec,
                },
            }
        )
        return self.get("service", name, self.namespace)

    def retry(self, callback: Callable[[], Any]) -> Any:
        """Allow bounded convergence for new endpoints and CoreDNS reloads."""
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                return callback()
            except (RuntimeError, OSError, urllib.error.URLError, http.client.HTTPException) as error:
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"Timed out waiting for networking probe: {error}") from error
                time.sleep(3)

    def load_balancer(self) -> None:
        """Create three Service modes and contact each ingress address with a nonce request."""
        modes = self.settings.get("services")
        static = self.settings.get("static_ips")
        if modes is None or not static:
            raise PrerequisiteMissing("LoadBalancer service settings and reserved static_ips are required")
        if not isinstance(modes, dict) or set(modes) != {"external", "internal", "static"}:
            raise ValueError("services must configure external, internal and static modes")
        if not isinstance(static, list) or not all(isinstance(ip, str) for ip in static):
            raise ValueError("static_ips must be a list of IP strings")
        for ip in static:
            ipaddress.ip_address(ip)
        for mode in modes.values():
            if not isinstance(mode, dict) or set(mode) - {"annotations", "loadBalancerClass", "loadBalancerIP"}:
                raise ValueError("Service settings support annotations, loadBalancerClass and loadBalancerIP only")
        self.begin()
        nonce = uuid.uuid4().hex
        self.pod(
            "backend",
            self.settings.get("python_image", PYTHON_IMAGE),
            ["python", "-c", HTTP_SERVER],
            container={"env": [{"name": "RESPONSE", "value": nonce}]},
        )
        self.client()
        for mode, spec in modes.items():
            self.service(mode, "backend", 80, 8080, type="LoadBalancer", **spec)
        observed = []
        for mode in modes:

            def inspect(mode: str = mode) -> dict[str, Any]:
                """Wait for assigned addresses and working backend connectivity."""
                service = self.get("service", mode, self.namespace)
                ingress = service.get("status", {}).get("loadBalancer", {}).get("ingress", [])
                if not ingress:
                    raise RuntimeError(f"{mode}: no LoadBalancer ingress assigned")
                ips = set()
                for endpoint in ingress:
                    if endpoint.get("ip"):
                        ips.add(str(ipaddress.ip_address(endpoint["ip"])))
                    elif endpoint.get("hostname"):
                        if mode == "internal":
                            ips.update(self.python(RESOLVE, endpoint["hostname"]))
                        else:
                            ips.update(
                                r[4][0]
                                for r in socket.getaddrinfo(
                                    endpoint["hostname"], None, socket.AF_UNSPEC, socket.SOCK_STREAM
                                )
                            )
                    else:
                        raise RuntimeError("Ingress contains neither IP nor hostname")
                if mode == "internal":
                    probes = self.python(HTTP_CLIENT, json.dumps(sorted(ips)))
                else:
                    probes = []
                    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                    for ip in sorted(ips):
                        host = f"[{ip}]" if ":" in ip else ip
                        with opener.open(f"http://{host}/", timeout=10) as response:
                            probes.append({"address": ip, "body": response.read(4096).decode()})
                return {
                    "kind": mode,
                    "service_type": service["spec"]["type"],
                    "probe_location": "pod" if mode == "internal" else "controller",
                    "probes": probes,
                }

            observed.append(self.retry(inspect))
        self.output.update(expected_response=nonce, requested_static_ips=static, services=observed)

    def corefile_patch(self, namespace: str, name: str, uid: str, before: str, after: str) -> None:
        """Compare and swap only our Corefile change, preserving unrelated ConfigMap data."""
        patch = [
            {"op": "test", "path": "/metadata/uid", "value": uid},
            {"op": "test", "path": "/data/Corefile", "value": before},
            {"op": "replace", "path": "/data/Corefile", "value": after},
        ]
        self.command(["patch", "configmap", name, "-n", namespace, "--type=json", "-p", json.dumps(patch)])

    def dns_forwarding(self) -> None:
        """Install a unique forwarding zone, query through cluster DNS, then restore it."""
        backend = self.settings.get("backend", "corefile")
        if backend == "openshift_operator":
            self.openshift_dns_forwarding()
            return
        if backend != "corefile":
            raise ValueError("dns_forwarding backend must be 'corefile' or 'openshift_operator'")

        namespace = self.settings.get("namespace", "kube-system")
        name = self.settings.get("configmap", "coredns")
        config = self.get("configmap", name, namespace)
        if not config:
            raise PrerequisiteMissing("CoreDNS ConfigMap is absent")
        original = config.get("data", {}).get("Corefile", "")
        if not re.search(r"^\s*reload(?:\s|$)", original, re.MULTILINE):
            raise PrerequisiteMissing("CoreDNS requires the reload plugin for this probe")
        uid = config["metadata"]["uid"]
        kubernetes = self.get("service", "kubernetes", "default")
        control_expected = kubernetes["spec"].get("clusterIPs") or [kubernetes["spec"]["clusterIP"]]
        self.begin()
        self.client()
        zone = self.namespace + ".invalid"
        query = "canary." + zone
        answer = "198.18.0.42"
        corefile = f"{zone}:53 {{\n  hosts {{\n    {answer} {query}\n    ttl 1\n  }}\n  log\n  errors\n}}\n"
        self.create(
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": "upstream", "namespace": self.namespace},
                "data": {"Corefile": corefile},
            }
        )
        self.pod(
            "upstream",
            self.settings.get("coredns_image", COREDNS_IMAGE),
            ["/coredns", "-conf", "/etc/coredns/Corefile"],
            volumes=[{"name": "config", "configMap": {"name": "upstream"}}],
            container={"volumeMounts": [{"name": "config", "mountPath": "/etc/coredns", "readOnly": True}]},
        )
        service = self.service("upstream", "upstream", 53, 53, protocol="UDP")
        block = f"\n{zone}:53 {{\n  forward . {service['spec']['clusterIP']}\n  errors\n}}\n"

        def restore() -> None:
            """Remove just our unique block; never overwrite concurrent edits or a replacement ConfigMap."""
            for attempt in range(3):
                current = self.get("configmap", name, namespace)
                if current.get("metadata", {}).get("uid") != uid:
                    raise RuntimeError("CoreDNS ConfigMap was replaced; refusing to overwrite it")
                text = current["data"]["Corefile"]
                if block not in text:
                    self.output["rule_restored"] = True
                    return
                try:
                    self.corefile_patch(namespace, name, uid, text, text.replace(block, "", 1))
                    self.output["rule_restored"] = True
                    return
                except RuntimeError:
                    if attempt == 2:
                        raise

        self.callbacks.append(("restore CoreDNS forwarding rule", restore))
        self.corefile_patch(namespace, name, uid, original, original + block)

        def lookup() -> list[str]:
            """Wait until all answers match the designated resolver's unique test record."""
            answers = self.python(RESOLVE, query)
            if set(answers) != {answer}:
                raise RuntimeError(f"Unexpected DNS answers: {answers}")
            return answers

        answers = self.retry(lookup)
        logs = self.command(["logs", "-n", self.namespace, "upstream"])
        control = self.python(RESOLVE, "kubernetes.default.svc." + self.settings.get("cluster_domain", "cluster.local"))
        self.output.update(
            query=query,
            expected_addresses=[answer],
            answers=answers,
            forwarded_query_seen=query in logs,
            control_expected=control_expected,
            control_answers=control,
        )

    def openshift_dns_forwarding(self) -> None:
        """Test conditional forwarding through the OpenShift DNS Operator API."""
        if self.settings.get("allow_operator_rollout") is not True:
            raise PrerequisiteMissing(
                "OpenShift DNS forwarding can roll out cluster DNS; set allow_operator_rollout=true on a test cluster"
            )

        operator_name = self.settings.get("operator", "default")
        dns_namespace = self.settings.get("dns_namespace", "openshift-dns")
        dns_configmap = self.settings.get("dns_configmap", "dns-default")
        operator = self.get("dnses.operator.openshift.io", operator_name)
        if not operator:
            raise PrerequisiteMissing("OpenShift DNS Operator resource is absent")
        original_spec = operator.get("spec", {})
        original_present = "servers" in original_spec
        original_servers = original_spec.get("servers", [])
        if not isinstance(original_servers, list):
            raise ValueError("OpenShift DNS spec.servers must be a list")

        service = self.get("service", "kubernetes", "default")
        control_expected = service["spec"].get("clusterIPs") or [service["spec"]["clusterIP"]]
        self.begin()
        self.client()
        zone = self.namespace + ".invalid"
        query = "canary." + zone
        answer = "198.18.0.42"
        corefile = f".:5353 {{\n  hosts {{\n    {answer} {query}\n    ttl 1\n  }}\n  log\n  errors\n}}\n"
        self.create(
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": "upstream", "namespace": self.namespace},
                "data": {"Corefile": corefile},
            }
        )
        self.pod(
            "upstream",
            self.settings.get("coredns_image", COREDNS_IMAGE),
            ["/coredns", "-conf", "/etc/coredns/Corefile"],
            volumes=[{"name": "config", "configMap": {"name": "upstream"}}],
            container={"volumeMounts": [{"name": "config", "mountPath": "/etc/coredns", "readOnly": True}]},
        )
        upstream = self.service("upstream", "upstream", 53, 5353, protocol="UDP")
        upstream_ip = ipaddress.ip_address(upstream["spec"]["clusterIP"])
        upstream_address = f"[{upstream_ip}]:53" if upstream_ip.version == 6 else f"{upstream_ip}:53"
        server = {
            "name": "isv-" + self.namespace.rsplit("-", 1)[-1],
            "zones": [zone],
            "forwardPlugin": {"policy": "Sequential", "upstreams": [upstream_address]},
        }
        if any(zone in existing.get("zones", []) for existing in original_servers if isinstance(existing, dict)):
            raise ValueError(f"OpenShift DNS already has a forwarding server for {zone}")
        updated_servers = [*original_servers, server]

        def patch_servers(expected: list[dict[str, Any]] | None, desired: list[dict[str, Any]] | None) -> None:
            """Compare and swap only spec.servers, preserving unrelated operator settings."""
            current = self.get("dns.operator.openshift.io", operator_name)
            current_spec = current.get("spec", {})
            current_present = "servers" in current_spec
            current_servers = current_spec.get("servers", [])
            if expected is None:
                if current_present:
                    raise RuntimeError("OpenShift DNS servers changed concurrently; refusing to overwrite them")
                if desired is None:
                    return
                patch = [
                    {
                        "op": "test",
                        "path": "/metadata/resourceVersion",
                        "value": current["metadata"]["resourceVersion"],
                    },
                    {"op": "add", "path": "/spec/servers", "value": desired},
                ]
            else:
                if not current_present or current_servers != expected:
                    raise RuntimeError("OpenShift DNS servers changed concurrently; refusing to overwrite them")
                patch = [{"op": "test", "path": "/spec/servers", "value": expected}]
                if desired is None:
                    patch.append({"op": "remove", "path": "/spec/servers"})
                else:
                    patch.append({"op": "replace", "path": "/spec/servers", "value": desired})
            self.command(
                ["patch", "dnses.operator.openshift.io", operator_name, "--type=json", "-p", json.dumps(patch)]
            )

        def wait_for_dns(zone_present: bool) -> None:
            """Wait for the operator-generated Corefile and DNS DaemonSet rollout."""

            def ready() -> None:
                config = self.get("configmap", dns_configmap, dns_namespace)
                corefile_text = config.get("data", {}).get("Corefile", "")
                if (zone in corefile_text) != zone_present:
                    raise RuntimeError("OpenShift DNS Corefile has not converged to the requested forwarding config")
                daemonset = self.get("daemonset", "dns-default", dns_namespace)
                metadata = daemonset.get("metadata", {})
                status = daemonset.get("status", {})
                desired = status.get("desiredNumberScheduled", 0)
                if (
                    not desired
                    or status.get("observedGeneration", 0) < metadata.get("generation", 0)
                    or status.get("updatedNumberScheduled", 0) != desired
                    or status.get("numberAvailable", 0) < desired
                ):
                    raise RuntimeError("OpenShift DNS DaemonSet rollout is still in progress")

            self.retry(ready)

        def restore() -> None:
            """Restore the original forwarding list without clobbering concurrent edits."""
            current = self.get("dnses.operator.openshift.io", operator_name).get("spec", {})
            current_servers = current.get("servers", [])
            if current_servers == original_servers and ("servers" in current) == original_present:
                self.output["rule_restored"] = True
                return
            if current_servers != updated_servers:
                raise RuntimeError("OpenShift DNS servers changed concurrently; refusing to overwrite them")
            patch_servers(updated_servers, original_servers if original_present else None)
            wait_for_dns(False)
            self.output["rule_restored"] = True

        self.callbacks.append(("restore OpenShift DNS forwarding config", restore))
        patch_servers(original_servers if original_present else None, updated_servers)
        wait_for_dns(True)

        def lookup() -> list[str]:
            """Wait until cluster DNS returns the unique answer from the test upstream."""
            answers = self.python(RESOLVE, query)
            if set(answers) != {answer}:
                raise RuntimeError(f"Unexpected DNS answers: {answers}")
            return answers

        answers = self.retry(lookup)
        logs = self.command(["logs", "-n", self.namespace, "upstream"])
        cluster_domain = self.settings.get("cluster_domain", "cluster.local")
        control = self.python(RESOLVE, "kubernetes.default.svc." + cluster_domain)
        self.output.update(
            query=query,
            expected_addresses=[answer],
            answers=answers,
            forwarded_query_seen=query in logs,
            control_expected=control_expected,
            control_answers=control,
        )

    def cidr_ranges(self) -> None:
        """Read configured CIDRs and check fresh service/pod allocations on this cluster."""
        backend = self.settings.get("backend", "kubernetes")
        if backend == "openshift_ovn":
            self.openshift_ovn_cidr_ranges()
            return
        if backend != "kubernetes":
            raise ValueError("cidr_ranges backend must be 'kubernetes' or 'openshift_ovn'")

        requested = self.settings.get("ranges")
        if requested is None:
            raise PrerequisiteMissing("Explicit service, node and pod ranges from provisioning are required")
        if not isinstance(requested, dict) or set(requested) != {"service", "node", "pod"}:
            raise ValueError("ranges must specify service, node and pod CIDRs")
        for values in requested.values():
            if not isinstance(values, list) or not values or not all(isinstance(v, str) for v in values):
                raise ValueError("Each requested range must be a nonempty list of CIDRs")
            for value in values:
                ipaddress.ip_network(value)
        resources = self.command(["api-resources", "--api-group=networking.k8s.io", "-o", "name"])
        if "servicecidrs.networking.k8s.io" not in resources.split():
            raise PrerequisiteMissing("ServiceCIDR API is unavailable")
        service_cidrs = self.get("servicecidrs.networking.k8s.io")
        nodes = self.get("nodes")["items"]
        if not nodes or any(not (n.get("spec", {}).get("podCIDRs") or n.get("spec", {}).get("podCIDR")) for n in nodes):
            raise PrerequisiteMissing(
                "The CNI does not expose node podCIDRs; a provider-specific range probe is required"
            )
        self.begin()
        self.client()
        service = self.service("allocation", "client", 80, 8080)
        pod = self.get("pod", "client", self.namespace)
        self.output.update(
            requested_ranges=requested,
            service_cidrs=[c for s in service_cidrs["items"] for c in s["spec"]["cidrs"]],
            node_pod_cidrs=[c for n in nodes for c in (n["spec"].get("podCIDRs") or [n["spec"]["podCIDR"]])],
            node_ips=[a["address"] for n in nodes for a in n["status"]["addresses"] if a["type"] == "InternalIP"],
            pod_ips=[a["ip"] for a in pod["status"]["podIPs"]],
            service_ips=service["spec"].get("clusterIPs") or [service["spec"]["clusterIP"]],
        )

    def openshift_ovn_cidr_ranges(self) -> None:
        """Use OpenShift Network status and OVN node annotations as CIDR evidence."""
        requested = self.settings.get("ranges")
        if not isinstance(requested, dict) or set(requested) != {"service", "node", "pod"}:
            raise ValueError("ranges must specify service, node and pod CIDRs")
        for values in requested.values():
            if not isinstance(values, list) or not values or not all(isinstance(value, str) for value in values):
                raise ValueError("Each requested range must be a nonempty list of CIDRs")
            for value in values:
                ipaddress.ip_network(value)

        network = self.get("networks.config.openshift.io", "cluster")
        if not network:
            raise PrerequisiteMissing("OpenShift cluster Network configuration is unavailable")
        status = network.get("status", {})
        if status.get("networkType") != "OVNKubernetes":
            raise PrerequisiteMissing("The OpenShift CIDR probe requires OVNKubernetes")
        service_cidrs = status.get("serviceNetwork")
        cluster_network = status.get("clusterNetwork")
        if not isinstance(service_cidrs, list) or not service_cidrs:
            raise PrerequisiteMissing("OpenShift Network status does not report serviceNetwork")
        if not isinstance(cluster_network, list) or not cluster_network:
            raise PrerequisiteMissing("OpenShift Network status does not report clusterNetwork")
        configured_pod_cidrs = [entry.get("cidr") for entry in cluster_network if isinstance(entry, dict)]
        configured_pod_networks = {ipaddress.ip_network(value) for value in configured_pod_cidrs}
        requested_pod_networks = {ipaddress.ip_network(value) for value in requested["pod"]}
        if not configured_pod_networks or configured_pod_networks != requested_pod_networks:
            raise ValueError("Configured pod CIDRs differ from OpenShift Network status")

        nodes = self.get("nodes").get("items", [])
        if not nodes:
            raise PrerequisiteMissing("The cluster has no nodes to inspect")
        node_pod_cidrs: list[str] = []
        for node in nodes:
            annotations = node.get("metadata", {}).get("annotations", {})
            raw_subnets = annotations.get("k8s.ovn.org/node-subnets")
            if not raw_subnets:
                raise PrerequisiteMissing(
                    f"Node {node.get('metadata', {}).get('name', '<unknown>')} has no OVN pod-subnet annotation"
                )
            try:
                subnet_map = json.loads(raw_subnets) if isinstance(raw_subnets, str) else raw_subnets
            except json.JSONDecodeError as error:
                raise ValueError("OVN node-subnets annotation is not valid JSON") from error
            if not isinstance(subnet_map, dict):
                raise ValueError("OVN node-subnets annotation must be a JSON object")
            default_subnets = subnet_map.get("default")
            if isinstance(default_subnets, str):
                node_pod_cidrs.append(default_subnets)
            elif isinstance(default_subnets, list) and all(isinstance(value, str) for value in default_subnets):
                node_pod_cidrs.extend(default_subnets)
            else:
                raise PrerequisiteMissing(
                    f"Node {node.get('metadata', {}).get('name', '<unknown>')} has no default OVN pod subnet"
                )
        for cidr in node_pod_cidrs:
            ipaddress.ip_network(cidr)

        self.begin()
        self.client()
        service = self.service("allocation", "client", 80, 8080)
        pod = self.get("pod", "client", self.namespace)
        self.output.update(
            requested_ranges=requested,
            service_cidrs=service_cidrs,
            node_pod_cidrs=node_pod_cidrs,
            node_ips=[
                address["address"]
                for node in nodes
                for address in node.get("status", {}).get("addresses", [])
                if address.get("type") == "InternalIP"
            ],
            pod_ips=[address["ip"] for address in pod.get("status", {}).get("podIPs", [])],
            service_ips=service["spec"].get("clusterIPs") or [service["spec"]["clusterIP"]],
        )


def run(name: str, settings: Any) -> dict[str, Any]:
    """Emit evidence or an explicit skip; cleanup failures always override success."""
    result: dict[str, Any] = {"success": False, "platform": "kubernetes", "test_name": name}
    probe = None
    try:
        if settings is None:
            raise PrerequisiteMissing(f"{name} has no explicit probe configuration")
        if not isinstance(settings, dict):
            raise ValueError("Probe configuration must be an object")
        kubectl = shlex.split(os.environ.get("KUBECTL") or "kubectl")
        if not kubectl or not shutil.which(kubectl[0]):
            raise PrerequisiteMissing("Selected kubectl executable is absent")
        probe = Probe(kubectl, settings)
        getattr(probe, name)()
        result["success"] = True
    except PrerequisiteMissing as error:
        result.update(skipped=True, skip_reason=str(error))
    except (
        RuntimeError,
        OSError,
        subprocess.SubprocessError,
        ValueError,
        KeyError,
        TypeError,
        http.client.HTTPException,
    ) as error:
        result["error"] = str(error)
    finally:
        if probe:
            errors = []
            for label, callback in reversed(probe.callbacks):
                try:
                    callback()
                except (RuntimeError, OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as error:
                    errors.append(f"{label}: {error}")
            result.update(probe.output)
            if errors:
                result.update(success=False, cleanup_errors=errors)
    return result


def main() -> int:
    """Select one independently configurable networking probe."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", required=True, choices=("load_balancer", "dns_forwarding", "cidr_ranges"))
    parser.add_argument("--config", default=os.environ.get("K8S_NETWORKING_CONFIG", ""))
    args = parser.parse_args()
    try:
        config = json.loads(Path(args.config).read_text()) if args.config else {}
        if not isinstance(config, dict):
            raise ValueError("Networking configuration must be a JSON object")
        result = run(args.check, config.get(args.check))
    except (OSError, ValueError) as error:
        result = {"success": False, "platform": "kubernetes", "test_name": args.check, "error": str(error)}
    print(json.dumps(result, indent=2))
    return 0 if result["success"] or (result.get("skipped") and not result.get("cleanup_errors")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
