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

"""Test network connectivity from a running BareMetalInstance via SSH.

Connects to the BMI and runs:
1. SSH reachability (echo ok)
2. Ping the default gateway
3. DNS resolution (nslookup)

Outputs an ``instances`` list and a ``tests`` dict for
NetworkConnectivityCheck validation.
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
SSH_USER = "fedora"


def _ssh_cmd(key_file: str, external_ip: str, remote_cmd: str, timeout: int = 30) -> tuple[int, str, str]:
    """Run a command on the BMI via SSH. Returns (returncode, stdout, stderr)."""
    try:
        proc = subprocess.run(
            [
                "ssh",
                "-i",
                key_file,
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "ConnectTimeout=10",
                "-o",
                "BatchMode=yes",
                f"{SSH_USER}@{external_ip}",
                remote_cmd,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except subprocess.TimeoutExpired:
        return 1, "", "SSH command timed out"
    except Exception as exc:
        return 1, "", str(exc)


def test_ssh_reachable(key_file: str, external_ip: str) -> dict[str, Any]:
    """Verify SSH connectivity with retries."""
    for attempt in range(SSH_RETRIES):
        rc, stdout, _stderr = _ssh_cmd(key_file, external_ip, "echo ok")
        if rc == 0 and "ok" in stdout:
            return {"passed": True, "message": "SSH connection successful"}
        if attempt < SSH_RETRIES - 1:
            time.sleep(SSH_SLEEP)
    return {"passed": False, "message": f"SSH not available after {SSH_RETRIES} attempts"}


def test_ping_gateway(key_file: str, external_ip: str) -> dict[str, Any]:
    """Ping the default gateway from the BMI."""
    rc, stdout, _stderr = _ssh_cmd(
        key_file,
        external_ip,
        'gw=$(ip route | awk \'/default/ {print $3}\' | head -1) && echo "gateway=$gw" && ping -c 3 -W 5 "$gw"',
        timeout=30,
    )
    if rc == 0:
        gw = ""
        for line in stdout.splitlines():
            if line.startswith("gateway="):
                gw = line.split("=", 1)[1]
                break
        return {"passed": True, "message": f"Gateway {gw} reachable"}
    return {"passed": False, "message": f"Gateway ping failed: {stdout}"}


def test_dns_resolution(key_file: str, external_ip: str) -> dict[str, Any]:
    """Test DNS resolution from the BMI."""
    rc, stdout, stderr = _ssh_cmd(
        key_file,
        external_ip,
        "nslookup google.com 2>&1 || host google.com 2>&1 || getent hosts google.com 2>&1",
        timeout=15,
    )
    if rc == 0 and stdout:
        return {"passed": True, "message": "DNS resolves google.com"}
    return {"passed": False, "message": f"DNS resolution failed: {stderr or stdout}"}


def main() -> int:
    parser = argparse.ArgumentParser(description="Network connectivity test (OSAC)")
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--tenant-namespace", required=True)
    parser.add_argument("--key-file", default="", help="Path to private SSH key")
    parser.add_argument("--external-ip", default="", help="External/BMH IP")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "bare_metal",
        "test_name": "connectivity_test",
        "instances": [],
        "tests": {},
    }

    if DEMO_MODE:
        result.update(
            {
                "success": True,
                "instances": [
                    {
                        "instance_id": args.instance_id or "demo-bmi-001",
                        "private_ip": args.external_ip or "192.0.2.1",
                        "public_ip": args.external_ip or "192.0.2.1",
                    }
                ],
                "tests": {
                    "ssh_reachable": {"passed": True, "message": "SSH connection successful"},
                    "ping_gateway": {"passed": True, "message": "Gateway 192.0.2.254 reachable"},
                    "dns_resolution": {"passed": True, "message": "DNS resolves google.com"},
                },
            }
        )
        print(json.dumps(result, indent=2))
        return 0

    if not args.key_file or not args.external_ip:
        result["error"] = "Missing --key-file or --external-ip"
        print(json.dumps(result, indent=2))
        return 1

    result["instances"] = [
        {
            "instance_id": args.instance_id,
            "private_ip": args.external_ip,
            "public_ip": args.external_ip,
        }
    ]

    ssh_result = test_ssh_reachable(args.key_file, args.external_ip)
    result["tests"]["ssh_reachable"] = ssh_result

    if not ssh_result["passed"]:
        result["error"] = "SSH not reachable — skipping remaining tests"
        result["tests"]["ping_gateway"] = {"passed": False, "message": "Skipped: SSH not reachable"}
        result["tests"]["dns_resolution"] = {"passed": False, "message": "Skipped: SSH not reachable"}
        print(json.dumps(result, indent=2))
        return 1

    result["tests"]["ping_gateway"] = test_ping_gateway(args.key_file, args.external_ip)
    result["tests"]["dns_resolution"] = test_dns_resolution(args.key_file, args.external_ip)

    result["success"] = True

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
