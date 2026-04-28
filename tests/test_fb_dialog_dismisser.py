# -*- coding: utf-8 -*-
"""OPT-5 v3 (2026-04-28) — fb_dialog_dismisser 通用 dismiss 模块单测.

覆盖:
  - KNOWN_DIALOGS 字典契约 (restriction page 必 skip / 必须含已知 dialog)
  - dismiss_known_dialogs 主接口行为 (cleared list / skip 也算 / max_rounds /
    异常吞 / 无命中早 break)
  - 各 dialog pattern 单独触发 (语言不支持 / Previews on / 通知权限 / 通讯录
    权限 / e2e 介绍 / restriction page)

不测真机 — 用 MagicMock d 模拟 textContains/text/desc 命中情况。
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest


class _Hit:
    """模拟 d(...) 返回的 UiObject."""
    def __init__(self, hit: bool):
        self._hit = hit

    def exists(self, timeout=0):
        return self._hit

    def click(self):
        return None


def _mk_d(marker_to_hit: dict):
    """构造 mock d, marker_to_hit dict 决定哪些 marker 命中。

    keys 是 "kind:value" 格式, 如 "textContains:Previews are on" /
    "text:继续使用美式英语" / "description:Click to dismiss this bottom sheet"
    """
    d = MagicMock()
    def _call(**kw):
        for k_kind in ("textContains", "text", "descriptionContains",
                       "description"):
            if k_kind in kw:
                key = f"{k_kind}:{kw[k_kind]}"
                return _Hit(marker_to_hit.get(key, False))
        return _Hit(False)
    d.side_effect = _call
    d.__call__ = _call
    return d


# ════════════════════════════════════════════════════════════════════════
# KNOWN_DIALOGS 字典契约
# ════════════════════════════════════════════════════════════════════════

class TestKnownDialogsContract:
    """字典契约保护 — 防未来误改破坏 OPT-4/5 v2 链路。"""

    def test_restriction_page_present_and_skip(self):
        """restriction_page 必须存在且 action=skip, 否则 OPT-4 失效。"""
        from src.app_automation.fb_dialog_dismisser import KNOWN_DIALOGS
        rp = next(
            (d for d in KNOWN_DIALOGS if d["name"] == "restriction_page"),
            None,
        )
        assert rp is not None, "restriction_page must be in KNOWN_DIALOGS"
        assert rp["action"] == "skip", \
            "restriction_page must SKIP, dismiss it would break OPT-4"

    def test_restriction_page_first_in_priority(self):
        """restriction_page 应排第一 — 否则可能被 e2e_intro 等先命中。"""
        from src.app_automation.fb_dialog_dismisser import KNOWN_DIALOGS
        assert KNOWN_DIALOGS[0]["name"] == "restriction_page"

    def test_language_not_supported_present(self):
        from src.app_automation.fb_dialog_dismisser import KNOWN_DIALOGS
        names = {d["name"] for d in KNOWN_DIALOGS}
        assert "language_not_supported" in names

    def test_previews_on_present(self):
        from src.app_automation.fb_dialog_dismisser import KNOWN_DIALOGS
        names = {d["name"] for d in KNOWN_DIALOGS}
        assert "previews_on" in names

    def test_notification_permission_dont_allow(self):
        """反追踪策略 — 不应让 Messenger 拿通知权限。"""
        from src.app_automation.fb_dialog_dismisser import KNOWN_DIALOGS
        np = next(
            (d for d in KNOWN_DIALOGS if d["name"] == "notification_permission"),
            None,
        )
        assert np is not None
        assert np["tap_target"] == "Don't allow"

    def test_each_entry_has_required_fields(self):
        from src.app_automation.fb_dialog_dismisser import KNOWN_DIALOGS
        for entry in KNOWN_DIALOGS:
            assert "name" in entry
            assert "marker" in entry
            assert "marker_kind" in entry
            assert "action" in entry
            if entry["action"] in ("tap_text", "tap_desc"):
                assert entry.get("tap_target"), \
                    f"{entry['name']}: tap_text/tap_desc 必须有 tap_target"


# ════════════════════════════════════════════════════════════════════════
# dismiss_known_dialogs 主接口
# ════════════════════════════════════════════════════════════════════════

class TestDismissKnownDialogs:
    """主接口行为契约。"""

    def test_no_marker_returns_empty(self):
        from src.app_automation.fb_dialog_dismisser import dismiss_known_dialogs
        d = _mk_d({})  # 没任何 marker 命中
        cleared = dismiss_known_dialogs(d)
        assert cleared == []

    def test_language_not_supported_dismissed(self):
        """命中 '继续使用美式英语' 文字按钮 → cleared 含 language_not_supported。"""
        from src.app_automation.fb_dialog_dismisser import dismiss_known_dialogs
        d = _mk_d({
            "text:继续使用美式英语": True,  # marker (text=) 命中
        })
        cleared = dismiss_known_dialogs(d)
        assert "language_not_supported" in cleared

    def test_previews_on_dismissed_via_desc(self):
        """命中 'Previews are on' marker → tap content-desc=Click to dismiss。"""
        from src.app_automation.fb_dialog_dismisser import dismiss_known_dialogs
        d = _mk_d({
            "textContains:Previews are on": True,
            "description:Click to dismiss this bottom sheet": True,
        })
        cleared = dismiss_known_dialogs(d)
        assert "previews_on" in cleared

    def test_restriction_page_recognized_but_not_dismissed(self):
        """restriction page 识别 (cleared 含 name) 但不点 OK。"""
        from src.app_automation.fb_dialog_dismisser import dismiss_known_dialogs
        d = _mk_d({
            "textContains:Your account has been restricted": True,
            # OK 按钮也"在"但不应被点 — 通过 click MagicMock 不被调验证
        })
        cleared = dismiss_known_dialogs(d)
        assert "restriction_page" in cleared
        # 不应调任何 dismiss button click — 但 mock 无法直接验证
        # (skip action 走 _do_action 早 return True, 未触发 d(...) 二次调用)

    def test_e2e_encryption_intro_skipped(self):
        from src.app_automation.fb_dialog_dismisser import dismiss_known_dialogs
        d = _mk_d({
            "textContains:Messages and calls are secured with end-to-end encryption": True,
        })
        cleared = dismiss_known_dialogs(d)
        assert "e2e_encryption_intro" in cleared

    def test_multi_dialogs_in_one_call(self):
        """同时有多个 dialog → 全部 cleared (循环模式)。"""
        from src.app_automation.fb_dialog_dismisser import dismiss_known_dialogs
        d = _mk_d({
            "text:继续使用美式英语": True,
            "textContains:Previews are on": True,
            "description:Click to dismiss this bottom sheet": True,
        })
        cleared = dismiss_known_dialogs(d)
        assert "language_not_supported" in cleared
        assert "previews_on" in cleared

    def test_max_rounds_prevents_infinite_loop(self):
        """如果 marker 永远命中 (UI 不响应 click), max_rounds 应限制循环。"""
        from src.app_automation.fb_dialog_dismisser import dismiss_known_dialogs
        # marker 永远命中, 但 click 不"实际"消除 marker → 应在 max_rounds 后退出
        d = _mk_d({
            "text:继续使用美式英语": True,
            # action 是 tap_text 但 click 是 no-op (mock)
            # 重复模式: 每轮都命中 language_not_supported, 但已 seen → 跳过
        })
        cleared = dismiss_known_dialogs(d, max_rounds=2)
        # language_not_supported 第 1 次成功, 后续跳过 dedup
        assert "language_not_supported" in cleared
        # cleared 不应有重复
        assert len(cleared) == len(set(cleared))

    def test_exception_in_check_marker_swallowed(self):
        """u2 异常应被吞, 不阻断后续 dialog 检查。"""
        from src.app_automation.fb_dialog_dismisser import dismiss_known_dialogs
        d = MagicMock()
        def _bad_call(**kw):
            raise RuntimeError("u2 dead")
        d.side_effect = _bad_call
        d.__call__ = _bad_call

        # 不抛
        cleared = dismiss_known_dialogs(d)
        assert cleared == []  # 没命中, 但也没 crash

    def test_skip_action_dedup_across_rounds(self):
        """skip 类 dialog 在多轮中应只算一次, 不重复加入 cleared。"""
        from src.app_automation.fb_dialog_dismisser import dismiss_known_dialogs
        d = _mk_d({
            "textContains:Your account has been restricted": True,
            "textContains:Messages and calls are secured with end-to-end encryption": True,
        })
        cleared = dismiss_known_dialogs(d, max_rounds=3)
        # restriction_page 和 e2e_intro 都是 skip, 都只 cleared 一次
        assert cleared.count("restriction_page") == 1
        assert cleared.count("e2e_encryption_intro") == 1


# ════════════════════════════════════════════════════════════════════════
# 优先级 — restriction page 必须先于 e2e_encryption_intro 命中
# ════════════════════════════════════════════════════════════════════════

class TestPriorityRestrictionFirst:
    """如果 restriction page 和 e2e_intro 同时存在 (理论上不会, 但保险),
    restriction_page 应先 cleared (skip), 然后 e2e_intro。"""

    def test_restriction_first_then_e2e(self):
        from src.app_automation.fb_dialog_dismisser import dismiss_known_dialogs
        d = _mk_d({
            "textContains:Your account has been restricted": True,
            "textContains:Messages and calls are secured with end-to-end encryption": True,
        })
        cleared = dismiss_known_dialogs(d)
        # 顺序: restriction_page 在 KNOWN_DIALOGS 第 1 位 → 先发现
        assert cleared.index("restriction_page") < cleared.index(
            "e2e_encryption_intro")
