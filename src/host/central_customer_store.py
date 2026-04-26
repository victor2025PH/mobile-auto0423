# -*- coding: utf-8 -*-
"""L2 中央客户画像 store — PostgreSQL backend.

动机
----
200 设备 (10 worker × 20 phones) 跨机部署, 客户画像 + 聊天 + 漏斗事件
不能再各 worker 自己 SQLite. 主控部署 PostgreSQL, 各 worker 通过 HTTP
push 写入, 人工后台从主控统一查询.

部署
----
PG 16 已在主控 (192.168.0.118:5432) 跑, db=openclaw, user=openclaw_app.
schema 见 migrations/001_central_customer_schema.sql, 5 表:
- customers (master 客户记录, AI 画像)
- customer_events (业务事件 append-only)
- customer_chats (聊天历史 append-only, 含 AI 生成标记)
- customer_handoffs (人机交接状态机)
- _schema_version (schema 演进)

API
---
单进程内**单例**, 自动管 connection pool::

    from src.host.central_customer_store import get_store

    store = get_store()
    cust_id = store.upsert_customer(
        canonical_id="fb_uid_12345",
        canonical_source="facebook",
        primary_name="さとう たかひろ",
        ai_profile={"topics": ["food", "travel"], ...},
        worker_id="worker-175",
    )
    store.record_event(cust_id, "greeting_sent", worker_id="worker-175",
                       device_id="4HUSIB...", meta={"template_id": "jp_v3"})
    store.record_chat(cust_id, channel="messenger", direction="outgoing",
                      content="...", ai_generated=True)

线程安全: psycopg2 连接池 (ThreadedConnectionPool), 每次操作 borrow + return.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

import psycopg2
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool

logger = logging.getLogger(__name__)

# ── 配置 ──────────────────────────────────────────────────────────────
DEFAULT_PG_HOST = os.environ.get("OPENCLAW_PG_HOST", "127.0.0.1")
DEFAULT_PG_PORT = int(os.environ.get("OPENCLAW_PG_PORT", "5432"))
DEFAULT_PG_DB = os.environ.get("OPENCLAW_PG_DB", "openclaw")
DEFAULT_PG_USER = os.environ.get("OPENCLAW_PG_USER", "openclaw_app")
DEFAULT_PG_PASSWORD = os.environ.get("OPENCLAW_PG_PASSWORD", "")

DEFAULT_POOL_MIN = 2
DEFAULT_POOL_MAX = 20  # 200 worker 并发写, max 20 连接够 (每连接复用)


class CentralCustomerStore:
    """中央客户画像 store, 包装 PG ThreadedConnectionPool."""

    def __init__(
        self,
        host: str = DEFAULT_PG_HOST,
        port: int = DEFAULT_PG_PORT,
        dbname: str = DEFAULT_PG_DB,
        user: str = DEFAULT_PG_USER,
        password: str = DEFAULT_PG_PASSWORD,
        pool_min: int = DEFAULT_POOL_MIN,
        pool_max: int = DEFAULT_POOL_MAX,
    ):
        self._dsn = (
            f"host={host} port={port} dbname={dbname} user={user} "
            f"password={password} application_name=openclaw_central"
        )
        self._pool: Optional[ThreadedConnectionPool] = None
        self._pool_lock = threading.Lock()
        self._pool_min = pool_min
        self._pool_max = pool_max
        self._connect()

    def _connect(self) -> None:
        try:
            self._pool = ThreadedConnectionPool(
                self._pool_min, self._pool_max, dsn=self._dsn,
            )
            logger.info("[central_store] PG pool ready (min=%d, max=%d)",
                        self._pool_min, self._pool_max)
        except Exception as exc:
            logger.exception("[central_store] PG pool init failed: %s", exc)
            raise

    def close(self) -> None:
        if self._pool:
            self._pool.closeall()
            self._pool = None

    @contextmanager
    def _conn(self) -> Iterator[psycopg2.extensions.connection]:
        if not self._pool:
            raise RuntimeError("central_store pool not initialized")
        c = self._pool.getconn()
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            self._pool.putconn(c)

    @contextmanager
    def _cursor(self) -> Iterator[psycopg2.extras.RealDictCursor]:
        with self._conn() as c:
            with c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                yield cur

    # ── customers upsert ─────────────────────────────────────────────
    def upsert_customer(
        self,
        canonical_id: str,
        canonical_source: str,
        primary_name: Optional[str] = None,
        age_band: Optional[str] = None,
        gender: Optional[str] = None,
        country: Optional[str] = None,
        interests: Optional[List[str]] = None,
        ai_profile: Optional[Dict[str, Any]] = None,
        status: Optional[str] = None,
        worker_id: Optional[str] = None,
        device_id: Optional[str] = None,
        customer_id: Optional[str] = None,
    ) -> str:
        """upsert by (canonical_source, canonical_id), 返回 customer_id (UUID 字符串).

        已存在: 更新非 None 字段, 保留旧值; status 字段只在显式传入时覆盖.

        customer_id 给了就用 worker 端算的 UUIDv5; 没给主控用 gen_random_uuid().
        ON CONFLICT 路径 PK 不变, 总是返回首次写入时的 customer_id.
        """
        ai_profile = ai_profile or {}
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO customers (
                    customer_id,
                    canonical_id, canonical_source, primary_name, age_band,
                    gender, country, interests, ai_profile,
                    status, last_worker_id, last_device_id
                ) VALUES (
                    COALESCE(%s::uuid, gen_random_uuid()),
                    %s, %s, %s, %s, %s, %s, %s, %s::jsonb,
                    COALESCE(%s, 'in_funnel'), %s, %s
                )
                ON CONFLICT (canonical_source, canonical_id) DO UPDATE SET
                    primary_name = COALESCE(EXCLUDED.primary_name, customers.primary_name),
                    age_band = COALESCE(EXCLUDED.age_band, customers.age_band),
                    gender = COALESCE(EXCLUDED.gender, customers.gender),
                    country = COALESCE(EXCLUDED.country, customers.country),
                    interests = COALESCE(EXCLUDED.interests, customers.interests),
                    ai_profile = customers.ai_profile || EXCLUDED.ai_profile,
                    -- status 状态机: 终态/人工接管态不可由 worker push 降级,
                    -- in_line 不可回 messenger; 其它走单调升级 (in_funnel <
                    -- in_messenger < in_line) 或 EXCLUDED 覆盖.
                    status = CASE
                        WHEN customers.status IN ('accepted_by_human', 'converted', 'lost')
                            THEN customers.status
                        WHEN customers.status = 'in_line' AND EXCLUDED.status = 'in_messenger'
                            THEN customers.status
                        WHEN customers.status = 'in_messenger' AND EXCLUDED.status = 'in_funnel'
                            THEN customers.status
                        ELSE COALESCE(EXCLUDED.status, customers.status)
                    END,
                    last_worker_id = COALESCE(EXCLUDED.last_worker_id, customers.last_worker_id),
                    last_device_id = COALESCE(EXCLUDED.last_device_id, customers.last_device_id)
                RETURNING customer_id::text
                """,
                (
                    customer_id,
                    canonical_id, canonical_source, primary_name, age_band,
                    gender, country, interests, json.dumps(ai_profile),
                    status, worker_id, device_id,
                ),
            )
            row = cur.fetchone()
            return row["customer_id"]

    # ── events ──────────────────────────────────────────────────────
    def record_event(
        self,
        customer_id: str,
        event_type: str,
        worker_id: str,
        device_id: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> str:
        meta = meta or {}
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO customer_events (
                    customer_id, event_type, worker_id, device_id, meta
                ) VALUES (%s, %s, %s, %s, %s::jsonb)
                RETURNING event_id::text
                """,
                (customer_id, event_type, worker_id, device_id, json.dumps(meta)),
            )
            return cur.fetchone()["event_id"]

    # ── chats ───────────────────────────────────────────────────────
    def record_chat(
        self,
        customer_id: str,
        channel: str,
        direction: str,
        content: str,
        content_lang: Optional[str] = None,
        ai_generated: bool = False,
        template_id: Optional[str] = None,
        worker_id: Optional[str] = None,
        device_id: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> str:
        if direction not in ("incoming", "outgoing"):
            raise ValueError(f"invalid direction: {direction}")
        if channel not in ("facebook", "messenger", "line", "telegram"):
            raise ValueError(f"invalid channel: {channel}")
        meta = meta or {}
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO customer_chats (
                    customer_id, channel, direction, content, content_lang,
                    ai_generated, template_id, worker_id, device_id, meta
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                RETURNING chat_id::text
                """,
                (
                    customer_id, channel, direction, content, content_lang,
                    ai_generated, template_id, worker_id, device_id, json.dumps(meta),
                ),
            )
            return cur.fetchone()["chat_id"]

    # ── handoffs ────────────────────────────────────────────────────
    def initiate_handoff(
        self,
        customer_id: str,
        from_stage: str,
        to_stage: str,
        initiating_worker_id: str,
        initiating_device_id: Optional[str] = None,
        ai_summary: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> str:
        meta = meta or {}
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO customer_handoffs (
                    customer_id, from_stage, to_stage,
                    initiating_worker_id, initiating_device_id,
                    ai_summary, meta
                ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
                RETURNING handoff_id::text
                """,
                (
                    customer_id, from_stage, to_stage,
                    initiating_worker_id, initiating_device_id,
                    ai_summary, json.dumps(meta),
                ),
            )
            return cur.fetchone()["handoff_id"]

    def accept_handoff(
        self,
        handoff_id: str,
        accepted_by_human: str,
    ) -> bool:
        """人工接管. 已被接管返回 False (idempotent + 防重复)."""
        with self._cursor() as cur:
            cur.execute(
                """
                UPDATE customer_handoffs
                SET accepted_by_human = %s, accepted_at = NOW(),
                    outcome = 'accepted'
                WHERE handoff_id = %s AND accepted_by_human IS NULL
                RETURNING customer_id::text
                """,
                (accepted_by_human, handoff_id),
            )
            row = cur.fetchone()
            if not row:
                return False
            # 同步 customers.status='accepted_by_human' 让 L3 看板可见
            cur.execute(
                """
                UPDATE customers SET status = 'accepted_by_human'
                WHERE customer_id = %s
                  AND status NOT IN ('converted', 'lost', 'accepted_by_human')
                """,
                (row["customer_id"],),
            )
            return True

    def complete_handoff(
        self,
        handoff_id: str,
        outcome: str,
    ) -> bool:
        if outcome not in ("converted", "lost", "timeout"):
            raise ValueError(f"invalid outcome: {outcome}")
        with self._cursor() as cur:
            cur.execute(
                """
                UPDATE customer_handoffs
                SET completed_at = NOW(), outcome = %s
                WHERE handoff_id = %s AND completed_at IS NULL
                RETURNING customer_id::text
                """,
                (outcome, handoff_id),
            )
            row = cur.fetchone()
            if not row:
                return False
            customer_id = row["customer_id"]
            # 同步 customers.status 让 L3 看板一目了然.
            # 状态机守卫: 已 converted/lost 终态保持; in_line/accepted 升级到结果.
            cur.execute(
                """
                UPDATE customers SET status = %s
                WHERE customer_id = %s
                  AND status NOT IN ('converted', 'lost')
                """,
                (outcome, customer_id),
            )
            return True

    # ── 查询 ────────────────────────────────────────────────────────
    def list_customers(
        self,
        status: Optional[str] = None,
        country: Optional[str] = None,
        worker_id: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        where = []
        params = []
        if status:
            where.append("status = %s")
            params.append(status)
        if country:
            where.append("country = %s")
            params.append(country)
        if worker_id:
            where.append("last_worker_id = %s")
            params.append(worker_id)
        where_sql = " WHERE " + " AND ".join(where) if where else ""
        params.extend([limit, offset])

        with self._cursor() as cur:
            cur.execute(
                f"""
                SELECT customer_id::text, canonical_id, canonical_source,
                       primary_name, age_band, gender, country, interests,
                       ai_profile, status, priority_tag, last_worker_id, last_device_id,
                       created_at, updated_at
                FROM customers
                {where_sql}
                ORDER BY updated_at DESC
                LIMIT %s OFFSET %s
                """,
                params,
            )
            return [dict(r) for r in cur.fetchall()]

    def get_customer(
        self,
        customer_id: str,
        include_events: int = 50,
        include_chats: int = 100,
    ) -> Optional[Dict[str, Any]]:
        with self._cursor() as cur:
            cur.execute(
                "SELECT customer_id::text, canonical_id, canonical_source, "
                "primary_name, age_band, gender, country, interests, "
                "ai_profile, status, last_worker_id, last_device_id, "
                "created_at, updated_at "
                "FROM customers WHERE customer_id = %s",
                (customer_id,),
            )
            cust = cur.fetchone()
            if not cust:
                return None
            cust = dict(cust)

            cur.execute(
                "SELECT event_id::text, event_type, worker_id, device_id, meta, ts "
                "FROM customer_events WHERE customer_id = %s "
                "ORDER BY ts DESC LIMIT %s",
                (customer_id, include_events),
            )
            cust["events"] = [dict(r) for r in cur.fetchall()]

            cur.execute(
                "SELECT chat_id::text, channel, direction, content, content_lang, "
                "ai_generated, template_id, worker_id, ts "
                "FROM customer_chats WHERE customer_id = %s "
                "ORDER BY ts DESC LIMIT %s",
                (customer_id, include_chats),
            )
            cust["chats"] = [dict(r) for r in cur.fetchall()]

            cur.execute(
                "SELECT handoff_id::text, from_stage, to_stage, "
                "initiated_at, accepted_by_human, accepted_at, completed_at, "
                "outcome, ai_summary "
                "FROM customer_handoffs WHERE customer_id = %s "
                "ORDER BY initiated_at DESC",
                (customer_id,),
            )
            cust["handoffs"] = [dict(r) for r in cur.fetchall()]

            return cust

    def funnel_timeseries(self, days: int = 30) -> List[Dict[str, Any]]:
        """Phase-3: 历史漏斗时序 — 按天统计关键事件.

        返回 list of {date, friend_request_sent, greeting_sent, message_received,
                      wa_referral_sent, customer_converted, customer_lost}.
        """
        with self._cursor() as cur:
            cur.execute("""
                SELECT
                    DATE(ts) AS day,
                    SUM(CASE WHEN event_type='friend_request_sent' THEN 1 ELSE 0 END) AS friend_request_sent,
                    SUM(CASE WHEN event_type='greeting_sent' THEN 1 ELSE 0 END) AS greeting_sent,
                    SUM(CASE WHEN event_type='message_received' THEN 1 ELSE 0 END) AS message_received,
                    SUM(CASE WHEN event_type='wa_referral_sent' THEN 1 ELSE 0 END) AS wa_referral_sent
                FROM customer_events
                WHERE ts > NOW() - (INTERVAL '1 day' * %s)
                GROUP BY day
                ORDER BY day
            """, (max(1, min(int(days), 365)),))
            events_by_day = {str(r["day"]): dict(r) for r in cur.fetchall()}

            # converted/lost 来自 customer_handoffs.outcome
            cur.execute("""
                SELECT
                    DATE(completed_at) AS day,
                    SUM(CASE WHEN outcome='converted' THEN 1 ELSE 0 END) AS customer_converted,
                    SUM(CASE WHEN outcome='lost' THEN 1 ELSE 0 END) AS customer_lost
                FROM customer_handoffs
                WHERE completed_at IS NOT NULL
                  AND completed_at > NOW() - (INTERVAL '1 day' * %s)
                GROUP BY day
                ORDER BY day
            """, (max(1, min(int(days), 365)),))
            outcomes_by_day = {str(r["day"]): dict(r) for r in cur.fetchall()}

            # 合并 + 填零
            from datetime import datetime, timedelta
            all_days: List[Dict[str, Any]] = []
            today = datetime.utcnow().date()
            for i in range(int(days), -1, -1):
                d = today - timedelta(days=i)
                ds = d.isoformat()
                ev = events_by_day.get(ds, {})
                oc = outcomes_by_day.get(ds, {})
                all_days.append({
                    "date": ds,
                    "friend_request_sent": int(ev.get("friend_request_sent") or 0),
                    "greeting_sent": int(ev.get("greeting_sent") or 0),
                    "message_received": int(ev.get("message_received") or 0),
                    "wa_referral_sent": int(ev.get("wa_referral_sent") or 0),
                    "customer_converted": int(oc.get("customer_converted") or 0),
                    "customer_lost": int(oc.get("customer_lost") or 0),
                })
            return all_days

    def recompute_priority_tags(self) -> Dict[str, int]:
        """Phase-3: 客户分群 — 启发式 SQL 一次性重算所有 customers.priority_tag.

        规则:
            high: status in ('in_line', 'accepted_by_human', 'converted')
                  或 ai_profile->>'has_trigger_keyword' = 'true'
            low: status = 'lost' 或 status = 'in_funnel' 且 7 天没新事件
            medium: 默认

        返回 {high, medium, low, lost} 客户数.
        """
        with self._cursor() as cur:
            cur.execute("""
                UPDATE customers SET priority_tag = CASE
                    WHEN status IN ('converted', 'accepted_by_human', 'in_line') THEN 'high'
                    WHEN status = 'lost' THEN 'low'
                    WHEN status = 'in_funnel' AND updated_at < NOW() - INTERVAL '7 days' THEN 'low'
                    ELSE 'medium'
                END
            """)
            cur.execute("SELECT priority_tag, COUNT(*) AS n FROM customers GROUP BY priority_tag")
            return {r["priority_tag"]: int(r["n"]) for r in cur.fetchall()}

    def agent_sla_stats(self, days: int = 30) -> List[Dict[str, Any]]:
        """主管 SLA 看板: 按客服 username 统计接管 / 转化 / 平均处理时长.

        返回 list of dict, 每项含:
            - accepted_by_human (username)
            - handled (接管总数)
            - converted / lost / pending_count
            - conversion_rate (0-1)
            - avg_assign_to_complete_minutes (None if no completed)
        """
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT
                    accepted_by_human,
                    COUNT(*) AS handled,
                    SUM(CASE WHEN outcome='converted' THEN 1 ELSE 0 END) AS converted_n,
                    SUM(CASE WHEN outcome='lost' THEN 1 ELSE 0 END) AS lost_n,
                    SUM(CASE WHEN completed_at IS NULL THEN 1 ELSE 0 END) AS pending_n,
                    AVG(EXTRACT(EPOCH FROM (completed_at - accepted_at))/60.0)
                        FILTER (WHERE completed_at IS NOT NULL AND accepted_at IS NOT NULL)
                        AS avg_minutes
                FROM customer_handoffs
                WHERE accepted_by_human IS NOT NULL
                  AND accepted_at > NOW() - (INTERVAL '1 day' * %s)
                GROUP BY accepted_by_human
                ORDER BY handled DESC
                """,
                (max(1, min(int(days), 365)),),
            )
            rows = []
            for r in cur.fetchall():
                d = dict(r)
                handled = int(d.get("handled") or 0)
                conv = int(d.get("converted_n") or 0)
                d["conversion_rate"] = (conv / handled) if handled else 0.0
                d["avg_minutes"] = float(d["avg_minutes"]) if d.get("avg_minutes") is not None else None
                rows.append(d)
            return rows

    def list_pending_handoffs(self, limit: int = 100) -> List[Dict[str, Any]]:
        """L3 人工接管面板: 列出待接管 (accepted_by_human IS NULL)."""
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT
                    h.handoff_id::text, h.customer_id::text, h.from_stage,
                    h.to_stage, h.initiated_at, h.ai_summary,
                    h.initiating_worker_id, h.initiating_device_id,
                    c.canonical_source, c.primary_name, c.age_band, c.gender,
                    c.country, c.interests, c.ai_profile
                FROM customer_handoffs h
                JOIN customers c ON c.customer_id = h.customer_id
                WHERE h.accepted_by_human IS NULL
                ORDER BY h.initiated_at ASC
                LIMIT %s
                """,
                (limit,),
            )
            return [dict(r) for r in cur.fetchall()]

    def funnel_stats(self, days: int = 7) -> Dict[str, int]:
        """L3 dashboard 漏斗统计."""
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT status, COUNT(*) as cnt
                FROM customers
                WHERE updated_at > NOW() - INTERVAL '%s days'
                GROUP BY status
                """,
                (days,),
            )
            stats = {r["status"]: int(r["cnt"]) for r in cur.fetchall()}

            cur.execute(
                """
                SELECT event_type, COUNT(*) as cnt
                FROM customer_events
                WHERE ts > NOW() - INTERVAL '%s days'
                GROUP BY event_type
                """,
                (days,),
            )
            events = {r["event_type"]: int(r["cnt"]) for r in cur.fetchall()}

            return {"customers_by_status": stats, "events_by_type": events}


# ── 单例 ──────────────────────────────────────────────────────────────
_store_singleton: Optional[CentralCustomerStore] = None
_singleton_lock = threading.Lock()


def get_store() -> CentralCustomerStore:
    """获取/初始化单例."""
    global _store_singleton
    if _store_singleton is not None:
        return _store_singleton
    with _singleton_lock:
        if _store_singleton is None:
            _store_singleton = CentralCustomerStore()
        return _store_singleton


def reset_for_tests(**kwargs) -> CentralCustomerStore:
    """仅测试用: 重置单例."""
    global _store_singleton
    with _singleton_lock:
        if _store_singleton is not None:
            _store_singleton.close()
        _store_singleton = CentralCustomerStore(**kwargs)
        return _store_singleton
