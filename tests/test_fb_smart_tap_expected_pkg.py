# -*- coding: utf-8 -*-
"""OPT-7 (2026-04-28) — smart_tap expected_pkg 关键字单测。

动机: 真机 e2e 跑 attach_image dry_run 时观测到日志噪音
  [smart_tap-heal] tap 'Open photo gallery' 后 app 漂移:
    com.facebook.orca != com.facebook.katana, 启动自愈

attach_image 在 Messenger (orca) 内操作, 期望 current_pkg = orca, 但
heal 默认对比 PACKAGE = katana. 该误判触发多余 BACK + 重启 FB, 多增
1-3s 延迟。OPT-7 给 smart_tap 加 expected_pkg kwarg, 显式传 caller
期望的 pkg, heal 用该值对比。

兼容性: kwarg-only + 默认 None (回退 PACKAGE), send_message 等已稳定的
caller 不需修改。
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


def _make_fb_with_super_smart_tap(super_returns: bool):
    """构造 fb 对象 + 让 super().smart_tap 返回指定值, 不调真 adb。"""
    from src.app_automation.facebook import FacebookAutomation
    fb = FacebookAutomation.__new__(FacebookAutomation)
    fb._did = lambda x=None: x or "DEVICE-FAKE"
    fb._handle_xspace_dialog = MagicMock()
    fb._adb = MagicMock()
    fb._adb_start_main_user = MagicMock()
    return fb


def _build_u2(current_pkg: str):
    u2 = MagicMock()
    u2.app_current = MagicMock(return_value={"package": current_pkg})
    u2.invalidate_app_cache = MagicMock()
    return u2


# ════════════════════════════════════════════════════════════════════════
# expected_pkg 行为
# ════════════════════════════════════════════════════════════════════════

class TestExpectedPkgKwarg:
    """smart_tap 加 expected_pkg 关键字, heal 检查用此 pkg 而非默认 katana。"""

    def test_default_no_expected_pkg_uses_katana(self):
        """不传 expected_pkg 时 heal 用 PACKAGE (katana) — backward compat。"""
        fb = _make_fb_with_super_smart_tap(True)
        u2 = _build_u2("com.facebook.katana")
        fb._u2 = lambda did: u2
        with patch("src.app_automation.facebook.time.sleep"), \
             patch.object(
                 type(fb).__mro__[1], "smart_tap", return_value=True):
            ok = fb.smart_tap("Search bar", device_id="D1")
        assert ok is True
        # 当前 pkg 是 katana == PACKAGE → heal 不触发
        fb._handle_xspace_dialog.assert_not_called()
        fb._adb.assert_not_called()

    def test_expected_pkg_orca_no_heal_when_in_orca(self):
        """expected_pkg=MESSENGER_PACKAGE + 当前 orca → 不触发 heal。"""
        from src.app_automation.facebook import MESSENGER_PACKAGE
        fb = _make_fb_with_super_smart_tap(True)
        u2 = _build_u2(MESSENGER_PACKAGE)  # orca
        fb._u2 = lambda did: u2
        with patch("src.app_automation.facebook.time.sleep"), \
             patch.object(
                 type(fb).__mro__[1], "smart_tap", return_value=True):
            ok = fb.smart_tap("Open photo gallery", device_id="D1",
                              expected_pkg=MESSENGER_PACKAGE)
        assert ok is True
        # heal 不触发 (orca == expected_pkg)
        fb._handle_xspace_dialog.assert_not_called()
        fb._adb.assert_not_called()

    def test_expected_pkg_katana_but_in_orca_triggers_heal(self):
        """expected_pkg=PACKAGE (默认) + 当前 orca → heal 应该触发 (现有行为)。"""
        from src.app_automation.facebook import PACKAGE
        fb = _make_fb_with_super_smart_tap(True)
        # 第一次 app_current 返 orca → 触发 heal
        # heal 内会调多次 app_current — 让所有调用都返 orca (永不回到 katana)
        u2 = _build_u2("com.facebook.orca")
        fb._u2 = lambda did: u2
        with patch("src.app_automation.facebook.time.sleep"), \
             patch.object(
                 type(fb).__mro__[1], "smart_tap", return_value=True):
            ok = fb.smart_tap("Search bar", device_id="D1")
        # heal 触发但自愈失败 (current 始终 orca != katana) → 返 False
        assert ok is False
        fb._handle_xspace_dialog.assert_called()  # heal 启动

    def test_expected_pkg_orca_but_in_katana_triggers_heal(self):
        """expected_pkg=orca + 当前 katana → heal 应触发 (反向场景)。"""
        from src.app_automation.facebook import MESSENGER_PACKAGE
        fb = _make_fb_with_super_smart_tap(True)
        u2 = _build_u2("com.facebook.katana")
        fb._u2 = lambda did: u2
        with patch("src.app_automation.facebook.time.sleep"), \
             patch.object(
                 type(fb).__mro__[1], "smart_tap", return_value=True):
            ok = fb.smart_tap("Open photo gallery", device_id="D1",
                              expected_pkg=MESSENGER_PACKAGE)
        # 当前 katana != expected MESSENGER_PACKAGE → heal 触发但失败
        assert ok is False
        fb._handle_xspace_dialog.assert_called()

    def test_expected_pkg_none_treated_same_as_default(self):
        """expected_pkg=None 显式传 = 不传, 都默认 katana。"""
        fb = _make_fb_with_super_smart_tap(True)
        u2 = _build_u2("com.facebook.katana")
        fb._u2 = lambda did: u2
        with patch("src.app_automation.facebook.time.sleep"), \
             patch.object(
                 type(fb).__mro__[1], "smart_tap", return_value=True):
            ok = fb.smart_tap("Search bar", device_id="D1",
                              expected_pkg=None)
        assert ok is True
        fb._handle_xspace_dialog.assert_not_called()


# ════════════════════════════════════════════════════════════════════════
# 集成 — _open_messenger_photo_gallery 应传 expected_pkg=MESSENGER_PACKAGE
# ════════════════════════════════════════════════════════════════════════

class TestAttachImageCallsSmartTapWithExpectedPkg:
    """attach_image 路径调 smart_tap 应该显式传 expected_pkg=MESSENGER_PACKAGE。"""

    def test_open_messenger_photo_gallery_passes_messenger_package(self):
        from src.app_automation.facebook import (
            FacebookAutomation, MESSENGER_PACKAGE,
        )
        fb = FacebookAutomation.__new__(FacebookAutomation)
        fb.smart_tap = MagicMock(return_value=True)
        fb._did = lambda x=None: x or "D1"
        d = MagicMock()
        d.window_size = lambda: (720, 1438)
        fb._u2 = lambda did: d

        fb._open_messenger_photo_gallery(d, "D1")

        # smart_tap 调用应传 expected_pkg=MESSENGER_PACKAGE
        kwargs = fb.smart_tap.call_args.kwargs
        assert kwargs.get("expected_pkg") == MESSENGER_PACKAGE


# ════════════════════════════════════════════════════════════════════════
# OPT-7-v2: send_message 链路 4 处 smart_tap caller 都应透传 expected_pkg
# ════════════════════════════════════════════════════════════════════════

class TestOpt7v2SendMessageChainPassesExpectedPkg:
    """OPT-7-v2 (2026-04-28): send_message 全链路在 orca 内 tap 都应传
    expected_pkg=MESSENGER_PACKAGE 修 heal 误报. 改 4 处:
      - send_message_impl line ~1734: "Messenger or chat icon" (从 katana
        切 orca 的过渡, tap 后期望 orca)
      - _enter_messenger_search: "Search in Messenger" (orca 内)
      - _tap_messenger_send: "Send message button" (orca composer 内)
      - _tap_first_search_result: "First matching contact" (orca 搜索内)

    每改一处可能 break 现有 send_message_errors 测试 (assert_called_once_with
    精确参数), 必须同步更新测试 — 故 4 处分别有契约保护单测."""

    def test_enter_messenger_search_l1_passes_expected_pkg(self):
        """_enter_messenger_search L1 smart_tap 必须传 expected_pkg=
        MESSENGER_PACKAGE, 否则 heal 误判 orca 为漂移."""
        from src.app_automation.facebook import (
            FacebookAutomation, MESSENGER_PACKAGE,
        )
        fb = FacebookAutomation.__new__(FacebookAutomation)
        fb.smart_tap = MagicMock(return_value=True)
        fb._enter_messenger_search(MagicMock(), "devA")
        kwargs = fb.smart_tap.call_args.kwargs
        assert kwargs.get("expected_pkg") == MESSENGER_PACKAGE

    def test_tap_messenger_send_l1_passes_expected_pkg(self):
        from src.app_automation.facebook import (
            FacebookAutomation, MESSENGER_PACKAGE,
        )
        fb = FacebookAutomation.__new__(FacebookAutomation)
        fb.smart_tap = MagicMock(return_value=True)
        fb._tap_messenger_send(MagicMock(), "devA")
        kwargs = fb.smart_tap.call_args.kwargs
        assert kwargs.get("expected_pkg") == MESSENGER_PACKAGE

    def test_tap_first_search_result_l1_passes_expected_pkg(self):
        from src.app_automation.facebook import (
            FacebookAutomation, MESSENGER_PACKAGE,
        )
        fb = FacebookAutomation.__new__(FacebookAutomation)
        fb.smart_tap = MagicMock(return_value=True)
        fb._tap_first_search_result(MagicMock(), "devA", "Alice")
        kwargs = fb.smart_tap.call_args.kwargs
        assert kwargs.get("expected_pkg") == MESSENGER_PACKAGE

    def test_send_message_impl_messenger_icon_passes_expected_pkg(self):
        """send_message_impl 第一步 'Messenger or chat icon' tap 也应传
        expected_pkg=MESSENGER_PACKAGE — tap 后期望切到 orca, heal 之前
        默认 katana 误判会触发不必要的 BACK + 重启 FB. 这是 send_message
        每条消息浪费 1-3s 的根因."""
        # 直接 grep 源码找该 caller 是否传了 kwarg (避免跑完整 send_message_impl)
        import inspect
        from src.app_automation.facebook import FacebookAutomation
        src = inspect.getsource(FacebookAutomation._send_message_impl)
        # 应该含 'Messenger or chat icon' 调用 + expected_pkg=MESSENGER_PACKAGE
        assert "Messenger or chat icon" in src
        # 找该 smart_tap 调用块, 确保 expected_pkg= 在附近 (5 行内)
        idx = src.find("Messenger or chat icon")
        nearby = src[max(0, idx - 200):idx + 300]
        assert "expected_pkg" in nearby and "MESSENGER_PACKAGE" in nearby, (
            f"send_message_impl 'Messenger or chat icon' smart_tap 缺 "
            f"expected_pkg=MESSENGER_PACKAGE; 附近代码:\n{nearby}"
        )
