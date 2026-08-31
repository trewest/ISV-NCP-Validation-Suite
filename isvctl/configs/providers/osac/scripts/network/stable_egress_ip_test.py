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

"""Stable egress IP test (StableEgressIpCheck / DMS05-01).

SSHes into BMI-A and probes its egress IP three times. Verifies all probes
return the same address, confirming a stable outbound IP for allowlisting.
Uses https://api4.my-ip.io/ip as the egress IP discovery endpoint.
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
PROBE_COUNT = 3
# Virtual BMH needs ~2 min to boot after stable_ip_test stop/start cycle.
SSH_RETRIES = 18
SSH_SLEEP = 10
EGRESS_URL = "https://api.ipify.org"


def ssh_cmd(key_file: str, host: str, command: str, timeout: int = 30) -> tuple[int, str]:
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
        return proc.returncode, proc.stdout.strip()
    except Exception as e:
        return 1, str(e)


def wait_for_ssh(key_file: str, host: str) -> bool:
    for _ in range(SSH_RETRIES):
        rc, _ = ssh_cmd(key_file, host, "echo ok", timeout=10)
        if rc == 0:
            return True
        time.sleep(SSH_SLEEP)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Stable egress IP test (OSAC)")
    parser.add_argument("--external-ip", required=True, help="BMI-A external IP (SSH target)")
    parser.add_argument("--key-file", required=True, help="SSH private key path")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "test_name": "stable_egress_ip_test",
        "tests": {
            "create_instance": {"passed": True},  # already done by launch_bm_instances
            "probe_egress_ip": {"passed": False},
            "egress_ip_stable": {"passed": False},
        },
    }

    if DEMO_MODE:
        result["tests"] = {
            "create_instance": {"passed": True},
            "probe_egress_ip": {"passed": True, "probes": PROBE_COUNT, "ip": "203.0.113.1"},
            "egress_ip_stable": {"passed": True},
        }
        result["success"] = True
        print(json.dumps(result, indent=2))
        return 0

    if not wait_for_ssh(args.key_file, args.external_ip):
        result["tests"]["probe_egress_ip"]["error"] = f"SSH not available on {args.external_ip}"
        print(json.dumps(result, indent=2))
        return 1

    # Retry loop — first probe may fail if DNS/network isn't ready yet after a reboot
    probed_ips: list[str] = []
    max_attempts = PROBE_COUNT + 3
    for i in range(max_attempts):
        rc, out = ssh_cmd(args.key_file, args.external_ip, f"curl -s --max-time 15 {EGRESS_URL} 2>/dev/null || echo ''")
        ip = out.strip()
        if ip:
            probed_ips.append(ip)
            if len(probed_ips) >= PROBE_COUNT:
                break
        time.sleep(5)

    if len(probed_ips) < PROBE_COUNT:
        result["tests"]["probe_egress_ip"]["error"] = (
            f"Only {len(probed_ips)}/{PROBE_COUNT} probes returned an IP after {max_attempts} attempts"
        )
        print(json.dumps(result, indent=2))
        return 1

    result["tests"]["probe_egress_ip"] = {"passed": True, "probes": PROBE_COUNT, "ip": probed_ips[0]}

    stable = len(set(probed_ips)) == 1
    result["tests"]["egress_ip_stable"] = {
        "passed": stable,
        **({"error": f"Egress IPs varied across probes: {probed_ips}"} if not stable else {}),
    }

    result["success"] = all(t.get("passed") for t in result["tests"].values())
    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
