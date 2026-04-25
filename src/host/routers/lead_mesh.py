# -*- coding: utf-8 -*-
"""Lead Mesh HTTP API (Phase 5)。

暴露:
  * /lead-mesh/leads/{cid}         GET   — 拉 dossier
  * /lead-mesh/leads/search        GET   — 按名字/平台搜 lead
  * /lead-mesh/leads/{cid}/journey GET   — 单独拿事件流 (便于时间轴)
  * /lead-mesh/leads/{cid}/merge-candidates GET — 合并候选
  * /lead-mesh/leads/merge         POST  — 手动合并
  * /lead-mesh/leads/merges/{id}/revert POST — 撤销合并

  * /lead-mesh/handoffs            GET   — 队列 (带状态/接收方过滤)
  * /lead-mesh/handoffs            POST  — 创建
  * /lead-mesh/handoffs/{id}       GET   — 单详情
  * /lead-mesh/handoffs/{id}/acknowledge POST
  * /lead-mesh/handoffs/{id}/complete    POST
  * /lead-mesh/handoffs/{id}/reject      POST
  * /lead-mesh/handoffs/check-duplicate  GET (query: canonical_id, channel)

  * /lead-mesh/agents/messages     POST  — send_message (HTTP 通道)
  * /lead-mesh/agents/messages     GET   — 拉自己的队列
  * /lead-mesh/agents/messages/{id}/deliver POST
  * /lead-mesh/agents/messages/{id}/ack     POST
  * /lead-mesh/agents/query-sync   POST  — 同步 query-reply (阻塞)

  * /lead-mesh/webhooks/flush      POST  — 触发 webhook dispatcher
  * /lead-mesh/webhooks/dead-letters GET — 死信查询
  * /lead-mesh/webhooks/{id}/retry POST — 重置死信

所有端点都可以被人 (curl) 或 AI Agent 直接调, 统一 JSON 接口。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException, Query

from src.host import lead_mesh as lm

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/lead-mesh", tags=["lead-mesh"])


# ─── Leads / Dossier ─────────────────────────────────────────────────

# ⚠ 路由顺序: 静态路径(/leads/search, /leads/resolve, /leads/merge*) 必须
# 声明在 /leads/{canonical_id} 之前, 否则静态片段会被当成 path param 吃掉。
@router.get("/leads/search")
def api_search_leads(name_like: str = "",
                       platform: str = "",
                       account_id_like: str = "",
                       limit: int = Query(default=50, ge=1, le=500)):
    return {"results": lm.search_leads(
        name_like=name_like, platform=platform,
        account_id_like=account_id_like, limit=limit)}


@router.post("/leads/resolve")
def api_resolve_identity(body: Dict[str, Any] = Body(...)):
    """按 (platform, account_id) 拿 canonical_id (不存在则创建)。

    请求体: {platform, account_id, display_name?, language?,
             discovered_via?, discovered_by_device?, extra_metadata?}
    """
    platform = (body.get("platform") or "").strip().lower()
    account_id = (body.get("account_id") or "").strip()
    if not platform or not account_id:
        raise HTTPException(400, "platform / account_id 必填")
    try:
        cid = lm.resolve_identity(
            platform=platform, account_id=account_id,
            display_name=body.get("display_name") or "",
            language=body.get("language") or "",
            persona_key=body.get("persona_key") or "",
            extra_metadata=body.get("extra_metadata") or {},
            discovered_via=body.get("discovered_via") or "",
            discovered_by_device=body.get("discovered_by_device") or "",
            auto_merge=bool(body.get("auto_merge", True)),
        )
        return {"canonical_id": cid}
    except Exception as e:
        raise HTTPException(500, f"resolve 失败: {e}")


@router.post("/leads/merge")
def api_merge_manually(body: Dict[str, Any] = Body(...)):
    """手动合并 {source_canonical_id, target_canonical_id, merged_by, reason}。"""
    from src.host.lead_mesh.canonical import merge_manually
    src = (body.get("source_canonical_id") or "").strip()
    tgt = (body.get("target_canonical_id") or "").strip()
    if not src or not tgt:
        raise HTTPException(400, "source/target 必填")
    ok = merge_manually(src, tgt,
                         merged_by=body.get("merged_by") or "human",
                         reason=body.get("reason") or "")
    return {"ok": ok, "source": src, "target": tgt}


@router.post("/leads/merges/{merge_id}/revert")
def api_revert_merge(merge_id: int, body: Dict[str, Any] = Body(default={})):
    from src.host.lead_mesh.canonical import revert_merge
    ok = revert_merge(merge_id,
                       reverted_by=body.get("reverted_by") or "human",
                       reason=body.get("reason") or "")
    return {"ok": ok, "merge_id": merge_id}


@router.get("/leads/l2-verified")
def api_list_l2_verified_leads(
        age_band: Optional[str] = Query(default=None,
                                         description="例如 '40s' / '50s'"),
        gender: Optional[str] = Query(default=None,
                                       description="'female' / 'male'"),
        is_japanese: Optional[bool] = Query(default=None),
        persona_key: Optional[str] = Query(default=None,
                                            description="L2 匹配用的 persona"),
        platform: Optional[str] = Query(default=None,
                                         description="'facebook' / ..."),
        min_score: float = Query(default=0, ge=0, le=100),
        limit: int = Query(default=50, ge=1, le=1000),
        include_tags: Optional[List[str]] = Query(
            default=None,
            description="tags 必须全部包含, 例 ['line_referred']"),
        exclude_tags: Optional[List[str]] = Query(
            default=None,
            description="含任一此 tag 的 lead 排除, 例 ['referral_dead']")):
    """Phase 10.3 + 12.2: 查 L2 VLM 验证过的精准用户.

    只返回 tags 里带 ``l2_verified`` 的 lead, 按 l2_score 降序. 所有过滤 AND.
    Phase 12.2 新增 include/exclude tags: 例如查"已引流的":
      /leads/l2-verified?include_tags=line_referred
    查"L2 通过但 referral 已死不再骚扰的":
      /leads/l2-verified?include_tags=referral_dead
    """
    from src.host.lead_mesh.canonical import list_l2_verified_leads
    rows = list_l2_verified_leads(
        age_band=age_band, gender=gender,
        is_japanese=is_japanese, persona_key=persona_key,
        platform=platform, min_score=min_score, limit=limit,
        include_tags=include_tags, exclude_tags=exclude_tags,
    )
    return {"count": len(rows), "results": rows}


@router.post("/leads/{canonical_id}/revive-referral")
def api_revive_referral(canonical_id: str):
    """Phase 12.3: 给 peer 第二次机会 — 去 referral_dead tag + 清 fail counters.

    运营在 UI 点"恢复"按钮触发. 或 scheduled task line_pool_recycle_dead_peers
    按 dead_at 年龄自动调.
    """
    from src.host.lead_mesh import revive_referral
    ok = revive_referral(canonical_id)
    return {"ok": ok, "canonical_id": canonical_id}


@router.post("/leads/{canonical_id}/untag")
def api_untag(canonical_id: str, body: Dict[str, Any] = Body(...)):
    """Phase 12.3 通用 untag: body {tags: [...]}. 返 {ok: bool}."""
    from src.host.lead_mesh import remove_canonical_tags
    tags = body.get("tags") or []
    if not isinstance(tags, list):
        raise HTTPException(400, "tags 必须是 list")
    ok = remove_canonical_tags(canonical_id, tags)
    return {"ok": ok, "canonical_id": canonical_id, "removed": tags}


# 动态路径(path param) 必须在所有同前缀静态路径之后
@router.get("/leads/{canonical_id}")
def api_get_dossier(canonical_id: str, journey_limit: int = 100):
    d = lm.get_dossier(canonical_id, journey_limit=journey_limit)
    if not d:
        raise HTTPException(404, "lead not found")
    return d


@router.get("/leads/{canonical_id}/journey")
def api_get_journey(canonical_id: str,
                     limit: int = Query(default=100, ge=1, le=1000),
                     action_prefix: str = "",
                     since_iso: str = ""):
    return {"journey": lm.get_journey(canonical_id, limit=limit,
                                        action_prefix=action_prefix,
                                        since_iso=since_iso)}


@router.get("/leads/{canonical_id}/merge-candidates")
def api_merge_candidates(canonical_id: str,
                          min_confidence: float = 0.70):
    return {"candidates": lm.auto_merge_candidates(canonical_id,
                                                      min_confidence=min_confidence)}


# ─── Handoffs ────────────────────────────────────────────────────────

@router.get("/handoffs/check-duplicate")
def api_check_duplicate(canonical_id: str, channel: str, since_days: int = 30):
    """B 发引流前的去重检查端点。

    ⚠ 注意: 此路由必须在 ``/handoffs/{handoff_id}`` 之前注册, 否则
    ``check-duplicate`` 会被当成 handoff_id 匹配走。
    """
    from src.host.lead_mesh.handoff import check_duplicate_handoff
    dup = check_duplicate_handoff(canonical_id, channel, since_days=since_days)
    return {"is_duplicate": dup is not None, "existing": dup}


@router.get("/handoffs")
def api_list_handoffs(state: str = "",
                       receiver_account_key: str = "",
                       canonical_id: str = "",
                       channel: str = "",
                       limit: int = Query(default=100, ge=1, le=500)):
    return {"handoffs": lm.list_handoffs(
        state=state, receiver_account_key=receiver_account_key,
        canonical_id=canonical_id, channel=channel, limit=limit)}


@router.post("/handoffs")
def api_create_handoff(body: Dict[str, Any] = Body(...)):
    cid = (body.get("canonical_id") or "").strip()
    src_agent = (body.get("source_agent") or "").strip()
    channel = (body.get("channel") or "").strip()
    if not cid or not src_agent or not channel:
        raise HTTPException(400, "canonical_id / source_agent / channel 必填")
    hid = lm.create_handoff(
        canonical_id=cid, source_agent=src_agent, channel=channel,
        source_device=body.get("source_device") or "",
        target_agent=body.get("target_agent") or "",
        receiver_account_key=body.get("receiver_account_key") or "",
        conversation_snapshot=body.get("conversation_snapshot") or [],
        snippet_sent=body.get("snippet_sent") or "",
        enqueue_webhook=bool(body.get("enqueue_webhook", True)),
    )
    if not hid:
        raise HTTPException(500, "create handoff 失败")
    return {"handoff_id": hid}


@router.get("/handoffs/{handoff_id}")
def api_get_handoff(handoff_id: str):
    h = lm.get_handoff(handoff_id)
    if not h:
        raise HTTPException(404, "handoff not found")
    return h


@router.post("/handoffs/{handoff_id}/acknowledge")
def api_ack_handoff(handoff_id: str, body: Dict[str, Any] = Body(default={})):
    by = (body.get("by") or "").strip() or "human"
    ok = lm.acknowledge_handoff(handoff_id, by=by, notes=body.get("notes") or "")
    if not ok:
        raise HTTPException(409, "状态转移失败(可能已非 pending)")
    return {"ok": True, "new_state": "acknowledged"}


@router.post("/handoffs/{handoff_id}/complete")
def api_complete_handoff(handoff_id: str, body: Dict[str, Any] = Body(default={})):
    by = (body.get("by") or "").strip() or "human"
    ok = lm.complete_handoff(handoff_id, by=by, notes=body.get("notes") or "")
    if not ok:
        raise HTTPException(409, "状态转移失败")
    return {"ok": True, "new_state": "completed"}


@router.post("/handoffs/{handoff_id}/reject")
def api_reject_handoff(handoff_id: str, body: Dict[str, Any] = Body(default={})):
    by = (body.get("by") or "").strip() or "human"
    ok = lm.reject_handoff(handoff_id, by=by, notes=body.get("notes") or "")
    if not ok:
        raise HTTPException(409, "状态转移失败")
    return {"ok": True, "new_state": "rejected"}


# ─── Agent Mesh ──────────────────────────────────────────────────────

@router.post("/agents/messages")
def api_send_message(body: Dict[str, Any] = Body(...)):
    """SQLite + HTTP 双通道的 HTTP 入口。"""
    frm = (body.get("from_agent") or "").strip()
    to = (body.get("to_agent") or "").strip()
    if not frm or not to:
        raise HTTPException(400, "from_agent / to_agent 必填")
    cid = lm.send_message(
        from_agent=frm, to_agent=to,
        message_type=body.get("message_type") or "notification",
        canonical_id=body.get("canonical_id") or "",
        payload=body.get("payload") or {},
        correlation_id=body.get("correlation_id") or "",
    )
    return {"correlation_id": cid}


@router.get("/agents/messages")
def api_poll_messages(to_agent: str,
                        message_type: str = "",
                        status: str = "pending",
                        limit: int = Query(default=50, ge=1, le=200)):
    msgs = lm.poll_messages(to_agent, message_type=message_type,
                              status=status, limit=limit)
    return {"messages": msgs, "count": len(msgs)}


@router.post("/agents/messages/{message_id}/deliver")
def api_mark_delivered(message_id: int):
    ok = lm.mark_delivered(message_id)
    return {"ok": ok}


@router.post("/agents/messages/{message_id}/ack")
def api_mark_ack(message_id: int, body: Dict[str, Any] = Body(default={})):
    ok = lm.mark_acknowledged(message_id, error=body.get("error") or "")
    return {"ok": ok}


@router.post("/agents/query-sync")
def api_query_sync(body: Dict[str, Any] = Body(...)):
    """HTTP 同步 query-reply。阻塞等 reply 或超时。

    ⚠ 慎用: 阻塞 FastAPI worker thread. 对于不需实时的场景仍推荐异步 poll 模式。
    """
    frm = (body.get("from_agent") or "").strip()
    to = (body.get("to_agent") or "").strip()
    if not frm or not to:
        raise HTTPException(400, "from_agent / to_agent 必填")
    reply = lm.query_sync(
        from_agent=frm, to_agent=to,
        payload=body.get("payload") or {},
        canonical_id=body.get("canonical_id") or "",
        timeout_sec=float(body.get("timeout_sec", 30)),
        poll_interval=float(body.get("poll_interval", 1.0)),
    )
    return {"reply": reply, "timed_out": reply is None}


# ─── Receivers (接收方账号管理, Phase 6.B) ──────────────────────────

@router.get("/receivers")
def api_list_receivers(channel: str = "",
                         enabled_only: bool = False,
                         with_load: bool = True):
    """列所有接收方, with_load=True 时附每个的今日负载。"""
    from src.host.lead_mesh.receivers import (list_receivers, receiver_load,
                                                 all_loads)
    items = list_receivers(channel=channel or None,
                             enabled_only=enabled_only)
    if with_load:
        # 按 key 合并 load 信息
        loads = {l["key"]: l for l in all_loads()}
        for it in items:
            ld = loads.get(it["key"], {})
            for k in ("current", "cap", "remaining",
                       "percent_used", "at_cap"):
                if k in ld:
                    it[k] = ld[k]
            it["account_id_masked"] = ld.get("account_id_masked", "")
    return {"receivers": items, "count": len(items)}


@router.get("/receivers/{key}")
def api_get_receiver(key: str):
    from src.host.lead_mesh.receivers import get_receiver, receiver_load
    r = get_receiver(key)
    if not r:
        raise HTTPException(404, "receiver not found")
    r.update({"load": receiver_load(key)})
    return r


@router.post("/receivers/{key}")
def api_upsert_receiver(key: str, body: Dict[str, Any] = Body(...)):
    """新建或更新一个 receiver。"""
    from src.host.lead_mesh.receivers import upsert_receiver
    if not body.get("channel") and not body.get("account_id"):
        # 允许只改部分字段(如只 toggle enabled), 但至少得有 1 个字段
        if not any(k in body for k in ("enabled", "daily_cap",
                                          "backup_key", "persona_filter",
                                          "display_name", "tags",
                                          "webhook_url")):
            raise HTTPException(400, "body 至少包含一个字段")
    try:
        r = upsert_receiver(key, body)
        return {"ok": True, "receiver": r}
    except Exception as e:
        raise HTTPException(500, f"upsert 失败: {e}")


@router.delete("/receivers/{key}")
def api_delete_receiver(key: str):
    from src.host.lead_mesh.receivers import delete_receiver
    ok = delete_receiver(key)
    if not ok:
        raise HTTPException(404, "receiver not found")
    return {"ok": True, "deleted": key}


@router.get("/receivers-pick")
def api_pick_receiver(channel: str,
                       persona_key: str = "",
                       preferred_key: str = ""):
    """按 channel + persona 模拟 pick_receiver(不实际占位,只返回谁会被选)。

    给 Dashboard 看"当前引流到某渠道会路由到哪个账号"用。
    """
    from src.host.lead_mesh.receivers import pick_receiver
    picked = pick_receiver(channel, persona_key=persona_key or None,
                             preferred_key=preferred_key or None)
    return {"channel": channel, "persona_key": persona_key,
            "picked": picked,
            "all_at_cap": picked is None}


# ─── Webhooks ─────────────────────────────────────────────────────────

@router.post("/webhooks/flush")
def api_flush_webhooks(max_batch: int = Query(default=50, ge=1, le=500)):
    """手动触发 webhook dispatcher (也可由定时任务周期调)。"""
    stats = lm.flush_pending_webhooks(max_batch=max_batch)
    return {"ok": True, "stats": stats}


@router.get("/webhooks/dead-letters")
def api_list_dead_letters(limit: int = Query(default=100, ge=1, le=500)):
    from src.host.lead_mesh.webhook_dispatcher import list_dead_letters
    return {"dead_letters": list_dead_letters(limit)}


@router.post("/webhooks/{dispatch_id}/retry")
def api_retry_dead(dispatch_id: int):
    from src.host.lead_mesh.webhook_dispatcher import retry_dead_letter
    ok = retry_dead_letter(dispatch_id)
    return {"ok": ok}


# ── Phase 8h: Blocklist (运营一键 skip 骚扰保护) ────────────────────
@router.post("/peers/{canonical_id}/blocklist")
def api_add_blocklist(canonical_id: str, body: Dict[str, Any] = Body(default={})):
    """加入 blocklist. body 可传 {reason, note, created_by}."""
    from src.host.lead_mesh import add_to_blocklist
    created = add_to_blocklist(
        canonical_id,
        reason=str(body.get("reason") or ""),
        note=str(body.get("note") or ""),
        created_by=str(body.get("created_by") or "operator"))
    return {"ok": True, "canonical_id": canonical_id,
             "created": created, "was_already_blocklisted": not created}


@router.delete("/peers/{canonical_id}/blocklist")
def api_remove_blocklist(canonical_id: str):
    from src.host.lead_mesh import remove_from_blocklist
    removed = remove_from_blocklist(canonical_id)
    return {"ok": True, "canonical_id": canonical_id, "removed": removed}


@router.get("/blocklist")
def api_list_blocklist(limit: int = Query(default=50, ge=1, le=200)):
    from src.host.lead_mesh import list_blocklist, count_blocklist
    items = list_blocklist(limit=limit)
    return {"total": count_blocklist(), "count": len(items), "items": items}


# ── Phase 8b: 漏斗报告 (给 Command Center Dashboard 卡片用) ─────────
@router.get("/funnel")
def api_funnel_report(days: int = Query(default=7, ge=1, le=90),
                       actor: str = Query(default=""),
                       date: str = Query(default="")):
    """A 端 add_friend → greeting 漏斗统计. 从 lead_journey 聚合.

    Args:
        days: 时间窗口 (1-90 天; date 未提供时生效)
        actor: 可选过滤 agent_a / agent_b; 空 = 不限
        date: Phase 8g 下钻参数, YYYY-MM-DD 单日过滤 (优先于 days).
              非法格式降级到 days.
    """
    from src.host.lead_mesh.funnel_report import compute_funnel
    stats = compute_funnel(days=days, actor=actor or None,
                             date=date or None)
    return stats.to_dict()


# ── Phase 8e: 近 N 天按日时序 (Dashboard sparkline 用) ──────────────
@router.get("/funnel/timeseries")
def api_funnel_timeseries(days: int = Query(default=7, ge=1, le=90),
                            actor: str = Query(default="")):
    """近 N 天按日分桶的漏斗时序. 缺失日填 0 避免 sparkline 断线."""
    from src.host.lead_mesh.funnel_report import compute_funnel_timeseries
    series = compute_funnel_timeseries(days=days, actor=actor or None)
    return {"days": days, "actor": actor or "", "series": series}


# ── Phase 8d: 点击某 blocked reason 看具体 peer 列表 ────────────────
@router.get("/funnel/blocked-peers")
def api_blocked_peers(reason: str = Query(...),
                       days: int = Query(default=7, ge=1, le=90),
                       limit: int = Query(default=50, ge=1, le=200),
                       date: str = Query(default="")):
    """被某 greeting_blocked.reason 挡住的 peer 列表, 按最近时间倒序.

    供 Dashboard 点击 top_blocked_reason 子 modal 展示, 帮运营定位个案.
    date 参数 (Phase 8g): YYYY-MM-DD 单日过滤, 优先于 days.
    """
    from src.host.lead_mesh.funnel_report import list_blocked_peers
    peers = list_blocked_peers(reason=reason, days=days, limit=limit,
                                 date=date or None)
    return {"reason": reason, "days": days, "date": date or "",
             "count": len(peers), "peers": peers}
