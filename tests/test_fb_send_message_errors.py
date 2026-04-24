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
        "blocked_popup_text": "",  # F4: 非空时模拟点 Send 后 FB 拒绝
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
    # 2026-04-24: helper 方法 `_enter_messenger_search` / `_tap_messenger_send`
    # 在 fixture 里 mock 成"smart_tap 语义对齐" — smart_tap MISS 则 raise 对应
    # MessengerError, smart_tap HIT 则 no-op, 使现有以 smart_tap 为 knob 的测试
    # 仍然覆盖 helper 之后的三级 fallback 逻辑 (三级 fallback 本身另有
    # TestMessengerUIFallback 专门测)。
    def _enter_search_mock(_d, _did):
        if not _smart_tap("Search in Messenger", device_id=_did):
            from src.app_automation.facebook import MessengerError
            raise MessengerError(
                "search_ui_missing", "fixture-mock three-tier miss",
                hint="mocked fixture — 真实 fallback 路径见 TestMessengerUIFallback")

    def _tap_send_mock(_d, _did):
        if not _smart_tap("Send message button", device_id=_did):
            from src.app_automation.facebook import MessengerError
            raise MessengerError(
                "send_button_missing", "fixture-mock three-tier miss",
                hint="mocked fixture — 真实 fallback 路径见 TestMessengerUIFallback")

    stack.enter_context(patch.object(fb, "_enter_messenger_search",
                                     side_effect=_enter_search_mock))
    stack.enter_context(patch.object(fb, "_tap_messenger_send",
                                     side_effect=_tap_send_mock))
    stack.enter_context(patch.object(fb, "_focus_messenger_composer",
                                     return_value=False))
    stack.enter_context(patch.object(fb, "_dismiss_dialogs"))
    stack.enter_context(patch.object(fb, "_handle_xspace_dialog",
                                     side_effect=_handle_xspace))
    stack.enter_context(patch.object(fb, "_detect_risk_dialog",
                                     side_effect=_detect_risk))
    stack.enter_context(patch.object(fb, "_xspace_select_sheet_visible",
                                     side_effect=_xspace_visible))
    stack.enter_context(patch.object(fb, "_detect_send_blocked",
                                     side_effect=lambda _d: knobs["blocked_popup_text"]))
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

    def test_send_blocked_by_content(self, fb_env):
        """F4: Send 点击成功但 FB 弹拒绝提示 → send_blocked_by_content。"""
        from src.app_automation.facebook import MessengerError
        fb, knobs = fb_env
        knobs["blocked_popup_text"] = "This message can't be sent"
        with patch("src.host.fb_store.record_risk_event") as m_risk:
            with pytest.raises(MessengerError) as ei:
                fb.send_message("Alice", "hi", raise_on_error=True)
        assert ei.value.code == "send_blocked_by_content"
        assert "text_hash=" in ei.value.hint
        # record_risk_event 被调 (record_risk_event 内部会分类,
        # F4-support commit 在 followup PR 加的 content_blocked 规则生效后
        # 会归 kind='content_blocked')
        m_risk.assert_called_once()
        args, kwargs = m_risk.call_args
        assert args[0] == "devA"  # device_id
        assert "can't be sent" in args[1].lower()  # raw_message
        assert "task_id" in kwargs

    def test_send_blocked_by_content_no_raise_mode(self, fb_env):
        """raise_on_error=False 时 send_blocked_by_content 返 False 向后兼容。"""
        fb, knobs = fb_env
        knobs["blocked_popup_text"] = "message can't be sent"
        with patch("src.host.fb_store.record_risk_event"):
            assert fb.send_message("Alice", "hi") is False

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
            "send_blocked_by_content", "send_fail",
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
            lambda k: k.__setitem__("blocked_popup_text", "message can't be sent"),  # F4
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
            knobs["blocked_popup_text"] = ""
            scenario(knobs)
            try:
                with patch("src.host.fb_store.record_risk_event"):
                    fb.send_message("Alice", "hello", raise_on_error=True)
            except MessengerError as e:
                seen.add(e.code)
        assert seen <= known, f"新 code {seen - known} 未写入契约表"
        assert len(seen) >= 7, f"只观察到 {seen}, 少于预期 7 种失败"


