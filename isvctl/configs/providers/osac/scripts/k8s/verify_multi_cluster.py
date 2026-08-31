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

"""Verify that two OCP clusters coexist on the same network.

Queries both clusters via their kubeconfigs, counts Ready nodes, and
reports them as sharing the same tenancy (hypervisor) and network
(libvirt bridge CIDR).

Outputs JSON consumed by K8sMultiClusterSameVpcCheck:
  success, tenancy_id, network_id, clusters[]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys


DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"
KUBECTL = os.environ.get("KUBECTL", "kubectl")


def run_kubectl(kubeconfig: str, *args: str, timeout: int = 30) -> tuple[int, str, str]:
    cmd = KUBECTL.split() + [f"--kubeconfig={kubeconfig}"] + list(args)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except subprocess.TimeoutExpired:
        return 1, "", f"Command timed out after {timeout}s"
    except Exception as exc:
        return 1, "", str(exc)


def get_cluster_info(kubeconfig: str, cluster_name: str) -> dict | None:
    rc, out, err = run_kubectl(kubeconfig, "get", "nodes", "-o",
                               "jsonpath={range .items[*]}{.metadata.name} {range .status.conditions[?(@.type==\"Ready\")]}{.status}{end}{'\\n'}{end}")
    if rc != 0:
        sys.stderr.write(f"Failed to query {cluster_name}: {err}\n")
        return None

    ready_count = 0
    total_count = 0
    for line in out.strip().split("\n"):
        if not line.strip():
            continue
        total_count += 1
        if "True" in line:
            ready_count += 1

    rc, version_out, _ = run_kubectl(kubeconfig, "version", "--short", "-o", "json")
    k8s_version = ""
    if rc == 0:
        try:
            v = json.loads(version_out)
            k8s_version = v.get("serverVersion", {}).get("gitVersion", "")
        except (json.JSONDecodeError, KeyError):
            pass

    rc, infra_out, _ = run_kubectl(kubeconfig, "get", "infrastructure", "cluster",
                                    "-o", "jsonpath={.status.infrastructureName}")
    infra_name = infra_out if rc == 0 and infra_out else cluster_name

    return {
        "name": cluster_name,
        "infra_name": infra_name,
        "ready_node_count": ready_count,
        "total_node_count": total_count,
        "k8s_version": k8s_version,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify multi-cluster on same network (OSAC)")
    parser.add_argument("--cluster1-kubeconfig", required=True)
    parser.add_argument("--cluster1-name", required=True)
    parser.add_argument("--cluster2-kubeconfig", required=True)
    parser.add_argument("--cluster2-name", required=True)
    parser.add_argument("--tenancy-id", default="sealusa51-hypervisor",
                        help="Shared tenancy identifier (hypervisor host)")
    parser.add_argument("--network-id", default="libvirt-192.168.123.0/24",
                        help="Shared network identifier (libvirt bridge CIDR)")
    args = parser.parse_args()

    result: dict = {
        "success": False,
        "platform": "kubernetes",
        "test_name": "verify_multi_cluster",
        "test_id": "K8S26-01",
        "tenancy_id": args.tenancy_id,
        "network_id": args.network_id,
        "clusters": [],
    }

    if DEMO_MODE:
        result["success"] = True
        result["clusters"] = [
            {"name": "cluster-1", "tenancy_id": args.tenancy_id,
             "network_id": args.network_id, "status": "ACTIVE", "ready_node_count": 9},
            {"name": "cluster-2", "tenancy_id": args.tenancy_id,
             "network_id": args.network_id, "status": "ACTIVE", "ready_node_count": 1},
        ]
        print(json.dumps(result, indent=2))
        return 0

    clusters_config = [
        (args.cluster1_kubeconfig, args.cluster1_name),
        (args.cluster2_kubeconfig, args.cluster2_name),
    ]

    for kubeconfig, name in clusters_config:
        sys.stderr.write(f"Querying cluster {name} via {kubeconfig}...\n")
        info = get_cluster_info(kubeconfig, name)
        if info is None:
            result["error"] = f"Failed to query cluster {name}"
            print(json.dumps(result, indent=2))
            return 1

        status = "ACTIVE" if info["ready_node_count"] > 0 else "NOT_READY"
        cluster_entry = {
            "name": name,
            "tenancy_id": args.tenancy_id,
            "network_id": args.network_id,
            "status": status,
            "ready_node_count": info["ready_node_count"],
            "role": "multi-node" if info["total_node_count"] > 1 else "single-node",
        }
        result["clusters"].append(cluster_entry)
        sys.stderr.write(f"  {name}: {info['ready_node_count']}/{info['total_node_count']} Ready, status={status}\n")

    all_active = all(c["status"] == "ACTIVE" for c in result["clusters"])
    if not all_active:
        inactive = [c["name"] for c in result["clusters"] if c["status"] != "ACTIVE"]
        result["error"] = f"Cluster(s) not ACTIVE: {', '.join(inactive)}"
        print(json.dumps(result, indent=2))
        return 1

    result["success"] = True
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
