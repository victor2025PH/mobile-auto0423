# -*- coding: utf-8 -*-
"""
智能调度引擎 — 基于健康评分 + 负载 + 任务亲和度的动态任务分配。

调度评分 = health_score * 0.40 + load_score * 0.35 + affinity_score * 0.25

健康评分: 直接使用 HealthMonitor 的综合评分 (0-100)
负载评分: 空闲=100, 1个任务=60, 2+=30, 设备锁定=0
亲和度:   该设备执行过同类任务的成功率 → 加权
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_WEIGHT_HEALTH = 0.40
_WEIGHT_LOAD = 0.35
_WEIGHT_AFFINITY = 0.25


class SmartScheduler:
    def __init__(self):
        self._lock = threading.Lock()
        self._task_history: Dict[str, Dict[str, List[bool]]] = defaultdict(
            lambda: defaultdict(list)
        )

    def record_task_result(self, device_id: str, task_type: str,
                           success: bool):
        """Record task result for affinity learning."""
        prefix = self._task_prefix(task_type)
        with self._lock:
            hist = self._task_history[device_id][prefix]
            hist.append(success)
            if len(hist) > 50:
                self._task_history[device_id][prefix] = hist[-25:]

    def select_device(self, task_type: str,
                      preferred: Optional[str] = None,
                      exclude: Optional[List[str]] = None) -> Optional[str]:
        """
        Select the best device for a task based on health, load, and affinity.

        Returns device_id or None if no devices available.
        """
        from .health_monitor import metrics
        from .worker_pool import get_worker_pool

        pool = get_worker_pool()
        candidates = []

        vpn_paused = set()
        try:
            from src.behavior.vpn_health import get_vpn_health_monitor
            vpn_mon = get_vpn_health_monitor()
            for did_check, st in vpn_mon.get_status().items():
                if st.get("paused"):
                    vpn_paused.add(did_check)
        except Exception:
            pass

        for did, status in metrics.device_status.items():
            if status.get("status") != "connected":
                continue
            if metrics.is_isolated(did):
                continue
            if did in vpn_paused:
                continue
            if exclude and did in exclude:
                continue
            candidates.append(did)

        if not candidates:
            return preferred

        if preferred and preferred in candidates:
            return preferred

        scores = {}
        for did in candidates:
            health = metrics.device_health_score(did).get("total", 50)
            load = self._load_score(pool, did)
            affinity = self._affinity_score(did, task_type)

            total = (health * _WEIGHT_HEALTH
                     + load * _WEIGHT_LOAD
                     + affinity * _WEIGHT_AFFINITY)
            scores[did] = {
                "total": round(total, 1),
                "health": health,
                "load": load,
                "affinity": affinity,
            }

        best = max(scores, key=lambda d: scores[d]["total"])
        logger.debug("智能调度: task=%s → 选择 %s (%.1f) 候选=%d",
                     task_type, best[:8], scores[best]["total"],
                     len(candidates))
        return best

    def get_scheduling_scores(self, task_type: str = "") -> Dict[str, dict]:
        """Return scheduling scores for all devices (for dashboard)."""
        from .health_monitor import metrics
        from .worker_pool import get_worker_pool

        pool = get_worker_pool()
        result = {}

        for did in metrics.device_status:
            health = metrics.device_health_score(did).get("total", 50)
            load = self._load_score(pool, did)
            affinity = self._affinity_score(did, task_type) if task_type else 70
            total = (health * _WEIGHT_HEALTH
                     + load * _WEIGHT_LOAD
                     + affinity * _WEIGHT_AFFINITY)
            result[did] = {
                "total": round(total, 1),
                "health": health,
                "load": load,
                "affinity": affinity,
                "busy": pool.is_device_busy(did),
                "isolated": metrics.is_isolated(did),
            }
        return result

    def _load_score(self, pool, device_id: str) -> int:
        """Score based on current task load: idle=100, busy=30."""
        status = pool.get_status()
        active_map = status.get("active_tasks", {})
        running_on = sum(1 for did in active_map.values() if did == device_id)
        device_locks = status.get("device_locks", {})
        is_locked = device_locks.get(device_id) == "busy"

        if is_locked:
            return 10
        if running_on >= 2:
            return 20
        if running_on == 1:
            return 50
        return 100

    def _affinity_score(self, device_id: str, task_type: str) -> int:
        """Score based on historical success rate for this task type."""
        prefix = self._task_prefix(task_type)
        with self._lock:
            hist = self._task_history.get(device_id, {}).get(prefix, [])

        if not hist:
            return 70

        recent = hist[-10:]
        success_rate = sum(1 for r in recent if r) / len(recent)
        return int(50 + success_rate * 50)

    @staticmethod
    def _task_prefix(task_type: str) -> str:
        if "_" in task_type:
            return task_type.split("_")[0] + "_"
        return task_type


_scheduler: Optional[SmartScheduler] = None
_scheduler_lock = threading.Lock()


def get_smart_scheduler() -> SmartScheduler:
    global _scheduler
    if _scheduler is None:
        with _scheduler_lock:
            if _scheduler is None:
                _scheduler = SmartScheduler()
    return _scheduler
