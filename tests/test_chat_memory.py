# -*- coding: utf-8 -*-
"""P3 `src/ai/chat_memory.py` 单元测试 — B 机 Messenger 长久记忆层。

覆盖 3 大接口:
  * get_history: 设备/peer 过滤、排序、limit
  * get_derived_profile: 派生画像各字段聚合正确
  * format_history_for_llm / format_profile_for_llm / build_context_block: 格式化语义

不 mock DB — 用 conftest 的 ``tmp_db`` 起临时 sqlite,record_inbox_message 直写,
chat_memory 从同库 SELECT 出。这样画像聚合 SQL 的正确性直接验证,不掩盖 bug。
"""
from __future__ import annotations

import pytest


# ─── get_history ─────────────────────────────────────────────────────────────

class TestGetHistory:
    def test_empty_db_returns_empty(self, tmp_db):
        from src.ai.chat_memory import get_history
        assert get_history("devA", "Alice") == []

    def test_single_peer_returns_sorted_ascending(self, tmp_db):
        from src.host.fb_store import record_inbox_message
        from src.ai.chat_memory import get_history
        record_inbox_message("devA", "Alice", direction="incoming",
                             message_text="m1")
        record_inbox_message("devA", "Alice", direction="outgoing",
                             message_text="m2")
        record_inbox_message("devA", "Alice", direction="incoming",
                             message_text="m3")
        hist = get_history("devA", "Alice", limit=5)
        assert [r["message_text"] for r in hist] == ["m1", "m2", "m3"]
        assert hist[0]["direction"] == "incoming"
        assert hist[1]["direction"] == "outgoing"

    def test_limit_caps_to_most_recent(self, tmp_db):
        from src.host.fb_store import record_inbox_message
        from src.ai.chat_memory import get_history
        for i in range(5):
            record_inbox_message("devA", "Alice", direction="incoming",
                                 message_text=f"m{i}")
            hist = get_history("devA", "Alice", limit=3)
        assert len(hist) == 3
        # 最近 3 条,正序
        assert [r["message_text"] for r in hist] == ["m2", "m3", "m4"]

    def test_filters_other_peers(self, tmp_db):
        from src.host.fb_store import record_inbox_message
        from src.ai.chat_memory import get_history
        record_inbox_message("devA", "Alice", direction="incoming",
                             message_text="to_alice")
        record_inbox_message("devA", "Bob", direction="incoming",
                             message_text="to_bob")
        hist = get_history("devA", "Alice", limit=5)
        assert len(hist) == 1
        assert hist[0]["message_text"] == "to_alice"

    def test_filters_other_devices(self, tmp_db):
        from src.host.fb_store import record_inbox_message
        from src.ai.chat_memory import get_history
        record_inbox_message("devA", "Alice", direction="incoming",
                             message_text="on_A")
        record_inbox_message("devB", "Alice", direction="incoming",
                             message_text="on_B")
        hist = get_history("devA", "Alice", limit=5)
        assert len(hist) == 1
        assert hist[0]["message_text"] == "on_A"

    def test_empty_inputs_return_empty(self, tmp_db):
        from src.ai.chat_memory import get_history
        assert get_history("", "Alice") == []
        assert get_history("devA", "") == []
        assert get_history("devA", "Alice", limit=0) == []
        assert get_history("devA", "Alice", limit=-1) == []


# ─── get_derived_profile ─────────────────────────────────────────────────────

