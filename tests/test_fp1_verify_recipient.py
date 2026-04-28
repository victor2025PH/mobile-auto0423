# -*- coding: utf-8 -*-
"""OPT-FP1 (2026-04-28) — _verify_recipient_in_conv_title + send_message
集成防 false positive.

发现真机 IJ8H 实测 send_message 返 True 但实际进了错对话页:
搜索 "Meta AI" → tap 第 1 条 → 进 "Shuichi Ito" (自己) 对话页 → 整条
send 链路在错的人对话页跑完, Send 按钮真被点了 → 函数返 True (false
positive).

修复: tap 第一结果后调 _verify_recipient_in_conv_title, dump 对话页
XML 验证含 recipient name (substring match). 不匹配抛
MessengerError(recipient_not_found, hint=verify_failed).

fail-safe: dump 异常时返 True 放行, 不阻断主流程.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def _make_fb():
    from src.app_automation.facebook import FacebookAutomation
    return FacebookAutomation.__new__(FacebookAutomation)


def _mock_d_with_xml(xml: str):
    d = MagicMock()
    d.dump_hierarchy = MagicMock(return_value=xml)
    return d


# ════════════════════════════════════════════════════════════════════════
# _verify_recipient_in_conv_title 边界
# ════════════════════════════════════════════════════════════════════════

class TestVerifyRecipientInConvTitle:

    def test_recipient_present_returns_true(self):
        """对话页 dump 含 recipient name → 验证通过."""
        fb = _make_fb()
        xml = '<?xml version="1.0"?><node text="Meta AI, Verified"/>'
        d = _mock_d_with_xml(xml)
        assert fb._verify_recipient_in_conv_title(d, "Meta AI") is True

    def test_recipient_absent_returns_false(self):
        """IJ8H 真机场景: 搜 'Meta AI' 但进了 Shuichi Ito 对话页."""
        fb = _make_fb()
        xml = ('<?xml version="1.0"?>'
               '<node content-desc="Shuichi Ito, Active 16 hours ago,'
               ' Thread details"/>')
        d = _mock_d_with_xml(xml)
        assert fb._verify_recipient_in_conv_title(d, "Meta AI") is False

    def test_empty_recipient_returns_true(self):
        """recipient 为空字符串 → 不验证, 返 True (兼容老调用)."""
        fb = _make_fb()
        d = _mock_d_with_xml("<node/>")
        assert fb._verify_recipient_in_conv_title(d, "") is True

    def test_substring_match_works(self):
        """recipient='Shuichi' 跟 dump 含 'Shuichi Ito' 匹配 (substring)."""
        fb = _make_fb()
        xml = '<node content-desc="Shuichi Ito"/>'
        d = _mock_d_with_xml(xml)
        assert fb._verify_recipient_in_conv_title(d, "Shuichi") is True

    def test_japanese_recipient_match(self):
        """日文 recipient 'しょうぶ あより' 匹配 dump."""
        fb = _make_fb()
        xml = '<node content-desc="しょうぶ あより, Thread details"/>'
        d = _mock_d_with_xml(xml)
        assert fb._verify_recipient_in_conv_title(
            d, "しょうぶ あより") is True

    def test_dump_returns_empty_returns_true_failsafe(self):
        """dump_hierarchy 返空 (u2 hiccup) → fail-safe True (放行)."""
        fb = _make_fb()
        d = _mock_d_with_xml("")
        assert fb._verify_recipient_in_conv_title(d, "Meta AI") is True

    def test_dump_raises_returns_true_failsafe(self):
        """dump_hierarchy 抛异常 → fail-safe True (不阻断主流程)."""
        fb = _make_fb()
        d = MagicMock()
        d.dump_hierarchy = MagicMock(side_effect=RuntimeError("u2 dead"))
        assert fb._verify_recipient_in_conv_title(d, "Meta AI") is True

    def test_recipient_with_whitespace_stripped(self):
        """recipient='  Meta AI  ' 应去白边后匹配."""
        fb = _make_fb()
        xml = '<node text="Meta AI"/>'
        d = _mock_d_with_xml(xml)
        assert fb._verify_recipient_in_conv_title(
            d, "  Meta AI  ") is True


# ════════════════════════════════════════════════════════════════════════
# 集成 — _send_message_impl 命中 false positive 时应抛 recipient_not_found
# ════════════════════════════════════════════════════════════════════════

class TestSendMessageImplVerifyIntegration:
    """验证 send_message_impl 在 tap_first_search_result 之后调 verify,
    不匹配时抛 MessengerError(recipient_not_found, hint=verify_failed)."""

    def test_verify_method_signature(self):
        """_verify_recipient_in_conv_title 必须存在 + 签名 (self, d, recipient)."""
        from src.app_automation.facebook import FacebookAutomation
        import inspect
        sig = inspect.signature(
            FacebookAutomation._verify_recipient_in_conv_title)
        params = list(sig.parameters.keys())
        assert params == ["self", "d", "recipient"]

    def test_send_message_impl_calls_verify(self):
        """source-level 检查: _send_message_impl 应在 _tap_first_search_result
        附近调 _verify_recipient_in_conv_title (静态 verify, 避免 600 行
        集成 mock)."""
        from src.app_automation.facebook import FacebookAutomation
        import inspect
        src = inspect.getsource(FacebookAutomation._send_message_impl)
        assert "_verify_recipient_in_conv_title" in src, (
            "_send_message_impl 没调 _verify_recipient_in_conv_title — "
            "OPT-FP1 修复未生效")
        # verify call 应在 _tap_first_search_result 之后
        idx_tap = src.find("_tap_first_search_result")
        idx_verify = src.find("_verify_recipient_in_conv_title")
        assert idx_verify > idx_tap, (
            "verify 调用应在 _tap_first_search_result 之后, 当前位置反了")

    def test_send_message_impl_raises_recipient_not_found_on_verify_fail(
            self):
        """src 中 verify 失败的 raise code 必须含 recipient_not_found
        (复用 INTEGRATION_CONTRACT §7.6 既有 code, 不引入新 code)."""
        from src.app_automation.facebook import FacebookAutomation
        import inspect
        src = inspect.getsource(FacebookAutomation._send_message_impl)
        # verify 不匹配应抛 recipient_not_found (复用既有 code)
        idx_verify = src.find("_verify_recipient_in_conv_title")
        # 找 verify 之后的 200 chars 内含 recipient_not_found
        nearby = src[idx_verify:idx_verify + 500]
        assert "recipient_not_found" in nearby, (
            "verify 失败的 raise code 应是 recipient_not_found")
        assert "verify_failed" in nearby, (
            "raise hint 应含 verify_failed 标识让调用方区分场景")
