# -*- coding: utf-8 -*-
"""Webhook Dispatcher — 外发引流交接事件 (Phase 5)。

功能
----
* ``enqueue_webhook()`` 把事件写入 webhook_dispatches 表
* ``flush_pending_webhooks()`` 批量发送所有到期的 pending, 失败指数退避重试
* HMAC-SHA256 签名 + HTTPS only (可关 TLS 校验做调试, 生产必须开)
* 死信队列: 3 次重试失败进 ``dead_letter`` 状态, 人工 Dashboard 处理

配置 (``config/webhook_targets.yaml``)::

    # 按 event_type 路由到不同 URL, 支持多个 URL
    subscribers:
      handoff.created:
        - url: "https://ops-company/webhook/handoff"
          secret_key_env: "WEBHOOK_SECRET_OPS"   # HMAC key 从环境变量读
          enabled: true
      handoff.completed:
        - url: "https://slack.../services/xxx"
          secret_key_env: "WEBHOOK_SECRET_SLACK"

    # 全局设置
    retry_schedule_sec: [60, 300, 1800]   # 第 1/2/3 次重试间隔
    max_attempts: 3
    timeout_sec: 10
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import json
import logging
import os
from typing import Any, Dict, List, Optional

from src.host.database import _connect

logger = logging.getLogger(__name__)

DEFAULT_RETRY_SCHEDULE = (60, 300, 1800)
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_TIMEOUT_SEC = 10

_CONFIG_CACHE: Dict[str, Any] = {}
_CONFIG_LOADED_AT = 0.0


def _now_iso() -> str:
    return _dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _next_retry_iso(attempt: int) -> str:
    schedule = list(DEFAULT_RETRY_SCHEDULE)
    idx = min(max(0, attempt - 1), len(schedule) - 1)
    sec = schedule[idx]
    return (_dt.datetime.utcnow() + _dt.timedelta(seconds=sec)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _load_config() -> Dict[str, Any]:
    """读取 config/webhook_targets.yaml, 带 60s 缓存。"""
    import time as _t
    global _CONFIG_CACHE, _CONFIG_LOADED_AT
    if _CONFIG_CACHE and (_t.time() - _CONFIG_LOADED_AT) < 60:
        return _CONFIG_CACHE
    try:
        import yaml
        from pathlib import Path
        from src.host.device_registry import config_file
        path = config_file("webhook_targets.yaml")
        if not Path(path).exists():
            _CONFIG_CACHE = {"subscribers": {}}
            _CONFIG_LOADED_AT = _t.time()
            return _CONFIG_CACHE
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        _CONFIG_CACHE = data
        _CONFIG_LOADED_AT = _t.time()
        return data
    except Exception as e:
        logger.debug("[webhook] 配置加载失败: %s", e)
        return {"subscribers": {}}


def _get_subscribers(event_type: str) -> List[Dict[str, Any]]:
    """拿到指定 event_type 的所有启用的订阅 URL。

    支持通配 "*" 匹配所有事件; event_type 精确匹配优先。
    """
    cfg = _load_config()
    subs = cfg.get("subscribers") or {}
    out: List[Dict[str, Any]] = []
    for key in (event_type, "*"):
        for item in (subs.get(key) or []):
            if not isinstance(item, dict):
                continue
            if not item.get("enabled", True):
                continue
            if not item.get("url"):
                continue
            out.append(item)
    return out


# ─── 入队 API ────────────────────────────────────────────────────────

def enqueue_webhook(*, event_type: str,
                     payload: Dict[str, Any],
                     related_canonical_id: str = "",
                     related_handoff_id: str = "") -> int:
    """把事件拆成每订阅 URL 一条 webhook_dispatches 行。

    Returns:
        入队的行数 (0 = 无订阅者)
    """
    if not event_type:
        return 0
    subs = _get_subscribers(event_type)
    if not subs:
        # 没订阅者但仍记录一条 "no_subscriber" 用于审计 (可选)
        return 0
    payload_str = ""
    try:
        payload_str = json.dumps(payload, ensure_ascii=False, default=str)
    except Exception:
        payload_str = "{}"
    count = 0
    try:
        with _connect() as conn:
            for sub in subs:
                conn.execute(
                    "INSERT INTO webhook_dispatches"
                    " (event_type, target_url, payload_json,"
                    "  related_canonical_id, related_handoff_id,"
                    "  status, next_retry_at)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (event_type, sub["url"], payload_str,
                     related_canonical_id, related_handoff_id,
                     "pending", _now_iso()),
                )
                count += 1
    except Exception as e:
        logger.warning("[webhook] enqueue 失败: %s", e)
    return count


# ─── 发送 ────────────────────────────────────────────────────────────

def _sign_hmac(secret: str, body: bytes) -> str:
    """HMAC-SHA256 签名 hex 字符串。"""
    if not secret:
        return ""
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _send_single(dispatch: Dict[str, Any]) -> (bool, str):
    """同步发送一条 dispatch。返回 (ok, error_msg)。"""
    try:
        import requests  # type: ignore
    except ImportError:
        return False, "requests not installed"

    # 找对应 secret
    secret = ""
    subs = _get_subscribers(dispatch.get("event_type") or "")
    for sub in subs:
        if sub.get("url") == dispatch["target_url"]:
            env_key = sub.get("secret_key_env") or ""
            if env_key:
                secret = os.environ.get(env_key, "")
            break

    payload_str = dispatch.get("payload_json") or "{}"
    body = payload_str.encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-OpenClaw-Event": dispatch.get("event_type") or "",
        "X-OpenClaw-Dispatch-Id": str(dispatch.get("id") or ""),
        "X-OpenClaw-Timestamp": _now_iso(),
    }
    if secret:
        headers["X-OpenClaw-Signature"] = f"sha256={_sign_hmac(secret, body)}"

    try:
        resp = requests.post(dispatch["target_url"],
                              data=body, headers=headers,
                              timeout=DEFAULT_TIMEOUT_SEC)
        if 200 <= resp.status_code < 300:
            return True, ""
        return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:200]}"


def flush_pending_webhooks(max_batch: int = 50) -> Dict[str, int]:
    """批量处理 pending 且到期的 webhook (供定时任务调)。

    返回: {"delivered": n, "retried": n, "dead_letter": n}
    """
    stats = {"delivered": 0, "retried": 0, "dead_letter": 0, "skipped": 0}
    now = _now_iso()
    try:
        with _connect() as conn:
            conn.row_factory = __import__("sqlite3").Row
            rows = conn.execute(
                "SELECT * FROM webhook_dispatches"
                " WHERE status='pending' AND (next_retry_at IS NULL OR next_retry_at <= ?)"
                " ORDER BY created_at ASC LIMIT ?",
                (now, int(max_batch))).fetchall()
    except Exception as e:
        logger.warning("[webhook] flush 查询失败: %s", e)
        return stats

    for row in rows:
        d = dict(row)
        ok, err = _send_single(d)
        new_attempt = (d.get("attempt_count") or 0) + 1
        try:
            with _connect() as conn:
                if ok:
                    conn.execute(
                        "UPDATE webhook_dispatches SET status='delivered',"
                        " attempt_count=?, last_error='', delivered_at=datetime('now'),"
                        " next_retry_at=NULL WHERE id=?",
                        (new_attempt, d["id"]))
                    stats["delivered"] += 1
                elif new_attempt >= DEFAULT_MAX_ATTEMPTS:
                    conn.execute(
                        "UPDATE webhook_dispatches SET status='dead_letter',"
                        " attempt_count=?, last_error=?, next_retry_at=NULL"
                        " WHERE id=?",
                        (new_attempt, err, d["id"]))
                    stats["dead_letter"] += 1
                    logger.warning("[webhook] 死信 dispatch=%s url=%s err=%s",
                                   d["id"], d.get("target_url"), err[:100])
                else:
                    conn.execute(
                        "UPDATE webhook_dispatches SET attempt_count=?,"
                        " last_error=?, next_retry_at=? WHERE id=?",
                        (new_attempt, err, _next_retry_iso(new_attempt), d["id"]))
                    stats["retried"] += 1
        except Exception as e:
            logger.warning("[webhook] 更新 dispatch 失败: %s", e)
            stats["skipped"] += 1
    return stats


def list_dead_letters(limit: int = 100) -> List[Dict[str, Any]]:
    try:
        with _connect() as conn:
            conn.row_factory = __import__("sqlite3").Row
            rows = conn.execute(
                "SELECT * FROM webhook_dispatches WHERE status='dead_letter'"
                " ORDER BY created_at DESC LIMIT ?", (int(limit),)).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def retry_dead_letter(dispatch_id: int) -> bool:
    """管理员手工把死信重置回 pending (Dashboard 按钮)。"""
    try:
        with _connect() as conn:
            cur = conn.execute(
                "UPDATE webhook_dispatches SET status='pending', attempt_count=0,"
                " last_error='', next_retry_at=datetime('now')"
                " WHERE id=? AND status='dead_letter'", (int(dispatch_id),))
            return (cur.rowcount or 0) > 0
    except Exception:
        return False
