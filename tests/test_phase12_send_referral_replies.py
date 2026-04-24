# -*- coding: utf-8 -*-
"""Phase 12 Alpha (2026-04-25): facebook_send_referral_replies 单测.

覆盖:
  * happy path: line_dispatch_planned event → send_message 成功 →
    wa_referral_sent event 写入 + mark_dispatch_outcome(sent)
  * strict_device_match 下 original_device_id 不匹配 → skipped_device
  * peer 24h 内已有 wa_referral_sent → skipped_dedup
  * send_message 返 False → failed 计数 + mark(failed)
  * send_message 抛异常 → failed (不崩)
  * dispatch_mode != 'messenger_text' → skipped_mode
  * limit 上限生效

mock fb.send_message, 不依赖真机.
"""
from __future__ import annotations

import json

import pytest
from unittest.mock import MagicMock


def _seed_planned_event(device_id: str, peer_name: str, *,
                         line_id: str = "along2026",
                         line_account_id: int = 1,
                         dispatch_mode: str = "messenger_text",
                         message_template: str = "LINE: along2026",
                         canonical_id: str = "",
                         original_device_id: str = "") -> int:
    from src.host.fb_store import record_contact_event
    return record_contact_event(
        device_id, peer_name,
        "line_dispatch_planned",
        preset_key=f"line_pool:{line_account_id}",
        meta={
            "line_id": line_id,
            "line_account_id": line_account_id,
            "dispatch_mode": dispatch_mode,
            "message_template": message_template,
            "canonical_id": canonical_id,
            "original_device_id": original_device_id or device_id,
        },
    )


def _make_fb_stub(send_return=True, send_raises=None):
    """构造只实现 send_message 的 fb mock."""
    fb = MagicMock()
    if send_raises:
        fb.send_message.side_effect = send_raises
    else:
        fb.send_message.return_value = send_return
    return fb


