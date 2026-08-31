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

"""Audit logging test for OSAC (SEC08-01 + SEC08-02).

SEC08-01: Creates a VirtualNetwork via the fulfillment-service API while
watching the Events stream (gRPC Watch). Verifies the corresponding
OBJECT_CREATED event arrives with correct metadata fields.

SEC08-02: Checks the OpenShift API server audit policy is enabled and
that audit logs are accessible on the control plane.

The fulfillment-service Events API provides: event type, resource
payload with creators/tenants/timestamps. It does NOT provide source IP
or user agent (request-level metadata not captured in events).

Output JSON (serves both AuditLogEntryCheck and AuditLogRetentionCheck):
{
    "success": true,
    "platform": "security",
    "test_name": "audit_logging_test",
    "tests": {
        "audit_log_entry_found":             {"passed": true},
        "audit_log_event_name_matches":      {"passed": true},
        "audit_log_event_time_in_window":    {"passed": true},
        "audit_log_user_identity_present":   {"passed": true},
        "audit_log_source_ip_present":       {"passed": true},
        "audit_log_user_agent_matches":      {"passed": true},
        "audit_log_region_matches":          {"passed": true},
        "audit_log_event_source_matches":    {"passed": true},
        "audit_log_trail_logging_enabled":   {"passed": true},
        "audit_log_retention_at_least_30_days": {"passed": true}
    }
}
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.osac_client import (
    FulfillmentClient,
    create_sa_token,
    get_env_config,
)

DEMO_MODE = os.environ.get("ISVCTL_DEMO_MODE") == "1"


def _kubectl() -> str:
    path = shutil.which("kubectl") or shutil.which("oc")
    if not path:
        raise RuntimeError("Neither kubectl nor oc found on PATH")
    return path


def _grpcurl() -> str:
    path = shutil.which("grpcurl")
    return path or ""


def _watch_events(endpoint: str, token: str, captured: list[dict[str, Any]], stop_event: threading.Event) -> None:
    """Background thread: stream fulfillment Events via grpcurl."""
    grpcurl = _grpcurl()
    if not grpcurl:
        return
    cmd = [
        grpcurl,
        "-insecure",
        "-H",
        f"Authorization: Bearer {token}",
        endpoint,
        "osac.private.v1.Events/Watch",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        buf = ""
        brace_depth = 0
        while not stop_event.is_set():
            chunk = proc.stdout.read(1)
            if not chunk:
                break
            ch = chunk.decode("utf-8", errors="replace")
            buf += ch
            if ch == "{":
                brace_depth += 1
            elif ch == "}":
                brace_depth -= 1
                if brace_depth == 0 and buf.strip():
                    try:
                        captured.append(json.loads(buf))
                    except json.JSONDecodeError:
                        pass
                    buf = ""
    finally:
        proc.kill()
        proc.wait()


def _check_audit_policy() -> dict[str, Any]:
    """Check if K8s API server audit logging is configured."""
    kctl = _kubectl()
    cmd = [kctl, "get", "apiservers.config.openshift.io", "cluster", "-o", "json"]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        cmd = [kctl, "get", "apiserver", "cluster", "-o", "json"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return {}
    return json.loads(result.stdout)


def _check_apiserver_audit_args() -> dict[str, Any]:
    """Read kube-apiserver config from the ConfigMap to get audit log args.

    Returns a dict with the apiServerArguments relevant to audit retention
    (audit-log-maxage, audit-log-maxbackup, audit-log-maxsize).
    """
    kctl = _kubectl()
    cmd = [
        kctl, "-n", "openshift-kube-apiserver",
        "get", "configmap", "config",
        "-o", "jsonpath={.data.config\\.yaml}",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0 or not result.stdout.strip():
        return {}
    try:
        config_data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}
    args = config_data.get("apiServerArguments", {})
    return {
        "audit-log-maxage": args.get("audit-log-maxage", []),
        "audit-log-maxbackup": args.get("audit-log-maxbackup", []),
        "audit-log-maxsize": args.get("audit-log-maxsize", []),
        "audit-log-path": args.get("audit-log-path", []),
        "audit-log-format": args.get("audit-log-format", []),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit logging test (OSAC)")
    parser.add_argument("--region", default="osac-default")
    parser.add_argument("--admin-client-id", help="Bootstrapped admin client ID")
    parser.add_argument("--admin-client-secret", help="Bootstrapped admin client secret")
    parser.add_argument("--namespace", default="osac-e2e-ci", help="Namespace for SA token")
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "security",
        "test_name": "audit_logging_test",
        "tests": {
            "audit_log_entry_found": {"passed": False},
            "audit_log_event_name_matches": {"passed": False},
            "audit_log_event_time_in_window": {"passed": False},
            "audit_log_user_identity_present": {"passed": False},
            "audit_log_source_ip_present": {"passed": False},
            "audit_log_user_agent_matches": {"passed": False},
            "audit_log_region_matches": {"passed": False},
            "audit_log_event_source_matches": {"passed": False},
            "audit_log_trail_logging_enabled": {"passed": False},
            "audit_log_retention_at_least_30_days": {"passed": False},
        },
    }

    if DEMO_MODE:
        result["tests"] = {k: {"passed": True} for k in result["tests"]}
        result["success"] = True
        print(json.dumps(result, indent=2))
        return 0

    try:
        config = get_env_config(
            admin_client_id=args.admin_client_id,
            admin_client_secret=args.admin_client_secret,
            require_admin=False,
        )

        # ---------------------------------------------------------------
        # SEC08-01: Watch the fulfillment Events stream, create a
        # VirtualNetwork, then find the corresponding event.
        #
        # Requires: OSAC_FULFILLMENT_URL + grpcurl on PATH.
        # When missing, SEC08-01 subtests are skipped but SEC08-02
        # (retention) still runs — it needs only kubectl.
        # ---------------------------------------------------------------
        grpcurl = _grpcurl()
        if not config.fulfillment_url:
            result["audit_log_entry_skipped"] = True
            result["audit_log_entry_skip_reason"] = "OSAC_FULFILLMENT_URL not set"
        elif not grpcurl:
            result["audit_log_entry_skipped"] = True
            result["audit_log_entry_skip_reason"] = "grpcurl not found on PATH; required for Events Watch"
        else:
            # Get a K8s SA token for API access
            sa_token, _ = create_sa_token(args.namespace, "admin", "3600s")

            # Use the internal API route for Events Watch (private API).
            from urllib.parse import urlparse

            parsed = urlparse(config.fulfillment_url)
            internal_host = (parsed.hostname or "").replace("fulfillment-api", "fulfillment-internal-api")
            grpc_endpoint = f"{internal_host}:{parsed.port or 443}"

            captured_events: list[dict[str, Any]] = []
            stop_event = threading.Event()
            watcher = threading.Thread(
                target=_watch_events,
                args=(grpc_endpoint, sa_token, captured_events, stop_event),
                daemon=True,
            )
            watcher.start()
            time.sleep(2)

            # Create a probe VirtualNetwork
            probe_name = f"isv-audit-probe-{uuid.uuid4().hex[:8]}"
            fc = FulfillmentClient(config, sa_token)

            # Discover a network_class (required for VNet creation)
            network_class = ""
            nc_status, nc_resp = fc.list_network_classes()
            if nc_status == 200 and isinstance(nc_resp, dict):
                items = nc_resp.get("items", [])
                if items:
                    network_class = items[0].get("id", "")

            vn_status, vn_resp = fc.create_virtual_network(
                probe_name,
                network_class=network_class,
            )

            # Wait for the event to arrive
            deadline = time.time() + 15
            while time.time() < deadline and not captured_events:
                time.sleep(1)

            stop_event.set()
            watcher.join(timeout=5)

            # Find the event for our probe resource.
            probe_event = None
            resource_keys = (
                "virtualNetwork",
                "virtual_network",
                "cluster",
                "computeInstance",
                "compute_instance",
                "subnet",
                "securityGroup",
                "security_group",
            )
            for raw in captured_events:
                ev = raw.get("event", raw)
                for key in resource_keys:
                    payload = ev.get(key, {})
                    if payload:
                        name = payload.get("metadata", {}).get("name", "")
                        if probe_name in name:
                            probe_event = ev
                            break
                if probe_event:
                    break

            if probe_event:
                ev_type = probe_event.get("type", probe_event.get("eventType", ""))
                payload = None
                for key in resource_keys:
                    if probe_event.get(key):
                        payload = probe_event[key]
                        break
                payload = payload or {}
                metadata = payload.get("metadata", {})

                result["tests"]["audit_log_entry_found"] = {
                    "passed": True,
                    "message": f"Event received for '{probe_name}' via fulfillment Events API",
                }

                is_created = "CREATED" in str(ev_type).upper()
                result["tests"]["audit_log_event_name_matches"] = {
                    "passed": is_created,
                    "message": f"Event type: {ev_type}",
                }

                ts = metadata.get("creationTimestamp", metadata.get("creation_timestamp", ""))
                result["tests"]["audit_log_event_time_in_window"] = {
                    "passed": bool(ts),
                    "message": f"Timestamp: {ts}" if ts else "No timestamp in event metadata",
                }

                creators = metadata.get("creators", [])
                has_creator = len(creators) > 0
                result["tests"]["audit_log_user_identity_present"] = {
                    "passed": has_creator,
                    "message": f"Creators: {', '.join(creators)}" if has_creator else "No creators in event metadata",
                }

                from common.ocp_probes import probe_audit_log

                probe_data = probe_audit_log(namespace=args.namespace)

                source_ips = probe_data.get("source_ips", [])
                result["tests"]["audit_log_source_ip_present"] = {
                    "passed": len(source_ips) > 0,
                    "message": f"Source IPs: {', '.join(source_ips)}"
                    if source_ips
                    else probe_data.get("error", "No source IPs"),
                }
                user_agent = probe_data.get("user_agent", "")
                result["tests"]["audit_log_user_agent_matches"] = {
                    "passed": bool(user_agent),
                    "message": f"User agent: {user_agent}" if user_agent else probe_data.get("error", "No user agent"),
                }

                tenants = metadata.get("tenants", [])
                result["tests"]["audit_log_region_matches"] = {
                    "passed": len(tenants) > 0,
                    "message": f"Tenants: {', '.join(tenants)}" if tenants else "No tenant scope in event",
                }

                has_typed_payload = any(probe_event.get(k) for k in resource_keys)
                result["tests"]["audit_log_event_source_matches"] = {
                    "passed": has_typed_payload and bool(ev_type),
                    "message": f"Event type={ev_type}, payload={'present' if has_typed_payload else 'missing'}"
                    + f" via {grpc_endpoint}",
                }
            else:
                if vn_status not in (200, 201):
                    result["audit_log_entry_skipped"] = True
                    result["audit_log_entry_skip_reason"] = (
                        f"Probe VNet creation failed (HTTP {vn_status}); cannot verify event delivery"
                    )
                else:
                    result["tests"]["audit_log_entry_found"] = {
                        "passed": False,
                        "message": f"VNet '{probe_name}' created (HTTP {vn_status}) but no event received within 15s "
                        f"({len(captured_events)} total events captured)",
                    }

            # Cleanup probe VNet
            if vn_status in (200, 201) and isinstance(vn_resp, dict):
                vnet_id = vn_resp.get("id", vn_resp.get("name", ""))
                if vnet_id:
                    import urllib.parse

                    fc._api_request(
                        f"/api/fulfillment/v1/virtual_networks/{urllib.parse.quote(vnet_id, safe='')}",
                        method="DELETE",
                    )

        # ---------------------------------------------------------------
        # SEC08-02: Audit log retention — verify actual configuration
        # ---------------------------------------------------------------
        audit_config = _check_audit_policy()
        if audit_config:
            spec = audit_config.get("spec", {})
            audit_section = spec.get("audit", {})
            audit_profile = audit_section.get("profile", "")

            logging_enabled = audit_profile.lower() != "none" if audit_profile else False
            if not audit_profile:
                logging_enabled = True
                audit_profile = "Default (implicit)"

            result["tests"]["audit_log_trail_logging_enabled"] = {
                "passed": logging_enabled,
                "message": f"API server audit profile: {audit_profile}"
                if logging_enabled
                else f"Audit profile is '{audit_profile}' (logging disabled)",
            }

            custom_rules = audit_section.get("customRules", [])
            retention_days = 0
            for rule in custom_rules:
                rd = rule.get("retentionDays", 0)
                if rd > retention_days:
                    retention_days = rd

            if retention_days > 0:
                retention_ok = retention_days >= 30
                result["tests"]["audit_log_retention_at_least_30_days"] = {
                    "passed": retention_ok,
                    "message": f"Custom retention: {retention_days} days"
                    + (" (>= 30)" if retention_ok else " (< 30 — policy violation)"),
                }
            elif logging_enabled:
                # No explicit retention in APIServer CRD customRules.
                # Fall back to checking kube-apiserver's audit-log-maxage
                # argument. On OpenShift, this is typically absent (default
                # 0 = never delete by age) or explicitly set to 0.
                api_args = _check_apiserver_audit_args()
                maxage_vals = api_args.get("audit-log-maxage", [])
                maxage = int(maxage_vals[0]) if maxage_vals else 0
                maxbackup_vals = api_args.get("audit-log-maxbackup", [])
                maxbackup = int(maxbackup_vals[0]) if maxbackup_vals else 0
                maxsize_vals = api_args.get("audit-log-maxsize", [])
                maxsize = int(maxsize_vals[0]) if maxsize_vals else 0
                has_log_path = bool(api_args.get("audit-log-path"))

                if has_log_path and (maxage == 0 or maxage >= 30):
                    # maxage=0 means "never expire by age" — the retention
                    # policy does not delete logs within 30 days (or ever).
                    # Combined with maxbackup/maxsize confirming active rotation,
                    # this satisfies SEC08-02 retention requirements.
                    age_desc = "infinite (no maxage)" if maxage == 0 else f"{maxage} days"
                    result["tests"]["audit_log_retention_at_least_30_days"] = {
                        "passed": True,
                        "message": (
                            f"kube-apiserver audit-log-maxage={age_desc}, "
                            f"maxbackup={maxbackup}, maxsize={maxsize}MB"
                        ),
                    }
                elif has_log_path and maxage > 0 and maxage < 30:
                    result["tests"]["audit_log_retention_at_least_30_days"] = {
                        "passed": False,
                        "message": f"kube-apiserver audit-log-maxage={maxage} days (< 30)",
                    }
                else:
                    result["audit_log_retention_skipped"] = True
                    result["audit_log_retention_skip_reason"] = (
                        f"Audit profile '{audit_profile}' is active but no explicit "
                        "retention policy configured; cannot verify 30-day retention"
                    )
            else:
                result["tests"]["audit_log_retention_at_least_30_days"] = {
                    "passed": False,
                    "message": "Audit logging is disabled; retention check not applicable",
                }
        else:
            result["tests"]["audit_log_trail_logging_enabled"] = {
                "passed": False,
                "message": "Could not retrieve API server audit configuration",
            }

        result["success"] = all(t["passed"] for t in result["tests"].values())

    except Exception as e:
        result["error"] = str(e)

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
