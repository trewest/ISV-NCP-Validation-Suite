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

"""Snapshot a PVC volume and restore it, verifying sentinel data integrity.

Uses the K8s VolumeSnapshot CRD (snapshot.storage.k8s.io/v1) to create a
snapshot of an existing PVC, restore it to a new PVC, and verify the
sentinel data written by create_volume.py is intact. Covers DATASVC02-01.

Outputs JSON consumed by VolumeSnapshotRestoredCheck (composed from
StepSuccessCheck, FieldExistsCheck, CrudOperationsCheck).
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


def find_snapshot_class(storage_class: str) -> str | None:
    """Find a VolumeSnapshotClass whose driver matches the StorageClass provisioner."""
    rc, out, _ = run_kubectl(
        "get", "storageclass", storage_class,
        "-o", "jsonpath={.provisioner}",
    )
    if rc != 0 or not out:
        return None
    provisioner = out

    rc, out, _ = run_kubectl("get", "volumesnapshotclass", "-o", "json")
    if rc != 0:
        return None

    try:
        items = json.loads(out).get("items", [])
    except json.JSONDecodeError:
        return None

    for vsc in items:
        if vsc.get("driver") == provisioner:
            return vsc["metadata"]["name"]
    return None


def poll_snapshot_ready(namespace: str, snap_name: str, timeout_s: int = 180) -> tuple[bool, str]:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        rc, out, _ = run_kubectl(
            "get", "volumesnapshot", snap_name, "-n", namespace,
            "-o", "jsonpath={.status.readyToUse}",
        )
        if rc == 0 and out.lower() == "true":
            return True, "Snapshot ready"
        time.sleep(5)
    return False, f"Snapshot not ready after {timeout_s}s"


def main() -> int:
    parser = argparse.ArgumentParser(description="Snapshot + restore lifecycle (OSAC)")
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--pvc-name", required=True)
    parser.add_argument("--expected-content", required=True)
    parser.add_argument("--storage-class", required=True)
    parser.add_argument("--snapshot-timeout", type=int, default=180)
    args = parser.parse_args()

    suffix = uuid.uuid4().hex[:8]
    snap_name = f"snap-{suffix}"
    restore_pvc = f"restore-{suffix}"
    restore_pod = f"restore-pod-{suffix}"

    result: dict = {
        "success": False,
        "platform": "storage",
        "test_name": "snapshot_lifecycle",
        "volume_id": args.pvc_name,
        "snapshot_id": snap_name,
        "operations": {},
    }

    if DEMO_MODE:
        for op in ("create_snapshot", "restore_volume", "verify_data"):
            result["operations"][op] = {"passed": True, "message": f"demo: {op} ok"}
        result["success"] = True
        print(json.dumps(result, indent=2))
        return 0

    try:
        # Find matching VolumeSnapshotClass
        vsc_name = find_snapshot_class(args.storage_class)
        if not vsc_name:
            result["operations"]["create_snapshot"] = {
                "passed": False,
                "error": (
                    f"No VolumeSnapshotClass found for StorageClass '{args.storage_class}'. "
                    "Ensure the VolumeSnapshot CRD and a matching VolumeSnapshotClass are installed."
                ),
            }
            print(json.dumps(result, indent=2))
            return 1

        # CREATE_SNAPSHOT
        snap_manifest = f"""\
apiVersion: snapshot.storage.k8s.io/v1
kind: VolumeSnapshot
metadata:
  name: {snap_name}
  namespace: {args.namespace}
spec:
  volumeSnapshotClassName: {vsc_name}
  source:
    persistentVolumeClaimName: {args.pvc_name}
"""
        rc, _, err = run_kubectl("apply", "-f", "-", stdin=snap_manifest)
        if rc != 0:
            result["operations"]["create_snapshot"] = {"passed": False, "error": f"Apply failed: {err}"}
            print(json.dumps(result, indent=2))
            return 1

        ok, msg = poll_snapshot_ready(args.namespace, snap_name, args.snapshot_timeout)
        if not ok:
            result["operations"]["create_snapshot"] = {"passed": False, "error": msg}
            print(json.dumps(result, indent=2))
            return 1
        result["operations"]["create_snapshot"] = {"passed": True, "message": f"VolumeSnapshot {snap_name} ready"}

        # RESTORE_VOLUME: create PVC from snapshot
        # Get the original PVC size
        rc, pvc_size, _ = run_kubectl(
            "get", "pvc", args.pvc_name, "-n", args.namespace,
            "-o", "jsonpath={.spec.resources.requests.storage}",
        )
        if rc != 0 or not pvc_size:
            pvc_size = "10Gi"

        restore_manifest = f"""\
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: {restore_pvc}
  namespace: {args.namespace}
spec:
  accessModes: ["ReadWriteOnce"]
  storageClassName: {args.storage_class}
  resources:
    requests:
      storage: {pvc_size}
  dataSource:
    name: {snap_name}
    kind: VolumeSnapshot
    apiGroup: snapshot.storage.k8s.io
"""
        rc, _, err = run_kubectl("apply", "-f", "-", stdin=restore_manifest)
        if rc != 0:
            result["operations"]["restore_volume"] = {"passed": False, "error": f"Restore PVC failed: {err}"}
            print(json.dumps(result, indent=2))
            return 1

        # Deploy pod to mount restored PVC
        pod_manifest = f"""\
apiVersion: v1
kind: Pod
metadata:
  name: {restore_pod}
  namespace: {args.namespace}
spec:
  containers:
    - name: verify
      image: busybox:1.36
      command: ["sleep", "3600"]
      volumeMounts:
        - name: restored-vol
          mountPath: {MOUNT_POINT}
  volumes:
    - name: restored-vol
      persistentVolumeClaim:
        claimName: {restore_pvc}
  terminationGracePeriodSeconds: 0
"""
        rc, _, err = run_kubectl("apply", "-f", "-", stdin=pod_manifest)
        if rc != 0:
            result["operations"]["restore_volume"] = {"passed": False, "error": f"Restore pod failed: {err}"}
            print(json.dumps(result, indent=2))
            return 1

        rc, _, err = run_kubectl(
            "wait", "--for=condition=Ready", f"pod/{restore_pod}",
            "-n", args.namespace, "--timeout=180s",
            timeout=200,
        )
        if rc != 0:
            result["operations"]["restore_volume"] = {"passed": False, "error": f"Restore pod not ready: {err}"}
            print(json.dumps(result, indent=2))
            return 1
        result["operations"]["restore_volume"] = {
            "passed": True,
            "message": f"PVC {restore_pvc} restored from snapshot",
        }

        # VERIFY_DATA: read sentinel from restored volume
        rc, out, err = run_kubectl(
            "exec", restore_pod, "-n", args.namespace, "--",
            "cat", f"{MOUNT_POINT}/{SENTINEL_FILE}",
        )
        if rc != 0:
            result["operations"]["verify_data"] = {"passed": False, "error": f"Read failed: {err}"}
            print(json.dumps(result, indent=2))
            return 1

        actual = out.strip()
        if actual != args.expected_content:
            result["operations"]["verify_data"] = {
                "passed": False,
                "error": f"Data mismatch: expected '{args.expected_content}', got '{actual}'",
            }
            print(json.dumps(result, indent=2))
            return 1

        result["operations"]["verify_data"] = {"passed": True, "message": "Sentinel data intact after restore"}
        result["success"] = True

    except Exception as exc:
        result["error"] = str(exc)
        result["error_type"] = type(exc).__name__

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