# ─── F4: _detect_send_blocked 独立测试 ─────────────────────────────────────

class TestDetectSendBlocked:
    def test_empty_xml_returns_empty(self):
        from src.app_automation.facebook import FacebookAutomation
        fb = FacebookAutomation.__new__(FacebookAutomation)
        d = MagicMock()
        d.dump_hierarchy = MagicMock(return_value="")
        with patch("src.app_automation.facebook.time.sleep"):
            assert fb._detect_send_blocked(d) == ""

    def test_english_popup_detected(self):
        from src.app_automation.facebook import FacebookAutomation
        fb = FacebookAutomation.__new__(FacebookAutomation)
        d = MagicMock()
        d.dump_hierarchy = MagicMock(return_value=(
            "<node><text>This message can't be sent to this person</text></node>"
        ))
        with patch("src.app_automation.facebook.time.sleep"):
            r = fb._detect_send_blocked(d)
        assert r != ""
        assert "can't be sent" in r.lower()

    def test_chinese_popup_detected(self):
        from src.app_automation.facebook import FacebookAutomation
        fb = FacebookAutomation.__new__(FacebookAutomation)
        d = MagicMock()
        d.dump_hierarchy = MagicMock(
            return_value="<node>发送失败,请重试</node>")
        with patch("src.app_automation.facebook.time.sleep"):
            assert fb._detect_send_blocked(d) != ""

    def test_japanese_popup_detected(self):
        from src.app_automation.facebook import FacebookAutomation
        fb = FacebookAutomation.__new__(FacebookAutomation)
        d = MagicMock()
        d.dump_hierarchy = MagicMock(
            return_value="<node>送信できませんでした</node>")
        with patch("src.app_automation.facebook.time.sleep"):
            assert fb._detect_send_blocked(d) != ""

    def test_italian_popup_detected(self):
        from src.app_automation.facebook import FacebookAutomation
        fb = FacebookAutomation.__new__(FacebookAutomation)
        d = MagicMock()
        d.dump_hierarchy = MagicMock(
            return_value="<node>Messaggio non inviato</node>")
        with patch("src.app_automation.facebook.time.sleep"):
            assert fb._detect_send_blocked(d) != ""

    def test_normal_dump_no_false_positive(self):
        from src.app_automation.facebook import FacebookAutomation
        fb = FacebookAutomation.__new__(FacebookAutomation)
        d = MagicMock()
        d.dump_hierarchy = MagicMock(
            return_value="<node>Hello friend, how are you?</node>")
        with patch("src.app_automation.facebook.time.sleep"):
            assert fb._detect_send_blocked(d) == ""

    def test_dump_hierarchy_exception_returns_empty(self):
        from src.app_automation.facebook import FacebookAutomation
        fb = FacebookAutomation.__new__(FacebookAutomation)
        d = MagicMock()
        d.dump_hierarchy = MagicMock(side_effect=RuntimeError("disconnected"))
        with patch("src.app_automation.facebook.time.sleep"):
            assert fb._detect_send_blocked(d) == ""


# ─── Messenger UI 三级 fallback (2026-04-24 真机发现中文化失配) ─────────────

