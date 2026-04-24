# -*- coding: utf-8 -*-
"""Phase 11 (2026-04-25): LINE account pool CRUD + 轮循分配.

设计思路:
  - 表 line_accounts 存池子 (line_id 唯一, owner_device_id/persona_key/region 分群).
  - allocate() 用 ``last_used_at ASC`` 轮循 + 24h cap check, 取到后立即
    UPDATE last_used_at + times_used, 同时写 line_dispatch_log 审计.
  - 整个 allocate 在一个 transaction 内, 避免并发撞同一个账号超 cap.
  - CSV/XLSX 批量导入由上层 router 解析后调 add_many().
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from src.host.database import _connect

logger = logging.getLogger(__name__)


VALID_STATUSES = frozenset({"active", "cooldown", "banned", "disabled"})


def _now_iso() -> str:
    import datetime as _dt
    return _dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _row_to_dict(row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "line_id": row["line_id"],
        "owner_device_id": row["owner_device_id"] or "",
        "persona_key": row["persona_key"] or "",
        "region": row["region"] or "",
        "status": row["status"] or "active",
        "last_used_at": row["last_used_at"] or "",
        "times_used": int(row["times_used"] or 0),
        "daily_cap": int(row["daily_cap"] or 20),
        "notes": row["notes"] or "",
        "created_at": row["created_at"] or "",
        "updated_at": row["updated_at"] or "",
    }


# ─── 增 ─────────────────────────────────────────────────────────────────

def add(line_id: str, *,
        owner_device_id: str = "",
        persona_key: str = "",
        region: str = "",
        status: str = "active",
        daily_cap: int = 20,
        notes: str = "") -> int:
    """新增一条 LINE 账号. line_id 重复 → 抛 ValueError."""
    line_id = (line_id or "").strip()
    if not line_id:
        raise ValueError("line_id 必填")
    if status not in VALID_STATUSES:
        raise ValueError(f"status 必须在 {sorted(VALID_STATUSES)}")
    try:
        with _connect() as conn:
            cur = conn.execute(
                "INSERT INTO line_accounts (line_id, owner_device_id, persona_key,"
                " region, status, daily_cap, notes) VALUES (?,?,?,?,?,?,?)",
                (line_id, owner_device_id.strip(), persona_key.strip(),
                 region.strip(), status, int(daily_cap), notes.strip()),
            )
            return int(cur.lastrowid)
    except Exception as e:
        msg = str(e).lower()
        if "unique" in msg or "constraint" in msg:
            raise ValueError(f"line_id 已存在: {line_id}") from e
        raise


def add_many(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """批量导入. 返回 {inserted, duplicate, invalid, errors: [(line_id, reason)]}.

    单条失败不中断, 全部跑完. 空 line_id / status 非法 → invalid.
    """
    inserted = 0
    duplicate = 0
    invalid = 0
    errors: List[Dict[str, str]] = []
    for rec in records or []:
        line_id = (rec.get("line_id") or "").strip()
        if not line_id:
            invalid += 1
            errors.append({"line_id": "", "reason": "empty line_id"})
            continue
        try:
            add(line_id,
                owner_device_id=rec.get("owner_device_id", "") or "",
                persona_key=rec.get("persona_key", "") or "",
                region=rec.get("region", "") or "",
                status=rec.get("status", "active") or "active",
                daily_cap=int(rec.get("daily_cap", 20) or 20),
                notes=rec.get("notes", "") or "")
            inserted += 1
        except ValueError as e:
            if "已存在" in str(e):
                duplicate += 1
            else:
                invalid += 1
            errors.append({"line_id": line_id, "reason": str(e)})
    return {"inserted": inserted, "duplicate": duplicate,
            "invalid": invalid, "total": len(records or []),
            "errors": errors[:50]}  # cap error list 避免 response 过大


# ─── 查 ─────────────────────────────────────────────────────────────────

def list_accounts(*,
                  status: Optional[str] = None,
                  region: Optional[str] = None,
                  persona_key: Optional[str] = None,
                  owner_device_id: Optional[str] = None,
                  limit: int = 200,
                  includes_24h_stats: bool = False
                  ) -> List[Dict[str, Any]]:
    where = []
    args: List[Any] = []
    if status:
        where.append("status = ?")
        args.append(status)
    if region:
        where.append("region = ?")
        args.append(region)
    if persona_key:
        where.append("persona_key = ?")
        args.append(persona_key)
    if owner_device_id:
        where.append("owner_device_id = ?")
        args.append(owner_device_id)
    sql = "SELECT * FROM line_accounts"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, min(int(limit or 200), 2000)))
    with _connect() as conn:
        out = [_row_to_dict(r) for r in conn.execute(sql, args).fetchall()]
        # Phase 11.1: 可选 24h 使用次数 (供 UI 显示真实 cap 余量)
        if includes_24h_stats and out:
            ids_tuple = tuple(a["id"] for a in out)
            placeholders = ",".join(["?"] * len(ids_tuple))
            counts = {}
            for r in conn.execute(
                f"SELECT line_account_id, COUNT(*) AS n FROM line_dispatch_log"
                f" WHERE line_account_id IN ({placeholders})"
                f" AND status != 'skipped'"
                f" AND created_at >= datetime('now', '-24 hours')"
                f" GROUP BY line_account_id",
                ids_tuple,
            ).fetchall():
                counts[r["line_account_id"]] = int(r["n"])
            for a in out:
                a["used_24h"] = counts.get(a["id"], 0)
        return out


def get_by_id(account_id: int) -> Optional[Dict[str, Any]]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM line_accounts WHERE id=?", (account_id,),
        ).fetchone()
    return _row_to_dict(row) if row else None


# ─── 改 ─────────────────────────────────────────────────────────────────

def update(account_id: int, **fields) -> bool:
    allowed = {"owner_device_id", "persona_key", "region", "status",
                "daily_cap", "notes", "line_id"}
    sets = []
    args: List[Any] = []
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k == "status" and v not in VALID_STATUSES:
            raise ValueError(f"status 非法: {v}")
        sets.append(f"{k} = ?")
        args.append(v)
    if not sets:
        return False
    sets.append("updated_at = datetime('now')")
    args.append(account_id)
    try:
        with _connect() as conn:
            cur = conn.execute(
                f"UPDATE line_accounts SET {', '.join(sets)} WHERE id=?",
                args,
            )
            return cur.rowcount > 0
    except Exception as e:
        msg = str(e).lower()
        if "unique" in msg or "constraint" in msg:
            raise ValueError("line_id 已存在") from e
        raise


def delete(account_id: int) -> bool:
    with _connect() as conn:
        cur = conn.execute("DELETE FROM line_accounts WHERE id=?",
                            (account_id,))
        return cur.rowcount > 0


# ─── 轮循分配 ───────────────────────────────────────────────────────────

def _write_skipped_log(conn, *, reason: str, canonical_id: str = "",
                         peer_name: str = "", source_device_id: str = "",
                         source_event_id: str = "",
                         filters: Optional[Dict[str, Any]] = None) -> None:
    """Phase 12.1: allocate 失败时写一条 skipped log 供 UI 诊断.

    line_account_id=0 表示"无账号被选", line_id='' 清晰标记这是 allocation-miss,
    不是发送失败. note 里带 reason + filter 组合帮运营判断该加账号还是调 cap.
    """
    try:
        note_dict = {"reason": reason}
        if filters:
            note_dict["filters"] = {k: v for k, v in filters.items() if v}
        import json as _json
        note = _json.dumps(note_dict, ensure_ascii=False)[:500]
    except Exception:
        note = reason
    try:
        conn.execute(
            "INSERT INTO line_dispatch_log (line_account_id, line_id,"
            " canonical_id, peer_name, source_device_id, source_event_id,"
            " status, note) VALUES (0, '', ?, ?, ?, ?, 'skipped', ?)",
            (canonical_id, peer_name, source_device_id, source_event_id, note),
        )
    except Exception as e:
        logger.debug("[line_pool] write skipped log 失败: %s", e)


def _count_recent_usage(conn, line_account_id: int, hours: int = 24) -> int:
    """近 N 小时 line_dispatch_log 记录数 (status != skipped)."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM line_dispatch_log"
        " WHERE line_account_id = ? AND status != 'skipped'"
        " AND created_at >= datetime('now', ?)",
        (line_account_id, f"-{int(hours)} hours"),
    ).fetchone()
    return int(row["n"] if row else 0)


