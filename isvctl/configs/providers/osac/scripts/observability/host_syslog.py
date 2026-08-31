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

"""Query host syslogs via oc adm node-logs or K8s node proxy API.

On OCP clusters, uses `oc adm node-logs --tail=100` for efficient
tail-based access to recent journal entries. Falls back to the kubelet
/logs/journal node proxy endpoint on non-OCP clusters.

Outputs JSON consumed by HostSyslogCheck.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone


DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"
KUBECTL = os.environ.get("KUBECTL", "kubectl")

SYSLOG_TS_RE = re.compile(
    r"^(\w{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})"
)


def run_cmd(*args: str, timeout: int = 60) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(list(args), capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except subprocess.TimeoutExpired:
        return 1, "", f"Command timed out after {timeout}s"
    except Exception as exc:
        return 1, "", str(exc)


def run_kubectl(*args: str, timeout: int = 60) -> tuple[int, str, str]:
    cmd = KUBECTL.split() + list(args)
    return run_cmd(*cmd, timeout=timeout)


def _find_oc() -> str | None:
    """Find the oc binary if available."""
    try:
        proc = subprocess.run(["which", "oc"], capture_output=True, text=True, timeout=5)
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    except Exception:
        pass
    return None


def read_node_journal(node: str, tail: int = 100) -> list[str]:
    """Read recent journal entries from a node.

    Tries oc adm node-logs --tail first (OCP, gives recent entries directly),
    then falls back to the raw journal proxy endpoint.
    """
    oc = _find_oc()
    if oc:
        rc, stdout, _ = run_cmd(oc, "adm", "node-logs", node, f"--tail={tail}", timeout=30)
        if rc == 0 and stdout:
            return stdout.split("\n")

    cmd = KUBECTL.split() + [
        "get", "--raw",
        f"/api/v1/nodes/{node}/proxy/logs/journal",
    ]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        output = b""
        start = time.time()
        while time.time() - start < 10:
            chunk = proc.stdout.read(8192)
            if not chunk:
                break
            output += chunk
            if len(output) >= 200_000:
                break
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        lines = output.decode(errors="replace").strip().split("\n")
        return lines[-tail:]
    except Exception:
        return []


def parse_syslog_timestamp(line: str) -> str | None:
    """Parse a syslog-format timestamp and return ISO 8601."""
    m = SYSLOG_TS_RE.match(line)
    if not m:
        return None
    ts_str = m.group(1)
    now = datetime.now(timezone.utc)
    try:
        parsed = datetime.strptime(ts_str, "%b %d %H:%M:%S")
        parsed = parsed.replace(year=now.year, tzinfo=timezone.utc)
        if parsed > now:
            parsed = parsed.replace(year=now.year - 1)
        return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Query host syslogs via K8s node proxy API")
    parser.add_argument("--max-age-seconds", type=int, default=600,
                        help="Maximum age in seconds for entries to be considered recent")
    args = parser.parse_args()

    result: dict = {
        "success": False,
        "platform": "observability",
        "test_name": "host_syslog",
        "tests": {},
    }

    if DEMO_MODE:
        demo_probes = {
            "log_source": "journald",
            "hosts_checked": 3,
            "entry_count": 100,
            "latest_timestamp": "2026-01-01T00:00:00Z",
        }
        for t in ("syslog_endpoint_reachable", "host_log_source_present", "entries_recent"):
            result["tests"][t] = {"passed": True, "message": f"demo: {t} ok", "probes": demo_probes}
        result["success"] = True
        print(json.dumps(result, indent=2))
        return 0

    try:
        rc, stdout, err = run_kubectl(
            "get", "nodes", "-o", "jsonpath={.items[*].metadata.name}",
        )
        if rc != 0 or not stdout:
            result["tests"]["syslog_endpoint_reachable"] = {
                "passed": False,
                "message": f"Failed to list nodes: {err}",
                "probes": {"hosts_checked": 0, "log_source": "journald",
                           "entry_count": 0, "latest_timestamp": ""},
            }
            print(json.dumps(result, indent=2))
            return 1

        nodes = stdout.split()
        hosts_checked = 0
        total_entries = 0
        latest_ts = ""
        reachable = False

        for node in nodes:
            lines = read_node_journal(node)
            if not lines or (len(lines) == 1 and not lines[0]):
                continue
            reachable = True
            hosts_checked += 1
            total_entries += len(lines)

            for line in reversed(lines):
                ts = parse_syslog_timestamp(line)
                if ts and ts > latest_ts:
                    latest_ts = ts
                    break

        probes = {
            "log_source": "journald",
            "hosts_checked": hosts_checked,
            "entry_count": total_entries,
            "latest_timestamp": latest_ts,
        }

        result["tests"]["syslog_endpoint_reachable"] = {
            "passed": reachable,
            "message": (
                f"Journal accessible on {hosts_checked}/{len(nodes)} nodes"
                if reachable
                else "Failed to read journal from any node"
            ),
            "probes": probes,
        }

        result["tests"]["host_log_source_present"] = {
            "passed": hosts_checked > 0,
            "message": (
                f"journald log source available on {hosts_checked} host(s)"
                if hosts_checked > 0
                else "No log source found"
            ),
            "probes": probes,
        }

        if latest_ts:
            latest_epoch = time.mktime(time.strptime(latest_ts, "%Y-%m-%dT%H:%M:%SZ"))
            age_s = time.time() - latest_epoch
            recent = age_s < args.max_age_seconds
        else:
            recent = False
            age_s = -1

        result["tests"]["entries_recent"] = {
            "passed": recent,
            "message": (
                f"{total_entries} entries, latest {int(age_s)}s old (within {args.max_age_seconds}s)"
                if recent
                else f"No recent entries (age: {int(age_s)}s, threshold: {args.max_age_seconds}s)"
            ),
            "probes": probes,
        }

        result["success"] = all(t["passed"] for t in result["tests"].values())

    except Exception as exc:
        result["error"] = str(exc)
        result["error_type"] = type(exc).__name__

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
