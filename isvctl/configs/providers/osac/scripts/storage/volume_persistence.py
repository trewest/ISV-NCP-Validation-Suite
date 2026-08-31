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

"""Verify a PVC volume persists with data intact across pod restart.

Deletes the consumer pod, deploys a new pod mounting the same PVC,
and verifies the sentinel data written by create_volume is still present.
Covers DATASVC04-01.

Outputs JSON consumed by VolumePersistsAcrossRestartCheck (composed from
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify volume persistence across restart (OSAC)")
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--pvc-name", required=True)
    parser.add_argument("--pod-name", required=True, help="Existing pod to restart")
    parser.add_argument("--expected-content", required=True, help="Sentinel content to verify")
    args = parser.parse_args()

    suffix = uuid.uuid4().hex[:6]
    new_pod_name = f"vol-restart-{suffix}"

    result: dict = {
        "success": False,
        "platform": "storage",
        "test_name": "volume_persistence",
        "volume_id": args.pvc_name,
        "pod_name": new_pod_name,
        "operations": {},
    }

    if DEMO_MODE:
        for op in ("stop", "start", "verify_attached", "verify_data"):
            result["operations"][op] = {"passed": True, "message": f"demo: {op} ok"}
        result["success"] = True
        print(json.dumps(result, indent=2))
        return 0

    try:
        # STOP: delete the existing consumer pod
        rc, _, err = run_kubectl(
            "delete", "pod", args.pod_name, "-n", args.namespace,
            "--wait=true", "--timeout=60s",
            timeout=70,
        )
        if rc != 0:
            result["operations"]["stop"] = {"passed": False, "error": f"Pod delete failed: {err}"}
            print(json.dumps(result, indent=2))
            return 1
        result["operations"]["stop"] = {"passed": True, "message": f"Pod {args.pod_name} deleted"}

        # START: deploy a new pod mounting the same PVC
        pod_manifest = f"""\
apiVersion: v1
kind: Pod
metadata:
  name: {new_pod_name}
  namespace: {args.namespace}
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
        claimName: {args.pvc_name}
  terminationGracePeriodSeconds: 0
"""
        rc, _, err = run_kubectl("apply", "-f", "-", stdin=pod_manifest)
        if rc != 0:
            result["operations"]["start"] = {"passed": False, "error": f"Pod create failed: {err}"}
            print(json.dumps(result, indent=2))
            return 1

        rc, _, err = run_kubectl(
            "wait", "--for=condition=Ready", f"pod/{new_pod_name}",
            "-n", args.namespace, "--timeout=120s",
            timeout=140,
        )
        if rc != 0:
            result["operations"]["start"] = {"passed": False, "error": f"Pod not ready: {err}"}
            print(json.dumps(result, indent=2))
            return 1
        result["operations"]["start"] = {"passed": True, "message": f"New pod {new_pod_name} running"}

        # VERIFY_ATTACHED: check PVC is still Bound
        rc, phase, _ = run_kubectl(
            "get", "pvc", args.pvc_name, "-n", args.namespace,
            "-o", "jsonpath={.status.phase}",
        )
        if rc != 0 or phase != "Bound":
            result["operations"]["verify_attached"] = {
                "passed": False,
                "error": f"PVC phase is '{phase}', expected 'Bound'",
            }
            print(json.dumps(result, indent=2))
            return 1
        result["operations"]["verify_attached"] = {"passed": True, "message": "PVC still Bound after restart"}

        # VERIFY_DATA: read sentinel and compare
        rc, out, err = run_kubectl(
            "exec", new_pod_name, "-n", args.namespace, "--",
            "cat", f"{MOUNT_POINT}/{SENTINEL_FILE}",
        )
        if rc != 0:
            result["operations"]["verify_data"] = {
                "passed": False,
                "error": f"Could not read sentinel: {err}",
            }
            print(json.dumps(result, indent=2))
            return 1

        if out.strip() != args.expected_content:
            result["operations"]["verify_data"] = {
                "passed": False,
                "error": f"Data mismatch: expected '{args.expected_content}', got '{out.strip()}'",
            }
            print(json.dumps(result, indent=2))
            return 1

        result["operations"]["verify_data"] = {"passed": True, "message": "Sentinel data intact after restart"}
        result["success"] = True

    except Exception as exc:
        result["error"] = str(exc)
        result["error_type"] = type(exc).__name__

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
