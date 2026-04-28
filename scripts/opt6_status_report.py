# -*- coding: utf-8 -*-
"""OPT-6 多设备状态报告 — 批量扫描 + markdown 输出.

扩展 opt6_trigger_real_or_manual.py 的 verify 单设备模式 → 批量扫所有
真机, 一键查看哪些设备当前 in restriction + 何时解封. 运维 daily 健康
检查友好.

数据源:
  - device list: config/device_aliases.json (项目设备注册表)
  - state read: device_state.platform='facebook' (OPT-6 写入)
  - logic: executor._opt6_check_restriction (调度器实际跑的判断)

用法:
  python scripts/opt6_status_report.py
  python scripts/opt6_status_report.py --json   # 输出 JSON 供 dashboard 集成
  python scripts/opt6_status_report.py --only-restricted  # 仅显示 restricted 设备

输出格式 (markdown 默认):
  | device | alias | restriction_status | days remaining | lifted_at_iso |

退出码: 0 全部 healthy / 1 有 restricted 设备 (CI 警报触发用)
"""
from __future__ import annotations

import argparse
import json
import os
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


def load_device_list():
    """从 config/device_aliases.json 读所有注册设备 serial.

    Returns: list of (serial, alias) tuples
    """
    path = os.path.join(_ROOT, "config", "device_aliases.json")
    if not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"WARN: 读 device_aliases.json 失败: {e}", file=sys.stderr)
        return []
    out = []
    for serial, info in data.items():
        # 跳过 IP 地址类 key (192.168.0.160:5555 等)
        if ":" in serial or serial.startswith("192."):
            continue
        if not isinstance(info, dict):
            continue
        alias = info.get("alias") or info.get("display_label") or "?"
        out.append((serial, alias))
    return out


def collect_status(device_id):
    """对单设备调 _opt6_check_restriction + 读完整 device_state.

    Returns: dict {device, alias, is_restricted, lifted_at, lifted_at_iso,
                   remaining_days, restriction_days, restriction_full_msg,
                   detected_at, executor_skip, executor_reason}
    """
    from src.app_automation.facebook import FacebookAutomation
    from src.host.device_state import DeviceStateStore
    from src.host.executor import _opt6_check_restriction

    ds = DeviceStateStore(platform="facebook")
    lifted_at = ds.get_float(device_id, "restriction_lifted_at", 0.0)
    days = ds.get_int(device_id, "restriction_days", 0)
    full_msg = ds.get(device_id, "restriction_full_msg", "")
    detected_at = ds.get_float(device_id, "restriction_detected_at", 0.0)

    fb = FacebookAutomation.__new__(FacebookAutomation)
    is_r, _ = fb._is_account_restricted(device_id)
    skip, reason = _opt6_check_restriction(device_id)

    now = time.time()
    remaining = max(0.0, (lifted_at - now) / 86400) if lifted_at > 0 else 0.0

    return {
        "device": device_id,
        "is_restricted": is_r,
        "lifted_at": lifted_at,
        "lifted_at_iso": (datetime.fromtimestamp(lifted_at).isoformat()
                          if lifted_at > 0 else ""),
        "remaining_days": round(remaining, 2),
        "restriction_days": days,
        "restriction_full_msg": full_msg[:120],
        "detected_at_iso": (datetime.fromtimestamp(detected_at).isoformat()
                            if detected_at > 0 else ""),
        "executor_skip": skip,
        "executor_reason": reason,
    }


def render_markdown(rows, only_restricted=False):
    lines = [
        f"# OPT-6 多设备 restriction 状态报告 ({datetime.now().isoformat()})",
        "",
        f"扫描设备数: {len(rows)}",
        f"受限设备数: {sum(1 for r in rows if r['is_restricted'])}",
        "",
        "## 概览",
        "",
        "| device | alias | restriction | days remaining | lifted_at | reason |",
        "|---|---|---|---|---|---|",
    ]
    for r in rows:
        if only_restricted and not r["is_restricted"]:
            continue
        status = "**🚫 RESTRICTED**" if r["is_restricted"] else "✅ healthy"
        days_remaining = (f"{r['remaining_days']:.1f}"
                          if r["is_restricted"] else "-")
        lifted = r["lifted_at_iso"][:19] if r["lifted_at_iso"] else "-"
        reason = (r["executor_reason"][:80].replace("|", "/")
                  if r["executor_reason"] else "-")
        lines.append(
            f"| {r['device'][:12]} | {r['alias']} | {status} | "
            f"{days_remaining} | {lifted} | {reason} |"
        )
    if any(r["is_restricted"] for r in rows):
        lines.append("")
        lines.append("## Restricted 设备详情")
        lines.append("")
        for r in rows:
            if not r["is_restricted"]:
                continue
            lines.append(f"### {r['device']} ({r['alias']})")
            lines.append("```")
            for k, v in r.items():
                lines.append(f"  {k}: {v}")
            lines.append("```")
            lines.append("")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true",
                    help="输出 JSON 而非 markdown (供 dashboard 集成)")
    ap.add_argument("--only-restricted", action="store_true",
                    help="只显示 restricted 设备")
    args = ap.parse_args()

    devices = load_device_list()
    if not devices:
        print("FAIL: device_aliases.json 没找到任何设备", file=sys.stderr)
        return 1

    rows = [
        {**collect_status(serial), "alias": alias}
        for serial, alias in devices
    ]

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        print(render_markdown(rows, only_restricted=args.only_restricted))

    return 1 if any(r["is_restricted"] for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
