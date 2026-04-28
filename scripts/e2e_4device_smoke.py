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


def dump_xml(device, label="dump", d=None):
    """dump 当前 UI XML 到本地 + 返路径。

    优先用 d.dump_hierarchy() (跟 fb_dialog_dismisser 共用 u2 server,
    避免 adb shell uiautomator 跟 u2 server 冲突). d=None 时回退 adb。
    """
    ts = int(time.time())
    local = os.path.join(OUT_DIR, f"smoke_{device}_{label}_{ts}.xml")

    if d is not None:
        try:
            xml = d.dump_hierarchy()
            if xml:
                with open(local, "w", encoding="utf-8") as f:
                    f.write(xml)
                return local
        except Exception as e:
            print(f"  WARN: d.dump_hierarchy() 失败 ({e}), 回退 adb")

    # adb 兜底 (d=None 或 d.dump 失败)
    remote = f"/sdcard/_smoke_{label}_{ts}.xml"
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
    """从 inbox XML 找第一条会话的中心 y 坐标 (smoke v5 修).

    v4 用 `content-desc="...SimpleTextThreadSnippet..."[^>]*bounds=...`
    单 regex 因 `[^>]*` 贪婪匹配跨过 bounds → 0 命中. v5 改两步法:
      1. 先找含 SimpleTextThreadSnippet 的 content-desc 起始位置
      2. 从该位置 +400 chars 内找 bounds (一个 node 一般 < 400 chars)

    同时拓展 thread node identifier (除 SimpleTextThreadSnippet 外):
      - `X.2Xl@` (Messenger 反编译节点 obfuscation 标识)
      - `class="android.widget.Button"` 高度 100-200 + y > 500 兜底

    Returns:
        中心 y 坐标 int / None 没找到
    """
    if not xml_path or not os.path.isfile(xml_path):
        return None
    try:
        with open(xml_path, encoding="utf-8", errors="replace") as f:
            xml = f.read()
    except Exception:
        return None

    # === Pass 1: SimpleTextThreadSnippet 模式 (主路径) ===
    bounds_re = re.compile(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"')
    for m in re.finditer(r'SimpleTextThreadSnippet', xml):
        # 从该位置 +400 chars 内找 bounds
        chunk = xml[m.start():m.start() + 400]
        bm = bounds_re.search(chunk)
        if bm:
            y1, y2 = int(bm.group(2)), int(bm.group(4))
            if y1 > 300:  # 跳 stories + search bar
                return (y1 + y2) // 2

    # === Pass 2: X.2Xl@ obfuscation 标识 (Messenger 反编译节点) ===
    for m in re.finditer(r'X\.2Xl@', xml):
        chunk = xml[m.start():m.start() + 400]
        bm = bounds_re.search(chunk)
        if bm:
            y1, y2 = int(bm.group(2)), int(bm.group(4))
            if y1 > 300:
                return (y1 + y2) // 2

    # === Pass 3: 兜底 — Button 节点高度 100-200 + y > 500 ===
    button_re = re.compile(
        r'<node[^>]*class="android\.widget\.Button"[^>]*?'
        r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"')
    for m in button_re.finditer(xml):
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


def launch_messenger_stable(device):
    """smoke v4 (2026-04-28): 强制把 Messenger 拉到前台, 替代 monkey 不可靠。

    monkey 在覆盖窗口存在时 (锁屏/通知栏下拉/前台是别的 app/MIUI 安全弹窗)
    可能成功调度 intent 但 UI 不切换. v4 修复:
      1. 多次 KEYCODE_BACK 清覆盖窗口 (3 次足够, 大部分弹窗 1-2 次能关)
      2. KEYCODE_HOME 回 launcher
      3. force-stop com.facebook.orca 干净启动 (避免 resume 上次 view)
      4. am start -n com.facebook.orca/.MainActivity 显式 entry
      5. 等 5s UI 渲染
      6. verify dumpsys window 含 com.facebook.orca

    Returns: bool 启动成功 (orca 在前台)
    """
    # smoke v6 (2026-04-28): 先 collapse 通知栏 (KEYCODE_BACK 对通知栏
    # 无效, IJ8H 实测 launcher 下拉通知栏时 BACK x3 + HOME + am-start
    # 都进不了 Messenger → state=unknown). cmd statusbar collapse 是
    # Android 系统命令, 直接通知栏收起.
    adb(device, "shell", "cmd", "statusbar", "collapse")
    time.sleep(0.5)
    # 1. 清覆盖窗口
    for _ in range(3):
        adb(device, "shell", "input", "keyevent", "KEYCODE_BACK")
        time.sleep(0.3)
    # 2. 回 launcher
    adb(device, "shell", "input", "keyevent", "KEYCODE_HOME")
    time.sleep(0.6)
    # 3. force-stop 干净启动
    adb(device, "shell", "am", "force-stop", "com.facebook.orca")
    time.sleep(1.0)
    # 4. am start LAUNCHER intent (比 -n 显式 activity 更可靠 —
    # Android 自动找包名对应的入口 activity)
    adb(device, "shell", "am", "start", "-a", "android.intent.action.MAIN",
        "-c", "android.intent.category.LAUNCHER", "com.facebook.orca/.MainActivity")
    # 5. 等 UI 渲染
    time.sleep(5)
    # 6. verify
    ok, out = adb(device, "shell", "dumpsys", "window")
    if "com.facebook.orca" in (out or ""):
        return True
    # 7. 最后兜底: monkey LAUNCHER (force-stop 之后 monkey 也比裸 monkey 稳)
    adb(device, "shell", "monkey", "-p", "com.facebook.orca",
        "-c", "android.intent.category.LAUNCHER", "1")
    time.sleep(4)
    ok, out = adb(device, "shell", "dumpsys", "window")
    return "com.facebook.orca" in (out or "")


def run_one_device(device, skip_attach=False):
    """对一台真机跑完整 smoke test。返回结果 dict。"""
    result = {
        "device": device,
        "state": "unknown",
        "marker": "",
        "dismissed": [],
        "cleared_dialogs": [],
        "launched_via": "",
        "attach_dry_run": None,
        "attach_error": "",
    }

    # 1. 启动 Messenger (smoke v4 — 稳定 launch)
    launch_ok = launch_messenger_stable(device)
    result["launched_via"] = "force-stop+am-start" if launch_ok else "FAIL"
    if not launch_ok:
        # 兜底回退 monkey (老路径, 兼容某些 ROM force-stop 受限)
        adb(device, "shell", "monkey", "-p", "com.facebook.orca",
            "-c", "android.intent.category.LAUNCHER", "1")
        time.sleep(5)
        result["launched_via"] = "monkey-fallback"

    # OPT-5 v3 集成 (2026-04-28): 用 fb_dialog_dismisser 通用模块清场,
    # 处理 Q4N7 "Previews are on" / 中文区不支持 / 通知请求等 startup dialog.
    # 同时拿 d 共用给 dump_xml (避免 u2 server 跟 adb shell uiautomator 冲突).
    d_for_dump = None
    try:
        from src.app_automation.fb_dialog_dismisser import (
            dismiss_known_dialogs,
        )
        from src.device_control.device_manager import get_device_manager
        dm = get_device_manager()
        d_for_dump = dm.get_u2(device)
        if d_for_dump:
            cleared = dismiss_known_dialogs(d_for_dump)
            result["cleared_dialogs"] = cleared
            if cleared:
                time.sleep(1.5)  # 给 UI 稳定时间
    except Exception as e:
        result["dismisser_error"] = str(e)

    # 2. dump + classify (最多 2 次 — 第一次可能有 dialog 挡)
    for round_idx in range(2):
        xml_path = dump_xml(device, label=f"home_r{round_idx}", d=d_for_dump)
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
        # smoke v5 (2026-04-28): thread tap + retry verify + 备用 y 兜底
        thread_y = find_first_thread_y(xml_path)
        result["thread_y_parsed"] = thread_y

        # 主尝试 + 3 个备用 y 坐标 (覆盖不同 inbox 布局)
        candidate_ys = []
        if thread_y is not None:
            candidate_ys.append(thread_y)
        for fallback_y in (609, 753, 897):  # IJ8H 实测前 3 thread 中心
            if fallback_y not in candidate_ys:
                candidate_ys.append(fallback_y)

        composer_ok = False
        tried_ys = []
        for tap_y in candidate_ys:
            adb(device, "shell", "input", "tap", "360", str(tap_y))
            tried_ys.append(tap_y)
            # retry verify 3 次, 每次 sleep 2s = 最多 6s 等 UI 稳定
            for retry_idx in range(3):
                time.sleep(2.0)
                verify_xml = dump_xml(device, label=f"conv_y{tap_y}_r{retry_idx}",
                                      d=d_for_dump)
                try:
                    with open(verify_xml, encoding="utf-8",
                              errors="replace") as f:
                        vx = f.read()
                except Exception:
                    vx = ""
                if ("Type a message" in vx
                        or "输入消息" in vx
                        or "メッセージを入力" in vx):
                    composer_ok = True
                    break
            if composer_ok:
                break
            # 没进 conversation, BACK 一次准备下一个 y
            adb(device, "shell", "input", "keyevent", "KEYCODE_BACK")
            time.sleep(1.0)

        result["composer_visible"] = composer_ok
        result["tried_ys"] = tried_ys

        if composer_ok:
            ok, err = run_attach_dry_run(device)
            result["attach_dry_run"] = "PASS" if ok else "FAIL"
            result["attach_error"] = err
        else:
            result["attach_dry_run"] = "SKIP"
            result["attach_error"] = (
                f"no composer in conversation page after trying y={tried_ys}")

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
