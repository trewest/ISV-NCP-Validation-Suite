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

"""Upload a custom OS image to OCP internal registry (BOOT01-01).

Uses OCP ImageStream to import a container image (containerDisk format)
into the cluster's internal registry. Reports the image ID, registry
namespace (storage bucket), and layer digests (disk IDs).

Output JSON consumed by CustomOsImageUploadedCheck (composed:
StepSuccessCheck + FieldExistsCheck for image_id, storage_bucket, disk_ids).
"""

from __future__ import annotations

import json
import os
import random
import string
import subprocess
import sys
import time


DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"
KUBECTL = os.environ.get("KUBECTL", "kubectl")
SOURCE_IMAGE = os.environ.get(
    "ISV_IMAGE_SOURCE",
    "quay.io/containerdisks/fedora:latest",
)


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


def main() -> int:
    suffix = _suffix()
    ns = f"isv-img-test-{suffix}"
    is_name = f"isv-test-image-{suffix}"

    result: dict = {
        "success": False,
        "platform": "image_registry",
        "test_name": "upload_image",
        "image_id": "",
        "storage_bucket": "",
        "disk_ids": [],
    }

    if DEMO_MODE:
        result["success"] = True
        result["image_id"] = f"{is_name}:latest"
        result["storage_bucket"] = ns
        result["disk_ids"] = ["sha256:demo0123456789abcdef"]
        print(json.dumps(result, indent=2))
        return 0

    try:
        rc, _, err = _run(["create", "namespace", ns])
        if rc != 0:
            result["error"] = f"Failed to create namespace: {err}"
            print(json.dumps(result, indent=2))
            return 1
        sys.stderr.write(f"Created namespace {ns}\n")

        is_manifest = json.dumps({
            "apiVersion": "image.openshift.io/v1",
            "kind": "ImageStream",
            "metadata": {"name": is_name, "namespace": ns},
        })
        cmd = KUBECTL.split() + ["apply", "-f", "-"]
        proc = subprocess.run(cmd, input=is_manifest, capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            result["error"] = f"ImageStream create failed: {proc.stderr.strip()}"
            print(json.dumps(result, indent=2))
            return 1

        sys.stderr.write(f"Importing image from {SOURCE_IMAGE}...\n")
        tag_manifest = json.dumps({
            "apiVersion": "image.openshift.io/v1",
            "kind": "ImageStreamTag",
            "metadata": {"name": f"{is_name}:latest", "namespace": ns},
            "tag": {
                "from": {"kind": "DockerImage", "name": SOURCE_IMAGE},
                "referencePolicy": {"type": "Source"},
            },
        })

        rc, _, err = _run([
            "import-image", is_name,
            f"--from={SOURCE_IMAGE}",
            "--confirm",
            "-n", ns,
        ], timeout=120)

        if rc != 0:
            oc_cmd = ["oc", "import-image", is_name,
                       f"--from={SOURCE_IMAGE}", "--confirm", "-n", ns]
            try:
                proc = subprocess.run(oc_cmd, capture_output=True, text=True, timeout=120)
                if proc.returncode != 0:
                    result["error"] = f"Image import failed: {proc.stderr.strip()[:300]}"
                    print(json.dumps(result, indent=2))
                    return 1
            except FileNotFoundError:
                result["error"] = f"Image import failed (oc not found): {err[:300]}"
                print(json.dumps(result, indent=2))
                return 1

        deadline = time.time() + 60
        imported = False
        while time.time() < deadline:
            rc, out, _ = _run(["get", "imagestreamtag", f"{is_name}:latest",
                                "-n", ns, "-o", "json"])
            if rc == 0 and out:
                try:
                    ist = json.loads(out)
                    image = ist.get("image", {})
                    docker_ref = image.get("dockerImageReference", "")
                    if docker_ref:
                        imported = True
                        break
                except json.JSONDecodeError:
                    pass
            time.sleep(3)

        if not imported:
            result["error"] = "ImageStreamTag did not resolve within 60s"
            print(json.dumps(result, indent=2))
            return 1

        image_data = ist.get("image", {})
        docker_ref = image_data.get("dockerImageReference", "")
        image_digest = image_data.get("metadata", {}).get("name", "")

        layers = image_data.get("dockerImageLayers", [])
        disk_ids = [layer.get("name", "") for layer in layers if layer.get("name")]

        if not disk_ids and image_digest:
            disk_ids = [image_digest]

        result["image_id"] = f"{is_name}:latest"
        result["storage_bucket"] = ns
        result["disk_ids"] = disk_ids
        result["success"] = bool(result["image_id"] and result["storage_bucket"] and result["disk_ids"])

        sys.stderr.write(f"Image imported: {docker_ref}\n")
        sys.stderr.write(f"Layers: {len(disk_ids)}\n")

    finally:
        sys.stderr.write(f"Cleaning up namespace {ns}\n")
        _run(["delete", "namespace", ns, "--wait=false"], timeout=15)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
