# -*- coding: utf-8 -*-
"""OPT-4 (2026-04-28) — Facebook account_restricted 风控识别单测。

触发场景: Community Standards 违规后 N 天 restriction 页面 (SWZL 真机摸底
产物). 文案: "Your account has been restricted for X days". 该页面只有
OK 按钮 + 长期持续, 跟 _detect_risk_dialog 现有的 "需 button + 1.6s 校验"
不兼容, OPT-4 给独立路径 + kind=account_restricted + 解析 X 天数。

覆盖:
  - _parse_restriction_days 解析鲁棒性 (大小写/多空格/无数字/空)
  - _classify_risk_kind 优先级 (restricted 必须 *先于* account_review 命中)
  - _detect_risk_dialog 对 restriction 跳过 button 校验直接判 risk
  - _extract_restriction_full_text 抽完整文案 (含 X days)
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest


# ════════════════════════════════════════════════════════════════════════
# _parse_restriction_days 工具函数
# ════════════════════════════════════════════════════════════════════════

class TestParseRestrictionDays:
    """从 FB restriction 文案抽取限制天数。"""

    @pytest.mark.parametrize("text,expected", [
        # SWZL 真机实测原文
        ("Your account has been restricted for 6 days", 6),
        # 单数 day
        ("restricted for 1 day", 1),
        # 大写
        ("RESTRICTED FOR 30 DAYS", 30),
        # 多空格容错 (re.compile 用 \s+)
        ("restricted   for   12   days", 12),
        # 嵌入长文本
        ("Your account has been restricted for 7 days because a message that "
         "you sent didn't follow our Community Standards on bullying and "
         "harassment.", 7),
        # 缺天数
        ("restricted", 0),
        # 空 / None
        ("", 0),
        (None, 0),
        # 'X' 占位 (非数字)
        ("restricted for X days", 0),
        # 没 restricted 关键词
        ("for 6 days only", 0),
        # 极大值 (上限不限制, 调用方需判断合理性)
        ("restricted for 365 days", 365),
        # 0 days (理论上不该有, 但应能解析)
        ("restricted for 0 days", 0),
    ])
    def test_various_inputs(self, text, expected):
        from src.app_automation.facebook import _parse_restriction_days
        assert _parse_restriction_days(text) == expected


# ════════════════════════════════════════════════════════════════════════
# _classify_risk_kind 优先级 — restricted 必须先于 account_review
# ════════════════════════════════════════════════════════════════════════

class TestClassifyKind:
    """fb_store._RISK_KIND_RULES 顺序匹配, 加 account_restricted 后必须先于
    account_review 命中, 否则 'account has been restricted' 文案被 catch
    成 account_review (现有规则) 就丢失了 restriction 语义。"""

    def test_swzl_full_message_classified_account_restricted(self):
        from src.host.fb_store import _classify_risk_kind
        msg = "Your account has been restricted for 6 days"
        assert _classify_risk_kind(msg) == "account_restricted"

    def test_short_restricted_for_phrase(self):
        from src.host.fb_store import _classify_risk_kind
        assert _classify_risk_kind("restricted for 1 day") == "account_restricted"

    def test_account_restricted_phrase(self):
        from src.host.fb_store import _classify_risk_kind
        assert _classify_risk_kind("account has been restricted") == "account_restricted"

    def test_disabled_still_account_review_not_break(self):
        """加新规则后 disabled 路径不应被破坏 (account_review kind)。"""
        from src.host.fb_store import _classify_risk_kind
        assert _classify_risk_kind(
            "Your account has been disabled") == "account_review"

    def test_locked_still_account_review(self):
        from src.host.fb_store import _classify_risk_kind
        assert _classify_risk_kind("account is locked") == "account_review"

    def test_unrelated_still_other(self):
        from src.host.fb_store import _classify_risk_kind
        assert _classify_risk_kind("hello world") == "other"


# ════════════════════════════════════════════════════════════════════════
# _detect_risk_dialog — restricted 独立路径
# ════════════════════════════════════════════════════════════════════════

def _make_fb_with_d(d_factory):
    """构造一个 FacebookAutomation + mock 的 d, knobs 由 d_factory 决定。"""
    from src.app_automation.facebook import FacebookAutomation
    fb = FacebookAutomation.__new__(FacebookAutomation)
    fb._handle_xspace_dialog = MagicMock()
    fb._report_risk = MagicMock()
    fb._did = lambda x=None: x or ""
    return fb


class _MockExists:
    """模拟 d(textContains=...).exists(timeout=...)."""
    def __init__(self, hit: bool, text: str = ""):
        self._hit = hit
        self._text = text

    def exists(self, timeout=0):
        return self._hit

    def get_text(self):
        return self._text

    @property
    def info(self):
        return {"text": self._text}


class TestDetectRiskDialogRestricted:
    """SWZL 风控页 restriction 走独立路径, 不需 button + 不做 1.6s 校验。"""

    def _build_d_with_restriction(self, full_text=None):
        """模拟 d(textContains=...) 行为:
        - 命中 'Your account has been restricted' kw
        - _extract_restriction_full_text 抽到 full_text (含 X days)
        - 不命中 _FB_RISK_BUTTONS (确认 OPT-4 路径不需 button)
        """
        if full_text is None:
            full_text = "Your account has been restricted for 6 days"

        def d_call(*a, **kw):
            txt = kw.get("textContains") or kw.get("text") or ""
            # restriction kw 命中
            if "restricted" in txt.lower() and "account" in txt.lower():
                return _MockExists(True, text=full_text)
            # full_text extract 路径 (textContains="restricted for")
            if "restricted for" in txt.lower():
                return _MockExists(True, text=full_text)
            return _MockExists(False)
        d = MagicMock()
        d.side_effect = d_call
        d.__call__ = d_call
        d.serial = "DEVICE-FAKE"
        return d

    def test_swzl_restriction_detected_skips_button_check(self):
        fb = _make_fb_with_d(None)
        d = self._build_d_with_restriction()
        is_risk, msg = fb._detect_risk_dialog(d)
        assert is_risk is True
        assert "restricted" in msg.lower()
        # button 校验路径**不应**被走 (因为 OPT-4 早退); _report_risk 必被调
        fb._report_risk.assert_called_once()
        args, kwargs = fb._report_risk.call_args
        # _report_risk 接 message 参数 (positional 或 keyword)
        reported = args[0] if args else kwargs.get("message", "")
        assert "for 6 days" in reported

    def test_returns_full_message_with_days(self):
        """返回的 message 应含完整文案 (含 'for X days') 而非短 keyword,
        这样下游 _classify_risk_kind 能命中 account_restricted。"""
        fb = _make_fb_with_d(None)
        d = self._build_d_with_restriction(
            "Your account has been restricted for 6 days")
        is_risk, msg = fb._detect_risk_dialog(d)
        assert is_risk is True
        assert "6 days" in msg

    def test_extract_full_text_falls_back_when_long_text_missing(self):
        """_extract_restriction_full_text 抽不到完整文案时,
        _detect_risk_dialog 仍应判 risk (用 hit_kw 兜底)。"""
        fb = _make_fb_with_d(None)
        # 模拟 hit_kw 命中, 但 textContains="restricted for" 抽不到
        def d_call(*a, **kw):
            txt = kw.get("textContains") or kw.get("text") or ""
            if "restricted" in txt.lower() and "account" in txt.lower():
                # 命中 keyword 但 get_text 返空
                return _MockExists(True, text="")
            return _MockExists(False)
        d = MagicMock()
        d.side_effect = d_call
        d.__call__ = d_call
        d.serial = "FAKE"

        is_risk, msg = fb._detect_risk_dialog(d)
        assert is_risk is True
        assert "restricted" in msg.lower()


class TestExtractRestrictionFullText:
    """_extract_restriction_full_text helper 单测。"""

    def test_returns_full_text_when_present(self):
        from src.app_automation.facebook import FacebookAutomation
        fb = FacebookAutomation.__new__(FacebookAutomation)

        full = "Your account has been restricted for 6 days"
        def d_call(*a, **kw):
            txt = kw.get("textContains") or ""
            if txt == "restricted for":
                return _MockExists(True, text=full)
            return _MockExists(False)
        d = MagicMock()
        d.side_effect = d_call
        d.__call__ = d_call

        result = fb._extract_restriction_full_text(d)
        assert "for 6 days" in result

    def test_falls_back_to_secondary_needle(self):
        """primary needle 'restricted for' miss 时, 用 'account has been
        restricted' 兜底 (没天数也比没文案好)。"""
        from src.app_automation.facebook import FacebookAutomation
        fb = FacebookAutomation.__new__(FacebookAutomation)

        def d_call(*a, **kw):
            txt = kw.get("textContains") or ""
            if txt == "account has been restricted":
                return _MockExists(True, text="account has been restricted")
            return _MockExists(False)
        d = MagicMock()
        d.side_effect = d_call
        d.__call__ = d_call

        result = fb._extract_restriction_full_text(d)
        assert "restricted" in result.lower()

    def test_returns_empty_when_both_miss(self):
        from src.app_automation.facebook import FacebookAutomation
        fb = FacebookAutomation.__new__(FacebookAutomation)

        d = MagicMock()
        d.side_effect = lambda *a, **kw: _MockExists(False)
        d.__call__ = lambda *a, **kw: _MockExists(False)

        assert fb._extract_restriction_full_text(d) == ""

    def test_swallows_exception_continues_to_next(self):
        from src.app_automation.facebook import FacebookAutomation
        fb = FacebookAutomation.__new__(FacebookAutomation)

        call_count = {"n": 0}

        def d_call(*a, **kw):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("u2 hiccup")
            return _MockExists(True, text="account has been restricted")
        d = MagicMock()
        d.side_effect = d_call
        d.__call__ = d_call

        # 第 1 次抛异常被吞, 第 2 次 needle 命中
        result = fb._extract_restriction_full_text(d)
        assert "restricted" in result.lower()
