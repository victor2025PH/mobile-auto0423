# -*- coding: utf-8 -*-
"""OPT-FP6 (2026-04-28) — _verify_messenger_in_foreground 防 Q4N7 类
activity 跑偏 false positive.

Q4N7 真机实测: send_message 返 True 但截图在 katana FB 主 app 备份页
MibCloudBackupNuxActivity, OPT-FP3 dump-based verify 被 substring 误放行.
OPT-FP6 在 _send_message_impl 最末尾 dumpsys window 强制检查 orca 在前台,
catch 这种"消息没真发但走完链路返 True"的最后一道防线.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def _make_fb():
    from src.app_automation.facebook import FacebookAutomation
    fb = FacebookAutomation.__new__(FacebookAutomation)
    fb.dm = MagicMock()
    return fb


# ════════════════════════════════════════════════════════════════════════
# _verify_messenger_in_foreground 边界
# ════════════════════════════════════════════════════════════════════════

class TestVerifyMessengerInForeground:

    def test_orca_in_foreground_returns_true(self):
        """dumpsys 含 com.facebook.orca → 验证通过."""
        fb = _make_fb()
        fb.dm._run_adb = MagicMock(return_value=(
            True,
            "mCurrentFocus=Window{x u0 com.facebook.orca/MainActivity}"))
        assert fb._verify_messenger_in_foreground("D1") is True

    def test_orca_not_in_foreground_returns_false_after_retry(self):
        """dumpsys 不含 orca + 2 次 retry 都 miss → False (Q4N7 类场景)."""
        fb = _make_fb()
        # 模拟 Q4N7: 在 katana 备份页
        katana_dump = (
            "mCurrentFocus=Window{x u0 "
            "com.facebook.katana/com.facebook.messaginginblue.e2ee."
            "cloudbackup.ui.activities.onboardingnux.MibCloudBackupNuxActivity}")
        fb.dm._run_adb = MagicMock(return_value=(True, katana_dump))
        # patch sleep 避免真等
        with __import__("unittest.mock").mock.patch(
                "src.app_automation.facebook.time.sleep"):
            assert fb._verify_messenger_in_foreground("D1") is False
        # 应调 dumpsys 2 次 (retry)
        assert fb.dm._run_adb.call_count == 2

    def test_first_attempt_miss_second_hit_returns_true(self):
        """第 1 次 miss, 第 2 次 hit (UI 切换中途) → True."""
        fb = _make_fb()
        outputs = [
            (True, "mCurrentFocus=Window{x u0 com.miui.home/Launcher}"),
            (True, "mCurrentFocus=Window{x u0 com.facebook.orca/Main}"),
        ]
        fb.dm._run_adb = MagicMock(side_effect=outputs)
        with __import__("unittest.mock").mock.patch(
                "src.app_automation.facebook.time.sleep"):
            assert fb._verify_messenger_in_foreground("D1") is True

    def test_dumpsys_call_failed_failsafe_returns_true(self):
        """dumpsys 调用失败 (ok=False) → fail-safe True (让 OPT-FP3 当主防线)."""
        fb = _make_fb()
        fb.dm._run_adb = MagicMock(return_value=(False, ""))
        assert fb._verify_messenger_in_foreground("D1") is True

    def test_dumpsys_raises_failsafe_returns_true(self):
        """dumpsys 抛异常 → fail-safe True."""
        fb = _make_fb()
        fb.dm._run_adb = MagicMock(side_effect=RuntimeError("adb dead"))
        assert fb._verify_messenger_in_foreground("D1") is True

    def test_logs_warning_with_activity_snippet_on_miss(self):
        """2 次 miss 时应 log.warning 含当前 activity snippet (Q4N7 实测
        helpful for debugging)."""
        fb = _make_fb()
        katana_focus = (
            "  mCurrentFocus=Window{x u0 com.facebook.katana/"
            "MibCloudBackupNuxActivity}\n"
            "  mFocusedApp=ActivityRecord{x u0 com.facebook.katana/...}")
        fb.dm._run_adb = MagicMock(return_value=(True, katana_focus))
        with __import__("unittest.mock").mock.patch(
                "src.app_automation.facebook.time.sleep"):
            with __import__("unittest.mock").mock.patch(
                    "src.app_automation.facebook.log") as mock_log:
                result = fb._verify_messenger_in_foreground("D1")
        assert result is False
        # 应 log.warning 含 katana / activity snippet
        warning_calls = [
            c for c in mock_log.warning.call_args_list
            if "[opt-fp6]" in str(c)]
        assert len(warning_calls) >= 1


# ════════════════════════════════════════════════════════════════════════
# 集成 — _send_message_impl 末尾应调 _verify_messenger_in_foreground
# ════════════════════════════════════════════════════════════════════════

class TestIntegration:
    def test_send_message_impl_calls_fp6_at_end(self):
        """source-level: _send_message_impl 在 OPT-FP3 verify 之后,
        return True 之前应调 _verify_messenger_in_foreground."""
        from src.app_automation.facebook import FacebookAutomation
        import inspect
        src = inspect.getsource(FacebookAutomation._send_message_impl)
        assert "_verify_messenger_in_foreground" in src, (
            "OPT-FP6 没集成到 _send_message_impl")
        idx_fp3 = src.find("_verify_message_actually_sent")
        idx_fp6 = src.find("_verify_messenger_in_foreground")
        assert idx_fp6 > idx_fp3, "OPT-FP6 应在 OPT-FP3 之后"

    def test_fp6_failure_raises_send_button_missing_with_hint(self):
        """source-level: FP6 失败应抛 send_button_missing (复用既有 code)
        + hint=post_send_activity_check_failed."""
        from src.app_automation.facebook import FacebookAutomation
        import inspect
        src = inspect.getsource(FacebookAutomation._send_message_impl)
        idx_fp6 = src.find("_verify_messenger_in_foreground")
        nearby = src[idx_fp6:idx_fp6 + 800]
        assert "send_button_missing" in nearby, (
            "FP6 失败应复用 send_button_missing code")
        assert "post_send_activity_check_failed" in nearby, (
            "FP6 hint 应含 post_send_activity_check_failed 让调用方区分")
