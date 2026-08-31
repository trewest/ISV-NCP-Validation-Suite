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

"""OCP K8s Inventory — query an existing OCP cluster and output inventory JSON.

Detects GPU nodes, driver version, CSI StorageClasses, control-plane
namespace, and the OCP API endpoint.  The JSON output feeds Jinja2
templates in the k8s suite config (``steps.setup.*``).

Requirements:
  - ``oc`` (or ``kubectl``) configured and accessible
  - NVIDIA GPU Operator installed (for GPU detection)
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from typing import Any


def _kubectl() -> str:
    """Return the kubectl-compatible CLI command."""
    env = os.environ.get("KUBECTL", "").strip()
    if env:
        return env
    for cmd in ("oc", "kubectl"):
        if _which(cmd):
            return cmd
    print("Error: Neither oc nor kubectl found. Set KUBECTL to override.", file=sys.stderr)
    sys.exit(1)


def _which(cmd: str) -> bool:
    return subprocess.run(["which", cmd], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def _run(cmd: str, *, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, check=check)


def _run_json(cmd: str) -> Any:
    r = _run(cmd)
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
# Inventory collectors
# ---------------------------------------------------------------------------


def _detect_api_endpoint(kc: str) -> str:
    r = _run(f"{kc} whoami --show-server")
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip()
    r = _run(f"{kc} config view --minify -o jsonpath='{{.clusters[0].cluster.server}}'")
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip()
    return ""


def _detect_cluster_name(kc: str) -> str:
    r = _run(f"{kc} config current-context")
    return r.stdout.strip() if r.returncode == 0 else "unknown"


def _node_info(kc: str) -> tuple[int, list[str]]:
    data = _run_json(f"{kc} get nodes -o json")
    if not data:
        return 0, []
    items = data.get("items", [])
    names = [item["metadata"]["name"] for item in items if "metadata" in item]
    return len(names), names


def _gpu_info(kc: str) -> tuple[int, int, int]:
    r = _run(f"{kc} get nodes -l nvidia.com/gpu.present=true -o name")
    gpu_node_count = len(r.stdout.strip().splitlines()) if r.returncode == 0 and r.stdout.strip() else 0

    gpu_per_node = 0
    if gpu_node_count > 0:
        r = _run(
            f"{kc} get nodes -l nvidia.com/gpu.present=true"
            " -o jsonpath='{.items[0].status.capacity.nvidia\\.com/gpu}'"
        )
        val = r.stdout.strip().strip("'")
        if val and val != "null":
            try:
                gpu_per_node = int(val)
            except ValueError:
                pass

    return gpu_node_count, gpu_per_node, gpu_node_count * gpu_per_node


def _driver_version(kc: str) -> str:
    parts: list[str] = []
    for suffix in ("major", "minor", "rev"):
        r = _run(
            f"{kc} get nodes -l nvidia.com/gpu.present=true"
            f" -o jsonpath='{{.items[0].metadata.labels.nvidia\\.com/cuda\\.driver\\.{suffix}}}'"
        )
        val = r.stdout.strip().strip("'")
        if val:
            parts.append(val)
        else:
            break
    return ".".join(parts) if parts else "unknown"


def _gpu_operator_namespace(kc: str) -> str:
    for ns in ("gpu-operator", "gpu-operator-resources", "nvidia-gpu-operator"):
        r = _run(f"{kc} get namespace {shlex.quote(ns)}")
        if r.returncode == 0:
            return ns
    return "nvidia-gpu-operator"


def _control_plane_namespace(kc: str) -> str:
    for ns in ("openshift-kube-apiserver", "kube-system"):
        r = _run(f"{kc} get pods -n {shlex.quote(ns)} -l component=kube-apiserver --no-headers")
        if r.returncode == 0 and r.stdout.strip():
            return ns
    return "kube-system"


def _runtime_class(kc: str) -> str:
    r = _run(f"{kc} get runtimeclass nvidia")
    if r.returncode != 0:
        return ""
    # OCP GPU Operator uses OCI prestart hooks, not a CRI-O runtime handler.
    # The RuntimeClass exists but CRI-O has no matching handler, so pods with
    # runtimeClassName=nvidia are rejected. Return "" to omit the field.
    r2 = _run(f"{kc} api-versions")
    if r2.returncode == 0 and "config.openshift.io/v1" in r2.stdout:
        return ""
    return "nvidia"


def _csi_storage_classes(kc: str) -> dict[str, str]:
    """Auto-detect block, shared-fs, and NFS StorageClasses from CSI drivers."""
    block = os.environ.get("K8S_CSI_BLOCK_SC", "")
    shared = os.environ.get("K8S_CSI_SHARED_FS_SC", "")
    nfs = os.environ.get("K8S_CSI_NFS_SC", "")
    if block and shared and nfs:
        return {"block": block, "shared_fs": shared, "nfs": nfs}

    r = _run(f"{kc} get csidrivers -o jsonpath='{{.items[*].metadata.name}}'")
    drivers = set(r.stdout.strip().strip("'").split()) if r.returncode == 0 else set()
    if not drivers:
        return {"block": block, "shared_fs": shared, "nfs": nfs}

    sc_data = _run_json(f"{kc} get sc -o json")
    if not sc_data:
        return {"block": block, "shared_fs": shared, "nfs": nfs}

    shared_fs_provs = re.compile(r"nfs|efs|cephfs|filestore|azurefile")

    for item in sc_data.get("items", []):
        name = item.get("metadata", {}).get("name", "")
        prov = item.get("provisioner", "")
        if prov not in drivers:
            continue
        if "nfs" in prov and not nfs:
            nfs = name
        if shared_fs_provs.search(prov):
            if not shared:
                shared = name
        elif not block:
            block = name

    return {"block": block, "shared_fs": shared, "nfs": nfs}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    kc = _kubectl()

    # Verify cluster connectivity
    r = _run(f"{kc} cluster-info")
    if r.returncode != 0:
        print(f"Error: Cannot connect to Kubernetes cluster (using: {kc})", file=sys.stderr)
        return 1

    api_endpoint = _detect_api_endpoint(kc)
    cluster_name = _detect_cluster_name(kc)
    node_count, nodes = _node_info(kc)
    gpu_node_count, gpu_per_node, total_gpus = _gpu_info(kc)
    driver_ver = _driver_version(kc)
    gpu_ns = _gpu_operator_namespace(kc)
    cp_ns = _control_plane_namespace(kc)
    rt_class = _runtime_class(kc)
    csi = _csi_storage_classes(kc)

    result = {
        "success": True,
        "platform": "kubernetes",
        "cluster_name": cluster_name,
        "node_count": node_count,
        "kubernetes": {
            "driver_version": driver_ver,
            "node_count": node_count,
            "nodes": nodes,
            "gpu_node_count": gpu_node_count,
            "gpu_per_node": gpu_per_node,
            "total_gpus": total_gpus,
            "gpu_operator_namespace": gpu_ns,
            "control_plane_namespace": cp_ns,
            "runtime_class": rt_class,
            "gpu_resource_name": "nvidia.com/gpu",
            "api_endpoint": api_endpoint,
        },
        "csi": {
            "block_storage_class": csi["block"],
            "shared_fs_storage_class": csi["shared_fs"],
            "nfs_storage_class": csi["nfs"],
            "static_volume_handle": "",
            "static_driver_name": "",
        },
    }

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