class TestMessengerUIFallback:
    """``_find_messenger_ui_fallback`` + ``_enter_messenger_search`` +
    ``_tap_messenger_send`` + ``_focus_messenger_composer`` — 修 2026 中文
    Messenger UI 导致 smart_tap MISS 的三级 fallback。"""

    # ── _find_messenger_ui_fallback ──────────────────────────────────

    def test_find_hits_first_selector(self):
        fb = _make_fb()
        d = MagicMock()
        obj = MagicMock(); obj.exists = MagicMock(return_value=True)
        d.return_value = obj
        assert fb._find_messenger_ui_fallback(
            d, ({"description": "A"}, {"description": "B"})) is obj
        assert d.call_count == 1  # 第 1 hit 即退出

    def test_find_hits_third_selector(self):
        fb = _make_fb()
        d = MagicMock()
        miss = MagicMock(); miss.exists = MagicMock(return_value=False)
        hit = MagicMock(); hit.exists = MagicMock(return_value=True)
        d.side_effect = [miss, miss, hit]
        assert fb._find_messenger_ui_fallback(
            d, ({"a": 1}, {"b": 2}, {"c": 3})) is hit
        assert d.call_count == 3

    def test_find_all_miss_returns_none(self):
        fb = _make_fb()
        d = MagicMock()
        miss = MagicMock(); miss.exists = MagicMock(return_value=False)
        d.return_value = miss
        assert fb._find_messenger_ui_fallback(
            d, ({"x": 1}, {"y": 2})) is None

    def test_find_empty_selectors(self):
        fb = _make_fb()
        assert fb._find_messenger_ui_fallback(MagicMock(), ()) is None

    def test_find_selector_exception_continues(self):
        """某 selector 抛异常, 工具继续 try 下一个。"""
        fb = _make_fb()
        d = MagicMock()
        hit = MagicMock(); hit.exists = MagicMock(return_value=True)
        d.side_effect = [RuntimeError("boom"), hit]
        assert fb._find_messenger_ui_fallback(
            d, ({"a": 1}, {"b": 2})) is hit

    # ── _enter_messenger_search 三级 fallback ─────────────────────────

    def test_enter_search_smart_tap_hit_returns(self):
        fb = _make_fb()
        fb.smart_tap = MagicMock(return_value=True)
        fb._enter_messenger_search(MagicMock(), "devA")  # 不抛
        fb.smart_tap.assert_called_once_with(
            "Search in Messenger", device_id="devA")

    def test_enter_search_multi_locale_hit(self):
        fb = _make_fb()
        fb.smart_tap = MagicMock(return_value=False)
        d = MagicMock()
        obj = MagicMock(); obj.exists = MagicMock(return_value=True)
        d.return_value = obj
        with patch("src.app_automation.facebook.time.sleep"):
            fb._enter_messenger_search(d, "devA")
        obj.click.assert_called_once()

    def test_enter_search_coordinate_fallback_ok(self):
        """smart_tap + 所有 selector MISS → coordinate click → EditText 出现 → 返回。"""
        fb = _make_fb()
        fb.smart_tap = MagicMock(return_value=False)
        d = MagicMock()
        d.window_size = MagicMock(return_value=(720, 1600))
        miss = MagicMock(); miss.exists = MagicMock(return_value=False)
        edit = MagicMock(); edit.exists = MagicMock(return_value=True)

        def _sel(**kw):
            if kw.get("className") == "android.widget.EditText":
                return edit
            return miss
        d.side_effect = _sel
        with patch("src.app_automation.facebook.time.sleep"):
            fb._enter_messenger_search(d, "devA")
        d.click.assert_called_once_with(360, int(1600 * 0.20))

    def test_enter_search_all_three_miss_raises(self):
        from src.app_automation.facebook import MessengerError
        fb = _make_fb()
        fb.smart_tap = MagicMock(return_value=False)
        d = MagicMock()
        d.window_size = MagicMock(return_value=(720, 1600))
        miss = MagicMock(); miss.exists = MagicMock(return_value=False)
        d.return_value = miss
        with patch("src.app_automation.facebook.time.sleep"):
            with pytest.raises(MessengerError) as ei:
                fb._enter_messenger_search(d, "devA")
        assert ei.value.code == "search_ui_missing"
        # 2026-04-24: raise 消息升到 "4 级 fallback" (含 VLM Level 4);
        # 老测试本质是"三级 fallback 全 miss 时 raise", 现在 Level 4 也 miss
        # 时 raise, 语义保持。断言通用 "fallback" + 含 "4 级"。
        assert "fallback" in str(ei.value)
        assert "4 级" in str(ei.value)

    # ── _tap_messenger_send 三级 fallback ───────────────────────────

    def test_tap_send_smart_tap_hit(self):
        fb = _make_fb()
        fb.smart_tap = MagicMock(return_value=True)
        fb._tap_messenger_send(MagicMock(), "devA")
        fb.smart_tap.assert_called_once_with(
            "Send message button", device_id="devA")

    def test_tap_send_multi_locale_hit(self):
        fb = _make_fb()
        fb.smart_tap = MagicMock(return_value=False)
        d = MagicMock()
        obj = MagicMock(); obj.exists = MagicMock(return_value=True)
        d.return_value = obj
        fb._tap_messenger_send(d, "devA")
        obj.click.assert_called_once()

    def test_tap_send_coordinate_fallback(self):
        fb = _make_fb()
        fb.smart_tap = MagicMock(return_value=False)
        d = MagicMock()
        d.window_size = MagicMock(return_value=(720, 1600))
        miss = MagicMock(); miss.exists = MagicMock(return_value=False)
        d.return_value = miss
        fb._tap_messenger_send(d, "devA")
        d.click.assert_called_once_with(
            int(720 * 0.93), int(1600 * 0.91))

    def test_tap_send_all_three_miss_raises(self):
        """coordinate fallback 里 ``d.window_size`` 抛 → 三级都挂 → raise."""
        from src.app_automation.facebook import MessengerError
        fb = _make_fb()
        fb.smart_tap = MagicMock(return_value=False)
        d = MagicMock()
        d.window_size = MagicMock(side_effect=RuntimeError("boom"))
        miss = MagicMock(); miss.exists = MagicMock(return_value=False)
        d.return_value = miss
        with pytest.raises(MessengerError) as ei:
            fb._tap_messenger_send(d, "devA")
        assert ei.value.code == "send_button_missing"

    # ── _focus_messenger_composer (safety, 不抛) ─────────────────────

    def test_focus_composer_hit_returns_true(self):
        fb = _make_fb()
        d = MagicMock()
        obj = MagicMock(); obj.exists = MagicMock(return_value=True)
        d.return_value = obj
        with patch("src.app_automation.facebook.time.sleep"):
            assert fb._focus_messenger_composer(d) is True
        obj.click.assert_called_once()

    def test_focus_composer_miss_returns_false(self):
        """composer selector 全 miss → 返回 False, 不抛 (不阻塞后续 type_text)。"""
        fb = _make_fb()
        d = MagicMock()
        miss = MagicMock(); miss.exists = MagicMock(return_value=False)
        d.return_value = miss
        assert fb._focus_messenger_composer(d) is False

    def test_focus_composer_click_exception_returns_false(self):
        """composer click 抛异常 → 返回 False 不重抛 (type_text 仍能跑)。"""
        fb = _make_fb()
        d = MagicMock()
        obj = MagicMock()
        obj.exists = MagicMock(return_value=True)
        obj.click = MagicMock(side_effect=RuntimeError("boom"))
        d.return_value = obj
        with patch("src.app_automation.facebook.time.sleep"):
            assert fb._focus_messenger_composer(d) is False


