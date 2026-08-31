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

"""S3 object lifecycle test via MinIO deployed on the cluster.

Exercises CRUD operations (put, get, delete) on a MinIO instance running
as a K8s service. Uses the mc-client pod (deployed by deploy-test-services.sh)
to run MinIO Client commands via kubectl exec.

Covers DATASVC01-01 (ObjectStorageCrudCheck — composed check).

Output JSON:
{
    "success": true,
    "platform": "control_plane",
    "test_name": "s3_object_lifecycle",
    "bucket_name": "isvtest-...",
    "object_key": "test-object.txt",
    "operations": {
        "put":    {"passed": true, "message": "..."},
        "get":    {"passed": true, "message": "..."},
        "delete": {"passed": true, "message": "..."}
    }
}
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"
KUBECTL = os.environ.get("KUBECTL", "kubectl")

MINIO_NS = os.environ.get("MINIO_NS", "minio")
MC_POD = os.environ.get("MC_POD", "mc-client")
MC_ALIAS = "local"


def run_kubectl(*args: str, stdin: str | None = None, timeout: int = 60) -> tuple[int, str, str]:
    cmd = KUBECTL.split() + list(args)
    try:
        proc = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except subprocess.TimeoutExpired:
        return 1, "", f"kubectl timed out after {timeout}s"
    except Exception as exc:
        return 1, "", str(exc)


def mc_exec(*mc_args: str, timeout: int = 30) -> tuple[int, str, str]:
    return run_kubectl(
        "exec", "-n", MINIO_NS, MC_POD, "--", "mc", *mc_args,
        timeout=timeout,
    )


def main() -> int:
    suffix = uuid.uuid4().hex[:8]
    bucket_name = f"isvtest-{suffix}"
    object_key = "test-object.txt"
    object_content = f"validation-test-data-{suffix}"
    bucket_path = f"{MC_ALIAS}/{bucket_name}"

    result: dict = {
        "success": False,
        "platform": "control_plane",
        "test_name": "s3_object_lifecycle",
        "bucket_name": bucket_name,
        "object_key": object_key,
        "operations": {
            "put": {"passed": False, "message": ""},
            "get": {"passed": False, "message": ""},
            "delete": {"passed": False, "message": ""},
        },
    }

    if DEMO_MODE:
        for op in result["operations"]:
            result["operations"][op] = {"passed": True, "message": f"demo: {op} ok"}
        result["success"] = True
        print(json.dumps(result, indent=2))
        return 0

    try:
        # Create bucket
        rc, out, err = mc_exec("mb", bucket_path)
        if rc != 0:
            result["operations"]["put"] = {"passed": False, "error": f"mb failed: {err}"}
            print(json.dumps(result, indent=2))
            return 0

        # PUT — pipe content into the bucket as an object
        rc, out, err = run_kubectl(
            "exec", "-n", MINIO_NS, MC_POD, "-i", "--",
            "sh", "-c", f"mc pipe {bucket_path}/{object_key}",
            stdin=object_content,
            timeout=30,
        )
        if rc != 0:
            result["operations"]["put"] = {"passed": False, "error": f"put failed: {err}"}
        else:
            result["operations"]["put"] = {"passed": True, "message": f"object '{object_key}' uploaded to '{bucket_name}'"}

        # GET — read the object back and verify content
        rc, out, err = mc_exec("cat", f"{bucket_path}/{object_key}")
        if rc != 0:
            result["operations"]["get"] = {"passed": False, "error": f"get failed: {err}"}
        elif out != object_content:
            result["operations"]["get"] = {
                "passed": False,
                "error": f"content mismatch: got '{out[:50]}', expected '{object_content[:50]}'",
            }
        else:
            result["operations"]["get"] = {"passed": True, "message": f"object '{object_key}' content verified"}

        # DELETE — remove the object and bucket
        rc, out, err = mc_exec("rm", f"{bucket_path}/{object_key}")
        if rc != 0:
            result["operations"]["delete"] = {"passed": False, "error": f"rm failed: {err}"}
        else:
            rc2, _, err2 = mc_exec("rb", bucket_path)
            if rc2 != 0:
                result["operations"]["delete"] = {"passed": False, "error": f"rb failed: {err2}"}
            else:
                result["operations"]["delete"] = {"passed": True, "message": f"object and bucket deleted"}

        result["success"] = all(op["passed"] for op in result["operations"].values())

    except Exception as e:
        result["error"] = str(e)
        result["error_type"] = type(e).__name__
        # Best-effort cleanup
        mc_exec("rb", "--force", bucket_path)

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
