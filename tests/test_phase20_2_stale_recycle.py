# -*- coding: utf-8 -*-
"""Phase 20.2 (2026-04-25): SLA 死信回收 — mark stale + escalate dead."""
from __future__ import annotations

import datetime as dt
import time

import pytest


@pytest.fixture(autouse=True)
def _reset():
    from src.host.fb_store import reset_peer_name_reject_count
    reset_peer_name_reject_count()
    from src.host import executor as _ex
    _ex._REFERRAL_KEYWORDS_CACHE["data"] = None
    _ex._peer_region_cache_clear()
    yield


def _seed_l2_lead(name, persona="jp_female_midlife"):
    from src.host.lead_mesh import (resolve_identity,
                                      update_canonical_metadata)
    cid = resolve_identity(platform="facebook",
                            account_id=f"fb:{name}",
                            display_name=name)
    update_canonical_metadata(cid, {
        "l2_score": 80, "l2_persona_key": persona,
    }, tags=["l2_verified"])
    return cid


def _seed_event_at(device, peer, etype, sent_at_str):
    """直接 INSERT 一个指定 at 的事件 (绕开 datetime('now'))."""
    from src.host.database import _connect
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO fb_contact_events (device_id, peer_name,"
            " event_type, meta_json, at) VALUES (?, ?, ?, ?, ?)",
            (device, peer, etype, "{}", sent_at_str))
        return int(cur.lastrowid or 0)


# ═══════════════════════════════════════════════════════════════════
# 20.2.1: mark_stale_referrals
# ═══════════════════════════════════════════════════════════════════

class TestMarkStaleReferrals:
    def test_no_pending_returns_zero(self, tmp_db):
        from src.host.fb_store import mark_stale_referrals
        s = mark_stale_referrals(stale_hours=48)
        assert s["scanned"] == 0
        assert s["marked_stale"] == 0

    def test_recent_sent_not_stale(self, tmp_db):
        """sent 1h 前不超过 48h 阈值, 不该 mark."""
        from src.host.fb_store import mark_stale_referrals
        _seed_l2_lead("花子")
        _seed_event_at("D1", "花子", "wa_referral_sent",
                          (dt.datetime.utcnow()
                            - dt.timedelta(hours=1)
                            ).strftime("%Y-%m-%d %H:%M:%S"))
        s = mark_stale_referrals(stale_hours=48)
        assert s["marked_stale"] == 0
        assert s["candidates"] == 0  # age 没到阈值不算 candidate

    def test_old_sent_marked_stale(self, tmp_db):
        """sent 50h 前 > 48h 阈值 → mark stale."""
        from src.host.fb_store import (mark_stale_referrals,
                                          count_contact_events)
        _seed_l2_lead("花子")
        _seed_event_at("D1", "花子", "wa_referral_sent",
                          (dt.datetime.utcnow()
                            - dt.timedelta(hours=50)
                            ).strftime("%Y-%m-%d %H:%M:%S"))
        s = mark_stale_referrals(stale_hours=48,
                                    escalate_to_dead_days=7)
        assert s["marked_stale"] == 1
        assert s["escalated_dead"] == 0  # 50h < 7天
        # 应该写了 referral_stale event
        n = count_contact_events(event_type="referral_stale", hours=24)
        assert n == 1

    def test_replied_excluded(self, tmp_db):
        """已 replied 的不应被标 stale."""
        from src.host.fb_store import mark_stale_referrals
        _seed_l2_lead("花子")
        old_at = (dt.datetime.utcnow()
                   - dt.timedelta(hours=50)
                   ).strftime("%Y-%m-%d %H:%M:%S")
        _seed_event_at("D1", "花子", "wa_referral_sent", old_at)
        _seed_event_at("D1", "花子", "wa_referral_replied", old_at)
        s = mark_stale_referrals(stale_hours=48)
        assert s["marked_stale"] == 0

    def test_old_sent_escalates_to_dead(self, tmp_db):
        """sent 8 天前 → 同时打 stale + dead."""
        from src.host.fb_store import mark_stale_referrals
        from src.host.lead_mesh import get_dossier
        cid = _seed_l2_lead("花子")
        _seed_event_at("D1", "花子", "wa_referral_sent",
                          (dt.datetime.utcnow()
                            - dt.timedelta(days=8)
                            ).strftime("%Y-%m-%d %H:%M:%S"))
        s = mark_stale_referrals(stale_hours=48,
                                    escalate_to_dead_days=7)
        assert s["marked_stale"] == 1
        assert s["escalated_dead"] == 1
        # 验 canonical tag
        d = get_dossier(cid)
        tags_csv = (d.get("canonical", {}).get("tags") or "")
        tags = {t.strip() for t in tags_csv.split(",") if t.strip()}
        assert "referral_stale" in tags
        assert "referral_dead" in tags

    def test_dry_run_no_writes(self, tmp_db):
        from src.host.fb_store import (mark_stale_referrals,
                                          count_contact_events)
        from src.host.lead_mesh import get_dossier
        cid = _seed_l2_lead("花子")
        _seed_event_at("D1", "花子", "wa_referral_sent",
                          (dt.datetime.utcnow()
                            - dt.timedelta(hours=50)
                            ).strftime("%Y-%m-%d %H:%M:%S"))
        s = mark_stale_referrals(stale_hours=48, dry_run=True)
        # 报告说 marked_stale=1 (统计目的) 但无实际写入
        assert s["dry_run"] is True
        assert s["marked_stale"] == 1
        # 没写 event
        n = count_contact_events(event_type="referral_stale", hours=24)
        assert n == 0
        # 没打 tag
        d = get_dossier(cid)
        assert "referral_stale" not in (d.get("tags") or [])

    def test_already_stale_skipped_for_remarking(self, tmp_db):
        """重复运行不应多次写 event 或重复 mark."""
        from src.host.fb_store import (mark_stale_referrals,
                                          count_contact_events)
        _seed_l2_lead("花子")
        _seed_event_at("D1", "花子", "wa_referral_sent",
                          (dt.datetime.utcnow()
                            - dt.timedelta(hours=50)
                            ).strftime("%Y-%m-%d %H:%M:%S"))
        s1 = mark_stale_referrals(stale_hours=48)
        assert s1["marked_stale"] == 1
        s2 = mark_stale_referrals(stale_hours=48)
        # 第 2 次跑: 已 tag stale, 走 already_stale 分支
        assert s2["marked_stale"] == 0
        assert s2["already_stale"] >= 1
        # 仅 1 个 stale event
        n = count_contact_events(event_type="referral_stale", hours=24)
        assert n == 1

    def test_already_stale_can_still_escalate(self, tmp_db):
        """已 stale 的 peer 后续达 7d → 升级 dead."""
        from src.host.fb_store import mark_stale_referrals
        from src.host.lead_mesh import get_dossier
        cid = _seed_l2_lead("花子")
        _seed_event_at("D1", "花子", "wa_referral_sent",
                          (dt.datetime.utcnow()
                            - dt.timedelta(days=8)
                            ).strftime("%Y-%m-%d %H:%M:%S"))
        # 第 1 次跑 escalate_days=10 不会 dead
        s1 = mark_stale_referrals(stale_hours=48,
                                     escalate_to_dead_days=10)
        assert s1["marked_stale"] == 1
        assert s1["escalated_dead"] == 0
        # 第 2 次 escalate_days=7 → 应升级 dead (already_stale + escalate)
        s2 = mark_stale_referrals(stale_hours=48,
                                     escalate_to_dead_days=7)
        assert s2["already_stale"] >= 1
        assert s2["escalated_dead"] == 1
        d = get_dossier(cid)
        tags_csv = (d.get("canonical", {}).get("tags") or "")
        assert "referral_dead" in tags_csv

    def test_per_device_filter(self, tmp_db):
        from src.host.fb_store import mark_stale_referrals
        _seed_l2_lead("花子")
        _seed_l2_lead("Maria")
        old = (dt.datetime.utcnow()
                - dt.timedelta(hours=50)
                ).strftime("%Y-%m-%d %H:%M:%S")
        _seed_event_at("D1", "花子", "wa_referral_sent", old)
        _seed_event_at("D2", "Maria", "wa_referral_sent", old)
        s = mark_stale_referrals(stale_hours=48, device_id="D1")
        assert s["marked_stale"] == 1
        # D2 没动
        s2 = mark_stale_referrals(stale_hours=48, device_id="D2")
        assert s2["marked_stale"] == 1


