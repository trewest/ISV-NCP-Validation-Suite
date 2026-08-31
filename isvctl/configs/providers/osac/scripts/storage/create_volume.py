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

"""Create a PVC-backed volume, mount it in a pod, and seed it with sentinel data.

Uses K8s PVC + Pod APIs (kubectl) to exercise the CSI volume lifecycle:
create PVC → deploy consumer pod (attach + format + mount) → write sentinel.

Outputs JSON consumed by VolumeProvisionedCheck (composed from StepSuccessCheck,
FieldExistsCheck, CrudOperationsCheck).
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
MOUNT_POINT = "/data"
SENTINEL_FILE = "sentinel.txt"


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


def poll_pvc_bound(namespace: str, pvc_name: str, timeout_s: int = 120) -> tuple[bool, str]:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        rc, out, _ = run_kubectl(
            "get", "pvc", pvc_name, "-n", namespace,
            "-o", "jsonpath={.status.phase}",
        )
        if rc == 0 and out == "Bound":
            return True, "PVC bound"
        time.sleep(3)
    return False, f"PVC not bound after {timeout_s}s (last phase: {out})"


def main() -> int:
    parser = argparse.ArgumentParser(description="Create PVC volume with sentinel data (OSAC)")
    parser.add_argument("--storage-class", required=True)
    parser.add_argument("--size", default="10Gi")
    parser.add_argument("--namespace", required=True, help="Pre-existing namespace (created by launch_instance)")
    args = parser.parse_args()

    suffix = uuid.uuid4().hex[:8]
    namespace = args.namespace
    pvc_name = f"vol-{suffix}"
    pod_name = f"vol-pod-{suffix}"
    sentinel_content = f"isv-sentinel-{suffix}"

    result: dict = {
        "success": False,
        "platform": "storage",
        "test_name": "create_volume",
        "volume_id": pvc_name,
        "mount_point": MOUNT_POINT,
        "sentinel_content": sentinel_content,
        "namespace": namespace,
        "pod_name": pod_name,
        "storage_class": args.storage_class,
        "operations": {},
    }

    if DEMO_MODE:
        for op in ("create", "attach", "format", "mount", "write_sentinel"):
            result["operations"][op] = {"passed": True, "message": f"demo: {op} ok"}
        result["success"] = True
        print(json.dumps(result, indent=2))
        return 0

    try:
        # CREATE: apply PVC
        pvc_manifest = f"""\
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: {pvc_name}
  namespace: {namespace}
spec:
  accessModes: ["ReadWriteOnce"]
  storageClassName: {args.storage_class}
  resources:
    requests:
      storage: {args.size}
"""
        rc, _, err = run_kubectl("apply", "-f", "-", stdin=pvc_manifest)
        if rc != 0:
            result["operations"]["create"] = {"passed": False, "error": f"PVC apply failed: {err}"}
            print(json.dumps(result, indent=2))
            return 1
        result["operations"]["create"] = {"passed": True, "message": f"PVC {pvc_name} created"}

        # ATTACH + FORMAT + MOUNT: deploy consumer pod
        pod_manifest = f"""\
apiVersion: v1
kind: Pod
metadata:
  name: {pod_name}
  namespace: {namespace}
spec:
  containers:
    - name: volume-test
      image: busybox:1.36
      command: ["sleep", "3600"]
      volumeMounts:
        - name: test-vol
          mountPath: {MOUNT_POINT}
  volumes:
    - name: test-vol
      persistentVolumeClaim:
        claimName: {pvc_name}
  terminationGracePeriodSeconds: 0
"""
        rc, _, err = run_kubectl("apply", "-f", "-", stdin=pod_manifest)
        if rc != 0:
            result["operations"]["attach"] = {"passed": False, "error": f"Pod apply failed: {err}"}
            print(json.dumps(result, indent=2))
            return 1

        # Wait for pod ready (implies PVC bound + volume attached + formatted + mounted)
        rc, _, err = run_kubectl(
            "wait", "--for=condition=Ready", f"pod/{pod_name}",
            "-n", namespace, "--timeout=180s",
            timeout=200,
        )
        if rc != 0:
            result["operations"]["attach"] = {"passed": False, "error": f"Pod not ready: {err}"}
            print(json.dumps(result, indent=2))
            return 1

        bound_ok, bound_msg = poll_pvc_bound(namespace, pvc_name, timeout_s=10)
        if not bound_ok:
            result["operations"]["attach"] = {"passed": False, "error": bound_msg}
            print(json.dumps(result, indent=2))
            return 1

        result["operations"]["attach"] = {"passed": True, "message": "Volume attached to pod"}
        result["operations"]["format"] = {"passed": True, "message": "Filesystem formatted by CSI driver"}
        result["operations"]["mount"] = {"passed": True, "message": f"Volume mounted at {MOUNT_POINT}"}

        # WRITE SENTINEL
        rc, _, err = run_kubectl(
            "exec", pod_name, "-n", namespace, "--",
            "sh", "-c", f"echo '{sentinel_content}' > {MOUNT_POINT}/{SENTINEL_FILE}",
        )
        if rc != 0:
            result["operations"]["write_sentinel"] = {"passed": False, "error": f"Write failed: {err}"}
            print(json.dumps(result, indent=2))
            return 1

        # Verify sentinel
        rc, out, err = run_kubectl(
            "exec", pod_name, "-n", namespace, "--",
            "cat", f"{MOUNT_POINT}/{SENTINEL_FILE}",
        )
        if rc != 0 or out.strip() != sentinel_content:
            result["operations"]["write_sentinel"] = {
                "passed": False,
                "error": f"Verify failed: expected '{sentinel_content}', got '{out}'",
            }
            print(json.dumps(result, indent=2))
            return 1

        result["operations"]["write_sentinel"] = {"passed": True, "message": "Sentinel data written and verified"}
        result["success"] = True

    except Exception as exc:
        result["error"] = str(exc)
        result["error_type"] = type(exc).__name__

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
