# -*- coding: utf-8 -*-
"""§13.2 device_assignment.yaml 静态分配 loader 单测."""
from __future__ import annotations

from pathlib import Path

import pytest


def _write_yaml(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")


def test_assigned_devices_reads_repo_key(tmp_path, monkeypatch):
    yaml_path = tmp_path / "device_assignment.yaml"
    _write_yaml(yaml_path, "a_repo:\n  - AAA\n  - BBB\nb_repo:\n  - CCC\n")
    monkeypatch.setenv("DEVICE_ASSIGNMENT_PATH", str(yaml_path))

    import importlib

    import src.host.fb_concurrency as mod
    importlib.reload(mod)

    assert mod.assigned_devices("a_repo") == ["AAA", "BBB"]
    assert mod.assigned_devices("b_repo") == ["CCC"]
    assert mod.assigned_devices("missing_key") == []


def test_assigned_devices_missing_file(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "DEVICE_ASSIGNMENT_PATH", str(tmp_path / "does_not_exist.yaml")
    )
    import importlib

    import src.host.fb_concurrency as mod
    importlib.reload(mod)

    assert mod.assigned_devices("a_repo") == []


def test_assigned_devices_corrupt_yaml(tmp_path, monkeypatch):
    yaml_path = tmp_path / "device_assignment.yaml"
    _write_yaml(yaml_path, "{ unmatched: [bracket\n")
    monkeypatch.setenv("DEVICE_ASSIGNMENT_PATH", str(yaml_path))

    import importlib

    import src.host.fb_concurrency as mod
    importlib.reload(mod)

    assert mod.assigned_devices("a_repo") == []