class TestSendReferralReplies:
    def test_happy_path_sends_and_records(self, tmp_db):
        from src.host.executor import _fb_send_referral_replies
        from src.host.fb_store import count_contact_events
        from src.host import line_pool as lp

        # 建一个 account + planned event
        aid = lp.add("along2026", region="jp", daily_cap=20)
        # 先 allocate 一次模拟真实 dispatcher 写的 planned log
        lp.allocate(region="jp", canonical_id="c1",
                     peer_name="花子", source_device_id="DEV1")
        _seed_planned_event("DEV1", "花子",
                             line_account_id=aid,
                             canonical_id="c1",
                             original_device_id="DEV1")

        fb = _make_fb_stub(send_return=True)
        ok, _msg, stats = _fb_send_referral_replies(fb, "DEV1", {
            "hours_window": 2, "strict_device_match": True,
        })
        assert ok
        assert stats["sent"] == 1
        assert stats["failed"] == 0
        # fb.send_message 被调了, 参数含 message_template
        args, kwargs = fb.send_message.call_args
        assert kwargs.get("recipient") == "花子"
        assert "along2026" in kwargs.get("message", "")

        # wa_referral_sent event 写了
        n = count_contact_events(device_id="DEV1", peer_name="花子",
                                   event_type="wa_referral_sent", hours=2)
        assert n == 1

        # dispatch_log outcome=sent
        log = lp.recent_dispatch_log(limit=10)
        assert any(r["line_account_id"] == aid and r["status"] == "sent"
                   for r in log)

    def test_strict_device_match_skips_other_device(self, tmp_db):
        from src.host.executor import _fb_send_referral_replies
        _seed_planned_event("DEV_B", "美咲",
                             original_device_id="DEV_B",  # 不匹配 resolved
                             line_account_id=1)
        fb = _make_fb_stub()
        ok, _, stats = _fb_send_referral_replies(fb, "DEV_A", {
            "hours_window": 2, "strict_device_match": True,
        })
        assert ok and stats["sent"] == 0
        assert stats["skipped_device"] == 1
        fb.send_message.assert_not_called()

    def test_strict_false_sends_across_device(self, tmp_db):
        from src.host.executor import _fb_send_referral_replies
        from src.host import line_pool as lp
        aid = lp.add("along2026", region="jp")
        _seed_planned_event("DEV_B", "恵",
                             line_account_id=aid,
                             original_device_id="DEV_B")
        fb = _make_fb_stub(send_return=True)
        ok, _, stats = _fb_send_referral_replies(fb, "DEV_A", {
            "hours_window": 2, "strict_device_match": False,
        })
        assert stats["sent"] == 1

    def test_dedupe_when_wa_already_sent(self, tmp_db):
        from src.host.executor import _fb_send_referral_replies
        from src.host.fb_store import record_contact_event
        from src.host import line_pool as lp
        aid = lp.add("along2026", region="jp")
        _seed_planned_event("DEV1", "由美",
                             line_account_id=aid,
                             original_device_id="DEV1")
        # 已经有人工发过一条 wa_referral_sent
        record_contact_event("DEV1", "由美", "wa_referral_sent",
                              meta={"line_id": "prev"})
        fb = _make_fb_stub(send_return=True)
        ok, _, stats = _fb_send_referral_replies(fb, "DEV1", {})
        assert stats["skipped_dedup"] == 1
        assert stats["sent"] == 0
        fb.send_message.assert_not_called()

    def test_send_failed_marks_outcome(self, tmp_db):
        from src.host.executor import _fb_send_referral_replies
        from src.host import line_pool as lp
        aid = lp.add("along2026", region="jp")
        lp.allocate(region="jp", canonical_id="c1", peer_name="裕子",
                     source_device_id="DEV1")
        _seed_planned_event("DEV1", "裕子",
                             line_account_id=aid,
                             original_device_id="DEV1")
        fb = _make_fb_stub(send_return=False)
        ok, _, stats = _fb_send_referral_replies(fb, "DEV1", {})
        assert ok and stats["failed"] == 1
        log = lp.recent_dispatch_log()
        assert any(r["line_account_id"] == aid and r["status"] == "failed"
                   for r in log)

    def test_send_exception_caught_and_counted(self, tmp_db):
        from src.host.executor import _fb_send_referral_replies
        from src.host import line_pool as lp
        aid = lp.add("along2026", region="jp")
        _seed_planned_event("DEV1", "香織",
                             line_account_id=aid,
                             original_device_id="DEV1")
        fb = _make_fb_stub(send_raises=RuntimeError("messenger frozen"))
        ok, _, stats = _fb_send_referral_replies(fb, "DEV1", {})
        assert ok and stats["failed"] == 1
        # outcome note 带异常类名
        out = stats["outcomes"][0]
        assert "send_message_exception" in (out.get("note") or "")

    def test_line_direct_send_mode_skipped(self, tmp_db):
        """dispatch_mode=line_direct_send 留给 LINE automation 处理, 本 task
        不动 → skipped_mode."""
        from src.host.executor import _fb_send_referral_replies
        _seed_planned_event("DEV1", "真理子",
                             dispatch_mode="line_direct_send",
                             line_account_id=1,
                             original_device_id="DEV1")
        fb = _make_fb_stub()
        ok, _, stats = _fb_send_referral_replies(fb, "DEV1", {})
        assert stats["skipped_mode"] == 1
        assert stats["sent"] == 0
        fb.send_message.assert_not_called()

    def test_limit_caps_send_count(self, tmp_db):
        from src.host.executor import _fb_send_referral_replies
        from src.host import line_pool as lp
        aid = lp.add("along2026", region="jp", daily_cap=100)
        for i in range(5):
            _seed_planned_event("DEV1", f"U{i}",
                                 line_account_id=aid,
                                 original_device_id="DEV1")
        fb = _make_fb_stub(send_return=True)
        ok, _, stats = _fb_send_referral_replies(fb, "DEV1",
                                                    {"limit": 2})
        assert stats["sent"] == 2
        assert fb.send_message.call_count == 2
