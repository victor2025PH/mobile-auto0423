# -*- coding: utf-8 -*-
"""4 台真机轮流给 Meta AI 真发测试文字 (验证 OPT-FP1-FP6 完整防御链)."""
from __future__ import annotations

import os
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


DEVICES = [
    ("IJ8HZLORS485PJWW", "主控-03 (Shuichi Ito 日本账号)"),
    ("Q4N7AM7HMZGU4LZD", "主控-07"),
    ("SWZLPNYTROMZMJLR", "主控-06 (风控 6 天)"),
    ("XW8TQKEQIVJRQO69", "主控-08 (中文 UI)"),
]


def send_one(fb, device, label):
    timestamp = time.strftime("%H:%M:%S")
    msg = f"hi meta-ai {timestamp}"
    print(f"\n{'-' * 60}")
    print(f"  {label}\n  serial:  {device}\n  message: {msg}")
    print(f"{'-' * 60}")
    t0 = time.time()
    result = {"device": device, "label": label, "message": msg}
    try:
        from src.app_automation.facebook import MessengerError
        ok = fb.send_message(
            "Meta AI", msg, device_id=device, raise_on_error=True)
        elapsed = time.time() - t0
        result["status"] = "PASS"
        result["elapsed_s"] = round(elapsed, 1)
        result["error"] = ""
        print(f"  ✅ PASS — 用时 {elapsed:.1f}s")
    except Exception as e:
        elapsed = time.time() - t0
        from src.app_automation.facebook import MessengerError
        if isinstance(e, MessengerError):
            result["status"] = f"MessengerError({e.code})"
            result["error"] = f"{e}"
            result["hint"] = e.hint
            print(f"  ⚠️  code={e.code} 用时 {elapsed:.1f}s\n  hint: {e.hint}")
        else:
            result["status"] = "EXCEPTION"
            result["error"] = f"{type(e).__name__}: {e}"
            print(f"  ❌ {type(e).__name__}: {e}")
        result["elapsed_s"] = round(elapsed, 1)
    return result


def main():
    print("=" * 60)
    print("L5 v2: 4 真机给 Meta AI 真发 — 验证 OPT-FP1-FP6 完整防线")
    print("=" * 60)

    from src.app_automation.facebook import FacebookAutomation
    from src.device_control.device_manager import get_device_manager
    dm = get_device_manager()
    fb = FacebookAutomation(device_manager=dm)

    results = []
    for serial, label in DEVICES:
        r = send_one(fb, serial, label)
        results.append(r)
        time.sleep(2)

    print(f"\n{'=' * 60}\n  汇总\n{'=' * 60}")
    for r in results:
        emoji = "✅" if r["status"] == "PASS" else "⚠️ "
        print(f"  {r['device'][:18]} {emoji} {r['status']:<35} {r['elapsed_s']}s")
        if r.get("hint"):
            print(f"      hint: {r['hint'][:100]}")

    pass_count = sum(1 for r in results if r["status"] == "PASS")
    print(f"\n  PASS: {pass_count}/{len(results)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
