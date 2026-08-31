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

"""SDN filtering rule audit trail test for OSAC (SDN09-03).

Creates a SecurityGroup, mutates its rules, deletes it, then verifies
that every lifecycle event (create, update, delete) was captured in the
OpenShift kube-apiserver audit log.

The fulfillment REST API operations trigger osac-operator CRD
reconciliation; those CRD mutations appear in the K8s audit log with
actor, timestamp, verb, and target resource.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.osac_client import FulfillmentClient, create_sa_token, get_env_config, wait_crd_ready

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"
AUDIT_POLL_SECONDS = 5
AUDIT_WAIT_SECONDS = 30
DELETE_POLL_SECONDS = 5
DELETE_TIMEOUT_SECONDS = 120


def _oc() -> str:
    path = shutil.which("oc")
    return path or ""


def _kubectl() -> str:
    path = shutil.which("kubectl") or shutil.which("oc")
    if not path:
        raise RuntimeError("Neither kubectl nor oc found on PATH")
    return path


def _get_audit_log_files(oc: str) -> list[str]:
    """List kube-apiserver audit log files on master nodes."""
    cmd = [oc, "adm", "node-logs", "--role=master", "--path=kube-apiserver/"]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        return []
    lines = result.stdout.strip().split("\n")
    files: list[str] = []
    if any("audit.log" in ln for ln in lines):
        files.append("audit.log")
    rotated = [ln.split()[-1] for ln in lines if "audit-" in ln and ln.endswith(".log")]
    if rotated:
        files.append(rotated[-1])
    return files


def _get_master_nodes(oc: str, log_file: str) -> list[str]:
    """Return all master node names that have the given audit log file."""
    list_cmd = [oc, "adm", "node-logs", "--role=master", "--path=kube-apiserver/"]
    list_result = subprocess.run(list_cmd, capture_output=True, text=True, timeout=30)
    if list_result.returncode != 0:
        return []
    nodes: list[str] = []
    for line in list_result.stdout.strip().split("\n"):
        if log_file in line:
            node = line.split()[0]
            if node not in nodes:
                nodes.append(node)
    return nodes


def _search_node_audit(oc: str, node: str, log_file: str, search_term: str) -> list[dict[str, Any]]:
    """Search a single node's audit log for entries matching *search_term*."""
    oc_proc = subprocess.Popen(
        [oc, "adm", "node-logs", node, f"--path=kube-apiserver/{log_file}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    grep_proc = subprocess.Popen(
        ["grep", search_term],
        stdin=oc_proc.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    oc_proc.stdout.close()
    try:
        grep_out, _ = grep_proc.communicate(timeout=120)
    except subprocess.TimeoutExpired:
        grep_proc.kill()
        oc_proc.kill()
        return []
    finally:
        oc_proc.wait()

    if grep_proc.returncode != 0 or not grep_out:
        return []

    entries: list[dict[str, Any]] = []
    for raw_line in grep_out.decode().strip().split("\n"):
        if not raw_line.strip():
            continue
        try:
            entries.append(json.loads(raw_line))
        except json.JSONDecodeError:
            continue
    return entries


def _search_all_audit_entries(oc: str, log_file: str, search_term: str) -> list[dict[str, Any]]:
    """Search ALL master nodes' audit logs for entries matching *search_term*."""
    nodes = _get_master_nodes(oc, log_file)
    all_entries: list[dict[str, Any]] = []
    for node in nodes:
        all_entries.extend(_search_node_audit(oc, node, log_file, search_term))
    return all_entries


def _get_crd_name(sg_id: str, crd_ns: str) -> str:
    """Look up the SecurityGroup CRD name by its fulfillment UUID label."""
    kctl = _kubectl()
    cmd = [
        kctl,
        "get",
        "securitygroup",
        "-n",
        crd_ns,
        "-l",
        f"osac.openshift.io/securitygroup-uuid={sg_id}",
        "-o",
        "jsonpath={.items[0].metadata.name}",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    return ""


def _evaluate_audit_entries(
    entries: list[dict[str, Any]],
    crd_name: str,
) -> dict[str, dict[str, Any]]:
    """Map collected audit entries to the SDN09-03 subtest contract.

    The osac-operator creates SecurityGroup CRDs via CreateOrUpdate
    (verb "update", not "create"). The actual filtering rules are
    NetworkPolicies named ``sg-{crd_name}``. Both resource types are
    considered when evaluating the audit trail.
    """
    np_name = f"sg-{crd_name}"

    sg_entries = [
        e
        for e in entries
        if (
            (
                "securitygroup" in e.get("objectRef", {}).get("resource", "").lower()
                and e.get("objectRef", {}).get("name") == crd_name
            )
            or (
                e.get("objectRef", {}).get("resource") == "networkpolicies"
                and e.get("objectRef", {}).get("name") == np_name
            )
        )
    ]

    sg_verbs = {
        e.get("verb") for e in sg_entries if "securitygroup" in e.get("objectRef", {}).get("resource", "").lower()
    }
    np_verbs = {e.get("verb") for e in sg_entries if e.get("objectRef", {}).get("resource") == "networkpolicies"}

    results: dict[str, dict[str, Any]] = {}

    # create: accept CRD create, CRD first update (CreateOrUpdate), or
    # NetworkPolicy create (the actual filtering rule).
    if "create" in sg_verbs:
        results["create_rule_logged"] = {
            "passed": True,
            "message": f"Audit log contains 'create' for CRD {crd_name}",
        }
    elif "create" in np_verbs:
        results["create_rule_logged"] = {
            "passed": True,
            "message": f"Audit log contains 'create' for NetworkPolicy {np_name}",
        }
    elif "update" in sg_verbs:
        results["create_rule_logged"] = {
            "passed": True,
            "message": f"Audit log contains 'update' for CRD {crd_name} (CreateOrUpdate pattern)",
        }
    else:
        results["create_rule_logged"] = {
            "passed": False,
            "error": f"No create/update audit entry for {crd_name} or {np_name}",
        }

    if "update" in sg_verbs or "patch" in sg_verbs:
        verb_found = "update" if "update" in sg_verbs else "patch"
        results["modify_rule_logged"] = {
            "passed": True,
            "message": f"Audit log contains '{verb_found}' for {crd_name}",
        }
    elif "update" in np_verbs or "patch" in np_verbs:
        verb_found = "update" if "update" in np_verbs else "patch"
        results["modify_rule_logged"] = {
            "passed": True,
            "message": f"Audit log contains '{verb_found}' for {np_name}",
        }
    else:
        results["modify_rule_logged"] = {
            "passed": False,
            "error": f"No 'update' or 'patch' audit entry for {crd_name}",
        }

    if "delete" in sg_verbs:
        results["delete_rule_logged"] = {
            "passed": True,
            "message": f"Audit log contains 'delete' for {crd_name}",
        }
    elif "delete" in np_verbs:
        results["delete_rule_logged"] = {
            "passed": True,
            "message": f"Audit log contains 'delete' for {np_name}",
        }
    else:
        results["delete_rule_logged"] = {
            "passed": False,
            "error": f"No 'delete' audit entry for {crd_name}",
        }

    mutation_verbs = {"create", "update", "patch", "delete"}
    mutation_entries = [e for e in sg_entries if e.get("verb") in mutation_verbs]
    if mutation_entries:
        missing_fields = []
        for entry in mutation_entries:
            username = entry.get("user", {}).get("username", "")
            timestamp = entry.get("requestReceivedTimestamp", "")
            obj_name = entry.get("objectRef", {}).get("name", "")
            if not username or not timestamp or not obj_name:
                missing_fields.append(entry.get("verb", "<unknown>"))
        if missing_fields:
            results["audit_event_has_required_fields"] = {
                "passed": False,
                "error": f"Entries missing user/timestamp/objectRef: {missing_fields}",
            }
        else:
            results["audit_event_has_required_fields"] = {
                "passed": True,
                "message": f"{len(mutation_entries)} audit entries have required fields",
            }
    else:
        results["audit_event_has_required_fields"] = {
            "passed": False,
            "error": "No mutation audit entries available to validate",
        }

    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="SDN filter audit trail test (OSAC)")
    parser.add_argument("--region", required=True)
    parser.add_argument("--tenant-namespace", required=True)
    parser.add_argument("--vnet-id", required=True, help="Existing VirtualNetwork ID")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "network",
        "test_name": "sdn_filter_audit_trail",
        "trail_id": "kube-apiserver-audit",
        "actor_field": "user.username",
        "target_rule_id": "",
        "tests": {
            "audit_endpoint_reachable": {"passed": False},
            "create_rule_logged": {"passed": False},
            "modify_rule_logged": {"passed": False},
            "delete_rule_logged": {"passed": False},
            "audit_event_has_required_fields": {"passed": False},
            "cleanup": {"passed": False},
        },
    }

    if DEMO_MODE:
        result["target_rule_id"] = "isv-sg-audit-demo"
        result["tests"] = {
            "audit_endpoint_reachable": {"passed": True, "message": "audit.log accessible"},
            "create_rule_logged": {"passed": True, "message": "create logged"},
            "modify_rule_logged": {"passed": True, "message": "patch logged"},
            "delete_rule_logged": {"passed": True, "message": "delete logged"},
            "audit_event_has_required_fields": {"passed": True, "message": "3 entries valid"},
            "cleanup": {"passed": True, "message": "SG deleted"},
        }
        result["success"] = True
        print(json.dumps(result, indent=2))
        return 0

    sg_id = ""
    config = None
    try:
        config = get_env_config(require_admin=False)
        token, _ttl = create_sa_token(args.tenant_namespace, "default")
        client = FulfillmentClient(config, token)
        crd_ns = config.tenant_namespace

        suffix = f"{int(time.time()) % 0xFFFF:04x}"
        sg_name = f"isv-sg-audit-{suffix}"

        # --- Create SecurityGroup with TCP 443 ingress rule ---
        s, b = client.create_security_group(
            sg_name,
            args.vnet_id,
            ingress=[
                {
                    "protocol": "PROTOCOL_TCP",
                    "port_from": 443,
                    "port_to": 443,
                    "ipv4_cidr": "0.0.0.0/0",
                },
            ],
        )
        if s not in (200, 201):
            result["tests"]["cleanup"] = {"passed": True, "message": "SG was not created"}
            result["error"] = f"Failed to create SecurityGroup: HTTP {s}: {b}"
            print(json.dumps(result, indent=2))
            return 1
        sg_id = b["id"]
        result["target_rule_id"] = sg_id

        wait_crd_ready("securitygroup", sg_id, crd_ns, label="osac.openshift.io/securitygroup-uuid")

        crd_name = _get_crd_name(sg_id, crd_ns)

        # --- Update SecurityGroup: change rule to TCP 8443 ---
        s, b = client.update_security_group(
            sg_id,
            "spec.ingress",
            {
                "spec": {
                    "virtual_network": {"id": args.vnet_id},
                    "ingress": [
                        {
                            "protocol": "PROTOCOL_TCP",
                            "port_from": 8443,
                            "port_to": 8443,
                            "ipv4_cidr": "0.0.0.0/0",
                        },
                    ],
                },
            },
        )
        time.sleep(5)

        # --- Delete SecurityGroup ---
        s, b = client.delete_security_group(sg_id)
        cleanup_passed = s in (200, 204)

        if cleanup_passed:
            deadline = time.time() + DELETE_TIMEOUT_SECONDS
            while time.time() < deadline:
                s, _ = client.get_security_group(sg_id)
                if s in (404, 410):
                    break
                time.sleep(DELETE_POLL_SECONDS)
            if s not in (404, 410):
                cleanup_passed = False

        if cleanup_passed:
            result["tests"]["cleanup"] = {"passed": True, "message": f"SG {sg_id} deleted"}
            sg_id = ""
        else:
            result["tests"]["cleanup"] = {"passed": False, "error": f"SG delete returned HTTP {s}"}

        # --- Wait for audit log propagation ---
        time.sleep(AUDIT_WAIT_SECONDS)

        # --- Search K8s audit log ---
        oc = _oc()
        if not oc:
            result["tests"]["audit_endpoint_reachable"] = {
                "passed": False,
                "error": "oc not found on PATH",
            }
            print(json.dumps(result, indent=2))
            return 1

        log_files = _get_audit_log_files(oc)
        if not log_files:
            result["tests"]["audit_endpoint_reachable"] = {
                "passed": False,
                "error": "No audit log files found on master nodes",
            }
            print(json.dumps(result, indent=2))
            return 1

        result["tests"]["audit_endpoint_reachable"] = {
            "passed": True,
            "message": f"Found {len(log_files)} audit log file(s): {', '.join(log_files)}",
        }

        search_term = crd_name if crd_name else sg_id
        all_entries: list[dict[str, Any]] = []
        for log_file in log_files:
            all_entries.extend(_search_all_audit_entries(oc, log_file, search_term))

        if not all_entries and crd_name and crd_name != sg_id:
            for log_file in log_files:
                all_entries.extend(_search_all_audit_entries(oc, log_file, sg_id))

        if all_entries and crd_name:
            audit_results = _evaluate_audit_entries(all_entries, crd_name)
            result["tests"].update(audit_results)
        elif all_entries:
            result["tests"]["create_rule_logged"] = {
                "passed": False,
                "error": f"Found {len(all_entries)} entries but CRD name unknown; cannot match",
            }
        else:
            for key in (
                "create_rule_logged",
                "modify_rule_logged",
                "delete_rule_logged",
                "audit_event_has_required_fields",
            ):
                result["tests"][key] = {
                    "passed": False,
                    "error": f"No audit entries found for '{search_term}' in {len(log_files)} log(s)",
                }

        result["success"] = all(t.get("passed") for t in result["tests"].values())

    except Exception as exc:
        result["error"] = str(exc)
        result["error_type"] = type(exc).__name__
    finally:
        if sg_id and config:
            try:
                cleanup_token, _ttl = create_sa_token(args.tenant_namespace, "default")
                cleanup_client = FulfillmentClient(config, cleanup_token)
                cleanup_client.delete_security_group(sg_id)
            except Exception:
                pass

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
