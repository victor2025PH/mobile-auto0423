# -*- coding: utf-8 -*-
"""L1 (fb_store SQLite) → L2 (中央 PG) 双写门面.

接入方式
--------
greeting bot 在落 fb_store 的同时调本模块的 ``sync_*`` 函数, 把同一个事件
ALSO push 到中央客户画像. 全部 fire_and_forget + 全部 try/except 静默,
**任何失败都不影响调用方主流程** (跟现存 fb_store 写入失败的容错语义一致).

canonical_id 约定 (v1)
----------------------
greeting bot 阶段尚无 fb_uid 抓取, 用现存 ``peer_name`` 字符串. 为避免重名
碰撞 + 与未来 fb_uid 命名空间冲突:

    canonical_source = "facebook_name"
    canonical_id     = f"{device_id}::{peer_name}"

跨 device 的同名客户会成 N 行 (false negative), 但 greeting bot 实际业务里
"同一客户被多 device 加" 极少见. v2 引入 fb_uid 抓取后会:

1. 新 source = "facebook" (用 fb_uid)
2. 引入 customer_aliases 表 merge "facebook_name" → "facebook"

customer_id 用 push_client.compute_customer_id 算 UUIDv5, worker 离线时也
能不阻塞地拿到 ID (主控离线时全走本地 retry queue 兜底).

事件类型命名空间 (L2 customer_events.event_type)
-----------------------------------------------
- ``friend_request_sent`` / ``friend_request_risk``
- ``greeting_sent`` / ``greeting_fallback``

(对应 fb_store 的 CONTACT_EVT_ADD_FRIEND_SENT / *_RISK / *_GREETING_SENT /
GREETING_FALLBACK; 这里去掉 add_friend_ 前缀简化, L2 是 customer 视角)
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

CANONICAL_SOURCE = "facebook_name"
CHANNEL_FACEBOOK = "facebook"


def _build_canonical_id(device_id: str, peer_name: str) -> str:
    return f"{device_id}::{peer_name}"


def _safe_worker_id() -> str:
    try:
        from src.host.cluster_lock_client import get_worker_id
        return get_worker_id()
    except Exception:  # noqa: BLE001
        return ""


def _ensure_customer(
    device_id: str,
    peer_name: str,
    *,
    ai_profile: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """upsert_customer + 返回 customer_id (deterministic UUIDv5)."""
    try:
        from src.host.central_push_client import upsert_customer
        return upsert_customer(
            canonical_source=CANONICAL_SOURCE,
            canonical_id=_build_canonical_id(device_id, peer_name),
            primary_name=peer_name,
            worker_id=_safe_worker_id() or None,
            device_id=device_id,
            ai_profile=ai_profile,
            fire_and_forget=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("[customer_sync] upsert failed: %s", exc)
        return None


def sync_friend_request_sent(
    device_id: str,
    peer_name: str,
    *,
    status: str = "sent",
    persona_key: Optional[str] = None,
    preset_key: Optional[str] = None,
    source: Optional[str] = None,
    note: Optional[str] = None,
) -> Optional[str]:
    """好友请求落事件: status='sent' → friend_request_sent, 'risk' → friend_request_risk.

    返回 customer_id (调试/链路追踪用). None 表示 sync bridge 内部异常 (业务无感).
    """
    if not device_id or not peer_name:
        return None
    cid = _ensure_customer(
        device_id, peer_name,
        ai_profile={"persona_key": persona_key} if persona_key else None,
    )
    if not cid:
        return None
    event_type = "friend_request_sent" if status == "sent" else "friend_request_risk"
    try:
        from src.host.central_push_client import record_event
        record_event(
            customer_id=cid,
            event_type=event_type,
            worker_id=_safe_worker_id(),
            device_id=device_id,
            meta={
                "persona_key": persona_key or "",
                "preset_key": preset_key or "",
                "source": source or "",
                "has_note": bool(note),
            },
            fire_and_forget=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("[customer_sync] friend_request event failed: %s", exc)
    return cid


def sync_greeting_sent(
    device_id: str,
    peer_name: str,
    *,
    greeting: str,
    template_id: Optional[str] = None,
    preset_key: Optional[str] = None,
    persona_key: Optional[str] = None,
    phase: Optional[str] = None,
    fallback: bool = False,
    content_lang: Optional[str] = None,
) -> Optional[str]:
    """打招呼成功落事件 + 聊天 (channel=facebook, direction=outgoing).

    fallback=True 时事件类型用 greeting_fallback (messenger 降级路径).
    返回 customer_id.
    """
    if not device_id or not peer_name or not greeting:
        return None
    cid = _ensure_customer(
        device_id, peer_name,
        ai_profile={"persona_key": persona_key} if persona_key else None,
    )
    if not cid:
        return None

    event_type = "greeting_fallback" if fallback else "greeting_sent"
    worker_id = _safe_worker_id()
    try:
        from src.host.central_push_client import record_event, record_chat
        record_event(
            customer_id=cid,
            event_type=event_type,
            worker_id=worker_id,
            device_id=device_id,
            meta={
                "template_id": template_id or "",
                "preset_key": preset_key or "",
                "persona_key": persona_key or "",
                "phase": phase or "",
                "msg_len": len(greeting),
            },
            fire_and_forget=True,
        )
        record_chat(
            customer_id=cid,
            channel=CHANNEL_FACEBOOK,
            direction="outgoing",
            content=greeting,
            content_lang=content_lang,
            ai_generated=False,
            template_id=template_id,
            worker_id=worker_id or None,
            device_id=device_id,
            meta={
                "preset_key": preset_key or "",
                "persona_key": persona_key or "",
                "phase": phase or "",
            },
            fire_and_forget=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("[customer_sync] greeting push failed: %s", exc)
    return cid
