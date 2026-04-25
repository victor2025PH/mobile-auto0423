# -*- coding: utf-8 -*-
"""customer_sync_bridge 单测 — mock central_push_client, 验证 facebook 事件
正确翻译为 push 调用 (canonical_id / event_type / channel 映射).

bridge 设计是 fire_and_forget + 全 try/except 静默, 单测要确认:
1. canonical_source = "facebook_name", canonical_id = f"{device}::{peer}"
2. friend_request status=sent → event_type=friend_request_sent
3. friend_request status=risk → event_type=friend_request_risk
4. greeting_sent fallback=False → event_type=greeting_sent + record_chat outgoing
5. greeting_sent fallback=True → event_type=greeting_fallback
6. push_client 内部抛异常时 bridge 静默返 None (不影响 caller)
7. 空 device_id / peer_name 直接早退
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.host import customer_sync_bridge as bridge


@pytest.fixture
def mock_pc(monkeypatch):
    """mock central_push_client 模块的 upsert / record_event / record_chat."""
    pc = MagicMock()
    pc.upsert_customer = MagicMock(return_value="cust-uuid-mocked")
    pc.record_event = MagicMock(return_value=None)
    pc.record_chat = MagicMock(return_value=None)

    # bridge 用 lazy import (`from src.host.central_push_client import ...`),
    # 所以要 patch 模块本身的属性.
    import src.host.central_push_client as _cp
    monkeypatch.setattr(_cp, "upsert_customer", pc.upsert_customer)
    monkeypatch.setattr(_cp, "record_event", pc.record_event)
    monkeypatch.setattr(_cp, "record_chat", pc.record_chat)
    # 强制 worker_id 稳定可断言
    monkeypatch.setattr(bridge, "_safe_worker_id", lambda: "w-test")
    return pc


# ── canonical_id 约定 ────────────────────────────────────────────────
def test_canonical_id_composes_device_and_peer():
    cid = bridge._build_canonical_id("d-abc", "Alice")
    assert cid == "d-abc::Alice"


# ── friend_request_sent ──────────────────────────────────────────────
def test_friend_request_sent_calls_upsert_then_event(mock_pc):
    cid = bridge.sync_friend_request_sent(
        "d1", "Alice",
        status="sent",
        persona_key="hostess_jp",
        preset_key="growth_v2",
        source="search",
        note="hi there",
    )
    assert cid == "cust-uuid-mocked"

    mock_pc.upsert_customer.assert_called_once()
    upsert_kwargs = mock_pc.upsert_customer.call_args.kwargs
    assert upsert_kwargs["canonical_source"] == "facebook_name"
    assert upsert_kwargs["canonical_id"] == "d1::Alice"
    assert upsert_kwargs["primary_name"] == "Alice"
    assert upsert_kwargs["worker_id"] == "w-test"
    assert upsert_kwargs["device_id"] == "d1"
    assert upsert_kwargs["fire_and_forget"] is True
    assert upsert_kwargs["ai_profile"] == {"persona_key": "hostess_jp"}

    mock_pc.record_event.assert_called_once()
    evt_kwargs = mock_pc.record_event.call_args.kwargs
    assert evt_kwargs["event_type"] == "friend_request_sent"
    assert evt_kwargs["customer_id"] == "cust-uuid-mocked"
    assert evt_kwargs["device_id"] == "d1"
    assert evt_kwargs["worker_id"] == "w-test"
    assert evt_kwargs["meta"]["persona_key"] == "hostess_jp"
    assert evt_kwargs["meta"]["has_note"] is True


def test_friend_request_risk_status_maps_to_risk_event(mock_pc):
    bridge.sync_friend_request_sent("d1", "Bob", status="risk")
    evt_kwargs = mock_pc.record_event.call_args.kwargs
    assert evt_kwargs["event_type"] == "friend_request_risk"


def test_friend_request_no_persona_key_no_ai_profile(mock_pc):
    bridge.sync_friend_request_sent("d1", "Carol", status="sent")
    upsert_kwargs = mock_pc.upsert_customer.call_args.kwargs
    assert upsert_kwargs["ai_profile"] is None


# ── greeting_sent ────────────────────────────────────────────────────
def test_greeting_sent_writes_event_and_chat(mock_pc):
    cid = bridge.sync_greeting_sent(
        "d1", "Alice",
        greeting="こんにちは!",
        template_id="jp:3",
        preset_key="growth_v2",
        persona_key="hostess_jp",
        phase="warmup",
    )
    assert cid == "cust-uuid-mocked"

    evt_kwargs = mock_pc.record_event.call_args.kwargs
    assert evt_kwargs["event_type"] == "greeting_sent"
    assert evt_kwargs["meta"]["template_id"] == "jp:3"
    assert evt_kwargs["meta"]["msg_len"] == len("こんにちは!")

    chat_kwargs = mock_pc.record_chat.call_args.kwargs
    assert chat_kwargs["channel"] == "facebook"
    assert chat_kwargs["direction"] == "outgoing"
    assert chat_kwargs["content"] == "こんにちは!"
    assert chat_kwargs["template_id"] == "jp:3"
    assert chat_kwargs["ai_generated"] is False
    assert chat_kwargs["device_id"] == "d1"


def test_greeting_fallback_uses_fallback_event(mock_pc):
    bridge.sync_greeting_sent(
        "d1", "Alice", greeting="hi", fallback=True,
    )
    evt_kwargs = mock_pc.record_event.call_args.kwargs
    assert evt_kwargs["event_type"] == "greeting_fallback"


# ── 早退保护 ─────────────────────────────────────────────────────────
def test_empty_device_id_early_returns_none(mock_pc):
    assert bridge.sync_friend_request_sent("", "Alice") is None
    assert bridge.sync_greeting_sent("", "Alice", greeting="hi") is None
    mock_pc.upsert_customer.assert_not_called()


def test_empty_peer_name_early_returns_none(mock_pc):
    assert bridge.sync_friend_request_sent("d1", "") is None
    assert bridge.sync_greeting_sent("d1", "", greeting="hi") is None
    mock_pc.upsert_customer.assert_not_called()


def test_empty_greeting_early_returns_none(mock_pc):
    assert bridge.sync_greeting_sent("d1", "Alice", greeting="") is None
    mock_pc.upsert_customer.assert_not_called()


# ── 异常静默 ─────────────────────────────────────────────────────────
def test_upsert_exception_returns_none_no_event(monkeypatch):
    """upsert 抛异常: bridge 静默返 None, 后续 record_event 不调."""
    import src.host.central_push_client as _cp

    def _raise(**_):
        raise RuntimeError("network down")

    monkeypatch.setattr(_cp, "upsert_customer", _raise)
    record_event_calls = []
    monkeypatch.setattr(
        _cp, "record_event",
        lambda **kw: record_event_calls.append(kw),
    )

    result = bridge.sync_friend_request_sent("d1", "Alice", status="sent")
    assert result is None
    assert record_event_calls == []


def test_record_event_exception_does_not_propagate(monkeypatch):
    """upsert OK 但 record_event 抛异常: bridge 静默, 仍返回 customer_id."""
    import src.host.central_push_client as _cp
    monkeypatch.setattr(_cp, "upsert_customer", lambda **kw: "cust-x")
    monkeypatch.setattr(
        _cp, "record_event",
        lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    cid = bridge.sync_friend_request_sent("d1", "Alice", status="sent")
    assert cid == "cust-x"