class TestGetDerivedProfile:
    def test_empty_peer_returns_defaults(self, tmp_db):
        from src.ai.chat_memory import get_derived_profile
        p = get_derived_profile("devA", "Alice")
        assert p["total_turns"] == 0
        assert p["peer_reply_count"] == 0
        assert p["language_pref"] == ""
        assert p["active_hours_utc"] == []
        assert p["referral_attempts"] == 0
        assert p["referral_got_reply"] is False
        assert p["greeting_template_ids"] == []

    def test_empty_inputs_returns_defaults(self, tmp_db):
        from src.ai.chat_memory import get_derived_profile
        p = get_derived_profile("", "Alice")
        assert p["total_turns"] == 0
        p = get_derived_profile("devA", "")
        assert p["total_turns"] == 0

    def test_counts_direction_and_decision(self, tmp_db):
        from src.host.fb_store import record_inbox_message
        from src.ai.chat_memory import get_derived_profile
        record_inbox_message("devA", "Alice", direction="incoming",
                             message_text="hi")
        record_inbox_message("devA", "Alice", direction="incoming",
                             message_text="hello")
        record_inbox_message("devA", "Alice", direction="outgoing",
                             message_text="yo", ai_decision="reply")
        record_inbox_message("devA", "Alice", direction="outgoing",
                             message_text="refer", ai_decision="wa_referral")
        record_inbox_message("devA", "Alice", direction="outgoing",
                             message_text="nice to meet",
                             ai_decision="greeting", template_id="yaml:jp:3")
        p = get_derived_profile("devA", "Alice")
        assert p["total_turns"] == 5
        assert p["peer_reply_count"] == 2
        assert p["bot_reply_count"] == 2  # reply + wa_referral
        assert p["greeting_count"] == 1
        assert "yaml:jp:3" in p["greeting_template_ids"]

    def test_language_pref_is_mode(self, tmp_db):
        from src.host.fb_store import record_inbox_message
        from src.ai.chat_memory import get_derived_profile
        record_inbox_message("devA", "Alice", direction="incoming",
                             message_text="こんにちは",
                             language_detected="ja")
        record_inbox_message("devA", "Alice", direction="incoming",
                             message_text="おはよう",
                             language_detected="ja")
        record_inbox_message("devA", "Alice", direction="incoming",
                             message_text="hello",
                             language_detected="en")
        # outgoing 的 language_detected 不应该计入"对方偏好"
        record_inbox_message("devA", "Alice", direction="outgoing",
                             message_text="hi",
                             language_detected="en")
        p = get_derived_profile("devA", "Alice")
        assert p["language_pref"] == "ja"
        assert p["language_stats"] == {"ja": 2, "en": 1}

    def test_language_skips_empty_values(self, tmp_db):
        from src.host.fb_store import record_inbox_message
        from src.ai.chat_memory import get_derived_profile
        record_inbox_message("devA", "Alice", direction="incoming",
                             message_text="??")
        record_inbox_message("devA", "Alice", direction="incoming",
                             message_text="hi",
                             language_detected="en")
        p = get_derived_profile("devA", "Alice")
        assert p["language_pref"] == "en"
        assert p["language_stats"] == {"en": 1}

    def test_referral_attempts_and_got_reply(self, tmp_db):
        from src.host.fb_store import record_inbox_message
        from src.ai.chat_memory import get_derived_profile
        # 1. 对方先发
        record_inbox_message("devA", "Alice", direction="incoming",
                             message_text="m1")
        # 2. 机器引流
        record_inbox_message("devA", "Alice", direction="outgoing",
                             message_text="加 LINE 吧", ai_decision="wa_referral")
        # 3. 对方回复
        record_inbox_message("devA", "Alice", direction="incoming",
                             message_text="ok")
        p = get_derived_profile("devA", "Alice")
        assert p["referral_attempts"] == 1
        assert p["referral_got_reply"] is True

    def test_referral_no_reply_yet(self, tmp_db):
        from src.host.fb_store import record_inbox_message
        from src.ai.chat_memory import get_derived_profile
        record_inbox_message("devA", "Alice", direction="incoming",
                             message_text="m1")
        record_inbox_message("devA", "Alice", direction="outgoing",
                             message_text="加 LINE 吧", ai_decision="wa_referral")
        # 没有后续 incoming
        p = get_derived_profile("devA", "Alice")
        assert p["referral_attempts"] == 1
        assert p["referral_got_reply"] is False

    def test_multiple_referrals_last_wins(self, tmp_db):
        from src.host.fb_store import record_inbox_message
        from src.ai.chat_memory import get_derived_profile
        record_inbox_message("devA", "Alice", direction="outgoing",
                             message_text="r1", ai_decision="wa_referral")
        record_inbox_message("devA", "Alice", direction="incoming",
                             message_text="ok")
        record_inbox_message("devA", "Alice", direction="outgoing",
                             message_text="r2", ai_decision="wa_referral")
        # 第二次引流后没新 incoming
        p = get_derived_profile("devA", "Alice")
        assert p["referral_attempts"] == 2
        # 以最近一次为准: r2 后无 incoming
        assert p["referral_got_reply"] is False

    def test_recent_topics_snippet_truncation(self, tmp_db):
        from src.host.fb_store import record_inbox_message
        from src.ai.chat_memory import get_derived_profile
        long_text = "x" * 200
        # 3 条长消息,拼接应被截到 400 chars
        for _ in range(3):
            record_inbox_message("devA", "Alice", direction="incoming",
                                 message_text=long_text)
        p = get_derived_profile("devA", "Alice")
        assert len(p["recent_topics_snippet"]) <= 400
        assert p["recent_topics_snippet"].endswith("...")

    def test_recent_topics_snippet_strips_newlines(self, tmp_db):
        from src.host.fb_store import record_inbox_message
        from src.ai.chat_memory import get_derived_profile
        record_inbox_message("devA", "Alice", direction="incoming",
                             message_text="line1\nline2")
        p = get_derived_profile("devA", "Alice")
        assert "\n" not in p["recent_topics_snippet"]
        assert "line1 line2" in p["recent_topics_snippet"]

    def test_recent_topics_only_incoming(self, tmp_db):
        from src.host.fb_store import record_inbox_message
        from src.ai.chat_memory import get_derived_profile
        record_inbox_message("devA", "Alice", direction="outgoing",
                             message_text="SHOULD NOT APPEAR")
        record_inbox_message("devA", "Alice", direction="incoming",
                             message_text="visible")
        p = get_derived_profile("devA", "Alice")
        assert "visible" in p["recent_topics_snippet"]
        assert "SHOULD NOT APPEAR" not in p["recent_topics_snippet"]

    def test_timestamps_first_last(self, tmp_db):
        from src.host.fb_store import record_inbox_message
        from src.ai.chat_memory import get_derived_profile
        record_inbox_message("devA", "Alice", direction="incoming",
                             message_text="first")
        record_inbox_message("devA", "Alice", direction="outgoing",
                             message_text="last", ai_decision="reply")
        p = get_derived_profile("devA", "Alice")
        assert p["first_seen_at"]
        assert p["last_seen_at"]
        assert p["first_seen_at"] <= p["last_seen_at"]
        assert p["last_incoming_at"] == p["first_seen_at"]
        assert p["last_outgoing_at"] == p["last_seen_at"] or p["last_outgoing_at"]

    def test_active_hours_empty_for_new_peer(self, tmp_db):
        from src.ai.chat_memory import get_derived_profile
        p = get_derived_profile("devA", "Alice")
        assert p["active_hours_utc"] == []


