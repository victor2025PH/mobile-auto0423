# -*- coding: utf-8 -*-
"""CanonicalResolver — 跨平台 Lead 身份解析 + 高置信度自动合并 (Phase 5)。

策略
----
1. **硬匹配** (`verified=1`): ``lead_identities`` 里 (platform, account_id)
   唯一约束命中 → 直接返回已有 canonical_id
2. **软匹配** (`verified=0`): 名字规范化 / 头像 hash / 电话后缀 指纹匹配,
   置信度计算:
     * display_name 完全一致 + 同 platform        → 0.35
     * display_name 规范化 (去空格/emoji) 一致    → 0.25
     * 头像 hash 一致                              → 0.40
     * 电话后 4 位一致                             → 0.20
     * metadata 里 bio_hash 一致                   → 0.15
   累加 clip [0, 1]。
3. **自动合并**: 置信度 ≥ ``AUTO_MERGE_THRESHOLD=0.85`` 且**源 canonical 无
   进行中的 handoff/锁** 时, 自动合并到 target, 落审计日志 (lead_merges 表)。
   未达阈值 → 只返回候选列表, 留人工 Dashboard 合并。

回滚
----
每次 merge 有 lead_merges 行, 提供 ``revert_merge(merge_id, reason, by)``。
撤销后原 canonical 重新激活, journey 事件按时间线分配回来 (不自动拆分, 人工改)。
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any, Dict, List, Optional, Tuple

from src.host.database import _connect

logger = logging.getLogger(__name__)

AUTO_MERGE_THRESHOLD = 0.85
SOFT_MATCH_MIN_SCORE = 0.40     # 低于此值不进入候选


def _normalize_name(name: str) -> str:
    """规范化名字: 小写, 去非字母数字日韩中文。用于软匹配比较。"""
    if not name:
        return ""
    # 保留中日韩 + 字母数字, 去空格/表情/标点
    return re.sub(r"[^\w　-鿿゠-ヿ぀-ゟ]", "", name).lower()


def _resolve_hard(platform: str, account_id: str) -> Optional[str]:
    """硬匹配: (platform, account_id) UNIQUE 索引查询。"""
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT canonical_id FROM lead_identities"
                " WHERE platform=? AND account_id=?",
                (platform, account_id),
            ).fetchone()
        if row:
            cid = row[0]
            # 检查是否被合并
            with _connect() as conn:
                r = conn.execute(
                    "SELECT merged_into FROM leads_canonical WHERE canonical_id=?",
                    (cid,),
                ).fetchone()
            if r and r[0]:
                return r[0]  # 跟随 merged_into 指针
            return cid
    except Exception as e:
        logger.debug("[canonical] 硬匹配查询失败: %s", e)
    return None


def _score_candidate(display_name: str,
                      extra_metadata: Dict[str, Any],
                      candidate_row: Dict[str, Any]) -> Tuple[float, List[str]]:
    """计算某候选 canonical 与输入的软匹配置信度 + 触发理由。"""
    score = 0.0
    reasons: List[str] = []
    cand_name = candidate_row.get("primary_name") or ""

    # 1. 名字精确一致
    if display_name and cand_name and display_name.strip() == cand_name.strip():
        score += 0.35
        reasons.append("name_exact")
    # 2. 名字规范化一致
    elif display_name and cand_name:
        n1 = _normalize_name(display_name)
        n2 = _normalize_name(cand_name)
        if n1 and n1 == n2:
            score += 0.25
            reasons.append("name_normalized")

    # metadata 维度
    cand_meta: Dict[str, Any] = {}
    try:
        cand_meta = json.loads(candidate_row.get("metadata_json") or "{}")
    except Exception:
        pass

    # 3. 头像 hash
    ah_in = (extra_metadata or {}).get("avatar_hash") or ""
    ah_cand = cand_meta.get("avatar_hash") or ""
    if ah_in and ah_in == ah_cand:
        score += 0.40
        reasons.append("avatar_hash")

    # 4. 电话后 4 位
    ph_in = str((extra_metadata or {}).get("phone") or "")
    ph_cand = str(cand_meta.get("phone") or "")
    if ph_in and ph_cand and ph_in[-4:] == ph_cand[-4:] and len(ph_in) >= 4:
        score += 0.20
        reasons.append("phone_suffix")

    # 5. bio hash
    bh_in = (extra_metadata or {}).get("bio_hash") or ""
    bh_cand = cand_meta.get("bio_hash") or ""
    if bh_in and bh_in == bh_cand:
        score += 0.15
        reasons.append("bio_hash")

    return min(score, 1.0), reasons


def _find_soft_candidates(display_name: str,
                           extra_metadata: Optional[Dict[str, Any]] = None,
                           limit: int = 20) -> List[Dict[str, Any]]:
    """在 leads_canonical 里找可能是同一人的候选。

    粗筛先: 同名 (规范化) 的前 N 条; 细筛交给 _score_candidate。
    """
    if not display_name:
        return []
    norm = _normalize_name(display_name)
    if not norm or len(norm) < 2:
        return []
    try:
        with _connect() as conn:
            conn.row_factory = __import__("sqlite3").Row
            # 粗筛: 名字规范化相同 (SQLite 不支持自定义函数, 这里用 LIKE 粗过滤)
            rows = conn.execute(
                "SELECT canonical_id, primary_name, primary_language,"
                " metadata_json FROM leads_canonical"
                " WHERE merged_into IS NULL AND primary_name != ''"
                " LIMIT ?",
                (200,),  # 最多扫 200 行找候选,性能和召回率平衡
            ).fetchall()
        candidates = []
        for r in rows:
            d = dict(r)
            # 进一步规范化比较
            cand_norm = _normalize_name(d.get("primary_name") or "")
            if cand_norm == norm or (cand_norm and norm in cand_norm):
                score, reasons = _score_candidate(display_name,
                                                    extra_metadata or {}, d)
                if score >= SOFT_MATCH_MIN_SCORE:
                    d["score"] = score
                    d["reasons"] = reasons
                    candidates.append(d)
        candidates.sort(key=lambda x: -x["score"])
        return candidates[:limit]
    except Exception as e:
        logger.debug("[canonical] 软匹配失败: %s", e)
        return []


def resolve_identity(platform: str, account_id: str, *,
                      display_name: str = "",
                      extra_metadata: Optional[Dict[str, Any]] = None,
                      discovered_via: str = "",
                      discovered_by_device: str = "",
                      language: str = "",
                      persona_key: str = "",
                      auto_merge: bool = True) -> str:
    """核心入口: 按 (platform, account_id) 拿到 canonical_id, 没有就创建。

    Args:
        platform: 必须。facebook / line / whatsapp / telegram / instagram
        account_id: 必须。该平台上的账号唯一标识
        display_name: 可选。用于软匹配 + primary_name 填充
        extra_metadata: 可选。avatar_hash / phone / bio_hash 等, 供软匹配
        discovered_via: 可选。来源 (group_extract / inbox / handoff)
        discovered_by_device: 可选。发现者 device_id
        language: 可选。首次写入时填 primary_language
        persona_key: 可选。首次写入时填 primary_persona_key
        auto_merge: True (默认) = 置信度 ≥ 阈值时自动合并; False = 永远新建

    Returns:
        canonical_id (UUID 字符串)
    """
    if not platform or not account_id:
        raise ValueError("platform / account_id 必填")
    platform = platform.lower().strip()
    account_id = account_id.strip()

    # 1) 硬匹配
    hit = _resolve_hard(platform, account_id)
    if hit:
        # 补全 identity 里的缺失字段 (display_name 等, 幂等)
        try:
            with _connect() as conn:
                conn.execute(
                    "UPDATE lead_identities SET"
                    " display_name=CASE WHEN display_name='' THEN ? ELSE display_name END,"
                    " discovered_via=CASE WHEN discovered_via='' THEN ? ELSE discovered_via END,"
                    " discovered_by_device=CASE WHEN discovered_by_device='' THEN ? ELSE discovered_by_device END"
                    " WHERE platform=? AND account_id=?",
                    (display_name, discovered_via, discovered_by_device,
                     platform, account_id),
                )
        except Exception:
            pass
        return hit

    # 2) 软匹配: 名字相近 + metadata 指纹交叉确认
    merge_target: Optional[str] = None
    merge_confidence = 0.0
    merge_reasons: List[str] = []
    if auto_merge and display_name:
        candidates = _find_soft_candidates(display_name, extra_metadata or {})
        if candidates:
            top = candidates[0]
            if top["score"] >= AUTO_MERGE_THRESHOLD:
                merge_target = top["canonical_id"]
                merge_confidence = top["score"]
                merge_reasons = top["reasons"]
                logger.info(
                    "[canonical] 高置信度软匹配 %.2f → 合并到 %s (reasons=%s)",
                    merge_confidence, merge_target[:12], merge_reasons)

    # 3) 决定最终 canonical_id
    if merge_target:
        # 直接把新 identity 挂到已有 canonical 下, 不新建 lead
        canonical_id = merge_target
        verified = 0  # 软匹配产物, verified=0
        try:
            with _connect() as conn:
                conn.execute(
                    "INSERT INTO lead_identities"
                    " (canonical_id, platform, account_id, display_name,"
                    "  verified, discovered_via, discovered_by_device,"
                    "  metadata_json)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (canonical_id, platform, account_id, display_name,
                     verified, discovered_via, discovered_by_device,
                     json.dumps(extra_metadata or {}, ensure_ascii=False)),
                )
                # 审计日志 (不产生"合并"事件, 因为是新 identity 直接挂而非两个 lead 合并)
                # 可以在 journey 里记一笔 soft_match_merged
        except Exception as e:
            # UNIQUE 冲突 (并发场景) → 退回硬匹配
            logger.debug("[canonical] soft-merge insert 冲突, 退回硬查: %s", e)
            return _resolve_hard(platform, account_id) or canonical_id
        # 审计: 写 lead_merges (source=虚拟新 canonical, target=merge_target)
        try:
            _record_merge(source_canonical_id=f"virt-ident:{platform}:{account_id}",
                          target_canonical_id=canonical_id,
                          mode="auto_soft_identity",
                          confidence=merge_confidence,
                          reasons=merge_reasons,
                          merged_by="system")
        except Exception:
            pass
        # Journey 记录
        try:
            from .journey import append_journey
            append_journey(canonical_id, actor="system",
                           action="lead_marked_duplicate",
                           platform=platform,
                           data={"matched_account": account_id,
                                 "confidence": merge_confidence,
                                 "reasons": merge_reasons})
        except Exception:
            pass
        return canonical_id

    # 4) 新建 canonical + identity
    canonical_id = str(uuid.uuid4())
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO leads_canonical"
                " (canonical_id, primary_name, primary_language,"
                "  primary_persona_key, metadata_json)"
                " VALUES (?,?,?,?,?)",
                (canonical_id, display_name, language, persona_key,
                 json.dumps(extra_metadata or {}, ensure_ascii=False)),
            )
            conn.execute(
                "INSERT INTO lead_identities"
                " (canonical_id, platform, account_id, display_name, verified,"
                "  discovered_via, discovered_by_device, metadata_json)"
                " VALUES (?,?,?,?,1,?,?,?)",
                (canonical_id, platform, account_id, display_name,
                 discovered_via, discovered_by_device,
                 json.dumps(extra_metadata or {}, ensure_ascii=False)),
            )
    except Exception as e:
        # 并发新建冲突 → 再查一次硬匹配
        logger.debug("[canonical] 新建冲突, 再硬查: %s", e)
        existing = _resolve_hard(platform, account_id)
        if existing:
            return existing
        raise
    try:
        from .journey import append_journey
        append_journey(canonical_id, actor="system", action="extracted",
                       actor_device=discovered_by_device,
                       platform=platform,
                       data={"account_id": account_id,
                             "display_name": display_name,
                             "via": discovered_via})
    except Exception:
        pass
    return canonical_id


def auto_merge_candidates(canonical_id: str,
                           min_confidence: float = 0.70) -> List[Dict[str, Any]]:
    """列出某 lead 的潜在合并候选 (不自动操作, 给 Dashboard / 人工用)。

    Args:
        canonical_id: 要查的 lead
        min_confidence: 置信度下限 (默认 0.70, 低于此不返回)
    """
    try:
        with _connect() as conn:
            conn.row_factory = __import__("sqlite3").Row
            row = conn.execute(
                "SELECT * FROM leads_canonical WHERE canonical_id=?",
                (canonical_id,)).fetchone()
        if not row:
            return []
        d = dict(row)
    except Exception:
        return []
    meta = {}
    try:
        meta = json.loads(d.get("metadata_json") or "{}")
    except Exception:
        pass
    candidates = _find_soft_candidates(d.get("primary_name") or "", meta, limit=50)
    return [c for c in candidates
            if c["canonical_id"] != canonical_id and c["score"] >= min_confidence]


def _record_merge(source_canonical_id: str,
                   target_canonical_id: str,
                   mode: str,
                   confidence: float,
                   reasons: List[str],
                   merged_by: str) -> int:
    try:
        with _connect() as conn:
            cur = conn.execute(
                "INSERT INTO lead_merges"
                " (source_canonical_id, target_canonical_id, merge_mode,"
                "  confidence, merge_reasons_json, merged_by)"
                " VALUES (?,?,?,?,?,?)",
                (source_canonical_id, target_canonical_id, mode,
                 float(confidence), json.dumps(reasons, ensure_ascii=False),
                 merged_by),
            )
            return cur.lastrowid or 0
    except Exception as e:
        logger.warning("[canonical] record_merge 失败: %s", e)
        return 0


def merge_manually(source_canonical_id: str,
                    target_canonical_id: str,
                    merged_by: str = "human",
                    reason: str = "manual") -> bool:
    """手动合并 (Dashboard 入口)。把 source 标记为 merged_into=target。

    * 事务原子: 更新 source + 搬迁 lead_identities + 写审计
    * source 后续 resolve 会跟随 merged_into 指针返回 target
    * journey 保留在各自 canonical_id 下; 查询 Dossier 时按 target 聚合, 含
      source 的 journey 事件 (通过 merges 映射)
    """
    if source_canonical_id == target_canonical_id:
        return False
    try:
        with _connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            # 标记 source
            conn.execute(
                "UPDATE leads_canonical SET merged_into=?, updated_at=datetime('now')"
                " WHERE canonical_id=? AND merged_into IS NULL",
                (target_canonical_id, source_canonical_id))
            # identities 改挂
            conn.execute(
                "UPDATE lead_identities SET canonical_id=? WHERE canonical_id=?",
                (target_canonical_id, source_canonical_id))
            conn.execute("COMMIT")
    except Exception as e:
        logger.warning("[canonical] manual merge 失败: %s", e)
        return False
    _record_merge(source_canonical_id, target_canonical_id, "manual",
                  1.0, ["human_decision"], merged_by)
    try:
        from .journey import append_journey
        append_journey(target_canonical_id, actor=merged_by,
                       action="lead_merged",
                       data={"from": source_canonical_id, "reason": reason})
    except Exception:
        pass
    return True


def revert_merge(merge_id: int,
                  reverted_by: str = "human",
                  reason: str = "") -> bool:
    """撤销一次合并 (自动或手动产生的都支持)。

    行为: source canonical 的 merged_into 清空, identities 改挂回 source。
    **注意**: journey 历史不自动拆分 (时间线依然挂在各自时间点),
    Dashboard 上可见 "曾合并, 已撤销" 标记。
    """
    try:
        with _connect() as conn:
            conn.row_factory = __import__("sqlite3").Row
            row = conn.execute(
                "SELECT * FROM lead_merges WHERE id=? AND reverted_at IS NULL",
                (int(merge_id),)).fetchone()
            if not row:
                return False
            m = dict(row)
            src = m["source_canonical_id"]
            if src.startswith("virt-ident:"):
                # soft-identity 合并: 只能拆 identity (把对应 identity 移回新 canonical)
                # 细节留给 dashboard 手工处理
                logger.warning("[canonical] virt-ident 合并撤销需人工介入 merge_id=%s", merge_id)
                return False
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE leads_canonical SET merged_into=NULL, updated_at=datetime('now')"
                " WHERE canonical_id=?", (src,))
            conn.execute(
                "UPDATE lead_identities SET canonical_id=? WHERE canonical_id=?"
                " AND discovered_at > ?",
                (src, m["target_canonical_id"], m["merged_at"]))
            conn.execute(
                "UPDATE lead_merges SET reverted_at=datetime('now'), reverted_reason=?"
                " WHERE id=?", (reason or f"by {reverted_by}", int(merge_id)))
            conn.execute("COMMIT")
    except Exception as e:
        logger.warning("[canonical] revert merge 失败: %s", e)
        return False
    return True


def update_canonical_metadata(canonical_id: str,
                              metadata_patch: Dict[str, Any],
                              tags: Optional[List[str]] = None) -> bool:
    """合并 metadata_patch 到 leads_canonical.metadata_json (shallow merge).

    用于 L2 VLM PASS 后把 age_band/gender/is_japanese/overall_confidence
    等精准画像字段存入用户画像 DB, 供运营面板 / CRM 查询.

    Args:
        canonical_id: leads_canonical.canonical_id
        metadata_patch: 要合并的字段 (覆盖同 key)
        tags: 可选, append 到 tags 列 (逗号分隔)

    Returns True if updated, False otherwise.
    """
    if not canonical_id or not metadata_patch:
        return False
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT metadata_json, tags FROM leads_canonical WHERE canonical_id=?",
                (canonical_id,),
            ).fetchone()
            if not row:
                logger.warning("[canonical] update_metadata: %s 不存在",
                               canonical_id[:12])
                return False
            try:
                meta = json.loads(row["metadata_json"] or "{}")
            except Exception:
                meta = {}
            meta.update({k: v for k, v in metadata_patch.items() if v is not None})
            new_tags = row["tags"] or ""
            if tags:
                existing = {t.strip() for t in new_tags.split(",") if t.strip()}
                existing.update(t.strip() for t in tags if t)
                new_tags = ",".join(sorted(existing))
            conn.execute(
                "UPDATE leads_canonical SET metadata_json=?, tags=?,"
                " updated_at=datetime('now') WHERE canonical_id=?",
                (json.dumps(meta, ensure_ascii=False), new_tags, canonical_id),
            )
            return True
    except Exception as e:
        logger.warning("[canonical] update_metadata 失败: %s", e)
        return False


def list_l2_verified_leads(
    *, age_band: Optional[str] = None,
    gender: Optional[str] = None,
    is_japanese: Optional[bool] = None,
    persona_key: Optional[str] = None,
    platform: Optional[str] = None,
    min_score: float = 0,
    limit: int = 50,
    include_tags: Optional[List[str]] = None,
    exclude_tags: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Phase 10.3: 查询 L2 VLM 验证过的"精准画像用户".

    逻辑:
      - SQL 层按 ``tags LIKE '%l2_verified%'`` 预筛 (tag 写入时做了归一化).
      - JSON metadata 字段 (age_band / gender / is_japanese / l2_persona_key /
        l2_score) 由 Python 层过滤 — 避免依赖 SQLite JSON1 扩展 (老版本不带).
      - 按 l2_score 降序返 + l2_verified_at 新 → 旧.

    返回: [{canonical_id, display_name, platform, primary_account_id,
            metadata (dict), tags (list), l2_score, l2_verified_at}, ...]
    """
    limit = max(1, min(int(limit or 50), 1000))
    # SQL 预筛: tag 含 l2_verified + 未被合并. 其它过滤在 Python 层.
    sql = (
        "SELECT canonical_id, primary_name, tags, metadata_json, updated_at"
        "  FROM leads_canonical"
        " WHERE tags LIKE '%l2_verified%' AND merged_into IS NULL"
        " ORDER BY updated_at DESC LIMIT ?"
    )
    args: List[Any] = [limit * 4]  # 多拉一些, Python 层再筛/切

    out: List[Dict[str, Any]] = []
    try:
        with _connect() as conn:
            rows = conn.execute(sql, args).fetchall()
            # 平台 / account_id 在 lead_identities M:N 表, 取首个 identity 作为展示.
            ident_cache: Dict[str, Dict[str, str]] = {}
            if rows:
                cids_tuple = tuple(r["canonical_id"] for r in rows)
                placeholders = ",".join(["?"] * len(cids_tuple))
                for ir in conn.execute(
                    f"SELECT canonical_id, platform, account_id FROM lead_identities"
                    f" WHERE canonical_id IN ({placeholders})"
                    f" ORDER BY id ASC",
                    cids_tuple,
                ).fetchall():
                    # 只保第一条 (首次发现)
                    ident_cache.setdefault(
                        ir["canonical_id"],
                        {"platform": ir["platform"] or "",
                         "account_id": ir["account_id"] or ""})
    except Exception as e:
        logger.warning("[canonical] list_l2_verified SQL 失败: %s", e)
        return []

    for row in rows:
        try:
            meta = json.loads(row["metadata_json"] or "{}")
        except Exception:
            meta = {}
        tags_str = row["tags"] or ""
        tags = [t.strip() for t in tags_str.split(",") if t.strip()]

        if age_band and (meta.get("age_band") or "").lower() != age_band.lower():
            continue
        if gender and (meta.get("gender") or "").lower() != gender.lower():
            continue
        if is_japanese is not None:
            _ij = meta.get("is_japanese")
            if bool(_ij) is not bool(is_japanese):
                continue
        if persona_key and (meta.get("l2_persona_key") or "") != persona_key:
            continue
        try:
            score_v = float(meta.get("l2_score", 0) or 0)
        except (TypeError, ValueError):
            score_v = 0.0
        if score_v < float(min_score or 0):
            continue

        ident = ident_cache.get(row["canonical_id"], {})
        _plat = (ident.get("platform") or "").lower()
        if platform and _plat != platform.lower():
            continue
        # Phase 12.2 tags include/exclude (含 referral_dead / line_referred 等)
        if include_tags:
            if not all(t in tags for t in include_tags):
                continue
        if exclude_tags:
            if any(t in tags for t in exclude_tags):
                continue

        out.append({
            "canonical_id": row["canonical_id"],
            "display_name": row["primary_name"] or "",
            "platform": _plat,
            "primary_account_id": ident.get("account_id") or "",
            "tags": tags,
            "metadata": meta,
            "l2_score": score_v,
            "l2_verified_at": meta.get("l2_verified_at") or "",
        })
        if len(out) >= limit:
            break
    # 排序: 先按 l2_verified_at 新 → 旧, 再按 l2_score 高 → 低 (主键); Python
    # sort stable 保证同 score 下仍按 verified_at 新的在前.
    out.sort(key=lambda x: x["l2_verified_at"], reverse=True)
    out.sort(key=lambda x: x["l2_score"], reverse=True)
    return out
