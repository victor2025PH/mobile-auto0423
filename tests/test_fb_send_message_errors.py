# -*- coding: utf-8 -*-
"""P2 `send_message` 错误细分测试 — B 机为 A 机 A2 降级路径提供归因契约。

覆盖:
  * `MessengerError` 基本语义 (code/hint/repr)
  * `_xspace_select_sheet_visible` 探测
  * `send_message(raise_on_error=False)` 所有失败路径均 return False (向后兼容)
  * `send_message(raise_on_error=True)` 7 种 code 分别抛出:
      - messenger_unavailable / xspace_blocked / risk_detected
      - search_ui_missing / recipient_not_found / send_button_missing
  * XSpace dismiss 成功时不抛 (降级成功)
  * 成功路径 return True

不测设备层,所有 adb/u2/compliance 交互都 patch 掉。
"""
from __future__ import annotations

from contextlib import ExitStack, nullcontext
from unittest.mock import MagicMock, patch

import pytest


def _make_fb():
    from src.app_automation.facebook import FacebookAutomation
    fb = FacebookAutomation.__new__(FacebookAutomation)
    fb.hb = MagicMock()
    fb.hb.type_text = MagicMock()
    fb.hb.wait_think = MagicMock()
    return fb


@pytest.fixture
def fb_env():
    """Yield (fb, knobs) 其中 knobs 是一个 dict, 测试按需 override 行为。

    默认 knobs (全成功路径):
      smart_tap:        每次 True
      app_current_pkg:  com.facebook.orca (Messenger)
      xspace_sheet:     False
      risk:             (False, "")
      dismiss_xspace:   True
    测试可 mutate knobs 来模拟各种失败。
    """
    fb = _make_fb()
    knobs = {
        "smart_tap_results": {},  # name substring → bool (default True)
        "app_current_pkg": "com.facebook.orca",
        "xspace_sheet_visible": False,
        "risk_dialog": (False, ""),
        "handle_xspace_dismissed": True,
        "app_start_raises": False,
    }

    def _smart_tap(name, device_id=None, **kw):
        for key, val in knobs["smart_tap_results"].items():
            if key in name:
                return val
        return True  # default success

    def _detect_risk(_d):
        return knobs["risk_dialog"]

    def _xspace_visible(_d):
        return knobs["xspace_sheet_visible"]

    def _handle_xspace(_d, _did):
        if knobs["handle_xspace_dismissed"]:
            knobs["app_current_pkg"] = "com.facebook.orca"
            knobs["xspace_sheet_visible"] = False
            return True
        return False

    fake_u2 = MagicMock()
    fake_u2.app_current = lambda: {"package": knobs["app_current_pkg"]}

    def _app_start(pkg, *a, **kw):
        if knobs["app_start_raises"]:
            raise RuntimeError("synthetic app_start fail")
    fake_u2.app_start = _app_start

    stack = ExitStack()
    stack.enter_context(patch.object(fb, "_did", return_value="devA"))
    stack.enter_context(patch.object(fb, "_u2", return_value=fake_u2))
    stack.enter_context(patch.object(fb, "rewrite_message",
                                     side_effect=lambda msg, _ctx: msg))
    stack.enter_context(patch.object(fb, "smart_tap", side_effect=_smart_tap))
    stack.enter_context(patch.object(fb, "_dismiss_dialogs"))
    stack.enter_context(patch.object(fb, "_handle_xspace_dialog",
                                     side_effect=_handle_xspace))
    stack.enter_context(patch.object(fb, "_detect_risk_dialog",
                                     side_effect=_detect_risk))
    stack.enter_context(patch.object(fb, "_xspace_select_sheet_visible",
                                     side_effect=_xspace_visible))
    stack.enter_context(patch.object(fb, "guarded",
                                     return_value=nullcontext()))
    stack.enter_context(patch("src.app_automation.facebook.time.sleep"))
    try:
        yield fb, knobs
    finally:
        stack.close()


# ─── MessengerError basics ───────────────────────────────────────────────────

class TestMessengerErrorBasics:
    def test_code_and_message(self):
        from src.app_automation.facebook import MessengerError
        e = MessengerError("xspace_blocked", "挡路了", hint="切回 FB")
        assert e.code == "xspace_blocked"
        assert str(e) == "挡路了"
        assert e.hint == "切回 FB"

    def test_default_message_is_code(self):
        from src.app_automation.facebook import MessengerError
        e = MessengerError("send_fail")
        assert str(e) == "send_fail"
        assert e.hint == ""

    def test_repr_includes_code(self):
        from src.app_automation.facebook import MessengerError
        e = MessengerError("recipient_not_found", "no match")
        assert "recipient_not_found" in repr(e)

    def test_is_exception_subclass(self):
        from src.app_automation.facebook import MessengerError
        assert issubclass(MessengerError, Exception)