# ─── _iso_to_hour 独立测试 ───────────────────────────────────────────────────

class TestIsoToHour:
    def test_iso_z_format(self):
        from src.ai.chat_memory import _iso_to_hour
        assert _iso_to_hour("2026-04-23T10:30:00Z") == 10

    def test_sqlite_datetime_format(self):
        from src.ai.chat_memory import _iso_to_hour
        assert _iso_to_hour("2026-04-23 08:15:00") == 8

    def test_empty_returns_none(self):
        from src.ai.chat_memory import _iso_to_hour
        assert _iso_to_hour("") is None

    def test_bogus_returns_none_gracefully(self):
        from src.ai.chat_memory import _iso_to_hour
        assert _iso_to_hour("not-a-date") is None

    def test_fallback_slice_parses(self):
        from src.ai.chat_memory import _iso_to_hour
        # 非标 ISO 但前 13 字符有效
        assert _iso_to_hour("2026-04-23T23:59:59+08:00") == 23


# ─── format_history_for_llm ──────────────────────────────────────────────────

class TestFormatHistory:
    def test_empty_returns_empty_string(self):
        from src.ai.chat_memory import format_history_for_llm
        assert format_history_for_llm([]) == ""

    def test_shape_has_header_and_lines(self):
        from src.ai.chat_memory import format_history_for_llm
        txt = format_history_for_llm([
            {"direction": "incoming", "message_text": "hi",
             "seen_at": "2026-04-23T10:30:00Z"},
            {"direction": "outgoing", "message_text": "yo",
             "seen_at": "2026-04-23T10:31:00Z"},
        ])
        assert "历史对话" in txt
        assert "对方: hi" in txt
        assert "我方: yo" in txt
        assert "2026-04-23" in txt

    def test_long_text_truncated(self):
        from src.ai.chat_memory import format_history_for_llm
        long = "x" * 300
        txt = format_history_for_llm([
            {"direction": "incoming", "message_text": long,
             "seen_at": "2026-04-23T10:30:00Z"},
        ])
        assert "..." in txt
        # 160 char truncation + "..."
        assert "x" * 200 not in txt

    def test_skips_empty_text_rows(self):
        from src.ai.chat_memory import format_history_for_llm
        txt = format_history_for_llm([
            {"direction": "incoming", "message_text": "",
             "seen_at": "2026-04-23T10:30:00Z"},
        ])
        # 只有 header 没有真实行 → 空串
        assert txt == ""


