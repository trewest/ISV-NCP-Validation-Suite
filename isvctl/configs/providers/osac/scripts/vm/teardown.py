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

"""Teardown for OSAC VM validation.

Best-effort cleanup of all resources created by the VM test suite:
  1. Delete the ComputeInstance via fulfillment API
  2. Delete Subnet and VirtualNetwork
  3. Delete the InstanceType via private API
  4. Delete the fulfillment-service Tenant (via gRPC)
  5. Delete the Kubernetes Tenant CRD and namespace
  6. Delete the storage-config hub secret

All deletions are best-effort: errors are collected but do not block
subsequent cleanup steps.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
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
)

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"
PREFIX = "isv-"


def main() -> int:
    parser = argparse.ArgumentParser(description="VM teardown (OSAC)")
    parser.add_argument("--region", required=True)
    parser.add_argument("--instance-id", default="")
    parser.add_argument("--tenant-name", default="")
    parser.add_argument("--tenant-namespace", default="")
    parser.add_argument("--instance-type-id", default="")
    parser.add_argument("--sa-token", default="")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "vm",
        "test_name": "teardown",
        "resources_deleted": [],
        "message": "",
    }

    if DEMO_MODE:
        result["success"] = True
        result["message"] = "Demo teardown complete"
        print(json.dumps(result, indent=2))
        return 0

    cleanup_errors: list[str] = []
    try:
        config = get_env_config(require_admin=False)
        admin_config = get_env_config()
        try:
            admin_token = get_admin_token(admin_config)
        except Exception:
            admin_token, _ = create_sa_token(admin_config.tenant_namespace, "admin")

        # 1. Delete ComputeInstance
        if args.instance_id and args.sa_token:
            try:
                client = FulfillmentClient(config, args.sa_token)
                status, _ = client.delete_compute_instance(args.instance_id, token=args.sa_token)
                if status in (200, 204, 404):
                    result["resources_deleted"].append(f"compute_instance:{args.instance_id}")
                else:
                    cleanup_errors.append(f"delete ComputeInstance: HTTP {status}")
            except Exception as e:
                cleanup_errors.append(f"delete ComputeInstance: {e}")

        # 2. Delete Subnets and VirtualNetworks via tenant SA token
        if args.sa_token:
            try:
                client = FulfillmentClient(config, args.sa_token)

                # Delete subnets first
                s, body = client.list_subnets(token=args.sa_token)
                if s == 200 and isinstance(body, dict):
                    for sub in body.get("items", []):
                        name = sub.get("metadata", {}).get("name", "")
                        rid = sub.get("id", "")
                        if name.startswith(PREFIX) and rid:
                            try:
                                client.delete_subnet(rid, token=args.sa_token)
                                result["resources_deleted"].append(f"subnet:{name}")
                            except Exception as e:
                                cleanup_errors.append(f"delete subnet {name}: {e}")

                # Delete VNets
                s, body = client.list_virtual_networks(token=args.sa_token)
                if s == 200 and isinstance(body, dict):
                    for vnet in body.get("items", []):
                        name = vnet.get("metadata", {}).get("name", "")
                        rid = vnet.get("id", "")
                        if name.startswith(PREFIX) and rid:
                            try:
                                client.delete_virtual_network(rid, token=args.sa_token)
                                result["resources_deleted"].append(f"vnet:{name}")
                            except Exception as e:
                                cleanup_errors.append(f"delete VNet {name}: {e}")
            except Exception as e:
                cleanup_errors.append(f"network cleanup: {e}")

        # 3. Delete InstanceType via private API
        if args.instance_type_id:
            try:
                admin_client = FulfillmentClient(config, admin_token)
                s, _ = admin_client.delete_instance_type(args.instance_type_id, admin_token)
                if s in (200, 204, 404):
                    result["resources_deleted"].append(f"instance_type:{args.instance_type_id}")
                else:
                    cleanup_errors.append(f"delete InstanceType: HTTP {s}")
            except Exception as e:
                cleanup_errors.append(f"delete InstanceType: {e}")

        # 4. Delete fulfillment-service Tenant via gRPC
        if args.tenant_name and admin_config.fulfillment_grpc_address:
            try:
                grpcurl_call(
                    admin_config.fulfillment_grpc_address,
                    "osac.private.v1.Tenants/Delete",
                    {"id": args.tenant_name},
                    admin_token,
                    verify_ssl=admin_config.verify_ssl,
                )
                result["resources_deleted"].append(f"fulfillment_tenant:{args.tenant_name}")
            except Exception as e:
                cleanup_errors.append(f"delete fulfillment tenant: {e}")

        # 5. Delete K8s Tenant CRD
        if args.tenant_name:
            try:
                TenantClient(config).delete(args.tenant_name)
                result["resources_deleted"].append(f"tenant_crd:{args.tenant_name}")
            except Exception as e:
                cleanup_errors.append(f"delete tenant CRD: {e}")

        # 6. Delete storage-config hub secret
        if args.tenant_name:
            try:
                kubectl = shutil.which("kubectl") or shutil.which("oc") or "kubectl"
                subprocess.run(
                    [
                        kubectl,
                        "delete",
                        "secret",
                        f"vast-tenant-config-{args.tenant_name}",
                        "-n",
                        config.tenant_namespace,
                        "--ignore-not-found",
                    ],
                    capture_output=True,
                    timeout=15,
                )
                result["resources_deleted"].append(f"secret:vast-tenant-config-{args.tenant_name}")
            except Exception as e:
                cleanup_errors.append(f"delete storage secret: {e}")

        result["message"] = f"Teardown complete: {len(result['resources_deleted'])} resources deleted"
        result["success"] = True

    except Exception as exc:
        result["error"] = str(exc)
        result["error_type"] = type(exc).__name__

    if cleanup_errors:
        result["cleanup_errors"] = cleanup_errors

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
