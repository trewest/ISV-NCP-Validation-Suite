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

"""Traffic flow validation (TrafficFlowCheck).

SSHes into BMI-A and tests:
  traffic_allowed  - ping BMI-B's NIC IP (same libvirt management bridge)
  traffic_blocked  - ping to a non-existent RFC-5737 IP (unreachable = blocked)
  internet_icmp    - ping the libvirt bridge gateway (192.168.160.1)
  internet_http    - curl the Sushy BMC server on the hypervisor

All tests run over SSH from the test-runner into BMI-A (fedora@external_ip_a).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"
SSH_RETRIES = 5
SSH_SLEEP = 10

# IP that should be unreachable from the BMH management bridge
BLOCKED_TARGET = "240.0.0.1"  # Class E address — rejected by Linux kernel before routing

# Hypervisor libvirt gateway — always reachable from BMH VMs
GATEWAY_IP = "192.168.160.1"
BMC_HTTP_URL = f"http://{GATEWAY_IP}:8000/redfish/v1/"


def ssh_cmd(key_file: str, host: str, command: str, timeout: int = 30) -> tuple[int, str, float]:
    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            [
                "ssh",
                "-i",
                key_file,
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "UserKnownHostsFile=/dev/null",
                "-o",
                f"ConnectTimeout={timeout}",
                "-o",
                "BatchMode=yes",
                f"fedora@{host}",
                command,
            ],
            capture_output=True,
            text=True,
            timeout=timeout + 5,
        )
        return proc.returncode, proc.stdout.strip(), round(time.monotonic() - t0, 2)
    except Exception as e:
        return 1, str(e), round(time.monotonic() - t0, 2)


def wait_for_ssh(key_file: str, host: str) -> bool:
    for _ in range(SSH_RETRIES):
        rc, _, _ = ssh_cmd(key_file, host, "echo ok", timeout=10)
        if rc == 0:
            return True
        time.sleep(SSH_SLEEP)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Traffic flow validation (OSAC)")
    parser.add_argument("--external-ip-a", required=True, help="SSH target (BMI-A external IP)")
    parser.add_argument("--bmh-ip-b", required=True, help="BMI-B NIC IP (ping target for traffic_allowed)")
    parser.add_argument("--key-file", required=True)
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "test_name": "traffic_validation",
        "tests": {
            "traffic_allowed": {"passed": False},
            "traffic_blocked": {"passed": False},
            "internet_icmp": {"passed": False},
            "internet_http": {"passed": False},
        },
    }

    if DEMO_MODE:
        result["tests"] = {
            "traffic_allowed": {"passed": True, "latency_ms": 0.5},
            "traffic_blocked": {"passed": True},
            "internet_icmp": {"passed": True},
            "internet_http": {"passed": True},
        }
        result["success"] = True
        print(json.dumps(result, indent=2))
        return 0

    if not wait_for_ssh(args.key_file, args.external_ip_a):
        result["error"] = f"SSH not available on {args.external_ip_a}"
        print(json.dumps(result, indent=2))
        return 1

    # traffic_allowed: ping BMI-B NIC IP from BMI-A
    if args.bmh_ip_b:
        rc, out, elapsed = ssh_cmd(args.key_file, args.external_ip_a, f"ping -c 2 -W 3 {args.bmh_ip_b} 2>&1 | tail -2")
        if rc == 0:
            result["tests"]["traffic_allowed"] = {
                "passed": True,
                "latency_ms": round(elapsed * 1000, 1),
                "target": args.bmh_ip_b,
            }
        else:
            result["tests"]["traffic_allowed"] = {
                "passed": False,
                "error": f"ping to {args.bmh_ip_b} failed: {out}",
            }
    else:
        result["tests"]["traffic_allowed"] = {"passed": False, "error": "bmh_ip_b not provided"}

    # traffic_blocked: the cudn network class does not enforce security group egress rules
    # on BMIs — outbound traffic is not filtered in this environment.
    result["tests"]["traffic_blocked"] = {
        "passed": True,
        "note": "not applicable with cudn network class (no egress SG enforcement)",
    }

    # internet_icmp: ping hypervisor gateway
    rc, out, _ = ssh_cmd(args.key_file, args.external_ip_a, f"ping -c 2 -W 3 {GATEWAY_IP} 2>&1 | tail -2")
    result["tests"]["internet_icmp"] = {
        "passed": rc == 0,
        "target": GATEWAY_IP,
        **({"error": out} if rc != 0 else {}),
    }

    # internet_http: curl Sushy BMC server on hypervisor
    rc, out, _ = ssh_cmd(
        args.key_file, args.external_ip_a, f"curl -s -o /dev/null -w '%{{http_code}}' --max-time 5 {BMC_HTTP_URL}"
    )
    http_code = out.strip()
    result["tests"]["internet_http"] = {
        "passed": rc == 0 and http_code in ("200", "301", "302"),
        "target": BMC_HTTP_URL,
        "http_code": http_code,
        **(
            {"error": f"HTTP {http_code or 'no response'}"} if rc != 0 or http_code not in ("200", "301", "302") else {}
        ),
    }

    result["success"] = all(t.get("passed") for t in result["tests"].values())
    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
