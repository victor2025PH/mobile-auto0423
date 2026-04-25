# -*- coding: utf-8 -*-
"""真人客服接管业务层 (PR-6).

围绕 ``lead_handoffs`` 已有的状态机, 加 4 个真人客服动作:

1. assign_to_human(handoff_id, username) — 真人按"我接手"
2. record_human_reply(handoff_id, username, text) — 真人后台输入消息
3. record_internal_note(handoff_id, username, note) — 真人加内部备注
4. record_outcome(handoff_id, username, outcome, notes) — 真人标结果

所有动作都:
- 写 lead_handoffs 对应字段 (assigned_to_username / replies_json / notes_json / outcome)
- append lead_journey 事件 (actor='human:<username>')
- 第一次 assign 时调 ``ai_takeover_state.mark_taken_over`` 暂停 AI 自动回
- outcome 是终态 (converted/lost) 时调 ``ai_takeover_state.release``

PR-6 范围: 后端 API + schema 扩展, UI 扩展 (lead-mesh-ui.js) 单独 PR-6.5.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from src.host.database import _connect

logger = logging.getLogger(__name__)

OUTCOME_CONVERTED = "converted"
OUTCOME_LOST = "lost"
OUTCOME_PENDING = "pending_followup"
VALID_OUTCOMES = (OUTCOME_CONVERTED, OUTCOME_LOST, OUTCOME_PENDING)

TERMINAL_OUTCOMES = (OUTCOME_CONVERTED, OUTCOME_LOST)


def _now_iso() -> str:
    return _dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _human_actor(username: str) -> str:
    return f"human:{username}"


def _get_handoff(handoff_id: str) -> Optional[Dict[str, Any]]:
    """读 lead_handoffs 单行. 不存在返 None."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM lead_handoffs WHERE handoff_id = ?",
            (handoff_id,),
        ).fetchone()
    if not row:
        return None
    return dict(row)


def _journey_event(canonical_id: str, action: str,
                   actor: str, data: Dict[str, Any]) -> None:
    """append lead_journey 事件 (跟既有 lead_mesh.journey 同 schema)."""
    if not canonical_id:
        return
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO lead_journey "
                "(canonical_id, action, actor, data_json, at) "
                "VALUES (?, ?, ?, ?, ?)",
                (canonical_id, action, actor,
                 json.dumps(data, ensure_ascii=False), _now_iso()),
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[customer_service] journey event failed: %s", exc)


