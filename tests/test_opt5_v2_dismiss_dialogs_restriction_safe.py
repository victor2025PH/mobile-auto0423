# -*- coding: utf-8 -*-
"""OPT-5 v2 (2026-04-28) — _dismiss_dialogs 在 restriction page 时 no-op.

发现的 bug: _FB_DISMISS_TEXTS 含 "OK", 当 SWZL 风控页弹出时, send_message
流程的 _dismiss_dialogs 会先于 _detect_risk_dialog 把 "OK" 点掉, 导致 OPT-4
检测路径在生产环境失效 (full_msg 已经被关掉了, _detect_risk_dialog 命中不到
"Your account has been restricted")。

OPT-5 v2 v1: _dismiss_dialogs 入口加 _in_restriction_page short-circuit,
让 _detect_risk_dialog 后续抛 MessengerError(risk_detected) + OPT-6 写状态
后调度器接管, 整个链路语义正确.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def _make_fb():
    from src.app_automation.facebook import FacebookAutomation
    fb = FacebookAutomation.__new__(FacebookAutomation)
    fb.hb = MagicMock()
    fb.hb.tap = MagicMock()
    fb._handle_xspace_dialog = MagicMock(return_value=False)
    fb._el_center = lambda obj: (100, 100)
    return fb


class _ExistsObj:
    def __init__(self, hit):
        self._hit = hit
    def exists(self, timeout=0):
        return self._hit


# ════════════════════════════════════════════════════════════════════════
# _in_restriction_page 检测
# ════════════════════════════════════════════════════════════════════════

class TestInRestrictionPage:
    def test_returns_true_when_marker_present(self):
        fb = _make_fb()
        d = MagicMock()
        def _call(**kw):
            txt = kw.get("textContains", "")
            return _ExistsObj("restricted" in txt.lower())
        d.side_effect = _call
        d.__call__ = _call
        assert fb._in_restriction_page(d) is True

    def test_returns_false_when_no_marker(self):
        fb = _make_fb()
        d = MagicMock()
        d.side_effect = lambda **kw: _ExistsObj(False)
        d.__call__ = lambda **kw: _ExistsObj(False)
        assert fb._in_restriction_page(d) is False

    def test_returns_false_on_exception(self):
        """u2 异常时 fail-safe = False, 不阻断 _dismiss_dialogs 正常流程。"""
        fb = _make_fb()
        d = MagicMock()
        def _call(**kw):
            raise RuntimeError("u2 dead")
        d.side_effect = _call
        d.__call__ = _call
        assert fb._in_restriction_page(d) is False


# ════════════════════════════════════════════════════════════════════════
# _dismiss_dialogs 在 restriction page 时 no-op
# ════════════════════════════════════════════════════════════════════════

class TestDismissDialogsOnRestrictionPage:
    def test_returns_immediately_no_taps_when_restricted(self):
        """restriction page 时不应点任何按钮 — _FB_DISMISS_TEXTS 里有 OK,
        点了会让 OPT-4 _detect_risk_dialog 后续命中不到。"""
        fb = _make_fb()
        d = MagicMock()
        # restriction marker 命中
        def _call(**kw):
            txt = kw.get("textContains") or kw.get("text", "")
            return _ExistsObj("restricted" in txt.lower())
        d.side_effect = _call
        d.__call__ = _call

        fb._dismiss_dialogs(d, max_attempts=5)

        # _handle_xspace_dialog 不应被调 (restriction 时直接 return)
        fb._handle_xspace_dialog.assert_not_called()
        # hb.tap 不应被调 (没有点任何东西)
        fb.hb.tap.assert_not_called()

    def test_normal_page_still_dismisses_normally(self):
        """非 restriction page → _dismiss_dialogs 正常工作 (向后兼容)。"""
        fb = _make_fb()
        d = MagicMock()
        # _in_restriction_page → False
        # _FB_DISMISS_TEXTS 里 "OK" 命中
        call_count = {"n": 0}
        def _call(**kw):
            call_count["n"] += 1
            txt = kw.get("textContains") or kw.get("text", "")
            if "restricted" in txt.lower():
                return _ExistsObj(False)  # 非 restriction
            if txt == "OK":
                return _ExistsObj(True)   # 假命中 OK 按钮
            return _ExistsObj(False)
        d.side_effect = _call
        d.__call__ = _call

        fb._dismiss_dialogs(d, max_attempts=2)

        # 正常路径 hb.tap 应被调 (有 OK 命中)
        assert fb.hb.tap.call_count >= 1
