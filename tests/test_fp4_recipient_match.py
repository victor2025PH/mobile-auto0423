# -*- coding: utf-8 -*-
"""OPT-FP4 (2026-04-28) — _tap_search_result_by_recipient_match 单测.

L0 在 _tap_first_search_result 4 级 fallback 之前: dump 搜索结果 hierarchy
找 row content-desc 含 recipient → tap 中心. 解决 Messenger 搜索"Meta AI"
把"自己头像"放第 1 → tap 错对话页 (FP1 已 catch 但 FP4 修真根因).
"""
from __future__ import annotations

import re
from unittest.mock import MagicMock

import pytest


def _make_fb():
    from src.app_automation.facebook import FacebookAutomation
    return FacebookAutomation.__new__(FacebookAutomation)


def _mock_d_with_xml(xml: str):
    d = MagicMock()
    d.dump_hierarchy = MagicMock(return_value=xml)
    d.click = MagicMock()
    return d


# ════════════════════════════════════════════════════════════════════════
# _tap_search_result_by_recipient_match 命中
# ════════════════════════════════════════════════════════════════════════

class TestRecipientMatch:
    def test_finds_row_by_substring(self):
        """搜索结果 dump 含 'Meta AI' row → 找到 + tap."""
        fb = _make_fb()
        xml = (
            '<?xml version="1.0"?><hierarchy>'
            '<node class="android.widget.Button" '
            'content-desc="Meta AI" '
            'bounds="[0,500][720,650]"/>'
            '</hierarchy>'
        )
        d = _mock_d_with_xml(xml)
        ok = fb._tap_search_result_by_recipient_match(d, "Meta AI")
        assert ok is True
        # 中心点 (360, 575) — 跳过 search bar / stories
        d.click.assert_called_once_with(360, 575)

    def test_skips_search_bar_y_lt_300(self):
        """y1 < 300 是 search bar / status bar, 应跳过."""
        fb = _make_fb()
        xml = (
            '<node class="android.widget.Button" '
            'content-desc="Meta AI Search Bar" '
            'bounds="[0,189][720,269]"/>'
        )
        d = _mock_d_with_xml(xml)
        ok = fb._tap_search_result_by_recipient_match(d, "Meta AI")
        assert ok is False
        d.click.assert_not_called()

    def test_skips_short_stories_avatar(self):
        """高度 < 100 是 active-now stories 头像, 应跳过."""
        fb = _make_fb()
        xml = (
            '<node class="android.widget.Button" '
            'content-desc="Meta AI active now" '
            'bounds="[100,400][240,470]"/>'
        )
        d = _mock_d_with_xml(xml)
        ok = fb._tap_search_result_by_recipient_match(d, "Meta AI")
        assert ok is False

    def test_finds_first_valid_row_skips_stories(self):
        """搜索结果同时含 stories 头像 + 真 row, 应选真 row 不选头像."""
        fb = _make_fb()
        xml = (
            # stories 头像 (high y but height < 100)
            '<node class="android.view.ViewGroup" '
            'content-desc="Meta AI active now" '
            'bounds="[100,400][240,470]"/>'
            # 真搜索结果 row
            '<node class="android.widget.Button" '
            'content-desc="Meta AI, Verified, AI Assistant" '
            'bounds="[0,600][720,750]"/>'
        )
        d = _mock_d_with_xml(xml)
        ok = fb._tap_search_result_by_recipient_match(d, "Meta AI")
        assert ok is True
        d.click.assert_called_once_with(360, 675)  # 真 row 中心

    def test_japanese_recipient_match(self):
        """日文 recipient match."""
        fb = _make_fb()
        xml = (
            '<node class="android.widget.Button" '
            'content-desc="しょうぶ あより, Active 16 hours ago" '
            'bounds="[0,500][720,650]"/>'
        )
        d = _mock_d_with_xml(xml)
        ok = fb._tap_search_result_by_recipient_match(
            d, "しょうぶ あより")
        assert ok is True

    def test_no_match_returns_false_for_fallback(self):
        """搜索结果不含 recipient → 返 False 让 L1-L4 fallback 接管."""
        fb = _make_fb()
        xml = (
            '<node class="android.widget.Button" '
            'content-desc="Shuichi Ito" '
            'bounds="[0,500][720,650]"/>'
        )
        d = _mock_d_with_xml(xml)
        ok = fb._tap_search_result_by_recipient_match(d, "Meta AI")
        assert ok is False
        d.click.assert_not_called()

    def test_empty_recipient_returns_false(self):
        fb = _make_fb()
        d = _mock_d_with_xml("<node/>")
        assert fb._tap_search_result_by_recipient_match(d, "") is False

    def test_dump_returns_non_str_failsafe(self):
        """mock dump_hierarchy 返 MagicMock → fail-safe False (走 fallback)."""
        fb = _make_fb()
        d = MagicMock()  # dump_hierarchy 返 MagicMock 默认
        ok = fb._tap_search_result_by_recipient_match(d, "Meta AI")
        assert ok is False  # 而不是 True (跟 verify 不同 — 这里 fail = 走 fallback)

    def test_dump_raises_failsafe(self):
        fb = _make_fb()
        d = MagicMock()
        d.dump_hierarchy = MagicMock(side_effect=RuntimeError("u2 dead"))
        ok = fb._tap_search_result_by_recipient_match(d, "Meta AI")
        assert ok is False

    def test_recipient_with_special_regex_chars_escaped(self):
        """recipient 含 regex 特殊字符 (e.g. 'A.B') 应 re.escape 防 regex 注入."""
        fb = _make_fb()
        xml = '<node class="android.widget.Button" content-desc="A.B" bounds="[0,500][720,650]"/>'
        d = _mock_d_with_xml(xml)
        # "A.B" 在 regex 中 . 匹配任何字符 — re.escape 后应严格 match "A.B"
        ok = fb._tap_search_result_by_recipient_match(d, "A.B")
        assert ok is True


# ════════════════════════════════════════════════════════════════════════
# 集成 — _tap_first_search_result 应 L0 调 recipient_match
# ════════════════════════════════════════════════════════════════════════

class TestIntegration:
    def test_tap_first_search_result_calls_l0_match(self):
        """source-level: _tap_first_search_result 应在 smart_tap 之前调
        _tap_search_result_by_recipient_match (L0 优先级)."""
        from src.app_automation.facebook import FacebookAutomation
        import inspect
        src = inspect.getsource(
            FacebookAutomation._tap_first_search_result)
        assert "_tap_search_result_by_recipient_match" in src
        # L0 应在 smart_tap 之前
        idx_l0 = src.find("_tap_search_result_by_recipient_match")
        idx_smart = src.find('smart_tap("First matching contact"')
        assert idx_l0 < idx_smart, "L0 调用应在 L1 smart_tap 之前"

    def test_l0_match_returns_no_fallback_called(self):
        """L0 命中 → 不调 L1 smart_tap 或后续 fallback."""
        fb = _make_fb()
        fb.smart_tap = MagicMock(return_value=True)
        # L0 命中 (recipient 在 dump 里)
        xml = (
            '<node class="android.widget.Button" '
            'content-desc="Meta AI" '
            'bounds="[0,500][720,650]"/>'
        )
        d = _mock_d_with_xml(xml)
        # 调 _tap_first_search_result
        fb._tap_first_search_result(d, "DEV1", "Meta AI")
        # L0 应命中 → smart_tap 不调
        fb.smart_tap.assert_not_called()