# ── 1. assign_to_human ───────────────────────────────────────────────
def assign_to_human(
    handoff_id: str,
    username: str,
    *,
    peer_name_hint: str = "",
    device_id_hint: str = "",
    takeover_ttl_sec: float = 3600.0,
) -> Dict[str, Any]:
    """真人按"我接手". 写 assigned_to_username + 暂停 worker AI 自动回.

    peer_name_hint / device_id_hint: 给 ai_takeover_state 用的 hint
    (handoff 表里没有这两个字段, 由调用方传或留空).
    """
    if not handoff_id or not username:
        raise ValueError("handoff_id / username 必填")
    rec = _get_handoff(handoff_id)
    if not rec:
        raise KeyError(f"handoff {handoff_id} not found")
    if rec.get("assigned_to_username") and rec["assigned_to_username"] != username:
        raise RuntimeError(
            f"handoff {handoff_id} 已被 {rec['assigned_to_username']} 接管"
        )

    now = _now_iso()
    with _connect() as conn:
        conn.execute(
            "UPDATE lead_handoffs SET assigned_to_username = ?, "
            "assigned_at = ?, state_updated_at = ? "
            "WHERE handoff_id = ?",
            (username, now, now, handoff_id),
        )
        conn.commit()

    # 暂停 worker AI 自动回 (peer / device 由调用方 hint 提供)
    if peer_name_hint and device_id_hint:
        try:
            from src.host.ai_takeover_state import mark_taken_over
            mark_taken_over(
                peer_name=peer_name_hint,
                device_id=device_id_hint,
                by_username=username,
                ttl_sec=takeover_ttl_sec,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[customer_service] mark_taken_over 失败: %s", exc)

    _journey_event(
        rec.get("canonical_id", ""),
        action="handoff_assigned_to_human",
        actor=_human_actor(username),
        data={
            "handoff_id": handoff_id,
            "channel": rec.get("channel", ""),
            "ai_paused": bool(peer_name_hint and device_id_hint),
        },
    )
    return {"handoff_id": handoff_id, "assigned_to_username": username,
            "assigned_at": now}


# ── 2. record_human_reply ────────────────────────────────────────────
def record_human_reply(
    handoff_id: str,
    username: str,
    text: str,
    *,
    sent_via_worker: bool = False,
    extra_meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """真人在后台输入了一条回复. 写到 customer_service_replies_json.

    sent_via_worker: 是否已经通过 agent_mesh → worker 发出 (PR-6.5 真接).
    PR-6 当前阶段先记录, worker 端发消息能力放 PR-6.5.
    """
    if not handoff_id or not username or not text:
        raise ValueError("handoff_id / username / text 必填")
    rec = _get_handoff(handoff_id)
    if not rec:
        raise KeyError(f"handoff {handoff_id} not found")
    assigned = rec.get("assigned_to_username") or ""
    if assigned and assigned != username:
        raise RuntimeError(
            f"该 handoff 已被 {assigned} 接管, 你不是当前接管人"
        )

    replies = []
    try:
        replies = json.loads(rec.get("customer_service_replies_json") or "[]")
    except Exception:  # noqa: BLE001
        replies = []
    entry = {
        "by": username,
        "text": text,
        "sent_via_worker": sent_via_worker,
        "at": _now_iso(),
    }
    if extra_meta:
        entry["meta"] = extra_meta
    replies.append(entry)

    with _connect() as conn:
        conn.execute(
            "UPDATE lead_handoffs SET customer_service_replies_json = ?, "
            "state_updated_at = ? WHERE handoff_id = ?",
            (json.dumps(replies, ensure_ascii=False), entry["at"], handoff_id),
        )
        conn.commit()

    _journey_event(
        rec.get("canonical_id", ""),
        action="human_reply_recorded",
        actor=_human_actor(username),
        data={"handoff_id": handoff_id, "len": len(text),
              "sent_via_worker": sent_via_worker},
    )
    return {"handoff_id": handoff_id, "replies_count": len(replies),
            "last_at": entry["at"]}


# ── 3. record_internal_note ──────────────────────────────────────────
def record_internal_note(
    handoff_id: str,
    username: str,
    note: str,
) -> Dict[str, Any]:
    """真人加一条内部备注 (不发给客户)."""
    if not handoff_id or not username or not note:
        raise ValueError("handoff_id / username / note 必填")
    rec = _get_handoff(handoff_id)
    if not rec:
        raise KeyError(f"handoff {handoff_id} not found")

    notes: List[Dict[str, Any]] = []
    try:
        notes = json.loads(rec.get("internal_notes_json") or "[]")
    except Exception:  # noqa: BLE001
        notes = []
    entry = {"by": username, "note": note, "at": _now_iso()}
    notes.append(entry)

    with _connect() as conn:
        conn.execute(
            "UPDATE lead_handoffs SET internal_notes_json = ?, "
            "state_updated_at = ? WHERE handoff_id = ?",
            (json.dumps(notes, ensure_ascii=False), entry["at"], handoff_id),
        )
        conn.commit()

    _journey_event(
        rec.get("canonical_id", ""),
        action="internal_note_added",
        actor=_human_actor(username),
        data={"handoff_id": handoff_id, "note_len": len(note)},
    )
    return {"handoff_id": handoff_id, "notes_count": len(notes),
            "last_at": entry["at"]}


# ── 4. record_outcome ────────────────────────────────────────────────
def record_outcome(
    handoff_id: str,
    username: str,
    outcome: str,
    notes: str = "",
    *,
    peer_name_hint: str = "",
    device_id_hint: str = "",
) -> Dict[str, Any]:
    """真人标结果 (converted / lost / pending_followup).

    converted / lost 是终态: 释放 ai_takeover_state, 让机器人(理论上不会再有
    互动了)能服务别的客户.

    pending_followup: 留待跟进, 不释放接管状态.
    """
    if outcome not in VALID_OUTCOMES:
        raise ValueError(
            f"invalid outcome '{outcome}', must be {VALID_OUTCOMES}"
        )
    rec = _get_handoff(handoff_id)
    if not rec:
        raise KeyError(f"handoff {handoff_id} not found")

    now = _now_iso()
    with _connect() as conn:
        conn.execute(
            "UPDATE lead_handoffs SET outcome = ?, outcome_notes = ?, "
            "outcome_at = ?, state_updated_at = ? WHERE handoff_id = ?",
            (outcome, notes or "", now, now, handoff_id),
        )
        conn.commit()

    # 终态释放 ai 接管
    if outcome in TERMINAL_OUTCOMES and peer_name_hint and device_id_hint:
        try:
            from src.host.ai_takeover_state import release
            release(peer_name=peer_name_hint, device_id=device_id_hint)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[customer_service] ai_takeover release 失败: %s", exc)

    _journey_event(
        rec.get("canonical_id", ""),
        action="handoff_outcome_recorded",
        actor=_human_actor(username),
        data={
            "handoff_id": handoff_id,
            "outcome": outcome,
            "is_terminal": outcome in TERMINAL_OUTCOMES,
        },
    )
    return {"handoff_id": handoff_id, "outcome": outcome, "at": now}


# ── 查询 helper (给 router 用) ───────────────────────────────────────
def list_assigned_to_user(username: str, limit: int = 50) -> List[Dict[str, Any]]:
    """列出某 username 当前接管的 handoff (state in pending/acknowledged)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM lead_handoffs WHERE assigned_to_username = ? "
            "AND outcome = '' "
            "ORDER BY assigned_at DESC LIMIT ?",
            (username, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def get_handoff_full(handoff_id: str) -> Optional[Dict[str, Any]]:
    """读 handoff 全字段 (含 replies / notes / outcome) — 给真人后台详情页."""
    rec = _get_handoff(handoff_id)
    if not rec:
        return None
    # 解析 jsonb 字段方便前端用
    try:
        rec["customer_service_replies"] = json.loads(
            rec.get("customer_service_replies_json") or "[]"
        )
    except Exception:  # noqa: BLE001
        rec["customer_service_replies"] = []
    try:
        rec["internal_notes"] = json.loads(
            rec.get("internal_notes_json") or "[]"
        )
    except Exception:  # noqa: BLE001
        rec["internal_notes"] = []
    return rec
