#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not use
# this file except in compliance with the License. You may obtain a copy of the
# License at
#
# http://www.apache.org/licenses/LICENSE-2.0

"""Report the current NICo limitation for BMC kernel-log history."""

from __future__ import annotations

from common import skipped

if __name__ == "__main__":
    raise SystemExit(
        skipped(
            "query_bmc_kernel_logs",
            "NICo admin APIs do not expose BMC kernel-log history in this deployment",
        )
    )
