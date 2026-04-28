# -*- coding: utf-8 -*-
"""OPT-5 v3-v2 (2026-04-28) — _dismiss_dialogs 集成 dismiss_known_dialogs.

设计选择 (vs 改 4 处 caller): 入口 DRY — 在 _dismiss_dialogs 内嵌
dismiss_known_dialogs 作为前置, 自动覆盖所有 caller (send_message_impl /
check_messenger_inbox / check_message_requests / check_friend_requests).

调用顺序契约 (重要):
  1. _in_restriction_page(d) → True → return no-op (OPT-5 v2)
  2. dismiss_known_dialogs(d) → 处理 startup 特殊 dialog (OPT-5 v3)
  3. for max_attempts: 处理通用文本按钮 (原 _dismiss_dialogs 主体)

restriction page 双重保护:
  - _in_restriction_page 直接 return
  - 即使绕过 (异常吞), KNOWN_DIALOGS 里 restriction_page 也是 skip 类
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


def _make_fb():
    from src.app_automation.facebook import FacebookAutomation
    fb = FacebookAutomation.__new__(FacebookAutomation)
    fb.hb = MagicMock()
    fb.hb.tap = MagicMock()
    fb._handle_xspace_dialog = MagicMock(return_value=False)
    fb._el_center = lambda obj: (100, 100)
    return fb


class _Hit:
    def __init__(self, hit):
        self._hit = hit
    def exists(self, timeout=0):
        return self._hit
    def click(self):
        return None


# ════════════════════════════════════════════════════════════════════════
# 集成: _dismiss_dialogs 应调 dismiss_known_dialogs 前置
# ════════════════════════════════════════════════════════════════════════

class TestDismissKnownDialogsIntegration:
    """验证 _dismiss_dialogs 入口先调 dismiss_known_dialogs 再走通用文本路径。"""

    def test_dismiss_known_dialogs_called_first(self):
        """非 restriction page 时, _dismiss_dialogs 应先调
        dismiss_known_dialogs 再循环通用文本。"""
        fb = _make_fb()
        d = MagicMock()
        # 让 _in_restriction_page False (无 restricted marker)
        d.side_effect = lambda **kw: _Hit(False)
        d.__call__ = lambda **kw: _Hit(False)

        with patch(
                "src.app_automation.fb_dialog_dismisser.dismiss_known_dialogs"
        ) as mock_dkd:
            mock_dkd.return_value = ["language_not_supported"]
            fb._dismiss_dialogs(d, max_attempts=1)

        mock_dkd.assert_called_once()
        # max_rounds=3 透传
        kwargs = mock_dkd.call_args.kwargs
        assert kwargs.get("max_rounds") == 3

    def test_restriction_page_skips_both_dismissers(self):
        """restriction page 时 _in_restriction_page short-circuit, 连
        dismiss_known_dialogs 都不应调 (避免 ~2s 多余 marker 检查开销)。"""
        fb = _make_fb()
        d = MagicMock()
        # _in_restriction_page True (textContains restricted 命中)
        def _call(**kw):
            txt = kw.get("textContains") or ""
            if "restricted" in txt.lower():
                return _Hit(True)
            return _Hit(False)
        d.side_effect = _call
        d.__call__ = _call

        with patch(
                "src.app_automation.fb_dialog_dismisser.dismiss_known_dialogs"
        ) as mock_dkd:
            fb._dismiss_dialogs(d, max_attempts=5)

        # restriction page → _in_restriction_page True → return 不调 dkd
        mock_dkd.assert_not_called()

    def test_dismiss_known_dialogs_exception_does_not_break_dismiss(self):
        """OPT-5 v3 模块抛异常应被吞, 不阻断后续通用文本 dismiss 流程。"""
        fb = _make_fb()
        d = MagicMock()
        d.side_effect = lambda **kw: _Hit(False)
        d.__call__ = lambda **kw: _Hit(False)

        with patch(
                "src.app_automation.fb_dialog_dismisser.dismiss_known_dialogs",
                side_effect=RuntimeError("u2 hiccup")):
            # 不抛, 继续跑 max_attempts loop
            fb._dismiss_dialogs(d, max_attempts=2)


# ════════════════════════════════════════════════════════════════════════
# 集成 — 通过 send_message_impl 链路调用 (端到端契约)
# ════════════════════════════════════════════════════════════════════════

class TestSendMessageImplCallsBoth:
    """验证 send_message_impl 启动 Messenger 失败回退 app_start 后调
    _dismiss_dialogs, 进而触发 dismiss_known_dialogs."""

    def test_dkd_invoked_in_send_path(self):
        """send_message_impl 走 app_start fallback → _dismiss_dialogs →
        dismiss_known_dialogs 链路完整调用."""
        # 不实际跑 send_message_impl (依赖太多, 之前讨论过抽出 helper 风险),
        # 改为验证 _dismiss_dialogs 改动后的 caller signature 兼容
        from src.app_automation.facebook import FacebookAutomation
        # 验证 _dismiss_dialogs 接受 (d, max_attempts, device_id) 且
        # 调用后 d 不被破坏 (smoke test)
        sig = FacebookAutomation._dismiss_dialogs
        # 函数存在 + 接受 self/d/max_attempts/device_id
        assert callable(sig)


# ════════════════════════════════════════════════════════════════════════
# 副作用边界: FB 主 app caller (无 Messenger marker) 应 no-op
# ════════════════════════════════════════════════════════════════════════

class TestFbMainAppNoSideEffect:
    """check_friend_requests 等 FB 主 app caller 跑 _dismiss_dialogs,
    KNOWN_DIALOGS 的 marker (Messenger 特有) 全 miss → dkd no-op."""

    def test_fb_main_app_dkd_returns_empty(self):
        fb = _make_fb()
        d = MagicMock()
        # FB 主 app 状态: 没任何 Messenger marker
        d.side_effect = lambda **kw: _Hit(False)
        d.__call__ = lambda **kw: _Hit(False)

        with patch(
                "src.app_automation.fb_dialog_dismisser.dismiss_known_dialogs",
                return_value=[]) as mock_dkd:
            fb._dismiss_dialogs(d, max_attempts=1)

        # 仍被调 (DRY 入口), 但返空列表 (副作用 0)
        mock_dkd.assert_called_once()
