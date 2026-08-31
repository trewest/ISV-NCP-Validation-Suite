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

"""CRUD OS install configuration via Kubernetes ConfigMaps (BOOT01-02).

Models OS install configurations (cloud-init user-data, boot method, etc.)
as labeled ConfigMaps in an ephemeral namespace, exercising full CRUD
lifecycle through the K8s API.

Output JSON consumed by OsInstallConfigCrudCheck (composed:
StepSuccessCheck + FieldExistsCheck + CrudOperationsCheck).
"""

from __future__ import annotations

import argparse
import json
import os
import random
import string
import subprocess
import sys
from typing import Any


DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"
KUBECTL = os.environ.get("KUBECTL", "kubectl")


def _suffix() -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=6))


def _run(args: list[str], timeout: int = 60) -> tuple[int, str, str]:
    cmd = KUBECTL.split() + args
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except subprocess.TimeoutExpired:
        return 1, "", f"timeout after {timeout}s"
    except Exception as exc:
        return 1, "", str(exc)


def _apply(manifest: str) -> tuple[int, str]:
    cmd = KUBECTL.split() + ["apply", "-f", "-"]
    try:
        proc = subprocess.run(cmd, input=manifest, capture_output=True, text=True, timeout=30)
        return proc.returncode, proc.stderr.strip()
    except Exception as exc:
        return 1, str(exc)


CLOUD_INIT_DATA = """\
#cloud-config
hostname: isv-test-host
users:
  - name: isv-test-user
    ssh_authorized_keys: []
packages: []
"""

CLOUD_INIT_UPDATED = """\
#cloud-config
hostname: isv-updated-host
users:
  - name: isv-test-user
    ssh_authorized_keys: []
packages:
  - curl
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="CRUD OS install configuration")
    parser.add_argument("--region", default="osac-default", help="Region (unused)")
    parser.parse_args()

    sfx = _suffix()
    ns = f"isv-install-cfg-{sfx}"
    cm_name = f"isv-install-config-{sfx}"

    result: dict[str, Any] = {
        "success": False,
        "platform": "image_registry",
        "test_name": "crud_install_config",
        "config_id": "",
        "config_name": "",
        "operations": {
            "create": {"passed": False},
            "read": {"passed": False},
            "update": {"passed": False},
            "delete": {"passed": False},
        },
    }

    if DEMO_MODE:
        result["config_id"] = "demo-install-config-0001"
        result["config_name"] = "demo-install-config"
        for op in result["operations"].values():
            op["passed"] = True
        result["success"] = True
        print(json.dumps(result, indent=2))
        return 0

    try:
        _run(["create", "namespace", ns])
        sys.stderr.write(f"Created namespace {ns}\n")

        # --- CREATE ---
        configmap = json.dumps({
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": cm_name,
                "namespace": ns,
                "labels": {
                    "app.kubernetes.io/managed-by": "isvctl",
                    "osac.openshift.io/type": "os-install-config",
                },
            },
            "data": {
                "user-data": CLOUD_INIT_DATA,
                "config-format": "cloud-init",
                "boot-method": "pxe",
            },
        })
        rc, err = _apply(configmap)
        if rc != 0:
            result["operations"]["create"]["error"] = f"apply failed: {err}"
            print(json.dumps(result, indent=2))
            return 1
        result["config_id"] = cm_name
        result["config_name"] = cm_name
        result["operations"]["create"]["passed"] = True
        sys.stderr.write(f"CREATE passed: {cm_name}\n")

        # --- READ ---
        rc, out, err = _run(["get", "configmap", cm_name, "-n", ns, "-o", "json"])
        if rc != 0:
            result["operations"]["read"]["error"] = f"get failed: {err}"
            print(json.dumps(result, indent=2))
            return 1
        try:
            cm_data = json.loads(out)
            read_user_data = cm_data.get("data", {}).get("user-data", "")
            if "isv-test-host" not in read_user_data:
                result["operations"]["read"]["error"] = "user-data content mismatch"
                print(json.dumps(result, indent=2))
                return 1
            if cm_data.get("data", {}).get("config-format") != "cloud-init":
                result["operations"]["read"]["error"] = "config-format mismatch"
                print(json.dumps(result, indent=2))
                return 1
        except json.JSONDecodeError:
            result["operations"]["read"]["error"] = "invalid JSON from kubectl get"
            print(json.dumps(result, indent=2))
            return 1
        result["operations"]["read"]["passed"] = True
        sys.stderr.write("READ passed\n")

        # --- UPDATE ---
        patch = json.dumps({
            "data": {
                "user-data": CLOUD_INIT_UPDATED,
                "config-format": "cloud-init",
                "boot-method": "pxe",
            },
        })
        rc, _, err = _run(["patch", "configmap", cm_name, "-n", ns,
                           "--type=merge", "-p", patch])
        if rc != 0:
            result["operations"]["update"]["error"] = f"patch failed: {err}"
            print(json.dumps(result, indent=2))
            return 1
        rc, out, err = _run(["get", "configmap", cm_name, "-n", ns,
                             "-o", "jsonpath={.data.user-data}"])
        if rc != 0 or "isv-updated-host" not in out:
            result["operations"]["update"]["error"] = "update verification failed"
            print(json.dumps(result, indent=2))
            return 1
        result["operations"]["update"]["passed"] = True
        sys.stderr.write("UPDATE passed\n")

        # --- DELETE ---
        rc, _, err = _run(["delete", "configmap", cm_name, "-n", ns])
        if rc != 0:
            result["operations"]["delete"]["error"] = f"delete failed: {err}"
            print(json.dumps(result, indent=2))
            return 1
        rc, _, _ = _run(["get", "configmap", cm_name, "-n", ns])
        if rc == 0:
            result["operations"]["delete"]["error"] = "configmap still exists after delete"
            print(json.dumps(result, indent=2))
            return 1
        result["operations"]["delete"]["passed"] = True
        sys.stderr.write("DELETE passed\n")

        result["success"] = all(
            op["passed"] for op in result["operations"].values()
        )

    finally:
        sys.stderr.write(f"Cleaning up namespace {ns}\n")
        _run(["delete", "namespace", ns, "--wait=false"], timeout=15)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
