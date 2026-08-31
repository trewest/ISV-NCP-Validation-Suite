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

"""Echo node pool info without creating resources.

Used as the ``create_test_delete_node_pool`` step so the delete check
can reference the label_selector of an already-existing pool.
"""

from __future__ import annotations

import json
import os
import sys

TRACKING_LABEL_KEY = "isv.ncp.validation/node-pool"


def main() -> int:
    pool_name = os.environ.get("OCP_NODE_POOL_NAME", "isv-test-pool")
    node_type = os.environ.get("OCP_NODE_POOL_NODE_TYPE", "cpu")

    result = {
        "success": True,
        "platform": "kubernetes",
        "label_selector": f"{TRACKING_LABEL_KEY}={pool_name}",
        "node_type": node_type,
    }
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
