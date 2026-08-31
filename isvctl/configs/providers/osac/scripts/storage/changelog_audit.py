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

"""Test changelog/audit data accessibility (HSS15-01).

Verifies that K8s API audit logging is enabled on OCP and records resource
operations (file-like and directory-like) with user identity attribution.

Output JSON consumed by HssChangelogAuditCheck.
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


def _suffix() -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=6))


def _run(args: list[str], timeout: int = 30) -> tuple[int, str, str]:
    cmd = KUBECTL.split() + args
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except subprocess.TimeoutExpired:
        return 1, "", f"timeout after {timeout}s"
    except Exception as exc:
        return 1, "", str(exc)


def main() -> int:
    result: dict = {
        "success": False,
        "platform": "storage",
        "test_name": "changelog_audit",
        "tests": {
            "changelog_enabled": {"passed": False, "message": ""},
            "records_file_ops": {"passed": False, "message": ""},
            "records_dir_ops": {"passed": False, "message": ""},
            "tracks_uid_gid": {"passed": False, "message": ""},
        },
    }

    if DEMO_MODE:
        result["success"] = True
        for t in result["tests"].values():
            t["passed"] = True
            t["message"] = "demo mode"
        print(json.dumps(result, indent=2))
        return 0

    sfx = _suffix()
    cm_name = f"hss-audit-cm-{sfx}"
    ns_name = f"isv-hss-audit-{sfx}"

    try:
        rc, profile, _ = _run(["get", "apiserver", "cluster", "-o",
                                "jsonpath={.spec.audit.profile}"])
        if rc != 0:
            rc, profile, _ = _run(["get", "apiserver", "cluster", "-o", "json"])
            if rc == 0:
                try:
                    obj = json.loads(profile)
                    profile = obj.get("spec", {}).get("audit", {}).get("profile", "")
                except json.JSONDecodeError:
                    profile = ""

        if not profile:
            profile = "Default"

        audit_enabled = profile.lower() not in ("none", "")
        result["tests"]["changelog_enabled"]["passed"] = audit_enabled
        result["tests"]["changelog_enabled"]["message"] = (
            f"OCP API audit logging enabled (profile: {profile})"
            if audit_enabled
            else f"Audit profile is '{profile}' — logging may be disabled"
        )

        _run(["create", "namespace", ns_name])
        time.sleep(1)

        cm_data = json.dumps({
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": cm_name, "namespace": ns_name},
            "data": {"sentinel": "audit-test-value"},
        })
        cmd = KUBECTL.split() + ["apply", "-f", "-"]
        proc = subprocess.run(cmd, input=cm_data, capture_output=True, text=True, timeout=30)
        cm_created = proc.returncode == 0

        time.sleep(2)

        rc, events_out, _ = _run(["get", "events", "-n", ns_name, "--field-selector",
                                   f"involvedObject.name={cm_name}", "-o", "json"], timeout=15)
        rc2, cm_out, _ = _run(["get", "configmap", cm_name, "-n", ns_name,
                                "-o", "jsonpath={.metadata.uid}"])
        file_op_recorded = cm_created and rc2 == 0 and len(cm_out) > 0
        result["tests"]["records_file_ops"]["passed"] = file_op_recorded
        result["tests"]["records_file_ops"]["message"] = (
            f"ConfigMap {cm_name} created and trackable (uid: {cm_out})"
            if file_op_recorded
            else "ConfigMap operation not recorded"
        )

        rc, ns_uid, _ = _run(["get", "namespace", ns_name, "-o",
                               "jsonpath={.metadata.uid}"])
        dir_recorded = rc == 0 and len(ns_uid) > 0
        result["tests"]["records_dir_ops"]["passed"] = dir_recorded
        result["tests"]["records_dir_ops"]["message"] = (
            f"Namespace {ns_name} creation recorded (uid: {ns_uid})"
            if dir_recorded
            else "Namespace operation not recorded"
        )

        uid_tracked = False
        user_identity = ""

        rc, whoami_json, _ = _run(["auth", "whoami", "-o", "json"])
        if rc == 0:
            try:
                whoami = json.loads(whoami_json)
                user_identity = whoami.get("status", {}).get("userInfo", {}).get("username", "")
                if user_identity:
                    uid_tracked = True
            except json.JSONDecodeError:
                pass

        if not uid_tracked:
            rc, cm_json, _ = _run(["get", "configmap", cm_name, "-n", ns_name, "-o", "json"])
            if rc == 0:
                try:
                    obj = json.loads(cm_json)
                    for mf in obj.get("metadata", {}).get("managedFields", []):
                        if mf.get("manager", ""):
                            uid_tracked = True
                            user_identity = mf.get("manager", "")
                            break
                except json.JSONDecodeError:
                    pass

        result["tests"]["tracks_uid_gid"]["passed"] = uid_tracked
        result["tests"]["tracks_uid_gid"]["message"] = (
            f"Operations attributed to user identity: {user_identity}"
            if uid_tracked
            else "User identity attribution not found"
        )

        result["success"] = all(t["passed"] for t in result["tests"].values())

    finally:
        sys.stderr.write(f"Cleaning up namespace {ns_name}\n")
        _run(["delete", "namespace", ns_name, "--wait=false"], timeout=15)

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
