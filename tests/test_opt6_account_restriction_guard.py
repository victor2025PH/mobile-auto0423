# -*- coding: utf-8 -*-
"""OPT-6 (2026-04-28) — account restriction 调度器/AI guard 单测.

OPT-4 已落地 "识别 + 记 risk event + log days". OPT-6 在此基础上:
  - facebook.py::_mark_account_restricted_state(did, full_msg, days):
    把 N 天 restriction 写入 device_state.platform='facebook' 4 个 key
  - facebook.py::_is_account_restricted(did): 纯读 device_state 判断
  - facebook.py::_detect_risk_dialog OPT-4 路径调 mark
  - facebook.py::_ai_reply_and_send 入口调 _is_account_restricted guard
  - executor.py::_opt6_check_restriction(did): 调度器侧拦截
  - executor.py::_execute_with_retry: BLOCKED_TASK_TYPES 集成

覆盖:
  - mark 边界 (days <= 0 / 空 did) + 正常写 4 key
  - lifted_at 计算 (now + days*86400)
  - _is_account_restricted: 期内 / 已过期 / 无记录 / 异常 fail-open
  - executor._opt6_check_restriction 同上 + reason 文案
  - executor._execute_with_retry: blocked task_type 跳过 / 非 blocked 不影响
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest


# ════════════════════════════════════════════════════════════════════════
# _mark_account_restricted_state — 写入 device_state 4 个 key
# ════════════════════════════════════════════════════════════════════════

class TestMarkRestrictedState:
    def _make_fb(self):
        from src.app_automation.facebook import FacebookAutomation
        return FacebookAutomation.__new__(FacebookAutomation)

    def test_zero_days_is_noop(self):
        fb = self._make_fb()
        with patch("src.host.device_state.DeviceStateStore") as ds_cls:
            fb._mark_account_restricted_state("D1", "msg", 0)
        ds_cls.assert_not_called()

    def test_negative_days_is_noop(self):
        fb = self._make_fb()
        with patch("src.host.device_state.DeviceStateStore") as ds_cls:
            fb._mark_account_restricted_state("D1", "msg", -1)
        ds_cls.assert_not_called()

    def test_empty_did_is_noop(self):
        fb = self._make_fb()
        with patch("src.host.device_state.DeviceStateStore") as ds_cls:
            fb._mark_account_restricted_state("", "msg", 6)
        ds_cls.assert_not_called()

    def test_writes_four_keys(self):
        fb = self._make_fb()
        ds_inst = MagicMock()
        with patch("src.host.device_state.DeviceStateStore",
                   return_value=ds_inst) as ds_cls, \
             patch("src.app_automation.facebook.time.time",
                   return_value=1_700_000_000.0):
            fb._mark_account_restricted_state(
                "DEVICE-Z9",
                "Your account has been restricted for 6 days",
                6)
        # platform='facebook'
        ds_cls.assert_called_once_with(platform="facebook")
        # 4 个 key 都写
        keys_written = {c.args[1] for c in ds_inst.set.call_args_list}
        assert keys_written == {
            "restriction_lifted_at",
            "restriction_full_msg",
            "restriction_days",
            "restriction_detected_at",
        }

    def test_lifted_at_is_now_plus_days(self):
        fb = self._make_fb()
        ds_inst = MagicMock()
        fixed_now = 1_700_000_000.0
        with patch("src.host.device_state.DeviceStateStore",
                   return_value=ds_inst), \
             patch("src.app_automation.facebook.time.time",
                   return_value=fixed_now):
            fb._mark_account_restricted_state("D1", "msg", 6)
        # 找 restriction_lifted_at 的 set 调用
        for c in ds_inst.set.call_args_list:
            if c.args[1] == "restriction_lifted_at":
                lifted_at = float(c.args[2])
                assert lifted_at == fixed_now + 6 * 86400
                return
        pytest.fail("restriction_lifted_at not set")

    def test_full_msg_truncated_to_500(self):
        fb = self._make_fb()
        ds_inst = MagicMock()
        big_msg = "x" * 1000
        with patch("src.host.device_state.DeviceStateStore",
                   return_value=ds_inst):
            fb._mark_account_restricted_state("D1", big_msg, 1)
        for c in ds_inst.set.call_args_list:
            if c.args[1] == "restriction_full_msg":
                assert len(c.args[2]) == 500
                return
        pytest.fail("restriction_full_msg not set")

    def test_db_exception_swallowed(self):
        fb = self._make_fb()
        with patch("src.host.device_state.DeviceStateStore",
                   side_effect=RuntimeError("db locked")):
            # 不抛
            fb._mark_account_restricted_state("D1", "msg", 6)


# ════════════════════════════════════════════════════════════════════════
# _is_account_restricted — 纯读, fail-open
# ════════════════════════════════════════════════════════════════════════

class TestIsAccountRestricted:
    def _make_fb(self):
        from src.app_automation.facebook import FacebookAutomation
        return FacebookAutomation.__new__(FacebookAutomation)

    def test_empty_did_returns_not_restricted(self):
        fb = self._make_fb()
        is_r, lifted = fb._is_account_restricted("")
        assert is_r is False
        assert lifted == 0.0

    def test_no_record_returns_not_restricted(self):
        fb = self._make_fb()
        ds_inst = MagicMock()
        ds_inst.get_float = MagicMock(return_value=0.0)
        with patch("src.host.device_state.DeviceStateStore",
                   return_value=ds_inst):
            is_r, lifted = fb._is_account_restricted("D1")
        assert is_r is False
        assert lifted == 0.0

    def test_lifted_in_future_returns_restricted(self):
        fb = self._make_fb()
        ds_inst = MagicMock()
        fixed_now = 1_700_000_000.0
        future = fixed_now + 6 * 86400
        ds_inst.get_float = MagicMock(return_value=future)
        with patch("src.host.device_state.DeviceStateStore",
                   return_value=ds_inst), \
             patch("src.app_automation.facebook.time.time",
                   return_value=fixed_now):
            is_r, lifted = fb._is_account_restricted("D1")
        assert is_r is True
        assert lifted == future

    def test_lifted_in_past_returns_not_restricted(self):
        """已过解封时间 → 不再 restriction (TTL 自然失效)。"""
        fb = self._make_fb()
        ds_inst = MagicMock()
        fixed_now = 1_700_000_000.0
        past = fixed_now - 86400  # 1 天前已解封
        ds_inst.get_float = MagicMock(return_value=past)
        with patch("src.host.device_state.DeviceStateStore",
                   return_value=ds_inst), \
             patch("src.app_automation.facebook.time.time",
                   return_value=fixed_now):
            is_r, lifted = fb._is_account_restricted("D1")
        assert is_r is False
        assert lifted == past

    def test_db_exception_fail_open(self):
        """device_state 读异常 → fail-open (False, 0.0), 不阻断生产。"""
        fb = self._make_fb()
        with patch("src.host.device_state.DeviceStateStore",
                   side_effect=RuntimeError("db dead")):
            is_r, lifted = fb._is_account_restricted("D1")
        assert is_r is False
        assert lifted == 0.0


# ════════════════════════════════════════════════════════════════════════
# executor._opt6_check_restriction — 调度器侧拦截
# ════════════════════════════════════════════════════════════════════════

class TestExecutorCheckRestriction:
    def test_empty_device_returns_not_skip(self):
        from src.host.executor import _opt6_check_restriction
        skip, reason = _opt6_check_restriction("")
        assert skip is False
        assert reason == ""

    def test_no_record_returns_not_skip(self):
        from src.host.executor import _opt6_check_restriction
        ds_inst = MagicMock()
        ds_inst.get_float = MagicMock(return_value=0.0)
        with patch("src.host.device_state.DeviceStateStore",
                   return_value=ds_inst):
            skip, reason = _opt6_check_restriction("D1")
        assert skip is False
        assert reason == ""

    def test_lifted_in_future_returns_skip_with_reason(self):
        from src.host.executor import _opt6_check_restriction
        ds_inst = MagicMock()
        fixed_now = 1_700_000_000.0
        future = fixed_now + 6 * 86400
        ds_inst.get_float = MagicMock(return_value=future)
        ds_inst.get_int = MagicMock(return_value=6)
        with patch("src.host.device_state.DeviceStateStore",
                   return_value=ds_inst), \
             patch("src.host.executor.time.time", return_value=fixed_now):
            skip, reason = _opt6_check_restriction("DEVICE-Z9")
        assert skip is True
        assert "restriction" in reason.lower()
        assert "DEVICE-Z9"[:12] in reason
        assert "6.0 天" in reason  # remaining_d float format

    def test_lifted_in_past_returns_not_skip(self):
        from src.host.executor import _opt6_check_restriction
        ds_inst = MagicMock()
        fixed_now = 1_700_000_000.0
        past = fixed_now - 86400
        ds_inst.get_float = MagicMock(return_value=past)
        with patch("src.host.device_state.DeviceStateStore",
                   return_value=ds_inst), \
             patch("src.host.executor.time.time", return_value=fixed_now):
            skip, reason = _opt6_check_restriction("D1")
        assert skip is False

    def test_db_exception_fail_open(self):
        from src.host.executor import _opt6_check_restriction
        with patch("src.host.device_state.DeviceStateStore",
                   side_effect=RuntimeError("db dead")):
            skip, reason = _opt6_check_restriction("D1")
        assert skip is False


# ════════════════════════════════════════════════════════════════════════
# BLOCKED_TASK_TYPES 集合契约 — 主动行为类必须含, 被动接收类不应含
# ════════════════════════════════════════════════════════════════════════

class TestBlockedTaskTypes:
    def test_send_message_blocked(self):
        from src.host.executor import _OPT6_BLOCKED_TASK_TYPES
        assert "facebook_send_message" in _OPT6_BLOCKED_TASK_TYPES

    def test_send_greeting_blocked(self):
        from src.host.executor import _OPT6_BLOCKED_TASK_TYPES
        assert "facebook_send_greeting" in _OPT6_BLOCKED_TASK_TYPES

    def test_add_friend_blocked(self):
        from src.host.executor import _OPT6_BLOCKED_TASK_TYPES
        assert "facebook_add_friend" in _OPT6_BLOCKED_TASK_TYPES

    def test_add_friend_and_greet_blocked(self):
        from src.host.executor import _OPT6_BLOCKED_TASK_TYPES
        assert "facebook_add_friend_and_greet" in _OPT6_BLOCKED_TASK_TYPES

    def test_check_inbox_NOT_blocked(self):
        """check_inbox 是被动接收, restriction 期仍可跑 (只读 inbox)。
        若误进 BLOCKED_TASK_TYPES, 6 天 inbox 不读会丢漏对方回复。"""
        from src.host.executor import _OPT6_BLOCKED_TASK_TYPES
        assert "facebook_check_inbox" not in _OPT6_BLOCKED_TASK_TYPES

    def test_browse_feed_NOT_blocked(self):
        from src.host.executor import _OPT6_BLOCKED_TASK_TYPES
        assert "facebook_browse_feed" not in _OPT6_BLOCKED_TASK_TYPES

    def test_check_message_requests_NOT_blocked(self):
        from src.host.executor import _OPT6_BLOCKED_TASK_TYPES
        assert "facebook_check_message_requests" not in _OPT6_BLOCKED_TASK_TYPES
