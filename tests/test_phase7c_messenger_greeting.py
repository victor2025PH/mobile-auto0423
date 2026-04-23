# -*- coding: utf-8 -*-
"""Phase 7c: _send_messenger_greeting_to_peer 单测 (2026-04-24).

逻辑分支覆盖 (UI 用 FakeDevice 模拟, 不真碰设备):
  * Messenger app 启动失败 → messenger_unavailable
  * 搜索入口找不到 → search_ui_missing
  * 搜索无候选 → recipient_not_found
  * 对话页无输入框 → send_fail
  * Send 按钮找不到 → send_button_missing
  * 全 happy path → (True, "")
"""
from __future__ import annotations

import pytest


def _stub_fb():
    from src.app_automation.facebook import FacebookAutomation
    fb = FacebookAutomation.__new__(FacebookAutomation)
    fb._messenger_installed_cache = {}
    return fb


class FakeEl:
    def __init__(self, exists_ret: bool = True):
        self._exists = exists_ret

    def exists(self, timeout=0):
        return self._exists

    def click(self):
        return True

    def clear_text(self):
        return True

    def set_text(self, t):
        return True


class FakeDevice:
    """模拟 u2 Device. 通过 query_map 控制每种 selector 是否存在."""

    def __init__(self, *, cur_pkg_seq=None, xml_seq=None, el_map=None):
        self._pkgs = cur_pkg_seq or ["com.facebook.orca"]
        self._xml_seq = xml_seq or [""]
        self._el_map = el_map or {}
        self._xml_idx = 0
        self._pkg_idx = 0

    def app_stop(self, pkg):
        pass

    def app_start(self, pkg):
        pass

    def app_current(self):
        p = self._pkgs[min(self._pkg_idx, len(self._pkgs) - 1)]
        self._pkg_idx += 1
        return {"package": p}

    def dump_hierarchy(self):
        x = self._xml_seq[min(self._xml_idx, len(self._xml_seq) - 1)]
        self._xml_idx += 1
        return x

    def click(self, x, y):
        pass

    def __call__(self, **kwargs):
        key = tuple(sorted(kwargs.items()))
        if key in self._el_map:
            return self._el_map[key]
        # 默认 exists False
        return FakeEl(exists_ret=False)


# Happy-path xml: Messenger 搜索页含 EditText, 搜索后 4 候选
_HAPPY_SEARCH_XML = """<?xml version='1.0'?>
<hierarchy>
  <node index="0" class="android.widget.ImageView" text="" content-desc="山田花子" bounds="[0,388][720,527]" clickable="false" />
</hierarchy>"""


class TestMessengerFallbackPathways:

    def test_messenger_unavailable_when_app_not_foreground(self):
        fb = _stub_fb()
        fake = FakeDevice(cur_pkg_seq=["com.miui.home"])
        fb._u2 = lambda did=None: fake
        ok, code = fb._send_messenger_greeting_to_peer(
            did="D1", peer_name="山田花子", greeting="はじめまして")
        assert ok is False
        assert code == "messenger_unavailable"

    def test_search_ui_missing_when_no_search_entry(self):
        fb = _stub_fb()
        fake = FakeDevice(cur_pkg_seq=["com.facebook.orca"])
        # 不注册任何搜索入口 selector → 全 exists=False
        fb._u2 = lambda did=None: fake
        ok, code = fb._send_messenger_greeting_to_peer(
            did="D1", peer_name="山田花子", greeting="はじめまして")
        assert ok is False
        assert code == "search_ui_missing"

    def test_recipient_not_found_when_no_candidates(self):
        fb = _stub_fb()
        fake = FakeDevice(
            cur_pkg_seq=["com.facebook.orca"],
            xml_seq=["<?xml version='1.0'?><hierarchy></hierarchy>"],
            el_map={
                (("descriptionContains", "search"),): FakeEl(True),
                (("className", "android.widget.EditText"),): FakeEl(True),
            },
        )
        fb._u2 = lambda did=None: fake
        ok, code = fb._send_messenger_greeting_to_peer(
            did="D1", peer_name="山田花子", greeting="はじめまして")
        assert ok is False
        assert code == "recipient_not_found"

    def test_happy_path_returns_true(self):
        fb = _stub_fb()
        fake = FakeDevice(
            cur_pkg_seq=["com.facebook.orca"],
            xml_seq=[_HAPPY_SEARCH_XML],
            el_map={
                (("descriptionContains", "search"),): FakeEl(True),
                # 搜索输入框 + 对话输入框 (都被映射到同 selector)
                (("className", "android.widget.EditText"),): FakeEl(True),
                (("description", "Send"),): FakeEl(True),
            },
        )
        fb._u2 = lambda did=None: fake
        ok, code = fb._send_messenger_greeting_to_peer(
            did="D1", peer_name="山田花子", greeting="はじめまして")
        assert ok is True
        assert code == ""

    def test_send_button_missing(self):
        fb = _stub_fb()
        fake = FakeDevice(
            cur_pkg_seq=["com.facebook.orca"],
            xml_seq=[_HAPPY_SEARCH_XML],
            el_map={
                (("descriptionContains", "search"),): FakeEl(True),
                (("className", "android.widget.EditText"),): FakeEl(True),
                # 没注册 Send 按钮 → 全 exists=False
            },
        )
        fb._u2 = lambda did=None: fake
        ok, code = fb._send_messenger_greeting_to_peer(
            did="D1", peer_name="山田花子", greeting="はじめまして")
        assert ok is False
        assert code == "send_button_missing"
