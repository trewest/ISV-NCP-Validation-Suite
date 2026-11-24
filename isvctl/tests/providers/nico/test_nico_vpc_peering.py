# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not use
# this file except in compliance with the License. You may obtain a copy of the
# License at
#
# http://www.apache.org/licenses/LICENSE-2.0

"""Tests for the guarded NICo VPC-peering probe."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from urllib.error import HTTPError

import pytest

NICO_SCRIPTS = Path(__file__).resolve().parents[3] / "configs" / "providers" / "nico" / "scripts"


def _load_script() -> ModuleType:
    """Load the peering script as a module."""
    path = NICO_SCRIPTS / "network" / "query_vpc_peering.py"
    spec = importlib.util.spec_from_file_location("test_query_vpc_peering", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["test_query_vpc_peering"] = module
    spec.loader.exec_module(module)
    return module


def test_vpc_peering_probe_is_safe_without_explicit_guard(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A plain invocation must not create any VPC or peering."""
    module = _load_script()
    monkeypatch.delenv("NICO_ALLOW_VPC_PEERING_MUTATION", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        ["query_vpc_peering.py", "--org", "o", "--site-id", "s", "--api-base", "https://nico.example/v2/org"],
    )

    assert module.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["success"] is True
    assert payload["skipped"] is True
    assert all(entry.get("skipped") is True for entry in payload["tests"].values())


def test_vpc_peering_probe_structured_skips_when_site_disables_peering(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A site policy gap skips cleanly after cleaning any temporary VPCs."""
    module = _load_script()
    monkeypatch.setenv("NICO_ALLOW_VPC_PEERING_MUTATION", "1")
    monkeypatch.setenv("NICO_TEST_PEERING_TIMEOUT", "1")
    monkeypatch.setattr(module, "resolve_auth", lambda: type("Auth", (), {"token": "test-token"})())

    created = iter([{"id": "vpc-a"}, {"id": "vpc-b"}])

    def _post(_org: str, path: str, _token: str, **_kwargs: object) -> dict[str, str]:
        if path == "vpc":
            return next(created)
        raise HTTPError(
            "https://nico.example/vpc-peering",
            500,
            "VPC peering is disabled at this site",
            {},
            None,
        )

    monkeypatch.setattr(module, "forge_post", _post)
    monkeypatch.setattr(module, "forge_get", lambda *_args, **_kwargs: {"status": "Ready"})
    monkeypatch.setattr(module, "forge_delete", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(module, "_wait_deleted", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        sys,
        "argv",
        ["query_vpc_peering.py", "--org", "o", "--site-id", "s", "--api-base", "https://nico.example/v2/org"],
    )

    assert module.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["success"] is True
    assert payload["skipped"] is True
    assert "peering is disabled" in payload["skip_reason"]
    assert payload["cleanup_errors"] == []
    assert payload["tests"]["create_peering"]["skipped"] is True
