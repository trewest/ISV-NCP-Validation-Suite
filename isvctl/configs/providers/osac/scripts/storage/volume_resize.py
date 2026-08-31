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

"""Resize a PVC-backed volume online and verify the filesystem reports the new capacity.

Uses kubectl to patch the PVC spec, poll PVC/PV capacity, and verify via df
inside the consumer pod. Covers DATASVC03-01.

Outputs JSON consumed by VolumeResizedCheck (composed from StepSuccessCheck,
FieldExistsCheck, CrudOperationsCheck).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time


DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"
KUBECTL = os.environ.get("KUBECTL", "kubectl")


def run_kubectl(*args: str, stdin: str | None = None, timeout: int = 60) -> tuple[int, str, str]:
    cmd = KUBECTL.split() + list(args)
    try:
        proc = subprocess.run(
            cmd,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except subprocess.TimeoutExpired:
        return 1, "", f"Command timed out after {timeout}s"
    except Exception as exc:
        return 1, "", str(exc)


def parse_gi(size_str: str) -> float:
    m = re.match(r"^(\d+(?:\.\d+)?)\s*Gi$", size_str)
    if m:
        return float(m.group(1))
    m = re.match(r"^(\d+(?:\.\d+)?)\s*G$", size_str)
    if m:
        return float(m.group(1))
    m = re.match(r"^(\d+)\s*$", size_str)
    if m:
        return float(m.group(1)) / (1024**3)
    raise ValueError(f"Cannot parse size: {size_str}")


def poll_pvc_capacity(namespace: str, pvc_name: str, expected_size: str, timeout_s: int = 180) -> tuple[bool, str]:
    expected_gi = parse_gi(expected_size)
    deadline = time.time() + timeout_s
    last_cap = ""
    while time.time() < deadline:
        rc, out, _ = run_kubectl(
            "get", "pvc", pvc_name, "-n", namespace,
            "-o", "jsonpath={.status.capacity.storage}",
        )
        if rc == 0 and out:
            last_cap = out
            try:
                actual_gi = parse_gi(out)
                if actual_gi >= expected_gi:
                    return True, f"PVC capacity updated to {out}"
            except ValueError:
                pass
        time.sleep(5)
    return False, f"PVC capacity not updated after {timeout_s}s (last: {last_cap}, expected: {expected_size})"


def parse_df_size_kb(df_output: str, mount_point: str = "/data") -> int | None:
    """Parse 1K-blocks from df output, handling wrapped long device names."""
    lines = df_output.strip().splitlines()
    # Join continuation lines: if a line starts with whitespace, append to previous
    merged: list[str] = []
    for line in lines[1:]:
        if line and line[0].isspace() and merged:
            merged[-1] += " " + line.strip()
        else:
            merged.append(line)
    for line in merged:
        parts = line.split()
        # Standard df: device  1K-blocks  Used  Available  Use%  Mounted-on
        if len(parts) >= 6 and parts[-1] == mount_point:
            try:
                return int(parts[1])
            except ValueError:
                continue
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Resize PVC volume (OSAC)")
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--pvc-name", required=True)
    parser.add_argument("--pod-name", required=True)
    parser.add_argument("--mount-point", default="/data")
    parser.add_argument("--new-size", default="20Gi")
    parser.add_argument("--expand-timeout", type=int, default=180)
    args = parser.parse_args()

    result: dict = {
        "success": False,
        "platform": "storage",
        "test_name": "volume_resize",
        "volume_id": args.pvc_name,
        "operations": {},
    }

    if DEMO_MODE:
        for op in ("modify_volume", "grow_partition", "resize_filesystem", "verify_size"):
            result["operations"][op] = {"passed": True, "message": f"demo: {op} ok"}
        result["success"] = True
        print(json.dumps(result, indent=2))
        return 0

    try:
        # MODIFY_VOLUME: patch PVC to new size
        patch = json.dumps({"spec": {"resources": {"requests": {"storage": args.new_size}}}})
        rc, _, err = run_kubectl(
            "patch", "pvc", args.pvc_name,
            "-n", args.namespace,
            "--type=merge", f"-p={patch}",
        )
        if rc != 0:
            result["operations"]["modify_volume"] = {"passed": False, "error": f"Patch failed: {err}"}
            print(json.dumps(result, indent=2))
            return 1
        result["operations"]["modify_volume"] = {"passed": True, "message": f"PVC patched to {args.new_size}"}

        # GROW_PARTITION + RESIZE_FILESYSTEM: CSI handles both automatically
        # Poll PVC capacity to confirm the resize propagated
        ok, msg = poll_pvc_capacity(args.namespace, args.pvc_name, args.new_size, args.expand_timeout)
        if not ok:
            result["operations"]["grow_partition"] = {"passed": False, "error": msg}
            print(json.dumps(result, indent=2))
            return 1
        result["operations"]["grow_partition"] = {"passed": True, "message": "Partition grown (CSI online expansion)"}
        result["operations"]["resize_filesystem"] = {"passed": True, "message": "Filesystem resized by CSI driver"}

        # VERIFY_SIZE: check df inside pod
        expected_kb = int(parse_gi(args.new_size) * 1024 * 1024)
        threshold_kb = int(expected_kb * 0.90)

        deadline = time.time() + 60
        verified = False
        last_df = ""
        while time.time() < deadline:
            rc, out, _ = run_kubectl(
                "exec", args.pod_name, "-n", args.namespace, "--",
                "df", "-k", args.mount_point,
            )
            if rc == 0:
                last_df = out
                actual_kb = parse_df_size_kb(out, args.mount_point)
                if actual_kb and actual_kb >= threshold_kb:
                    verified = True
                    break
            time.sleep(5)

        if not verified:
            result["operations"]["verify_size"] = {
                "passed": False,
                "error": f"df reports insufficient capacity (expected >= {threshold_kb}KB): {last_df}",
            }
            print(json.dumps(result, indent=2))
            return 1
        result["operations"]["verify_size"] = {"passed": True, "message": f"df confirms {args.new_size} capacity"}

        result["success"] = True

    except Exception as exc:
        result["error"] = str(exc)
        result["error_type"] = type(exc).__name__

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
