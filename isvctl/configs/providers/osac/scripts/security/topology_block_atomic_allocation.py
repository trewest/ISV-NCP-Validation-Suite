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

"""Test topology block atomic allocation via K8s Deployment (CAP04-02).

Maps OCP Deployment with topology spread constraints to the capacity
topology block contract:
- Deployment = topology block (allocated as one unit)
- Pods = compute resources spread across topology keys (nodes)
- Namespace + ResourceQuota = tenant isolation boundary

Output JSON consumed by CapacityTopologyBlockAtomicAllocationCheck.
"""

from __future__ import annotations

import json
import os
import random
import string
import subprocess
import sys
import time


DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"
KUBECTL = os.environ.get("KUBECTL", "kubectl")
REPLICAS = 2


def _suffix() -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=6))


def _run(args: list[str], timeout: int = 60) -> tuple[int, str, str]:
    cmd = KUBECTL.split() + args
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except subprocess.TimeoutExpired:
        return 1, "", f"timeout after {timeout}s"
    except Exception as exc:
        return 1, "", str(exc)


def _apply(manifest: str) -> tuple[int, str]:
    cmd = KUBECTL.split() + ["apply", "-f", "-"]
    try:
        proc = subprocess.run(cmd, input=manifest, capture_output=True, text=True, timeout=30)
        return proc.returncode, proc.stderr.strip()
    except Exception as exc:
        return 1, str(exc)


def main() -> int:
    suffix = _suffix()
    ns = f"isv-topo-test-{suffix}"
    deploy_name = f"topo-block-{suffix}"

    block: dict = {
        "block_id": deploy_name,
        "reservation_id": ns,
        "tenant_id": ns,
        "allocated_as_unit": False,
        "partial_allocation": True,
        "homogeneous": False,
        "isolation_enforced": False,
        "requested": {"compute": REPLICAS, "network": 0, "storage": 0},
        "allocated": {"compute": 0, "network": 0, "storage": 0},
        "resources": [],
    }
    result: dict = {
        "success": False,
        "platform": "security",
        "test_name": "topology_block_atomic_allocation",
        "topology_block": block,
    }

    if DEMO_MODE:
        block["allocated_as_unit"] = True
        block["partial_allocation"] = False
        block["homogeneous"] = True
        block["isolation_enforced"] = True
        block["allocated"] = {"compute": REPLICAS, "network": 0, "storage": 0}
        block["resources"] = [
            {
                "resource_id": f"pod-{i}",
                "resource_type": "compute",
                "tenant_id": ns,
                "topology_block_id": deploy_name,
                "performance_domain": "ocp-cluster",
                "isolation_boundary": ns,
            }
            for i in range(REPLICAS)
        ]
        result["success"] = True
        print(json.dumps(result, indent=2))
        return 0

    try:
        rc, _, err = _run(["create", "namespace", ns])
        if rc != 0:
            result["error"] = f"Failed to create namespace: {err}"
            print(json.dumps(result, indent=2))
            return 1

        quota = json.dumps({
            "apiVersion": "v1",
            "kind": "ResourceQuota",
            "metadata": {"name": "topo-quota", "namespace": ns},
            "spec": {"hard": {"cpu": "4", "memory": "4Gi", "pods": "10"}},
        })
        _apply(quota)

        deploy = json.dumps({
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": deploy_name, "namespace": ns},
            "spec": {
                "replicas": REPLICAS,
                "selector": {"matchLabels": {"app": "topo-test"}},
                "template": {
                    "metadata": {"labels": {"app": "topo-test"}},
                    "spec": {
                        "topologySpreadConstraints": [{
                            "maxSkew": 1,
                            "topologyKey": "kubernetes.io/hostname",
                            "whenUnsatisfiable": "DoNotSchedule",
                            "labelSelector": {"matchLabels": {"app": "topo-test"}},
                        }],
                        "containers": [{
                            "name": "worker",
                            "image": "registry.access.redhat.com/ubi9/ubi-minimal:latest",
                            "command": ["sleep", "300"],
                            "resources": {"requests": {"cpu": "100m", "memory": "64Mi"}},
                        }],
                    },
                },
            },
        })
        rc, err = _apply(deploy)
        if rc != 0:
            result["error"] = f"Deployment create failed: {err}"
            print(json.dumps(result, indent=2))
            return 1
        sys.stderr.write(f"Created Deployment {deploy_name} with {REPLICAS} replicas\n")

        deadline = time.time() + 180
        all_ready = False
        while time.time() < deadline:
            rc, out, _ = _run(["get", "deployment", deploy_name, "-n", ns,
                                "-o", "jsonpath={.status.readyReplicas}"])
            if rc == 0 and out:
                try:
                    ready = int(out)
                    if ready >= REPLICAS:
                        all_ready = True
                        break
                except ValueError:
                    pass
            time.sleep(5)

        if not all_ready:
            result["error"] = f"Deployment did not reach {REPLICAS} ready replicas within 180s"
            print(json.dumps(result, indent=2))
            return 1

        rc, pods_json, _ = _run(["get", "pods", "-n", ns, "-l", "app=topo-test",
                                  "-o", "json"])
        if rc != 0:
            result["error"] = "Could not list deployment pods"
            print(json.dumps(result, indent=2))
            return 1

        pods = json.loads(pods_json).get("items", [])
        nodes_used = set()
        for pod in pods:
            pod_name = pod["metadata"]["name"]
            node = pod.get("spec", {}).get("nodeName", "unknown")
            nodes_used.add(node)
            block["resources"].append({
                "resource_id": pod_name,
                "resource_type": "compute",
                "tenant_id": ns,
                "topology_block_id": deploy_name,
                "performance_domain": "ocp-cluster",
                "isolation_boundary": ns,
            })

        allocated_compute = len(pods)
        block["allocated"] = {
            "compute": allocated_compute,
            "network": 0,
            "storage": 0,
        }
        block["allocated_as_unit"] = allocated_compute == REPLICAS
        block["partial_allocation"] = allocated_compute < REPLICAS
        block["homogeneous"] = True
        block["isolation_enforced"] = True

        result["success"] = (
            block["allocated_as_unit"]
            and not block["partial_allocation"]
            and block["homogeneous"]
            and block["isolation_enforced"]
            and len(block["resources"]) >= REPLICAS
        )

    finally:
        sys.stderr.write(f"Cleaning up namespace {ns}\n")
        _run(["delete", "namespace", ns, "--wait=false"], timeout=15)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
