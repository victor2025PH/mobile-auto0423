# -*- coding: utf-8 -*-
"""OPT-4-v2 (2026-04-28) — send 失败补登记 restriction.

OPT-4 _detect_risk_dialog 在 user-acked restriction page 后失效 (FB
restriction page 用户/系统点 OK 后不再弹), 但账号 restriction 服务端属性
仍生效, send 真发时返回 snackbar 含 "restricted" / "violated Community
Standards" / 等关键词. OPT-4-v2 在 send_message_impl 命中 blocked_text
后扫 hint, 命中即调 _mark_account_restricted_state(days=7 默认猜测) 补
登记 OPT-6 device_state, 让调度器从此避开.

覆盖:
  - _FB_RESTRICTION_HINT_KEYWORDS 字典契约 (en/zh/ja/it/es 多语言)
  - _detect_restriction_hint 命中各语言 + 不命中普通 content_blocked
  - 边界 (空/None/大小写)
"""
from __future__ import annotations

import pytest


def _make_fb():
    from src.app_automation.facebook import FacebookAutomation
    return FacebookAutomation.__new__(FacebookAutomation)


# ════════════════════════════════════════════════════════════════════════
# _FB_RESTRICTION_HINT_KEYWORDS 字典契约
# ════════════════════════════════════════════════════════════════════════

class TestRestrictionHintKeywordsContract:
    def test_en_restricted_present(self):
        from src.app_automation.facebook import FacebookAutomation
        kws = FacebookAutomation._FB_RESTRICTION_HINT_KEYWORDS
        joined = " ".join(kws).lower()
        assert "restricted" in joined

    def test_en_violated_present(self):
        from src.app_automation.facebook import FacebookAutomation
        kws = FacebookAutomation._FB_RESTRICTION_HINT_KEYWORDS
        joined = " ".join(kws).lower()
        assert "violated" in joined

    def test_en_community_standards_present(self):
        """FB 风控文案常含 'Community Standards' 政策提示。"""
        from src.app_automation.facebook import FacebookAutomation
        kws = FacebookAutomation._FB_RESTRICTION_HINT_KEYWORDS
        joined = " ".join(kws)
        assert "Community Standards" in joined

    def test_zh_keyword_present(self):
        from src.app_automation.facebook import FacebookAutomation
        kws = FacebookAutomation._FB_RESTRICTION_HINT_KEYWORDS
        joined = "".join(kws)
        assert "已限制" in joined or "已被限制" in joined

    def test_ja_keyword_present(self):
        from src.app_automation.facebook import FacebookAutomation
        kws = FacebookAutomation._FB_RESTRICTION_HINT_KEYWORDS
        joined = "".join(kws)
        # コミュニティ規定 / 制限されました 至少一个
        assert "コミュニティ規定" in joined or "制限されました" in joined


# ════════════════════════════════════════════════════════════════════════
# _detect_restriction_hint — 多语言命中
# ════════════════════════════════════════════════════════════════════════

class TestDetectRestrictionHint:
    @pytest.mark.parametrize("text", [
        # 真实 FB send 失败文案 (FB 实际推送过的样本)
        "Your message can't be sent because your account is currently restricted.",
        "You can't reply because you've violated our Community Standards.",
        "Message wasn't sent — your account has limits on sending",
        # 大小写不敏感
        "RESTRICTED",
        "Violated Community Standards",
        "you violated our community standards",
        # zh-CN
        "您的消息发送失败，账户已限制",
        "已被限制操作",
        "违反社区准则",
        # zh-TW
        "您已被限制操作",
        "違反社群守則",
        # ja
        "コミュニティ規定に違反しました",
        "アカウントが制限されました",
        # it
        "Account limitato per violazione degli Standard della community",
        # es
        "Cuenta restringido por incumplir las Normas de la Comunidad",
    ])
    def test_hint_keywords_detected(self, text):
        fb = _make_fb()
        assert fb._detect_restriction_hint(text) is True, (
            f"应命中 restriction hint: {text!r}"
        )

    @pytest.mark.parametrize("text", [
        # 普通 content_blocked 文案 (不含 restriction hint, 不应命中)
        "Message can't be sent",
        "Message wasn't sent",
        "couldn't send",
        "无法发送",
        "送信できませんでした",
        "Mensaje no enviado",
        # 空 / None
        "",
        None,
    ])
    def test_normal_content_blocked_not_detected(self, text):
        fb = _make_fb()
        assert fb._detect_restriction_hint(text) is False, (
            f"普通 content_blocked 不应误命中: {text!r}"
        )

    def test_partial_match_in_long_text(self):
        """长 dump 文本里嵌 restriction 短语应能命中 (substring search)。"""
        fb = _make_fb()
        long_text = (
            "<some surrounding XML> Your account is currently restricted "
            "from sending new messages. <more surrounding XML>"
        )
        assert fb._detect_restriction_hint(long_text) is True


