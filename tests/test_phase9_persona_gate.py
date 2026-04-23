# -*- coding: utf-8 -*-
"""Phase 9 C: add_friend_with_note persona L1 gate 单测 (2026-04-24).

覆盖 3 路径:
  * 日文女性名 (佐藤花子) → L1 PASS → 继续 add_friend 流程
  * 英文男性名 (John Smith) → L1 REJECT → add_friend_blocked journey
  * 无 persona_key → gate skip (不改原行为)
"""
from __future__ import annotations

import pytest
from unittest.mock import patch, MagicMock


def _stub_fb():
    from src.app_automation.facebook import FacebookAutomation
    fb = FacebookAutomation.__new__(FacebookAutomation)
    fb._current_device = "D_p9"
    fb._last_greet_skip_reason = ""
    fb._current_lead_cid = ""
    fb._messenger_installed_cache = {}
    # Stub blocklist 检查 (always False 放行)
    fb._check_peer_blocklist = lambda *a, **kw: False
    # stub _did + _u2 避免真实 device 调用
    fb._did = lambda dev=None: dev or "D_p9"
    fb._u2 = lambda dev=None: MagicMock()
    return fb


class TestPersonaL1Gate:
    def test_jp_female_name_passes(self, tmp_db):
        """日文女性名 L1 命中 → 继续流程 (不 early return False)."""
        fb = _stub_fb()
        # 让 _resolve_phase_and_cfg 走真实路径, 返回 growth 配置
        # 但下游 _add_friend_with_note_locked 内部会调 device_section_lock
        # 需要 mock device_section_lock 让其能直接 yield
        from src.app_automation.facebook import _resolve_phase_and_cfg
        # 跑到 add_friend_with_note_locked 外层即可验证 gate 不 block
        captured = {}

        def _fake_locked(*args, **kwargs):
            captured["called"] = True
            return True

        with patch.object(fb, "_add_friend_with_note_locked", side_effect=_fake_locked):
            with patch("src.host.fb_concurrency.device_section_lock") as mock_lock:
                mock_lock.return_value.__enter__ = lambda *a: None
                mock_lock.return_value.__exit__ = lambda *a: None
                # u2 auto-foreground 装饰器
                fb._ensure_foreground = lambda *a, **kw: True
                result = fb.add_friend_with_note(
                    "佐藤花子",
                    persona_key="jp_female_midlife",
                    phase="growth",   # bypass cold_start gate
                    device_id="D_p9",
                )
        # 被 gate 放行, 下游 locked 应该被调到
        assert captured.get("called") is True, \
            "日文女性名应 L1 PASS, 继续走到 _add_friend_with_note_locked"

    def test_english_male_name_blocked(self, tmp_db):
        """英文男性名 L1 不命中 → return False + journey add_friend_blocked."""
        fb = _stub_fb()

        captured = {}

        def _fake_locked(*args, **kwargs):
            captured["called"] = True
            return True

        with patch.object(fb, "_add_friend_with_note_locked", side_effect=_fake_locked):
            with patch("src.host.fb_concurrency.device_section_lock") as mock_lock:
                mock_lock.return_value.__enter__ = lambda *a: None
                mock_lock.return_value.__exit__ = lambda *a: None
                fb._ensure_foreground = lambda *a, **kw: True
                result = fb.add_friend_with_note(
                    "John Smith",
                    persona_key="jp_female_midlife",
                    phase="growth",
                    device_id="D_p9",
                )
        assert result is False, "英文男性名应被 L1 拦截"
        assert "called" not in captured, "不应进入 _add_friend_with_note_locked"

        # journey 应有 add_friend_blocked{reason=persona_l1_rejected}
        from src.host.lead_mesh import resolve_identity, get_journey
        cid = resolve_identity(platform="facebook",
                                 account_id="fb:John Smith",
                                 display_name="John Smith")
        events = get_journey(cid)
        blocked = [e for e in events if e["action"] == "add_friend_blocked"]
        assert any(e["data"].get("reason") == "persona_l1_rejected" for e in blocked), \
            f"期望 persona_l1_rejected event, 实际: {blocked}"

    def test_no_persona_key_skips_gate(self, tmp_db):
        """persona_key=None 时, gate 不生效 (向后兼容)."""
        fb = _stub_fb()

        captured = {}

        def _fake_locked(*args, **kwargs):
            captured["called"] = True
            return True

        with patch.object(fb, "_add_friend_with_note_locked", side_effect=_fake_locked):
            with patch("src.host.fb_concurrency.device_section_lock") as mock_lock:
                mock_lock.return_value.__enter__ = lambda *a: None
                mock_lock.return_value.__exit__ = lambda *a: None
                fb._ensure_foreground = lambda *a, **kw: True
                fb.add_friend_with_note(
                    "Arbitrary Name",
                    persona_key=None,  # 没 persona
                    phase="growth",
                    device_id="D_p9",
                )
        assert captured.get("called") is True, "无 persona_key 应放行"

    def test_persona_pass_writes_journey(self, tmp_db):
        """L1 PASS 时写 persona_classified journey (供 funnel 统计命中率)."""
        fb = _stub_fb()

        with patch.object(fb, "_add_friend_with_note_locked", return_value=True):
            with patch("src.host.fb_concurrency.device_section_lock") as mock_lock:
                mock_lock.return_value.__enter__ = lambda *a: None
                mock_lock.return_value.__exit__ = lambda *a: None
                fb._ensure_foreground = lambda *a, **kw: True
                fb.add_friend_with_note(
                    "山田花子",
                    persona_key="jp_female_midlife",
                    phase="growth",
                    device_id="D_p9",
                )

        from src.host.lead_mesh import resolve_identity, get_journey
        cid = resolve_identity(platform="facebook",
                                 account_id="fb:山田花子",
                                 display_name="山田花子")
        events = get_journey(cid)
        classified = [e for e in events if e["action"] == "persona_classified"]
        assert len(classified) >= 1
        assert classified[0]["data"]["match"] is True
        assert classified[0]["data"]["stage"] == "L1"
        assert classified[0]["data"]["score"] > 0
