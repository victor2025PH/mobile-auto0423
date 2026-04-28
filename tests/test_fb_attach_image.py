# -*- coding: utf-8 -*-
"""D1-A `attach_image` 单元测试 — 覆盖 6 档错误码 + 成功路径 + selector 顺序。

D1-A 主真机摸底数据 (IJ8H Redmi 13C / Android 13 / orca 556.0.0.60.64,
2026-04-28):
  - 对话页底部 "Open photo gallery." button content-desc 稳定
  - photo picker 内每张图 content-desc = "Photo taken DD MMM YYYY"
  - resource-id 全被 ProGuard 混淆 → 只能用 desc/text

不测设备层, 所有 adb/u2/dm/hb 交互都 patch 掉; 真机集成另走 e2e。
"""
from __future__ import annotations

import os
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest


def _make_fb():
    from src.app_automation.facebook import FacebookAutomation
    fb = FacebookAutomation.__new__(FacebookAutomation)
    fb.hb = MagicMock()
    fb.hb.type_text = MagicMock()
    fb.hb.wait_think = MagicMock()
    fb.dm = MagicMock()
    return fb


@pytest.fixture
def fb_env(tmp_path):
    """Yield (fb, knobs, image_path) — 默认全成功路径 knobs。"""
    fb = _make_fb()
    img = tmp_path / "qr.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)  # minimal PNG-ish

    knobs = {
        "grant_results": [(True, "Success\n"), (True, "Success\n")],
        "push_result": (True, "1 file pushed."),
        "scan_result": (True, ""),
        "smart_tap_gallery": False,  # 让 multi-locale 走起
        "gallery_obj_exists": True,
        "photo_obj_exists": True,
        "photo_retry_succeeds_at": 1,  # 第几次 retry 命中 (1 = 第一次)
        "tap_send_raises": None,
    }

    def _exec_adb(cmd, did, timeout=None):
        if cmd[:2] == ["shell", "pm"]:
            return knobs["grant_results"].pop(0) if knobs["grant_results"] else (True, "")
        if cmd[0] == "push":
            return knobs["push_result"]
        if cmd[:2] == ["shell", "am"]:
            return knobs["scan_result"]
        return (True, "")
    fb.dm.execute_adb_command = MagicMock(side_effect=_exec_adb)

    def _smart_tap(target, device_id=None, **kw):
        if "photo gallery" in target.lower():
            return knobs["smart_tap_gallery"]
        return False
    fb.smart_tap = MagicMock(side_effect=_smart_tap)
    fb._find_messenger_ui_fallback = MagicMock(
        side_effect=lambda d, sels: MagicMock() if knobs["gallery_obj_exists"] else None
    )

    fake_u2 = MagicMock()
    fake_u2.window_size = lambda: (720, 1438)
    # photo selector retry: 让前 N-1 次 miss, 第 N 次命中
    photo_call_count = {"n": 0}

    def _u2_call(**kwargs):
        # 模拟 d(descriptionMatches=...) 调用
        photo_call_count["n"] += 1
        m = MagicMock()
        if knobs["photo_obj_exists"] and \
                photo_call_count["n"] >= knobs["photo_retry_succeeds_at"]:
            m.exists = lambda timeout=0: True
        else:
            m.exists = lambda timeout=0: False
        return m
    fake_u2.side_effect = _u2_call
    # u2 device 也是 callable (d(...))
    fake_u2.__call__ = _u2_call

    fb._did = lambda did=None: did or "DEVICE-FAKE"
    fb._u2 = lambda did: fake_u2

    fb.guarded = MagicMock()
    fb.guarded.return_value.__enter__ = MagicMock(return_value=None)
    fb.guarded.return_value.__exit__ = MagicMock(return_value=False)

    fb._focus_messenger_composer = MagicMock()
    fb.rewrite_message = lambda text, ctx: text

    if knobs["tap_send_raises"] is None:
        fb._tap_messenger_send = MagicMock()
    else:
        fb._tap_messenger_send = MagicMock(side_effect=knobs["tap_send_raises"])

    return fb, knobs, str(img)


# ════════════════════════════════════════════════════════════════════════
# AttachImageError 类基本语义
# ════════════════════════════════════════════════════════════════════════

class TestAttachImageErrorClass:
    def test_code_and_hint_stored(self):
        from src.app_automation.facebook import AttachImageError
        e = AttachImageError("push_failed", "msg here", hint="check disk")
        assert e.code == "push_failed"
        assert e.hint == "check disk"
        assert "msg here" in str(e)

    def test_repr_includes_code(self):
        from src.app_automation.facebook import AttachImageError
        e = AttachImageError("picker_empty")
        assert "picker_empty" in repr(e)

    def test_default_message_falls_back_to_code(self):
        from src.app_automation.facebook import AttachImageError
        e = AttachImageError("send_failed")
        assert "send_failed" in str(e)


