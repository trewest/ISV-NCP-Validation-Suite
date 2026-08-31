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

"""Create an ephemeral tenant with networking and instance type for VM tests.

Setup step that provisions everything needed before a ComputeInstance can be
launched:

  1. Register a fulfillment-service Tenant (via gRPC)
  2. Create namespace + Tenant CRD + hub secret for storage controller
  3. Wait for ``tenant.status.storageClasses`` to populate
  4. Create an ephemeral InstanceType via the private admin API
  5. Create a VirtualNetwork + Subnet (required for ``network_attachments``)
  6. Mint a short-lived SA token from the tenant namespace

Output fields consumed by subsequent steps via Jinja2 templates:
  ``tenant_name``, ``tenant_namespace``, ``instance_type_name``,
  ``instance_type_id``, ``subnet_id``, ``vpc_id``, ``sa_token``
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.osac_client import (
    FulfillmentClient,
    TenantClient,
    create_sa_token,
    get_admin_token,
    get_env_config,
    grpcurl_call,
    wait_crd_ready,
)

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"
POLL_INTERVAL = 2
POLL_TIMEOUT = 180


def main() -> int:
    parser = argparse.ArgumentParser(description="Setup VM test tenant (OSAC)")
    parser.add_argument("--region", required=True)
    parser.add_argument("--instance-type", default="")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "vm",
        "test_name": "setup_vm_tenant",
        "tenant_name": "",
        "tenant_namespace": "",
        "instance_type_name": "",
        "instance_type_id": "",
        "subnet_id": "",
        "vpc_id": "",
        "sa_token": "",
    }

    if DEMO_MODE:
        result["success"] = True
        result["tenant_name"] = "isv-vm-tenant-demo"
        result["tenant_namespace"] = "isv-vm-tenant-demo"
        result["instance_type_name"] = "isv-vm-type-demo"
        result["instance_type_id"] = "isv-vm-type-demo-id"
        result["subnet_id"] = "isv-vm-subnet-demo-id"
        result["vpc_id"] = "isv-vm-vnet-demo-id"
        result["sa_token"] = "demo-token"
        print(json.dumps(result, indent=2))
        return 0

    try:
        config = get_env_config()
        if not config.fulfillment_grpc_address:
            raise RuntimeError("OSAC_FULFILLMENT_GRPC_ADDRESS is required")
        try:
            admin_token = get_admin_token(config)
        except Exception:
            admin_token, _ = create_sa_token(config.tenant_namespace, "admin")

        suffix = f"{int(time.time()) % 0xFFFF:04x}"
        tenant_name = f"isv-vm-tenant-{suffix}"
        domain = f"{suffix}.isv-vm-test.local"
        kubectl = shutil.which("kubectl") or shutil.which("oc") or "kubectl"

        # --- 1. Register tenant via gRPC ---
        grpcurl_call(
            config.fulfillment_grpc_address,
            "osac.private.v1.Tenants/Create",
            {"object": {"metadata": {"name": tenant_name}, "spec": {"domains": [domain]}}},
            admin_token,
            verify_ssl=config.verify_ssl,
        )
        result["tenant_name"] = tenant_name

        # --- 2. Create namespace + Tenant CRD ---
        subprocess.run(
            [kubectl, "create", "namespace", tenant_name],
            capture_output=True,
            text=True,
            timeout=15,
        )
        tc = TenantClient(config)
        tc.create(tenant_name)

        # Wait for tenant namespace to be provisioned
        deadline = time.time() + POLL_TIMEOUT
        namespace = ""
        while time.time() < deadline:
            try:
                tenant = tc.get(tenant_name)
                namespace = tenant.get("status", {}).get("namespace", "")
            except RuntimeError:
                namespace = ""
            if namespace:
                break
            time.sleep(POLL_INTERVAL)

        if not namespace:
            result["error"] = f"Tenant '{tenant_name}' namespace not provisioned within {POLL_TIMEOUT}s"
            print(json.dumps(result, indent=2))
            return 1
        result["tenant_namespace"] = namespace

        # --- 3. Create hub secret for storage controller ---
        secret_manifest = (
            f"apiVersion: v1\nkind: Secret\n"
            f"metadata:\n  name: vast-tenant-config-{tenant_name}\n"
            f"  namespace: {config.tenant_namespace}\n"
            f"  labels:\n    osac.openshift.io/tenant: {tenant_name}\n"
            f"stringData:\n  placeholder: 'true'\n"
        )
        subprocess.run(
            [kubectl, "apply", "-f", "-"],
            input=secret_manifest,
            text=True,
            capture_output=True,
            timeout=15,
        )

        # Wait for storage controller to populate tenant.status.storageClasses
        storage_deadline = time.time() + 60
        while time.time() < storage_deadline:
            probe = subprocess.run(
                [
                    kubectl,
                    "get",
                    "tenant",
                    tenant_name,
                    "-n",
                    config.tenant_namespace,
                    "-o",
                    "jsonpath={.status.storageClasses}",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            sc = probe.stdout.strip()
            if sc and sc not in ("null", "[]", ""):
                break
            time.sleep(3)

        # --- 4. Create ephemeral InstanceType via private API ---
        instance_type_name = args.instance_type or f"isv-vm-type-{suffix}"
        client = FulfillmentClient(config, admin_token)
        it_status, it_body = client.create_instance_type(instance_type_name, cores=2, memory_gib=4, admin_token=admin_token)
        if it_status not in (200, 201):
            raise RuntimeError(f"create InstanceType failed (HTTP {it_status}): {it_body}")
        instance_type_id = it_body.get("id", "")
        result["instance_type_name"] = instance_type_name
        result["instance_type_id"] = instance_type_id

        # --- 5. Create VirtualNetwork + Subnet ---
        sa_token, _ttl = create_sa_token(namespace, "default")
        tenant_client = FulfillmentClient(config, sa_token)

        # Discover network class
        network_class = ""
        nc_status, nc_body = tenant_client.list_network_classes()
        if nc_status == 200:
            items = nc_body.get("items", [])
            for nc in items:
                if nc.get("is_default"):
                    network_class = nc.get("id", "")
                    break
            if not network_class and items:
                network_class = items[0].get("id", "")

        vnet_name = f"isv-vm-net-{suffix}"
        cidr = "10.201.0.0/16"
        vs, vb = tenant_client.create_virtual_network(vnet_name, network_class=network_class, ipv4_cidr=cidr)
        if vs not in (200, 201):
            raise RuntimeError(f"create VNet failed (HTTP {vs}): {vb}")
        vnet_id = vb["id"]
        result["vpc_id"] = vnet_id

        # Wait for fulfillment-controller to reconcile VNet CRD and reach READY
        crd_ns = config.tenant_namespace
        wait_crd_ready("virtualnetwork", vnet_id, crd_ns, label="osac.openshift.io/virtualnetwork-uuid")

        vnet_deadline = time.time() + 120
        while time.time() < vnet_deadline:
            vnet_status, vnet_body = tenant_client.get_virtual_network(vnet_name)
            if vnet_status == 200:
                vnet_state = vnet_body.get("status", {}).get("state", "")
                if vnet_state == "VIRTUAL_NETWORK_STATE_READY":
                    break
            time.sleep(5)

        # Create Subnet
        sub_name = f"isv-vm-sub-{suffix}"
        sub_cidr = "10.201.0.0/24"
        ss, sb = tenant_client.create_subnet(sub_name, vnet_id, sub_cidr)
        if ss not in (200, 201):
            raise RuntimeError(f"create subnet failed (HTTP {ss}): {sb}")
        sub_id = sb["id"]

        wait_crd_ready("subnet", sub_id, crd_ns, label="osac.openshift.io/subnet-uuid")
        result["subnet_id"] = sub_id

        # --- 6. Mint fresh SA token for subsequent steps ---
        token, _ttl = create_sa_token(namespace, "default")
        result["sa_token"] = token

        result["success"] = True

    except Exception as exc:
        result["error"] = str(exc)
        result["error_type"] = type(exc).__name__

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
