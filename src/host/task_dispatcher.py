# -*- coding: utf-8 -*-
"""任务派发工具 - 统一的 "create_task 之后如何让它真正跑起来" 的入口。

背景：
    历史上 routers/tasks.py 的 POST /tasks 里内联写了一段"先问集群 Worker，
    失败再本机 WorkerPool.submit"的逻辑。其他入口（AI 快捷指令、风控降级等）
    经常漏掉这一段，只 create_task 不 submit，任务就卡在 pending 了。
    本模块把派发逻辑抽出来，供所有入口复用。

    同时提供 pending_rescue_loop：在 host 进程启动时注册的后台扫描器，
    定期把"写进 DB 却没被线程池登记"的 pending 任务重新补一遍 submit，
    兜住 get_retry_ready_tasks 无调用方、进程重启后内存队列丢失等情况。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

# pending_rescue_loop 默认轮询间隔（秒）
_RESCUE_INTERVAL_SEC = 15
# 多久未更新的 pending 才判定为"僵尸"（避免抢走刚 create 还没 submit 的任务）
_ORPHAN_AGE_SEC = 120
# 每轮最多补派的任务数，避免洪水
_RESCUE_BATCH_LIMIT = 100

_rescue_thread: Optional[threading.Thread] = None
_rescue_stop = threading.Event()


def dispatch_after_create(
    task_id: str,
    device_id: Optional[str],
    task_type: str,
    params: dict | None = None,
    priority: int = 50,
) -> dict:
    """把刚 create_task 出来的任务真正送上线程池。

    流程和 POST /tasks 完全一致：
      1. 若设备在集群 Worker 上 → HTTP 转发到 Worker 的 /tasks
      2. 否则 → 本机 WorkerPool.submit(run_task, ...)

    返回 {"dispatched": bool, "mode": "worker"/"local"/"skip",
          "worker_ip": Optional[str], "reason": Optional[str]}，
    出错不抛异常，只记录日志，调用方可以据此决定是否重试。
    """
    result: dict[str, Any] = {
        "dispatched": False,
        "mode": "skip",
        "worker_ip": None,
        "reason": None,
    }

    if not task_id:
        result["reason"] = "empty_task_id"
        return result

    params = params or {}

    # 1) 集群路由
    if device_id:
        try:
            from .routers.cluster import _get_best_worker_url
            worker = _get_best_worker_url(device_id)
        except Exception as err:
            worker = None
            logger.debug("[dispatch] 查 worker 失败 task=%s: %s", task_id[:8], err)
        if worker:
            try:
                import urllib.request as _ur
                import json as _json
                url = f"http://{worker['ip']}:{worker['port']}/tasks"
                payload = _json.dumps({
                    "type": task_type,
                    "device_id": device_id,
                    "params": params,
                    "created_via": params.get("_created_via"),
                }).encode()
                req = _ur.Request(
                    url, data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                resp = _ur.urlopen(req, timeout=10)
                remote = _json.loads(resp.read().decode())
                from . import task_store
                # Phase-13 fix: 用 set_task_running (task_store 没 update_task)
                task_store.set_task_running(task_id)
                logger.info(
                    "[dispatch] task=%s → worker %s (remote_id=%s)",
                    task_id[:8], worker["ip"],
                    str(remote.get("task_id", ""))[:8],
                )
                result.update(dispatched=True, mode="worker",
                              worker_ip=worker["ip"],
                              remote_task_id=remote.get("task_id", ""))
                return result
            except Exception as err:
                logger.info("[dispatch] 集群派发失败 task=%s，回落本机: %s",
                            task_id[:8], err)

    # 2) 本机 WorkerPool.submit
    try:
        from src.device_control.device_manager import get_device_manager
        from .executor import _get_device_id, run_task
        from .worker_pool import get_worker_pool

        from .device_registry import DEFAULT_DEVICES_YAML

        config_path = DEFAULT_DEVICES_YAML
        manager = get_device_manager(config_path)
        try:
            manager.discover_devices()
        except Exception:
            pass
        resolved = _get_device_id(manager, device_id, config_path) if device_id else None
        device_for_lock = resolved or device_id or "default"
        pool = get_worker_pool()
        ok = pool.submit(task_id, device_for_lock, run_task, task_id, config_path,
                         priority=priority)
        if ok:
            result.update(dispatched=True, mode="local")
            logger.info("[dispatch] task=%s → local pool device=%s",
                        task_id[:8], device_for_lock[:12] if device_for_lock else "?")
        else:
            result["reason"] = "worker_pool_rejected"
            logger.warning("[dispatch] WorkerPool 拒收 task=%s（可能高风险或池已关闭）",
                           task_id[:8])
    except Exception as err:
        result["reason"] = f"local_submit_error: {err}"
        logger.exception("[dispatch] 本机派发异常 task=%s: %s", task_id[:8], err)
    return result


# ---------------------------------------------------------------------------
# pending 救援循环
# ---------------------------------------------------------------------------


def _iso_to_dt(s: str | None) -> Optional[datetime]:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _load_orphan_pending(limit: int) -> list[dict]:
    """扫"pending 但既不是刚创建也不是线程池在跑"的任务。

    判定条件：
      - status='pending' 且未删除
      - updated_at 距今 >= _ORPHAN_AGE_SEC（避开刚创建未 submit 的窗口）
      - 不在 WorkerPool._futures 里
    """
    try:
        from .database import get_conn
        from .task_store import _alive_sql
        from .worker_pool import get_worker_pool
    except Exception as err:
        logger.debug("[rescue] 依赖加载失败: %s", err)
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(seconds=_ORPHAN_AGE_SEC)
    cutoff_iso = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT task_id, type, device_id, params, priority, retry_count, "
            f"next_retry_at, created_at, updated_at "
            f"FROM tasks WHERE status='pending' AND {_alive_sql()} "
            f"AND (updated_at IS NULL OR updated_at <= ?) "
            f"ORDER BY priority DESC, created_at ASC LIMIT ?",
            (cutoff_iso, limit),
        ).fetchall()

    pool = get_worker_pool()
    inflight = set(getattr(pool, "_futures", {}).keys()) | set(
        getattr(pool, "_cancel_flags", {}).keys()
    )
    now = datetime.now(timezone.utc)

    out: list[dict] = []
    for row in rows:
        task_id = row["task_id"]
        if task_id in inflight:
            continue
        next_retry = _iso_to_dt(row["next_retry_at"])
        if next_retry and next_retry > now:
            continue
        try:
            params = json.loads(row["params"] or "{}")
        except Exception:
            params = {}
        if params.get("run_on_host") is False:
            continue
        out.append({
            "task_id": task_id,
            "type": row["type"],
            "device_id": row["device_id"],
            "params": params,
            "priority": row["priority"] or 50,
        })
    return out


def _rescue_once() -> tuple[int, int]:
    """跑一轮救援。返回 (scanned, resubmit)."""
    scanned = 0
    resubmit = 0

    try:
        from . import task_store
        retry_ready = task_store.get_retry_ready_tasks(limit=_RESCUE_BATCH_LIMIT)
    except Exception as err:
        logger.debug("[rescue] get_retry_ready_tasks 失败: %s", err)
        retry_ready = []

    orphans = _load_orphan_pending(limit=_RESCUE_BATCH_LIMIT)

    all_candidates: dict[str, dict] = {}
    for t in retry_ready:
        all_candidates[t["task_id"]] = t
    for t in orphans:
        all_candidates.setdefault(t["task_id"], t)

    for task_id, t in all_candidates.items():
        scanned += 1
        r = dispatch_after_create(
            task_id=task_id,
            device_id=t.get("device_id"),
            task_type=t.get("type") or "",
            params=t.get("params") or {},
            priority=int(t.get("priority") or 50),
        )
        if r.get("dispatched"):
            resubmit += 1

    return scanned, resubmit


def _rescue_loop():
    logger.info("[rescue] pending_rescue_loop 启动，间隔 %ds，批量 %d，孤儿阈值 %ds",
                _RESCUE_INTERVAL_SEC, _RESCUE_BATCH_LIMIT, _ORPHAN_AGE_SEC)
    while not _rescue_stop.is_set():
        start_ts = time.time()
        try:
            scanned, resubmit = _rescue_once()
            if scanned or resubmit:
                logger.info("[rescue] scanned=%d resubmit=%d cost=%.2fs",
                            scanned, resubmit, time.time() - start_ts)
            else:
                logger.debug("[rescue] idle (scanned=0)")
        except Exception as err:
            logger.exception("[rescue] 轮次异常: %s", err)
        if _rescue_stop.wait(_RESCUE_INTERVAL_SEC):
            break
    logger.info("[rescue] pending_rescue_loop 已退出")


def start_pending_rescue_loop() -> bool:
    """幂等启动后台救援线程。已启动返回 False。"""
    global _rescue_thread
    if _rescue_thread and _rescue_thread.is_alive():
        return False
    _rescue_stop.clear()
    _rescue_thread = threading.Thread(
        target=_rescue_loop, daemon=True, name="pending-rescue-loop"
    )
    _rescue_thread.start()
    return True


def stop_pending_rescue_loop() -> None:
    _rescue_stop.set()