# ════════════════════════════════════════════════════════════════════════
# Selector 常量稳定性 (公开契约 — 真机摸底产物)
# ════════════════════════════════════════════════════════════════════════

class TestSelectorContracts:
    def test_gallery_selector_includes_realtap_data(self):
        """实测真机命中的 'Open photo gallery.' 必须在 selector tuple 第一档。"""
        from src.app_automation.facebook import FacebookAutomation
        first = FacebookAutomation._MESSENGER_ATTACH_GALLERY_SELECTORS[0]
        assert first == {"description": "Open photo gallery."}

    def test_gallery_selector_covers_zh_ja_ko(self):
        from src.app_automation.facebook import FacebookAutomation
        descs = [s.get("description", "") + s.get("descriptionContains", "")
                 for s in FacebookAutomation._MESSENGER_ATTACH_GALLERY_SELECTORS]
        assert any("图库" in d or "相簿" in d for d in descs), "需 zh selector"
        assert any("ギャラリー" in d for d in descs), "需 ja selector"
        assert any("갤러리" in d for d in descs), "需 ko selector"

    def test_photo_selector_uses_date_regex_first(self):
        """真机 picker 实测每张图 desc = 'Photo taken DD MMM YYYY' — 必须
        是第一档 selector。"""
        from src.app_automation.facebook import FacebookAutomation
        first = FacebookAutomation._MESSENGER_PHOTO_NODE_SELECTORS[0]
        assert "descriptionMatches" in first
        assert "Photo taken" in first["descriptionMatches"]


# ════════════════════════════════════════════════════════════════════════
# attach_image 公共 API: raise_on_error 行为
# ════════════════════════════════════════════════════════════════════════

class TestPublicApi:
    def test_success_returns_true(self, fb_env):
        fb, knobs, img = fb_env
        assert fb.attach_image(img, device_id="D1") is True

    def test_silent_mode_returns_false_on_error(self, fb_env):
        fb, knobs, img = fb_env
        knobs["push_result"] = (False, "No space left")
        assert fb.attach_image(img, device_id="D1") is False

    def test_raise_mode_propagates_attach_image_error(self, fb_env):
        fb, knobs, img = fb_env
        from src.app_automation.facebook import AttachImageError
        knobs["push_result"] = (False, "No space left")
        with pytest.raises(AttachImageError) as exc:
            fb.attach_image(img, device_id="D1", raise_on_error=True)
        assert exc.value.code == "push_failed"


# ════════════════════════════════════════════════════════════════════════
# 错误归因
# ════════════════════════════════════════════════════════════════════════

class TestErrorCodes:
    def test_local_image_missing_raises_push_failed(self, fb_env):
        fb, knobs, img = fb_env
        from src.app_automation.facebook import AttachImageError
        with pytest.raises(AttachImageError) as exc:
            fb.attach_image("/no/such/file.png", device_id="D1",
                            raise_on_error=True)
        assert exc.value.code == "push_failed"

    def test_adb_push_failure_raises_push_failed(self, fb_env):
        fb, knobs, img = fb_env
        from src.app_automation.facebook import AttachImageError
        knobs["push_result"] = (False, "remote object cannot be created")
        with pytest.raises(AttachImageError) as exc:
            fb.attach_image(img, device_id="D1", raise_on_error=True)
        assert exc.value.code == "push_failed"

    def test_gallery_button_all_miss_raises(self, fb_env):
        fb, knobs, img = fb_env
        from src.app_automation.facebook import AttachImageError
        knobs["smart_tap_gallery"] = False
        knobs["gallery_obj_exists"] = False
        # window_size 走 coordinate fallback 不抛 — 但 picker_empty 会
        # 让我们捕获到失败; 这里把 _u2 的 click 也 mock 让 coordinate
        # fallback "成功" (避免 coordinate 抛异常掩盖语义)
        # 然后 photo selector 全 miss → picker_empty
        knobs["photo_obj_exists"] = False
        with pytest.raises(AttachImageError) as exc:
            fb.attach_image(img, device_id="D1", raise_on_error=True)
        # coordinate fallback 不会抛 gallery_button_missing — 它是兜底
        # 成功; 后续 picker dump 不到节点抛 picker_empty
        assert exc.value.code == "picker_empty"

    def test_gallery_button_coordinate_failure_raises(self, fb_env):
        """三级 fallback 全 fail (smart_tap False + multi-locale None +
        coordinate 抛异常) 抛 gallery_button_missing。"""
        fb, knobs, img = fb_env
        from src.app_automation.facebook import AttachImageError
        knobs["smart_tap_gallery"] = False
        knobs["gallery_obj_exists"] = False
        # 让 window_size 抛
        bad_u2 = MagicMock()
        bad_u2.window_size = MagicMock(side_effect=RuntimeError("u2 dead"))
        bad_u2.side_effect = lambda **kw: MagicMock(
            exists=lambda timeout=0: False)
        fb._u2 = lambda did: bad_u2
        with pytest.raises(AttachImageError) as exc:
            fb.attach_image(img, device_id="D1", raise_on_error=True)
        assert exc.value.code == "gallery_button_missing"

    def test_picker_empty_after_3_retries(self, fb_env):
        fb, knobs, img = fb_env
        from src.app_automation.facebook import AttachImageError
        knobs["photo_obj_exists"] = False
        with pytest.raises(AttachImageError) as exc:
            fb.attach_image(img, device_id="D1", raise_on_error=True)
        assert exc.value.code == "picker_empty"

    def test_send_failure_wraps_messenger_error_to_send_failed(self, fb_env):
        fb, knobs, img = fb_env
        from src.app_automation.facebook import (
            AttachImageError, MessengerError,
        )
        # patch _tap_messenger_send to raise MessengerError
        fb._tap_messenger_send = MagicMock(
            side_effect=MessengerError("send_button_missing", "x", hint="y"))
        with pytest.raises(AttachImageError) as exc:
            fb.attach_image(img, device_id="D1", raise_on_error=True)
        assert exc.value.code == "send_failed"
        assert "send_button_missing" in str(exc.value)