def allocate(*,
             region: Optional[str] = None,
             persona_key: Optional[str] = None,
             owner_device_id: Optional[str] = None,
             canonical_id: str = "",
             peer_name: str = "",
             source_device_id: str = "",
             source_event_id: str = "") -> Optional[Dict[str, Any]]:
    """按轮循 (last_used_at ASC) + daily_cap 分配一个 LINE 账号.

    过滤顺序:
      1. status = 'active'
      2. region / persona_key / owner_device_id 匹配 (参数给了才过滤)
      3. 最近 24h 使用次数 < daily_cap
      4. 按 last_used_at ASC (空串视为最老) 取第 1 个

    分配成功:
      - UPDATE line_accounts.last_used_at = now, times_used += 1
      - INSERT line_dispatch_log(status='planned') 审计
      - 返回 dict {id, line_id, ...}
    分配失败:
      - 没匹配账号 / 全部超 cap → 返 None (调用方决定 retry / fallback)
    """
    where = ["status = 'active'"]
    args: List[Any] = []
    if region:
        where.append("region = ?")
        args.append(region)
    if persona_key:
        where.append("persona_key = ?")
        args.append(persona_key)
    if owner_device_id:
        # owner 可以为空 (通用池) 或匹配
        where.append("(owner_device_id = ? OR owner_device_id = '')")
        args.append(owner_device_id)
    sql = ("SELECT * FROM line_accounts WHERE " + " AND ".join(where)
           + " ORDER BY (last_used_at = '') DESC, last_used_at ASC, id ASC")
    # Phase 11.1 CAS: UPDATE ... WHERE last_used_at=<prev> 保并发严格原子.
    # 最多 retry 3 次 (覆盖 "被另一进程抢先更新" 的罕见 race).
    MAX_ATTEMPTS = 3
    for attempt in range(MAX_ATTEMPTS):
        with _connect() as conn:
            rows = conn.execute(sql, args).fetchall()
            if not rows:
                # Phase 12.1: 写 skipped log 供 UI 诊断 "为什么没分配"
                _write_skipped_log(
                    conn, reason="no_match",
                    canonical_id=canonical_id, peer_name=peer_name,
                    source_device_id=source_device_id,
                    source_event_id=source_event_id,
                    filters={"region": region, "persona_key": persona_key,
                             "owner_device_id": owner_device_id})
                return None
            picked = None
            prev_last_used = None
            for row in rows:
                used = _count_recent_usage(conn, row["id"], hours=24)
                if used < int(row["daily_cap"] or 20):
                    picked = row
                    prev_last_used = row["last_used_at"] or ""
                    break
            if picked is None:
                logger.info("[line_pool.allocate] %d accounts matched 全超 cap",
                             len(rows))
                _write_skipped_log(
                    conn, reason="all_capped",
                    canonical_id=canonical_id, peer_name=peer_name,
                    source_device_id=source_device_id,
                    source_event_id=source_event_id,
                    filters={"region": region, "persona_key": persona_key,
                             "owner_device_id": owner_device_id,
                             "candidates": len(rows)})
                return None
            # CAS: 如果 last_used_at 被别人改了, UPDATE 影响 0 行, retry.
            new_last = _now_iso()
            cur = conn.execute(
                "UPDATE line_accounts SET last_used_at=?,"
                " times_used=times_used+1, updated_at=datetime('now')"
                " WHERE id=? AND COALESCE(last_used_at,'') = ?",
                (new_last, picked["id"], prev_last_used),
            )
            if cur.rowcount == 0:
                logger.debug("[line_pool.allocate] CAS 冲突 attempt=%d, retry",
                             attempt + 1)
                continue
            # CAS 成功 → 写 dispatch_log + 返
            conn.execute(
                "INSERT INTO line_dispatch_log (line_account_id, line_id,"
                " canonical_id, peer_name, source_device_id, source_event_id,"
                " status) VALUES (?,?,?,?,?,?,'planned')",
                (picked["id"], picked["line_id"], canonical_id, peer_name,
                 source_device_id, source_event_id),
            )
            fresh = conn.execute(
                "SELECT * FROM line_accounts WHERE id=?", (picked["id"],),
            ).fetchone()
            return _row_to_dict(fresh)
    # 3 次 CAS 都冲突 — 极罕见, 放弃本轮让上游 retry/fallback
    logger.warning("[line_pool.allocate] CAS 连续 %d 次冲突, 放弃", MAX_ATTEMPTS)
    return None


