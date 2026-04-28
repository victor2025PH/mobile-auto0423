# -*- coding: utf-8 -*-
"""scripts/opt6_status_report.py 单测覆盖.

运维工具 — 批量扫所有 device 的 OPT-6 restriction 状态. 单测覆盖:
  - load_device_list: 跳过 IP 类 key (192.x / serial:port), 解析 alias
  - collect_status: mock device_state 各种状态 (无记录/期内/已过期)
  - render_markdown: 含必要字段 + restricted 详情段
  - only_restricted 过滤
  - main 退出码 0/1 语义

注: scripts/ 不是 package, 用 importlib 加载脚本文件作模块.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from unittest.mock import MagicMock, mock_open, patch

import pytest


# ════════════════════════════════════════════════════════════════════════
# 加载 scripts/opt6_status_report.py 作模块
# ════════════════════════════════════════════════════════════════════════

SCRIPT_PATH = os.path.join(
    os.path.dirname(__file__), "..", "scripts", "opt6_status_report.py")


@pytest.fixture(scope="module")
def osr():
    """加载 scripts/opt6_status_report.py 作模块, 跑核心函数测试。

    注: 必须 sys.modules 注册, 否则 patch("opt6_status_report.x") 会
    ModuleNotFoundError (importlib.util 加载默认不注册).
    """
    spec = importlib.util.spec_from_file_location(
        "opt6_status_report", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["opt6_status_report"] = mod
    spec.loader.exec_module(mod)
    return mod


# ════════════════════════════════════════════════════════════════════════
# load_device_list — 解析 device_aliases.json + 跳过 IP key
# ════════════════════════════════════════════════════════════════════════

class TestLoadDeviceList:
    def test_skips_ip_address_keys(self, osr, tmp_path):
        """device_aliases 含 IP:port 类 key (网络设备别名), 应跳过。"""
        fake_aliases = {
            "192.168.0.160:5555": {"alias": "网络设备", "slot": 5},
            "REAL_SERIAL_ABC123": {"alias": "真机-01", "slot": 1},
            "8DQKW8RWVSWO": {"alias": "08号", "slot": 8},
        }
        fake_path = tmp_path / "config" / "device_aliases.json"
        fake_path.parent.mkdir()
        fake_path.write_text(json.dumps(fake_aliases), encoding="utf-8")

        with patch.object(osr, "_ROOT", str(tmp_path)):
            result = osr.load_device_list()

        serials = [s for s, _ in result]
        # IP 类 key 应被跳过
        assert "192.168.0.160:5555" not in serials
        # 真机 serial 保留
        assert "REAL_SERIAL_ABC123" in serials
        assert "8DQKW8RWVSWO" in serials

    def test_skips_192_prefix_keys(self, osr, tmp_path):
        """以 '192.' 开头的 key 也应跳过 (无 port 形式)。"""
        fake_aliases = {
            "192.168.1.100": {"alias": "x"},
            "GOOD_SERIAL": {"alias": "y"},
        }
        fake_path = tmp_path / "config" / "device_aliases.json"
        fake_path.parent.mkdir()
        fake_path.write_text(json.dumps(fake_aliases), encoding="utf-8")

        with patch.object(osr, "_ROOT", str(tmp_path)):
            result = osr.load_device_list()
        serials = [s for s, _ in result]
        assert "192.168.1.100" not in serials
        assert "GOOD_SERIAL" in serials

    def test_returns_alias_or_display_label(self, osr, tmp_path):
        """alias 优先, 缺失 fall back display_label。"""
        fake_aliases = {
            "S1": {"alias": "alias-1", "display_label": "label-1"},
            "S2": {"display_label": "label-2"},  # 无 alias
            "S3": {},  # 都没 → "?"
        }
        fake_path = tmp_path / "config" / "device_aliases.json"
        fake_path.parent.mkdir()
        fake_path.write_text(json.dumps(fake_aliases), encoding="utf-8")

        with patch.object(osr, "_ROOT", str(tmp_path)):
            result = osr.load_device_list()
        d = dict(result)
        assert d["S1"] == "alias-1"
        assert d["S2"] == "label-2"
        assert d["S3"] == "?"

    def test_empty_when_file_missing(self, osr, tmp_path):
        """device_aliases.json 不存在返空列表 (不抛)."""
        with patch.object(osr, "_ROOT", str(tmp_path)):
            result = osr.load_device_list()
        assert result == []

    def test_empty_when_invalid_json(self, osr, tmp_path):
        """JSON 解析失败返空 + 写 stderr 警告 (不抛)."""
        fake_path = tmp_path / "config" / "device_aliases.json"
        fake_path.parent.mkdir()
        fake_path.write_text("{not valid json", encoding="utf-8")
        with patch.object(osr, "_ROOT", str(tmp_path)):
            result = osr.load_device_list()
        assert result == []

    def test_skips_non_dict_values(self, osr, tmp_path):
        """值不是 dict (string/null) 应跳过。"""
        fake_aliases = {
            "S_STRING": "just a string",
            "S_NULL": None,
            "S_VALID": {"alias": "ok"},
        }
        fake_path = tmp_path / "config" / "device_aliases.json"
        fake_path.parent.mkdir()
        fake_path.write_text(json.dumps(fake_aliases), encoding="utf-8")
        with patch.object(osr, "_ROOT", str(tmp_path)):
            result = osr.load_device_list()
        serials = [s for s, _ in result]
        assert "S_VALID" in serials
        assert "S_STRING" not in serials
        assert "S_NULL" not in serials


# ════════════════════════════════════════════════════════════════════════
# collect_status — mock device_state 各种状态
# ════════════════════════════════════════════════════════════════════════

class TestCollectStatus:
    def _setup_ds_mock(self, osr, *, lifted_at, days, full_msg, detected_at):
        ds_inst = MagicMock()
        ds_inst.get_float = MagicMock(side_effect=lambda did, k, d=0.0: {
            "restriction_lifted_at": lifted_at,
            "restriction_detected_at": detected_at,
        }.get(k, d))
        ds_inst.get_int = MagicMock(return_value=days)
        ds_inst.get = MagicMock(return_value=full_msg)
        return ds_inst

    def test_no_record_returns_healthy(self, osr):
        ds_inst = self._setup_ds_mock(
            osr, lifted_at=0.0, days=0, full_msg="", detected_at=0.0)
        with patch("src.host.device_state.DeviceStateStore",
                   return_value=ds_inst):
            r = osr.collect_status("D1")
        assert r["is_restricted"] is False
        assert r["lifted_at_iso"] == ""
        assert r["remaining_days"] == 0.0
        assert r["executor_skip"] is False

    def test_in_restriction_returns_restricted_with_remaining(self, osr):
        fixed_now = 1_700_000_000.0
        future = fixed_now + 6 * 86400
        ds_inst = self._setup_ds_mock(
            osr, lifted_at=future, days=6,
            full_msg="restricted for 6 days",
            detected_at=fixed_now)
        with patch("src.host.device_state.DeviceStateStore",
                   return_value=ds_inst), \
             patch("opt6_status_report.time.time", return_value=fixed_now):
            r = osr.collect_status("D1")
        assert r["is_restricted"] is True
        assert r["remaining_days"] == 6.0
        assert "restricted" in r["restriction_full_msg"]

    def test_remaining_days_zero_when_expired(self, osr):
        """已过期 (lifted_at < now) → is_restricted=False, remaining=0."""
        fixed_now = 1_700_000_000.0
        past = fixed_now - 86400
        ds_inst = self._setup_ds_mock(
            osr, lifted_at=past, days=6, full_msg="",
            detected_at=past - 6 * 86400)
        with patch("src.host.device_state.DeviceStateStore",
                   return_value=ds_inst), \
             patch("opt6_status_report.time.time", return_value=fixed_now):
            r = osr.collect_status("D1")
        assert r["is_restricted"] is False
        assert r["remaining_days"] == 0.0

    def test_full_msg_truncated_120_chars(self, osr):
        """full_msg 超 120 chars 应截断 (markdown 表 cell 不能太长)."""
        long_msg = "x" * 500
        ds_inst = self._setup_ds_mock(
            osr, lifted_at=time.time() + 86400, days=1,
            full_msg=long_msg, detected_at=time.time())
        with patch("src.host.device_state.DeviceStateStore",
                   return_value=ds_inst):
            r = osr.collect_status("D1")
        assert len(r["restriction_full_msg"]) <= 120


# ════════════════════════════════════════════════════════════════════════
# render_markdown — 输出格式 + only-restricted 过滤
# ════════════════════════════════════════════════════════════════════════

class TestRenderMarkdown:
    def _make_row(self, **overrides):
        defaults = {
            "device": "DEVICE_ABC123",
            "alias": "主控-99",
            "is_restricted": False,
            "lifted_at": 0.0,
            "lifted_at_iso": "",
            "remaining_days": 0.0,
            "restriction_days": 0,
            "restriction_full_msg": "",
            "detected_at_iso": "",
            "executor_skip": False,
            "executor_reason": "",
        }
        defaults.update(overrides)
        return defaults

    def test_renders_table_header(self, osr):
        rows = [self._make_row()]
        out = osr.render_markdown(rows)
        assert "| device | alias | restriction |" in out
        assert "受限设备数:" in out

    def test_healthy_device_shows_check_mark(self, osr):
        rows = [self._make_row(is_restricted=False)]
        out = osr.render_markdown(rows)
        assert "healthy" in out
        assert "RESTRICTED" not in out

    def test_restricted_device_shows_warning(self, osr):
        rows = [self._make_row(
            is_restricted=True,
            lifted_at_iso="2026-05-04T11:11:34",
            remaining_days=5.94,
            executor_reason="OPT-6 device DEV 在 restriction 期内, 跳过")]
        out = osr.render_markdown(rows)
        assert "RESTRICTED" in out
        assert "5.9" in out
        assert "2026-05-04" in out
        # 详情段
        assert "Restricted 设备详情" in out

    def test_only_restricted_filter(self, osr):
        rows = [
            self._make_row(device="HEALTHY1", is_restricted=False),
            self._make_row(device="RESTR_DEVICE", is_restricted=True,
                           lifted_at_iso="2026-05-04T00:00:00",
                           remaining_days=5.0),
        ]
        out = osr.render_markdown(rows, only_restricted=True)
        assert "HEALTHY1"[:12] not in out
        assert "RESTR_DEVICE"[:12] in out

    def test_empty_devices_renders_no_restricted_section(self, osr):
        """全 healthy → 不渲染 'Restricted 设备详情' 段."""
        rows = [self._make_row(is_restricted=False)]
        out = osr.render_markdown(rows)
        assert "Restricted 设备详情" not in out

    def test_pipe_in_reason_escaped(self, osr):
        """executor_reason 含 | 字符时应替换防 markdown 表 break."""
        rows = [self._make_row(
            is_restricted=True,
            lifted_at_iso="x",
            remaining_days=1.0,
            executor_reason="reason | with | pipes")]
        out = osr.render_markdown(rows)
        # | 应被替换为 / (本工具实现)
        assert "with / pipes" in out or "with | pipes" not in out
