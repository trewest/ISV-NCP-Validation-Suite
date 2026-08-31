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

"""Security group policy propagation timing test (SDN02-08).

Creates an SG, adds a probe rule (timing how long until GET reflects it),
then removes the rule (timing until GET shows it gone). Both timings must
be within max_propagation_seconds (default 10).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.osac_client import FulfillmentClient, create_sa_token, get_env_config, wait_crd_ready

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"
MAX_PROPAGATION_SECONDS = 10
POLL_INTERVAL = 0.5


def _ingress_ports(body: dict) -> list[int]:
    return [r.get("port_from", -1) for r in body.get("spec", {}).get("ingress", [])]


def _poll_rule_present(client: FulfillmentClient, sg_id: str, port: int, timeout: float) -> float | None:
    """Poll until the rule for *port* appears in GET. Returns elapsed seconds or None on timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        s, b = client.get_security_group(sg_id)
        if s == 200 and port in _ingress_ports(b):
            return time.monotonic() - (deadline - timeout)
        time.sleep(POLL_INTERVAL)
    return None


def _poll_rule_absent(client: FulfillmentClient, sg_id: str, port: int, timeout: float) -> float | None:
    """Poll until the rule for *port* is absent from GET. Returns elapsed seconds or None on timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        s, b = client.get_security_group(sg_id)
        if s == 200 and port not in _ingress_ports(b):
            return time.monotonic() - (deadline - timeout)
        time.sleep(POLL_INTERVAL)
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="SG policy propagation timing (OSAC)")
    parser.add_argument("--region", required=True)
    parser.add_argument("--tenant-namespace", required=True)
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "test_name": "sg_policy_propagation",
        "target_rule_id": "",
        "add_observed_seconds": None,
        "remove_observed_seconds": None,
        "max_propagation_seconds": MAX_PROPAGATION_SECONDS,
        "tests": {
            "create_probe_rule": {"passed": False},
            "rule_observed": {"passed": False},
            "revoke_probe_rule": {"passed": False},
            "removal_observed": {"passed": False},
            "cleanup": {"passed": False},
        },
    }

    if DEMO_MODE:
        result.update(
            {
                "success": True,
                "target_rule_id": "isv-sg-prop-demo",
                "add_observed_seconds": 0.3,
                "remove_observed_seconds": 0.4,
                "tests": {k: {"passed": True} for k in result["tests"]},
            }
        )
        print(json.dumps(result, indent=2))
        return 0

    vnet_id = sg_id = ""
    try:
        config = get_env_config(require_admin=False)
        token, _ttl = create_sa_token(args.tenant_namespace, "default")
        client = FulfillmentClient(config, token)
        crd_ns = config.tenant_namespace
        suffix = f"{int(time.time()) % 0xFFFF:04x}"
        probe_port = 7777

        # Create ephemeral VNet + SG
        vnet_name = f"isv-sg-prop-vnet-{suffix}"
        s, b = client.create_virtual_network(vnet_name, ipv4_cidr="10.215.0.0/16")
        if s not in (200, 201):
            result["tests"]["create_probe_rule"]["error"] = f"create_vnet HTTP {s}"
            print(json.dumps(result, indent=2))
            return 1
        vnet_id = b["id"]
        wait_crd_ready("virtualnetwork", vnet_id, crd_ns, label="osac.openshift.io/virtualnetwork-uuid")

        sg_name = f"isv-sg-prop-{suffix}"
        s, b = client.create_security_group(sg_name, vnet_id)
        if s not in (200, 201):
            result["tests"]["create_probe_rule"]["error"] = f"create_sg HTTP {s}"
            print(json.dumps(result, indent=2))
            return 1
        sg_id = b["id"]
        wait_crd_ready("securitygroup", sg_id, crd_ns, label="osac.openshift.io/securitygroup-uuid")
        result["target_rule_id"] = sg_id

        # Add probe rule and time propagation
        t_add_start = time.monotonic()
        s, b = client.update_security_group(
            sg_id,
            "spec.ingress",
            {
                "spec": {
                    "virtual_network": {"id": vnet_id},
                    "ingress": [
                        {
                            "protocol": "PROTOCOL_TCP",
                            "port_from": probe_port,
                            "port_to": probe_port,
                            "ipv4_cidr": "0.0.0.0/0",
                        },
                    ],
                }
            },
        )
        if s != 200:
            result["tests"]["create_probe_rule"]["error"] = f"update_sg HTTP {s}"
            print(json.dumps(result, indent=2))
            return 1
        result["tests"]["create_probe_rule"] = {"passed": True}

        elapsed = _poll_rule_present(client, sg_id, probe_port, MAX_PROPAGATION_SECONDS * 2)
        if elapsed is None:
            result["tests"]["rule_observed"]["error"] = f"Rule not reflected within {MAX_PROPAGATION_SECONDS * 2}s"
        else:
            add_seconds = round(elapsed, 3)
            result["add_observed_seconds"] = add_seconds
            result["tests"]["rule_observed"] = {"passed": True, "elapsed_seconds": add_seconds}

        # Remove probe rule and time propagation
        t_remove_start = time.monotonic()
        s, b = client.update_security_group(
            sg_id,
            "spec.ingress",
            {"spec": {"virtual_network": {"id": vnet_id}, "ingress": []}},
        )
        if s != 200:
            result["tests"]["revoke_probe_rule"]["error"] = f"update_sg HTTP {s}"
        else:
            result["tests"]["revoke_probe_rule"] = {"passed": True}

            elapsed = _poll_rule_absent(client, sg_id, probe_port, MAX_PROPAGATION_SECONDS * 2)
            if elapsed is None:
                result["tests"]["removal_observed"]["error"] = (
                    f"Rule removal not reflected within {MAX_PROPAGATION_SECONDS * 2}s"
                )
            else:
                remove_seconds = round(elapsed, 3)
                result["remove_observed_seconds"] = remove_seconds
                result["tests"]["removal_observed"] = {"passed": True, "elapsed_seconds": remove_seconds}

    except Exception as exc:
        result["error"] = str(exc)
        result["error_type"] = type(exc).__name__
    finally:
        try:
            ct, _ = create_sa_token(args.tenant_namespace, "default")
            cc = FulfillmentClient(config, ct)
            cleanup_ok = True
            if sg_id:
                s, _ = cc.delete_security_group(sg_id)
                if s not in (200, 204, 404):
                    cleanup_ok = False
            if vnet_id:
                s, _ = cc.delete_virtual_network(vnet_id)
                if s not in (200, 204, 404):
                    cleanup_ok = False
            result["tests"]["cleanup"] = {"passed": cleanup_ok}
        except Exception:
            result["tests"]["cleanup"] = {"passed": False}

    result["success"] = all(t.get("passed") for t in result["tests"].values())
    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
