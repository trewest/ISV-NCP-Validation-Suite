# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not use
# this file except in compliance with the License. You may obtain a copy of the
# License at
#
# http://www.apache.org/licenses/LICENSE-2.0

"""Tests for the runtime-driven disposable NICo instance helpers."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

NICO_SCRIPTS = Path(__file__).resolve().parents[3] / "configs" / "providers" / "nico" / "scripts"


def _load_script(relative_path: str, module_name: str) -> ModuleType:
    """Load a provider script as a module."""
    path = NICO_SCRIPTS / relative_path
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_create_instance_request_uses_runtime_target_and_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """The create payload contains only runtime-provided IDs."""
    module = _load_script("instance/create_disposable_instance.py", "test_create_disposable_instance")
    monkeypatch.setenv("NICO_TEST_TENANT_ID", "tenant-1")
    monkeypatch.setenv("NICO_TEST_VPC_ID", "vpc-1")
    monkeypatch.setenv("NICO_TEST_MACHINE_ID", "machine-1")
    monkeypatch.setenv("NICO_TEST_INSTANCE_NAME", "disposable-1")
    monkeypatch.setenv("NICO_TEST_VPC_PREFIX_ID", "prefix-1")

    request = module.build_instance_request()

    assert request == {
        "name": "disposable-1",
        "tenantId": "tenant-1",
        "vpcId": "vpc-1",
        "machineId": "machine-1",
        "interfaces": [{"vpcPrefixId": "prefix-1", "isPhysical": True}],
    }


def test_create_instance_request_supports_flat_auto_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Flat/auto networking does not require a hard-coded interface ID."""
    module = _load_script("instance/create_disposable_instance.py", "test_create_disposable_instance_auto")
    monkeypatch.setenv("NICO_TEST_TENANT_ID", "tenant-1")
    monkeypatch.setenv("NICO_TEST_VPC_ID", "vpc-1")
    monkeypatch.setenv("NICO_TEST_INSTANCE_TYPE_ID", "type-1")
    monkeypatch.setenv("NICO_TEST_AUTO_NETWORK", "1")

    request = module.build_instance_request()

    assert request["instanceTypeId"] == "type-1"
    assert request["autoNetwork"] is True
    assert "interfaces" not in request


def test_cleanup_without_created_instance_is_safe(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Teardown with no setup output must be a successful no-op."""
    module = _load_script("instance/cleanup_disposable_instance.py", "test_cleanup_disposable_instance")
    monkeypatch.setattr(
        sys, "argv", ["cleanup_disposable_instance.py", "--org", "o", "--site-id", "s", "--api-base", "b"]
    )

    assert module.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["success"] is True
    assert payload["skipped"] is True