# ─── format_profile_for_llm ──────────────────────────────────────────────────

class TestFormatProfile:
    def test_cold_start_returns_empty(self):
        from src.ai.chat_memory import format_profile_for_llm, _empty_profile
        assert format_profile_for_llm(_empty_profile()) == ""
        assert format_profile_for_llm({}) == ""

    def test_populated_has_sections(self):
        from src.ai.chat_memory import format_profile_for_llm, _empty_profile
        p = _empty_profile()
        p.update({
            "total_turns": 10,
            "peer_reply_count": 5,
            "bot_reply_count": 5,
            "greeting_count": 1,
            "language_pref": "ja",
            "active_hours_utc": [10, 14, 20],
            "recent_topics_snippet": "こんにちは | おはよう",
        })
        txt = format_profile_for_llm(p)
        assert "Peer 画像提示" in txt
        assert "累计 10 条消息" in txt
        assert "ja" in txt
        assert "UTC 时段" in txt
        assert "こんにちは" in txt

    def test_referral_unreplied_emits_warning(self):
        from src.ai.chat_memory import format_profile_for_llm, _empty_profile
        p = _empty_profile()
        p.update({
            "total_turns": 3,
            "referral_attempts": 1,
            "last_referral_at": "2026-04-23T10:00:00Z",
            "referral_got_reply": False,
        })
        txt = format_profile_for_llm(p)
        assert "历史引流尝试" in txt
        assert "未回复" in txt
        assert "不要再重复引流" in txt

    def test_referral_replied_no_warning(self):
        from src.ai.chat_memory import format_profile_for_llm, _empty_profile
        p = _empty_profile()
        p.update({
            "total_turns": 3,
            "referral_attempts": 1,
            "last_referral_at": "2026-04-23T10:00:00Z",
            "referral_got_reply": True,
        })
        txt = format_profile_for_llm(p)
        assert "有回复" in txt
        assert "不要再重复引流" not in txt


# ─── build_context_block ─────────────────────────────────────────────────────

class TestBuildContextBlock:
    def test_cold_start_returns_empty_hint(self, tmp_db):
        from src.ai.chat_memory import build_context_block
        ctx = build_context_block("devA", "Alice")
        assert ctx["hint_text"] == ""
        assert ctx["should_block_referral"] is False
        assert ctx["history"] == []
        assert ctx["profile"]["total_turns"] == 0

    def test_with_history_composes_hint(self, tmp_db):
        from src.host.fb_store import record_inbox_message
        from src.ai.chat_memory import build_context_block
        record_inbox_message("devA", "Alice", direction="incoming",
                             message_text="こんにちは",
                             language_detected="ja")
        record_inbox_message("devA", "Alice", direction="outgoing",
                             message_text="はじめまして",
                             ai_decision="reply")
        ctx = build_context_block("devA", "Alice")
        assert "历史对话" in ctx["hint_text"]
        assert "Peer 画像提示" in ctx["hint_text"]
        assert "ja" in ctx["hint_text"]
        assert ctx["should_block_referral"] is False

    def test_should_block_referral_when_unanswered(self, tmp_db):
        from src.host.fb_store import record_inbox_message
        from src.ai.chat_memory import build_context_block
        record_inbox_message("devA", "Alice", direction="outgoing",
                             message_text="加 LINE 吧", ai_decision="wa_referral")
        ctx = build_context_block("devA", "Alice")
        assert ctx["profile"]["referral_attempts"] == 1
        assert ctx["should_block_referral"] is True

    def test_should_not_block_after_reply(self, tmp_db):
        from src.host.fb_store import record_inbox_message
        from src.ai.chat_memory import build_context_block
        record_inbox_message("devA", "Alice", direction="outgoing",
                             message_text="加 LINE 吧", ai_decision="wa_referral")
        record_inbox_message("devA", "Alice", direction="incoming",
                             message_text="ok")
        ctx = build_context_block("devA", "Alice")
        assert ctx["should_block_referral"] is False

    def test_history_limit_respected(self, tmp_db):
        from src.host.fb_store import record_inbox_message
        from src.ai.chat_memory import build_context_block
        for i in range(8):
            record_inbox_message("devA", "Alice", direction="incoming",
                                 message_text=f"msg{i}")
            ctx = build_context_block("devA", "Alice", history_limit=3)
        assert len(ctx["history"]) == 3
        # profile 聚合用的是全量,不受 limit 影响
        assert ctx["profile"]["peer_reply_count"] == 8