# ─── _xspace_select_sheet_visible 独立测试 ───────────────────────────────────

class TestXSpaceSheetVisible:
    def test_english_sheet_detected(self):
        fb = _make_fb()
        d = MagicMock()
        d.side_effect = None
        en_sel = MagicMock()
        en_sel.exists.return_value = True
        cn_sel = MagicMock()
        cn_sel.exists.return_value = False

        def _by_text(text):
            return en_sel if text == "Select app" else cn_sel
        d.side_effect = _by_text
        assert fb._xspace_select_sheet_visible(d) is True

    def test_chinese_sheet_detected(self):
        fb = _make_fb()
        d = MagicMock()
        en_sel = MagicMock()
        en_sel.exists.return_value = False
        cn_sel = MagicMock()
        cn_sel.exists.return_value = True

        def _by_text(text):
            return en_sel if text == "Select app" else cn_sel
        d.side_effect = _by_text
        assert fb._xspace_select_sheet_visible(d) is True

    def test_neither_returns_false(self):
        fb = _make_fb()
        d = MagicMock()
        sel = MagicMock()
        sel.exists.return_value = False
        d.return_value = sel
        assert fb._xspace_select_sheet_visible(d) is False

    def test_exception_swallowed(self):
        fb = _make_fb()
        d = MagicMock()
        d.side_effect = RuntimeError("boom")
        assert fb._xspace_select_sheet_visible(d) is False


# ─── 成功路径 ────────────────────────────────────────────────────────────────

class TestSendMessageHappyPath:
    def test_default_success_returns_true(self, fb_env):
        fb, _ = fb_env
        assert fb.send_message("Alice", "hello") is True

    def test_raise_on_error_success_returns_true(self, fb_env):
        fb, _ = fb_env
        assert fb.send_message("Alice", "hello", raise_on_error=True) is True


# ─── raise_on_error=False 向后兼容 ───────────────────────────────────────────

class TestBackwardCompatReturnFalse:
    def test_messenger_icon_and_app_start_both_fail(self, fb_env):
        fb, knobs = fb_env
        knobs["smart_tap_results"]["Messenger or chat"] = False
        knobs["app_start_raises"] = True
        assert fb.send_message("Alice", "hello") is False

    def test_xspace_blocked_returns_false(self, fb_env):
        fb, knobs = fb_env
        knobs["app_current_pkg"] = "com.miui.securitycore"
        knobs["handle_xspace_dismissed"] = False
        assert fb.send_message("Alice", "hello") is False

    def test_risk_returns_false(self, fb_env):
        fb, knobs = fb_env
        knobs["risk_dialog"] = (True, "login challenge")
        assert fb.send_message("Alice", "hello") is False

    def test_search_missing_returns_false(self, fb_env):
        fb, knobs = fb_env
        knobs["smart_tap_results"]["Search in Messenger"] = False
        assert fb.send_message("Alice", "hello") is False

    def test_recipient_not_found_returns_false(self, fb_env):
        fb, knobs = fb_env
        knobs["smart_tap_results"]["First matching contact"] = False
        assert fb.send_message("Alice", "hello") is False

    def test_send_button_missing_returns_false(self, fb_env):
        fb, knobs = fb_env
        knobs["smart_tap_results"]["Send message button"] = False
        assert fb.send_message("Alice", "hello") is False


# ─── raise_on_error=True 细分归因 ────────────────────────────────────────────

