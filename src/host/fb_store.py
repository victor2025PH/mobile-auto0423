# -*- coding: utf-8 -*-
"""Facebook 业务数据 store(Sprint 2 P0)。

3 张表的 CRUD + 漏斗聚合查询封装。
所有写入函数都是幂等/upsert 模式,可被 automation 层重复调用。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .database import _connect


def _now_iso() -> str:
    import datetime as _dt
    return _dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# facebook_groups
# ─────────────────────────────────────────────────────────────────────

def upsert_group(device_id: str, group_name: str, *,
                 group_url: str = "", member_count: int = 0,
                 language: str = "", country: str = "",
                 status: str = "joined",
                 preset_key: str = "") -> int:
    """新加入或更新群信息。返回 row id。"""
    if not device_id or not group_name:
        return 0
    now = _now_iso()
    with _connect() as conn:
        row = conn.execute(
            "SELECT id FROM facebook_groups WHERE device_id=? AND group_name=?",
            (device_id, group_name),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE facebook_groups SET group_url=COALESCE(NULLIF(?,''), group_url),"
                " member_count=CASE WHEN ?>0 THEN ? ELSE member_count END,"
                " language=COALESCE(NULLIF(?,''), language),"
                " country=COALESCE(NULLIF(?,''), country),"
                " status=?,"
                " preset_key=COALESCE(NULLIF(?,''), preset_key)"
                " WHERE id=?",
                (group_url, member_count, member_count, language, country,
                 status, preset_key, row[0]),
            )
            return row[0]
        cur = conn.execute(
            "INSERT INTO facebook_groups"
            " (device_id, group_name, group_url, member_count, language, country,"
            "  status, joined_at, preset_key)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (device_id, group_name, group_url, member_count, language, country,
             status, now, preset_key),
        )
        return cur.lastrowid


def mark_group_visit(device_id: str, group_name: str,
                     extracted_count: int = 0):
    with _connect() as conn:
        conn.execute(
            "UPDATE facebook_groups SET last_visited_at=?, visit_count=visit_count+1,"
            " extracted_member_count=extracted_member_count+? "
            "WHERE device_id=? AND group_name=?",
            (_now_iso(), int(extracted_count or 0), device_id, group_name),
        )


def list_groups(device_id: Optional[str] = None,
                status: str = "joined",
                limit: int = 200) -> List[Dict]:
    sql = "SELECT * FROM facebook_groups WHERE 1=1"
    params: list = []
    if device_id:
        sql += " AND device_id=?"
        params.append(device_id)
    if status:
        sql += " AND status=?"
        params.append(status)
    sql += " ORDER BY joined_at DESC LIMIT ?"
    params.append(limit)
    with _connect() as conn:
        conn.row_factory = __import__("sqlite3").Row
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────────────────────────────
# facebook_friend_requests
# ─────────────────────────────────────────────────────────────────────

def record_friend_request(device_id: str, target_name: str, *,
                          note: str = "", source: str = "",
                          target_profile_url: str = "",
                          status: str = "sent",
                          lead_id: Optional[int] = None,
                          preset_key: str = "") -> int:
    if not device_id or not target_name:
        return 0
    now = _now_iso()
    with _connect() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO facebook_friend_requests"
                " (device_id, target_name, target_profile_url, note, source,"
                "  status, sent_at, lead_id, preset_key)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (device_id, target_name, target_profile_url, note, source,
                 status, now, lead_id, preset_key),
            )
            return cur.lastrowid
        except Exception as e:
            logger.debug("record_friend_request 失败: %s", e)
            return 0


def update_friend_request_status(device_id: str, target_name: str,
                                 new_status: str):
    with _connect() as conn:
        if new_status == "accepted":
            conn.execute(
                "UPDATE facebook_friend_requests SET status=?, accepted_at=? "
                "WHERE device_id=? AND target_name=? "
                "AND status='sent'",
                (new_status, _now_iso(), device_id, target_name),
            )
        else:
            conn.execute(
                "UPDATE facebook_friend_requests SET status=? "
                "WHERE device_id=? AND target_name=? AND status='sent'",
                (new_status, device_id, target_name),
            )


def get_friend_request_stats(device_id: Optional[str] = None,
                             since_iso: Optional[str] = None,
                             preset_key: Optional[str] = None) -> Dict[str, int]:
    sql = ("SELECT status, COUNT(*) FROM facebook_friend_requests WHERE 1=1")
    params: list = []
    if device_id:
        sql += " AND device_id=?"
        params.append(device_id)
    if since_iso:
        sql += " AND sent_at >= ?"
        params.append(since_iso)
    if preset_key:
        sql += " AND preset_key=?"
        params.append(preset_key)
    sql += " GROUP BY status"
    with _connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    stats = {"sent": 0, "accepted": 0, "rejected": 0,
             "cancelled": 0, "risk": 0, "pending": 0}
    for s, c in rows:
        stats[s] = stats.get(s, 0) + c
    sent = stats["sent"]
    accepted = stats["accepted"]
    # 修正:旧逻辑 sent 不含 accepted 时分母漏算,现在 sent_total = sent + accepted
    sent_total = sent + accepted
    stats["sent_total"] = sent_total
    stats["accept_rate"] = round(accepted / sent_total, 3) if sent_total else 0.0
    return stats


def count_friend_requests_sent_since(device_id: str, hours: int = 24) -> int:
    """统计 rolling 窗口内发出的好友请求条数（按 ``sent_at`` 过滤）。

    用于 ``facebook_playbook.add_friend.daily_cap_per_account`` 硬闸：
    同一 device 在 24h 内尝试次数 ≥ cap 则拒绝新请求。

    计数包含所有 ``sent_at`` 落在窗口内的行（含后来 accepted / rejected 的），
    因为每一次尝试都消耗风控预算。
    """
    if not device_id or hours <= 0:
        return 0
    import datetime as _dt
    cutoff = (_dt.datetime.utcnow() - _dt.timedelta(hours=hours)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM facebook_friend_requests"
            " WHERE device_id=? AND sent_at>=?",
            (device_id, cutoff),
        ).fetchone()
    return int(row[0]) if row else 0


# ─────────────────────────────────────────────────────────────────────
# facebook_inbox_messages
# ─────────────────────────────────────────────────────────────────────

def record_inbox_message(device_id: str, peer_name: str, *,
                         peer_type: str = "friend",
                         message_text: str = "",
                         direction: str = "incoming",
                         ai_decision: str = "",
                         ai_reply_text: str = "",
                         language_detected: str = "",
                         lead_id: Optional[int] = None,
                         replied_at: Optional[str] = None,
                         preset_key: str = "",
                         template_id: str = "") -> int:
    """记录一条 Messenger 消息(incoming/outgoing 皆可)。

    2026-04-23 新增:
      * direction=outgoing 时自动填 ``sent_at`` 列(与 ``seen_at`` 同值);
        下游 count_outgoing_messages_since 优先读 sent_at,避免 seen_at 对
        outgoing 语义不清。老行 sent_at=NULL 会回退到 seen_at 兜底。
      * template_id: 打招呼文案来自哪个模板(格式 '<country>:<idx>'),
        供 A/B 分析;其他场景写空串。
    """
    if not device_id or not peer_name:
        return 0
    now = _now_iso()
    # direction=outgoing 时同步把 sent_at 写上(incoming 留 NULL)
    sent_at = now if direction == "outgoing" else None
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO facebook_inbox_messages"
            " (device_id, peer_name, peer_type, message_text, direction,"
            "  ai_decision, ai_reply_text, language_detected,"
            "  seen_at, sent_at, replied_at, lead_id, preset_key, template_id)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (device_id, peer_name, peer_type, message_text, direction,
             ai_decision, ai_reply_text, language_detected,
             now, sent_at, replied_at, lead_id, preset_key, template_id),
        )
        return cur.lastrowid


def count_outgoing_messages_since(device_id: str,
                                  hours: int = 24,
                                  ai_decision: Optional[str] = None) -> int:
    """统计 rolling 窗口内本机发出的消息条数。

    用于 ``facebook_playbook.send_greeting.daily_cap_per_account`` 硬闸：
    同一 device 在 24h 内主动外发消息次数 ≥ cap 则拒绝新的打招呼。

    2026-04-23 修复: 优先读 ``sent_at`` 列(outgoing 专用),回落到 ``seen_at``
    保证历史行(sent_at=NULL)仍被计入,避免 daily_cap 漏算。

    Args:
        device_id: 设备 ID
        hours: 窗口长度（小时）
        ai_decision: 可选过滤 ai_decision 字段（如 ``'greeting'`` 只统计
            "加好友后打招呼"这一类），为空则统计所有方向=outgoing 的消息
    """
    if not device_id or hours <= 0:
        return 0
    import datetime as _dt
    cutoff = (_dt.datetime.utcnow() - _dt.timedelta(hours=hours)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    # COALESCE(sent_at, seen_at) 让新旧行都能正确比较
    sql = ("SELECT COUNT(*) FROM facebook_inbox_messages"
           " WHERE device_id=? AND direction='outgoing'"
           " AND COALESCE(sent_at, seen_at) >= ?")
    params: list = [device_id, cutoff]
    if ai_decision:
        sql += " AND ai_decision=?"
        params.append(ai_decision)
    try:
        with _connect() as conn:
            row = conn.execute(sql, params).fetchone()
        return int(row[0]) if row else 0
    except Exception as e:
        logger.debug("count_outgoing_messages_since 失败: %s", e)
        return 0


def count_unreplied_greetings_to_peer(device_id: str,
                                      peer_name: str) -> int:
    """同 peer 最后一次 incoming 之后 B 机发出的 greeting 条数。

    语义说明 (F6, 来自 B→A 协作约定, 为 A 的 send_greeting_after_add_friend
    per-peer 5 次硬顶提供数据源):

      * "对方一旦回过就算关系建立,重置计数" — 用最后一次 incoming 的 id
        作为分界,只数它之后的 greetings
      * 对方从未发过 → 数所有历史 greetings (分界为 0)
      * 不依赖 ``replied_at`` 字段 (那个只在 B 回复时被设,若 auto_reply 关
        就漏设;按 incoming 时序更稳)

    典型用法 (A 机 send_greeting_after_add_friend 开头):

    .. code-block:: python

        if count_unreplied_greetings_to_peer(did, profile_name) >= 5:
            self._set_greet_reason("peer_cap_5x")
            return False

    Args:
        device_id: 设备 ID
        peer_name: 目标 peer 姓名 (与 ``facebook_inbox_messages.peer_name``
            精确匹配; 如果 A 机 normalize_name 未处理全角/变音,这里也不会处理)

    Returns:
        未回复 greeting 条数 (包含 ``peer_type`` 为 friend/friend_request 等所有类型);
        空参数/DB 异常返回 0。
    """
    if not device_id or not peer_name:
        return 0
    try:
        with _connect() as conn:
            # id 而非 seen_at/sent_at 做分界 — 高并发同秒写入时严格单调
            row = conn.execute(
                "SELECT COUNT(*) FROM facebook_inbox_messages"
                " WHERE device_id=? AND peer_name=?"
                " AND direction='outgoing' AND ai_decision='greeting'"
                " AND id > COALESCE("
                "   (SELECT MAX(id) FROM facebook_inbox_messages"
                "    WHERE device_id=? AND peer_name=? AND direction='incoming'),"
                "   0)",
                (device_id, peer_name, device_id, peer_name),
            ).fetchone()
        return int(row[0]) if row else 0
    except Exception as e:
        logger.debug("count_unreplied_greetings_to_peer 失败: %s", e)
        return 0


def list_inbox_messages(device_id: Optional[str] = None,
                        since_iso: Optional[str] = None,
                        limit: int = 200,
                        preset_key: Optional[str] = None) -> List[Dict]:
    sql = "SELECT * FROM facebook_inbox_messages WHERE 1=1"
    params: list = []
    if device_id:
        sql += " AND device_id=?"
        params.append(device_id)
    if since_iso:
        sql += " AND seen_at >= ?"
        params.append(since_iso)
    if preset_key:
        sql += " AND preset_key=?"
        params.append(preset_key)
    sql += " ORDER BY seen_at DESC LIMIT ?"
    params.append(limit)
    with _connect() as conn:
        conn.row_factory = __import__("sqlite3").Row
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def mark_incoming_replied(device_id: str, peer_name: str, *,
                          replied_at: Optional[str] = None,
                          peer_type: Optional[str] = None) -> int:
    """给该 peer 最近一条尚未标记的 incoming 行写 replied_at。

    触发时机: ``_ai_reply_and_send`` 成功发出回复后同步调用。
    幂等: 若该 peer 最近的 incoming 行已有 ``replied_at``, 不更新。

    Args:
        device_id: 设备 ID
        peer_name: 对方姓名 (与 incoming 行的 peer_name 完全相等)
        replied_at: 可选,覆盖默认 now; 便于测试注入固定时间
        peer_type: 可选过滤 (friend/stranger/...);默认不过滤

    Returns:
        实际更新的行数 (0 或 1)。
    """
    if not device_id or not peer_name:
        return 0
    ts = replied_at or _now_iso()
    sql = (
        "UPDATE facebook_inbox_messages SET replied_at=? "
        "WHERE id = ("
        " SELECT id FROM facebook_inbox_messages"
        " WHERE device_id=? AND peer_name=? AND direction='incoming'"
        " AND (replied_at IS NULL OR replied_at='')"
    )
    params: list = [ts, device_id, peer_name]
    if peer_type:
        sql += " AND peer_type=?"
        params.append(peer_type)
    sql += " ORDER BY id DESC LIMIT 1)"
    try:
        with _connect() as conn:
            cur = conn.execute(sql, params)
            return cur.rowcount or 0
    except Exception as e:
        logger.debug("mark_incoming_replied 失败: %s", e)
        return 0


def mark_greeting_replied_back(device_id: str, peer_name: str, *,
                               window_days: int = 7,
                               replied_at: Optional[str] = None) -> int:
    """跨 bot 归因:对方回复了 A 写入的 greeting 行 → 回写 ``replied_at``。

    对应 INTEGRATION_CONTRACT §三 "B 允许回写 A 写入的 greeting 行的 replied_at"。
    扫描条件:
      * ``direction='outgoing'`` + ``ai_decision='greeting'``
      * ``peer_type='friend_request'`` (A 的 greeting 路径写入的 peer_type)
      * ``COALESCE(sent_at, seen_at) >= utcnow() - window_days``
      * ``replied_at IS NULL`` (幂等,已标记过则跳过)

    命中最新一条 greeting 行写入 ``replied_at``,让 A 端的
    ``reply_rate_by_template`` / A/B 模板效果统计能跑。

    Args:
        device_id: 设备 ID
        peer_name: 对方姓名 (与 greeting 行的 peer_name 完全相等)
        window_days: 回溯窗口,默认 7 天;超出窗口的 greeting 视为"机缘已过"
        replied_at: 可选,覆盖默认 now

    Returns:
        实际更新的行数 (0 或 1)。
    """
    if not device_id or not peer_name or window_days <= 0:
        return 0
    import datetime as _dt
    cutoff = (_dt.datetime.utcnow() - _dt.timedelta(days=window_days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    ts = replied_at or _now_iso()
    sql = (
        "UPDATE facebook_inbox_messages SET replied_at=? "
        "WHERE id = ("
        " SELECT id FROM facebook_inbox_messages"
        " WHERE device_id=? AND peer_name=?"
        " AND direction='outgoing' AND ai_decision='greeting'"
        " AND peer_type='friend_request'"
        " AND COALESCE(sent_at, seen_at) >= ?"
        " AND (replied_at IS NULL OR replied_at='')"
        " ORDER BY id DESC LIMIT 1)"
    )
    try:
        with _connect() as conn:
            cur = conn.execute(sql, (ts, device_id, peer_name, cutoff))
            rc = cur.rowcount or 0
            # F1 (A→B review Q1): 命中时同步写一条 fb_contact_events
            # (Phase 5 事件表, /facebook/greeting-reply-rate 的权威数据源).
            # Phase 5 未 merge 时 record_contact_event 不在 globals → 静默 skip
            # 让老 replied_at 一路继续, 新 contact_events 在 merge 后自动激活。
            if rc > 0:
                _sync_greeting_replied_contact_event(
                    conn, device_id, peer_name, ts, window_days)
            return rc
    except Exception as e:
        logger.debug("mark_greeting_replied_back 失败: %s", e)
        return 0


def _sync_greeting_replied_contact_event(conn, device_id: str, peer_name: str,
                                         ts: str, window_days: int) -> None:
    """F1 辅助: 把 greeting_replied 事件同步到 fb_contact_events。

    Phase 5 (A 的 fb_contact_events + record_contact_event) 未 merge 时
    ``record_contact_event`` 不在模块 globals 里, 静默 skip 不抛。Phase 5
    merge 后自动工作, 不需要二次改动。
    """
    if "record_contact_event" not in globals():
        return
    try:
        # M2.1 (A Round 3 review): 本 SELECT 依赖 mark_greeting_replied_back
        # 的 UPDATE 行 `set replied_at=?` 用了同一个 ts 参数, 故 WHERE replied_at=ts
        # 一定命中刚更新那行。如果未来把 UPDATE 改成 replied_at=now() 或其他 ts,
        # 本 SELECT 会 miss — 需同步调整两侧, 或改用 RETURNING 子句 (SQLite 3.35+)。
        row = conn.execute(
            "SELECT template_id, preset_key FROM facebook_inbox_messages"
            " WHERE device_id=? AND peer_name=? AND direction='outgoing'"
            " AND ai_decision='greeting' AND replied_at=?"
            " ORDER BY id DESC LIMIT 1",
            (device_id, peer_name, ts),
        ).fetchone()
        if not row:
            return
        tid = (row[0] or "").split("|")[0]  # 去 '|fallback' 后缀,对齐 A 建议
        pkey = row[1] or ""
        evt_const = globals().get(
            "CONTACT_EVT_GREETING_REPLIED", "greeting_replied")
        globals()["record_contact_event"](
            device_id, peer_name, evt_const,
            template_id=tid,
            preset_key=pkey,
            meta={"via": "mark_greeting_replied_back",
                  "window_days": window_days},
        )
    except Exception as e:
        logger.debug(
            "[mark_greeting_replied_back] contact_event 同步失败: %s", e)


# ─────────────────────────────────────────────────────────────────────
# 漏斗聚合 — /facebook/funnel 数据源
# ─────────────────────────────────────────────────────────────────────

def get_funnel_metrics(device_id: Optional[str] = None,
                       since_iso: Optional[str] = None,
                       preset_key: Optional[str] = None) -> Dict[str, Any]:
    """计算完整漏斗:浏览群 → 提取成员 → 加好友 → 通过 → 私信 → 转化。

    Sprint 3: 支持 preset_key 切片。
    2026-04-23: 新增 greeting 专项维度
        * stage_greetings_sent      主动打招呼总数(ai_decision=greeting + outgoing)
        * stage_greetings_fallback  其中走 Messenger fallback 的数量
        * rate_greet_after_add      打招呼数 / 好友请求数 (覆盖率指标)
        * greeting_template_distribution 前 N 个最常用模板 + 命中数
    """
    fr_stats = get_friend_request_stats(device_id, since_iso, preset_key)
    inbox_msgs = list_inbox_messages(device_id, since_iso, limit=10000,
                                     preset_key=preset_key)
    incoming = [m for m in inbox_msgs if m.get("direction") == "incoming"]
    outgoing = [m for m in inbox_msgs if m.get("direction") == "outgoing"]
    wa_referrals = [m for m in outgoing if m.get("ai_decision") == "wa_referral"]
    greetings = [m for m in outgoing if m.get("ai_decision") == "greeting"]
    greetings_fallback = [
        m for m in greetings
        if isinstance(m.get("template_id"), str) and m.get("template_id", "").endswith("|fallback")
    ]
    # 模板分布 (top 5)
    template_counter: Dict[str, int] = {}
    for m in greetings:
        tid = (m.get("template_id") or "").split("|")[0]
        if tid:
            template_counter[tid] = template_counter.get(tid, 0) + 1
    template_dist = sorted(template_counter.items(), key=lambda kv: kv[1],
                           reverse=True)[:5]

    extracted_total = 0
    extra_filters = []
    extra_params: list = []
    if device_id:
        extra_filters.append("device_id=?")
        extra_params.append(device_id)
    if preset_key:
        extra_filters.append("preset_key=?")
        extra_params.append(preset_key)
    where = (" WHERE " + " AND ".join(extra_filters)) if extra_filters else ""
    with _connect() as conn:
        row = conn.execute(
            f"SELECT COALESCE(SUM(extracted_member_count), 0) "
            f"FROM facebook_groups{where}",
            extra_params,
        ).fetchone()
        extracted_total = row[0] or 0

    sent_total = fr_stats.get("sent_total", 0)
    accepted = fr_stats.get("accepted", 0)
    greet_count = len(greetings)
    return {
        "stage_extracted_members": int(extracted_total),
        "stage_friend_request_sent": int(sent_total),
        "stage_friend_accepted": int(accepted),
        "stage_greetings_sent": greet_count,
        "stage_greetings_fallback": len(greetings_fallback),
        "stage_inbox_incoming": len(incoming),
        "stage_outgoing_replies": len(outgoing),
        "stage_wa_referrals": len(wa_referrals),
        "rate_accept": fr_stats.get("accept_rate", 0.0),
        "rate_extract_to_request": round(sent_total / extracted_total, 3)
            if extracted_total else 0.0,
        "rate_request_to_inbox": round(len(incoming) / sent_total, 3)
            if sent_total else 0.0,
        "rate_inbox_to_referral": round(len(wa_referrals) / max(len(incoming), 1), 3),
        # 覆盖率: 每发 N 个好友请求, 有多少个实际打了招呼
        "rate_greet_after_add": round(greet_count / sent_total, 3) if sent_total else 0.0,
        # 模板 A/B: [["yaml:jp:3", 12], ["yaml:jp:1", 7], ...]
        "greeting_template_distribution": template_dist,
        "scope_device": device_id or "all",
        "scope_since": since_iso or "all_time",
        "scope_preset": preset_key or "all",
    }


def get_funnel_metrics_by_preset(device_id: Optional[str] = None,
                                 since_iso: Optional[str] = None
                                 ) -> List[Dict[str, Any]]:
    """按预设切片返回漏斗。Sprint 3 P1 — /facebook/funnel?group_by=preset_key。

    返回 [{preset_key, ..metrics..}, ...],按 sent_total 降序。
    """
    with _connect() as conn:
        sql = ("SELECT DISTINCT COALESCE(NULLIF(preset_key, ''), '_no_preset') "
               "FROM facebook_friend_requests WHERE 1=1")
        params: list = []
        if device_id:
            sql += " AND device_id=?"
            params.append(device_id)
        if since_iso:
            sql += " AND sent_at >= ?"
            params.append(since_iso)
        rows = conn.execute(sql, params).fetchall()
    presets = [r[0] for r in rows]
    out: List[Dict[str, Any]] = []
    for pk in presets:
        actual_pk = None if pk == "_no_preset" else pk
        m = get_funnel_metrics(device_id=device_id, since_iso=since_iso,
                               preset_key=actual_pk)
        m["preset_key"] = pk
        out.append(m)
    out.sort(key=lambda x: x.get("stage_friend_request_sent", 0), reverse=True)
    return out


# ─────────────────────────────────────────────────────────────────────
# fb_risk_events — 风控事件（驱动 Gate 自动冷却红旗）
# ─────────────────────────────────────────────────────────────────────

_RISK_KIND_RULES = [
    # 关键词 → 归一化 kind，顺序匹配（先到先得）
    (("identity", "confirm it's you", "confirm it is you", "verify"), "identity_verify"),
    (("captcha", "robot", "are you a human"), "captcha"),
    (("checkpoint", "temporarily blocked", "temporarily restricted"), "checkpoint"),
    (("disabled", "account is locked", "account has been"), "account_review"),
    (("suspicious", "unusual login"), "identity_verify"),
    (("can't use this feature", "cannot use this feature"), "policy_warning"),
    # F4 (来自 A→B review Q6): Messenger 发送文案被 FB 拒绝
    # (多语言,ja/zh/en/it 对齐 persona)
    (("can't be sent", "cannot be sent", "couldn't send", "unable to send",
      "message can't be sent", "message wasn't sent",
      "不能发送此消息", "发送失败", "无法发送", "訊息無法傳送",
      "送信できませんでした", "メッセージを送信できません",
      "non inviabile", "messaggio non inviato"), "content_blocked"),
]


def _classify_risk_kind(raw_message: str) -> str:
    s = (raw_message or "").lower()
    for kws, kind in _RISK_KIND_RULES:
        for kw in kws:
            if kw in s:
                return kind
    return "other"


def record_risk_event(device_id: str, raw_message: str, *,
                      task_id: str = "",
                      debounce_seconds: int = 60) -> int:
    """记录一条风控事件。

    debounce_seconds:
        同 device_id + 同 kind 在最近 N 秒内已落过库，则**去重不写**，
        避免 browse_feed 循环里每屏检测刷出几十条。返回 0 表示被去重。
    """
    if not device_id:
        return 0
    kind = _classify_risk_kind(raw_message)
    try:
        with _connect() as conn:
            if debounce_seconds > 0:
                row = conn.execute(
                    "SELECT id FROM fb_risk_events "
                    "WHERE device_id=? AND kind=? "
                    "AND detected_at > datetime('now', ?) "
                    "ORDER BY id DESC LIMIT 1",
                    (device_id, kind, f"-{int(debounce_seconds)} seconds"),
                ).fetchone()
                if row:
                    return 0
            cur = conn.execute(
                "INSERT INTO fb_risk_events (device_id, task_id, kind, raw_message)"
                " VALUES (?,?,?,?)",
                (device_id, task_id or "", kind, (raw_message or "")[:500]),
            )
            return cur.lastrowid or 0
    except Exception as e:
        logger.warning("[fb_risk] 写入失败 device=%s: %s", device_id[:12], e)
        return 0


# ─────────────────────────────────────────────────────────────────────
# 2026-04-23 Phase 3 P3-3: fb_contact_events —— 统一接触事件流水
# ─────────────────────────────────────────────────────────────────────

# event_type 枚举，供调用方和测试共用
CONTACT_EVT_ADD_FRIEND_SENT = "add_friend_sent"
CONTACT_EVT_ADD_FRIEND_RISK = "add_friend_risk"
CONTACT_EVT_ADD_FRIEND_ACCEPTED = "add_friend_accepted"    # B 写
CONTACT_EVT_ADD_FRIEND_REJECTED = "add_friend_rejected"    # B 写
CONTACT_EVT_GREETING_SENT = "greeting_sent"
CONTACT_EVT_GREETING_FALLBACK = "greeting_fallback"
CONTACT_EVT_GREETING_REPLIED = "greeting_replied"          # B 写,对方回了我们的 greeting
CONTACT_EVT_MESSAGE_RECEIVED = "message_received"          # B 写,对方主动 DM
CONTACT_EVT_WA_REFERRAL_SENT = "wa_referral_sent"          # B 写

VALID_CONTACT_EVENT_TYPES = frozenset({
    CONTACT_EVT_ADD_FRIEND_SENT,
    CONTACT_EVT_ADD_FRIEND_RISK,
    CONTACT_EVT_ADD_FRIEND_ACCEPTED,
    CONTACT_EVT_ADD_FRIEND_REJECTED,
    CONTACT_EVT_GREETING_SENT,
    CONTACT_EVT_GREETING_FALLBACK,
    CONTACT_EVT_GREETING_REPLIED,
    CONTACT_EVT_MESSAGE_RECEIVED,
    CONTACT_EVT_WA_REFERRAL_SENT,
})


def record_contact_event(device_id: str, peer_name: str, event_type: str, *,
                         template_id: str = "",
                         preset_key: str = "",
                         meta: Optional[Dict[str, Any]] = None) -> int:
    """记录一条接触事件。

    ``event_type`` 不在 ``VALID_CONTACT_EVENT_TYPES`` 时会记 warn log 但仍然写入
    (允许 B 扩展新类型,但提醒可能拼写错误)。

    ``meta`` 会被 JSON 序列化为 meta_json; 不强约束 schema, 允许放:
      * ``reply_to_template_id`` (B 写 greeting_replied 时放)
      * ``reply_ms_after`` (B 写 greeting_replied 时放, 对方回复距 greeting 发出的毫秒数)
      * ``decision`` / ``lang_detected`` 等任意辅助字段
    """
    if not device_id or not peer_name or not event_type:
        return 0
    if event_type not in VALID_CONTACT_EVENT_TYPES:
        logger.warning("[fb_contact_events] 未知 event_type=%s, 仍写入但请检查拼写", event_type)
    meta_str = ""
    if meta:
        try:
            import json as _j
            meta_str = _j.dumps(meta, ensure_ascii=False)
        except Exception as e:
            logger.debug("[fb_contact_events] meta 序列化失败: %s", e)
            meta_str = ""
    try:
        with _connect() as conn:
            cur = conn.execute(
                "INSERT INTO fb_contact_events"
                " (device_id, peer_name, event_type, template_id, preset_key, meta_json)"
                " VALUES (?,?,?,?,?,?)",
                (device_id, peer_name, event_type, template_id or "",
                 preset_key or "", meta_str),
            )
            return cur.lastrowid or 0
    except Exception as e:
        logger.debug("[fb_contact_events] 写入失败: %s", e)
        return 0


def count_contact_events(device_id: Optional[str] = None, *,
                         peer_name: Optional[str] = None,
                         event_type: Optional[str] = None,
                         hours: int = 24) -> int:
    """按 (device_id, peer_name, event_type) 组合计数 rolling 窗口内事件数。

    用途举例:
      * 同一对方在 24h 内被多次接触(发好友+打招呼+引流) → 骚扰配额
      * 某 device 24h 内 greeting_sent 总数 → 日限预警
    """
    if hours <= 0:
        return 0
    # at 列用 datetime('now') 默认格式 (空格分隔), 用 SQL 原生 datetime('now', '-N hours')
    # 做 cutoff 避免字符串格式不一致 (Phase 11 发现的 silent bug, 2026-04-25 修).
    sql = "SELECT COUNT(*) FROM fb_contact_events WHERE at >= datetime('now', ?)"
    params: list = [f"-{int(hours)} hours"]
    if device_id:
        sql += " AND device_id=?"
        params.append(device_id)
    if peer_name:
        sql += " AND peer_name=?"
        params.append(peer_name)
    if event_type:
        sql += " AND event_type=?"
        params.append(event_type)
    try:
        with _connect() as conn:
            row = conn.execute(sql, params).fetchone()
        return int(row[0]) if row else 0
    except Exception:
        return 0


def list_recent_contact_events_by_types(event_types: List[str],
                                        hours: int = 24,
                                        limit: int = 200,
                                        device_id: Optional[str] = None
                                        ) -> List[Dict[str, Any]]:
    """Phase 11: 扫近 N 小时指定类型 (多选) 的事件, 新 → 旧.

    用于 fb_line_dispatch_from_reply 消费 greeting_replied/message_received.
    ``at`` 列用 ``datetime('now')`` 默认格式 (空格分隔), 用 SQL 原生
    ``datetime('now', '-N hours')`` 做 cutoff 避免字符串格式不一致.
    """
    if not event_types or hours <= 0:
        return []
    placeholders = ",".join(["?"] * len(event_types))
    sql = ("SELECT id, device_id, peer_name, event_type, template_id,"
           " preset_key, meta_json, at FROM fb_contact_events"
           " WHERE at >= datetime('now', ?) AND event_type IN"
           f" ({placeholders})")
    params: list = [f"-{int(hours)} hours", *event_types]
    if device_id:
        sql += " AND device_id = ?"
        params.append(device_id)
    sql += " ORDER BY at DESC LIMIT ?"
    params.append(max(1, min(int(limit or 200), 2000)))
    try:
        with _connect() as conn:
            conn.row_factory = __import__("sqlite3").Row
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.debug("[fb_contact_events] list_recent_by_types 失败: %s", e)
        return []


def list_contact_events_by_peer(device_id: str, peer_name: str,
                                limit: int = 50) -> List[Dict[str, Any]]:
    """返回某 device 对某人的所有接触事件,按时间正序。

    用途: 诊断"为什么给 X 发了 5 次消息还没回" —— 看事件序列。
    """
    if not device_id or not peer_name:
        return []
    try:
        with _connect() as conn:
            conn.row_factory = __import__("sqlite3").Row
            rows = conn.execute(
                "SELECT id, device_id, peer_name, event_type, template_id,"
                " preset_key, meta_json, at FROM fb_contact_events"
                " WHERE device_id=? AND peer_name=?"
                " ORDER BY at ASC LIMIT ?",
                (device_id, peer_name, int(limit)),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def get_greeting_reply_rate_by_template(device_id: Optional[str] = None,
                                        hours: int = 168) -> List[Dict[str, Any]]:
    """按 template_id 分组, 计算 reply_rate = greeting_replied / greeting_sent。

    这是 A/B 实验的核心指标 —— 哪条打招呼模板通过率高。
    默认 168h (7 天) 窗口。B 的 Messenger 自动回复合并后这个数据才真正有效。
    """
    if hours <= 0:
        return []
    import datetime as _dt
    cutoff = (_dt.datetime.utcnow() - _dt.timedelta(hours=hours)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")

    # 先查各模板的 sent / replied 数
    where_dev = ""
    params: list = [cutoff]
    if device_id:
        where_dev = " AND device_id=?"
        params.append(device_id)
    sql = (
        "SELECT template_id, event_type, COUNT(*) FROM fb_contact_events"
        " WHERE at >= ?"
        + where_dev +
        " AND event_type IN ('greeting_sent','greeting_replied')"
        " AND template_id != ''"
        " GROUP BY template_id, event_type"
    )
    try:
        with _connect() as conn:
            rows = conn.execute(sql, params).fetchall()
    except Exception:
        return []
    bucket: Dict[str, Dict[str, int]] = {}
    for tid, evt, n in rows:
        b = bucket.setdefault(tid, {"sent": 0, "replied": 0})
        if evt == "greeting_sent":
            b["sent"] = int(n)
        elif evt == "greeting_replied":
            b["replied"] = int(n)
    out = []
    for tid, d in bucket.items():
        sent = d["sent"]
        replied = d["replied"]
        out.append({
            "template_id": tid,
            "sent": sent,
            "replied": replied,
            "reply_rate": round(replied / sent, 3) if sent else 0.0,
        })
    out.sort(key=lambda x: (-x["reply_rate"], -x["sent"]))
    return out


def count_risk_events_recent(device_id: str, hours: int = 24) -> int:
    """最近 N 小时内该设备风控事件总数，供 Gate 冷却判断。"""
    if not device_id:
        return 0
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM fb_risk_events "
                "WHERE device_id=? AND detected_at > datetime('now', ?)",
                (device_id, f"-{int(hours)} hours"),
            ).fetchone()
            return int(row[0] or 0)
    except Exception:
        return 0


# 2026-04-24 v2: L2 pause 决策要区分 severity.
# CRITICAL (account-level): 触发 L2 pause (12h default)
# MEDIUM/LOW (message-level, content_blocked 等): 不 pause L2, 只影响 greeting send
_CRITICAL_RISK_KINDS = frozenset({
    "identity_verify", "captcha", "checkpoint",
    "account_review", "policy_warning",
})


def count_critical_risk_events_recent(device_id: str, hours: int = 12) -> int:
    """仅 CRITICAL 级风控事件计数 (会触发 L2 pause 的).

    排除: kind='other' (通常是 content_blocked 一类短期 message-level friction),
    因为 content_blocked 只说 "今天这条消息被 FB 拒发", 不代表账号有长期风险,
    不该 pause L2 12 小时.
    """
    if not device_id:
        return 0
    try:
        with _connect() as conn:
            placeholders = ",".join("?" for _ in _CRITICAL_RISK_KINDS)
            sql = (f"SELECT COUNT(*) FROM fb_risk_events "
                   f"WHERE device_id=? AND detected_at > datetime('now', ?) "
                   f"AND kind IN ({placeholders})")
            params = [device_id, f"-{int(hours)} hours", *list(_CRITICAL_RISK_KINDS)]
            row = conn.execute(sql, params).fetchone()
            return int(row[0] or 0)
    except Exception:
        return 0


def list_recent_risk_events(device_id: Optional[str] = None,
                            hours: int = 24,
                            limit: int = 50) -> List[Dict[str, Any]]:
    """设备面板红旗下拉 / 调试用。"""
    sql = ("SELECT id, device_id, task_id, kind, raw_message, detected_at "
           "FROM fb_risk_events WHERE detected_at > datetime('now', ?)")
    params: list = [f"-{int(hours)} hours"]
    if device_id:
        sql += " AND device_id=?"
        params.append(device_id)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(int(limit))
    try:
        with _connect() as conn:
            conn.row_factory = __import__("sqlite3").Row
            rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []
