# -*- coding: utf-8 -*-
"""Phase 11 (2026-04-25): /line-pool 管理 API.

提供:
  * GET  /line-pool                列表 (filter: status/region/persona_key/owner)
  * POST /line-pool                单条新增
  * PUT  /line-pool/{id}           更新 (status/region/persona/cap/notes/line_id)
  * DELETE /line-pool/{id}         删除
  * POST /line-pool/bulk-import    批量导入 (CSV text 或 JSON array)
  * POST /line-pool/allocate       轮循分配 (内部/dispatcher 调用)
  * GET  /line-pool/dispatch-log   最近分发审计
"""
from __future__ import annotations

import csv
import io
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException, Query, UploadFile, File

from src.host import line_pool as lp

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/line-pool", tags=["line-pool"])


# ─── 查 ─────────────────────────────────────────────────────────────────

@router.get("")
def api_list_line_pool(
        status: Optional[str] = Query(default=None),
        region: Optional[str] = Query(default=None),
        persona_key: Optional[str] = Query(default=None),
        owner_device_id: Optional[str] = Query(default=None),
        limit: int = Query(default=200, ge=1, le=2000)):
    rows = lp.list_accounts(status=status, region=region,
                              persona_key=persona_key,
                              owner_device_id=owner_device_id,
                              limit=limit)
    return {"count": len(rows), "results": rows}


@router.get("/dispatch-log")
def api_dispatch_log(limit: int = Query(default=100, ge=1, le=1000)):
    return {"results": lp.recent_dispatch_log(limit=limit)}


@router.get("/{account_id}")
def api_get_line_account(account_id: int):
    row = lp.get_by_id(account_id)
    if not row:
        raise HTTPException(404, "not found")
    return row


# ─── 增 ─────────────────────────────────────────────────────────────────

@router.post("")
def api_add_line_account(body: Dict[str, Any] = Body(...)):
    try:
        aid = lp.add(
            line_id=(body.get("line_id") or "").strip(),
            owner_device_id=body.get("owner_device_id", "") or "",
            persona_key=body.get("persona_key", "") or "",
            region=body.get("region", "") or "",
            status=body.get("status", "active") or "active",
            daily_cap=int(body.get("daily_cap", 20) or 20),
            notes=body.get("notes", "") or "",
        )
        return {"id": aid}
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/bulk-import")
def api_bulk_import_json(body: Dict[str, Any] = Body(...)):
    """JSON 体批量: {records: [{line_id, owner_device_id, ...}, ...]}.

    文件上传 (CSV/XLSX) 走另一个端点, 本端点接收前端 parse 后的 JSON.
    """
    records = body.get("records") or []
    if not isinstance(records, list):
        raise HTTPException(400, "records 必须是 list")
    return lp.add_many(records)


@router.post("/bulk-import-csv")
async def api_bulk_import_csv(file: UploadFile = File(...)):
    """直接上传 CSV 文件. 首行 header, 支持列: line_id (必填), owner_device_id,
    persona_key, region, status, daily_cap, notes.
    """
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(400, "只支持 .csv 文件 (XLSX 请先前端转 CSV 或 JSON)")
    try:
        raw = await file.read()
        text = raw.decode("utf-8-sig", errors="replace")
    except Exception as e:
        raise HTTPException(400, f"读文件失败: {e}")
    try:
        reader = csv.DictReader(io.StringIO(text))
        records: List[Dict[str, Any]] = []
        for r in reader:
            records.append({
                "line_id": (r.get("line_id") or "").strip(),
                "owner_device_id": (r.get("owner_device_id") or "").strip(),
                "persona_key": (r.get("persona_key") or "").strip(),
                "region": (r.get("region") or "").strip(),
                "status": (r.get("status") or "active").strip(),
                "daily_cap": int((r.get("daily_cap") or "20").strip() or 20),
                "notes": (r.get("notes") or "").strip(),
            })
    except Exception as e:
        raise HTTPException(400, f"CSV 解析失败: {e}")
    return lp.add_many(records)


# ─── 改/删 ─────────────────────────────────────────────────────────────

@router.put("/{account_id}")
def api_update_line_account(account_id: int, body: Dict[str, Any] = Body(...)):
    if not lp.get_by_id(account_id):
        raise HTTPException(404, "not found")
    try:
        ok = lp.update(account_id, **{
            k: body[k] for k in body
            if k in {"line_id", "owner_device_id", "persona_key",
                     "region", "status", "daily_cap", "notes"}
        })
        return {"ok": ok, "id": account_id}
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.delete("/{account_id}")
def api_delete_line_account(account_id: int):
    ok = lp.delete(account_id)
    if not ok:
        raise HTTPException(404, "not found")
    return {"ok": True, "id": account_id}


# ─── 轮循分配 ───────────────────────────────────────────────────────────

@router.post("/allocate")
def api_allocate(body: Dict[str, Any] = Body(default={})):
    """分配一个 LINE id (供 dispatcher / B 机 messenger 回复调用).

    Body:
      region?, persona_key?, owner_device_id?
      canonical_id?, peer_name?, source_device_id?, source_event_id?

    返回: {allocated: bool, account?: {id, line_id, ...}, reason?: str}
    """
    acc = lp.allocate(
        region=body.get("region") or None,
        persona_key=body.get("persona_key") or None,
        owner_device_id=body.get("owner_device_id") or None,
        canonical_id=body.get("canonical_id", "") or "",
        peer_name=body.get("peer_name", "") or "",
        source_device_id=body.get("source_device_id", "") or "",
        source_event_id=body.get("source_event_id", "") or "",
    )
    if acc is None:
        return {"allocated": False,
                "reason": "no_matching_account_or_all_capped"}
    return {"allocated": True, "account": acc}


@router.post("/dispatch-log/{account_id}/outcome")
def api_mark_outcome(account_id: int, body: Dict[str, Any] = Body(...)):
    """B 机/dispatcher 完成发送后回写 status (sent/failed/skipped)."""
    status = (body.get("status") or "").strip()
    note = (body.get("note") or "").strip()
    try:
        ok = lp.mark_dispatch_outcome(account_id, status=status, note=note)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": ok}
