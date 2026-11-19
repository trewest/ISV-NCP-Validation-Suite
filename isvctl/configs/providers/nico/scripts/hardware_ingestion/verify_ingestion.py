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

"""Verify hardware ingestion against NICo expected-machine manifest.

Calls the NICo REST API to compare expected-machine records against
actually discovered machines. Each expected-machine record carries the
authoritative link to its discovered machine via the ``machineId`` field
(null when not yet ingested), so we join on that rather than chassis serial.

NICo API endpoints used:
  GET /v2/org/{org}/carbide/expected-machine?siteId={site_id}
  GET /v2/org/{org}/carbide/machine?siteId={site_id}
  GET /v2/org/{org}/carbide/dpu?siteId={site_id}

Auth:
  - NICO_BEARER_TOKEN, or
  - OIDC client_credentials via NICO_SSA_ISSUER,
    NICO_CLIENT_ID, NICO_CLIENT_SECRET, and optional NICO_OIDC_SCOPE.

Required JSON output fields:
  {
    "success": true,
    "platform": "nico",
    "site_id": "...",
    "expected_count": 4,
    "ingested_count": 4,
    "matched_count": 4,
    "missing": [],
    "extra": [],
    "machines": [
      {
        "chassis_serial": "1871125000734",
        "expected_machine_id": "...",
        "machine_id": "...",
        "status": "Ready",
        "health": "healthy",
        "gpu_count": 4,
        "dpu_count": 2,
        "capabilities": ["GPU", "DPU", "InfiniBand"]
      }
    ]
  }

Usage:
    NICO_BEARER_TOKEN=<token> python verify_ingestion.py --org <org> --site-id <uuid> --api-base <url>

    Wired via the bare_metal suite:
      uv run isvctl test run -f isvctl/configs/providers/nico/config/bare_metal.yaml

Reference:
    OpenAPI spec: ncp-isv-carbide-proxy-service/src/main/resources/docs/openapi/forge_api.yaml
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Allow importing from sibling common/ directory
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.nico_client import (
    NicoAuthError,
    classify_health,
    forge_get_all,
    resolve_auth,
    sum_capabilities,
)


def _normalize_text(value: Any) -> str:
    """Normalize a case-insensitive hardware identity field."""
    return value.strip().upper() if isinstance(value, str) else ""


def _normalize_mac(value: Any) -> str:
    """Normalize a MAC address for comparison."""
    return _normalize_text(value).replace(":", "").replace("-", "")


def _dpu_matches_expected(expected: dict[str, Any], dpu: dict[str, Any]) -> bool:
    """Match an expected DPU record by serial or BMC identity."""
    dmi = dpu.get("dmiData") or {}
    bmc = dpu.get("bmcInfo") or {}

    expected_serial = _normalize_text(expected.get("chassisSerialNumber"))
    dpu_serial = _normalize_text(dmi.get("productSerial") or dmi.get("chassisSerial"))
    if expected_serial and dpu_serial and expected_serial == dpu_serial:
        return True

    expected_mac = _normalize_mac(expected.get("bmcMacAddress"))
    dpu_mac = _normalize_mac(bmc.get("mac"))
    if expected_mac and dpu_mac and expected_mac == dpu_mac:
        return True

    expected_ip = _normalize_text(expected.get("bmcIpAddress"))
    dpu_ip = _normalize_text(bmc.get("ip"))
    return bool(expected_ip and dpu_ip and expected_ip == dpu_ip)


def _find_dpu_for_expected(expected: dict[str, Any], dpus: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Find the discovered DPU represented by an expected-machine record."""
    return next((dpu for dpu in dpus if _dpu_matches_expected(expected, dpu)), None)


def _dpu_status(state: Any) -> str:
    """Map NICo's hierarchical DPU state to the validation status vocabulary."""
    if not isinstance(state, str) or not state:
        return "Unknown"
    terminal_state = state.rsplit("/", 1)[-1]
    return terminal_state if terminal_state in {"Ready", "InUse"} else state


def _dpu_detail(expected: dict[str, Any], dpu: dict[str, Any]) -> dict[str, Any]:
    """Convert a discovered DPU to the provider-neutral ingestion shape."""
    dmi = dpu.get("dmiData") or {}
    dpu_id = dpu.get("id")
    return {
        "chassis_serial": expected.get("chassisSerialNumber") or dmi.get("productSerial", ""),
        "expected_machine_id": expected.get("id", ""),
        "machine_id": dpu_id,
        "status": _dpu_status(dpu.get("state")),
        "health": classify_health(dpu.get("health") or {}),
        "gpu_count": 0,
        "dpu_count": 1,
        "capabilities": ["DPU"],
    }


