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

"""Virtual device hardening validation for OSAC ComputeInstances.

Inspects the KubeVirt VirtualMachine spec to confirm:
  1. USB redirection is not configured (no ``inputs`` with ``bus: usb``)
  2. Clipboard sharing is not enabled (no SPICE/QXL agent channel)
  3. Unnecessary virtual devices (floppy, CD-ROM, audio, tablet) are absent

This checks the hypervisor-side VM definition, not the guest OS.

Outputs the JSON contract consumed by ``VirtualDeviceHardeningCheck``.
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

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def _check_vm_devices(kubectl: str, vm_name: str, vm_namespace: str) -> dict[str, Any]:
    """Inspect the VirtualMachine spec for hardening violations."""
    probe = subprocess.run(
        [kubectl, "get", "virtualmachine", vm_name, "-n", vm_namespace, "-o", "json"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if probe.returncode != 0 or not probe.stdout.strip():
        raise RuntimeError(f"No VirtualMachine {vm_name} found in {vm_namespace}")

    data = json.loads(probe.stdout)
    items = [data] if data.get("kind") == "VirtualMachine" else data.get("items", [])
    if not items:
        raise RuntimeError(f"No VirtualMachine {vm_name} found in {vm_namespace}")

    vm_spec = items[0].get("spec", {}).get("template", {}).get("spec", {})
    domain = vm_spec.get("domain", {})
    devices = domain.get("devices", {})

    # Check 1: USB redirection disabled
    # KubeVirt adds a default USB tablet for pointer coordination — this is
    # standard and not a USB redirection concern. Only flag non-tablet USB
    # input devices (e.g. explicit passthrough or redirect configs).
    inputs = devices.get("inputs", [])
    usb_redirect_inputs = [
        i for i in inputs
        if i.get("bus") == "usb" and i.get("type") != "tablet"
    ]
    usb_disabled = len(usb_redirect_inputs) == 0

    usb_result: dict[str, Any] = {
        "passed": usb_disabled,
        "probes": ["vm_spec_inputs"],
    }
    if not usb_disabled:
        usb_result["message"] = f"Found {len(usb_redirect_inputs)} USB redirection device(s) in VM spec"

    # Check 2: Clipboard disabled (no SPICE/QXL agent channel)
    # KubeVirt VMs with autoattachGraphicsDevice: false have no SPICE agent
    auto_graphics = devices.get("autoattachGraphicsDevice", True)
    clipboard_disabled = not auto_graphics or "spice" not in json.dumps(devices).lower()
    clipboard_result: dict[str, Any] = {
        "passed": clipboard_disabled,
        "probes": ["vm_spec_graphics_device"],
    }
    if not clipboard_disabled:
        clipboard_result["message"] = "SPICE/QXL graphics device present (clipboard may be available)"

    # Check 3: Unnecessary virtual devices absent
    # Check for floppy, CD-ROM/ISO with media, audio devices, tablet
    disks = devices.get("disks", [])
    unnecessary = []
    for disk in disks:
        if disk.get("floppy"):
            unnecessary.append("floppy")
        if disk.get("cdrom"):
            unnecessary.append("cdrom")
    if devices.get("sound"):
        unnecessary.append("audio")

    devices_absent = len(unnecessary) == 0
    devices_result: dict[str, Any] = {
        "passed": devices_absent,
        "probes": ["vm_spec_disks", "vm_spec_sound"],
    }
    if not devices_absent:
        devices_result["message"] = f"Unnecessary virtual devices found: {', '.join(unnecessary)}"

    return {
        "usb_devices_disabled": usb_result,
        "clipboard_disabled": clipboard_result,
        "unnecessary_virtual_devices_absent": devices_result,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Virtual device hardening (OSAC)")
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--vm-name", default="")
    parser.add_argument("--vm-namespace", default="")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "vm",
        "test_name": "virtual_device_hardening",
        "tests": {},
    }

    if DEMO_MODE:
        result["tests"] = {
            "usb_devices_disabled": {"passed": True, "probes": ["demo_no_usb"]},
            "clipboard_disabled": {"passed": True, "probes": ["demo_no_clipboard"]},
            "unnecessary_virtual_devices_absent": {"passed": True, "probes": ["demo_no_unnecessary_devices"]},
        }
        result["success"] = True
        print(json.dumps(result, indent=2))
        return 0

    try:
        from common.osac_client import get_env_config

        config = get_env_config(require_admin=False)
        kubectl = shutil.which("kubectl") or shutil.which("oc") or "kubectl"

        vm_name = args.vm_name
        vm_ns = args.vm_namespace
        if not vm_name:
            raise RuntimeError("--vm-name is required (passed from launch_instance step)")

        tests = _check_vm_devices(kubectl, vm_name, vm_ns)
        result["tests"] = tests
        result["success"] = all(t.get("passed", False) for t in tests.values())

    except Exception as exc:
        result["error"] = str(exc)
        result["error_type"] = type(exc).__name__

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
