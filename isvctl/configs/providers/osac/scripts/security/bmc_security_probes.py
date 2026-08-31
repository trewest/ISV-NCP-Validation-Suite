#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Provider-hidden BMC security probes for OSAC on OCP.

Emits provider_hidden=true for BMC security checks where the BMC/IPMI/Redfish
management plane is not exposed to tenants.  In OCP-based clouds, BMC
management is handled by the platform (via Metal3/Ironic); tenants have no
direct access to BMC endpoints.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

ASPECT_SUBTESTS: dict[str, dict[str, Any]] = {
    "bmc_management_network": {
        "subtests": [
            "dedicated_management_network",
            "restricted_management_routes",
            "tenant_network_not_management",
            "management_acl_enforced",
        ],
        "top_level": {"management_networks_checked": 0},
        "message": (
            "BMC management network is provider-owned in OCP; "
            "not exposed to tenants"
        ),
    },
    "bmc_tenant_isolation": {
        "subtests": [
            "probe_bmc_from_tenant",
            "probe_ipmi_port",
            "probe_redfish_port",
            "reverse_path_check",
        ],
        "top_level": {"bmc_endpoints_tested": 0},
        "message": (
            "BMC endpoints are not on the tenant network in OCP; "
            "isolation is architectural"
        ),
    },
    "bmc_protocol_security": {
        "subtests": [
            "ipmi_disabled",
            "redfish_tls_enabled",
            "redfish_plain_http_disabled",
            "redfish_authentication_required",
            "redfish_authorization_enforced",
            "redfish_accounting_enabled",
        ],
        "top_level": {"bmc_endpoints_tested": 0},
        "message": (
            "BMC protocol management is provider-owned in OCP; "
            "not exposed to tenants"
        ),
    },
    "bmc_bastion_access": {
        "subtests": [
            "bastion_identifiable",
            "management_ingress_via_bastion_only",
            "no_direct_public_route",
            "bastion_hardened",
        ],
        "top_level": {"management_networks_checked": 0},
        "message": (
            "BMC plane is provider-owned in OCP; "
            "no customer-visible BMC management network"
        ),
    },
}


def _provider_hidden(test_name: str, message: str) -> dict[str, Any]:
    return {
        "passed": True,
        "provider_hidden": True,
        "message": f"{test_name}: {message}",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--aspect", required=True, choices=sorted(ASPECT_SUBTESTS))
    args = parser.parse_args()

    spec = ASPECT_SUBTESTS[args.aspect]
    message = spec["message"]

    result: dict[str, Any] = {
        "success": True,
        "platform": "security",
        "test_name": args.aspect,
        **spec["top_level"],
        "tests": {
            name: _provider_hidden(name, message)
            for name in spec["subtests"]
        },
    }

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
