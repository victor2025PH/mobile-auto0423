"""
Facebook Automation — hybrid module combining AutoSelector with platform logic.

Architecture:
  - Inherits BaseAutomation for device/compliance/behavior
  - Uses AutoSelector for self-learning UI interaction
  - Adds Facebook-specific logic: dialog dismissal, feed patterns, Leads integration
  - Works with GenericAppPlugin YAML flows for basic operations

Sprint 1 expansion (Facebook 模块独立化):
  - 升级 add_friend → add_friend_with_note(safe_mode=True 默认开启,先进主页停留再加)
  - 新增 browse_groups / enter_group / scroll_group_posts / comment_on_post
  - 新增 extract_group_members / group_engage_session
  - 新增 view_profile / read_profile_about
  - Sprint 2 占位: check_messenger_inbox / check_message_requests / check_friend_requests_inbox
"""

from __future__ import annotations

import logging
import random
import time
from typing import Any, Dict, List, Optional, Tuple

from .base_automation import BaseAutomation

log = logging.getLogger(__name__)

PACKAGE = "com.facebook.katana"
MESSENGER_PACKAGE = "com.facebook.orca"

_FB_DISMISS_TEXTS = [
    "Not Now", "NOT NOW", "Not now",
    "Skip", "SKIP",
    "Maybe Later", "Later",
    "No Thanks", "No thanks",
    "Dismiss", "Close",
    "OK", "Got it", "GOT IT",
    "Continue", "CONTINUE",
    "Allow", "ALLOW",
    "Allow all the time",
    "While using the app",
]

_FB_RISK_KEYWORDS = [
    "Confirm your identity",
    "We've temporarily blocked",
    "We've temporarily restricted",
    "You can't use this feature",
    "We need to confirm it's you",
    "Suspicious login attempt",
    "Help us confirm it's you",
    "Please verify your account",
    "Your account has been disabled",
    "account is locked",
]

_FB_RISK_BUTTONS = [
    "Continue", "Confirm", "Verify", "Get a Code", "Help me", "Send Code",
    "Try Another Way", "I Can't Access",
]


_RISK_DETECT_VERIFY_DELAY = 1.6
_RISK_DETECT_PROBE_TIMEOUT = 0.25


# ─── P0-1: browse_feed 真人节奏常量（2026-04-21 重写）─────────────────────
# 动机: 旧版 `scroll_count = max(5, duration//6)` + `wait_read(200~800ms)`
# 意味着 duration=15 → 只滑 5 屏 × 0.5s ≈ 几秒就结束，完全达不到"养号"目的。
# 新公式按"每分钟 4 屏 + 每屏停留 2~8s + 15% 概率停留看视频 8~20s"建模，
# 让 duration=15 真的跑满 ~15 分钟、约 60 屏、约 1~2 次视频停留。
FB_BROWSE_DEFAULTS = {
    "scroll_per_min": 4,              # 每分钟滑多少屏（真人 feed 大约 3-6）
    "short_wait_ms": (2000, 8000),    # 每屏之间正常停留
    "video_dwell_prob": 0.15,         # 概率进入"看视频/长图文停留"子动作
    "video_dwell_ms": (8000, 20000),  # 长停留时长
    "like_probability": 0.05,         # 点赞率（真人 feed 1-3%）
    "pull_refresh_prob": 0.08,        # 概率下拉刷新
    "max_scrolls_hard_cap": 400,      # 安全上限
}


class FbWarmupError(Exception):
    """browse_feed 结构化错误。code 用于前端出 hint，message 给人看。"""

    def __init__(self, code: str, message: str, hint: str = ""):
        super().__init__(message)
        self.code = code
        self.hint = hint or ""


def _messenger_active_lock(device_id: str, timeout: float = 30.0):
    """F3 (A→B review Q10): ``device_section_lock("messenger_active")`` 的
    feature-detect wrapper。

    A 机 Phase 5 (``src.host.fb_concurrency``) 未合入 main 前,返回
    ``contextlib.nullcontext()`` 不加锁不报错,功能不降级;Phase 5 合入后
    自动启用真锁,和 A 的 ``send_greeting_after_add_friend`` fallback 分支
    共用 "messenger_active" section,实现同 device 两边 Messenger UI 操作
    串行化(避免抢输入框/撞 daily_cap)。

    超时行为: A 的实现超时 ``raise RuntimeError``,调用方要 catch 并降级
    (通常是 skip 本轮,记日志)。
    """
    try:
        from src.host.fb_concurrency import device_section_lock
        return device_section_lock(device_id, "messenger_active",
                                    timeout=timeout)
    except ImportError:
        from contextlib import nullcontext
        return nullcontext()


def _now_iso() -> str:
    """与 fb_store 同格式的 UTC ISO 串（用于 stats 时间戳）。"""
    import datetime as _dt
    return _dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _resolve_scroll_count(duration_minutes: Optional[int],
                          scroll_count: Optional[int],
                          cfg: Optional[Dict[str, Any]] = None) -> int:
    """按 duration 反推 scroll_count。显式传 scroll_count 则优先。

    ``cfg`` 可传入 playbook 解析后的 browse_feed 配置（phase 级），
    不传则用模块级 FB_BROWSE_DEFAULTS（P1-1 引入）。
    """
    effective = cfg or FB_BROWSE_DEFAULTS
    hard_cap = int(effective.get("max_scrolls_hard_cap") or FB_BROWSE_DEFAULTS["max_scrolls_hard_cap"])
    if scroll_count is not None and int(scroll_count) > 0:
        return min(int(scroll_count), hard_cap)
    minutes = int(duration_minutes or 15)
    minutes = max(1, minutes)
    n = minutes * int(effective.get("scroll_per_min") or FB_BROWSE_DEFAULTS["scroll_per_min"])
    return min(max(5, n), hard_cap)


def _load_browse_feed_cfg(phase: Optional[str] = None) -> Dict[str, Any]:
    """读 playbook 的 browse_feed 参数。读失败回退到 FB_BROWSE_DEFAULTS。"""
    try:
        from src.host.fb_playbook import resolve_browse_feed_params
        cfg = resolve_browse_feed_params(phase=phase)
        if cfg:
            return cfg
    except Exception as e:
        log.debug("[browse_feed] playbook 读取失败，回退到模块默认: %s", e)
    return dict(FB_BROWSE_DEFAULTS)


# ─── P0-2: playbook phase 参数 + 文案包的统一解析入口 ───────────────────
# 2026-04-22 新增:把 kwargs 中的 persona_key / device_id 转换成
# (phase, playbook_cfg) 二元组,避免每个业务方法都重复写"取 phase → resolve"。
def _resolve_phase_and_cfg(section: str,
                           device_id: Optional[str] = None,
                           phase_override: Optional[str] = None) -> Tuple[str, Dict[str, Any]]:
    """返回 (phase, cfg) — 业务方法调用一次即可。

    * phase 优先 explicit 参数 → fb_account_phase.get_phase(device_id) → 'cold_start'
    * cfg 走 fb_playbook.resolve_params(section, phase)，失败返回 {}
    """
    phase = phase_override or ""
    if not phase and device_id:
        try:
            from src.host.fb_account_phase import get_phase as _gp
            phase = (_gp(device_id) or {}).get("phase") or ""
        except Exception as e:
            log.debug("[%s] get_phase 失败: %s", section, e)
    phase = phase or "cold_start"
    try:
        from src.host.fb_playbook import resolve_params
        cfg = resolve_params(section, phase=phase) or {}
    except Exception as e:
        log.debug("[%s] resolve_params(%s) 失败: %s", section, phase, e)
        cfg = {}
    return phase, cfg


def _with_fb_foreground(method):
    """装饰器: 业务方法执行前自动 ensure FB 在前台 + dismiss XSpace 双开。

    所有面向 task entry 的 facebook 业务方法包一层即可,避免漏改。
    """
    import functools as _ft

    @_ft.wraps(method)
    def _wrapper(self, *args, **kwargs):
        try:
            did = self._did(kwargs.get("device_id"))
            d = self._u2(did)
            self._ensure_foreground(d, did)
        except Exception as e:
            log.warning("[%s] ensure_foreground 阶段异常(继续执行业务): %s",
                        method.__name__, e)
        return method(self, *args, **kwargs)
    return _wrapper


