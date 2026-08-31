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

"""Launch a lightweight CNV VirtualMachine as the storage test host.

Creates an ephemeral namespace, deploys a Fedora-based VirtualMachine via
KubeVirt, and waits for it to reach the Running phase. The VM serves as the
volume host for subsequent storage tests.

Outputs JSON consumed by VolumeHostRunningCheck (composed from InstanceStateCheck).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid


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


def poll_vmi_running(namespace: str, vm_name: str, timeout_s: int = 300) -> tuple[bool, str]:
    """Poll VirtualMachineInstance until phase is Running."""
    deadline = time.time() + timeout_s
    last_phase = ""
    while time.time() < deadline:
        rc, out, _ = run_kubectl(
            "get", "vmi", vm_name, "-n", namespace,
            "-o", "jsonpath={.status.phase}",
        )
        if rc == 0 and out:
            last_phase = out
            if out == "Running":
                return True, "VMI is Running"
        time.sleep(5)
    return False, f"VMI not Running after {timeout_s}s (last phase: {last_phase})"


def main() -> int:
    parser = argparse.ArgumentParser(description="Launch CNV VM as volume host (OSAC)")
    parser.add_argument("--namespace-prefix", default="isvtest-vol")
    parser.add_argument("--storage-class", required=True)
    parser.add_argument("--vm-timeout", type=int, default=300)
    args = parser.parse_args()

    suffix = uuid.uuid4().hex[:8]
    namespace = f"{args.namespace_prefix}-{suffix}"
    vm_name = f"vol-host-{suffix}"

    result: dict = {
        "success": False,
        "platform": "storage",
        "test_name": "launch_instance",
        "instance_id": vm_name,
        "state": "",
        "namespace": namespace,
    }

    if DEMO_MODE:
        result["success"] = True
        result["state"] = "running"
        print(json.dumps(result, indent=2))
        return 0

    try:
        # Create namespace
        rc, _, err = run_kubectl("create", "namespace", namespace)
        if rc != 0:
            result["error"] = f"Failed to create namespace: {err}"
            print(json.dumps(result, indent=2))
            return 1

        # Deploy a lightweight VirtualMachine via KubeVirt
        # Uses containerdisk (no PVC needed for boot) with a small Fedora cloud image
        vm_manifest = f"""\
apiVersion: kubevirt.io/v1
kind: VirtualMachine
metadata:
  name: {vm_name}
  namespace: {namespace}
spec:
  running: true
  template:
    metadata:
      labels:
        app: vol-host
    spec:
      domain:
        resources:
          requests:
            memory: 512Mi
            cpu: "1"
        devices:
          disks:
            - name: rootdisk
              disk:
                bus: virtio
      volumes:
        - name: rootdisk
          containerDisk:
            image: quay.io/containerdisks/fedora:latest
      terminationGracePeriodSeconds: 0
"""
        rc, _, err = run_kubectl("apply", "-f", "-", stdin=vm_manifest)
        if rc != 0:
            result["error"] = f"Failed to create VM: {err}"
            print(json.dumps(result, indent=2))
            return 1

        # Wait for VMI to reach Running phase
        ok, msg = poll_vmi_running(namespace, vm_name, args.vm_timeout)
        if not ok:
            result["error"] = msg
            print(json.dumps(result, indent=2))
            return 1

        result["state"] = "running"
        result["success"] = True

    except Exception as exc:
        result["error"] = str(exc)
        result["error_type"] = type(exc).__name__

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
