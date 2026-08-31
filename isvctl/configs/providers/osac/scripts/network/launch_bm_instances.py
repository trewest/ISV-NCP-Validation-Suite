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

"""Launch two BareMetalInstances for network connectivity/stability tests.

Shared setup for connectivity_test, stable_ip_test, stable_egress_ip_test,
and traffic_validation steps. Emits IDs, external IPs, BMH NIC IPs, and
a shared SSH key file for both instances.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.osac_client import FulfillmentClient, create_sa_token, get_env_config

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"
RUN_STRATEGY_ALWAYS = "BARE_METAL_INSTANCE_RUN_STRATEGY_ALWAYS"
PROVISION_TIMEOUT = 1800


def generate_ssh_key(key_dir: str) -> tuple[str, str]:
    key_path = os.path.join(key_dir, "osac_net_bmi_key")
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", key_path],
        capture_output=True,
        timeout=30,
        check=True,
    )
    with open(f"{key_path}.pub") as fh:
        pub_key = fh.read().strip()
    return key_path, pub_key


def extract_external_ip(body: dict[str, Any]) -> str | None:
    status = body.get("status", {})
    for field in ("external_ip", "externalIp", "public_ip", "publicIp"):
        ip = status.get(field)
        if ip:
            return str(ip)
    for att in status.get("network_attachments", []):
        for field in ("external_ip", "public_ip"):
            ip = att.get(field)
            if ip:
                return str(ip)
    return None


def get_bmh_ip(bmi_id: str, operator_ns: str) -> str | None:
    try:
        import shutil

        kubectl = shutil.which("kubectl") or shutil.which("oc")
        if not kubectl:
            return None
        crd_name = f"bmi-{bmi_id}"
        r = subprocess.run(
            [kubectl, "get", "baremetalinstance", crd_name, "-n", operator_ns, "-o", "jsonpath={.spec.externalHostID}"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if r.returncode != 0 or not r.stdout.strip():
            return None
        parts = r.stdout.strip().split("/", 1)
        if len(parts) != 2:
            return None
        bmh_ns, bmh_name = parts
        r2 = subprocess.run(
            [kubectl, "get", "baremetalhost", bmh_name, "-n", bmh_ns, "-o", "jsonpath={.status.hardware.nics[0].ip}"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        ip = r2.stdout.strip()
        return ip if ip else None
    except Exception:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Launch 2 BMIs for network tests (OSAC)")
    parser.add_argument("--tenant-namespace", required=True)
    parser.add_argument("--region", default="")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "test_name": "launch_bm_instances",
        "instance_a_id": "",
        "instance_b_id": "",
        "external_ip_a": "",
        "external_ip_b": "",
        "bmh_ip_a": "",
        "bmh_ip_b": "",
        "key_file": "",
    }

    if DEMO_MODE:
        result.update(
            {
                "success": True,
                "instance_a_id": "demo-bmi-net-a",
                "instance_b_id": "demo-bmi-net-b",
                "external_ip_a": "192.168.160.201",
                "external_ip_b": "192.168.160.202",
                "bmh_ip_a": "192.168.160.201",
                "bmh_ip_b": "192.168.160.202",
                "key_file": "/tmp/demo-net-bmi.pem",
            }
        )
        print(json.dumps(result, indent=2))
        return 0

    key_dir = tempfile.mkdtemp(prefix="osac-net-bmi-")
    bmi_a_id = bmi_b_id = ""
    try:
        config = get_env_config(require_admin=False)
        token, _ttl = create_sa_token(args.tenant_namespace, "default")
        client = FulfillmentClient(config, token)
        operator_ns = config.tenant_namespace

        key_file, pub_key = generate_ssh_key(key_dir)
        result["key_file"] = key_file

        catalog_item_id = os.environ.get("OSAC_CATALOG_ITEM", "") or client.get_baremetal_catalog_item_id()

        suffix = f"{int(time.time()) % 0xFFFF:04x}"

        # Launch BMI-A
        s, b = client.create_bare_metal_instance(
            {
                "metadata": {
                    "name": f"isv-net-bmi-a-{suffix}",
                    "labels": {"name": "isv-net-validation", "created-by": "isv-validation"},
                },
                "spec": {
                    "catalog_item": {"id": catalog_item_id},
                    "ssh_public_key": pub_key,
                    "auto_external_ip_attachment": True,
                    "run_strategy": RUN_STRATEGY_ALWAYS,
                },
            }
        )
        if s not in (200, 201):
            result["error"] = f"Create BMI-A failed (HTTP {s}): {b}"
            print(json.dumps(result, indent=2))
            return 1
        bmi_a_id = b["id"]
        result["instance_a_id"] = bmi_a_id

        # Launch BMI-B
        s, b = client.create_bare_metal_instance(
            {
                "metadata": {
                    "name": f"isv-net-bmi-b-{suffix}",
                    "labels": {"name": "isv-net-validation", "created-by": "isv-validation"},
                },
                "spec": {
                    "catalog_item": {"id": catalog_item_id},
                    "ssh_public_key": pub_key,
                    "auto_external_ip_attachment": True,
                    "run_strategy": RUN_STRATEGY_ALWAYS,
                },
            }
        )
        if s not in (200, 201):
            result["error"] = f"Create BMI-B failed (HTTP {s}): {b}"
            print(json.dumps(result, indent=2))
            return 1
        bmi_b_id = b["id"]
        result["instance_b_id"] = bmi_b_id

        # Wait for both to reach RUNNING
        body_a = client.wait_bare_metal_instance_state(bmi_a_id, "running", timeout=PROVISION_TIMEOUT)
        body_b = client.wait_bare_metal_instance_state(bmi_b_id, "running", timeout=PROVISION_TIMEOUT)

        # Retry ExternalIP extraction — it may lag a few seconds behind state=RUNNING
        for attempt in range(12):
            ip_a = extract_external_ip(body_a)
            if ip_a:
                break
            time.sleep(5)
            _, body_a = client.get_bare_metal_instance(bmi_a_id)
        bmh_ip_a = get_bmh_ip(bmi_a_id, operator_ns) or ""
        result["external_ip_a"] = ip_a or bmh_ip_a
        result["bmh_ip_a"] = bmh_ip_a

        for attempt in range(12):
            ip_b = extract_external_ip(body_b)
            if ip_b:
                break
            time.sleep(5)
            _, body_b = client.get_bare_metal_instance(bmi_b_id)
        bmh_ip_b = get_bmh_ip(bmi_b_id, operator_ns) or ""
        result["external_ip_b"] = ip_b or bmh_ip_b
        result["bmh_ip_b"] = bmh_ip_b

        # external_ip_a is required (SSH target for tests); external_ip_b is optional
        # (BMI-B is only used as a ping target via its BMH NIC IP)
        result["success"] = bool(result["instance_a_id"] and result["instance_b_id"] and result["external_ip_a"])

    except Exception as exc:
        result["error"] = str(exc)
        result["error_type"] = type(exc).__name__

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