# ════════════════════════════════════════════════════════════════════════
# 集成: send_message_impl 命中 blocked_text 后, restriction hint → mark
# ════════════════════════════════════════════════════════════════════════

class TestSendImplRestrictionHintIntegration:
    """验证 send_message_impl 的 OPT-4-v2 路径调用 chain:
    blocked_text → _detect_restriction_hint hit → _mark_account_restricted_state.

    不跑完整 send_message_impl (依赖太多), 只验证 _detect_restriction_hint
    的调用契约: 命中时返 True, 不命中返 False, 调用方按此走分支."""

    def test_real_fb_text_triggers_mark_path(self):
        """模拟真实 FB 文案: '...currently restricted...' 触发 hint 命中,
        调用方应走 _mark_account_restricted_state 路径。"""
        fb = _make_fb()
        real_text = (
            "Your message can't be sent because your account is currently "
            "restricted. To learn more, view our Community Standards."
        )
        # 应命中
        assert fb._detect_restriction_hint(real_text) is True

    def test_pure_content_violation_does_not_trigger_mark(self):
        """纯文本违规 (没账号 restriction): 'Message can't be sent' →
        不应命中 hint, 走原有 content_blocked 路径不补登记 restriction。"""
        fb = _make_fb()
        pure_text = "Message can't be sent. Please try again."
        assert fb._detect_restriction_hint(pure_text) is False


# ════════════════════════════════════════════════════════════════════════
# OPT-4-v3: 复用 _parse_restriction_days 解析 send 失败文案天数
# ════════════════════════════════════════════════════════════════════════

class TestOpt4v3DaysParsing:
    """OPT-4-v3 (2026-04-28): send_message_impl 命中 hint 时, 先尝试
    _parse_restriction_days(blocked_text) 解析具体天数, 解析失败回退 7 天."""

    @pytest.mark.parametrize("text,expected_days", [
        # FB 真机如果在 send 失败时推送含天数文案 (理论上可能)
        ("Your account is currently restricted for 6 days. Try again "
         "after Tuesday.", 6),
        ("You've been restricted for 1 day", 1),
        ("Your account has been restricted for 30 days due to violation",
         30),
        # OPT-4 实测的 SWZL 真机原文
        ("Your account has been restricted for 6 days because a message "
         "you sent didn't follow our Community Standards on bullying and "
         "harassment.", 6),
    ])
    def test_parses_days_from_send_failure_text(self, text, expected_days):
        """复用 _parse_restriction_days 顶级工具, 该函数 OPT-4 已实现."""
        from src.app_automation.facebook import _parse_restriction_days
        assert _parse_restriction_days(text) == expected_days

    @pytest.mark.parametrize("text", [
        # send 失败常见文案 — 不含天数, 应该回退默认 7 天
        ("Your message can't be sent because your account is currently "
         "restricted."),
        ("You can't reply because you've violated our Community Standards."),
        ("Account limitato per violazione degli Standard della community"),
        # 边界: 空 / 无 restricted 关键词
        "",
        "Random unrelated text",
    ])
    def test_no_days_in_text_returns_zero_for_v3_fallback(self, text):
        """这些文案 _parse_restriction_days 应返 0, 调用方 (OPT-4-v3) 用 0
        判断回退到 7 天默认值."""
        from src.app_automation.facebook import _parse_restriction_days
        assert _parse_restriction_days(text) == 0

    def test_v3_fallback_logic_chooses_parsed_when_positive(self):
        """OPT-4-v3 调用方逻辑: parsed_days > 0 用 parsed, 否则 7."""
        from src.app_automation.facebook import _parse_restriction_days
        # 真机推送的明确天数文案
        text_with_days = "restricted for 14 days"
        parsed = _parse_restriction_days(text_with_days)
        days = parsed if parsed > 0 else 7
        assert days == 14
        # 不含天数 → 7
        text_without_days = "Your account is currently restricted."
        parsed = _parse_restriction_days(text_without_days)
        days = parsed if parsed > 0 else 7
        assert days == 7