def main() -> int:
    """Compare the expected-machine manifest with discovered machines and print JSON."""
    parser = argparse.ArgumentParser(description="Verify NICo hardware ingestion")
    parser.add_argument("--org", required=True, help="NGC org name")
    parser.add_argument("--site-id", required=True, help="NICo site UUID")
    parser.add_argument(
        "--api-base",
        required=True,
        help="NICo API base URL",
    )
    args = parser.parse_args()

    result: dict[str, Any] = {
        "success": False,
        "platform": "nico",
        "site_id": args.site_id,
        "expected_count": 0,
        "ingested_count": 0,
        "matched_count": 0,
        "missing": [],
        "extra": [],
        "machines": [],
    }

    try:
        auth = resolve_auth()

        # Fetch all expected machines (paginated). Each carries machineId, the
        # authoritative link to a discovered machine (null when not ingested).
        expected_machines = forge_get_all(
            args.org,
            "expected-machine",
            auth.token,
            base_url=args.api_base,
            params={"siteId": args.site_id},
            result_key="expectedMachines",
        )

        # Fetch all actual machines (paginated). status, machineCapabilities and
        # health are top-level, so no metadata is needed -- we join by machine id.
        actual_machines = forge_get_all(
            args.org,
            "machine",
            auth.token,
            base_url=args.api_base,
            params={"siteId": args.site_id},
            result_key="machines",
        )

        # NICo exposes DPUs through a separate site-scoped endpoint. Expected
        # DPU records commonly have machineId=null because the DPU relation is
        # represented by hostMachineId in the DPU inventory, not by the host
        # ExpectedMachine.machineId field.
        actual_dpus = forge_get_all(
            args.org,
            "dpu",
            auth.token,
            base_url=args.api_base,
            params={"siteId": args.site_id},
            result_key="dpus",
        )

        actual_dpus_by_id: dict[str, dict[str, Any]] = {d["id"]: d for d in actual_dpus if d.get("id")}
        actual_by_id: dict[str, dict[str, Any]] = {m["id"]: m for m in [*actual_machines, *actual_dpus] if m.get("id")}
        linked_machine_ids: set[str] = set()
        machines_detail: list[dict[str, Any]] = []

        for em in expected_machines:
            expected_machine_id = em.get("id", "")
            machine_id = em.get("machineId")
            # Manifest chassis serial, kept as the provider-neutral identity hint.
            chassis_serial = em.get("chassisSerialNumber", "")

            # DPU ExpectedMachine records are linked by their DPU serial/BMC
            # identity because their host-machine relation is exposed by the
            # separate DPU endpoint rather than machineId on the expected row.
            dpu = actual_dpus_by_id.get(machine_id) if machine_id else _find_dpu_for_expected(em, actual_dpus)
            if dpu:
                dpu_detail = _dpu_detail(em, dpu)
                if dpu_detail["machine_id"]:
                    linked_machine_ids.add(dpu_detail["machine_id"])
                    machines_detail.append(dpu_detail)
                    continue

            if not machine_id:
                result["missing"].append({"expected_machine_id": expected_machine_id, "chassis_serial": chassis_serial})
                machines_detail.append(
                    {
                        "chassis_serial": chassis_serial,
                        "expected_machine_id": expected_machine_id,
                        "machine_id": None,
                        "status": "NotFound",
                        "health": "unknown",
                        "gpu_count": 0,
                        "dpu_count": 0,
                        "capabilities": [],
                    }
                )
                continue

            linked_machine_ids.add(machine_id)
            actual = actual_by_id.get(machine_id) or {}
            capabilities = actual.get("machineCapabilities") or []
            cap_types = list({c.get("type", "") for c in capabilities})
            health = actual.get("health") or {}

            machines_detail.append(
                {
                    "chassis_serial": chassis_serial,
                    "expected_machine_id": expected_machine_id,
                    "machine_id": machine_id,
                    "status": actual.get("status", "Unknown"),
                    "health": classify_health(health),
                    "gpu_count": sum_capabilities(capabilities, "GPU"),
                    "dpu_count": sum_capabilities(capabilities, "DPU"),
                    "capabilities": cap_types,
                }
            )

        # Find extra machines (discovered but not linked to any expected machine)
        for machine_id in actual_by_id:
            if machine_id not in linked_machine_ids:
                result["extra"].append({"machine_id": machine_id})

        matched = [m for m in machines_detail if m.get("machine_id") is not None]

        result["expected_count"] = len(expected_machines)
        result["ingested_count"] = len(actual_by_id)
        result["matched_count"] = len(matched)
        result["machines"] = machines_detail
        result["success"] = True

    except NicoAuthError as e:
        result["error_type"] = "auth"
        result["error"] = str(e)
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"

    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