class TestStrictRaisesWithCode:
    def test_messenger_unavailable(self, fb_env):
        from src.app_automation.facebook import MessengerError
        fb, knobs = fb_env
        knobs["smart_tap_results"]["Messenger or chat"] = False
        knobs["app_start_raises"] = True
        with pytest.raises(MessengerError) as ei:
            fb.send_message("Alice", "hello", raise_on_error=True)
        assert ei.value.code == "messenger_unavailable"
        assert ei.value.hint  # 非空 hint

    def test_xspace_blocked_when_dismiss_fails(self, fb_env):
        from src.app_automation.facebook import MessengerError
        fb, knobs = fb_env
        knobs["app_current_pkg"] = "com.miui.securitycore"
        knobs["handle_xspace_dismissed"] = False
        with pytest.raises(MessengerError) as ei:
            fb.send_message("Alice", "hello", raise_on_error=True)
        assert ei.value.code == "xspace_blocked"

    def test_xspace_dismissed_no_raise(self, fb_env):
        """XSpace 出现但 _handle_xspace_dialog 成功 dismiss → 不抛,继续流程。"""
        fb, knobs = fb_env
        knobs["app_current_pkg"] = "com.miui.securitycore"
        knobs["handle_xspace_dismissed"] = True
        assert fb.send_message("Alice", "hello", raise_on_error=True) is True

    def test_xspace_sheet_variant_blocked(self, fb_env):
        """浅色 Select app sheet 变体也识别为 xspace_blocked。"""
        from src.app_automation.facebook import MessengerError
        fb, knobs = fb_env
        knobs["xspace_sheet_visible"] = True
        knobs["handle_xspace_dismissed"] = False
        with pytest.raises(MessengerError) as ei:
            fb.send_message("Alice", "hello", raise_on_error=True)
        assert ei.value.code == "xspace_blocked"

    def test_risk_detected(self, fb_env):
        from src.app_automation.facebook import MessengerError
        fb, knobs = fb_env
        knobs["risk_dialog"] = (True, "login challenge")
        with pytest.raises(MessengerError) as ei:
            fb.send_message("Alice", "hello", raise_on_error=True)
        assert ei.value.code == "risk_detected"
        assert "login challenge" in ei.value.hint

    def test_search_ui_missing(self, fb_env):
        from src.app_automation.facebook import MessengerError
        fb, knobs = fb_env
        knobs["smart_tap_results"]["Search in Messenger"] = False
        with pytest.raises(MessengerError) as ei:
            fb.send_message("Alice", "hello", raise_on_error=True)
        assert ei.value.code == "search_ui_missing"

    def test_recipient_not_found(self, fb_env):
        from src.app_automation.facebook import MessengerError
        fb, knobs = fb_env
        knobs["smart_tap_results"]["First matching contact"] = False
        with pytest.raises(MessengerError) as ei:
            fb.send_message("Alice", "hello", raise_on_error=True)
        assert ei.value.code == "recipient_not_found"
        assert "Alice" in str(ei.value)

    def test_send_button_missing(self, fb_env):
        from src.app_automation.facebook import MessengerError
        fb, knobs = fb_env
        knobs["smart_tap_results"]["Send message button"] = False
        with pytest.raises(MessengerError) as ei:
            fb.send_message("Alice", "hello", raise_on_error=True)
        assert ei.value.code == "send_button_missing"

    def test_messenger_icon_fails_but_app_start_ok_no_raise(self, fb_env):
        """icon 点不开但 app_start 成功 → 不抛 messenger_unavailable,继续流程。"""
        fb, knobs = fb_env
        knobs["smart_tap_results"]["Messenger or chat"] = False
        knobs["app_start_raises"] = False
        assert fb.send_message("Alice", "hello", raise_on_error=True) is True


# ─── code 稳定性(契约测试) ──────────────────────────────────────────────────

class TestContractCodes:
    """这些 code 是对 A 机 A2 降级路径的公开契约,改名请先改 INTEGRATION_CONTRACT §二。"""

    def test_known_codes_set(self, fb_env):
        """扫描所有可能 code 输出,确认在契约列表里。"""
        from src.app_automation.facebook import MessengerError
        fb, knobs = fb_env

        known = {
            "messenger_unavailable", "xspace_blocked", "risk_detected",
            "search_ui_missing", "recipient_not_found", "send_button_missing",
            "send_fail",
        }
        # 遍历所有失败场景收集 code
        scenarios = [
            lambda k: (k["smart_tap_results"].__setitem__("Messenger or chat", False),
                      k.__setitem__("app_start_raises", True)),
            lambda k: (k.__setitem__("app_current_pkg", "com.miui.securitycore"),
                      k.__setitem__("handle_xspace_dismissed", False)),
            lambda k: k.__setitem__("risk_dialog", (True, "challenge")),
            lambda k: k["smart_tap_results"].__setitem__("Search in Messenger", False),
            lambda k: k["smart_tap_results"].__setitem__("First matching contact", False),
            lambda k: k["smart_tap_results"].__setitem__("Send message button", False),
        ]
        seen = set()
        for scenario in scenarios:
            # reset knobs
            knobs["smart_tap_results"] = {}
            knobs["app_current_pkg"] = "com.facebook.orca"
            knobs["xspace_sheet_visible"] = False
            knobs["risk_dialog"] = (False, "")
            knobs["handle_xspace_dismissed"] = True
            knobs["app_start_raises"] = False
            scenario(knobs)
            try:
                fb.send_message("Alice", "hello", raise_on_error=True)
            except MessengerError as e:
                seen.add(e.code)
        assert seen <= known, f"新 code {seen - known} 未写入契约表"
        assert len(seen) >= 6, f"只观察到 {seen}, 少于预期 6 种失败"
