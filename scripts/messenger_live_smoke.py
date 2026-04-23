# -*- coding: utf-8 -*-
"""真机 Messenger smoke (B 侧读取路径验证)。

和 ``messenger_workflow_smoke.py`` 互补:
  * workflow_smoke = 数据层集成测试 (不碰设备)
  * live_smoke = 真机代码跑通性验证 (只读, 不改 FB 状态)

本工具用途:
  * 合入 main 后, 首次连真机时快速验证 B 代码没有硬崩 bug (设备连接 /
    UI 层扫描 / DB 写入闭环)
  * 诊断: 某台设备出问题时用本工具隔离是代码 bug 还是环境问题

**关键约束 — 只读**:
  * ``auto_reply=False`` 强制, 不主动发消息到对方 (避免骚扰真人)
  * ``accept_all=False`` + ``max_requests=0``, 好友请求只列不接受
  * 不触发 send_greeting (A 的范畴, 本工具不碰)
  * DB 会有 incoming 行写入 (check_messenger_inbox 的副作用),
    这是正常行为,用 ``--preset-key live_smoke`` 隔离便于清理

用法:
    # 基础 — 列所有可以测的步骤
    python scripts/messenger_live_smoke.py --list

    # 跑指定步骤
    python scripts/messenger_live_smoke.py --device <did> --step inbox
    python scripts/messenger_live_smoke.py --device <did> --step all

    # 事后清理本次产生的 inbox rows (preset_key=live_smoke)
    python scripts/messenger_live_smoke.py --cleanup
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("live_smoke")


GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
RESET = "\033[0m"
BOLD = "\033[1m"

if os.environ.get("NO_COLOR") or (sys.platform == "win32" and
                                    not os.environ.get("ANSICON")):
    try:
        os.system("")  # enable ANSI on Win10+
    except Exception:
        GREEN = YELLOW = RED = RESET = BOLD = ""


# ─────────────────────────────────────────────────────────────────────────────
# Result
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class LiveStep:
    name: str
    status: str = "NOT_RUN"  # PASS / FAIL / SKIP / NOT_RUN
    reason: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    elapsed_ms: int = 0

    def render(self) -> str:
        color = {"PASS": GREEN, "SKIP": YELLOW,
                 "FAIL": RED, "NOT_RUN": ""}.get(self.status, "")
        tag = f"{color}[{self.status:6}]{RESET}"
        detail = f" ({self.reason})" if self.reason else ""
        elap = f"  [{self.elapsed_ms}ms]" if self.elapsed_ms else ""
        data = ""
        if self.data:
            # 只打印 scalar + 短 list 值
            kv_parts = []
            for k, v in self.data.items():
                if isinstance(v, (int, float, str, bool)) or v is None:
                    kv_parts.append(f"{k}={v}")
                elif isinstance(v, (list, tuple)) and len(v) <= 5:
                    kv_parts.append(f"{k}={list(v)}")
                else:
                    kv_parts.append(f"{k}=(...)")
            data = "\n    → " + ", ".join(kv_parts)
        return f"{tag} {self.name}{detail}{elap}{data}"


PRESET_KEY = "live_smoke"


# ─────────────────────────────────────────────────────────────────────────────
# 各步骤 (只读)
# ─────────────────────────────────────────────────────────────────────────────

def step_device_reachable(device_id: str) -> LiveStep:
    """验 adb 能连设备。不动设备状态。"""
    s = LiveStep("device_reachable")
    t0 = time.time()
    try:
        import subprocess
        r = subprocess.run(
            ["adb", "-s", device_id, "shell", "echo", "OK"],
            capture_output=True, text=True, timeout=15,
        )
        s.elapsed_ms = int((time.time() - t0) * 1000)
        if r.returncode == 0 and "OK" in r.stdout:
            s.status = "PASS"
            s.data["adb_stdout"] = r.stdout.strip()
        else:
            s.status = "FAIL"
            s.reason = f"rc={r.returncode} err={r.stderr[:80]}"
    except FileNotFoundError:
        s.status = "SKIP"
        s.reason = "adb not on PATH"
    except Exception as e:
        s.status = "FAIL"
        s.reason = str(e)[:80]
    return s


def step_fb_automation_init(device_id: str) -> LiveStep:
    """实例化 FacebookAutomation, 验 class 能构造。"""
    s = LiveStep("fb_automation_init")
    t0 = time.time()
    try:
        from src.app_automation.facebook import FacebookAutomation
        # 不传 device_manager, 默认会取 get_device_manager()
        fb = FacebookAutomation()
        s.data["class"] = type(fb).__name__
        s.data["platform"] = getattr(fb, "PLATFORM", "")
        s.status = "PASS"
    except Exception as e:
        s.status = "FAIL"
        s.reason = str(e)[:120]
    s.elapsed_ms = int((time.time() - t0) * 1000)
    return s


def step_list_messenger_conversations(device_id: str) -> LiveStep:
    """调 _list_messenger_conversations 读取可见对话 (不进入)。"""
    s = LiveStep("list_messenger_conversations")
    t0 = time.time()
    try:
        from src.app_automation.facebook import FacebookAutomation
        fb = FacebookAutomation()
        d = fb._u2(device_id)
        convs = fb._list_messenger_conversations(d, max_n=5)
        s.data["found"] = len(convs)
        s.data["first_names"] = [c.get("name", "") for c in convs[:3]]
        s.status = "PASS"
    except Exception as e:
        s.status = "FAIL"
        s.reason = str(e)[:120]
    s.elapsed_ms = int((time.time() - t0) * 1000)
    return s


def step_list_friend_requests(device_id: str) -> LiveStep:
    """只读: check_friend_requests_inbox(max_requests=0) 列出不接受。"""
    s = LiveStep("list_friend_requests")
    t0 = time.time()
    try:
        from src.app_automation.facebook import FacebookAutomation
        fb = FacebookAutomation()
        stats = fb.check_friend_requests_inbox(
            accept_all=False, safe_accept=True,
            max_requests=0,  # accept_quota = max(1, 0//2) = 1, 但接受前先
                             # 检查 requests_seen, 以 max_requests 拉取
                             # 列表. 这里其实会拉 0 条, 验入口即可
            min_mutual_friends=99,  # 强制 gate skip 保护
            device_id=device_id,
        )
        s.data["opened"] = stats.get("opened", False)
        s.data["requests_seen"] = stats.get("requests_seen", 0)
        s.data["accepted"] = stats.get("accepted", 0)
        s.data["error"] = stats.get("error", "")
        if stats.get("accepted", 0) > 0:
            s.status = "FAIL"
            s.reason = "意外接受了好友请求,安全约束失效"
        else:
            s.status = "PASS"
    except Exception as e:
        s.status = "FAIL"
        s.reason = str(e)[:120]
    s.elapsed_ms = int((time.time() - t0) * 1000)
    return s


def step_check_messenger_inbox_readonly(device_id: str) -> LiveStep:
    """只读: check_messenger_inbox(auto_reply=False, max_conversations=2)。

    会写 incoming 行到 facebook_inbox_messages (副作用, 用 preset_key
    隔离便于清理)。**不主动回复**。
    """
    s = LiveStep("check_messenger_inbox_readonly")
    t0 = time.time()
    try:
        from src.app_automation.facebook import FacebookAutomation
        fb = FacebookAutomation()
        stats = fb.check_messenger_inbox(
            auto_reply=False,
            max_conversations=2,
            preset_key=PRESET_KEY,
            device_id=device_id,
        )
        s.data["opened"] = stats.get("opened", False)
        s.data["conversations_listed"] = stats.get("conversations_listed", 0)
        s.data["unread_processed"] = stats.get("unread_processed", 0)
        s.data["replied"] = stats.get("replied", 0)
        s.data["errors"] = stats.get("errors", 0)
        if stats.get("replied", 0) > 0:
            s.status = "FAIL"
            s.reason = "auto_reply=False 但 replied > 0, 契约失效"
        elif stats.get("lock_timeout"):
            s.status = "SKIP"
            s.reason = "device 被其他进程占用 (messenger_active lock 超时)"
        else:
            s.status = "PASS"
    except Exception as e:
        s.status = "FAIL"
        s.reason = str(e)[:120]
    s.elapsed_ms = int((time.time() - t0) * 1000)
    return s


def step_funnel_metrics_snapshot(device_id: str) -> LiveStep:
    """漏斗指标快照 — 纯读 DB, 无副作用。"""
    s = LiveStep("funnel_metrics_snapshot")
    t0 = time.time()
    try:
        from src.host.fb_store import get_funnel_metrics
        m = get_funnel_metrics(device_id=device_id)
        s.data = {k: v for k, v in m.items()
                  if str(k).startswith("stage_")
                  and isinstance(v, (int, float))}
        s.status = "PASS"
    except Exception as e:
        s.status = "FAIL"
        s.reason = str(e)[:120]
    s.elapsed_ms = int((time.time() - t0) * 1000)
    return s


def step_extended_funnel(device_id: str) -> LiveStep:
    """P8/P9 扩展漏斗快照。"""
    s = LiveStep("extended_funnel_snapshot")
    t0 = time.time()
    try:
        from src.analytics.chat_funnel import get_funnel_metrics_extended
        m = get_funnel_metrics_extended(
            device_id=device_id,
            include_intent_coverage=True,
            include_greeting_template=True,
        )
        s.data["reply_rate_by_intent_count"] = len(
            m.get("reply_rate_by_intent", {}).get("by_intent", {}))
        s.data["stranger_peers"] = m.get("stranger_conversion_rate", {}).get(
            "stranger_peers", 0)
        health = m.get("intent_health", {})
        s.data["intent_health"] = health.get("health", "unknown")
        s.data["rule_coverage"] = health.get("rule_coverage", 0.0)
        s.status = "PASS"
    except Exception as e:
        s.status = "FAIL"
        s.reason = str(e)[:120]
    s.elapsed_ms = int((time.time() - t0) * 1000)
    return s


# ─────────────────────────────────────────────────────────────────────────────
# Step registry
# ─────────────────────────────────────────────────────────────────────────────

STEPS: List[Callable[[str], LiveStep]] = [
    step_device_reachable,
    step_fb_automation_init,
    step_list_messenger_conversations,
    step_list_friend_requests,
    step_check_messenger_inbox_readonly,
    step_funnel_metrics_snapshot,
    step_extended_funnel,
]

STEP_BY_KEY = {
    "adb": step_device_reachable,
    "init": step_fb_automation_init,
    "conversations": step_list_messenger_conversations,
    "friend_requests": step_list_friend_requests,
    "inbox": step_check_messenger_inbox_readonly,
    "funnel": step_funnel_metrics_snapshot,
    "extended_funnel": step_extended_funnel,
}


# ─────────────────────────────────────────────────────────────────────────────
# Cleanup
# ─────────────────────────────────────────────────────────────────────────────

def cleanup_smoke_rows() -> int:
    """删除 preset_key=PRESET_KEY 的 inbox 行 (只删本工具写的)。"""
    try:
        from src.host.database import _connect
    except Exception:
        log.error("无法 import fb DB, 跳过清理")
        return 0
    try:
        with _connect() as conn:
            cur = conn.execute(
                "DELETE FROM facebook_inbox_messages WHERE preset_key=?",
                (PRESET_KEY,),
            )
            n = cur.rowcount or 0
    except Exception as e:
        log.error("清理失败: %s", e)
        return 0
    log.info("删除 %d 条 preset_key=%s 的 inbox 行", n, PRESET_KEY)
    return n


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Messenger 真机 live smoke (B 读取路径只读验证)")
    parser.add_argument("--list", action="store_true",
                        help="列出所有可用 step")
    parser.add_argument("--device", "-d", type=str,
                        help="adb device_id (必需, 除非 --list 或 --cleanup)")
    parser.add_argument("--step", "-s", type=str, default="all",
                        help="step key (adb/init/conversations/friend_requests/"
                             "inbox/funnel/extended_funnel), 或 'all' 跑全部")
    parser.add_argument("--cleanup", action="store_true",
                        help=f"删 preset_key={PRESET_KEY} 的 inbox 行")
    parser.add_argument("--no-color", action="store_true")
    args = parser.parse_args()

    if args.no_color:
        global GREEN, YELLOW, RED, RESET, BOLD
        GREEN = YELLOW = RED = RESET = BOLD = ""

    if args.list:
        print(f"{BOLD}Available steps:{RESET}")
        for k in STEP_BY_KEY:
            print(f"  - {k}")
        print("  - all (跑全部)")
        return 0

    if args.cleanup:
        cleanup_smoke_rows()
        return 0

    if not args.device:
        parser.error("--device 必需 (或用 --list / --cleanup)")
    if args.step != "all" and args.step not in STEP_BY_KEY:
        parser.error(f"未知 step: {args.step}")

    steps_to_run = list(STEPS) if args.step == "all" else [STEP_BY_KEY[args.step]]

    print(f"\n{BOLD}=== Messenger Live Smoke ==={RESET}")
    print(f"device={args.device}  steps={len(steps_to_run)}\n")

    results: List[LiveStep] = []
    for fn in steps_to_run:
        r = fn(args.device)
        results.append(r)
        print(r.render())

    by_status: Dict[str, int] = {}
    for r in results:
        by_status[r.status] = by_status.get(r.status, 0) + 1

    print(f"\n{BOLD}=== Summary ==={RESET}")
    for status in ("PASS", "SKIP", "FAIL", "NOT_RUN"):
        n = by_status.get(status, 0)
        if n == 0:
            continue
        color = {"PASS": GREEN, "SKIP": YELLOW,
                 "FAIL": RED}.get(status, "")
        print(f"  {color}{status}{RESET}: {n}")

    return 1 if by_status.get("FAIL", 0) > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