# ════════════════════════════════════════════════════════════════════════
# 内部 helpers — 行为契约
# ════════════════════════════════════════════════════════════════════════

class TestHelpers:
    def test_grant_permissions_attempts_both_perms(self, fb_env):
        fb, knobs, img = fb_env
        fb._grant_orca_media_permissions("D1")
        # READ_MEDIA_IMAGES + READ_EXTERNAL_STORAGE 都试一遍
        cmds = [c.args[0] for c in fb.dm.execute_adb_command.call_args_list]
        joined = " ".join(" ".join(c) for c in cmds)
        assert "READ_MEDIA_IMAGES" in joined
        assert "READ_EXTERNAL_STORAGE" in joined

    def test_grant_permissions_silent_when_both_unknown(self, fb_env):
        fb, knobs, img = fb_env
        knobs["grant_results"] = [
            (True, "Unknown permission"),  # SDK <33 不识别 READ_MEDIA_IMAGES
            (True, "Unknown permission"),  # 也不识别 READ_EXTERNAL_STORAGE
        ]
        # 不抛 — pm grant 已授权过的设备也会"成功但无效"
        fb._grant_orca_media_permissions("D1")

    def test_push_returns_remote_path(self, fb_env):
        fb, knobs, img = fb_env
        path = fb._push_image_to_orca_gallery(img, "D1")
        assert path.startswith("/sdcard/Pictures/openclaw_")
        assert path.endswith(".png")

    def test_push_local_missing_raises(self, fb_env):
        fb, knobs, img = fb_env
        from src.app_automation.facebook import AttachImageError
        with pytest.raises(AttachImageError) as exc:
            fb._push_image_to_orca_gallery("/no/such.png", "D1")
        assert exc.value.code == "push_failed"

    def test_push_adb_failure_raises(self, fb_env):
        fb, knobs, img = fb_env
        from src.app_automation.facebook import AttachImageError
        knobs["push_result"] = (False, "remote dir not writable")
        with pytest.raises(AttachImageError) as exc:
            fb._push_image_to_orca_gallery(img, "D1")
        assert exc.value.code == "push_failed"

    def test_push_continues_when_media_scan_throws(self, fb_env):
        """媒体扫描 broadcast 失败不应抛 — picker_empty 后续会兜底。"""
        fb, knobs, img = fb_env
        knobs["scan_result"] = (False, "broadcast failed")
        # 不抛即可
        fb._push_image_to_orca_gallery(img, "D1")


# ════════════════════════════════════════════════════════════════════════
# Caption 路径
# ════════════════════════════════════════════════════════════════════════

class TestCaptionPath:
    def test_caption_triggers_composer_focus_and_type(self, fb_env):
        fb, knobs, img = fb_env
        ok = fb.attach_image(img, caption="加我 LINE: @abc",
                             device_id="D1", raise_on_error=True)
        assert ok is True
        fb._focus_messenger_composer.assert_called_once()
        fb.hb.type_text.assert_called_once()
        # 文本被 rewrite_message (no-op 在 fixture 里) 后送进 type_text
        args = fb.hb.type_text.call_args.args
        assert "@abc" in args[1]

    def test_no_caption_skips_composer(self, fb_env):
        fb, knobs, img = fb_env
        fb.attach_image(img, caption="", device_id="D1", raise_on_error=True)
        fb._focus_messenger_composer.assert_not_called()
        fb.hb.type_text.assert_not_called()