class FacebookAutomation(BaseAutomation):

    PLATFORM = "facebook"
    PACKAGE = PACKAGE
    MAIN_ACTIVITY = ""

    # Sprint 3 P2 真机验证经历:
    # ① 一开始 _force_adb_fallback=True 阻止了 AutoSelector(空 dump_hierarchy)
    # ② 切到 False 用真 u2,AutoSelector 复活,但触发 worker_pool / dm.get_u2 的锁
    #    竞争死锁(任务 4 分钟无任何输出,直到取消)
    # ③ 现在 AdbFallbackDevice.dump_hierarchy() 已通过 `uiautomator dump`
    #    实现真层级提取,所以可安全切回 ADB fallback,彻底绕开 dm/u2 死锁
    _force_adb_fallback = True

    def __init__(self, device_manager=None, **kwargs):
        if device_manager is None:
            from ..device_control.device_manager import get_device_manager
            device_manager = get_device_manager()
        super().__init__(device_manager, **kwargs)

    # ── Smart Tap with Post-Tap Self-Healing (Sprint 4 P0) ─────────────────

    def smart_tap(self, target_desc: str, context: str = "",
                  device_id: Optional[str] = None) -> bool:
        """FB 专用 smart_tap: 点击成功后立刻检查是否误点把 app 切走。

        动机(Sprint 3 P3.8 add_friend 失败复盘):
          点 "Search bar or search icon" 被 fallback selector 命中了
          右上角 Messenger 💬 icon (text/desc=Search 容易命中周边元素),
          结果整个流程进了 Messenger 的 "New message" 页,后续全部对错目标。

        修复策略:
          ① 调父类 smart_tap 执行真点击(tap 后 AdbFallbackDevice 已 invalidate cache)
          ② 等 UI 稳定 (700ms) — 避免 activity 切换中途 app_current 瞬时不准
          ③ 强制 invalidate app_cache,读最新 app_current
          ④ 如果脱离 FB:先试 _handle_xspace_dialog(Select app sheet),
             不行就 BACK,再不行就 _adb_start_main_user 重启
          ⑤ **2026-04-20 真机回归二次优化**: 自愈成功后如果 app 已回到
             FB,**自动再 tap 一次**(递归深度最多 1 层),第二次 tap
             会基于最新 dump 重新解析位置 → 大概率命中真正的目标控件;
             仍漂移/失败才返回 False。避免"自愈成功但业务意图未达成 →
             上层后续步骤连锁失败"。
        """
        return self._smart_tap_with_heal(target_desc, context, device_id,
                                         _heal_retry=True)

    def _smart_tap_with_heal(self, target_desc: str, context: str,
                             device_id: Optional[str],
                             _heal_retry: bool) -> bool:
        ok = super().smart_tap(target_desc, context, device_id)
        if not ok:
            return False
        did = self._did(device_id)
        d = self._u2(did)
        try:
            import time as _t
            _t.sleep(0.7)
            try:
                d.invalidate_app_cache()
            except Exception:
                pass
            try:
                current_pkg = (d.app_current() or {}).get("package", "")
            except Exception:
                current_pkg = ""
            if not current_pkg or current_pkg == PACKAGE:
                return ok  # tap 后仍在 FB 下,正常

            log.warning("[smart_tap-heal] tap '%s' 后 app 漂移: %s != %s,启动自愈",
                        target_desc, current_pkg, PACKAGE)
            self._handle_xspace_dialog(d, did)
            _t.sleep(0.4)
            try:
                d.invalidate_app_cache()
                current_pkg = (d.app_current() or {}).get("package", "")
            except Exception:
                current_pkg = ""

            if current_pkg != PACKAGE:
                self._adb(f"shell input keyevent 4", device_id=did)
                _t.sleep(0.6)
                try:
                    d.invalidate_app_cache()
                    current_pkg = (d.app_current() or {}).get("package", "")
                except Exception:
                    current_pkg = ""

            if current_pkg != PACKAGE:
                log.warning("[smart_tap-heal] BACK 失败 (current=%s),重启 FB",
                            current_pkg)
                self._adb_start_main_user(did)
                _t.sleep(2.5)

            if _heal_retry:
                try:
                    d.invalidate_app_cache()
                    current_pkg = (d.app_current() or {}).get("package", "")
                except Exception:
                    current_pkg = ""
                if current_pkg == PACKAGE:
                    log.info("[smart_tap-heal] 已回到 FB,对 '%s' 做一次 retry tap",
                             target_desc)
                    _t.sleep(1.0)
                    return self._smart_tap_with_heal(target_desc, context,
                                                    device_id, _heal_retry=False)
            log.warning("[smart_tap-heal] '%s' 自愈后仍异常(current=%s),放弃",
                        target_desc, current_pkg)
            return False
        except Exception as e:
            log.debug("[smart_tap-heal] 自愈阶段异常: %s", e)
        return ok

    # ── Startup & Dialog Handling ──────────────────────────────────────────

    def launch(self, device_id: Optional[str] = None) -> bool:
        did = self._did(device_id)
        d = self._u2(did)
        d.app_stop(PACKAGE)
        time.sleep(1)
        # 真机修复 (P3): 用 `am start --user 0` 替代 d.app_start(PACKAGE),
        # MIUI/HyperOS 装了"应用双开"时,d.app_start 走 monkey 默认会弹
        # XSpaceResolveActivity 让用户选;`--user 0` 显式指主用户,直接拉
        # 起业务账号所在的 FB,跳过对话框,实测 100% 可靠。
        # Sprint 5 P2-3: _adb_start_main_user 默认已跑 XSpace + dismiss,
        # 这里不再重复。
        self._adb_start_main_user(did)
        return self.is_foreground(did)

    def _adb_start_main_user(self, did: str, post_dismiss: bool = True) -> None:
        """通过 ADB 显式以主用户(user 0)启动 Facebook,绕过 XSpace 选择对话框。

        Sprint 5 P2-3: 启动后默认跑一轮 XSpace 对话框兜底 + _dismiss_dialogs
        (权限请求/not now/got it 等),避免 smart_tap-heal 重启 FB 兜底路径
        回到带弹窗的首屏后,下一次 smart_tap 仍然被弹窗拦下。
        """
        try:
            self._adb(f"shell am start --user 0 -n {PACKAGE}/.LoginActivity",
                      device_id=did)
        except Exception:
            try:
                self._adb(
                    f"shell monkey --pct-syskeys 0 -p {PACKAGE} 1",
                    device_id=did,
                )
            except Exception as e:
                log.warning("[start_main] FB 启动两路均失败: %s", e)
        if not post_dismiss:
            return
        try:
            import time as _t
            _t.sleep(2.5)  # 等 FB 冷启
            try:
                d = self._u2(did)
            except Exception:
                return
            try:
                d.invalidate_app_cache()
            except Exception:
                pass
            try:
                cur = (d.app_current() or {}).get("package", "")
            except Exception:
                cur = ""
            if cur == "com.miui.securitycore":
                self._handle_xspace_dialog(d, did)
                _t.sleep(1.0)
            try:
                self._dismiss_dialogs(d, max_attempts=2, device_id=did)
            except Exception as e:
                log.debug("[start_main] 启动后 dismiss 异常: %s", e)
        except Exception as e:
            log.debug("[start_main] post_dismiss 阶段异常: %s", e)

    def _ensure_foreground(self, d, did: str, max_wait_s: int = 10) -> bool:
        """确保 Facebook 在前台,并自动处理 MIUI 双开对话框。

        所有 facebook_* 任务通用前置: 避免在 launcher / XSpace / 系统通知页
        启动业务流程,避免 AutoSelector 学错系统按钮。
        """
        try:
            current_pkg = ""
            try:
                current_pkg = (d.app_current() or {}).get("package", "")
            except Exception:
                pass
            if current_pkg == PACKAGE:
                return True
            log.info("[ensure_fg] FB 未在前台 (current=%s),启动 %s",
                     current_pkg or "?", PACKAGE)
            self._adb_start_main_user(did)
            for _ in range(max_wait_s):
                time.sleep(1.0)
                try:
                    cur = (d.app_current() or {}).get("package", "")
                except Exception:
                    continue
                if cur == PACKAGE:
                    time.sleep(1.5)
                    return True
                # MIUI 弹了"应用双开请选择"对话框
                if cur == "com.miui.securitycore":
                    if self._handle_xspace_dialog(d, did):
                        time.sleep(2.0)
                        try:
                            if (d.app_current() or {}).get("package", "") == PACKAGE:
                                return True
                        except Exception:
                            pass
            return False
        except Exception as e:
            log.warning("[ensure_fg] 失败: %s", e)
            return False

    def _handle_xspace_dialog(self, d, did: str) -> bool:
        """MIUI/HyperOS 装了"应用双开"后,启动 FB / Messenger 等会弹"用哪个开"对话框。

        实测两种弹法:
          a) 启动 FB 时 securitycore/XSpaceResolveActivity (深色全屏)
          b) FB 内点 Messenger 按钮时 PackageInstaller / 系统 Select app
             弹底部 sheet (浅色),两个 Messenger 图标 + Cancel
        策略: 优先点第 1 个图标(主用户空间);失败则 BACK + 强停 securitycore。
        """
        try:
            cur = (d.app_current() or {}).get("package", "")
        except Exception:
            return False
        is_xspace_full = cur == "com.miui.securitycore"
        is_select_app_sheet = False
        if not is_xspace_full:
            # 检测 b) 浅色 Select app 对话框
            try:
                if d(text="Select app").exists(timeout=0.4) or d(text="选择应用").exists(timeout=0.4):
                    is_select_app_sheet = True
            except Exception:
                pass
        if not (is_xspace_full or is_select_app_sheet):
            return False
        # 文本层匹配(覆盖中/英 + 双开变体)
        for txt in ("Original app", "原应用", "Original", "应用", "Facebook",
                    "Messenger", "App"):
            try:
                btn = d(text=txt)
                if btn.exists(timeout=0.4):
                    info = btn.info or {}
                    bounds = info.get("bounds") or {}
                    cx = (bounds.get("left", 0) + bounds.get("right", 0)) // 2
                    cy = (bounds.get("top", 0) + bounds.get("bottom", 0)) // 2
                    if cx > 0 and cy > 0:
                        self.hb.tap(d, cx, cy, device_id=did)
                        log.info("[xspace] 点击 '%s' dismiss 双开/选择对话框", txt)
                        time.sleep(1.0)
                        return True
            except Exception:
                continue
        # 兜底: BACK 关闭对话框
        try:
            self._adb("shell input keyevent 4", device_id=did)
            time.sleep(0.5)
            if is_xspace_full:
                self._adb("shell am force-stop com.miui.securitycore",
                          device_id=did)
                time.sleep(0.5)
                self._adb_start_main_user(did)
            log.info("[xspace] BACK 兜底 (xspace_full=%s sheet=%s)",
                     is_xspace_full, is_select_app_sheet)
            return True
        except Exception as e:
            log.warning("[xspace] 兜底失败: %s", e)
            return False

    def _dismiss_dialogs(self, d, max_attempts: int = 5, device_id: str = ""):
        """Dismiss common Facebook popups (permissions, notifications, etc.).

        Sprint 3 P3 真机加固: 任务执行过程中随时可能弹 MIUI "Select app"
        sheet(双开时点 Messenger 触发),必须先处理再跑通用 dismiss。
        """
        did = device_id or getattr(self, "_current_device", "")
        for _ in range(max_attempts):
            dismissed = False
            # 优先:MIUI XSpace/双开对话框 — 一旦出现业务流程都会卡死
            try:
                if self._handle_xspace_dialog(d, did):
                    dismissed = True
                    time.sleep(0.5)
                    continue
            except Exception:
                pass
            for text in _FB_DISMISS_TEXTS:
                btn = d(text=text)
                if btn.exists(timeout=0.5):
                    self.hb.tap(d, *self._el_center(btn))
                    time.sleep(0.8)
                    dismissed = True
                    break
            if not dismissed:
                break

    def _detect_risk_dialog(self, d) -> Tuple[bool, str]:
        """检测当前界面是否有真实的风控/验证对话框 (Sprint 3 P2 真机加固版)。

        三重防误报:
          1. 关键词更长更专,避免命中 Feed 内零散文本(如旧 "Suspicious activity")
          2. 必须同时存在 "确认按钮"(Continue/Confirm 等),纯文本不计
          3. 1.6s 后二次校验,排除 FB 启动瞬间的教育弹窗 / 一闪而过提示

        检测到时:
          - 推送 facebook.risk_detected 事件(供 dashboard 实时响应)
          - 写设备状态 facebook_risk_status=red

        Returns:
            (is_risk, message): is_risk=True 表示遇到真实持续的限制
        """
        # Sprint 3 P3 加固:先尝试 dismiss MIUI XSpace/Select app sheet,
        # 它会挡在业务流程中间,让后面 smart_tap 全部 MISS。
        try:
            self._handle_xspace_dialog(d,
                                       getattr(self, "_current_device", ""))
        except Exception:
            pass
        hit_kw = ""
        try:
            for kw in _FB_RISK_KEYWORDS:
                if d(textContains=kw).exists(timeout=_RISK_DETECT_PROBE_TIMEOUT):
                    hit_kw = kw
                    break
        except Exception:
            return False, ""

        if not hit_kw:
            return False, ""

        try:
            has_button = False
            for btn in _FB_RISK_BUTTONS:
                if d(text=btn).exists(timeout=_RISK_DETECT_PROBE_TIMEOUT):
                    has_button = True
                    break
            if not has_button:
                log.info("[risk] '%s' 命中但无确认按钮,判定为误报(可能是 Feed 文本)", hit_kw)
                return False, ""
        except Exception:
            return False, ""

        try:
            time.sleep(_RISK_DETECT_VERIFY_DELAY)
            if not d(textContains=hit_kw).exists(timeout=_RISK_DETECT_PROBE_TIMEOUT):
                log.info("[risk] '%s' 1.6s 后已消失,判定为瞬时弹窗(误报)", hit_kw)
                return False, ""
        except Exception:
            pass

        self._report_risk(hit_kw, device_id_hint=getattr(d, "serial", "") or getattr(d, "_serial", ""))
        return True, hit_kw

    def _report_risk(self, message: str, device_id_hint: str = ""):
        """上报风控事件 + 标记设备状态。

        device_id_hint: 调用方可传入已知 device_id,避免 _did() 拿不到。
        """
        did = device_id_hint or ""
        if not did:
            try:
                did = self._did(None)
            except Exception:
                did = ""
        if not did:
            log.warning("[risk] 无法获取 device_id,跳过状态写入(避免 device_id='' 垃圾行) message=%s", message)
            return
        try:
            from src.host.event_stream import push_event
            push_event("facebook.risk_detected", {
                "device_id": did,
                "message": message,
            }, did)
        except Exception:
            pass
        try:
            from src.host.device_state import DeviceStateStore
            ds = DeviceStateStore(platform="facebook")
            ds.set(did, "risk_status", "red")
            ds.set(did, "last_risk_message", message[:200])
        except Exception:
            log.debug("[risk] DeviceStateStore 写入失败,跳过", exc_info=True)
        # P0-3: 风控事件落库（debounce 60s，同类型短时间内只记一条）
        try:
            from src.host.fb_store import record_risk_event
            # 尝试拿当前任务 id（executor 通过 threadlocal 写入）
            try:
                from src.host.executor import _get_current_task_id
                tid = _get_current_task_id() or ""
            except Exception:
                tid = ""
            record_risk_event(did, message, task_id=tid, debounce_seconds=60)
        except Exception:
            log.debug("[risk] fb_risk_events 写入失败,跳过", exc_info=True)
        # P1-2: 触发账号状态机，必要时自动迁入 cooldown
        try:
            from src.host.fb_account_phase import on_risk as _fb_phase_on_risk
            _fb_phase_on_risk(did)
        except Exception:
            log.debug("[risk] fb_account_phase on_risk 失败", exc_info=True)
        log.warning("[FB Risk] 设备 %s: %s", did[:12], message)

    # ── Core Actions ──────────────────────────────────────────────────────

    def _classify_candidate(self, candidate: str) -> Tuple[str, str]:
        """判断 candidate 是 URL / username / user_id / display_name。

        返回 (kind, normalized)
          kind ∈ { "url", "username", "user_id", "display_name" }
          normalized:
            - url        → 原 URL（https:// 或 http://）
            - username   → m.facebook.com 可直接拼的 username（不含 /）
            - user_id    → 纯数字
            - display_name → 原字符串（需要走 search_people）
        """
        import re as _re
        s = (candidate or "").strip()
        if not s:
            return "display_name", s
        # 1) http(s):// URL
        if s.lower().startswith(("http://", "https://")):
            return "url", s
        # 2) fb:// / facebook.com/... 裸域路径
        if s.lower().startswith(("facebook.com/", "m.facebook.com/", "www.facebook.com/")):
            return "url", "https://" + s
        # 3) 纯数字 → user_id
        if s.isdigit() and 6 <= len(s) <= 20:
            return "user_id", s
        # 4) username：字母数字.下划线，3~50 长度，不含空格，不是纯数字
        #    Facebook 规则：字母数字 + 点，至少 5 字符。收紧到至少含一个字母或点。
        if _re.fullmatch(r"[A-Za-z0-9._-]{3,50}", s) and not s.isdigit():
            # 避免把 "Tanaka Yumi"（含空格）误判，上面正则已不含空格
            # 避免把中日文名误判（因为包含中日文字符会 fullmatch 失败）
            return "username", s
        # 5) 其他（含空格、非拉丁字符等）→ display_name
        return "display_name", s

    def navigate_to_profile(self, candidate: str,
                            device_id: Optional[str] = None,
                            post_open_dwell_sec: Tuple[float, float] = (2.5, 4.0),
                            ) -> Dict[str, Any]:
        """Sprint E-1.1: 打开 FB 用户主页，优先 deep-link 绕开搜索 UI。

        对 URL / username / user_id 直接 ``am start -a VIEW`` 到 m.facebook.com
        （不依赖 uiautomator dump，MIUI 上也能用）。对纯显示名降级到
        ``search_people + 点第一条`` 的传统路径（需要 dump；MIUI 上会失败）。

        返回::
            {
              "ok": bool,                   # 是否成功打开某个 profile 页
              "kind": str,                  # url/username/user_id/display_name
              "via": str,                   # deeplink / search
              "target_key": str,            # 去重键（见下）
              "url": str,                   # deeplink 时的 URL
              "reason": str,                # ok=False 时的失败原因
            }

        target_key 规则（保证跨运行稳定，便于去重）:
            - url       → "url:<规范化 URL 去掉 query/fragment>"
            - username  → "user:<username>"
            - user_id   → "uid:<id>"
            - display_name → "search:<name>"
        """
        did = self._did(device_id)
        kind, norm = self._classify_candidate(candidate)
        lo_d, hi_d = float(post_open_dwell_sec[0]), float(post_open_dwell_sec[1])

        # ── deep-link 路径 ───────────────────────────────────
        if kind in ("url", "username", "user_id"):
            if kind == "url":
                url = norm
                # 统一去 query/fragment 做 target_key 更稳定
                try:
                    from urllib.parse import urlparse
                    u = urlparse(url)
                    tk_url = f"{u.scheme}://{u.netloc}{u.path}".rstrip("/")
                except Exception:
                    tk_url = url
                target_key = f"url:{tk_url}"
            elif kind == "username":
                url = f"https://m.facebook.com/{norm}"
                target_key = f"user:{norm}"
            else:  # user_id
                url = f"https://m.facebook.com/profile.php?id={norm}"
                target_key = f"uid:{norm}"

            try:
                res = self.open_mfacebook_deeplink(url, did, dwell_sec=(lo_d, hi_d))
                if not res.get("ok"):
                    log.warning("[navigate_to_profile] deeplink 失败: %s", res.get("reason"))
                    return {"ok": False, "kind": kind, "via": "deeplink",
                            "target_key": target_key, "url": url,
                            "reason": str(res.get("reason", "") or "deeplink_fail")}
                return {"ok": True, "kind": kind, "via": "deeplink",
                        "target_key": target_key, "url": url, "reason": ""}
            except Exception as e:
                log.warning("[navigate_to_profile] deeplink 异常: %s", e)
                return {"ok": False, "kind": kind, "via": "deeplink",
                        "target_key": target_key, "url": url,
                        "reason": f"deeplink_exc:{type(e).__name__}"}

        # ── display_name 传统路径 ─────────────────────────────
        target_key = f"search:{norm}"
        try:
            results = self.search_people(norm, did, max_results=3)
            if not results:
                return {"ok": False, "kind": "display_name", "via": "search",
                        "target_key": target_key, "url": "",
                        "reason": "search_no_result"}
            d = self._u2(did)
            first = self._first_search_result_element(d)
            if first is None:
                return {"ok": False, "kind": "display_name", "via": "search",
                        "target_key": target_key, "url": "",
                        "reason": "search_no_clickable"}
            self.hb.tap(d, *self._el_center(first))
            time.sleep(random.uniform(lo_d, hi_d))
            return {"ok": True, "kind": "display_name", "via": "search",
                    "target_key": target_key, "url": "", "reason": ""}
        except Exception as e:
            log.warning("[navigate_to_profile] search 失败: %s", e)
            return {"ok": False, "kind": "display_name", "via": "search",
                    "target_key": target_key, "url": "",
                    "reason": f"search_exc:{type(e).__name__}"}

    def open_mfacebook_deeplink(self, url: str,
                                device_id: Optional[str] = None,
                                dwell_sec: Tuple[float, float] = (2.0, 3.5),
                                ) -> Dict[str, Any]:
        """用 Facebook App 打开 m.facebook.com 系 URL（adb VIEW，不依赖 dump）。

        与 ``navigate_to_profile`` 的 deep-link 块逻辑一致，供搜索页 / 专题页复用。
        """
        did = self._did(device_id)
        lo, hi = float(dwell_sec[0]), float(dwell_sec[1])
        try:
            cmd = (f'shell am start -a android.intent.action.VIEW '
                   f'-d "{url}" -n com.facebook.katana/com.facebook.katana.IntentUriHandler '
                   f'--activity-clear-top')
            out = self._adb(cmd, device_id=did, timeout=10) or ""
            if "Error" in out or "not found" in out.lower() or "does not exist" in out.lower():
                self._adb(
                    f'shell am start -a android.intent.action.VIEW -d "{url}" com.facebook.katana',
                    device_id=did, timeout=10,
                )
            time.sleep(random.uniform(lo, hi))
            chk = self._adb("shell dumpsys window | grep mCurrentFocus",
                            device_id=did, timeout=6) or ""
            in_fb = ("com.facebook.katana" in chk) or ("com.facebook.orca" in chk)
            if not in_fb:
                return {"ok": False, "reason": "foreground_not_fb", "raw": chk.strip()[:160]}
            return {"ok": True, "reason": ""}
        except Exception as e:
            return {"ok": False, "reason": f"deeplink_exc:{type(e).__name__}", "raw": str(e)}

    def _fetch_device_interest_topics(self, device_id: str,
                                      persona_key: Optional[str],
                                      hours: int, limit: int) -> List[Dict[str, Any]]:
        """读本地 SQLite：本设备在 fb_content_exposure 里的 topic 热榜。"""
        rows_out: List[Dict[str, Any]] = []
        try:
            from src.host.database import get_conn
            since = f"-{int(max(1, min(hours, 24 * 90)))} hours"
            sql = (
                "SELECT topic, COUNT(*) AS n FROM fb_content_exposure "
                "WHERE seen_at >= datetime('now', ?) AND device_id = ?"
            )
            params: List[Any] = [since, device_id]
            pk = (persona_key or "").strip()
            if pk:
                sql += " AND meta_json LIKE ?"
                params.append(f'%"persona_key": "{pk}"%')
            sql += " GROUP BY topic ORDER BY n DESC LIMIT ?"
            params.append(int(max(1, min(limit, 50))))
            with get_conn() as conn:
                rows = conn.execute(sql, params).fetchall()
            for r in rows:
                t = (r[0] or "").strip()
                if not t or t.lower() == "other":
                    continue
                rows_out.append({"topic": t, "count": int(r[1] or 0)})
        except Exception as e:
            log.warning("[_fetch_device_interest_topics] %s", e)
        return rows_out

    def search_people(self, query: str, device_id: Optional[str] = None,
                      max_results: int = 10) -> List[Dict[str, str]]:
        """Search for people and return list of found profiles."""
        did = self._did(device_id)
        d = self._u2(did)

        with self.guarded("search", device_id=did):
            if not self.smart_tap("Search bar or search icon", device_id=did):
                self._fallback_search_tap(d)

            time.sleep(0.8)
            self.hb.type_text(d, query)
            time.sleep(1.5)

            d.press("enter")
            time.sleep(2)

            self.smart_tap("People tab or filter", device_id=did)
            time.sleep(1.5)

        results = self._extract_search_results(d, max_results)
        return results

    @_with_fb_foreground
    def send_message(self, recipient: str, message: str,
                     device_id: Optional[str] = None) -> bool:
        """Send a message via Messenger."""
        did = self._did(device_id)
        d = self._u2(did)

        with self.guarded("send_message", device_id=did):
            rewritten = self.rewrite_message(message, {"platform": "facebook", "recipient": recipient})

            if not self.smart_tap("Messenger or chat icon", device_id=did):
                d.app_start(MESSENGER_PACKAGE)
                time.sleep(3)
                self._dismiss_dialogs(d)

            time.sleep(1)

            if self.smart_tap("Search in Messenger", device_id=did):
                time.sleep(0.5)
                self.hb.type_text(d, recipient)
                time.sleep(1.5)

                if not self.smart_tap("First matching contact", device_id=did):
                    log.warning("Recipient not found: %s", recipient)
                    return False

                time.sleep(1)
                self.hb.type_text(d, rewritten)
                self.hb.wait_think(0.5)

                return self.smart_tap("Send message button", device_id=did)

        return False

    @_with_fb_foreground
    def add_friend(self, profile_name: str,
                   device_id: Optional[str] = None) -> bool:
        """Send a friend request from search results (legacy 直加,无安全模式)。

        ⚠ 推荐使用 add_friend_with_note(safe_mode=True) 替代,FB 风控对搜索后立即点 Add Friend 极敏感。
        本方法保留是为了向后兼容 task_type=facebook_add_friend 的旧调用。
        """
        did = self._did(device_id)
        d = self._u2(did)

        with self.guarded("add_friend", device_id=did):
            self.search_people(profile_name, did, max_results=3)
            time.sleep(1)

            if self.smart_tap("Add Friend button", device_id=did):
                log.info("Friend request sent to: %s", profile_name)
                return True

        return False

    @_with_fb_foreground
    def add_friend_with_note(self, profile_name: str,
                             note: str = "",
                             safe_mode: bool = True,
                             device_id: Optional[str] = None,
                             persona_key: Optional[str] = None,
                             phase: Optional[str] = None) -> bool:
        """带验证语的安全好友请求 — Sprint 1 新增 + 2026-04-22 persona 改造。

        相比 add_friend 的差异:
          1. safe_mode=True 时,先点击进入对方主页,停留 8-15s 模拟"看资料"
          2. 在主页找 "Add Friend" 按钮(而非搜索结果列表里的快捷按钮 — FB 风控对此更敏感)
          3. 若 note 非空且支持(部分 FB 版本会弹"加备注"框),自动填入
          4. **note 为空且 persona 要求 require_verification_note 时**,
             按 persona.country_code 从 chat_messages.yaml 随机抽一条验证语。
             这是日文客群(jp_female_midlife)的关键锚点 — 防止给日本女性发英文
             "Hi nice to meet you" 被直接忽略/举报。
          5. **phase=cold_start 时直接拒绝发送** (fb_account_phase 把新号关在笼子里),
             防止刚冷启动的号就被风控盯上。

        Args:
            profile_name: 目标姓名/搜索词
            note: 验证语(可选,部分版本支持);为空会按 persona 自动生成
            safe_mode: 是否走安全路径(进主页停留)
            device_id: 设备 ID
            persona_key: 目标客群 key(router 层已经 setdefault 注入)
            phase: 显式覆盖 phase；为空走 fb_account_phase.get_phase
        """
        did = self._did(device_id)
        d = self._u2(did)

        # P0-2: phase + playbook 参数解析
        eff_phase, ab_cfg = _resolve_phase_and_cfg("add_friend",
                                                   device_id=did,
                                                   phase_override=phase)
        # cold_start 直接拒绝（playbook 把 max_friends_per_run 设为 0）
        if int(ab_cfg.get("max_friends_per_run", 5)) <= 0:
            log.info("[add_friend_with_note] phase=%s 禁止加好友, skip: %s",
                     eff_phase, profile_name)
            return False

        # P1-2: 24h rolling 日上限（与单任务 max_friends_per_run 独立）
        daily_cap = int(ab_cfg.get("daily_cap_per_account") or 0)
        if daily_cap > 0:
            try:
                from src.host.fb_store import count_friend_requests_sent_since
                n24 = count_friend_requests_sent_since(did, hours=24)
                if n24 >= daily_cap:
                    log.info("[add_friend_with_note] 24h 已发 %s 次 ≥ daily_cap=%s, skip %s",
                             n24, daily_cap, profile_name)
                    return False
            except Exception as e:
                log.debug("[add_friend_with_note] daily_cap 检查异常(继续): %s", e)

        # P0-2: note 空 + playbook 要求携带验证语 → 从 persona 文案池抽
        require_note = bool(ab_cfg.get("require_verification_note", True))
        if not note and require_note:
            try:
                from .fb_content_assets import get_verification_note
                note = get_verification_note(persona_key=persona_key,
                                             name=profile_name)
                if note:
                    log.debug("[add_friend_with_note] 已从 persona=%s 自动生成"
                              " verification_note(len=%d)",
                              persona_key or "(default)", len(note))
            except Exception as e:
                log.debug("[add_friend_with_note] get_verification_note 失败: %s", e)

        with self.guarded("add_friend", device_id=did):
            results = self.search_people(profile_name, did, max_results=3)
            if not results:
                log.warning("[add_friend_with_note] 未找到目标: %s", profile_name)
                return False

            time.sleep(1)

            if safe_mode:
                # 安全路径: 点击第一个搜索结果进主页 → 停留 → 找主页内的 Add Friend
                first = self._first_search_result_element(d)
                if first is None:
                    log.warning("[add_friend_with_note] 无法定位首个搜索结果")
                    return False
                self.hb.tap(d, *self._el_center(first))
                time.sleep(random.uniform(2.5, 4.0))

                # 检测风控
                is_risk, msg = self._detect_risk_dialog(d)
                if is_risk:
                    log.warning("[add_friend_with_note] 检测到风控提示: %s", msg)
                    return False

                # 模拟看资料：随机滚动 1-2 次
                for _ in range(random.randint(1, 2)):
                    self.hb.scroll_down(d)
                    time.sleep(random.uniform(2.0, 4.0))

                # 总停留时长 8-15s（含上面滚动）
                self.hb.wait_read(random.randint(2000, 6000))

                # 主页内找 Add Friend
                if not self.smart_tap("Add Friend button on profile page",
                                      device_id=did):
                    if not self.smart_tap("Add Friend button", device_id=did):
                        log.info("[add_friend_with_note] 该用户无加好友按钮(可能已是好友/被限)")
                        return False
                time.sleep(1.5)
            else:
                if not self.smart_tap("Add Friend button", device_id=did):
                    return False
                time.sleep(1)

            # 部分 FB 版本会弹"加备注/Send"对话框
            if note:
                if d(textContains="Add").exists(timeout=1.0) or d(textContains="note").exists(timeout=0.5):
                    note_input = d(className="android.widget.EditText")
                    if note_input.exists(timeout=1.0):
                        try:
                            self.hb.tap(d, *self._el_center(note_input))
                            time.sleep(0.4)
                            self.hb.type_text(d, note[:200])
                            time.sleep(0.5)
                        except Exception:
                            pass
                    self.smart_tap("Send button", device_id=did)
                    time.sleep(1.0)

            log.info("[add_friend_with_note] 好友请求已发送: %s (note=%s)",
                     profile_name, bool(note))
            return True

    # ─── 2026-04-23: 加好友后打招呼（方案 A2 — 在 profile 页点 Message）──
    # 设计决策（vs 原方案 A1 "切到 Messenger App 搜名字"）:
    #   ① 刚加的人**还不是好友** → Messenger App 搜名字命中率极低
    #      (Messenger 搜索只搜已有对话 + 联系人 + 二度好友)
    #   ② profile 页的 "Message" 按钮就是 FB 真人路径,风控模型对此
    #      路径友好度远高于"切 app → 搜名字"
    #   ③ 全程停留在 com.facebook.katana,避免 MIUI XSpace "Select app"
    #      弹窗拦截(切 Messenger 是头号诱因)
    #   ④ Message 按钮打开的对话 = FB 内嵌 Messenger thread,对方看到
    #      的是"好友请求 + 消息请求",心理路径连续

    _GREETING_MESSAGE_BTN_ALTS = (
        "Message button on profile page",
        "Message button",
        "Send Message button on profile",
    )
    _GREETING_INLINE_TEXTS = (
        "Message", "MESSAGE", "Send Message", "Send message",
        "メッセージ", "メッセージを送信",   # 日文
        "Messaggio", "Invia messaggio",     # 意大利文
        "消息", "发消息", "发送消息",         # 中文
    )
    _GREETING_REQUEST_CONFIRM_TEXTS = (
        "Send", "Send Request", "SEND",
        "送信", "リクエストを送る",
        "Invia", "Invia richiesta",
        "发送", "发送请求",
    )

    def _tap_profile_message_button(self, d, did: str) -> bool:
        """在当前 profile 页上点击 Message 按钮进入内联对话。

        优先 smart_tap (走 AutoSelector 学习 + 自愈),失败降级到文本兜底。
        返回 True 表示点击成功(不保证对话页已加载,调用方需自行等待 + 校验)。
        """
        for alt in self._GREETING_MESSAGE_BTN_ALTS:
            try:
                if self.smart_tap(alt, device_id=did):
                    return True
            except Exception:
                continue
        # 文本兜底 —— 避免被 Feed 内的 "Message" TextView 误命中：
        # 限制必须是 clickable 且屏幕上半部（profile 页按钮一般在资料卡下方）
        try:
            try:
                sh = d.window_size()[1]
            except Exception:
                sh = 1920
            for txt in self._GREETING_INLINE_TEXTS:
                try:
                    btn = d(text=txt, clickable=True)
                    if btn.exists(timeout=0.6):
                        info = btn.info or {}
                        bounds = info.get("bounds") or {}
                        cx = (bounds.get("left", 0) + bounds.get("right", 0)) // 2
                        cy = (bounds.get("top", 0) + bounds.get("bottom", 0)) // 2
                        # 限定在屏幕 1/8 ~ 5/8 区域避免命中底部导航 Messenger tab
                        if cx > 0 and cy > 0 and cy < sh * 0.65:
                            self.hb.tap(d, cx, cy, device_id=did)
                            return True
                except Exception:
                    continue
        except Exception:
            pass
        return False

    def _confirm_message_request_if_any(self, d) -> bool:
        """部分 FB 版本:对非好友首次发消息会弹 "Send Message Request?" 确认框。

        仅在检测到标志性文案时点确认;未弹返回 False(无操作)。
        """
        try:
            # 标志性文案(含中英日意)
            has_request_hint = False
            for kw in ("Message Request", "Send as message request",
                       "メッセージリクエスト", "richiesta di messaggio",
                       "消息请求"):
                try:
                    if d(textContains=kw).exists(timeout=0.4):
                        has_request_hint = True
                        break
                except Exception:
                    pass
            if not has_request_hint:
                return False
            for btn_txt in self._GREETING_REQUEST_CONFIRM_TEXTS:
                try:
                    btn = d(text=btn_txt)
                    if btn.exists(timeout=0.4):
                        self.hb.tap(d, *self._el_center(btn))
                        time.sleep(0.8)
                        return True
                except Exception:
                    continue
        except Exception:
            pass
        return False

    # 归因标签(供 add_friend_and_greet / funnel 分析使用):
    #   phase_blocked / prob_gate / cap_hit / template_empty / search_miss
    #   / first_tap_miss / risk_before_msg / no_message_button / input_miss
    #   / send_miss / ok
    # 每次 send_greeting_after_add_friend 结束都会刷新 _last_greet_skip_reason。
    def _set_greet_reason(self, reason: str) -> None:
        self._last_greet_skip_reason = reason

    @_with_fb_foreground
    def send_greeting_after_add_friend(self,
                                       profile_name: str,
                                       greeting: str = "",
                                       device_id: Optional[str] = None,
                                       persona_key: Optional[str] = None,
                                       phase: Optional[str] = None,
                                       assume_on_profile: bool = True,
                                       preset_key: str = "",
                                       ai_decision: str = "greeting") -> bool:
        """加好友之后在 profile 页点 "Message" 发一条打招呼消息(方案 A2)。

        本方法**假设调用之前已成功 add_friend_with_note** —— 实际流程:
            add_friend_with_note(name) → 成功 → send_greeting_after_add_friend(name)

        Args:
            profile_name: 目标姓名(已在 profile 页时仅用于日志/文案 {name})
            greeting: 具体发送的文本;为空则按 persona 从 chat_messages.yaml 抽
            device_id: 设备 ID
            persona_key: 客群 key(决定打招呼语种 / 语气)
            phase: 显式覆盖 phase
            assume_on_profile: True=调用前已在目标 profile 页(add_friend 之后);
                               False=从当前页重走 search_people → 进 profile → 打招呼
            preset_key: 透传到 fb_inbox_messages.preset_key,便于漏斗切片
            ai_decision: 写库时的 decision tag,默认 "greeting";
                         方便与 auto_reply / wa_referral 等区分统计

        Returns:
            True=已发送(数据已入库); False=被闸门/上限/UI 失败拒绝
        """
        import random as _r
        did = self._did(device_id)
        d = self._u2(did)
        # 默认把归因置空,走到 return True 时再设 "ok"
        self._set_greet_reason("")

        # Phase + playbook
        eff_phase, sg_cfg = _resolve_phase_and_cfg("send_greeting",
                                                   device_id=did,
                                                   phase_override=phase)
        # 冷启/冷却 phase 直接拒绝（YAML 写 max_greetings_per_run=0）
        if int(sg_cfg.get("max_greetings_per_run", 0)) <= 0:
            log.info("[send_greeting] phase=%s 禁止打招呼, skip: %s",
                     eff_phase, profile_name)
            self._set_greet_reason("phase_blocked")
            return False

        # 概率闸：支持 A/B 抽样（默认 1.0 必发）
        enabled_p = float(sg_cfg.get("enabled_probability", 1.0) or 0.0)
        if enabled_p <= 0.0 or (enabled_p < 1.0 and _r.random() > enabled_p):
            log.info("[send_greeting] 概率门未命中(p=%.2f), skip: %s",
                     enabled_p, profile_name)
            self._set_greet_reason("prob_gate")
            return False

        # 24h rolling 日上限（与 add_friend.daily_cap 独立计）
        daily_cap = int(sg_cfg.get("daily_cap_per_account") or 0)
        if daily_cap > 0:
            try:
                from src.host.fb_store import count_outgoing_messages_since
                n24 = count_outgoing_messages_since(did, hours=24,
                                                    ai_decision=ai_decision)
                if n24 >= daily_cap:
                    log.info("[send_greeting] 24h 已发 %s 条 ≥ daily_cap=%s, skip %s",
                             n24, daily_cap, profile_name)
                    self._set_greet_reason("cap_hit")
                    return False
            except Exception as e:
                log.debug("[send_greeting] daily_cap 检查异常(继续): %s", e)

        # 文案：空则按 persona 从 chat_messages.yaml 抽(并记录 template_id 供 A/B)
        require_template = bool(sg_cfg.get("require_persona_template", True))
        template_id = ""  # 显式传入 greeting 时 tid 留空(运营自定义的不参与 A/B 统计)
        if not greeting:
            try:
                from .fb_content_assets import get_greeting_message_with_id
                greeting, template_id = get_greeting_message_with_id(
                    persona_key=persona_key, name=profile_name)
            except Exception as e:
                log.debug("[send_greeting] get_greeting_message_with_id 失败: %s", e)
                greeting, template_id = "", ""
        if not greeting:
            if require_template:
                log.info("[send_greeting] persona=%s 无打招呼模板且 require_persona_template=true,"
                         " 跳过避免发错语种: %s",
                         persona_key or "(default)", profile_name)
                self._set_greet_reason("template_empty")
                return False
            greeting = f"Hi {profile_name}!"
            # 兜底英文问候不记 template_id —— 防止污染 greeting_template_distribution
            # 统计。本质上这条"不该被发出"(persona 无本地化模板),只是作为硬兜底,
            # 不纳入 A/B 样本。
            template_id = ""

        # 非 assume_on_profile: 先重新搜索 + 进 profile（独立使用场景）
        if not assume_on_profile:
            results = self.search_people(profile_name, did, max_results=3)
            if not results:
                log.warning("[send_greeting] 未找到目标: %s", profile_name)
                self._set_greet_reason("search_miss")
                return False
            first = self._first_search_result_element(d)
            if first is None:
                log.warning("[send_greeting] 无法定位首个搜索结果: %s", profile_name)
                self._set_greet_reason("first_tap_miss")
                return False
            self.hb.tap(d, *self._el_center(first))
            time.sleep(random.uniform(2.5, 4.0))
            is_risk, msg = self._detect_risk_dialog(d)
            if is_risk:
                log.warning("[send_greeting] 检测到风控提示: %s", msg)
                self._set_greet_reason("risk_before_msg")
                return False

        with self.guarded("send_greeting", device_id=did):
            # 加好友后等一段时间再点 Message（真人节奏）
            if assume_on_profile:
                lo, hi = sg_cfg.get("post_add_friend_wait_sec") or (8, 18)
                time.sleep(random.uniform(float(lo), float(hi)))

            # 风控二次检测（可能加好友后弹 identity verification）
            is_risk, msg = self._detect_risk_dialog(d)
            if is_risk:
                log.warning("[send_greeting] profile 页检测到风控,放弃打招呼: %s", msg)
                self._set_greet_reason("risk_before_msg")
                return False

            # 点 Message 按钮 —— 进内联对话
            if not self._tap_profile_message_button(d, did):
                # 可选降级: 走 Messenger App 路径(allow_messenger_fallback=true 时)
                if bool(sg_cfg.get("allow_messenger_fallback", False)):
                    log.info("[send_greeting] profile 无 Message 按钮,降级 Messenger 路径: %s",
                             profile_name)
                    fallback_ok = False
                    try:
                        fallback_ok = self.send_message(profile_name, greeting,
                                                        device_id=did)
                    except Exception as e:
                        log.debug("[send_greeting] Messenger fallback 异常: %s", e)
                    if fallback_ok:
                        # 入库标注来源为 fallback,保留统计可区分
                        try:
                            from src.host.fb_store import record_inbox_message
                            record_inbox_message(
                                did, profile_name,
                                peer_type="friend_request",
                                message_text=greeting,
                                direction="outgoing",
                                ai_decision=ai_decision,
                                ai_reply_text=greeting,
                                preset_key=preset_key or "",
                                template_id=(template_id or "") + "|fallback",
                            )
                        except Exception:
                            pass
                        self._set_greet_reason("ok_via_fallback")
                        log.info("[send_greeting] Messenger fallback 成功: %s", profile_name)
                        return True
                    self._set_greet_reason("no_message_button_fallback_miss")
                    return False
                log.info("[send_greeting] 未找到 Message 按钮(profile 可能无此入口): %s",
                         profile_name)
                self._set_greet_reason("no_message_button")
                return False
            time.sleep(random.uniform(2.0, 3.5))

            # 可能弹 "Send Message Request?" 确认框（对非好友首次发）—— 先处理输入再确认
            # 部分版本顺序: 打开对话 → 输入 → 点 Send → 弹 Request 确认框
            # 部分版本顺序: 打开对话 → 立即弹确认框 → 同意后才显示输入
            # 所以这里先试一次: 若当前是确认框, 先点确认再继续
            self._confirm_message_request_if_any(d)

            # 打开对话页后的思考时间（像真人打字前的停顿）
            tlo, thi = sg_cfg.get("think_before_type_sec") or (3, 7)
            time.sleep(random.uniform(float(tlo), float(thi)))

            # 输入文字 + 发送
            try:
                input_box = d(className="android.widget.EditText")
                if not input_box.exists(timeout=3.0):
                    log.warning("[send_greeting] 未找到输入框,放弃: %s", profile_name)
                    self._set_greet_reason("input_miss")
                    return False
                self.hb.tap(d, *self._el_center(input_box))
                time.sleep(random.uniform(0.4, 0.9))
                self.hb.type_text(d, greeting[:300])
                time.sleep(random.uniform(0.8, 1.6))
            except Exception as e:
                log.warning("[send_greeting] 输入阶段异常: %s", e)
                self._set_greet_reason("input_miss")
                return False

            send_ok = False
            try:
                send_ok = self.smart_tap("Send message button", device_id=did)
            except Exception:
                send_ok = False
            if not send_ok:
                # 兜底: 回车键触发发送
                try:
                    d.press("enter")
                    time.sleep(0.5)
                    send_ok = True
                except Exception:
                    pass
            if not send_ok:
                log.warning("[send_greeting] 未能点击发送按钮: %s", profile_name)
                self._set_greet_reason("send_miss")
                return False
            time.sleep(random.uniform(1.0, 2.0))

            # 再次处理可能"发送后"才弹的 Send as Message Request 确认
            # 注意: 必须在 time.sleep 让 UI 渲染之后调用,否则对话框还没弹就会 miss
            self._confirm_message_request_if_any(d)

        # 数据入库: facebook_inbox_messages (direction=outgoing, decision=greeting)
        # 2026-04-23: 带 template_id 供 A/B 分析
        try:
            from src.host.fb_store import record_inbox_message
            record_inbox_message(
                did, profile_name,
                peer_type="friend_request",   # 刚发好友请求还未接受
                message_text=greeting,
                direction="outgoing",
                ai_decision=ai_decision,
                ai_reply_text=greeting,
                preset_key=preset_key or "",
                template_id=template_id or "",
            )
        except Exception as e:
            log.debug("[send_greeting] 入库失败(不影响主流程): %s", e)

        log.info("[send_greeting] 已向 %s 发送打招呼(len=%d, persona=%s, phase=%s)",
                 profile_name, len(greeting), persona_key or "(default)", eff_phase)
        self._set_greet_reason("ok")

        # 返回 profile 页,给上层调用方提供稳定的锚点 (BACK 一次)
        try:
            self._adb("shell input keyevent 4", device_id=did)
            time.sleep(0.8)
        except Exception:
            pass
        return True

    @_with_fb_foreground
    def add_friend_and_greet(self,
                             profile_name: str,
                             note: str = "",
                             greeting: str = "",
                             device_id: Optional[str] = None,
                             persona_key: Optional[str] = None,
                             phase: Optional[str] = None,
                             preset_key: str = "",
                             greet_on_failure: bool = False) -> Dict[str, Any]:
        """一体化: 搜索 → 加好友(带验证语) → 打招呼 DM(同 profile 页)。

        这是**方案 A2** 的默认入口 —— 把两个原子动作组合,让上层调用只需
        传一个名字。每一步失败都会在返回 dict 里体现,方便漏斗/审计。

        Args:
            profile_name: 目标姓名
            note: 加好友验证语(空则 persona 自动生成)
            greeting: 打招呼文案(空则 persona 自动生成)
            persona_key: 客群 key
            phase: 显式覆盖 phase(add_friend / send_greeting 共用)
            preset_key: 透传给两步入库
            greet_on_failure: add_friend 失败时是否仍然尝试打招呼
                              (默认 False —— 未加好友就发消息 = 极高风控风险,
                               只在特殊调试场景开启)

        Returns:
            {
              "add_friend_ok": bool,
              "greet_ok": bool,
              "greet_skipped_reason": str,   # 为何没打招呼(如 "add_friend_failed" / "cap")
              "profile_name": str,
            }
        """
        out: Dict[str, Any] = {
            "add_friend_ok": False,
            "greet_ok": False,
            "greet_skipped_reason": "",
            "profile_name": profile_name,
        }

        add_ok = self.add_friend_with_note(
            profile_name,
            note=note,
            safe_mode=True,
            device_id=device_id,
            persona_key=persona_key,
            phase=phase,
        )
        out["add_friend_ok"] = bool(add_ok)

        if not add_ok and not greet_on_failure:
            out["greet_skipped_reason"] = "add_friend_failed"
            return out

        greet_ok = self.send_greeting_after_add_friend(
            profile_name,
            greeting=greeting,
            device_id=device_id,
            persona_key=persona_key,
            phase=phase,
            assume_on_profile=True,
            preset_key=preset_key,
            ai_decision="greeting",
        )
        out["greet_ok"] = bool(greet_ok)
        # 细化原因: send_greeting_after_add_friend 已挂 _last_greet_skip_reason
        reason = getattr(self, "_last_greet_skip_reason", "") or ""
        if greet_ok:
            out["greet_skipped_reason"] = ""   # 成功时清空
        else:
            out["greet_skipped_reason"] = reason or "greet_failed"
        return out

    @_with_fb_foreground
    def _run_feed_scroll_phase(self, d, did: str, target_scrolls: int,
                               like_p: float, cfg: Dict[str, Any],
                               stats: Dict[str, Any]) -> None:
        """browse_feed 内层 scroll+like 循环（Sprint F 抽离供 interest feed 复用）。"""
        for i in range(target_scrolls):
            is_risk, msg = self._detect_risk_dialog(d)
            if is_risk:
                log.warning("[browse_feed] 检测到风控提示,提前结束: %s", msg)
                stats["risk_detected"] = msg
                break

            with self.guarded("browse_feed", device_id=did, weight=0.3):
                pull_p = float(cfg.get("pull_refresh_prob",
                                        FB_BROWSE_DEFAULTS["pull_refresh_prob"]))
                if i < target_scrolls // 4 and random.random() < pull_p:
                    try:
                        d.swipe(0.5, 0.3, 0.5, 0.8, duration=0.4)
                        dwell = random.uniform(1.5, 3.0)
                        time.sleep(dwell)
                        stats["pull_refreshes"] += 1
                        stats["dwell_seconds_total"] += dwell
                    except Exception:
                        pass

                self.hb.scroll_down(d)

                video_p = float(cfg.get("video_dwell_prob",
                                     FB_BROWSE_DEFAULTS["video_dwell_prob"]))
                if random.random() < video_p:
                    lo, hi = cfg.get("video_dwell_ms",
                                     FB_BROWSE_DEFAULTS["video_dwell_ms"])
                    ms = random.randint(int(lo), int(hi))
                    stats["video_dwells"] += 1
                else:
                    lo, hi = cfg.get("short_wait_ms",
                                     FB_BROWSE_DEFAULTS["short_wait_ms"])
                    ms = random.randint(int(lo), int(hi))
                self.hb.wait_read(ms)
                stats["dwell_seconds_total"] += ms / 1000.0
                stats["scrolls"] += 1

                if random.random() < like_p:
                    if self.smart_tap("Like button on a post", device_id=did):
                        stats["likes"] += 1
                        self.hb.wait_between_actions(1.5)
                        stats["dwell_seconds_total"] += 1.5

    def browse_feed(self,
                    scroll_count: Optional[int] = None,
                    like_probability: Optional[float] = None,
                    duration_minutes: Optional[int] = None,
                    phase: Optional[str] = None,
                    device_id: Optional[str] = None) -> Dict[str, Any]:
        """Browse news feed with natural behavior (P0 + P1-1 版)。

        改动要点:
          1. 节奏公式: scroll_count 由 duration_minutes × scroll_per_min 反推
          2. 每屏停留分布按 playbook 里的 short_wait_ms 抽样
          3. video_dwell_prob 概率模拟"看视频/长图文"长停留
          4. pull_refresh_prob 概率下拉刷新
          5. `smart_tap("Home tab")` 失败走 press home → app_start 回退链，
             三层仍失败则 raise `FbWarmupError(code=fb.home_tab_not_found)`
          6. 结构化输出: 返回 dict 含 `card_type="fb_warmup"`

        P1-1 变化: 节奏参数从 FB_BROWSE_DEFAULTS 常量 → 读 config/facebook_playbook.yaml，
          热加载 + 按 phase（cold_start/growth/mature/cooldown）分档覆盖。
          显式传入 scroll_count / like_probability 仍然优先。
        """
        did = self._did(device_id)
        d = self._u2(did)

        # P1-1: 按 phase 读 playbook；phase 为空则用 defaults
        cfg = _load_browse_feed_cfg(phase=phase)
        like_p = (float(cfg.get("like_probability", FB_BROWSE_DEFAULTS["like_probability"]))
                  if like_probability is None else float(like_probability))
        target_scrolls = _resolve_scroll_count(duration_minutes, scroll_count, cfg=cfg)

        stats: Dict[str, Any] = {
            "card_type": "fb_warmup",
            "phase": phase or "",
            "config_source": "playbook" if cfg is not FB_BROWSE_DEFAULTS else "defaults",
            "target_scrolls": target_scrolls,
            "scrolls": 0,
            "likes": 0,
            "video_dwells": 0,
            "pull_refreshes": 0,
            "dwell_seconds_total": 0.0,
            "home_tab_fallback": "",
            "duration_minutes": duration_minutes or 0,
            "started_at": _now_iso(),
        }

        # P0-2: Home tab 失败回退链 —— silent fail 导致后面滑错页面
        if not self._ensure_home_feed(d, did, stats):
            stats["narrative"] = "无法进入 Home Feed，任务中止"
            raise FbWarmupError(
                code="fb.home_tab_not_found",
                message="定位 Home tab 失败，连重启 FB 都未能进入首页",
                hint="更新 data/selectors/com_facebook_katana.yaml 中 'Home tab' 选择器；或设备重装 FB 后重试",
            )

        time.sleep(random.uniform(0.8, 1.8))

        self._run_feed_scroll_phase(d, did, target_scrolls, like_p, cfg, stats)

        stats["finished_at"] = _now_iso()
        stats["like_rate_actual"] = (stats["likes"] / max(1, stats["scrolls"]))
        stats["minutes_equivalent"] = round(stats["dwell_seconds_total"] / 60.0, 1)
        stats["narrative"] = (
            f"滑动 {stats['scrolls']} 屏 · 点赞 {stats['likes']} · "
            f"看视频 {stats['video_dwells']} 次 · 约 {stats['minutes_equivalent']} 分钟真人刷 feed"
        )
        if stats.get("risk_detected"):
            stats["narrative"] += f" · ⚠ 风控中断: {stats['risk_detected']}"

        # P1-2: 把本次战绩同步给账号状态机，可能触发 phase 迁移
        try:
            from src.host.fb_account_phase import on_scrolls as _fb_phase_on_scrolls
            transition = _fb_phase_on_scrolls(did, stats["scrolls"], stats["likes"])
            if transition:
                stats["phase_transition"] = transition
                if transition.get("changed"):
                    stats["narrative"] += f" · 🎯 phase: {transition['from']} → {transition['to']}"
        except Exception:
            log.debug("[browse_feed] phase hook 失败", exc_info=True)

        return stats

    def browse_feed_by_interest(
        self,
        persona_key: Optional[str] = "",
        interest_hours: int = 168,
        max_topics: int = 4,
        like_boost: float = 0.12,
        scroll_count: Optional[int] = None,
        duration_minutes: Optional[int] = 15,
        phase: Optional[str] = None,
        device_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Sprint F: 按 ``fb_content_exposure`` 热榜 topic 分段 deep-link 搜索页 + scroll/like。

        无本地兴趣数据时退化为普通 ``browse_feed``（同一风控与 phase 节奏）。
        """
        from urllib.parse import quote

        did = self._did(device_id)
        d = self._u2(did)
        cfg = _load_browse_feed_cfg(phase=phase)
        base_like = float(cfg.get("like_probability", FB_BROWSE_DEFAULTS["like_probability"]))
        like_p = min(0.55, base_like + max(0.0, float(like_boost)))

        total_dm = int(duration_minutes or 15)
        total_dm = max(5, total_dm)
        pk = (persona_key or "").strip() or None
        ih = int(max(1, min(int(interest_hours), 24 * 90)))
        mt = int(max(1, min(int(max_topics), 12)))

        topics = self._fetch_device_interest_topics(did, pk, ih, mt)

        stats: Dict[str, Any] = {
            "card_type": "fb_interest_feed",
            "phase": phase or "",
            "persona_key": pk or "",
            "interest_hours": ih,
            "topics_from_db": topics,
            "segments": [],
            "fallback_no_topics": False,
            "scrolls": 0,
            "likes": 0,
            "video_dwells": 0,
            "pull_refreshes": 0,
            "dwell_seconds_total": 0.0,
            "started_at": _now_iso(),
            "duration_minutes": total_dm,
        }

        if not topics:
            stats["fallback_no_topics"] = True
            inner = self.browse_feed(
                scroll_count=scroll_count,
                like_probability=None,
                duration_minutes=total_dm,
                phase=phase,
                device_id=did,
            )
            for k in (
                "scrolls", "likes", "video_dwells", "pull_refreshes",
                "dwell_seconds_total", "like_rate_actual", "minutes_equivalent",
                "narrative", "home_tab_fallback", "risk_detected", "phase_transition",
            ):
                if k in inner:
                    stats[k] = inner[k]
            stats["narrative"] = (
                "（无画像兴趣入库数据）→ 退化为普通养号 · "
                + str(inner.get("narrative", ""))
            )
            stats["finished_at"] = _now_iso()
            return stats

        n_seg = len(topics)
        scrolls_total_budget = _resolve_scroll_count(total_dm, scroll_count, cfg=cfg)
        scrolls_per = max(4, scrolls_total_budget // max(1, n_seg))

        for row in topics:
            topic = row["topic"]
            qurl = f"https://m.facebook.com/search/top/?q={quote(topic, safe='')}"
            o = self.open_mfacebook_deeplink(qurl, did, dwell_sec=(2.2, 3.8))
            seg: Dict[str, Any] = {
                "topic": topic,
                "count": row.get("count", 0),
                "url": qurl,
                "deeplink_ok": bool(o.get("ok")),
                "deeplink_reason": o.get("reason", ""),
                "scrolls": 0,
                "likes": 0,
                "video_dwells": 0,
                "pull_refreshes": 0,
                "dwell_seconds_total": 0.0,
                "risk_detected": "",
            }
            if o.get("ok"):
                self._run_feed_scroll_phase(d, did, scrolls_per, like_p, cfg, seg)
            stats["segments"].append(seg)
            for k in ("scrolls", "likes", "video_dwells", "pull_refreshes"):
                stats[k] = stats.get(k, 0) + int(seg.get(k, 0) or 0)
            stats["dwell_seconds_total"] = float(stats.get("dwell_seconds_total", 0.0)) + float(
                seg.get("dwell_seconds_total", 0.0) or 0.0
            )
            if seg.get("risk_detected"):
                stats["risk_detected"] = seg["risk_detected"]
                break

        home_meta: Dict[str, Any] = {"home_tab_fallback": ""}
        self._ensure_home_feed(d, did, home_meta)
        stats["home_tab_fallback_after"] = home_meta.get("home_tab_fallback", "")

        stats["finished_at"] = _now_iso()
        stats["like_rate_actual"] = stats["likes"] / max(1, stats["scrolls"])
        stats["minutes_equivalent"] = round(stats["dwell_seconds_total"] / 60.0, 1)
        stats["narrative"] = (
            f"兴趣驱动 {n_seg} 个 topic deep-link · 滑 {stats['scrolls']} 屏 · "
            f"赞 {stats['likes']} · 约 {stats['minutes_equivalent']} 分钟"
        )
        if stats.get("risk_detected"):
            stats["narrative"] += f" · ⚠ 风控中断: {stats['risk_detected']}"
        try:
            from src.host.fb_account_phase import on_scrolls as _fb_phase_on_scrolls
            transition = _fb_phase_on_scrolls(did, stats["scrolls"], stats["likes"])
            if transition and transition.get("changed"):
                stats["phase_transition"] = transition
                stats["narrative"] += (
                    f" · phase: {transition['from']} → {transition['to']}"
                )
        except Exception:
            log.debug("[browse_feed_by_interest] phase hook 失败", exc_info=True)
        return stats

    # ── Home Feed 保障 ─────────────────────────────────────────────────
    def _ensure_home_feed(self, d, did: str, stats: Optional[Dict] = None) -> bool:
        """确保当前在 Home Feed。三层回退：

        1. smart_tap("Home tab") — 命中选择器直接点
        2. press home → resume FB (stay resident) → 再试 Home tab
        3. d.app_start(PACKAGE) 强重启 → 再试 Home tab
        任一层成功返回 True；全部失败返回 False。
        stats["home_tab_fallback"] 记录最终成功的层级。
        """
        if self.smart_tap("Home tab", device_id=did):
            if stats is not None:
                stats["home_tab_fallback"] = "smart_tap"
            return True

        log.warning("[browse_feed] smart_tap('Home tab') 未命中，启用回退 L1")
        try:
            d.press("home")
            time.sleep(0.8)
            try:
                d.app_start(PACKAGE, stop=False)
            except Exception:
                try:
                    d.shell(f"monkey -p {PACKAGE} -c android.intent.category.LAUNCHER 1")
                except Exception:
                    pass
            time.sleep(2.5)
            if self.smart_tap("Home tab", device_id=did):
                if stats is not None:
                    stats["home_tab_fallback"] = "press_home_then_resume"
                return True
        except Exception as e:
            log.warning("[browse_feed] 回退 L1 异常: %s", e)

        log.warning("[browse_feed] 启用回退 L2: app_start(stop=True) 强重启 FB")
        try:
            d.app_stop(PACKAGE)
            time.sleep(1.0)
            d.app_start(PACKAGE)
            time.sleep(3.5)
            # 强重启后再 dismiss 一遍教育弹窗
            try:
                self._dismiss_dialogs(d, device_id=did)
            except Exception:
                pass
            if self.smart_tap("Home tab", device_id=did):
                if stats is not None:
                    stats["home_tab_fallback"] = "app_start_restart"
                return True
            # 最后一招: 很多 FB 版本首启直接落在 Home，点不到 Home tab 也意味已经在首页
            try:
                if d(resourceIdMatches=r".*news_feed.*|.*feed_.*").exists(timeout=1.0):
                    if stats is not None:
                        stats["home_tab_fallback"] = "restart_already_on_feed"
                    return True
            except Exception:
                pass
        except Exception as e:
            log.warning("[browse_feed] 回退 L2 异常: %s", e)

        return False

    @_with_fb_foreground
    def join_group(self, group_name: str,
                   device_id: Optional[str] = None) -> bool:
        """Search and join a group."""
        did = self._did(device_id)
        d = self._u2(did)

        with self.guarded("join_group", device_id=did):
            self.smart_tap("Search bar or search icon", device_id=did)
            time.sleep(0.5)
            self.hb.type_text(d, group_name)
            d.press("enter")
            time.sleep(2)

            self.smart_tap("Groups tab or filter", device_id=did)
            time.sleep(1)

            if self.smart_tap("First matching group", device_id=did):
                time.sleep(2)
                return self.smart_tap("Join Group button", device_id=did)

        return False

    # ── Group Operations (Sprint 1 新增 — Facebook 引流核心入口) ──────────

    @_with_fb_foreground
    def browse_groups(self, max_groups: int = 5,
                      device_id: Optional[str] = None) -> Dict[str, Any]:
        """浏览"我加入的群组"列表,顺序进每个群浏览 1 屏内容。

        作用:养号信号,让 FB 知道这些群是用户的兴趣所在,推送相关内容。
        """
        did = self._did(device_id)
        d = self._u2(did)
        stats = {"groups_visited": 0, "scrolls_total": 0, "groups_failed": 0}

        with self.guarded("browse_groups", device_id=did, weight=0.5):
            # 找 Groups Tab — Facebook Android 通常在底部菜单或左侧抽屉
            # 真机修复 (P3): cache key 必须短而稳定,使用 "Groups tab" 命中
            # data/selectors/com_facebook_katana.yaml:Groups tab
            self.smart_tap("Groups tab", device_id=did)
            time.sleep(2)
            # 也可能跳到 Groups 总览页,需点 "Your groups" / "我加入的"
            self.smart_tap("Your groups", device_id=did)
            time.sleep(1.5)

            for i in range(max_groups):
                is_risk, msg = self._detect_risk_dialog(d)
                if is_risk:
                    stats["risk_detected"] = msg
                    break
                # 进列表里第 1 个未访问的群 - smart_tap 找列表里 group 入口元素
                if not self.smart_tap("First group in your joined groups list",
                                      device_id=did):
                    stats["groups_failed"] += 1
                    continue

                time.sleep(random.uniform(2.5, 4.0))
                # 浏览 3-6 屏
                for _ in range(random.randint(3, 6)):
                    self.hb.scroll_down(d)
                    self.hb.wait_read(random.randint(800, 2500))
                    stats["scrolls_total"] += 1

                stats["groups_visited"] += 1
                # 退出群组返回列表
                d.press("back")
                time.sleep(random.uniform(1.0, 2.0))

        return stats

    def enter_group(self, group_name: str,
                    device_id: Optional[str] = None) -> bool:
        """通过搜索进入指定群组(假设已加入)。"""
        did = self._did(device_id)
        d = self._u2(did)

        with self.guarded("enter_group", device_id=did, weight=0.2):
            if not self.smart_tap("Search bar or search icon", device_id=did):
                self._fallback_search_tap(d)
            time.sleep(0.6)
            self.hb.type_text(d, group_name)
            time.sleep(1.0)
            d.press("enter")
            time.sleep(1.5)
            self.smart_tap("Groups tab or filter", device_id=did)
            time.sleep(1.0)
            ok = self.smart_tap("First matching group", device_id=did)
            if ok:
                time.sleep(random.uniform(2.0, 3.5))
            return ok

    def comment_on_post(self, comment_text: str,
                        device_id: Optional[str] = None) -> bool:
        """对当前可见的帖子发表评论(需先滚到一条帖子上)。"""
        did = self._did(device_id)
        d = self._u2(did)

        with self.guarded("comment", device_id=did):
            if not self.smart_tap("Comment button on the visible post", device_id=did):
                return False
            time.sleep(random.uniform(1.0, 2.0))

            rewritten = self.rewrite_message(comment_text,
                                             {"platform": "facebook", "context": "group_comment"})
            self.hb.type_text(d, rewritten)
            self.hb.wait_think(0.5)
            ok = self.smart_tap("Send comment button or Post button", device_id=did)
            time.sleep(random.uniform(1.0, 2.0))
            d.press("back")
            return ok

    @_with_fb_foreground
    def group_engage_session(self, group_name: str = "",
                             max_posts: int = 5,
                             comment_probability: float = 0.2,
                             like_probability: float = 0.4,
                             comment_pool: Optional[List[str]] = None,
                             device_id: Optional[str] = None,
                             persona_key: Optional[str] = None,
                             phase: Optional[str] = None) -> Dict[str, Any]:
        """进群后浏览/点赞/评论组合操作 — Sprint 1 新增 + 2026-04-22 persona 改造。

        Args:
            group_name: 群名(空则假设已在群内)
            max_posts: 浏览的帖子数上限（显式传 = 最高权重，否则按 phase 取 playbook）
            comment_probability: 每条帖子触发评论的概率
            like_probability: 每条帖子触发点赞的概率
            comment_pool: 可选的预设评论池；**为空时按 persona.country_code**
                去 ``chat_messages.yaml.countries[cc].comment_templates`` 抽，
                保证日本女性客群发日文评论、不会退化成英文。
            persona_key: 目标客群 key
            phase: 显式覆盖；默认走 fb_account_phase.get_phase(device_id)
        """
        did = self._did(device_id)
        d = self._u2(did)
        stats = {"posts_seen": 0, "likes": 0, "comments": 0,
                 "group": group_name,
                 "persona_key": persona_key or ""}

        # P0-2: 合并 playbook phase 参数。业务方法传入的显式参数权重最高。
        eff_phase, ab_cfg = _resolve_phase_and_cfg("group_engage",
                                                   device_id=did,
                                                   phase_override=phase)
        if ab_cfg:
            # yaml 字段名以 facebook_playbook.yaml 为准（max_posts / max_members）
            if "max_posts" in ab_cfg and max_posts == 5:  # 5 = 方法签名默认
                max_posts = int(ab_cfg.get("max_posts") or max_posts)
            if "comment_probability" in ab_cfg and comment_probability == 0.2:
                comment_probability = float(ab_cfg.get("comment_probability")
                                            or comment_probability)
            if "like_probability" in ab_cfg and like_probability == 0.4:
                like_probability = float(ab_cfg.get("like_probability")
                                         or like_probability)
        stats["phase"] = eff_phase
        stats["max_posts_applied"] = max_posts

        if group_name and not self.enter_group(group_name, device_id=did):
            stats["error"] = "无法进入群组"
            return stats

        # P0-2: comment_pool 为空 → 按 persona 抽日文/意大利文评论池
        if not comment_pool:
            try:
                from .fb_content_assets import get_comment_pool as _gcp
                comment_pool = _gcp(persona_key=persona_key)
            except Exception as e:
                log.debug("[group_engage] 拉 persona 评论池失败: %s", e)
        if not comment_pool:
            comment_pool = [
                "Interesting!",
                "Thanks for sharing.",
                "Useful info, appreciated.",
                "Great point.",
                "I agree with this.",
            ]
        stats["comment_pool_size"] = len(comment_pool)

        with self.guarded("group_engage", device_id=did, weight=0.5):
            for _ in range(max_posts):
                is_risk, msg = self._detect_risk_dialog(d)
                if is_risk:
                    stats["risk_detected"] = msg
                    break

                # 滚动到下一条帖子
                self.hb.scroll_down(d)
                self.hb.wait_read(random.randint(1500, 4500))
                stats["posts_seen"] += 1

                if random.random() < like_probability:
                    if self.smart_tap("Like button on the visible post",
                                      device_id=did):
                        stats["likes"] += 1
                        time.sleep(random.uniform(0.8, 1.5))

                if random.random() < comment_probability:
                    txt = random.choice(comment_pool)
                    if self.comment_on_post(txt, device_id=did):
                        stats["comments"] += 1
                        time.sleep(random.uniform(2.0, 5.0))

        return stats

    @_with_fb_foreground
    def extract_group_members(self, group_name: str = "",
                              max_members: int = 30,
                              use_llm_scoring: bool = False,
                              target_country: str = "",
                              device_id: Optional[str] = None,
                              persona_key: Optional[str] = None,
                              phase: Optional[str] = None) -> List[Dict[str, Any]]:
        """提取群成员列表 — FB 引流的核心入口。

        流程: 进群 → 点 "Members" Tab → 滚动列表 → 提取昵称/头像/简介
        提取后会自动写入 LeadsStore(source_platform=facebook, tag=群名)。

        2026-04-22 persona 改造:
          * ``max_members`` 未显式指定(用方法签名默认 30)时,按 ``phase`` 从
            ``facebook_playbook.yaml.extract_members.max_members`` 取。
            cold_start=0(禁提取) / growth=12 / mature=25 / cooldown=0。
          * ``target_country`` 空串时从 persona.country_code 自动派生,
            避免 scorer 拿 target_country="" 直接降档。
        """
        did = self._did(device_id)
        d = self._u2(did)
        members: List[Dict[str, Any]] = []

        # P0-2: phase 参数合并。max_members=0 表示该阶段禁用提取，需要短路。
        eff_phase, ab_cfg = _resolve_phase_and_cfg("extract_members",
                                                   device_id=did,
                                                   phase_override=phase)
        if ab_cfg and max_members == 30 and "max_members" in ab_cfg:
            max_members = int(ab_cfg.get("max_members") or max_members)
        if max_members <= 0:
            log.info("[extract_group_members] phase=%s 禁止提取 (max_members=0), skip",
                     eff_phase)
            return members

        # P0-2: target_country 从 persona 派生（避免评分器降档）
        if not target_country and persona_key:
            try:
                from src.host.fb_target_personas import get_persona_display
                target_country = (get_persona_display(persona_key).get("country_code")
                                  or "").upper()
            except Exception:
                pass

        if group_name and not self.enter_group(group_name, device_id=did):
            log.warning("[extract_group_members] 无法进入群组: %s", group_name)
            return members

        with self.guarded("extract_members", device_id=did, weight=0.6):
            # 找 Members Tab — 通常在群顶部菜单(About/Discussion/Members/Media)
            if not self.smart_tap("Members tab in the group header",
                                  device_id=did):
                # 退而求其次:点群头部进群信息页再点 Members
                self.smart_tap("Group name or icon at top to open info",
                               device_id=did)
                time.sleep(1.5)
                self.smart_tap("Members section", device_id=did)
            time.sleep(2.0)

            seen_names = set()
            scrolls = 0
            max_scrolls = max(5, max_members // 6)

            while len(members) < max_members and scrolls < max_scrolls:
                is_risk, msg = self._detect_risk_dialog(d)
                if is_risk:
                    break

                try:
                    xml = d.dump_hierarchy()
                    from ..vision.screen_parser import XMLParser
                    elements = XMLParser.parse(xml)
                    for el in elements:
                        if not (el.text and el.clickable and el.class_name
                                and "TextView" in el.class_name):
                            continue
                        name = (el.text or "").strip()
                        # 过滤掉非姓名文本(按钮/标签)
                        if (len(name) < 2 or name in seen_names
                                or name.lower() in ("see all", "members", "admin",
                                                    "moderator", "added by", "join")):
                            continue
                        members.append({"name": name})
                        seen_names.add(name)
                        if len(members) >= max_members:
                            break
                except Exception as e:
                    log.warning("[extract_group_members] 解析失败: %s", e)

                self.hb.scroll_down(d)
                self.hb.wait_read(random.randint(800, 2000))
                scrolls += 1

        # 写入 Leads Pool + Sprint 3 P1: scorer v2 两阶段评分(可选 LLM 精排)
        if members:
            try:
                from ..leads.store import get_leads_store
                store = get_leads_store()
                tag = f"fb_group:{group_name}" if group_name else "fb_group:current"

                if use_llm_scoring:
                    from ..ai.fb_lead_scorer_v2 import score_member_v2 as scorer
                    score_field = "final_score"
                    tier_field = "final_tier"
                    score_kwargs = {"use_llm": True}
                else:
                    from ..ai.fb_lead_scorer import score_member as scorer
                    score_field = "score"
                    tier_field = "tier"
                    score_kwargs = {}

                for m in members:
                    try:
                        lead_id = store.add_lead(
                            name=m.get("name", "Unknown"),
                            source_platform="facebook",
                            tags=[tag],
                        )
                        m["lead_id"] = lead_id

                        existing = store.get_lead(lead_id) if lead_id else None
                        s_result = scorer(
                            m.get("name", ""),
                            source_group=group_name,
                            target_country=target_country,
                            target_groups=[group_name] if group_name else [],
                            lead_record=existing,
                            **score_kwargs,
                        )
                        s = s_result.get(score_field, s_result.get("score", 0))
                        t = s_result.get(tier_field, s_result.get("tier", "D"))
                        m.update({
                            "score": s,
                            "tier": t,
                            "score_reasons": s_result.get("reasons", []),
                            "scorer_version": "v2" if use_llm_scoring else "v1",
                            "llm_used": s_result.get("llm_used", False),
                        })
                        if lead_id and s > 0:
                            try:
                                store.update_lead(lead_id, score=s)
                            except Exception:
                                pass
                    except Exception:
                        pass
                log.info("[extract_group_members] 入库+评分 %d 人 (scorer=%s, group=%s)",
                         len(members), "v2" if use_llm_scoring else "v1",
                         group_name or "current")
            except Exception as e:
                log.warning("[extract_group_members] LeadsStore 写入失败: %s", e)

        return members

    # ── Profile Operations ────────────────────────────────────────────────

    def view_profile(self, profile_name: str,
                     read_seconds: float = 10.0,
                     device_id: Optional[str] = None) -> bool:
        """打开目标用户主页并停留指定秒数(用于"先看再加"风控规避)。"""
        did = self._did(device_id)
        d = self._u2(did)

        with self.guarded("view_profile", device_id=did, weight=0.2):
            results = self.search_people(profile_name, did, max_results=3)
            if not results:
                return False
            first = self._first_search_result_element(d)
            if first is None:
                return False
            self.hb.tap(d, *self._el_center(first))
            time.sleep(random.uniform(2.0, 3.5))

            # 模拟阅读
            elapsed = 0.0
            while elapsed < read_seconds:
                if random.random() < 0.6:
                    self.hb.scroll_down(d)
                wait = random.uniform(2.0, 4.5)
                time.sleep(wait)
                elapsed += wait
            return True

    def read_profile_about(self, device_id: Optional[str] = None) -> Dict[str, Any]:
        """假设当前在某用户主页,提取 About 信息。

        返回:
            {about_text, work, education, lives_in, from_place} 等(尽力提取)
        """
        did = self._did(device_id)
        d = self._u2(did)
        info = {}
        try:
            self.smart_tap("About tab on profile", device_id=did)
            time.sleep(1.5)
            xml = d.dump_hierarchy()
            from ..vision.screen_parser import XMLParser
            elements = XMLParser.parse(xml)
            texts = [el.text for el in elements if el.text]
            joined = " | ".join(texts)
            info["raw_about"] = joined[:2000]
            for kw, key in [("Works at", "work"), ("Studied at", "education"),
                            ("Lives in", "lives_in"), ("From", "from_place"),
                            ("Married", "marital"), ("In a relationship", "marital")]:
                idx = joined.find(kw)
                if idx >= 0:
                    info[key] = joined[idx + len(kw):idx + len(kw) + 80].strip(" |:")
        except Exception as e:
            log.warning("[read_profile_about] 失败: %s", e)
        return info

    # ── Profile Snapshot for VLM Classifier (P2-4 Sprint A) ──────────────

    def capture_profile_snapshots(self,
                                   shot_count: int = 3,
                                   scroll_between: bool = True,
                                   scroll_dwell_sec: float = 1.5,
                                   device_id: Optional[str] = None,
                                   save_dir: Optional[str] = None,
                                   tag: str = "profile") -> Dict[str, Any]:
        """假设当前**已在某用户主页**，连续截 N 张图供 VLM 判定。

        参数:
            shot_count: 要截的张数（默认 3：头像区 + 下滑 1 次 + 下滑 2 次）
            scroll_between: 两张图之间是否滚动（False 则全是同一屏）
            scroll_dwell_sec: 滚动后等待再截图的秒数（让渲染稳定）
            save_dir: 截图保存目录，默认 data/fb_profile_shots/{device}_{ts}/
            tag: 文件名前缀，便于定位

        返回:
            {
              "image_paths": [...],
              "display_name": "" | "",      # 从顶部标题栏抠
              "bio_text": "",               # 从可见文字里抠前 300 字
              "shot_count": N,
            }
        """
        import os
        from time import time as _t

        from src.host.device_registry import data_dir

        did = self._did(device_id)
        d = self._u2(did)

        ts = int(_t())
        if save_dir is None:
            save_dir = str(data_dir() / "fb_profile_shots" / f"{did}_{ts}")
        os.makedirs(save_dir, exist_ok=True)

        image_paths: List[str] = []
        for i in range(max(1, shot_count)):
            p = os.path.join(save_dir, f"{tag}_{i+1}.png")
            try:
                saved = self.screenshot(device_id=did, save_path=p)
                if saved:
                    # screenshot() returns raw bytes but we want the file path string
                    image_paths.append(p)
            except Exception as e:
                log.warning("[capture_profile_snapshots] 截图失败 i=%d: %s", i, e)
            if scroll_between and i < shot_count - 1:
                try:
                    self.hb.scroll_down(d)
                except Exception:
                    pass
                time.sleep(scroll_dwell_sec)

        display_name, bio_text = self._extract_profile_text(d)
        # Sprint E-0.3: MIUI 上 uiautomator dump 被 kill → display_name/bio 为空。
        # 降级：用 qwen2.5vl:7b 对已有的 screencap 做一次 OCR 专用抽取。
        # 策略:
        #   - 仅当 display_name 和 bio 都为空，且 image_paths 非空时触发
        #   - 复用已在 GPU 的 VLM,不新增依赖；costs ~3-5s (预热后)
        #   - 失败也不抛,返回空串
        if image_paths and not (display_name and bio_text):
            try:
                ocr_name, ocr_bio = self._vlm_ocr_profile_texts(image_paths[:2])
                if not display_name and ocr_name:
                    display_name = ocr_name
                if not bio_text and ocr_bio:
                    bio_text = ocr_bio
            except Exception as _e:
                log.debug("[capture_profile_snapshots] VLM OCR 兜底失败（忽略）: %s", _e)
        return {
            "image_paths": image_paths,
            "display_name": display_name,
            "bio_text": bio_text,
            "shot_count": len(image_paths),
            "save_dir": save_dir,
        }

    _REPORT_DIALOG_TEXTS = {
        "what do you want to report?",
        "if someone is in immediate danger",
        "report this profile",
        "why are you reporting this?",
        "report a problem",
    }

    def _is_on_report_dialog(self, d) -> bool:
        """检测当前页面是否为 Facebook 的 Report（举报）对话框 / 页面。"""
        try:
            xml = d.dump_hierarchy()
            xml_lower = xml.lower()
            return any(kw in xml_lower for kw in self._REPORT_DIALOG_TEXTS)
        except Exception:
            return False

    def _extract_profile_text(self, d) -> Tuple[str, str]:
        """从当前 UI 抠 display_name 和 bio 文本（best effort）。"""
        name = ""
        bio = ""
        try:
            xml = d.dump_hierarchy()
            # 若当前页是 Report 对话框，直接返回空（避免把举报页文字当 profile）
            xml_lower = xml.lower()
            if any(kw in xml_lower for kw in self._REPORT_DIALOG_TEXTS):
                log.debug("[_extract_profile_text] 当前页是 Report 对话框，跳过")
                return "", ""
            from ..vision.screen_parser import XMLParser
            elements = XMLParser.parse(xml)
            texts = [(getattr(el, "text", "") or "").strip() for el in elements]
            texts = [t for t in texts if t]
            # Skip status bar clock patterns like 「2:32」 and pure digits
            import re as _re
            clock_pattern = _re.compile(r'^[\u300c\u300d]?\d{1,2}:\d{2}[\u300c\u300d]?$')
            skip_keywords = {"Home", "Friends", "Marketplace", "Search", "Like",
                             "Comment", "Share", "Follow", "Message", "Add Friend",
                             "Followers", "Following", "About", "Photos", "More",
                             "See All", "See more", "Report"}
            for t in texts:
                tt = t.strip()
                if clock_pattern.match(tt):
                    continue
                if 2 <= len(tt) <= 40 and tt not in skip_keywords and not tt.isdigit():
                    name = tt
                    break
            merged = " | ".join(texts)
            bio = merged[:300]
        except Exception as e:
            log.debug("[_extract_profile_text] 失败: %s", e)
        return name, bio

    def _vlm_ocr_profile_texts(self, image_paths: List[str]) -> Tuple[str, str]:
        """Sprint E-0.3: 用 VLM 从截图抠 display_name + bio（OCR 兜底）。

        单次 VLM 调用（不做分类，只抠文字），返回 (display_name, bio_text)。
        失败/超时都返回 ("", "")，不抛异常（调用方已 try/except）。

        注意: 此函数会**触发 _VLM_LOCK 排队**，建议只在 dump 失败的 MIUI 环境
        使用。正常 Android 手机走 dump 路径会快很多。
        """
        if not image_paths:
            return "", ""
        try:
            from src.host.ollama_vlm import classify_images
        except Exception:
            return "", ""

        prompt = (
            "You are an OCR assistant. Read the Facebook profile screenshot(s) and "
            "return ONLY a compact JSON object with two string fields:\n"
            '  {"display_name": "<user\'s shown name, best guess>", '
            '"bio": "<concatenated visible profile text (intro/about/city/work/school), '
            'strip nav labels like Home/Friends/Search/Marketplace/Like/Comment/Share/'
            'Followers/Following, keep under 300 chars>"}\n'
            "Rules:\n"
            "  * No extra text before/after the JSON\n"
            "  * If you can't find the name, use empty string\n"
            "  * Preserve original language (Japanese/English/Chinese), do NOT translate\n"
            "  * display_name must be 2~40 chars"
        )
        try:
            result, meta = classify_images(
                prompt=prompt,
                image_paths=image_paths[:2],
                scene="fb_profile_ocr",
                task_id="vlm_ocr",
                device_id="",
            )
            if not meta.get("ok"):
                return "", ""
            if not isinstance(result, dict):
                return "", ""
            name = (result.get("display_name") or "").strip()
            bio = (result.get("bio") or "").strip()
            if len(name) > 60:
                name = name[:60]
            if len(bio) > 300:
                bio = bio[:300]
            # 过滤明显垃圾（纯标点、全是数字、过短）
            if name and (len(name) < 2 or name.replace(" ", "").isdigit()):
                name = ""
            log.info("[vlm_ocr] name=%r bio_len=%d latency=%dms",
                     name, len(bio), int(meta.get("total_ms") or 0))
            return name, bio
        except Exception as e:
            log.debug("[vlm_ocr] 异常: %s", e)
            return "", ""

    @_with_fb_foreground
    def classify_current_profile(self,
                                  target_key: str,
                                  persona_key: Optional[str] = None,
                                  task_id: str = "",
                                  shot_count: int = 3,
                                  device_id: Optional[str] = None) -> Dict[str, Any]:
        """端到端：**假设当前已在某 FB 用户主页**，
        截图 → 抽文字 → 过 ProfileClassifier → 返回分类结果。

        target_key: 用户唯一标识（profile_url / user_id / 昵称），用于去重。
        """
        did = self._did(device_id)
        snap = self.capture_profile_snapshots(
            shot_count=shot_count, device_id=did, tag="classify",
        )
        try:
            from src.host.fb_profile_classifier import classify
            from src.host.fb_target_personas import get_persona
        except Exception as e:
            return {"ok": False, "error": f"import classifier 失败: {e}", "snapshot": snap}

        persona = get_persona(persona_key)
        result = classify(
            device_id=did,
            task_id=task_id,
            persona_key=persona["persona_key"],
            target_key=target_key,
            display_name=snap.get("display_name", ""),
            bio=snap.get("bio_text", ""),
            username="",
            locale=persona.get("locale", ""),
            image_paths=snap["image_paths"],
            l2_image_paths=snap["image_paths"],
            do_l2=True,
            dry_run=False,
        )
        result["snapshot"] = {
            "shot_count": snap["shot_count"],
            "save_dir": snap["save_dir"],
            "display_name": snap["display_name"],
        }
        return result

    # ── Profile Hunt: 批量候选 → L1+L2 → 命中处理 (P2-4 Sprint B) ──────

    @_with_fb_foreground
    def profile_hunt(self,
                     candidates: List[str],
                     persona_key: Optional[str] = None,
                     action_on_match: str = "none",   # none / follow / add_friend
                     note: str = "",
                     max_targets: Optional[int] = None,
                     inter_target_sec: Tuple[float, float] = (20.0, 34.0),
                     shot_count: int = 3,
                     task_id: str = "",
                     device_id: Optional[str] = None) -> Dict[str, Any]:
        """批量候选 profile hunt。

        流程（每个候选）:
          1. ``search_people(name)`` + 点第一条结果 → 进主页
          2. ``classify_current_profile`` 截图 + L1+L2（含配额/去重/风控）
          3. 命中时按 ``action_on_match`` 执行:
             - ``follow``    : 找并点 Follow 按钮
             - ``add_friend``: 在当前主页找并点 Add Friend（可带 note）
             - ``none``      : 只记录，不操作
          4. 返回主页（避免栈过深），sleep 真人间隔 20-34s（带抖动）

        返回结构（详细统计，供 tasks-chat 卡片渲染）:
          {
            card_type: "fb_profile_hunt",
            persona_key, action_on_match, candidates_total,
            processed, l1_pass, l2_run, matched, actioned,
            skipped: {l1_fail, l2_cap, risk_pause, cached, search_fail},
            results: [ {name, match, score, stage, reason, action_ok} ]
          }
        """
        did = self._did(device_id)
        d = self._u2(did)

        # 延迟导入以避免模块启动时的循环依赖
        from src.host.fb_target_personas import (
            get_persona, get_dedup_window_hours, get_quotas,
        )
        from src.host import fb_profile_classifier as _clf
        # Sprint E-0.1: 任务入口 fire-and-forget 预热 VLM，消掉首张 56s 冷启动。
        # 幂等：10 分钟内已 warmup 过则自动跳过，多任务同时开跑只会 warmup 一次。
        try:
            from src.host.ollama_vlm import warmup_async as _vlm_warmup_async
            _vlm_warmup_async()
        except Exception as _e:
            log.debug("[profile_hunt] VLM warmup_async 失败（忽略）: %s", _e)

        persona = get_persona(persona_key)
        pk = persona["persona_key"]
        l1_threshold = float(((persona or {}).get("l1") or {}).get("pass_threshold") or 30)
        dedup_h = int(get_dedup_window_hours() or 168)
        _q = get_quotas() or {}
        l2_daily_cap = int(_q.get("l2_per_device_per_day") or 100)
        l2_hourly_cap = int(_q.get("l2_per_device_per_hour") or 0)  # 0 = 关

        # Sprint D-1: 软降档（风控二级阶梯）
        # 硬暂停由 classifier.classify 内部处理（pause_l2_after_risk_hours）。
        # 这里处理"软降档"：近 soft_window h 内有风控但 hard pause 已过 → cap×ratio + interval×factor
        try:
            from src.host.fb_target_personas import get_risk_guard
            _rg = get_risk_guard() or {}
        except Exception:
            _rg = {}
        soft_win_h = int(_rg.get("soft_throttle_window_hours") or 0)
        cap_ratio = float(_rg.get("soft_throttle_cap_ratio") or 1.0)
        ivl_factor = float(_rg.get("soft_throttle_interval_factor") or 1.0)
        soft_throttled = False
        if soft_win_h > 0 and (cap_ratio < 1.0 or ivl_factor > 1.0):
            try:
                from src.host import fb_store as _fbs
                recent_risk = int(_fbs.count_risk_events_recent(device_id or "", soft_win_h))
            except Exception:
                recent_risk = 0
            if recent_risk > 0:
                soft_throttled = True
                old_cap_d, old_cap_h = l2_daily_cap, l2_hourly_cap
                l2_daily_cap = max(1, int(l2_daily_cap * cap_ratio))
                if l2_hourly_cap > 0:
                    l2_hourly_cap = max(1, int(l2_hourly_cap * cap_ratio))
                old_lo, old_hi = float(inter_target_sec[0]), float(inter_target_sec[1])
                inter_target_sec = (old_lo * ivl_factor, old_hi * ivl_factor)
                log.info(
                    "profile_hunt 进入软降档 (device=%s 近%dh风控=%d): "
                    "l2_daily %d→%d, l2_hour %d→%d, interval %.1f~%.1f→%.1f~%.1f",
                    device_id, soft_win_h, recent_risk,
                    old_cap_d, l2_daily_cap, old_cap_h, l2_hourly_cap,
                    old_lo, old_hi, inter_target_sec[0], inter_target_sec[1],
                )

        cands = [c.strip() for c in (candidates or []) if c and c.strip()]
        if max_targets is not None:
            cands = cands[:max(0, int(max_targets))]

        stats: Dict[str, Any] = {
            "card_type": "fb_profile_hunt",
            "persona_key": pk,
            "persona_name": persona.get("name", ""),
            "action_on_match": action_on_match,
            "candidates_total": len(cands),
            "processed": 0,
            "l1_pass": 0,
            "l2_run": 0,
            "matched": 0,
            "actioned": 0,
            "skipped": {"l1_fail": 0, "l2_cap": 0, "l2_hourly_cap": 0, "risk_pause": 0,
                        "cached": 0, "search_fail": 0, "classify_err": 0,
                        "prefilter": 0},
            "risk_interrupted": None,
            "results": [],
            "started_at": _now_iso(),
            "optimizations_applied": [
                "name_prefilter", "dedup_cache",
                "l2_daily_cap_short_circuit", "l2_hourly_cap_short_circuit",
                "vlm_warmup_async",           # Sprint E-0.1
                "deeplink_navigation",        # Sprint E-1.1
            ] + (["soft_throttle_by_risk"] if soft_throttled else []),
            "soft_throttled": soft_throttled,
            "effective_l2_daily_cap": l2_daily_cap,
            "effective_l2_hourly_cap": l2_hourly_cap,
            "effective_interval_sec": [round(inter_target_sec[0], 1),
                                       round(inter_target_sec[1], 1)],
        }

        if not cands:
            return stats

        lo, hi = float(inter_target_sec[0]), float(inter_target_sec[1])

        for idx, name in enumerate(cands):
            # ── 每轮开头：风控总闸（出现风控对话框 → 立刻中止本任务）──
            is_risk, msg = self._detect_risk_dialog(d)
            if is_risk:
                log.warning("[profile_hunt] 检测到风控，中止: %s", msg)
                stats["risk_interrupted"] = msg
                break

            item: Dict[str, Any] = {"name": name, "match": False, "score": 0,
                                    "stage": "", "reason": "", "action_ok": False,
                                    "from_cache": False}
            stats["processed"] += 1

            # ── 优化 A：name-only L1 预筛（避免为铁定不过的名字浪费 search+截图 25s）──
            pre_sc, pre_reasons = _clf.score_l1(
                persona, {"display_name": name, "bio": "", "username": ""}
            )
            if pre_sc <= 0:
                item["score"] = pre_sc
                item["stage"] = "L1-pre"
                item["reason"] = "prefilter_no_jp_signal"
                stats["skipped"]["prefilter"] += 1
                stats["results"].append(item)
                # 只跳过 search/snapshot，保持整体节奏（1-3s 小间隔避免打满循环）
                time.sleep(random.uniform(0.8, 1.6))
                continue

            # ── 优化 B：去重窗口前置（命中缓存直接用历史结果，不 search）──
            target_key_pre = f"search:{name}"
            cached = _clf._db_get_recent(pk, target_key_pre, dedup_h)
            if cached:
                item["score"] = float(cached.get("score") or 0)
                item["stage"] = cached.get("stage") or ""
                item["match"] = bool(cached.get("match"))
                item["from_cache"] = True
                item["reason"] = "dedup_cache_hit"
                stats["skipped"]["cached"] += 1
                if item["match"]:
                    stats["matched"] += 1
                    # 注意：复用缓存不再执行 action，避免对同一人短期重复触达
                stats["results"].append(item)
                time.sleep(random.uniform(0.5, 1.0))
                continue

            # ── 优化 C：L2 日配额提前检查。若今日 L2 已满 → 剩余候选只做 L1 预筛，不 search ──
            try:
                today_l2 = _clf._db_count_today(did, "L2")
            except Exception:
                today_l2 = 0
            try:
                hour_l2 = _clf._db_count_recent_hours(did, "L2", 1) if l2_hourly_cap > 0 else 0
            except Exception:
                hour_l2 = 0

            cap_hit = None
            if today_l2 >= l2_daily_cap:
                cap_hit = "l2_daily_cap"
            elif l2_hourly_cap > 0 and hour_l2 >= l2_hourly_cap:
                cap_hit = "l2_hourly_cap"

            if cap_hit:
                item["score"] = pre_sc
                item["stage"] = "L1-pre"
                if pre_sc < l1_threshold:
                    item["reason"] = "l1_below_threshold"
                    stats["skipped"]["l1_fail"] += 1
                else:
                    item["reason"] = cap_hit
                    if cap_hit == "l2_daily_cap":
                        stats["skipped"]["l2_cap"] += 1
                    else:
                        stats["skipped"]["l2_hourly_cap"] += 1
                stats["results"].append(item)
                time.sleep(random.uniform(0.8, 1.4))
                continue

            try:
                # Sprint E-1.1: 统一走 navigate_to_profile（URL/username/user_id 走 deeplink，
                # 纯显示名才降级 search）。这让 MIUI 限制下仍能跑 deeplink 链路。
                nav = self.navigate_to_profile(
                    name, device_id=did,
                    post_open_dwell_sec=(2.5, 4.0),
                )
                if not nav.get("ok"):
                    item["reason"] = f"search_fail:{nav.get('reason','')}"
                    item["nav_kind"] = nav.get("kind")
                    item["nav_via"] = nav.get("via")
                    stats["skipped"]["search_fail"] += 1
                    stats["results"].append(item)
                    time.sleep(random.uniform(lo / 3, lo / 2))
                    continue
                item["nav_kind"] = nav.get("kind")
                item["nav_via"] = nav.get("via")
                target_key = nav.get("target_key") or f"search:{name}"
                try:
                    r = self.classify_current_profile(
                        target_key=target_key,
                        persona_key=pk,
                        task_id=task_id,
                        shot_count=shot_count,
                        device_id=did,
                    )
                except Exception as e:
                    log.exception("[profile_hunt] classify 失败: %s", e)
                    item["reason"] = f"classify_err: {e}"
                    stats["skipped"]["classify_err"] += 1
                    stats["results"].append(item)
                    self._go_back_to_feed(d)
                    time.sleep(random.uniform(lo, hi))
                    continue

                item["score"] = float(r.get("score") or 0)
                item["stage"] = r.get("stage_reached") or ""
                item["match"] = bool(r.get("match"))
                item["from_cache"] = bool(r.get("from_cache"))

                if item["from_cache"]:
                    stats["skipped"]["cached"] += 1
                    item["reason"] = "dedup_cache_hit"
                elif r.get("quota", {}).get("exceeded") == "l2_daily_cap":
                    stats["skipped"]["l2_cap"] += 1
                    item["reason"] = "l2_daily_cap"
                elif r.get("quota", {}).get("exceeded") == "l2_paused_by_risk":
                    stats["skipped"]["risk_pause"] += 1
                    item["reason"] = "risk_pause"
                elif (r.get("l1") or {}).get("pass") is False:
                    stats["skipped"]["l1_fail"] += 1
                    item["reason"] = "l1_below_threshold"
                else:
                    stats["l1_pass"] += 1
                    if item["stage"] == "L2":
                        stats["l2_run"] += 1

                if item["match"]:
                    stats["matched"] += 1
                    if action_on_match and action_on_match != "none":
                        ok = self._do_action_on_profile(d, did, action_on_match, note=note)
                        item["action_ok"] = ok
                        if ok:
                            stats["actioned"] += 1

                stats["results"].append(item)

            except Exception as e:
                log.exception("[profile_hunt] 处理 %s 失败: %s", name, e)
                item["reason"] = f"exception: {e}"
                stats["results"].append(item)

            finally:
                # 回到 feed，清理 stack，继续下一位
                try:
                    self._go_back_to_feed(d)
                except Exception:
                    pass
                if idx < len(cands) - 1:
                    time.sleep(random.uniform(lo, hi))

        stats["finished_at"] = _now_iso()
        return stats

    def _do_action_on_profile(self, d, did: str, action: str, note: str = "") -> bool:
        """在当前主页上执行 follow / add_friend。假设当前已在目标主页。"""
        action = (action or "").lower().strip()
        # 滚回顶部（按钮通常在封面下方），避免识别失败
        try:
            for _ in range(3):
                d.swipe_ext("up", scale=0.8, duration=0.25)
                time.sleep(0.25)
        except Exception:
            pass
        time.sleep(random.uniform(1.0, 1.8))

        if action == "follow":
            if self.smart_tap("Follow button on profile", device_id=did) or \
               self.smart_tap("Follow button", device_id=did):
                time.sleep(random.uniform(1.2, 2.0))
                return True
            return False

        if action == "add_friend":
            hit = (self.smart_tap("Add Friend button on profile page", device_id=did) or
                   self.smart_tap("Add Friend button", device_id=did))
            if not hit:
                return False
            time.sleep(random.uniform(1.2, 2.0))
            is_risk, _ = self._detect_risk_dialog(d)
            if is_risk:
                return False
            if note:
                self.smart_tap("Add note / verification message", device_id=did)
                time.sleep(0.8)
                try:
                    d(focused=True).set_text(note)
                    time.sleep(0.8)
                except Exception:
                    pass
                self.smart_tap("Send friend request", device_id=did)
            return True

        return False

    def _go_back_to_feed(self, d):
        """尽力回到 feed/home。避免栈过深被 FB 视为异常。"""
        try:
            for _ in range(3):
                d.press("back")
                time.sleep(0.35)
        except Exception:
            pass

    # ── Inbox Operations (Sprint 2 — 这里先放骨架,确保 hasattr 检查通过) ──

    @_with_fb_foreground
    def check_messenger_inbox(self, auto_reply: bool = False,
                              max_conversations: int = 20,
                              referral_contact: str = "",
                              preset_key: str = "",
                              device_id: Optional[str] = None,
                              persona_key: Optional[str] = None,
                              phase: Optional[str] = None) -> Dict[str, Any]:
        """主收件箱(Messenger)— Sprint 2 完整实现 + 2026-04-22 persona 改造。

        2026-04-22 改动:
          * ``max_conversations`` 未显式覆盖时,按 phase 从
            ``facebook_playbook.yaml.check_inbox.max_conversations`` 取。
          * 记录 persona_key 到 stats 里,便于 fb_inbox_messages 后续聚合。

        流程:
          1. 打开 Messenger 并 dismiss 弹窗
          2. 风控检测,若红立即返回
          3. 枚举顶部对话(最多 max_conversations 个)
          4. 对每条未读对话:进入 → dump 最新一条对方消息 →
             写 fb_inbox_messages → 若 auto_reply,调 chat_bridge 生成回复并发送
          5. 返回详细统计
        """
        did = self._did(device_id)
        d = self._u2(did)

        # P0-2: check_inbox phase 参数合并
        eff_phase, ab_cfg = _resolve_phase_and_cfg("check_inbox",
                                                   device_id=did,
                                                   phase_override=phase)
        if ab_cfg and max_conversations == 20 and "max_conversations" in ab_cfg:
            max_conversations = int(ab_cfg.get("max_conversations")
                                    or max_conversations)

        stats = {"opened": False, "conversations_listed": 0,
                 "unread_processed": 0, "replied": 0,
                 "wa_referrals": 0, "errors": 0,
                 "auto_reply": auto_reply,
                 "persona_key": persona_key or "",
                 "phase": eff_phase,
                 "max_conversations_applied": max_conversations,
                 "messages": []}

        # F3 (A→B review Q10): 拿 device-level "messenger_active" 锁,和 A 的
        # send_greeting_after_add_friend fallback 串行化,避免抢输入框。
        # A 的 device_section_lock 实现在拿不到锁超时时 raise RuntimeError。
        try:
            with _messenger_active_lock(did, timeout=30.0):
                d.app_stop(MESSENGER_PACKAGE)
                time.sleep(0.5)
                d.app_start(MESSENGER_PACKAGE)
                time.sleep(3)
                self._dismiss_dialogs(d)
                stats["opened"] = True

                is_risk, msg = self._detect_risk_dialog(d)
                if is_risk:
                    stats["risk_detected"] = msg
                    return stats

                convs = self._list_messenger_conversations(d, max_conversations)
                stats["conversations_listed"] = len(convs)

                for c in convs:
                    if not c.get("unread"):
                        continue
                    if stats["unread_processed"] >= max_conversations:
                        break
                    try:
                        detail = self._open_and_read_conversation(d, c, did,
                                                                  preset_key=preset_key)
                        if detail:
                            stats["unread_processed"] += 1
                            stats["messages"].append(detail)

                            if auto_reply and detail.get("incoming_text"):
                                reply, decision = self._ai_reply_and_send(
                                    d, did,
                                    peer_name=c["name"],
                                    incoming_text=detail["incoming_text"],
                                    referral_contact=referral_contact,
                                    preset_key=preset_key,
                                    persona_key=persona_key,
                                )
                                if reply:
                                    stats["replied"] += 1
                                    if decision == "wa_referral":
                                        stats["wa_referrals"] += 1

                        d.press("back")
                        time.sleep(random.uniform(1.0, 1.8))
                    except Exception as e:
                        log.debug("[messenger_inbox] 单对话失败: %s", e)
                        stats["errors"] += 1
                        try:
                            d.press("back")
                        except Exception:
                            pass
        except RuntimeError as e:
            if "device_section_lock timeout" in str(e):
                log.info("[check_messenger_inbox] messenger_active 锁超时,skip: %s", e)
                stats["error"] = "device_busy_messenger_active"
                stats["lock_timeout"] = True
            else:
                stats["error"] = str(e)
                log.warning("[check_messenger_inbox] 失败: %s", e)
        except Exception as e:
            stats["error"] = str(e)
            log.warning("[check_messenger_inbox] 失败: %s", e)
        return stats

    @_with_fb_foreground
    def check_message_requests(self, auto_review: bool = True,
                               max_requests: int = 20,
                               preset_key: str = "",
                               device_id: Optional[str] = None,
                               persona_key: Optional[str] = None,
                               phase: Optional[str] = None,
                               auto_reply: bool = True,
                               referral_contact: str = "") -> Dict[str, Any]:
        """陌生人 Message Requests 收件箱 — Sprint 2 骨架 + 2026-04-22 persona + P6 auto_reply。

        P6 (2026-04-23): ``auto_reply=True`` 时在 Message Requests 文件夹
        内直接回复陌生人,走与主 inbox 相同的 ``_ai_reply_and_send`` 链路,
        但 ``peer_type='stranger'`` 让内部 referral_gate 使用更保守的阈值
        (min_turns 5 / min_peer_replies 3 / score_threshold 4 / cooldown 6h),
        防止陌生人场景触发 spam 式引流被 Meta 反垃圾。

        策略:Message Requests 是潜在线索富矿,但风险也大;P6 开启后仍遵循
        "intent=opening/smalltalk 不引流"的 soft gate,首轮只破冰不推销。

        Args:
          auto_review: 历史参数 (尚未实现接受/审核动作,保留占位)
          auto_reply:  True 时直接回复;False 维持 Sprint 2 的"读不回"行为
          referral_contact: WhatsApp/LINE 等引流渠道 ID (gate 打分 + hard_allow)

        2026-04-22:``max_requests`` 未显式覆盖时按 phase 从
        ``facebook_playbook.yaml.check_inbox.max_requests`` 取。
        """
        did = self._did(device_id)
        d = self._u2(did)

        # P0-2: phase 参数合并
        eff_phase, ab_cfg = _resolve_phase_and_cfg("check_inbox",
                                                   device_id=did,
                                                   phase_override=phase)
        if ab_cfg and max_requests == 20 and "max_requests" in ab_cfg:
            max_requests = int(ab_cfg.get("max_requests") or max_requests)
        # P6: playbook 也可配 auto_reply (覆盖默认 True)
        if ab_cfg and "auto_reply_stranger" in ab_cfg:
            auto_reply = bool(ab_cfg.get("auto_reply_stranger"))

        stats: Dict[str, Any] = {
            "opened": False, "requests_seen": 0,
            "messages_collected": 0,
            "replies_sent": 0, "wa_referrals": 0, "reply_skipped": 0,
            "errors": 0,
            "auto_reply": auto_reply,
            "persona_key": persona_key or "",
            "phase": eff_phase,
            "max_requests": max_requests,
        }

        # F3 (A→B review Q10): 和 A 的 send_greeting fallback 串行化
        try:
            with _messenger_active_lock(did, timeout=30.0):
                d.app_start(MESSENGER_PACKAGE)
                time.sleep(2.5)
                self._dismiss_dialogs(d)

                if not (self.smart_tap("Message Requests entry", device_id=did)
                        or self._open_message_requests_fallback(d)):
                    stats["error"] = "Message Requests 入口未找到"
                    return stats

                time.sleep(2)
                stats["opened"] = True

                is_risk, msg = self._detect_risk_dialog(d)
                if is_risk:
                    stats["risk_detected"] = msg
                    return stats

                convs = self._list_messenger_conversations(d, max_requests)
                stats["requests_seen"] = len(convs)

                for c in convs[:max_requests]:
                    try:
                        detail = self._open_and_read_conversation(d, c, did,
                                                                  peer_type="stranger",
                                                                  preset_key=preset_key)
                        if detail and detail.get("incoming_text"):
                            stats["messages_collected"] += 1

                            # P6: 陌生人场景自动回复 (peer_type='stranger' 触发
                            # referral_gate 保守配置)
                            if auto_reply and not detail.get("risk"):
                                reply, decision = self._ai_reply_and_send(
                                    d, did,
                                    peer_name=c["name"],
                                    incoming_text=detail["incoming_text"],
                                    referral_contact=referral_contact,
                                    preset_key=preset_key,
                                    persona_key=persona_key,
                                    peer_type="stranger",
                                )
                                if reply:
                                    stats["replies_sent"] += 1
                                    if decision == "wa_referral":
                                        stats["wa_referrals"] += 1
                                else:
                                    stats["reply_skipped"] += 1

                        d.press("back")
                        time.sleep(random.uniform(0.8, 1.5))
                    except Exception as e:
                        log.debug("[check_message_requests] 单对话失败: %s", e)
                        stats["errors"] += 1
                        try:
                            d.press("back")
                        except Exception:
                            pass
        except RuntimeError as e:
            if "device_section_lock timeout" in str(e):
                log.info("[check_message_requests] messenger_active 锁超时,skip: %s", e)
                stats["error"] = "device_busy_messenger_active"
                stats["lock_timeout"] = True
            else:
                stats["error"] = str(e)
                log.warning("[check_message_requests] 失败: %s", e)
        except Exception as e:
            stats["error"] = str(e)
            log.warning("[check_message_requests] 失败: %s", e)
        return stats

    @_with_fb_foreground
    def check_friend_requests_inbox(self, accept_all: bool = False,
                                    safe_accept: bool = True,
                                    max_requests: int = 20,
                                    min_mutual_friends: int = 1,
                                    device_id: Optional[str] = None,
                                    persona_key: Optional[str] = None,
                                    phase: Optional[str] = None) -> Dict[str, Any]:
        """好友请求收件箱 — Sprint 2 完整实现 + 2026-04-22 persona 改造。

        安全策略(safe_accept=True 默认):
          - 只接受有共同好友 >= min_mutual_friends 的请求(避免 honeypot)
          - 一次会话最多接受 max_requests/2,剩余留给"礼貌"
          - 每接受 1 个停顿 6-12s

        2026-04-22:``max_requests`` 未显式覆盖时按 phase 从
        ``facebook_playbook.yaml.check_inbox`` 取。cold_start 阶段自动降到 8。
        """
        did = self._did(device_id)
        d = self._u2(did)

        # P0-2: phase 参数合并
        eff_phase, ab_cfg = _resolve_phase_and_cfg("check_inbox",
                                                   device_id=did,
                                                   phase_override=phase)
        if ab_cfg and max_requests == 20 and "max_requests" in ab_cfg:
            max_requests = int(ab_cfg.get("max_requests") or max_requests)

        stats = {"opened": False, "requests_seen": 0,
                 "accepted": 0, "skipped": 0, "errors": 0,
                 "persona_key": persona_key or "",
                 "phase": eff_phase,
                 "max_requests": max_requests,
                 "accept_all": accept_all,
                 "safe_accept": safe_accept}

        try:
            d.app_start(PACKAGE)
            time.sleep(3)
            self._dismiss_dialogs(d)

            if not (self.smart_tap("Friends tab", device_id=did)
                    or self.smart_tap("Friend requests", device_id=did)):
                stats["error"] = "Friends 入口未找到"
                return stats
            time.sleep(2)
            stats["opened"] = True

            is_risk, msg = self._detect_risk_dialog(d)
            if is_risk:
                stats["risk_detected"] = msg
                return stats

            requests_meta = self._list_friend_requests(d, max_requests)
            stats["requests_seen"] = len(requests_meta)

            accept_quota = max_requests if accept_all else max(1, max_requests // 2)
            for meta in requests_meta:
                if stats["accepted"] >= accept_quota:
                    break
                if safe_accept and not accept_all:
                    if int(meta.get("mutual_friends", 0)) < min_mutual_friends:
                        stats["skipped"] += 1
                        continue
                try:
                    if self._tap_accept_button_for(d, meta):
                        stats["accepted"] += 1
                        self.hb.wait_think(random.uniform(6.0, 12.0))
                        try:
                            from src.host.fb_store import update_friend_request_status
                            update_friend_request_status(did, meta.get("name", ""), "accepted")
                        except Exception:
                            pass
                    else:
                        stats["errors"] += 1
                except Exception:
                    stats["errors"] += 1
        except Exception as e:
            stats["error"] = str(e)
            log.warning("[check_friend_requests_inbox] 失败: %s", e)
        return stats

    # ── Inbox helpers (Sprint 2 P0 内部支持函数) ─────────────────────────

    def _list_messenger_conversations(self, d, max_n: int) -> List[Dict]:
        """从 Messenger 主列表 dump 当前可见对话。返回 [{name, unread, bounds}]。"""
        try:
            xml = d.dump_hierarchy()
        except Exception:
            return []
        try:
            from ..vision.screen_parser import XMLParser
        except Exception:
            return []
        try:
            elements = XMLParser.parse(xml)
        except Exception:
            return []
        items: List[Dict] = []
        seen = set()
        for el in elements:
            text = (el.text or "").strip()
            if not text or len(text) < 2 or len(text) > 60:
                continue
            if not el.clickable:
                continue
            if text.lower() in {"chats", "people", "stories", "calls",
                                "messenger", "search", "back"}:
                continue
            if text in seen:
                continue
            seen.add(text)
            items.append({
                "name": text,
                "unread": bool(getattr(el, "selected", False) or "•" in text),
                "bounds": getattr(el, "bounds", None),
            })
            if len(items) >= max_n:
                break
        return items

    def _open_and_read_conversation(self, d, conv: Dict, did: str,
                                    peer_type: str = "friend",
                                    preset_key: str = "") -> Optional[Dict]:
        """点进对话,读最新一条对方消息,写入 fb_inbox_messages。"""
        bounds = conv.get("bounds")
        try:
            if bounds and isinstance(bounds, (tuple, list)) and len(bounds) >= 4:
                cx = (bounds[0] + bounds[2]) // 2
                cy = (bounds[1] + bounds[3]) // 2
                self.hb.tap(d, cx, cy)
            else:
                d(text=conv["name"]).click()
        except Exception:
            return None

        time.sleep(random.uniform(2.0, 3.0))
        is_risk, msg = self._detect_risk_dialog(d)
        if is_risk:
            return {"peer_name": conv["name"], "risk": msg}

        incoming_text = self._extract_latest_incoming_message(d)
        try:
            from src.host.fb_store import record_inbox_message
            record_inbox_message(
                did, conv["name"],
                peer_type=peer_type,
                message_text=incoming_text or "",
                direction="incoming",
                preset_key=preset_key,
            )
        except Exception:
            log.debug("[inbox] 写库失败", exc_info=True)
        return {"peer_name": conv["name"], "incoming_text": incoming_text}

    def _extract_latest_incoming_message(self, d) -> str:
        """从对话页面 dump 中提取最新一条对方消息(简单启发:取屏幕中靠左的最长 TextView)。"""
        try:
            xml = d.dump_hierarchy()
            from ..vision.screen_parser import XMLParser
            elements = XMLParser.parse(xml)
        except Exception:
            return ""
        candidates = []
        screen_w = 1080
        try:
            screen_w = d.window_size()[0]
        except Exception:
            pass
        for el in elements:
            text = (el.text or "").strip()
            if not text or len(text) < 3:
                continue
            bounds = getattr(el, "bounds", None)
            if not bounds:
                continue
            x_center = (bounds[0] + bounds[2]) / 2
            if x_center > screen_w * 0.6:
                continue
            if any(skip in text.lower() for skip in
                   ["type a message", "send", "active now", "online", "you"]):
                continue
            candidates.append((bounds[1], text))
        if not candidates:
            return ""
        candidates.sort(key=lambda t: -t[0])
        return candidates[0][1][:500]

    def _ai_reply_and_send(self, d, did: str, *, peer_name: str,
                           incoming_text: str,
                           referral_contact: str = "",
                           preset_key: str = "",
                           persona_key: Optional[str] = None,
                           peer_type: str = "friend") -> Tuple[Optional[str], str]:
        """调 ChatBrain 生成回复并发出;P1 接入 persona 语言 + 引流话术;
        P3 接入长久记忆;P4 意图分类;P5 统一引流决策闸;P6 peer_type 感知。

        * ``target_language`` 来自 ``fb_target_personas.get_persona_display``,
          日本客群强制 ``ja``,避免英文破冰。
        * 引流阶段(referral_score>0.5)且配置了 contact 时,**出站消息**
          以 ``fb_content_assets.get_referral_snippet`` 为主渠道话术为准。
        * **P3 长久记忆**: 同 peer 历史消息 + 派生画像通过 ``chat_memory.build_context_block``
          实时拼进 ``ab_style_hint``。
        * **P4 意图**: ``classify_intent`` 结果影响 ab_style_hint + referral_gate 判断。
        * **P5 引流闸**: ``should_refer`` 统一决定 wa_referral vs reply。
        * **P6 peer_type**: ``'stranger'`` 触发更保守的 referral_gate 配置
          (min_turns/score_threshold/cooldown 都上浮),防陌生人场景 spam 感引流。
        """
        reply = None
        decision = "skip"
        target_lang = ""
        ab_style_hint = ""
        try:
            from src.host.fb_target_personas import get_persona_display
            disp = get_persona_display(persona_key)
            target_lang = str(disp.get("language") or "").strip()
            short = str(disp.get("short_label") or disp.get("name") or "")
            if target_lang.startswith("ja"):
                ab_style_hint = (
                    "【言語・トーン必須】日本語のみ。丁寧語ベース、"
                    "押しが強くない・スパム感ゼロ。絵文字は1個まで。"
                    f" 想定読者:{short}。"
                )
            elif target_lang:
                ln = target_lang.lower()
                ab_style_hint = (
                    f"Reply ONLY in language code '{target_lang}'. "
                    f"Audience: {short}. Warm, not pushy, max 1 emoji."
                )
        except Exception as e:
            log.debug("[ai_reply] persona 元数据失败(继续): %s", e)

        # P3: 注入长久记忆 — 历史对话 + peer 派生画像
        memory_ctx: Dict[str, Any] = {
            "hint_text": "",
            "should_block_referral": False,
            "history": [],
            "profile": {},
        }
        try:
            from src.ai.chat_memory import build_context_block
            memory_ctx = build_context_block(did, peer_name, history_limit=5)
            if memory_ctx.get("hint_text"):
                if ab_style_hint:
                    ab_style_hint = ab_style_hint + "\n\n" + memory_ctx["hint_text"]
                else:
                    ab_style_hint = memory_ctx["hint_text"]
        except Exception as e:
            log.debug("[ai_reply] chat_memory 构建失败(降级): %s", e)

        # P4: 意图分类 (rule-first + LLM fallback) — 为生成 LLM 提供意图 hint,
        # 并影响引流决策 (buying/referral_ask 强制触发 wa_referral)
        intent_tag = "smalltalk"
        intent_confidence = 0.3
        try:
            from src.ai.chat_intent import (
                classify_intent,
                format_intent_for_llm_hint,
                should_trigger_referral,
            )
            intent_result = classify_intent(
                incoming_text,
                history=memory_ctx.get("history") or [],
                lang_hint=target_lang,
            )
            intent_tag = intent_result.intent
            intent_confidence = intent_result.confidence
            ih = format_intent_for_llm_hint(intent_result)
            if ih:
                ab_style_hint = (ab_style_hint + "\n\n" + ih) if ab_style_hint else ih
            log.debug(
                "[ai_reply] peer=%s intent=%s conf=%.2f src=%s",
                peer_name, intent_tag, intent_confidence, intent_result.source,
            )
        except Exception as e:
            log.debug("[ai_reply] chat_intent 失败(降级 smalltalk): %s", e)
            # 让后续流程继续用默认 smalltalk 行为

        # 空 incoming:破冰时叠一条 greeting 参考(约束 LLM 不要写英文 hello)
        try:
            if not (incoming_text or "").strip():
                from .fb_content_assets import get_greeting_message
                g = get_greeting_message(persona_key=persona_key, name=peer_name)
                if g:
                    ab_style_hint += f"\n【开场参考·勿逐字照抄】{g}"
        except Exception:
            pass

        # P1-5: 多通道 referral（line:|wa:|JSON）→ 按 persona 首推选值 + ChatBrain 摘要串
        _rc_raw = (referral_contact or "").strip()
        _rch_map: Dict[str, str] = {}
        _r_val, _r_channel = "", "whatsapp"
        try:
            from src.host.fb_referral_contact import (
                format_contact_for_chat_brain,
                parse_referral_channels,
                pick_referral_for_persona,
            )
            _rch_map = parse_referral_channels(_rc_raw)
            _r_val, _r_channel = pick_referral_for_persona(_rch_map, persona_key)
            _contact_for_brain = format_contact_for_chat_brain(_rc_raw) or _rc_raw
        except Exception as e:
            log.debug("[ai_reply] referral 解析失败(降级): %s", e)
            _contact_for_brain = _rc_raw

        try:
            from src.ai.chat_brain import ChatBrain, UserProfile
            brain = ChatBrain.get_instance()
            profile = UserProfile(lead_id=peer_name, name=peer_name,
                                  bio="", source="fb_inbox")
            result = brain.generate_reply(
                lead_id=peer_name,
                incoming_message=incoming_text,
                profile=profile,
                platform="facebook",
                target_language=target_lang,
                contact_info=_contact_for_brain,
                source="inbox",
                ab_style_hint=ab_style_hint.strip(),
            )
            if result and result.message:
                reply = result.message
                ref_score = float(getattr(result, "referral_score", 0.0) or 0.0)
                has_contact = bool(_rc_raw) or bool(_rch_map)
                # P5: 统一引流决策闸 — 替代 P3 post-block + P4 硬触发的散落逻辑
                try:
                    from src.ai.referral_gate import should_refer
                    # 只读拉 leads.store 的 A 打分供 gate 评估
                    lead_score_val = 0
                    try:
                        from src.leads.store import get_leads_store
                        _store = get_leads_store()
                        _lid = _store.find_match(name=peer_name)
                        if _lid:
                            _rec = _store.get_lead(_lid) or {}
                            try:
                                lead_score_val = int(_rec.get("score", 0) or 0)
                            except (TypeError, ValueError):
                                lead_score_val = 0
                    except Exception:
                        lead_score_val = 0
                    # P6: 陌生人场景使用更保守 gate 配置
                    _gate_cfg = None
                    if peer_type == "stranger":
                        _gate_cfg = {
                            "min_turns": 5,
                            "min_peer_replies": 3,
                            "score_threshold": 4,
                            "refer_cooldown_hours": 6,
                        }
                    gate = should_refer(
                        intent=intent_tag,
                        ref_score=ref_score,
                        memory_ctx=memory_ctx,
                        lead_score=lead_score_val,
                        has_contact=has_contact,
                        config=_gate_cfg,
                    )
                    decision = "wa_referral" if gate.refer else "reply"
                    log.info(
                        "[ai_reply] peer=%s gate level=%s refer=%s score=%d "
                        "reasons=%s",
                        peer_name, gate.level, gate.refer, gate.score,
                        "; ".join(gate.reasons)[:200],
                    )
                except Exception as e:
                    # gate 不可用降级到 pre-P5 行为
                    log.debug("[ai_reply] referral_gate 失败(降级): %s", e)
                    decision = "wa_referral" if (has_contact and ref_score > 0.5) else "reply"
                # P1-1 + P1-5: 引流出站用 **首推渠道 + 对应 ID** 的本地化模板
                if decision == "wa_referral" and _r_val:
                    try:
                        from .fb_content_assets import get_referral_snippet
                        snippet = get_referral_snippet(
                            _r_channel, _r_val,
                            persona_key=persona_key,
                        )
                        if snippet:
                            reply = snippet
                    except Exception as e:
                        log.debug("[ai_reply] referral_snippet 覆盖失败: %s", e)
        except Exception as e:
            log.debug("[ai_reply] 生成失败: %s", e)
            return None, "skip"

        if not reply:
            return None, "skip"

        try:
            input_box = d(className="android.widget.EditText")
            if input_box.exists(timeout=2.0):
                self.hb.tap(d, *self._el_center(input_box))
                time.sleep(0.4)
                self.hb.type_text(d, reply)
                time.sleep(random.uniform(0.6, 1.2))
                if not self.smart_tap("Send message button", device_id=did):
                    d.send_keys("\n")
                    time.sleep(0.5)
        except Exception as e:
            log.debug("[ai_reply] 发送失败: %s", e)
            return None, "skip"

        try:
            from src.host.fb_store import record_inbox_message
            record_inbox_message(
                did, peer_name,
                peer_type=peer_type,  # P6: 'friend'/'stranger'/'friend_request' 等
                message_text=reply,
                direction="outgoing",
                ai_decision=decision,
                ai_reply_text=reply,
                preset_key=preset_key,
            )
        except Exception:
            pass
        return reply, decision

    def _open_message_requests_fallback(self, d) -> bool:
        """Messenger 不同版本入口可能在右上角菜单或顶部 tab,做兜底。"""
        for kw in ("Message Requests", "Message requests", "Requests", "请求"):
            try:
                btn = d(textContains=kw)
                if btn.exists(timeout=0.6):
                    self.hb.tap(d, *self._el_center(btn))
                    return True
            except Exception:
                continue
        return False

    def _list_friend_requests(self, d, max_n: int) -> List[Dict]:
        """从 FB Friends 页 dump 当前可见好友请求。

        启发式:扫描页面所有 textView,查找带"X mutual friends"模式的卡片。
        """
        items: List[Dict] = []
        try:
            xml = d.dump_hierarchy()
            from ..vision.screen_parser import XMLParser
            elements = XMLParser.parse(xml)
        except Exception:
            return items
        for el in elements:
            text = (el.text or "").strip()
            if not text or len(text) < 2 or len(text) > 60:
                continue
            if " mutual friend" in text.lower() or text.lower().endswith("mutual"):
                import re as _re
                m = _re.search(r"(\d+)\s+mutual", text.lower())
                count = int(m.group(1)) if m else 0
                items.append({
                    "name": text.split(" •")[0].split("\n")[0],
                    "mutual_friends": count,
                })
                if len(items) >= max_n:
                    break
            elif el.clickable and len(text.split()) <= 4:
                items.append({"name": text, "mutual_friends": 0})
                if len(items) >= max_n:
                    break
        return items

    def _tap_accept_button_for(self, d, meta: Dict) -> bool:
        """在好友请求卡片上点击 Confirm/Accept。"""
        for kw in ("Confirm", "Accept", "确认", "接受"):
            try:
                btn = d(text=kw)
                if btn.exists(timeout=0.6):
                    self.hb.tap(d, *self._el_center(btn))
                    time.sleep(1.0)
                    return True
            except Exception:
                continue
        return False

    # ── Leads Integration ─────────────────────────────────────────────────

    @_with_fb_foreground
    def search_and_collect_leads(self, query: str,
                                 device_id: Optional[str] = None,
                                 max_leads: int = 10) -> List[int]:
        """Search people and store results in Leads Pool."""
        from ..leads.store import get_leads_store
        store = get_leads_store()
        profiles = self.search_people(query, device_id, max_leads)
        lead_ids = []

        for p in profiles:
            lead_id = store.add_lead(
                name=p.get("name", "Unknown"),
                source_platform="facebook",
                tags=[query],
            )
            if p.get("profile_url"):
                store.add_platform_profile(
                    lead_id, "facebook",
                    profile_url=p["profile_url"],
                    username=p.get("username", ""),
                )
            lead_ids.append(lead_id)

        log.info("Collected %d leads from FB search '%s'", len(lead_ids), query)
        return lead_ids

    # ── Internal Helpers ──────────────────────────────────────────────────

    def _fallback_search_tap(self, d):
        """Fallback: try common search button selectors."""
        for sel in [
            {"description": "Search"},
            {"resourceId": "com.facebook.katana:id/search_bar_text_view"},
            {"resourceId": "com.facebook.katana:id/search_bar"},
        ]:
            el = d(**sel)
            if el.exists(timeout=2):
                self.hb.tap(d, *self._el_center(el))
                return True
        return False

    # 排除列表：content_desc 匹配这些的不是人员卡片
    _SEARCH_RESULT_EXCLUDED_CDS = {
        "add friend", "add\xa0friend", "see all", "back",
        "filter all", "clear text", "more options",
        "all search results", "reels search results",
        "people search results", "groups search results",
        "events search results",
    }

    def _is_person_card(self, el) -> bool:
        """判断元素是否为搜索结果中的人员卡片（可点击的完整宽度 Button）。"""
        if not el.clickable:
            return False
        cd = (getattr(el, "content_desc", "") or "").strip()
        t = (el.text or "").strip()
        # 必须有 content_desc 或 text
        display = cd or t
        if len(display) < 2:
            return False
        # 排除 UI 动作按钮
        if display.lower() in self._SEARCH_RESULT_EXCLUDED_CDS:
            return False
        # 排除"更多选项"类
        if "更多选项" in display or "more options" in display.lower():
            return False
        # 人员卡片通常是宽 Button（全宽或接近全宽），高度 > 80px
        b = el.bounds
        if not b:
            return False
        w = b[2] - b[0]
        h = b[3] - b[1]
        if h < 80:
            return False
        # 优先：Button 类且全宽（0 到接近屏幕宽）
        if el.class_name and "Button" in el.class_name:
            if w > 400:  # 全宽或接近全宽
                return True
        # 也接受 ViewGroup 里的宽卡片
        if el.class_name and ("ViewGroup" in el.class_name or "FrameLayout" in el.class_name):
            if w > 400:
                return True
        return False

    def _extract_search_results(self, d, max_results: int) -> List[Dict[str, str]]:
        """Extract names from search results (heuristic, uses XML dump).
        
        支持两种 FB 界面：
        1. 中文界面：结果以 TextView 形式出现（text='人名'）
        2. 英文界面：结果以全宽 Button 出现（content_desc='人名,地点'）
        """
        results = []
        try:
            xml = d.dump_hierarchy()
            from ..vision.screen_parser import XMLParser
            elements = XMLParser.parse(xml)

            # 先尝试 TextView 模式（中文 FB 界面）
            for el in elements:
                if el.text and len(el.text) > 2 and el.clickable:
                    if el.class_name and "TextView" in el.class_name:
                        results.append({
                            "name": el.text,
                            "username": "",
                            "profile_url": "",
                        })
                        if len(results) >= max_results:
                            break

            # 如果 TextView 模式没结果，尝试 Button/content_desc 模式（英文 FB 界面）
            if not results:
                for el in elements:
                    if self._is_person_card(el):
                        cd = (getattr(el, "content_desc", "") or "").strip()
                        # content_desc 格式: "人名,地点" 或 "人名"
                        name = cd.split(",")[0].strip() if cd else (el.text or "").strip()
                        if name and len(name) >= 2:
                            results.append({
                                "name": name,
                                "username": "",
                                "profile_url": "",
                            })
                            if len(results) >= max_results:
                                break
        except Exception as e:
            log.warning("Failed to extract search results: %s", e)
        return results

    def _first_search_result_element(self, d):
        """返回搜索结果列表里第 1 个可点击的人员卡片元素(用于进入主页)。
        
        支持中文（TextView）和英文（Button/content_desc）两种 FB 界面。
        """
        try:
            xml = d.dump_hierarchy()
            from ..vision.screen_parser import XMLParser
            elements = XMLParser.parse(xml)

            # 先尝试 TextView 模式
            for el in elements:
                if (el.clickable and el.text and len(el.text) >= 2
                        and el.class_name and "TextView" in el.class_name):
                    return type("E", (), {
                        "info": {"bounds": {
                            "left": el.bounds[0], "top": el.bounds[1],
                            "right": el.bounds[2], "bottom": el.bounds[3],
                        }}
                    })()

            # 英文界面 Button 模式
            for el in elements:
                if self._is_person_card(el):
                    b = el.bounds
                    return type("E", (), {
                        "info": {"bounds": {
                            "left": b[0], "top": b[1],
                            "right": b[2], "bottom": b[3],
                        }}
                    })()
        except Exception:
            pass
        return None

    @staticmethod
    def _el_center(el) -> tuple:
        info = el.info
        b = info.get("bounds", {})
        cx = (b.get("left", 0) + b.get("right", 0)) // 2
        cy = (b.get("top", 0) + b.get("bottom", 0)) // 2
        return (cx, cy)
