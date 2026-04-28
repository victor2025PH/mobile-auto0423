# -*- coding: utf-8 -*-
"""OPT-FP7 (2026-04-28) — _tap_inbox_row_by_recipient 真消灭 false positive.

L5 v2 实测 OPT-FP1-FP6 5 重防线后**仍 3/4 false positive** (Q4N7 进齋藤步
ThreadSettings / SWZL 在 katana 菜单 / XW8T 在备份页). 根因: search 路径
不可靠 (Messenger 把"自己"放第 1 + FB 整合到 katana messaging-in-blue
后 search 切走 app).

OPT-FP7 重构: send_message_impl 优先走 inbox-direct-tap (不走 search).
直接在 inbox 列表找含 recipient name 的 row → tap row 中心进对话页.
miss 才 fallback search 路径 (向后兼容).

OPT-FP6-v2: 接受 katana 内嵌 messaging activity (com.facebook.messaging
+ ThreadActivity / ThreadViewActivity 在 katana 包下) — FB messaging-in-
blue 整合后 Messenger UI 跑在 katana 而非 orca.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def _make_fb():
    from src.app_automation.facebook import FacebookAutomation
    fb = FacebookAutomation.__new__(FacebookAutomation)
    fb.dm = MagicMock()
    return fb


def _mock_d_with_xml(xml: str, *, window_size=(720, 1438)):
    d = MagicMock()
    d.dump_hierarchy = MagicMock(return_value=xml)
    d.click = MagicMock()
    d.swipe = MagicMock()
    d.window_size = MagicMock(return_value=window_size)
    return d


# ════════════════════════════════════════════════════════════════════════
# OPT-FP7 _tap_inbox_row_by_recipient
# ════════════════════════════════════════════════════════════════════════

class TestTapInboxRowByRecipient:
    def test_finds_row_in_inbox_taps_center(self):
        fb = _make_fb()
        # IJ8H inbox 实测: Meta AI row 在 y=1113-1257
        xml = (
            '<node class="android.widget.Button" '
            'content-desc="Meta AI conversation snippet" '
            'bounds="[0,1113][720,1257]"/>'
        )
        d = _mock_d_with_xml(xml)
        ok = fb._tap_inbox_row_by_recipient(d, "Meta AI")
        assert ok is True
        d.click.assert_called_once_with(360, 1185)  # row 中心

    def test_skips_search_bar_y_lt_300(self):
        fb = _make_fb()
        xml = (
            '<node class="android.widget.Button" '
            'content-desc="Ask Meta AI or Search" '
            'bounds="[32,189][688,269]"/>'
        )
        d = _mock_d_with_xml(xml)
        ok = fb._tap_inbox_row_by_recipient(d, "Meta AI")
        assert ok is False  # search bar 跳过, 没有别的 row 命中

    def test_skips_stories_avatar(self):
        fb = _make_fb()
        xml = (
            '<node class="android.view.ViewGroup" '
            'content-desc="Meta AI active now" '
            'bounds="[100,400][240,470]"/>'
        )
        d = _mock_d_with_xml(xml)
        ok = fb._tap_inbox_row_by_recipient(d, "Meta AI")
        assert ok is False

    def test_finds_real_row_skips_stories(self):
        fb = _make_fb()
        xml = (
            # stories avatar (跳过)
            '<node class="android.view.ViewGroup" '
            'content-desc="Meta AI active now" '
            'bounds="[100,400][240,470]"/>'
            # 真 inbox row
            '<node class="android.widget.Button" '
            'content-desc="Meta AI 20 Questions Game ON!" '
            'bounds="[0,1113][720,1257]"/>'
        )
        d = _mock_d_with_xml(xml)
        ok = fb._tap_inbox_row_by_recipient(d, "Meta AI")
        assert ok is True
        d.click.assert_called_once_with(360, 1185)

    def test_no_match_scrolls_then_returns_false(self):
        """没找到 → swipe scroll → 仍没找到 → False."""
        fb = _make_fb()
        xml_no_match = (
            '<node content-desc="Other Person" bounds="[0,500][720,650]"/>'
        )
        d = _mock_d_with_xml(xml_no_match)
        ok = fb._tap_inbox_row_by_recipient(
            d, "Meta AI", max_scrolls=2)
        assert ok is False
        # 应 dump 2 次 (max_scrolls=2)
        assert d.dump_hierarchy.call_count == 2
        # 第 1 次 miss → swipe → 第 2 次 miss → return False
        assert d.swipe.call_count == 1

    def test_second_scroll_finds_returns_true(self):
        """第 1 次 miss, scroll 后第 2 次命中."""
        fb = _make_fb()
        xml_no = '<node content-desc="Other" bounds="[0,500][720,650]"/>'
        xml_yes = (
            '<node content-desc="Meta AI hello" '
            'bounds="[0,800][720,950]"/>')
        d = MagicMock()
        d.dump_hierarchy = MagicMock(side_effect=[xml_no, xml_yes])
        d.click = MagicMock()
        d.swipe = MagicMock()
        d.window_size = MagicMock(return_value=(720, 1438))
        ok = fb._tap_inbox_row_by_recipient(d, "Meta AI", max_scrolls=3)
        assert ok is True
        d.click.assert_called_once_with(360, 875)
        assert d.swipe.call_count == 1  # 第 1 次 miss 后 scroll

    def test_japanese_recipient(self):
        fb = _make_fb()
        xml = (
            '<node class="android.widget.Button" '
            'content-desc="しょうぶ あより You: Hey" '
            'bounds="[0,500][720,650]"/>'
        )
        d = _mock_d_with_xml(xml)
        ok = fb._tap_inbox_row_by_recipient(d, "しょうぶ あより")
        assert ok is True

    def test_empty_recipient_returns_false(self):
        fb = _make_fb()
        d = _mock_d_with_xml("<node/>")
        assert fb._tap_inbox_row_by_recipient(d, "") is False

    def test_dump_exception_failsafe_false(self):
        fb = _make_fb()
        d = MagicMock()
        d.dump_hierarchy = MagicMock(
            side_effect=RuntimeError("u2 hiccup"))
        d.window_size = MagicMock(return_value=(720, 1438))
        ok = fb._tap_inbox_row_by_recipient(
            d, "Meta AI", max_scrolls=1)
        assert ok is False

    def test_re_escape_prevents_regex_injection(self):
        """recipient 含 regex 特殊字符 (e.g. 'A.B') 应 re.escape."""
        fb = _make_fb()
        xml = ('<node content-desc="A.B exact" bounds="[0,500][720,650]"/>')
        d = _mock_d_with_xml(xml)
        ok = fb._tap_inbox_row_by_recipient(d, "A.B")
        assert ok is True


# ════════════════════════════════════════════════════════════════════════
# 集成 — _send_message_impl 应优先 inbox-direct-tap, miss fallback search
# ════════════════════════════════════════════════════════════════════════

class TestSendMessageImplIntegration:
    def test_send_message_impl_calls_inbox_direct_first(self):
        """source-level: _send_message_impl 应在 _enter_messenger_search 之前
        调 _tap_inbox_row_by_recipient (FP7 优先级)."""
        from src.app_automation.facebook import FacebookAutomation
        import inspect
        src = inspect.getsource(FacebookAutomation._send_message_impl)
        assert "_tap_inbox_row_by_recipient" in src, (
            "_send_message_impl 没集成 OPT-FP7 inbox-direct-tap")
        idx_inbox = src.find("_tap_inbox_row_by_recipient")
        idx_search = src.find("_enter_messenger_search(d, did)")
        assert idx_inbox < idx_search, (
            "OPT-FP7 inbox-direct-tap 应在 _enter_messenger_search 之前")

    def test_search_path_is_fallback(self):
        """source-level: search 路径应在 inbox-direct-tap miss 后才走
        (else 分支或类似条件结构)."""
        from src.app_automation.facebook import FacebookAutomation
        import inspect
        src = inspect.getsource(FacebookAutomation._send_message_impl)
        # 验证结构: inbox-direct-tap 命中后 skip search
        idx_inbox_call = src.find("_tap_inbox_row_by_recipient")
        nearby = src[idx_inbox_call:idx_inbox_call + 1000]
        # 应有 if / else 结构区分
        assert ("if" in nearby and
                ("else" in nearby or "fallback" in nearby.lower())), (
            "FP7 应该有 if/else 结构区分 inbox-direct vs search fallback")


# ════════════════════════════════════════════════════════════════════════
# OPT-FP6-v2 — 接受 katana 内嵌 messaging activity
# ════════════════════════════════════════════════════════════════════════

class TestFp6v2KatanaMessagingAccept:
    def test_orca_activity_returns_true(self):
        """com.facebook.orca 在前台 (旧 standalone Messenger) → True."""
        fb = _make_fb()
        fb.dm._run_adb = MagicMock(return_value=(
            True,
            "mCurrentFocus=Window{x u0 com.facebook.orca/MainActivity}"))
        assert fb._verify_messenger_in_foreground("D1") is True

    def test_katana_messaging_activity_returns_true(self):
        """com.facebook.katana/com.facebook.messaging.* 在前台 (FB
        messaging-in-blue) → True (OPT-FP6-v2 修)."""
        fb = _make_fb()
        fb.dm._run_adb = MagicMock(return_value=(
            True,
            "mCurrentFocus=Window{x u0 com.facebook.katana/"
            "com.facebook.messaging.threadview.ThreadViewActivity}"))
        assert fb._verify_messenger_in_foreground("D1") is True

    def test_katana_messaging_threadsettings_returns_true(self):
        """Q4N7 实测 katana/ThreadSettingsActivity (从对话 tap "i" 进的 设置页)
        — 仍是 messaging activity, 应接受."""
        fb = _make_fb()
        fb.dm._run_adb = MagicMock(return_value=(
            True,
            "mCurrentFocus=Window{x u0 com.facebook.katana/"
            "com.facebook.messaging.threadsettings2.activity."
            "ThreadSettingsActivity}"))
        assert fb._verify_messenger_in_foreground("D1") is True

    def test_katana_NON_messaging_returns_false(self):
        """katana 但非 messaging activity (e.g. LoginActivity / Composer)
        → False."""
        fb = _make_fb()
        fb.dm._run_adb = MagicMock(return_value=(
            True,
            "mCurrentFocus=Window{x u0 com.facebook.katana/"
            "com.facebook.katana.LoginActivity}"))
        with __import__("unittest.mock").mock.patch(
                "src.app_automation.facebook.time.sleep"):
            assert fb._verify_messenger_in_foreground("D1") is False

    def test_miui_home_returns_false(self):
        """完全在别的 app (launcher) → False."""
        fb = _make_fb()
        fb.dm._run_adb = MagicMock(return_value=(
            True,
            "mCurrentFocus=Window{x u0 com.miui.home/"
            "com.miui.home.launcher.Launcher}"))
        with __import__("unittest.mock").mock.patch(
                "src.app_automation.facebook.time.sleep"):
            assert fb._verify_messenger_in_foreground("D1") is False
