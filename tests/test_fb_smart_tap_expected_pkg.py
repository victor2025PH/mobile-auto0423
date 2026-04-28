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