def mark_dispatch_outcome(line_account_id: int, *,
                            status: str = "sent",
                            note: str = "") -> bool:
    """更新 dispatch_log 最近一条的 status (B 机/dispatcher 完成发送后回写)."""
    if status not in {"sent", "failed", "skipped"}:
        raise ValueError(f"status 非法: {status}")
    with _connect() as conn:
        # 找该 line_account 最近一条 planned
        row = conn.execute(
            "SELECT id FROM line_dispatch_log WHERE line_account_id=?"
            " AND status='planned' ORDER BY id DESC LIMIT 1",
            (line_account_id,),
        ).fetchone()
        if not row:
            return False
        conn.execute(
            "UPDATE line_dispatch_log SET status=?, note=? WHERE id=?",
            (status, note, row["id"]),
        )
        return True


def seed_from_config(config_path: Optional[str] = None,
                      *, only_if_empty: bool = True) -> Dict[str, Any]:
    """启动时从 YAML 注入默认 LINE 账号 (Phase 11).

    ``only_if_empty=True`` (默认): 池子非空 → 不做任何事 (避免覆盖运营在 UI 改过
    的状态). 空池才 seed.

    ``only_if_empty=False``: 强制调 add_many (重复 line_id 会被 add_many 计入
    duplicate, 不报错). 运维脚本用.

    返: {skipped: bool, total, inserted, duplicate, invalid, errors}
    """
    import os
    import pathlib
    import yaml

    # 测试/CI 可以设 OPENCLAW_LINE_POOL_SEED_SKIP=1 跳过
    if os.environ.get("OPENCLAW_LINE_POOL_SEED_SKIP"):
        return {"skipped": True, "reason": "env_skip"}

    if config_path is None:
        # 项目根目录/config/line_pool_seed.yaml
        here = pathlib.Path(__file__).resolve().parent.parent.parent
        config_path = str(here / "config" / "line_pool_seed.yaml")

    path = pathlib.Path(config_path)
    if not path.exists():
        return {"skipped": True, "reason": "seed_file_not_found"}

    if only_if_empty:
        with _connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM line_accounts"
            ).fetchone()
            if int(row["n"] if row else 0) > 0:
                return {"skipped": True, "reason": "pool_not_empty"}

    try:
        with path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception as e:
        return {"skipped": True, "reason": f"yaml_parse_failed: {e}"}

    accounts = data.get("accounts") or []
    if not isinstance(accounts, list):
        return {"skipped": True, "reason": "accounts_not_list"}

    result = add_many(accounts)
    result["skipped"] = False
    logger.info("[line_pool.seed] from %s → %d inserted, %d duplicate, "
                 "%d invalid", path, result.get("inserted", 0),
                 result.get("duplicate", 0), result.get("invalid", 0))
    return result


def recent_dispatch_log(limit: int = 100) -> List[Dict[str, Any]]:
    limit = max(1, min(int(limit or 100), 1000))
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM line_dispatch_log ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {
                "id": r["id"],
                "line_account_id": r["line_account_id"],
                "line_id": r["line_id"],
                "canonical_id": r["canonical_id"] or "",
                "peer_name": r["peer_name"] or "",
                "source_device_id": r["source_device_id"] or "",
                "source_event_id": r["source_event_id"] or "",
                "status": r["status"] or "",
                "note": r["note"] or "",
                "created_at": r["created_at"] or "",
            }
            for r in rows
        ]
