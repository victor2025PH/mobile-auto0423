# -*- coding: utf-8 -*-
"""OPT-5 v3 (2026-04-28) — Messenger startup dialog 通用 dismiss 模块.

Messenger 启动后常出现各种对话框, 阻塞业务流程. 现有 facebook.py::
_dismiss_dialogs 处理**通用文本按钮** (Not Now / OK / Skip / 等),
本模块处理**特定 startup dialog** (语言不支持 / Previews are on /
通知请求 / e2e 加密介绍 / restriction page 等), 两者互补.

数据驱动设计: KNOWN_DIALOGS list 维护 (marker, action, tap_target),
新增/调整 dialog 不需改流程, 改字典即可.

action 枚举:
  - tap_text  → tap text=tap_target 的按钮
  - tap_desc  → tap content-desc=tap_target 的按钮
  - skip      → 识别但不 dismiss (例如 restriction_page 留给 OPT-4 处理)

Usage:
    from src.app_automation.fb_dialog_dismisser import dismiss_known_dialogs
    cleared = dismiss_known_dialogs(d, max_rounds=5)
    # cleared = ["language_not_supported", "previews_on", ...]

设计原则:
  - skip 也算 cleared (告诉 caller "我识别了, 但故意没 dismiss")
  - 异常吞: u2 hiccup 不应阻断业务流程
  - max_rounds 防 dialog 嵌套死循环
  - 每轮无 dialog 命中即 break (避免空跑)
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════════════════
# 已知 dialog 字典 (按优先级排, 罕见在前以便快速 fall-through 到 break)
# ════════════════════════════════════════════════════════════════════════

KNOWN_DIALOGS: List[Dict[str, Any]] = [
    # ── 必须 SKIP — restriction page 留给 _detect_risk_dialog ──
    # 这条放最前, 否则后续可能误命中 e2e_intro 导致漏识别
    {
        "name": "restriction_page",
        "marker": "Your account has been restricted",
        "marker_kind": "textContains",
        "action": "skip",
        "wait_after_s": 0.0,
        "comment": "OPT-4 路径处理, 不 dismiss 否则 _detect_risk_dialog 检测不到",
    },

    # ── 中文(中国)区 Messenger 不支持 (Q4N7 摸到) ──
    {
        "name": "language_not_supported",
        "marker": "继续使用美式英语",
        "marker_kind": "text",
        "action": "tap_text",
        "tap_target": "继续使用美式英语",
        "wait_after_s": 3.0,
        "comment": "中国区 Messenger UI 不可用, 切美式英语",
    },

    # ── Previews are on bottom sheet (Q4N7 摸到) ──
    {
        "name": "previews_on",
        "marker": "Previews are on",
        "marker_kind": "textContains",
        "action": "tap_desc",
        "tap_target": "Click to dismiss this bottom sheet",
        "wait_after_s": 1.5,
        "comment": "chat heads 推荐 sheet, dismiss 不影响业务",
    },

    # ── 通知权限请求 (反追踪 — 默认拒绝) ──
    {
        "name": "notification_permission",
        "marker": "Allow Messenger to send you notifications",
        "marker_kind": "textContains",
        "action": "tap_text",
        "tap_target": "Don't allow",
        "wait_after_s": 1.0,
        "comment": "反追踪策略 — 不接收 push notification",
    },

    # ── 通讯录权限请求 ──
    {
        "name": "contacts_permission",
        "marker": "Allow Messenger to access your contacts",
        "marker_kind": "textContains",
        "action": "tap_text",
        "tap_target": "Don't allow",
        "wait_after_s": 1.0,
        "comment": "反追踪 — 不上传通讯录",
    },

    # ── e2e 加密介绍 (FB 一次性 educational popup) ──
    # 自动消失或 inline, 无独立 dismiss button. 识别后 skip 让其自然过去
    {
        "name": "e2e_encryption_intro",
        "marker": "Messages and calls are secured with end-to-end encryption",
        "marker_kind": "textContains",
        "action": "skip",
        "wait_after_s": 0.0,
        "comment": "Inline 提示, 无 dismiss button, 自然过去",
    },
]


# ════════════════════════════════════════════════════════════════════════
# 主接口
# ════════════════════════════════════════════════════════════════════════

def _check_marker(d, marker: str, marker_kind: str,
                  timeout: float = 0.4) -> bool:
    """检查 d hierarchy 中是否含 marker (textContains/text)。

    fail-safe 异常吞返 False — u2 hiccup 不应阻断 dismiss 流程。
    """
    try:
        if marker_kind == "textContains":
            return d(textContains=marker).exists(timeout=timeout)
        if marker_kind == "text":
            return d(text=marker).exists(timeout=timeout)
        if marker_kind == "descContains":
            return d(descriptionContains=marker).exists(timeout=timeout)
        if marker_kind == "desc":
            return d(description=marker).exists(timeout=timeout)
    except Exception as e:
        logger.debug("[opt5v3] _check_marker 异常 (%s=%r): %s",
                     marker_kind, marker, e)
    return False


def _do_action(d, action: str, tap_target: str) -> bool:
    """执行 action: tap_text / tap_desc / skip. Returns True 已执行 (含 skip)."""
    if action == "skip":
        return True
    try:
        if action == "tap_text":
            obj = d(text=tap_target)
            if obj.exists(timeout=0.3):
                obj.click()
                return True
        elif action == "tap_desc":
            obj = d(description=tap_target)
            if obj.exists(timeout=0.3):
                obj.click()
                return True
    except Exception as e:
        logger.debug("[opt5v3] _do_action 异常 (action=%s target=%r): %s",
                     action, tap_target, e)
    return False


def dismiss_known_dialogs(d, max_rounds: int = 5) -> List[str]:
    """循环识别 + dismiss/skip 已知 startup dialog.

    Args:
        d: uiautomator2 device (或 mock)
        max_rounds: 最多循环轮数 (防 dialog 嵌套死循环)

    Returns:
        本次 cleared 的 dialog name list (dedup, 按发现顺序). skip 也算
        cleared (caller 知道 "我识别了"). 全空时表示 startup 已干净。
    """
    cleared: List[str] = []
    seen: set = set()

    for round_idx in range(max_rounds):
        any_hit = False
        for dialog in KNOWN_DIALOGS:
            if dialog["name"] in seen:
                # 已 cleared (含 skip / tap 已成功) 不重复检查
                # 真机场景: tap 后 UI 变化, marker 真消失, 下轮不会命中
                # mock 场景: marker 不变, 必须靠 seen dedup 防死循环 +
                # 让后续 dialog 有机会被检查
                continue
            try:
                hit = _check_marker(
                    d, dialog["marker"], dialog["marker_kind"])
            except Exception:
                hit = False
            if not hit:
                continue
            any_hit = True
            action = dialog["action"]
            tap_target = dialog.get("tap_target", "")
            done = _do_action(d, action, tap_target)
            if done:
                if dialog["name"] not in seen:
                    cleared.append(dialog["name"])
                    seen.add(dialog["name"])
                logger.info(
                    "[opt5v3] dialog cleared: name=%s action=%s round=%d",
                    dialog["name"], action, round_idx)
                wait_s = dialog.get("wait_after_s", 0.5)
                if wait_s > 0 and action != "skip":
                    time.sleep(wait_s)
                # 同一轮里 dismiss 一个就 re-loop 全字典 (UI 可能变化)
                break
            else:
                logger.debug(
                    "[opt5v3] marker hit but action failed: name=%s",
                    dialog["name"])
        if not any_hit:
            break  # 整轮无命中 → 启动已干净

    return cleared
