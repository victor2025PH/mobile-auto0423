# -*- coding: utf-8 -*-
"""Handoff — 引流交接状态机 (Phase 5)。

状态机::

    pending ──ack──► acknowledged ──complete──► completed
       │                │
       │                └──reject──► rejected
       │
       └─────expire───► expired (72h 未 ack 自动过期)

所有状态转移都会:
  1. 更新 lead_handoffs.state + state_updated_at
  2. append lead_journey 相应事件
  3. enqueue webhook (dispatch 异步发)

脱敏: conversation_snapshot 入库前调 _sanitize() 替换手机/邮箱/ID。
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import re
import uuid
from typing import Any, Dict, List, Optional

from src.host.database import _connect

logger = logging.getLogger(__name__)

STATE_PENDING = "pending"
STATE_ACKNOWLEDGED = "acknowledged"
STATE_COMPLETED = "completed"
STATE_REJECTED = "rejected"
STATE_EXPIRED = "expired"
STATE_DUPLICATE_BLOCKED = "duplicate_blocked"

ACTIVE_STATES = (STATE_PENDING, STATE_ACKNOWLEDGED)
TERMINAL_STATES = (STATE_COMPLETED, STATE_REJECTED, STATE_EXPIRED,
                   STATE_DUPLICATE_BLOCKED)

DEFAULT_EXPIRE_HOURS = 72


# ─── 脱敏 ────────────────────────────────────────────────────────────
_PHONE_RE = re.compile(r"(\+?\d[\d\s\-()]{7,}\d)")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_LINE_ID_RE = re.compile(r"@[A-Za-z][\w.\-]{2,19}")


def _sanitize(text: str) -> str:
    """对话原文脱敏, 避免交接单二次泄露。"""
    if not text:
        return ""
    s = text
    s = _EMAIL_RE.sub("[EMAIL]", s)
    s = _PHONE_RE.sub("[PHONE]", s)
    # LINE @id 保留类型标签, 不留原值
    s = _LINE_ID_RE.sub("[LINE_ID]", s)
    return s


def _sanitize_snapshot(turns: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for t in turns or []:
        t2 = dict(t)
        if "text" in t2:
            t2["text"] = _sanitize(str(t2["text"]))
        if "message_text" in t2:
            t2["message_text"] = _sanitize(str(t2["message_text"]))
        out.append(t2)
    return out


def _now_iso() -> str:
    return _dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


# ─── CRUD ────────────────────────────────────────────────────────────

def create_handoff(*, canonical_id: str,
                    source_agent: str,
                    channel: str,
                    source_device: str = "",
                    target_agent: str = "",
                    receiver_account_key: str = "",
                    conversation_snapshot: Optional[List[Dict[str, Any]]] = None,
                    snippet_sent: str = "",
                    enqueue_webhook: bool = True) -> str:
    """创建交接单, 返回 handoff_id (UUID)。

    自动:
      * snapshot 脱敏
      * append lead_journey: handoff_created
      * 如果 enqueue_webhook=True, 写 webhook_dispatches 待发

    **不做**:
      * 不做去重检查 (那是调用方的 gate 职责, 防止 race)
      * 不同步发 webhook (异步由 dispatcher 取走)
    """
    if not canonical_id or not source_agent or not channel:
        raise ValueError("canonical_id / source_agent / channel 必填")
    handoff_id = str(uuid.uuid4())
    snap = _sanitize_snapshot(conversation_snapshot or [])
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO lead_handoffs"
                " (handoff_id, canonical_id, source_agent, source_device,"
                "  target_agent, channel, receiver_account_key,"
                "  conversation_snapshot_json, snippet_sent, state)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (handoff_id, canonical_id, source_agent, source_device,
                 target_agent, channel, receiver_account_key,
                 json.dumps(snap, ensure_ascii=False, default=str),
                 snippet_sent, STATE_PENDING),
            )
    except Exception as e:
        logger.warning("[handoff] create 失败: %s", e)
        return ""

    # Journey
    try:
        from .journey import append_journey
        append_journey(canonical_id, actor=source_agent, action="handoff_created",
                       actor_device=source_device, platform=channel,
                       data={"handoff_id": handoff_id,
                             "receiver": receiver_account_key,
                             "snapshot_turns": len(snap)})
    except Exception:
        pass

    # Webhook
    if enqueue_webhook:
        try:
            from .webhook_dispatcher import enqueue_webhook as eq
            eq(event_type="handoff.created",
               payload={
                   "handoff_id": handoff_id,
                   "canonical_id": canonical_id,
                   "source_agent": source_agent,
                   "channel": channel,
                   "receiver_account_key": receiver_account_key,
                   "created_at": _now_iso(),
                   "snapshot_turns": len(snap),
                   "snippet_sent": snippet_sent,
               },
               related_canonical_id=canonical_id,
               related_handoff_id=handoff_id)
        except Exception as e:
            logger.debug("[handoff] enqueue_webhook 失败(不阻塞创建): %s", e)
    return handoff_id


def get_handoff(handoff_id: str) -> Optional[Dict[str, Any]]:
    if not handoff_id:
        return None
    try:
        with _connect() as conn:
            conn.row_factory = __import__("sqlite3").Row
            row = conn.execute(
                "SELECT * FROM lead_handoffs WHERE handoff_id=?",
                (handoff_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            d["conversation_snapshot"] = json.loads(
                d.pop("conversation_snapshot_json") or "[]")
        except Exception:
            d["conversation_snapshot"] = []
        return d
    except Exception:
        return None


def list_handoffs(*,
                    state: str = "",
                    receiver_account_key: str = "",
                    canonical_id: str = "",
                    channel: str = "",
                    limit: int = 100) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM lead_handoffs WHERE 1=1"
    params: list = []
    if state:
        sql += " AND state=?"
        params.append(state)
    if receiver_account_key:
        sql += " AND receiver_account_key=?"
        params.append(receiver_account_key)
    if canonical_id:
        sql += " AND canonical_id=?"
        params.append(canonical_id)
    if channel:
        sql += " AND channel=?"
        params.append(channel)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(int(limit))
    try:
        with _connect() as conn:
            conn.row_factory = __import__("sqlite3").Row
            rows = conn.execute(sql, params).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["conversation_snapshot"] = json.loads(
                    d.pop("conversation_snapshot_json") or "[]")
            except Exception:
                d["conversation_snapshot"] = []
            out.append(d)
        return out
    except Exception as e:
        logger.debug("[handoff] list 失败: %s", e)
        return []


# ─── 状态转移 ────────────────────────────────────────────────────────

def _transition(handoff_id: str, from_states: tuple,
                 to_state: str, *,
                 by: str,
                 notes: str = "",
                 journey_action: str = "",
                 webhook_event: str = "") -> bool:
    """通用状态转移。from_states 定义允许的起始态。"""
    if not handoff_id or not to_state or not by:
        return False
    # 先取当前状态确认
    row = get_handoff(handoff_id)
    if not row:
        return False
    if row["state"] not in from_states:
        logger.info("[handoff] 拒绝转移 %s (当前=%s, 不在 %s)",
                    handoff_id[:12], row["state"], from_states)
        return False
    try:
        with _connect() as conn:
            cur = conn.execute(
                "UPDATE lead_handoffs SET state=?, state_updated_at=datetime('now'),"
                " state_notes=? WHERE handoff_id=? AND state IN ({})".format(
                    ",".join(["?"] * len(from_states))),
                (to_state, notes or "", handoff_id, *from_states),
            )
            if (cur.rowcount or 0) == 0:
                return False  # 并发抢占
    except Exception as e:
        logger.warning("[handoff] transition 失败: %s", e)
        return False
    # journey
    try:
        from .journey import append_journey
        append_journey(row["canonical_id"], actor=by,
                       action=journey_action or f"handoff_{to_state}",
                       platform=row.get("channel") or "",
                       data={"handoff_id": handoff_id,
                             "from_state": row["state"],
                             "notes": notes})
    except Exception:
        pass
    # webhook
    if webhook_event:
        try:
            from .webhook_dispatcher import enqueue_webhook as eq
            eq(event_type=webhook_event,
               payload={
                   "handoff_id": handoff_id,
                   "canonical_id": row["canonical_id"],
                   "new_state": to_state,
                   "transitioned_by": by,
                   "notes": notes,
                   "at": _now_iso(),
               },
               related_canonical_id=row["canonical_id"],
               related_handoff_id=handoff_id)
        except Exception:
            pass
    return True


def acknowledge_handoff(handoff_id: str, *, by: str, notes: str = "") -> bool:
    """接收方标记"已看到"。pending → acknowledged。"""
    return _transition(handoff_id, (STATE_PENDING,), STATE_ACKNOWLEDGED,
                        by=by, notes=notes,
                        journey_action="handoff_acknowledged",
                        webhook_event="handoff.acknowledged")


def complete_handoff(handoff_id: str, *, by: str, notes: str = "") -> bool:
    """接收方标记"已接上对话"。pending|ack → completed。"""
    return _transition(handoff_id, (STATE_PENDING, STATE_ACKNOWLEDGED),
                        STATE_COMPLETED, by=by, notes=notes,
                        journey_action="handoff_completed",
                        webhook_event="handoff.completed")


def reject_handoff(handoff_id: str, *, by: str, notes: str = "") -> bool:
    """接收方标记"对方拒接/放弃"。pending|ack → rejected。"""
    return _transition(handoff_id, (STATE_PENDING, STATE_ACKNOWLEDGED),
                        STATE_REJECTED, by=by, notes=notes,
                        journey_action="handoff_rejected",
                        webhook_event="handoff.rejected")


def expire_pending_handoffs(expire_hours: int = DEFAULT_EXPIRE_HOURS) -> int:
    """定时任务调用: 批量过期超过 N 小时未 ack 的 pending。

    返回: 被过期的条数。
    """
    cutoff = (_dt.datetime.utcnow() - _dt.timedelta(hours=expire_hours)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    try:
        with _connect() as conn:
            conn.row_factory = __import__("sqlite3").Row
            rows = conn.execute(
                "SELECT handoff_id, canonical_id, channel FROM lead_handoffs"
                " WHERE state=? AND created_at < ?",
                (STATE_PENDING, cutoff)).fetchall()
        count = 0
        for r in rows:
            hid = r["handoff_id"]
            if _transition(hid, (STATE_PENDING,), STATE_EXPIRED,
                            by="system:expire_job",
                            notes=f"自动过期 (pending>{expire_hours}h)",
                            journey_action="handoff_expired",
                            webhook_event="handoff.expired"):
                count += 1
        return count
    except Exception as e:
        logger.warning("[handoff] expire_pending 失败: %s", e)
        return 0


# ─── 去重检查 (供 B 发引流前调用) ───────────────────────────────────

def check_duplicate_handoff(canonical_id: str,
                             channel: str,
                             since_days: int = 30) -> Optional[Dict[str, Any]]:
    """查询指定 lead 在指定渠道最近 N 天内是否已有活跃/已完成的 handoff。

    有 → 返回那条 handoff 的关键字段, 调用方应跳过再次引流。
    无 → None。
    """
    if not canonical_id or not channel:
        return None
    cutoff = (_dt.datetime.utcnow() - _dt.timedelta(days=since_days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    try:
        with _connect() as conn:
            conn.row_factory = __import__("sqlite3").Row
            row = conn.execute(
                "SELECT handoff_id, state, created_at, source_agent FROM lead_handoffs"
                " WHERE canonical_id=? AND channel=? AND created_at >= ?"
                " AND state NOT IN (?, ?)"
                " ORDER BY created_at DESC LIMIT 1",
                (canonical_id, channel, cutoff, STATE_REJECTED, STATE_EXPIRED)).fetchone()
        return dict(row) if row else None
    except Exception as e:
        logger.debug("[handoff] check_duplicate 失败: %s", e)
        return None
