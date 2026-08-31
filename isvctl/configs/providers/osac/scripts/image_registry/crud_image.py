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

"""CRUD operations on custom OS images via OCP ImageStreams.

Maps image registry CRUD to OCP ImageStream operations:
  create  → kubectl apply ImageStream manifest
  get     → kubectl get imagestream (by name)
  list    → kubectl get imagestreams (all in namespace)
  delete  → kubectl delete imagestream

Outputs JSON consumed by CustomOsImageCrudCheck (BOOT03-02):
  success, image_id, operations.{create,get,list,delete}.passed
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time


DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"
KUBECTL = os.environ.get("KUBECTL", "kubectl")


def run_kubectl(*args: str, timeout: int = 60) -> tuple[int, str, str]:
    cmd = KUBECTL.split() + list(args)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except subprocess.TimeoutExpired:
        return 1, "", f"Command timed out after {timeout}s"
    except Exception as exc:
        return 1, "", str(exc)


def ensure_namespace(ns: str) -> bool:
    rc, _, _ = run_kubectl("get", "namespace", ns)
    if rc == 0:
        return True
    rc, _, err = run_kubectl("create", "namespace", ns)
    if rc != 0:
        sys.stderr.write(f"Failed to create namespace {ns}: {err}\n")
    return rc == 0


def build_imagestream_manifest(name: str, namespace: str, source_image: str) -> dict:
    return {
        "apiVersion": "image.openshift.io/v1",
        "kind": "ImageStream",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {"created-by": "isvtest"},
        },
        "spec": {
            "lookupPolicy": {"local": True},
            "tags": [
                {
                    "name": "latest",
                    "from": {"kind": "DockerImage", "name": source_image},
                    "referencePolicy": {"type": "Local"},
                },
            ],
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Image CRUD via OCP ImageStreams (OSAC)")
    parser.add_argument("--namespace", default="isv-image-crud-test")
    parser.add_argument("--image-name", default="isv-test-image")
    parser.add_argument("--source-image", default="quay.io/containerdisks/fedora:latest")
    args = parser.parse_args()

    result: dict = {
        "success": False,
        "platform": "image_registry",
        "test_name": "crud_image",
        "image_id": "",
        "operations": {},
    }

    if DEMO_MODE:
        result.update({
            "success": True,
            "image_id": "demo-isv-test-image",
            "operations": {
                "create": {"passed": True, "image_id": "demo-isv-test-image"},
                "get": {"passed": True, "image_name": "demo-isv-test-image", "state": "available"},
                "list": {"passed": True, "image_count": 1},
                "delete": {"passed": True},
            },
        })
        print(json.dumps(result, indent=2))
        return 0

    try:
        if not ensure_namespace(args.namespace):
            result["error"] = f"Failed to create namespace {args.namespace}"
            print(json.dumps(result, indent=2))
            return 1

        # --- CREATE ---
        sys.stderr.write(f"Creating ImageStream {args.image_name}...\n")
        run_kubectl(
            "delete", "imagestream", args.image_name, "-n", args.namespace,
            "--ignore-not-found=true",
        )

        manifest = build_imagestream_manifest(
            args.image_name, args.namespace, args.source_image,
        )
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False,
        ) as f:
            json.dump(manifest, f)
            manifest_path = f.name

        try:
            rc, out, err = run_kubectl("apply", "-f", manifest_path)
        finally:
            os.unlink(manifest_path)

        if rc != 0:
            result["operations"]["create"] = {"passed": False, "error": err}
            result["error"] = f"create failed: {err}"
            print(json.dumps(result, indent=2))
            return 1

        result["image_id"] = args.image_name
        result["operations"]["create"] = {
            "passed": True,
            "image_id": args.image_name,
        }
        sys.stderr.write("  create: OK\n")

        time.sleep(3)

        # --- GET ---
        sys.stderr.write(f"Getting ImageStream {args.image_name}...\n")
        rc, out, err = run_kubectl(
            "get", "imagestream", args.image_name, "-n", args.namespace,
            "-o", "json",
        )
        if rc == 0:
            data = json.loads(out)
            tags = data.get("status", {}).get("tags", [])
            result["operations"]["get"] = {
                "passed": True,
                "image_name": data["metadata"]["name"],
                "state": "available" if tags else "pending",
            }
            sys.stderr.write("  get: OK\n")
        else:
            result["operations"]["get"] = {"passed": False, "error": err}
            sys.stderr.write(f"  get: FAILED ({err})\n")

        # --- LIST ---
        sys.stderr.write("Listing ImageStreams...\n")
        rc, out, err = run_kubectl(
            "get", "imagestreams", "-n", args.namespace, "-o", "json",
        )
        if rc == 0:
            data = json.loads(out)
            items = data.get("items", [])
            found = any(
                item["metadata"]["name"] == args.image_name for item in items
            )
            result["operations"]["list"] = {
                "passed": found,
                "image_count": len(items),
            }
            if not found:
                result["operations"]["list"]["error"] = "Image not found in list"
            sys.stderr.write(f"  list: {'OK' if found else 'FAILED'} ({len(items)} images)\n")
        else:
            result["operations"]["list"] = {"passed": False, "error": err}
            sys.stderr.write(f"  list: FAILED ({err})\n")

        # --- DELETE ---
        sys.stderr.write(f"Deleting ImageStream {args.image_name}...\n")
        rc, out, err = run_kubectl(
            "delete", "imagestream", args.image_name, "-n", args.namespace,
        )
        if rc == 0:
            rc2, _, _ = run_kubectl(
                "get", "imagestream", args.image_name, "-n", args.namespace,
            )
            result["operations"]["delete"] = {"passed": rc2 != 0}
            if rc2 == 0:
                result["operations"]["delete"]["error"] = "ImageStream still exists after delete"
            sys.stderr.write(f"  delete: {'OK' if rc2 != 0 else 'FAILED'}\n")
        else:
            result["operations"]["delete"] = {"passed": False, "error": err}
            sys.stderr.write(f"  delete: FAILED ({err})\n")

        all_passed = all(
            op.get("passed", False) for op in result["operations"].values()
        )
        result["success"] = all_passed

    except Exception as exc:
        result["error"] = str(exc)
        result["error_type"] = type(exc).__name__

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
