# -*- coding: utf-8 -*-
"""Phase 20.1 (2026-04-25): A 侧 referral inbox 检测调度 + 关键词匹配单测.

B 侧 check_messenger_inbox(referral_mode=True) 用 fake fb 替代.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List

import pytest


@pytest.fixture(autouse=True)
def _reset():
    from src.host.fb_store import reset_peer_name_reject_count
    reset_peer_name_reject_count()
    # 强制重载 keyword cache
    from src.host import executor as _ex
    _ex._REFERRAL_KEYWORDS_CACHE["data"] = None
    _ex._REFERRAL_KEYWORDS_CACHE["loaded_at"] = 0.0
    yield


def _seed_event(device_id, peer_name, event_type, meta=None):
    from src.host.fb_store import record_contact_event
    return record_contact_event(device_id, peer_name, event_type,
                                  meta=meta or {"via": "test"},
                                  skip_sanitize=True)


# ═══════════════════════════════════════════════════════════════════
# 20.1.1: keyword 匹配引擎
# ═══════════════════════════════════════════════════════════════════

class TestKeywordMatch:
    def test_default_line_matches(self):
        from src.host.executor import _match_referral_keyword
        # default 组里有 "line"
        assert _match_referral_keyword("Send me your LINE id please") == "line"

    def test_jp_specific_keyword_matches_when_region_jp(self):
        from src.host.executor import _match_referral_keyword
        # 友達 在 jp 组, default 没有
        assert _match_referral_keyword("友達追加していい？", region="jp") in (
            "友達", "追加")

    def test_no_match_returns_empty(self):
        from src.host.executor import _match_referral_keyword
        assert _match_referral_keyword("hello, how are you today") == ""

    def test_empty_text_returns_empty(self):
        from src.host.executor import _match_referral_keyword
        assert _match_referral_keyword("") == ""
        assert _match_referral_keyword(None) == ""

    def test_keyword_loader_returns_default_at_minimum(self):
        from src.host.executor import _load_referral_keywords
        m = _load_referral_keywords(force=True)
        assert "default" in m
        # default 应非空 (yaml 里有 line / 加我 等)
        assert len(m["default"]) > 0


# ═══════════════════════════════════════════════════════════════════
# 20.1.2: get_pending_referral_peers helper
# ═══════════════════════════════════════════════════════════════════

class TestPendingPeers:
    def test_empty_when_no_events(self, tmp_db):
        from src.host.fb_store import get_pending_referral_peers
        assert get_pending_referral_peers(device_id="D1") == []

    def test_returns_sent_without_replied(self, tmp_db):
        from src.host.fb_store import get_pending_referral_peers
        _seed_event("D1", "花子", "wa_referral_sent")
        out = get_pending_referral_peers(device_id="D1")
        assert len(out) == 1
        assert out[0]["peer_name"] == "花子"

    def test_excludes_already_replied(self, tmp_db):
        from src.host.fb_store import get_pending_referral_peers
        _seed_event("D1", "花子", "wa_referral_sent")
        _seed_event("D1", "花子", "wa_referral_replied")
        out = get_pending_referral_peers(device_id="D1")
        assert out == []

    def test_dedup_multiple_sent_same_peer(self, tmp_db):
        from src.host.fb_store import get_pending_referral_peers
        _seed_event("D1", "花子", "wa_referral_sent")
        time.sleep(0.01)
        _seed_event("D1", "花子", "wa_referral_sent")
        out = get_pending_referral_peers(device_id="D1")
        assert len(out) == 1  # 同 peer 只出 1 次

    def test_per_device_filter(self, tmp_db):
        from src.host.fb_store import get_pending_referral_peers
        _seed_event("D1", "花子", "wa_referral_sent")
        _seed_event("D2", "Maria", "wa_referral_sent")
        out_d1 = get_pending_referral_peers(device_id="D1")
        out_d2 = get_pending_referral_peers(device_id="D2")
        assert len(out_d1) == 1
        assert out_d1[0]["peer_name"] == "花子"
        assert len(out_d2) == 1
        assert out_d2[0]["peer_name"] == "Maria"


# ═══════════════════════════════════════════════════════════════════
# 20.1.3: _fb_check_referral_replies — A 侧调度逻辑
# ═══════════════════════════════════════════════════════════════════

class _FakeFB:
    """模拟 B 实装的 check_messenger_inbox(referral_mode=...) 行为."""

    def __init__(self, conversations: List[Dict[str, Any]]):
        self._conversations = conversations
        self.calls: List[Dict[str, Any]] = []

    def check_messenger_inbox(self, **kwargs):
        self.calls.append(kwargs)
        return {"conversations": self._conversations}


class _LegacyFakeFB:
    """模拟 B 还没扩 referral_mode 的旧实装 (不认 kwargs)."""
    def check_messenger_inbox(self, auto_reply=False, max_conversations=20):
        return {"messenger_active": True}


class TestCheckReferralRepliesScheduler:
    def test_no_pending_returns_zero(self, tmp_db):
        from src.host.executor import _fb_check_referral_replies
        ok, _, stats = _fb_check_referral_replies(
            _FakeFB([]), "D1", {"hours_back": 48, "limit": 10})
        assert ok
        assert stats["pending_count"] == 0
        assert stats["replied_now"] == 0

    def test_pending_no_match_records_no_event(self, tmp_db):
        from src.host.executor import _fb_check_referral_replies
        from src.host.fb_store import count_contact_events
        _seed_event("D1", "花子", "wa_referral_sent")
        # B 返了 conv, 但 inbound text 没匹配关键词
        fb = _FakeFB([{"peer_name": "花子",
                        "last_inbound_text": "Hello, what's up?",
                        "conv_id": "c1"}])
        ok, _, stats = _fb_check_referral_replies(
            fb, "D1", {"hours_back": 48, "limit": 10})
        assert ok
        assert stats["pending_count"] == 1
        assert stats["replied_now"] == 0
        assert stats["no_match"] == 1
        # 不应有 wa_referral_replied 写入
        cnt = count_contact_events(
            device_id="D1",
            event_type="wa_referral_replied", hours=24)
        assert cnt == 0

    def test_match_triggers_replied_event(self, tmp_db):
        from src.host.executor import _fb_check_referral_replies
        from src.host.fb_store import (count_contact_events,
                                          list_contact_events_by_peer)
        _seed_event("D1", "花子", "wa_referral_sent")
        fb = _FakeFB([{"peer_name": "花子",
                        "last_inbound_text": "OK, send me your LINE id",
                        "conv_id": "c1"}])
        ok, _, stats = _fb_check_referral_replies(
            fb, "D1", {"hours_back": 48, "limit": 10})
        assert ok
        assert stats["replied_now"] == 1
        assert stats["matches"][0]["peer_name"] == "花子"
        assert stats["matches"][0]["keyword"] == "line"
        # 验事件确实写了
        cnt = count_contact_events(
            device_id="D1",
            event_type="wa_referral_replied", hours=24)
        assert cnt == 1
        # 验 meta 含 sent_event_id 链回原 sent
        evs = list_contact_events_by_peer("D1", "花子")
        replied = [e for e in evs if e["event_type"] == "wa_referral_replied"]
        assert len(replied) == 1
        import json as _json
        meta = _json.loads(replied[0]["meta_json"] or "{}")
        assert meta.get("keyword_matched") == "line"
        assert meta.get("sent_event_id")

    def test_b_legacy_signature_returns_error_message(self, tmp_db):
        """B 还没扩 referral_mode 时, 应清晰报错 + 引文档."""
        from src.host.executor import _fb_check_referral_replies
        _seed_event("D1", "花子", "wa_referral_sent")
        ok, msg, stats = _fb_check_referral_replies(
            _LegacyFakeFB(), "D1", {"hours_back": 48})
        assert ok is False
        assert "referral_mode" in msg
        assert "A_TO_B_PHASE20_INBOX" in msg

    def test_only_pending_peers_get_event(self, tmp_db):
        """B 返了多个 conv 但只 pending 中的会被处理."""
        from src.host.executor import _fb_check_referral_replies
        _seed_event("D1", "花子", "wa_referral_sent")
        # Maria 没 sent → B 即便返了也不应处理 (peers_filter 是参考, A 兜底过滤)
        fb = _FakeFB([
            {"peer_name": "花子", "last_inbound_text": "加我 line"},
            {"peer_name": "Maria",
              "last_inbound_text": "send your line id"},  # 不在 pending
            {"peer_name": "ghost",
              "last_inbound_text": "line@xxx"},  # 不在 pending
        ])
        ok, _, stats = _fb_check_referral_replies(
            fb, "D1", {"hours_back": 48, "limit": 10})
        assert ok
        assert stats["replied_now"] == 1  # 只 花子
        assert stats["matches"][0]["peer_name"] == "花子"

    def test_already_replied_skipped(self, tmp_db):
        """已 replied 的 peer 不再写第 2 次."""
        from src.host.executor import _fb_check_referral_replies
        from src.host.fb_store import count_contact_events
        _seed_event("D1", "花子", "wa_referral_sent")
        _seed_event("D1", "花子", "wa_referral_replied")  # 已 replied
        fb = _FakeFB([{"peer_name": "花子",
                        "last_inbound_text": "再发我个 LINE id"}])
        ok, _, stats = _fb_check_referral_replies(
            fb, "D1", {"hours_back": 48, "limit": 10})
        assert ok
        assert stats["pending_count"] == 0  # pending 已被 replied 排除
        cnt = count_contact_events(
            device_id="D1",
            event_type="wa_referral_replied", hours=24)
        assert cnt == 1  # 还是 1, 没重复写

    def test_b_kwargs_propagated(self, tmp_db):
        """A 调 B 时应传 referral_mode/peers_filter/max_messages_per_peer."""
        from src.host.executor import _fb_check_referral_replies
        _seed_event("D1", "花子", "wa_referral_sent")
        fb = _FakeFB([])
        _fb_check_referral_replies(
            fb, "D1", {"hours_back": 48, "limit": 10,
                        "max_messages_per_peer": 7})
        assert len(fb.calls) == 1
        kw = fb.calls[0]
        assert kw.get("referral_mode") is True
        assert kw.get("peers_filter") == ["花子"]
        assert kw.get("max_messages_per_peer") == 7
        assert kw.get("auto_reply") is False


# ═══════════════════════════════════════════════════════════════════
# 20.1.7.1: _resolve_peer_regions + auto region routing
# ═══════════════════════════════════════════════════════════════════

def _seed_l2_lead(name, persona="jp_female_midlife", region=None):
    from src.host.lead_mesh import (resolve_identity,
                                      update_canonical_metadata)
    cid = resolve_identity(platform="facebook",
                            account_id=f"fb:{name}",
                            display_name=name)
    meta = {"l2_score": 80, "l2_persona_key": persona,
            "l2_verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                              time.gmtime())}
    if region is not None:
        meta["region"] = region
    update_canonical_metadata(cid, meta, tags=["l2_verified"])
    return cid


class TestResolvePeerRegions:
    def test_empty_input_returns_empty(self, tmp_db):
        from src.host.executor import _resolve_peer_regions
        assert _resolve_peer_regions([]) == {}

    def test_unknown_peer_returns_empty_string(self, tmp_db):
        """没在 lead_identities 的 peer → ""."""
        from src.host.executor import _resolve_peer_regions
        m = _resolve_peer_regions(["ghost-name"])
        assert m == {"ghost-name": ""}

    def test_resolves_jp_persona_to_jp(self, tmp_db):
        from src.host.executor import _resolve_peer_regions
        _seed_l2_lead("花子", persona="jp_female_midlife")
        m = _resolve_peer_regions(["花子"])
        assert m["花子"] == "jp"

    def test_mixed_peers(self, tmp_db):
        from src.host.executor import _resolve_peer_regions
        _seed_l2_lead("花子", persona="jp_female_midlife")
        _seed_l2_lead("Maria", persona="it_female_midlife")
        m = _resolve_peer_regions(["花子", "Maria", "ghost"])
        assert m["花子"] == "jp"
        assert m["Maria"] == "it"
        assert m["ghost"] == ""


class TestAutoRegionRoutingInScheduler:
    def test_auto_region_picks_jp_keyword_for_jp_lead(self, tmp_db):
        """jp lead 回复 '友達追加', 走 jp 关键词组命中."""
        from src.host.executor import _fb_check_referral_replies
        _seed_l2_lead("花子", persona="jp_female_midlife")
        _seed_event("D1", "花子", "wa_referral_sent")
        fb = _FakeFB([{"peer_name": "花子",
                        "last_inbound_text": "友達追加していい？",
                        "conv_id": "c1"}])
        ok, _, stats = _fb_check_referral_replies(
            fb, "D1", {"hours_back": 48, "limit": 10})
        assert ok
        assert stats["replied_now"] == 1
        # jp 关键词组里有 "友達" / "追加"
        assert stats["matches"][0]["keyword"] in ("友達", "追加")
        assert stats["matches"][0]["region"] == "jp"

    def test_keyword_region_override_forces_all_peers(self, tmp_db):
        """params.keyword_region 覆盖 → 全部 peer 用同一 region."""
        from src.host.executor import _fb_check_referral_replies
        # 不 seed canonical, 让自动推断为空
        _seed_event("D1", "花子", "wa_referral_sent")
        fb = _FakeFB([{"peer_name": "花子",
                        "last_inbound_text": "友達 ok"}])
        # 强制走 jp → 命中 友達
        ok, _, stats = _fb_check_referral_replies(
            fb, "D1", {"hours_back": 48, "limit": 10,
                        "keyword_region": "jp"})
        assert stats["replied_now"] == 1
        assert stats["matches"][0]["keyword"] == "友達"

    def test_unknown_peer_falls_back_to_default_pool(self, tmp_db):
        """没 region 的 peer 用 default 关键词组."""
        from src.host.executor import _fb_check_referral_replies
        _seed_event("D1", "ghost", "wa_referral_sent")  # 没 seed canonical
        fb = _FakeFB([{"peer_name": "ghost",
                        "last_inbound_text": "send me your line id"}])
        ok, _, stats = _fb_check_referral_replies(
            fb, "D1", {"hours_back": 48, "limit": 10})
        # default 组里有 line → 命中
        assert stats["replied_now"] == 1
        assert stats["matches"][0]["keyword"] == "line"
        assert stats["matches"][0]["region"] == ""


# ═══════════════════════════════════════════════════════════════════
# 20.1.7.2: latency 计算 + meta 写入
# ═══════════════════════════════════════════════════════════════════

class TestParseEventAt:
    def test_sqlite_default_format(self):
        from src.host.executor import _parse_event_at
        ts = _parse_event_at("2026-04-25 05:00:00")
        assert ts is not None
        assert ts > 0

    def test_iso_z_format(self):
        from src.host.executor import _parse_event_at
        ts = _parse_event_at("2026-04-25T05:00:00Z")
        assert ts is not None

    def test_invalid_returns_none(self):
        from src.host.executor import _parse_event_at
        assert _parse_event_at("garbage") is None
        assert _parse_event_at("") is None
        assert _parse_event_at(None) is None


class TestLatencyInMeta:
    def test_latency_written_into_meta(self, tmp_db):
        """match 后 wa_referral_replied.meta 含 latency_seconds + latency_min."""
        from src.host.executor import _fb_check_referral_replies
        from src.host.fb_store import list_contact_events_by_peer
        import json as _j
        _seed_event("D1", "花子", "wa_referral_sent")
        # 等几十毫秒, 让 latency > 0
        time.sleep(0.05)
        fb = _FakeFB([{"peer_name": "花子",
                        "last_inbound_text": "加 line"}])
        _fb_check_referral_replies(fb, "D1", {"hours_back": 48})
        evs = list_contact_events_by_peer("D1", "花子")
        replied = [e for e in evs if e["event_type"] == "wa_referral_replied"]
        assert len(replied) == 1
        meta = _j.loads(replied[0]["meta_json"] or "{}")
        assert meta.get("latency_seconds") is not None
        assert meta.get("latency_seconds") >= 0
        assert meta.get("latency_min") is not None


# ═══════════════════════════════════════════════════════════════════
# 20.1.7.2: _compute_reply_latency_stats
# ═══════════════════════════════════════════════════════════════════

class TestReplyLatencyStats:
    def _seed_replied_with_latency(self, peer, latency_min):
        from src.host.fb_store import record_contact_event
        return record_contact_event(
            "D1", peer, "wa_referral_replied",
            meta={"latency_min": latency_min, "latency_seconds": latency_min * 60},
            skip_sanitize=True)

    def test_empty_returns_none_fields(self, tmp_db):
        from src.host.executor import _compute_reply_latency_stats
        s = _compute_reply_latency_stats(hours_window=24)
        assert s["samples"] == 0
        assert s["avg_min"] is None
        assert s["median_min"] is None

    def test_three_samples_computes_avg_median(self, tmp_db):
        from src.host.executor import _compute_reply_latency_stats
        for i, lat in enumerate([10, 20, 30]):
            self._seed_replied_with_latency(f"p{i}", lat)
        s = _compute_reply_latency_stats(hours_window=24)
        assert s["samples"] == 3
        assert s["avg_min"] == 20.0
        assert s["median_min"] == 20.0
        assert s["max_min"] == 30.0

    def test_even_samples_median_is_avg_of_two_middle(self, tmp_db):
        from src.host.executor import _compute_reply_latency_stats
        for i, lat in enumerate([10, 20, 30, 40]):
            self._seed_replied_with_latency(f"p{i}", lat)
        s = _compute_reply_latency_stats(hours_window=24)
        assert s["median_min"] == 25.0  # (20+30)/2

    def test_p95_uses_max_for_small_n(self, tmp_db):
        """n<20 时 p95 退化用 max (避免无意义 nearest-rank)."""
        from src.host.executor import _compute_reply_latency_stats
        for i, lat in enumerate([5, 10, 100]):
            self._seed_replied_with_latency(f"p{i}", lat)
        s = _compute_reply_latency_stats(hours_window=24)
        assert s["p95_min"] == 100.0

    def test_p95_with_20_plus_samples(self, tmp_db):
        from src.host.executor import _compute_reply_latency_stats
        # 20 samples: 1..20, p95_idx = round(0.95*19)=18, value=19
        for i in range(1, 21):
            self._seed_replied_with_latency(f"p{i}", i)
        s = _compute_reply_latency_stats(hours_window=24)
        assert s["samples"] == 20
        assert s["p95_min"] == 19.0


# ═══════════════════════════════════════════════════════════════════
# 20.1.7.2: daily summary 集成 reply_latency
# ═══════════════════════════════════════════════════════════════════

class TestDailySummaryWithLatency:
    def test_summary_contains_reply_latency_field(self, tmp_db, tmp_path,
                                                       monkeypatch):
        from src.host.executor import _fb_daily_referral_summary
        from src.host.fb_store import record_contact_event
        record_contact_event(
            "D1", "花子", "wa_referral_replied",
            meta={"latency_min": 7.5, "latency_seconds": 450},
            skip_sanitize=True)
        monkeypatch.chdir(tmp_path)
        ok, _, stats = _fb_daily_referral_summary({
            "hours_window": 24, "write_file": False, "send_webhook": False,
            "regions": []})
        rl = stats["summary"]["reply_latency"]
        assert rl["samples"] == 1
        assert rl["avg_min"] == 7.5