# ═══════════════════════════════════════════════════════════════════
# 20.2.2: facebook_mark_stale_referrals task
# ═══════════════════════════════════════════════════════════════════

class TestStaleTask:
    def test_dispatch_through_executor(self, tmp_db):
        from src.host.executor import _fb_mark_stale_referrals
        _seed_l2_lead("花子")
        _seed_event_at("D1", "花子", "wa_referral_sent",
                          (dt.datetime.utcnow()
                            - dt.timedelta(hours=50)
                            ).strftime("%Y-%m-%d %H:%M:%S"))
        ok, _, stats = _fb_mark_stale_referrals({"stale_hours": 48})
        assert ok
        assert stats["marked_stale"] == 1


# ═══════════════════════════════════════════════════════════════════
# 20.2.3: funnel + daily summary 集成
# ═══════════════════════════════════════════════════════════════════

class TestFunnelStale:
    def test_funnel_includes_stale_field(self, tmp_db):
        from src.host.line_pool import referral_funnel
        f = referral_funnel(hours_window=24)
        assert "stale" in f
        assert f["stale"] == 0
        assert "stale_rate" in f

    def test_funnel_counts_stale_events(self, tmp_db):
        from src.host.line_pool import referral_funnel
        from src.host.fb_store import record_contact_event
        _seed_l2_lead("花子")
        record_contact_event("D1", "花子", "wa_referral_sent",
                               skip_sanitize=True)
        record_contact_event("D1", "花子", "referral_stale",
                               skip_sanitize=True)
        f = referral_funnel(hours_window=24)
        assert f["sent"] == 1
        assert f["stale"] == 1
        assert f["stale_rate"] == 1.0  # 100% sent 也 stale

    def test_daily_summary_funnel_has_stale(self, tmp_db, tmp_path,
                                                 monkeypatch):
        from src.host.executor import _fb_daily_referral_summary
        from src.host.fb_store import record_contact_event
        _seed_l2_lead("花子")
        record_contact_event("D1", "花子", "wa_referral_sent",
                               skip_sanitize=True)
        record_contact_event("D1", "花子", "referral_stale",
                               skip_sanitize=True)
        monkeypatch.chdir(tmp_path)
        ok, _, stats = _fb_daily_referral_summary({
            "hours_window": 24, "write_file": False, "send_webhook": False,
            "regions": []})
        assert stats["summary"]["funnel"]["stale"] == 1
