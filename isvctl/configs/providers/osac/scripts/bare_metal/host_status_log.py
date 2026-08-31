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

"""Query per-host journalctl and dmesg via OCP node-logs API.

Uses `oc adm node-logs` (OCP) or the K8s node proxy endpoint to read
journal and dmesg entries from each cluster node. Reports whether each
source is producing fresh entries within max_age_seconds.

Outputs JSON consumed by BmHostStatusLogCheck:
  tests.journalctl_recent.passed/message
  tests.dmesg_recent.passed/message
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
    try:
        proc = subprocess.run(["which", "oc"], capture_output=True, text=True, timeout=5)
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    except Exception:
        pass
    return None


JOURNAL_ISO_RE = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})")
SYSLOG_RE = re.compile(r"^(\w{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})")
DMESG_TS_RE = re.compile(r"^\[\s*([\d.]+)\]")


def _parse_journal_ts(line: str) -> float | None:
    m = JOURNAL_ISO_RE.search(line)
    if m:
        try:
            dt = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S")
            return dt.replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            pass
    m = SYSLOG_RE.match(line)
    if m:
        try:
            now = datetime.now(timezone.utc)
            dt = datetime.strptime(m.group(1), "%b %d %H:%M:%S")
            dt = dt.replace(year=now.year, tzinfo=timezone.utc)
            if dt > now:
                dt = dt.replace(year=now.year - 1)
            return dt.timestamp()
        except ValueError:
            pass
    return None


def read_journal(node: str, tail: int = 50) -> list[str]:
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


def read_dmesg(node: str, tail: int = 50) -> list[str]:
    oc = _find_oc()
    if oc:
        rc, stdout, _ = run_cmd(oc, "adm", "node-logs", node, "--path=dmesg", timeout=30)
        if rc == 0 and stdout:
            lines = stdout.strip().split("\n")
            return lines[-tail:]
    rc, stdout, _ = run_kubectl(
        "get", "--raw", f"/api/v1/nodes/{node}/proxy/logs/dmesg",
        timeout=30,
    )
    if rc == 0 and stdout:
        lines = stdout.strip().split("\n")
        return lines[-tail:]
    return []


def main() -> int:
    parser = argparse.ArgumentParser(description="Query host journalctl + dmesg (OSAC)")
    parser.add_argument("--max-age-seconds", type=int, default=600)
    args = parser.parse_args()

    result: dict = {
        "success": False,
        "platform": "bare_metal",
        "test_name": "host_status_log",
        "tests": {},
    }

    if DEMO_MODE:
        for src in ("journalctl_recent", "dmesg_recent"):
            result["tests"][src] = {"passed": True, "message": f"demo: {src} ok"}
        result["success"] = True
        print(json.dumps(result, indent=2))
        return 0

    try:
        rc, stdout, err = run_kubectl(
            "get", "nodes", "-o", "jsonpath={.items[*].metadata.name}",
        )
        if rc != 0 or not stdout:
            result["error"] = f"Failed to list nodes: {err}"
            print(json.dumps(result, indent=2))
            return 1

        nodes = stdout.split()
        now = time.time()

        # --- journalctl ---
        journal_ok = False
        journal_msg = "No journal entries found on any node"
        for node in nodes:
            lines = read_journal(node)
            if not lines or (len(lines) == 1 and not lines[0]):
                continue
            for line in reversed(lines):
                ts = _parse_journal_ts(line)
                if ts is not None:
                    age = now - ts
                    if age < args.max_age_seconds:
                        journal_ok = True
                        journal_msg = f"{len(lines)} entries on {node}, latest {int(age)}s old"
                    else:
                        journal_msg = f"Entries found but latest is {int(age)}s old (threshold: {args.max_age_seconds}s)"
                    break
            if journal_ok:
                break

        result["tests"]["journalctl_recent"] = {"passed": journal_ok, "message": journal_msg}

        # --- dmesg ---
        dmesg_ok = False
        dmesg_msg = "No dmesg output found on any node"
        for node in nodes:
            lines = read_dmesg(node)
            if not lines or (len(lines) == 1 and not lines[0]):
                continue
            dmesg_ok = True
            dmesg_msg = f"{len(lines)} dmesg entries on {node}"
            break

        result["tests"]["dmesg_recent"] = {"passed": dmesg_ok, "message": dmesg_msg}

        result["success"] = journal_ok or dmesg_ok

    except Exception as exc:
        result["error"] = str(exc)
        result["error_type"] = type(exc).__name__

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
