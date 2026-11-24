#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not use
# this file except in compliance with the License. You may obtain a copy of the
# License at
#
# http://www.apache.org/licenses/LICENSE-2.0

"""Query the target machine's read-only NICo health report."""

from __future__ import annotations

import os
from typing import Any

from common import NicoCommandError, emit, env_value, nico_json

FAILURE_CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "device": ("power", "fan", "temp", "thermal", "voltage", "gpu", "cpu", "sensor", "psu"),
    "network": ("nic", "link", "ethernet", "infiniband", "network", "bgp", "mlx"),
    "memory": ("memory", "ecc", "dimm", "rowremap", "row_remap", "hbm", "sram"),
    "drive": ("disk", "nvme", "hdd", "storage", "drive", "boss", "raid"),
}


def _health_records(reports: Any) -> list[dict[str, Any]]:
    """Flatten health-report entries into success and alert probe records."""
    records: list[dict[str, Any]] = []
    if not isinstance(reports, list):
        return records
    for entry in reports:
        if not isinstance(entry, dict) or not isinstance(entry.get("report"), dict):
            continue
        report = entry["report"]
        records.extend(item for item in report.get("successes", []) if isinstance(item, dict))
        records.extend(item for item in report.get("alerts", []) if isinstance(item, dict))
    return records


def _category_observability(probes: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Map BMC probe IDs and targets to provider-neutral failure categories."""
    categories: dict[str, dict[str, Any]] = {}
    for category, keywords in FAILURE_CATEGORY_KEYWORDS.items():
        matched_ids = sorted(
            {
                str(probe.get("id"))
                for probe in probes
                if probe.get("id")
                and any(
                    keyword in " ".join(str(probe.get(key) or "").lower() for key in ("id", "target", "message"))
                    for keyword in keywords
                )
            }
        )
        categories[category] = {"observable": bool(matched_ids), "probe_ids": matched_ids}
    return categories


def main() -> int:
    """Emit OOB health evidence without mutating BMC or machine state."""
    result: dict[str, Any] = {
        "success": False,
        "platform": "nico",
        "site_id": os.environ.get("NICO_SITE_ID", "nvl72"),
        "test_name": "query_oob_health",
        "hosts_checked": 0,
        "hosts": [],
    }
    try:
        machine_id = env_value("NICO_MACHINE_ID")
        reports = nico_json("machine", "health-report", "show", "--extended", machine_id)
        probes = _health_records(reports)
        bmc_probes = [probe for probe in probes if str(probe.get("id") or "").lower().startswith("bmc")]
        bmc_probe_ids = sorted({str(probe.get("id")) for probe in bmc_probes if probe.get("id")})
        alerts = [probe for probe in bmc_probes if probe.get("message")]

        host = {
            "host_id": machine_id,
            "oob_health_present": bool(reports),
            "bmc_probe_ids": bmc_probe_ids,
            "failure_categories": _category_observability(bmc_probes),
            "alerts": alerts,
        }
        result.update({"success": True, "hosts_checked": 1, "hosts": [host]})
    except NicoCommandError as exc:
        result["error"] = str(exc)
        result["error_type"] = type(exc).__name__

    return emit(result)


if __name__ == "__main__":
    raise SystemExit(main())
