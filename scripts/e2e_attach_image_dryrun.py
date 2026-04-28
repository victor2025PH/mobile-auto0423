# -*- coding: utf-8 -*-
"""D1-A `attach_image` 真机 dry-run e2e 验证脚本（不真发消息）。

用途：在 IJ8H 真机驱动 attach_image 走完前 5 步（push/grant/tap gallery/
photo node 命中/tap select），最后 press back 退出 picker。验证 selector
在真机命中率，**不发任何消息**，零打扰。

测试拓扑（背景见 `~/.claude/.../fb_devices_2026-04-28.md`）:
  - IJ8H 是健康账号，可作发送端，但 4 台真机互相非好友 → 用 dry_run 模式
  - 默认进入 "しょうぶ あおり" 对话页（不活跃 last Sun）

用法：
  python scripts/e2e_attach_image_dryrun.py
  python scripts/e2e_attach_image_dryrun.py --device Q4N7AM7HMZGU4LZD
  python scripts/e2e_attach_image_dryrun.py --thread-tap-y 1185  # 进 Meta AI 对话替代

退出码: 0 全过, 1 dry_run 失败 (含 selector 真机 miss), 2 前置失败 (启动/进对话)
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time

# 修 Windows GBK 控制台中文/emoji 乱码
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# 让脚本能直接 import src.* (脚本在 scripts/ 下, 加项目根到 sys.path)
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


DEFAULT_DEVICE = "IJ8HZLORS485PJWW"
# しょうぶ あおり 对话条目中心坐标 (从 home XML 抓的 [0,681][720,825] 中心)
DEFAULT_THREAD_TAP_Y = 753


def adb(device: str, *args: str, timeout: int = 15) -> tuple[bool, str]:
    """run adb -s <device> ... and return (success, output)."""
    cmd = ["adb", "-s", device] + list(args)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return False, f"exception: {e}"


def step(label: str) -> None:
    print(f"\n{'=' * 60}\n  {label}\n{'=' * 60}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default=DEFAULT_DEVICE,
                    help="adb device serial")
    ap.add_argument("--thread-tap-y", type=int, default=DEFAULT_THREAD_TAP_Y,
                    help="点 Messenger 首页第几行的 y 坐标进对话")
    args = ap.parse_args()

    device = args.device

    # ── Pre 1. 验证设备在线 ──
    step("Pre 1: device check")
    ok, out = adb(device, "shell", "echo", "ok")
    if not ok or "ok" not in out:
        print(f"FAIL: device {device} not reachable: {out}")
        return 2
    print(f"  device {device} reachable")

    # ── Pre 2. 生成测试 QR ──
    step("Pre 2: generate test QR via qr_generator")
    try:
        from src.utils.qr_generator import build_line_qr
    except ImportError as e:
        print(f"FAIL: cannot import qr_generator: {e}")
        return 2
    qr_path = build_line_qr(f"e2e_test_user_{int(time.time())}",
                            force_regen=True)
    if not qr_path or not os.path.isfile(qr_path) \
            or os.path.getsize(qr_path) < 100:
        print(f"FAIL: QR not generated or too small: {qr_path}")
        return 2
    print(f"  QR generated: {qr_path} ({os.path.getsize(qr_path)} bytes)")

    # ── Pre 3. 重置真机到 launcher → 启动 Messenger ──
    step("Pre 3: launch Messenger")
    adb(device, "shell", "input", "keyevent", "KEYCODE_HOME")
    time.sleep(0.8)
    adb(device, "shell", "monkey", "-p", "com.facebook.orca",
        "-c", "android.intent.category.LAUNCHER", "1")
    time.sleep(5)
    ok, out = adb(device, "shell", "dumpsys", "window")
    if "com.facebook.orca" not in out:
        print(f"FAIL: Messenger not in foreground after launch")
        return 2
    print("  Messenger active")

    # ── Pre 4. tap into a conversation ──
    step("Pre 4: tap into conversation")
    # 中心 X = 360 (720 屏宽), Y 由参数控制
    adb(device, "shell", "input", "tap", "360", str(args.thread_tap_y))
    time.sleep(3)
    # 简单 verify: dump XML 看是否含 "Type a message"
    adb(device, "shell", "uiautomator", "dump", "/sdcard/_e2e_dump.xml")
    ok, _ = adb(device, "pull", "/sdcard/_e2e_dump.xml",
                os.path.join(tempfile.gettempdir(), "_e2e_dump.xml"))
    if ok:
        try:
            with open(os.path.join(tempfile.gettempdir(),
                                   "_e2e_dump.xml"),
                      encoding="utf-8", errors="replace") as f:
                xml = f.read()
            if "Type a message" not in xml and "输入消息" not in xml:
                print("WARN: 未检测到 composer; 可能没进对话页, 但仍尝试 attach")
            else:
                print("  conversation page confirmed (composer present)")
        except Exception:
            pass

    # ── Step 1: 构造 fb + 调 attach_image dry_run ──
    step("Step 1: attach_image(dry_run=True) on real device")
    try:
        from src.device_control.device_manager import get_device_manager
        from src.app_automation.facebook import (
            AttachImageError, FacebookAutomation,
        )
    except ImportError as e:
        print(f"FAIL: import error: {e}")
        return 2

    dm = get_device_manager()
    # ensure DM 知道这个设备 (有些 DM 实现要求显式 add_device)
    try:
        info = dm.get_device_info(device)
        print(f"  device info: {info}")
    except Exception as e:
        print(f"  WARN: get_device_info 异常: {e}")

    fb = FacebookAutomation(device_manager=dm)
    try:
        result = fb.attach_image(
            qr_path,
            device_id=device,
            dry_run=True,
            raise_on_error=True,
        )
    except AttachImageError as e:
        print(f"FAIL [AttachImageError]: code={e.code} msg={e} hint={e.hint}")
        return 1
    except Exception as e:
        print(f"FAIL [unexpected]: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return 1

    if not result:
        print("FAIL: attach_image returned False (silent mode shouldn't trigger here)")
        return 1

    print(f"\n{'=' * 60}\n  PASS: attach_image dry_run succeeded\n{'=' * 60}")
    print("  selector 真机命中, picker 选图成功, 已 press back 退出 (无发送)")

    # ── Cleanup: home ──
    adb(device, "shell", "input", "keyevent", "KEYCODE_HOME")
    return 0


if __name__ == "__main__":
    sys.exit(main())
