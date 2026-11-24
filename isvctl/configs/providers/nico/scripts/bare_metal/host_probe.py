#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not use
# this file except in compliance with the License. You may obtain a copy of the
# License at
#
# http://www.apache.org/licenses/LICENSE-2.0

"""Expose the NVL72 host SSH connection for provider-agnostic validations."""

from __future__ import annotations

import os

from common import NicoCommandError, emit, env_value, ssh_command


def main() -> int:
    """Verify the configured host endpoint and emit SSH connection metadata."""
    result = {
        "success": False,
        "platform": "nico",
        "test_name": "nvl72_host_probe",
        "machine_id": os.environ.get("NICO_MACHINE_ID", ""),
    }
    try:
        host = env_value("NVL72_HOST")
        user = os.environ.get("NVL72_SSH_USER", "ubuntu")
        key_file = env_value("NVL72_SSH_KEY_FILE")
        code, stdout, stderr = ssh_command(
            "printf '%s\\n' host-ready",
            host=host,
            user=user,
            key_file=key_file,
        )
        if code != 0 or stdout.strip() != "host-ready":
            raise NicoCommandError(stderr or "host SSH probe failed")

        result.update(
            {
                "success": True,
                "host": host,
                "public_ip": host,
                "private_ip": host,
                "ssh_user": user,
                "key_file": key_file,
            }
        )
    except NicoCommandError as exc:
        result["error"] = str(exc)
        result["error_type"] = type(exc).__name__

    return emit(result)


if __name__ == "__main__":
    raise SystemExit(main())
