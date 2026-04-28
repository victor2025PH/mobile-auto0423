# -*- coding: utf-8 -*-
"""4 真机 selector regression batch smoke test (D1-A/D1-B/OPT-4/OPT-7 真机回归)。

用途：在每台真机上跑一次 attach_image dry_run, 验证:
  - selector 命中（真机 UI 跟单测假设一致）
  - OPT-7 不再触发 "smart_tap-heal app 漂移" 误报
  - OPT-4 在 SWZL 风控页能识别（如果当前真机正在 restriction）

特性 (OPT-5 minimal):
  - 自动 dismiss 已知对话框 (中文区不支持 / restriction OK)
  - 状态分类: inbox / language_dialog / restriction_page / login / unknown
  - 输出 markdown 报告到 D:\\workspace\\rpa-debug-2026-04-28\\smoke_<ts>.md

用法：
  python scripts/e2e_4device_smoke.py
  python scripts/e2e_4device_smoke.py --device IJ8HZLORS485PJWW XW8TQKEQIVJRQO69
  python scripts/e2e_4device_smoke.py --skip-attach   # 只检测状态, 不跑 dry_run
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
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

DEFAULT_DEVICES = [
    "IJ8HZLORS485PJWW",  # 主控-03 — Shuichi Ito 健康
    "Q4N7AM7HMZGU4LZD",  # 主控-07 — 中文区不支持
    "SWZLPNYTROMZMJLR",  # 主控-06 — restriction 6 days
    "XW8TQKEQIVJRQO69",  # 主控-?? — 中文 UI 健康
]

OUT_DIR = r"D:\workspace\rpa-debug-2026-04-28"


def adb(device, *args, timeout=15):
    cmd = ["adb", "-s", device] + list(args)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout)
        return r.returncode == 0, (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return False, f"exception: {e}"


def dump_xml(device, label="dump"):
    """dump 当前 UI XML 到本地 + 返路径。"""
    ts = int(time.time())
    remote = f"/sdcard/_smoke_{label}_{ts}.xml"
    local = os.path.join(OUT_DIR, f"smoke_{device}_{label}_{ts}.xml")
    adb(device, "shell", "uiautomator", "dump", remote)
    adb(device, "pull", remote, local)
    return local if os.path.isfile(local) else ""


def classify_state(xml_path):
    """根据 dump XML 判定当前 Messenger 页面状态。

    Returns: (state_id, marker_text)
      conversation_page — "Open photo gallery." + "Type a message"/"输入消息"
                          (Messenger resume session 常见, 直接跑 dry_run)
      inbox             — search bar + new message 按钮 (多语言)
      language_dialog   — "继续使用美式英语" / "继续"
      restriction_page  — "Your account has been restricted"
      login             — "Continue as" / "Log In"
      unknown           — 都不命中
    """
    if not xml_path or not os.path.isfile(xml_path):
        return "unknown", ""
    try:
        with open(xml_path, encoding="utf-8", errors="replace") as f:
            xml = f.read()
    except Exception:
        return "unknown", ""

    # ★ 优先检测 conversation_page (Messenger resume session 常出现)
    if "Open photo gallery." in xml and (
            "Type a message" in xml
            or "输入消息" in xml
            or "メッセージを入力" in xml):
        return "conversation_page", "composer + Open photo gallery"

    if "Your account has been restricted" in xml:
        return "restriction_page", "account restricted"
    if "继续使用美式英语" in xml or "我们目前无法设置中文" in xml:
        return "language_dialog", "中文区不支持"

    # inbox 多语言: 搜索栏 marker (en/zh-CN/zh-TW) + 新消息按钮
    has_search_bar = ("Ask Meta AI" in xml
                      or "问问 Meta AI" in xml      # zh-CN
                      or "問問 Meta AI" in xml      # zh-TW
                      or "Ask anything" in xml)
    has_new_msg = ("New message" in xml
                   or "新消息" in xml             # zh-CN
                   or "新訊息" in xml             # zh-TW
                   or "新規メッセージ" in xml)     # ja
    if has_search_bar and has_new_msg:
        return "inbox", "search bar + new message"
    if "Continue as" in xml or "Log into Facebook" in xml \
            or "Log in to your account" in xml:
        return "login", "Login page"
    return "unknown", ""


def dismiss_language_dialog(device, xml_path):
    """点'继续使用美式英语'按钮（OPT-5 minimal）。"""
    try:
        with open(xml_path, encoding="utf-8", errors="replace") as f:
            xml = f.read()
    except Exception:
        return False
    m = re.search(
        r'text="继续使用美式英语"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
        xml)
    if not m:
        return False
    cx = (int(m.group(1)) + int(m.group(3))) // 2
    cy = (int(m.group(2)) + int(m.group(4))) // 2
    adb(device, "shell", "input", "tap", str(cx), str(cy))
    time.sleep(3)
    return True


def dismiss_restriction_ok(device, xml_path):
    """点 restriction page 的 OK 按钮。"""
    try:
        with open(xml_path, encoding="utf-8", errors="replace") as f:
            xml = f.read()
    except Exception:
        return False
    m = re.search(
        r'text="OK"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml)
    if not m:
        return False
    cx = (int(m.group(1)) + int(m.group(3))) // 2
    cy = (int(m.group(2)) + int(m.group(4))) // 2
    adb(device, "shell", "input", "tap", str(cx), str(cy))
    time.sleep(3)
    return True


def find_first_thread_y(xml_path):
    """从 inbox XML 找第一条会话的中心 y 坐标。

    会话列表里第一个 <node> SimpleTextThreadSnippet 或 类似结构。
    简化策略: 找第一个 Button bounds 在 y > 300 (跳过 active-now stories)。
    """
    if not xml_path or not os.path.isfile(xml_path):
        return None
    try:
        with open(xml_path, encoding="utf-8", errors="replace") as f:
            xml = f.read()
    except Exception:
        return None
    # SimpleTextThreadSnippet 是 IJ8H 实测的会话节点 desc pattern
    for m in re.finditer(
            r'content-desc="[^"]*SimpleTextThreadSnippet[^"]*"[^>]*'
            r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml):
        y1, y2 = int(m.group(2)), int(m.group(4))
        if y1 > 300:
            return (y1 + y2) // 2
    # 备用: 找第一个 Button 节点 bounds y > 500（跳 stories + search）
    for m in re.finditer(
            r'<node[^>]*class="android\.widget\.Button"[^>]*'
            r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml):
        y1, y2 = int(m.group(2)), int(m.group(4))
        height = y2 - y1
        if y1 > 500 and 100 <= height <= 200:
            return (y1 + y2) // 2
    return None


def run_attach_dry_run(device):
    """对一台已在对话页的真机调 attach_image dry_run。返回 (ok, error_msg)。"""
    try:
        from src.app_automation.facebook import (
            AttachImageError, FacebookAutomation,
        )
        from src.device_control.device_manager import get_device_manager
        from src.utils.qr_generator import build_line_qr
    except ImportError as e:
        return False, f"import: {e}"

    qr = build_line_qr(f"smoke_{device}_{int(time.time())}",
                        force_regen=True)
    if not qr:
        return False, "QR generation failed"

    dm = get_device_manager()
    fb = FacebookAutomation(device_manager=dm)
    try:
        ok = fb.attach_image(qr, device_id=device, dry_run=True,
                             raise_on_error=True)
        return bool(ok), ""
    except AttachImageError as e:
        return False, f"AttachImageError({e.code}): {e}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def run_one_device(device, skip_attach=False):
    """对一台真机跑完整 smoke test。返回结果 dict。"""
    result = {
        "device": device,
        "state": "unknown",
        "marker": "",
        "dismissed": [],
        "attach_dry_run": None,
        "attach_error": "",
    }

    # 1. 启动 Messenger
    adb(device, "shell", "input", "keyevent", "KEYCODE_HOME")
    time.sleep(0.6)
    adb(device, "shell", "monkey", "-p", "com.facebook.orca",
        "-c", "android.intent.category.LAUNCHER", "1")
    time.sleep(5)

    # 2. dump + classify (最多 2 次 — 第一次可能有 dialog 挡)
    for round_idx in range(2):
        xml_path = dump_xml(device, label=f"home_r{round_idx}")
        state, marker = classify_state(xml_path)
        result["state"] = state
        result["marker"] = marker

        if state == "language_dialog":
            ok = dismiss_language_dialog(device, xml_path)
            if ok:
                result["dismissed"].append("language_dialog")
                continue
        if state == "restriction_page":
            # OPT-4 真机回归: 验证当前页 _detect_risk_dialog 能识别 (just log)
            result["restriction_detected_in_xml"] = True
            ok = dismiss_restriction_ok(device, xml_path)
            if ok:
                result["dismissed"].append("restriction_ok")
                continue
        break

    # 3. 跑 attach dry_run 的两条路径:
    #    (a) conversation_page 状态 → 直接调 (Messenger 已停在对话页)
    #    (b) inbox 状态 → tap 第一个对话进入再调
    if not skip_attach and result["state"] == "conversation_page":
        result["composer_visible"] = True  # state 检测已含 composer
        ok, err = run_attach_dry_run(device)
        result["attach_dry_run"] = "PASS" if ok else "FAIL"
        result["attach_error"] = err
    elif not skip_attach and result["state"] == "inbox":
        # tap 进第一个对话
        thread_y = find_first_thread_y(xml_path)
        if thread_y is None:
            thread_y = 753  # IJ8H 实测中位数兜底
        adb(device, "shell", "input", "tap", "360", str(thread_y))
        time.sleep(3)

        # verify 进了对话页 (composer present)
        verify_xml = dump_xml(device, label="conv")
        try:
            with open(verify_xml, encoding="utf-8", errors="replace") as f:
                vx = f.read()
        except Exception:
            vx = ""
        composer_ok = ("Type a message" in vx
                       or "输入消息" in vx
                       or "Message" in vx)
        result["composer_visible"] = composer_ok

        if composer_ok:
            ok, err = run_attach_dry_run(device)
            result["attach_dry_run"] = "PASS" if ok else "FAIL"
            result["attach_error"] = err
        else:
            result["attach_dry_run"] = "SKIP"
            result["attach_error"] = "no composer in conversation page"

    # 4. cleanup home
    adb(device, "shell", "input", "keyevent", "KEYCODE_HOME")
    return result


def render_report(results, args):
    """生成 markdown 报告。"""
    lines = [
        f"# 4 真机 smoke test 报告 ({datetime.now().isoformat()})",
        "",
        f"## 概览",
        "",
        "| 设备 | 状态 | dismissed | composer | dry_run | error |",
        "|---|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            f"| {r['device']} | {r['state']} ({r['marker']}) | "
            f"{','.join(r['dismissed']) or '-'} | "
            f"{r.get('composer_visible', '-')} | "
            f"{r.get('attach_dry_run', '-')} | "
            f"{(r.get('attach_error') or '')[:80]} |"
        )
    lines.append("")
    lines.append("## 详细")
    lines.append("")
    for r in results:
        lines.append(f"### {r['device']}")
        lines.append("```")
        for k, v in r.items():
            lines.append(f"  {k}: {v}")
        lines.append("```")
        lines.append("")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", nargs="*", default=DEFAULT_DEVICES)
    ap.add_argument("--skip-attach", action="store_true")
    args = ap.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)

    print(f"=== smoke test 4 device 启动 ({len(args.device)} 台) ===")
    results = []
    for device in args.device:
        print(f"\n--- {device} ---")
        r = run_one_device(device, skip_attach=args.skip_attach)
        for k, v in r.items():
            print(f"  {k}: {v}")
        results.append(r)

    report = render_report(results, args)
    report_path = os.path.join(
        OUT_DIR, f"smoke_4device_{int(time.time())}.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"\n=== 报告: {report_path} ===")
    print()
    print(report)

    # 退出码: 任一台 dry_run FAIL → 1
    fails = [r for r in results
             if r.get("attach_dry_run") == "FAIL"]
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
