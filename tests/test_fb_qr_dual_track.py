# -*- coding: utf-8 -*-
"""D1-B `_send_line_qr_after_text` 双轨集成单测 — 触发条件 + 降级路径。

集成接入点 = _ai_reply_and_send 文字发完后调用。本测只针对 helper 自身
逻辑 (4 个触发条件 × 4 个降级分支), 不测 _ai_reply_and_send 整条 600 行
路径。helper 失败时永远不应让上层的 wa_referral decision 退化。
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


def _make_fb():
    from src.app_automation.facebook import FacebookAutomation
    fb = FacebookAutomation.__new__(FacebookAutomation)
    fb.hb = MagicMock()
    fb.dm = MagicMock()
    return fb


@pytest.fixture
def fb():
    f = _make_fb()
    # attach_image 默认返 True (集成路径成功)
    f.attach_image = MagicMock(return_value=True)
    return f


# ════════════════════════════════════════════════════════════════════════
# 触发条件: decision / channel / line_id 三者必须全满足
# ════════════════════════════════════════════════════════════════════════

class TestTriggerGuard:
    """不满足触发条件时不调 attach_image, 也不调 build_line_qr。"""

    def test_skip_when_decision_is_reply(self, fb):
        with patch("src.utils.qr_generator.build_line_qr") as mqr:
            ok = fb._send_line_qr_after_text(
                "reply", "line", "abc", "D1", "peer1")
        assert ok is False
        fb.attach_image.assert_not_called()
        mqr.assert_not_called()

    def test_skip_when_decision_is_skip(self, fb):
        with patch("src.utils.qr_generator.build_line_qr") as mqr:
            ok = fb._send_line_qr_after_text(
                "skip", "line", "abc", "D1", "peer1")
        assert ok is False
        fb.attach_image.assert_not_called()
        mqr.assert_not_called()

    def test_skip_when_channel_is_whatsapp(self, fb):
        with patch("src.utils.qr_generator.build_line_qr") as mqr:
            ok = fb._send_line_qr_after_text(
                "wa_referral", "whatsapp", "+886912345678", "D1", "peer1")
        assert ok is False
        fb.attach_image.assert_not_called()
        mqr.assert_not_called()

    def test_skip_when_channel_is_telegram(self, fb):
        with patch("src.utils.qr_generator.build_line_qr") as mqr:
            ok = fb._send_line_qr_after_text(
                "wa_referral", "telegram", "@user", "D1", "peer1")
        assert ok is False
        fb.attach_image.assert_not_called()
        mqr.assert_not_called()

    def test_skip_when_line_id_empty(self, fb):
        with patch("src.utils.qr_generator.build_line_qr") as mqr:
            ok = fb._send_line_qr_after_text(
                "wa_referral", "line", "", "D1", "peer1")
        assert ok is False
        fb.attach_image.assert_not_called()
        mqr.assert_not_called()

    def test_skip_when_line_id_whitespace_only(self, fb):
        with patch("src.utils.qr_generator.build_line_qr") as mqr:
            ok = fb._send_line_qr_after_text(
                "wa_referral", "line", "   \t\n", "D1", "peer1")
        assert ok is False
        fb.attach_image.assert_not_called()
        mqr.assert_not_called()

    def test_channel_case_insensitive(self, fb):
        """大写 LINE / 混合 Line 也应触发。"""
        with patch("src.utils.qr_generator.build_line_qr",
                   return_value="/tmp/x.png") as mqr, \
             patch("src.app_automation.facebook.time.sleep"), \
             patch("src.app_automation.facebook.random.uniform",
                   return_value=0.0):
            ok = fb._send_line_qr_after_text(
                "wa_referral", "LINE", "abc", "D1", "peer1")
        assert ok is True
        mqr.assert_called_once()


# ════════════════════════════════════════════════════════════════════════
# build_line_qr 路径
# ════════════════════════════════════════════════════════════════════════

class TestQrGeneration:
    def test_build_line_qr_returns_none_skips_attach(self, fb):
        with patch("src.utils.qr_generator.build_line_qr",
                   return_value=None):
            ok = fb._send_line_qr_after_text(
                "wa_referral", "line", "abc", "D1", "peer1")
        assert ok is False
        fb.attach_image.assert_not_called()

    def test_build_line_qr_path_passed_to_attach(self, fb):
        with patch("src.utils.qr_generator.build_line_qr",
                   return_value="/tmp/qr_xyz.png"), \
             patch("src.app_automation.facebook.time.sleep"), \
             patch("src.app_automation.facebook.random.uniform",
                   return_value=0.0):
            fb._send_line_qr_after_text(
                "wa_referral", "line", "abc", "D1", "peer1")
        fb.attach_image.assert_called_once()
        args, kwargs = fb.attach_image.call_args
        assert "/tmp/qr_xyz.png" in args
        assert kwargs.get("raise_on_error") is False

    def test_build_line_qr_exception_swallowed(self, fb):
        with patch("src.utils.qr_generator.build_line_qr",
                   side_effect=RuntimeError("disk full")):
            # 不抛, decision 不退化
            ok = fb._send_line_qr_after_text(
                "wa_referral", "line", "abc", "D1", "peer1")
        assert ok is False
        fb.attach_image.assert_not_called()


# ════════════════════════════════════════════════════════════════════════
# attach_image 失败降级
# ════════════════════════════════════════════════════════════════════════

class TestAttachImageDowngrade:
    def test_attach_failure_returns_false_does_not_raise(self, fb):
        fb.attach_image = MagicMock(return_value=False)
        with patch("src.utils.qr_generator.build_line_qr",
                   return_value="/tmp/x.png"), \
             patch("src.app_automation.facebook.time.sleep"), \
             patch("src.app_automation.facebook.random.uniform",
                   return_value=0.0):
            ok = fb._send_line_qr_after_text(
                "wa_referral", "line", "abc", "D1", "peer1")
        assert ok is False  # 文字已发, 但 QR 没附上

    def test_attach_image_exception_swallowed_by_outer_try(self, fb):
        fb.attach_image = MagicMock(side_effect=RuntimeError("u2 dead"))
        with patch("src.utils.qr_generator.build_line_qr",
                   return_value="/tmp/x.png"), \
             patch("src.app_automation.facebook.time.sleep"), \
             patch("src.app_automation.facebook.random.uniform",
                   return_value=0.0):
            ok = fb._send_line_qr_after_text(
                "wa_referral", "line", "abc", "D1", "peer1")
        assert ok is False


# ════════════════════════════════════════════════════════════════════════
# 反 spam — 文字与图之间随机延迟
# ════════════════════════════════════════════════════════════════════════

class TestAntiSpamDelay:
    def test_random_uniform_called_within_human_range(self, fb):
        """文字和 QR 之间应延迟 1.0~2.5s 模拟人类阅读+附件操作时间。"""
        with patch("src.utils.qr_generator.build_line_qr",
                   return_value="/tmp/x.png"), \
             patch("src.app_automation.facebook.time.sleep") as msleep, \
             patch("src.app_automation.facebook.random.uniform",
                   return_value=1.7) as muni:
            fb._send_line_qr_after_text(
                "wa_referral", "line", "abc", "D1", "peer1")
        muni.assert_called_once_with(1.0, 2.5)
        # 至少一次 sleep, 用 uniform 的返回值 1.7
        assert any(c.args[0] == 1.7 for c in msleep.call_args_list), \
            "expected sleep(1.7) but got: " + str(msleep.call_args_list)


# ════════════════════════════════════════════════════════════════════════
# 成功路径
# ════════════════════════════════════════════════════════════════════════

class TestSuccessPath:
    def test_full_success_returns_true(self, fb):
        with patch("src.utils.qr_generator.build_line_qr",
                   return_value="/tmp/x.png"), \
             patch("src.app_automation.facebook.time.sleep"), \
             patch("src.app_automation.facebook.random.uniform",
                   return_value=0.0):
            ok = fb._send_line_qr_after_text(
                "wa_referral", "line", "abc", "D1", "peer1")
        assert ok is True
        fb.attach_image.assert_called_once()

    def test_attach_image_passed_correct_device_id(self, fb):
        with patch("src.utils.qr_generator.build_line_qr",
                   return_value="/tmp/x.png"), \
             patch("src.app_automation.facebook.time.sleep"), \
             patch("src.app_automation.facebook.random.uniform",
                   return_value=0.0):
            fb._send_line_qr_after_text(
                "wa_referral", "line", "abc", "DEVICE-Z9", "peer1")
        kwargs = fb.attach_image.call_args.kwargs
        assert kwargs.get("device_id") == "DEVICE-Z9"