# ─── Level 4 VLM vision fallback (2026-04-24 对抗 Messenger 2026 Compose UI) ─

class TestMessengerUIVLMLevel4:
    """第 4 级 VLM vision fallback — 前 3 级 (smart_tap + multi-locale +
    coordinate) 全 miss 时用 VisionFallback 图像识别兜底。复用
    src/ai/vision_fallback.py (免费 Gemini/Ollama provider)。"""

    def _mk_vf(self, coords=None, raises=False, returns_none=False):
        """造 mock VisionFallback: 可控 find_element 返回。"""
        vf = MagicMock()
        if raises:
            vf.find_element = MagicMock(side_effect=RuntimeError("vlm boom"))
        elif returns_none or coords is None:
            vf.find_element = MagicMock(return_value=None)
        else:
            from src.ai.vision_fallback import VisionResult
            result = VisionResult(coordinates=coords, confidence="high")
            vf.find_element = MagicMock(return_value=result)
        return vf

    # ── _enter_messenger_search Level 4 ────────────────────────────

    def test_enter_search_vlm_hit_after_3_miss(self):
        """前 3 级 miss + VLM 返 coordinates + click 后 EditText 出现 → return。"""
        from src.app_automation import facebook as fb_mod
        from src.app_automation.facebook import FacebookAutomation
        fb = FacebookAutomation.__new__(FacebookAutomation)
        fb.smart_tap = MagicMock(return_value=False)
        d = MagicMock()
        d.window_size = MagicMock(return_value=(720, 1600))
        call_count = {"n": 0}

        def _sel(**kw):
            obj = MagicMock()
            if kw.get("className") == "android.widget.EditText":
                # coord (level 3) check: miss; VLM (level 4) post-click: hit
                call_count["n"] += 1
                obj.exists = MagicMock(return_value=call_count["n"] >= 2)
            else:
                obj.exists = MagicMock(return_value=False)
            return obj
        d.side_effect = _sel

        mock_vf = self._mk_vf(coords=(360, 280))
        with patch.object(fb_mod, "_get_vision_fallback",
                          return_value=mock_vf), \
             patch("src.app_automation.facebook.time.sleep"):
            fb._enter_messenger_search(d, "devA")
        mock_vf.find_element.assert_called_once()
        # VLM click @ (360, 280) 应 called
        calls = [c.args for c in d.click.call_args_list]
        assert (360, 280) in calls

    def test_enter_search_vlm_miss_raises(self):
        """前 3 级 + VLM 返 None → raise search_ui_missing 带 "4 级" hint。"""
        from src.app_automation import facebook as fb_mod
        from src.app_automation.facebook import (
            FacebookAutomation, MessengerError)
        fb = FacebookAutomation.__new__(FacebookAutomation)
        fb.smart_tap = MagicMock(return_value=False)
        d = MagicMock()
        d.window_size = MagicMock(return_value=(720, 1600))
        miss = MagicMock(); miss.exists = MagicMock(return_value=False)
        d.return_value = miss

        mock_vf = self._mk_vf(returns_none=True)
        with patch.object(fb_mod, "_get_vision_fallback",
                          return_value=mock_vf), \
             patch("src.app_automation.facebook.time.sleep"):
            with pytest.raises(MessengerError) as ei:
                fb._enter_messenger_search(d, "devA")
        assert ei.value.code == "search_ui_missing"
        assert "4 级" in str(ei.value)
        mock_vf.find_element.assert_called_once()

    def test_enter_search_vlm_provider_unavailable(self):
        """无 VLM provider → raise 4 级 (Level 4 跳过)。"""
        from src.app_automation import facebook as fb_mod
        from src.app_automation.facebook import (
            FacebookAutomation, MessengerError)
        fb = FacebookAutomation.__new__(FacebookAutomation)
        fb.smart_tap = MagicMock(return_value=False)
        d = MagicMock()
        d.window_size = MagicMock(return_value=(720, 1600))
        miss = MagicMock(); miss.exists = MagicMock(return_value=False)
        d.return_value = miss

        with patch.object(fb_mod, "_get_vision_fallback",
                          return_value=None), \
             patch("src.app_automation.facebook.time.sleep"):
            with pytest.raises(MessengerError) as ei:
                fb._enter_messenger_search(d, "devA")
        assert ei.value.code == "search_ui_missing"

    def test_enter_search_vlm_exception_raises(self):
        """VLM find_element 抛异常 → 不 bubble, 降为 search_ui_missing raise。"""
        from src.app_automation import facebook as fb_mod
        from src.app_automation.facebook import (
            FacebookAutomation, MessengerError)
        fb = FacebookAutomation.__new__(FacebookAutomation)
        fb.smart_tap = MagicMock(return_value=False)
        d = MagicMock()
        d.window_size = MagicMock(return_value=(720, 1600))
        miss = MagicMock(); miss.exists = MagicMock(return_value=False)
        d.return_value = miss

        mock_vf = self._mk_vf(raises=True)
        with patch.object(fb_mod, "_get_vision_fallback",
                          return_value=mock_vf), \
             patch("src.app_automation.facebook.time.sleep"):
            with pytest.raises(MessengerError) as ei:
                fb._enter_messenger_search(d, "devA")
        assert ei.value.code == "search_ui_missing"

    # ── _tap_messenger_send Level 4 ────────────────────────────────

    def test_tap_send_vlm_hit_after_3_miss(self):
        """send 的 VLM 命中路径 (coord level 异常触发 Level 4)。"""
        from src.app_automation import facebook as fb_mod
        from src.app_automation.facebook import FacebookAutomation
        fb = FacebookAutomation.__new__(FacebookAutomation)
        fb.smart_tap = MagicMock(return_value=False)
        d = MagicMock()
        d.window_size = MagicMock(side_effect=RuntimeError("boom"))
        miss = MagicMock(); miss.exists = MagicMock(return_value=False)
        d.return_value = miss

        mock_vf = self._mk_vf(coords=(670, 1460))
        with patch.object(fb_mod, "_get_vision_fallback",
                          return_value=mock_vf):
            fb._tap_messenger_send(d, "devA")
        mock_vf.find_element.assert_called_once()
        d.click.assert_called_with(670, 1460)

    def test_tap_send_vlm_miss_raises(self):
        """前 3 级 + VLM miss → raise send_button_missing 带 "4 级"。"""
        from src.app_automation import facebook as fb_mod
        from src.app_automation.facebook import (
            FacebookAutomation, MessengerError)
        fb = FacebookAutomation.__new__(FacebookAutomation)
        fb.smart_tap = MagicMock(return_value=False)
        d = MagicMock()
        d.window_size = MagicMock(side_effect=RuntimeError("boom"))
        miss = MagicMock(); miss.exists = MagicMock(return_value=False)
        d.return_value = miss

        mock_vf = self._mk_vf(returns_none=True)
        with patch.object(fb_mod, "_get_vision_fallback",
                          return_value=mock_vf):
            with pytest.raises(MessengerError) as ei:
                fb._tap_messenger_send(d, "devA")
        assert ei.value.code == "send_button_missing"
        assert "4 级" in str(ei.value)

    # ── _get_vision_fallback lazy init ─────────────────────────────

    def test_get_vision_fallback_no_provider_returns_none(self):
        """get_free_vision_client 返 None → lazy init 返 None + 不重试。"""
        from src.app_automation import facebook as fb_mod
        fb_mod._vision_fallback_instance = None
        fb_mod._vision_fallback_init_attempted = False
        try:
            with patch("src.ai.llm_client.get_free_vision_client",
                       return_value=None):
                r1 = fb_mod._get_vision_fallback()
                r2 = fb_mod._get_vision_fallback()
            assert r1 is None and r2 is None
            assert fb_mod._vision_fallback_init_attempted is True
        finally:
            # reset module state for subsequent tests
            fb_mod._vision_fallback_instance = None
            fb_mod._vision_fallback_init_attempted = False

    def test_get_vision_fallback_init_exception_returns_none(self):
        """get_free_vision_client 抛异常 → lazy init 返 None 不重试。"""
        from src.app_automation import facebook as fb_mod
        fb_mod._vision_fallback_instance = None
        fb_mod._vision_fallback_init_attempted = False
        try:
            with patch("src.ai.llm_client.get_free_vision_client",
                       side_effect=RuntimeError("import boom")):
                r = fb_mod._get_vision_fallback()
            assert r is None
            assert fb_mod._vision_fallback_init_attempted is True
        finally:
            fb_mod._vision_fallback_instance = None
            fb_mod._vision_fallback_init_attempted = False
