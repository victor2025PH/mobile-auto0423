# -*- coding: utf-8 -*-
"""LeadDossier — 聚合视图 (read path)。

把散落的 leads_canonical / lead_identities / lead_journey / lead_handoffs
拼成一个完整的"卷宗", 人类 Dashboard 和 AI Agent 都能读。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from src.host.database import _connect

logger = logging.getLogger(__name__)


def get_dossier(canonical_id: str,
                 journey_limit: int = 50) -> Optional[Dict[str, Any]]:
    """返回指定 lead 的完整 dossier。

    结构:
        {
          "canonical": {...},           # leads_canonical 行
          "identities": [...],           # lead_identities 各平台
          "journey": [...],              # 最近 N 条 lead_journey (时间升序)
          "handoffs": [...],             # 相关 lead_handoffs
          "current_owner": str,          # 推导: 最近一条非 'system' 动作的 actor
          "last_action_at": str,
          "journey_summary": {           # 统计
              "total_events": int,
              "by_action": {action: count},
              "platforms": [list of platforms touched]
          }
        }

    * 若 canonical 被合并, 自动跟随 merged_into 返回 target 的 dossier
    * 不存在返回 None
    """
    if not canonical_id:
        return None
    # 跟随 merged_into
    chain = [canonical_id]
    current = canonical_id
    for _ in range(5):  # 防止循环引用, 最多 5 级
        try:
            with _connect() as conn:
                row = conn.execute(
                    "SELECT merged_into FROM leads_canonical WHERE canonical_id=?",
                    (current,)).fetchone()
            if not row:
                return None
            if row[0]:
                current = row[0]
                chain.append(current)
                continue
            break
        except Exception as e:
            logger.debug("[dossier] merged_into 查询失败: %s", e)
            return None
    effective_id = current

    # 主记录
    try:
        with _connect() as conn:
            conn.row_factory = __import__("sqlite3").Row
            canonical_row = conn.execute(
                "SELECT * FROM leads_canonical WHERE canonical_id=?",
                (effective_id,)).fetchone()
    except Exception:
        return None
    if not canonical_row:
        return None

    canonical = dict(canonical_row)
    try:
        canonical["metadata"] = json.loads(canonical.pop("metadata_json") or "{}")
    except Exception:
        canonical["metadata"] = {}

    # identities (跨平台身份, 含所有合并链上的 identity)
    try:
        with _connect() as conn:
            conn.row_factory = __import__("sqlite3").Row
            # 因为合并后 identities 的 canonical_id 已经改到 target, 直接查 target 即可
            rows = conn.execute(
                "SELECT * FROM lead_identities WHERE canonical_id=?"
                " ORDER BY discovered_at ASC", (effective_id,)).fetchall()
        identities = []
        for r in rows:
            d = dict(r)
            try:
                d["metadata"] = json.loads(d.pop("metadata_json") or "{}")
            except Exception:
                d["metadata"] = {}
            identities.append(d)
    except Exception:
        identities = []

    # journey (聚合合并链上所有 canonical_id 的 journey)
    try:
        placeholders = ",".join(["?"] * len(chain))
        with _connect() as conn:
            conn.row_factory = __import__("sqlite3").Row
            rows = conn.execute(
                f"SELECT * FROM lead_journey WHERE canonical_id IN ({placeholders})"
                f" ORDER BY at ASC, id ASC LIMIT ?",
                (*chain, journey_limit),
            ).fetchall()
        journey = []
        for r in rows:
            d = dict(r)
            try:
                d["data"] = json.loads(d.pop("data_json") or "{}")
            except Exception:
                d["data"] = {}
            journey.append(d)
    except Exception:
        journey = []

    # handoffs
    try:
        with _connect() as conn:
            conn.row_factory = __import__("sqlite3").Row
            rows = conn.execute(
                "SELECT * FROM lead_handoffs WHERE canonical_id=?"
                " ORDER BY created_at DESC", (effective_id,)).fetchall()
        handoffs = []
        for r in rows:
            d = dict(r)
            try:
                d["conversation_snapshot"] = json.loads(
                    d.pop("conversation_snapshot_json") or "[]")
            except Exception:
                d["conversation_snapshot"] = []
            handoffs.append(d)
    except Exception:
        handoffs = []

    # 聚合字段
    last_action_at = journey[-1]["at"] if journey else None
    current_owner = "unclaimed"
    for ev in reversed(journey):
        if ev["actor"] not in ("system", ""):
            current_owner = ev["actor"]
            break
    # 按 action 分组统计
    by_action: Dict[str, int] = {}
    platforms_seen = set()
    for ev in journey:
        a = ev.get("action") or ""
        by_action[a] = by_action.get(a, 0) + 1
        if ev.get("platform"):
            platforms_seen.add(ev["platform"])

    return {
        "canonical": canonical,
        "effective_canonical_id": effective_id,
        "canonical_chain": chain,
        "identities": identities,
        "journey": journey,
        "handoffs": handoffs,
        "current_owner": current_owner,
        "last_action_at": last_action_at,
        "journey_summary": {
            "total_events": len(journey),
            "by_action": by_action,
            "platforms": sorted(platforms_seen),
        },
    }


def search_leads(*,
                  name_like: str = "",
                  platform: str = "",
                  account_id_like: str = "",
                  limit: int = 50) -> List[Dict[str, Any]]:
    """搜索 leads (Dashboard 用)。

    name_like / account_id_like 都用 LIKE '%X%'。
    """
    sql = ("SELECT DISTINCT c.canonical_id, c.primary_name, c.primary_language,"
           " c.primary_persona_key, c.created_at"
           " FROM leads_canonical c LEFT JOIN lead_identities i"
           " ON c.canonical_id = i.canonical_id"
           " WHERE c.merged_into IS NULL")
    params: list = []
    if name_like:
        sql += " AND c.primary_name LIKE ?"
        params.append(f"%{name_like}%")
    if platform:
        sql += " AND i.platform=?"
        params.append(platform)
    if account_id_like:
        sql += " AND i.account_id LIKE ?"
        params.append(f"%{account_id_like}%")
    sql += " ORDER BY c.updated_at DESC LIMIT ?"
    params.append(int(limit))
    try:
        with _connect() as conn:
            conn.row_factory = __import__("sqlite3").Row
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.debug("[dossier] search_leads 失败: %s", e)
        return []
