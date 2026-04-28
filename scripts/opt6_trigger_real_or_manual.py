# -*- coding: utf-8 -*-
"""OPT-6 手工触发工具 — 让调度器立即避开指定 restricted 设备。

用途：
  - 真机已撞 Community Standards 风控但未通过 _detect_risk_dialog 完整链路
    标记 (例如 SWZL 当前可能在 inbox 而非 restriction page) → 用本工具
    立即写入 device_state, 调度器下次执行任务时就避开。
  - PR #140 merge 后的 D2 灰度上线前的"现存风控设备保险"。

策略 (auto 模式默认):
  1. 启动 Messenger
  2. dump UI 看是否在 restriction page
  3a. 是 → 构造 FacebookAutomation + 调 _detect_risk_dialog 走全链路
       (OPT-4 识别 + OPT-6 写状态, 真机端到端)
  3b. 否 → 回退 manual 模式: 直接调 _mark_account_restricted_state(...)
       写 days 参数指定的天数
  4. 验证: _is_account_restricted + executor._opt6_check_restriction
     都返 True
  5. 输出 markdown 报告

用法:
  python scripts/opt6_trigger_real_or_manual.py
  python scripts/opt6_trigger_real_or_manual.py --device <serial> --days 6
  python scripts/opt6_trigger_real_or_manual.py --mode manual --days 6
  python scripts/opt6_trigger_real_or_manual.py --mode verify

退出码: 0 全过, 1 触发失败, 2 verify 不一致 (写入了但读不到)
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime

# 修 Windows GBK 控制台中文乱码
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

DEFAULT_DEVICE = "SWZLPNYTROMZMJLR"  # 主控-06, 当前 6 天 restriction
DEFAULT_DAYS = 6
OUT_DIR = r"D:\workspace\rpa-debug-2026-04-28"


def adb(device, *args, timeout=15):
    cmd = ["adb", "-s", device] + list(args)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout)
        return r.returncode == 0, (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return False, f"exception: {e}"


def step(label):
    print(f"\n{'=' * 60}\n  {label}\n{'=' * 60}", flush=True)


def is_in_restriction_page(device):
    """dump UI 检查是否在 restriction page (含 'Your account has been
    restricted for X days')。"""
    ts = int(time.time())
    remote = f"/sdcard/_opt6_dump_{ts}.xml"
    local = os.path.join(OUT_DIR, f"opt6_check_{device}_{ts}.xml")
    adb(device, "shell", "uiautomator", "dump", remote)
    adb(device, "pull", remote, local)
    if not os.path.isfile(local):
        return False, ""
    try:
        with open(local, encoding="utf-8", errors="replace") as f:
            xml = f.read()
    except Exception:
        return False, ""
    return ("Your account has been restricted" in xml), local


def trigger_real_detect(device):
    """构造 FacebookAutomation + 调 _detect_risk_dialog 走全链路。"""
    try:
        from src.app_automation.facebook import FacebookAutomation
        from src.device_control.device_manager import get_device_manager
    except ImportError as e:
        return False, f"import: {e}"

    dm = get_device_manager()
    fb = FacebookAutomation(device_manager=dm)
    d = fb._u2(device)
    if d is None:
        return False, "u2 device not available"
    try:
        is_risk, msg = fb._detect_risk_dialog(d)
    except Exception as e:
        return False, f"_detect_risk_dialog 抛 {type(e).__name__}: {e}"
    return is_risk, msg


def trigger_manual_mark(device, days):
    """跳过 _detect_risk_dialog, 直接调 _mark_account_restricted_state 写
    device_state (用于真机不在 restriction page 时的兜底)。"""
    try:
        from src.app_automation.facebook import FacebookAutomation
        from src.device_control.device_manager import get_device_manager
    except ImportError as e:
        return False, f"import: {e}"

    dm = get_device_manager()
    fb = FacebookAutomation(device_manager=dm)
    full_msg = (f"Your account has been restricted for {days} days "
                f"(manually marked by opt6_trigger script "
                f"@ {datetime.now().isoformat()})")
    try:
        fb._mark_account_restricted_state(device, full_msg, days)
    except Exception as e:
        return False, f"_mark_account_restricted_state 抛 {type(e).__name__}: {e}"
    return True, full_msg


def verify_state(device):
    """读 device_state + 跑 _is_account_restricted +
    _opt6_check_restriction, 输出验证报告。"""
    from src.app_automation.facebook import FacebookAutomation
    from src.device_control.device_manager import get_device_manager
    from src.host.device_state import DeviceStateStore
    from src.host.executor import _opt6_check_restriction

    ds = DeviceStateStore(platform="facebook")
    fields = {}
    for key in ("restriction_lifted_at", "restriction_full_msg",
                "restriction_days", "restriction_detected_at"):
        fields[key] = ds.get(device, key, "")

    dm = get_device_manager()
    fb = FacebookAutomation(device_manager=dm)
    is_r, lifted_at = fb._is_account_restricted(device)
    skip, reason = _opt6_check_restriction(device)

    return {
        "device_state_fields": fields,
        "is_account_restricted": is_r,
        "lifted_at_ts": lifted_at,
        "lifted_at_iso": (datetime.fromtimestamp(lifted_at).isoformat()
                          if lifted_at > 0 else ""),
        "remaining_days": ((lifted_at - time.time()) / 86400
                           if lifted_at > time.time() else 0.0),
        "executor_skip": skip,
        "executor_reason": reason,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default=DEFAULT_DEVICE)
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS)
    ap.add_argument("--mode", choices=["auto", "manual", "verify"],
                    default="auto")
    args = ap.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)

    print(f"=== OPT-6 手工触发工具 ===")
    print(f"  device:  {args.device}")
    print(f"  days:    {args.days}")
    print(f"  mode:    {args.mode}")

    # ── verify-only 模式 ──
    if args.mode == "verify":
        step(f"Verify only: 读取 {args.device} 当前 OPT-6 状态")
        v = verify_state(args.device)
        for k, val in v.items():
            print(f"  {k}: {val}")
        return 0 if v["is_account_restricted"] else 2

    # ── manual 模式 ──
    if args.mode == "manual":
        step(f"Manual mark: 直接写 device_state (跳过 _detect_risk_dialog)")
        ok, msg = trigger_manual_mark(args.device, args.days)
        if not ok:
            print(f"FAIL: {msg}")
            return 1
        print(f"  marked: {msg[:80]}")

    # ── auto 模式 (default) ──
    elif args.mode == "auto":
        step(f"Auto: 启动 Messenger 检查是否在 restriction page")
        adb(args.device, "shell", "input", "keyevent", "KEYCODE_HOME")
        time.sleep(0.6)
        adb(args.device, "shell", "monkey", "-p", "com.facebook.orca",
            "-c", "android.intent.category.LAUNCHER", "1")
        time.sleep(5)

        in_rp, xml_path = is_in_restriction_page(args.device)
        print(f"  in_restriction_page: {in_rp} (dump: {xml_path})")

        if in_rp:
            step("Real detect: _detect_risk_dialog 走 OPT-4+OPT-6 全链路")
            is_risk, msg = trigger_real_detect(args.device)
            print(f"  is_risk: {is_risk}")
            print(f"  msg: {msg[:150]}")
            if not is_risk:
                print("FAIL: in restriction page 但 _detect_risk_dialog 未识别")
                return 1
        else:
            step(f"Fallback to manual: 真机不在 restriction page, "
                 f"直接 _mark_account_restricted_state(days={args.days})")
            ok, msg = trigger_manual_mark(args.device, args.days)
            if not ok:
                print(f"FAIL: {msg}")
                return 1
            print(f"  marked: {msg[:80]}")

        # cleanup home (避免真机停在 Messenger)
        adb(args.device, "shell", "input", "keyevent", "KEYCODE_HOME")

    # ── 验证 ──
    step("Verify: 读 device_state + _is_account_restricted + executor._opt6_check_restriction")
    v = verify_state(args.device)
    for k, val in v.items():
        print(f"  {k}: {val}")

    if not v["is_account_restricted"]:
        print("\nFAIL: device_state 写入了但 _is_account_restricted=False")
        return 2
    if not v["executor_skip"]:
        print("\nFAIL: device_state 写入了但 executor 不 skip")
        return 2

    print(f"\n{'=' * 60}")
    print(f"  PASS: OPT-6 状态已生效 — 调度器将自动避开 {args.device[:12]} "
          f"约 {v['remaining_days']:.1f} 天")
    print(f"  lifted_at = {v['lifted_at_iso']}")
    print(f"{'=' * 60}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
