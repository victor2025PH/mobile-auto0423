# -*- coding: utf-8 -*-
"""执行器：领任务后通过 DeviceManager + 各平台 Automation 在指定设备上执行。"""

import logging
import subprocess
import time

from src.utils.subprocess_text import run as _sp_run_text
import yaml
from pathlib import Path
from typing import Optional, List, Dict, Any

from src.device_control.device_manager import get_device_manager, DeviceManager
from src.app_automation.telegram import TelegramAutomation
from src.app_automation.whatsapp import WhatsAppAutomation
from src.app_automation.linkedin import LinkedInAutomation
from src.behavior.compliance_guard import get_compliance_guard, QuotaExceeded
from src.utils.retry import retry, run_with_timeout, TaskTimeout
from src.utils.log_config import set_task_context, clear_task_context
from src.host.health_monitor import metrics

from .task_store import (get_task, set_task_running, set_task_result,
                         create_task, update_task_progress)
from .device_registry import DEFAULT_DEVICES_YAML, PROJECT_ROOT, config_file, logs_dir

logger = logging.getLogger(__name__)

_project_root = PROJECT_ROOT


def _phase10_task_extras(params: Dict[str, Any]) -> Dict[str, Any]:
    """Phase 10/10.2/10.3 opt-in kwargs 组装 (walk_candidates / l2_gate_shots /
    do_l2_gate / max_l2_calls).

    只在显式 True/>1/非默认 时加 key, 避免未升级的 automation 分支撞 TypeError.
    """
    out: Dict[str, Any] = {}
    if params.get("walk_candidates"):
        out["walk_candidates"] = True
    shots = int(params.get("l2_gate_shots", 0) or 0)
    if shots > 1:
        out["l2_gate_shots"] = shots
    if params.get("do_l2_gate"):
        out["do_l2_gate"] = True
    budget = int(params.get("max_l2_calls", 0) or 0)
    # walk 启用时才传 budget, 且非默认 3 才值得发 kwarg.
    if out.get("walk_candidates") and budget and budget != 3:
        out["max_l2_calls"] = budget
    return out


def _vpn_required() -> bool:
    """读取 config/devices.yaml 中 connection.vpn_required (默认 True)。"""
    try:
        cfg_path = Path(DEFAULT_DEVICES_YAML)
        with open(cfg_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return bool((data.get("connection") or {}).get("vpn_required", True))
    except Exception:
        return True


def _check_network(device_id: str) -> tuple:
    """检查设备网络连通性（外网可达性）。返回 (ok: bool, msg: str)。与 preflight 同源。"""
    try:
        from src.host.preflight import check_device_network_connectivity

        return check_device_network_connectivity(device_id)
    except Exception as e:
        logger.warning("[Network] %s: 检查异常: %s", device_id[:12], e)
        return False, f"网络检查异常: {e}"


def _ensure_vpn(device_id: str) -> tuple:
    """检查VPN并尝试静默重连。返回 (ok: bool, msg: str)。
    当 config/devices.yaml vpn_required=true 时，VPN失败则中止任务。
    当 vpn_required=false 时，仅警告，任务继续执行。
    """
    try:
        from src.behavior.vpn_manager import check_vpn_status, reconnect_vpn_silent
        vpn_s = check_vpn_status(device_id)
        if vpn_s.connected:
            return True, "connected"
        logger.info("[VPN] %s: VPN 未连接，尝试静默重连...", device_id[:12])
        ok = reconnect_vpn_silent(device_id)
        if ok:
            logger.info("[VPN] %s: 静默重连成功", device_id[:12])
            return True, "reconnected"
        if _vpn_required():
            logger.error("[VPN] %s: VPN 未连接且重连失败，任务中止", device_id[:12])
            return False, f"VPN 未连接（{device_id[:8]}），重连失败，任务已中止。请手动检查 V2RayNG"
        logger.warning("[VPN] %s: VPN 未连接，vpn_required=false，继续执行任务", device_id[:12])
        return True, f"vpn_not_connected_but_optional ({device_id[:8]})"
    except Exception as e:
        logger.warning("[VPN] %s: VPN 检查异常: %s，跳过检查继续执行", device_id[:12], e)
        return True, f"check_skipped ({e})"


def _ensure_italy_ip(device_id: str) -> tuple:
    """P4-C: 验证设备 IP 是否在意大利。失败时 fail-open（不阻断任务），只有明确检测到非意大利时才中止。"""
    try:
        from src.behavior.geo_check import check_device_geo
        result = check_device_geo(device_id, "italy")
        if result.error:
            # 无法完成检查 → fail-open
            logger.debug("[GEO] %s: IP 检查跳过 (%s)", device_id[:12], result.error)
            return True, f"geo_check_skipped ({result.error})"
        if result.matches:
            logger.info("[GEO] %s: IP=%s 国家=%s ✓ 符合意大利",
                        device_id[:12], result.public_ip, result.detected_country)
            return True, f"geo_ok ({result.detected_country})"
        # 明确检测到非意大利
        logger.warning("[GEO] %s: IP 不在意大利！IP=%s 国家=%s，任务中止",
                       device_id[:12], result.public_ip, result.detected_country)
        return False, (f"IP 国家不匹配：期望意大利，实际 {result.detected_country}（{result.public_ip}）。"
                       f"请检查 VPN 节点是否为意大利出口。")
    except Exception as e:
        logger.debug("[GEO] %s: geo 检查异常 %s，跳过", device_id[:12], e)
        return True, f"geo_check_skipped ({e})"


TASK_TIMEOUT_SEC = 120
TIKTOK_TIMEOUT_SEC = 3600

# Per-task-type timeouts (seconds) — override TIKTOK_TIMEOUT_SEC
_TASK_TYPE_TIMEOUTS = {
    "tiktok_warmup": 3600,                         # 60 min max (interest_building phase can take 50m)
    "tiktok_browse_feed": 900,                     # 15 min max
    "tiktok_follow": 900,                          # 15 min max
    "tiktok_check_inbox": 1200,                    # 20 min max (50 conversations × ~15s each)
    "tiktok_chat": 600,                            # 10 min max
    "tiktok_check_and_chat_followbacks": 600,      # 10 min max (same as tiktok_chat)
    "tiktok_test_follow": 120,                     # 2 min max
    "tiktok_follow_up": 600,                       # 10 min max (30 leads × ~20s each)
    "tiktok_auto": 7200,                           # 120 min max (warmup 50m + follow + extra warmup + chat)
    "tiktok_send_dm": 120,                         # 2 min max
    "tiktok_status": 60,                           # 1 min max
    "tiktok_scan_username": 120,                   # 2 min max
    "tiktok_follow_user": 300,                     # 5 min max
    "tiktok_interact_user": 600,                   # 10 min max
    "tiktok_keyword_search": 1200,             # 20 min (keyword search + comment warmup)
    "tiktok_live_engage": 900,                  # 15 min (live stream engagement)
    # ★ P0 新增任务类型
    "tiktok_contact_discovery": 600,            # 10 min (通讯录好友发现)
    "tiktok_drip_followup": 120,                # 2 min (drip campaign 跟进)
    "contacts_sync_leads": 60,                  # 1 min (通讯录→LeadsStore 同步)
    "tiktok_priority_outreach": 300,            # 5 min (P3-2 高分Lead优先触达)
    # ── Facebook 任务超时 (Sprint 3 真机测试发现 默认 120s 不够) ──
    # browse_feed: 每次 scroll 含风控检测 + hierarchy dump,真机平均 30s/scroll
    # join_group / extract_members: 含搜索 + 等待加载,大概 5-8 分钟
    # campaign_run: 完整养号→入群→提取→好友→DM 一条龙
    "facebook_browse_feed": 900,                # 15 min (3-5 scroll × 30s + buffer)
    # 多 topic deep-link + 分段 scroll，略高于普通 browse_feed
    "facebook_browse_feed_by_interest": 1200,   # 20 min
    "facebook_profile_hunt": 3600,              # 60 min (100 候选 × 30s 上限)
    "facebook_join_group": 600,                 # 10 min
    "facebook_browse_groups": 900,              # 15 min
    "facebook_group_engage": 1200,              # 20 min
    "facebook_extract_members": 1500,           # 25 min (一个群提 30 成员)
    "facebook_search_leads": 1200,              # 20 min
    "facebook_add_friend": 300,                 # 5 min
    "facebook_add_friend_and_greet": 480,       # 8 min (加好友 + profile 页打招呼一体)
    "facebook_send_greeting": 420,              # 7 min (search → profile → Message → 发送)
    "facebook_send_message": 300,               # 5 min
    "facebook_check_inbox": 900,                # 15 min
    "facebook_check_message_requests": 600,     # 10 min
    "facebook_check_friend_requests": 600,      # 10 min
    "facebook_campaign_run": 7200,              # 120 min (一条龙)
}


def _resolve_serial_from_config(config_path: str, device_id: str) -> str:
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
        devices = (config or {}).get("devices") or {}
        if device_id in devices and isinstance(devices[device_id], dict):
            return (devices[device_id].get("device_id") or device_id)
    except Exception:
        pass
    return device_id


def _get_device_id(manager: DeviceManager, preferred: Optional[str],
                   config_path: str, task_type: str = "") -> Optional[str]:
    connected = manager.get_connected_devices()
    if not connected:
        return None

    if preferred:
        serial = _resolve_serial_from_config(config_path, preferred)
        for d in connected:
            if d.device_id == serial or d.device_id == preferred or d.display_name == preferred:
                if metrics.is_isolated(d.device_id):
                    logger.warning("指定设备 %s 已被隔离，仍然使用", preferred[:8])
                return d.device_id

    try:
        from .smart_scheduler import get_smart_scheduler
        scheduler = get_smart_scheduler()
        best = scheduler.select_device(task_type, preferred=None)
        if best:
            return best
    except Exception:
        pass

    available = [d for d in connected if not metrics.is_isolated(d.device_id)]
    if not available:
        available = connected
    return available[0].device_id


def _fresh_telegram(manager, resolved) -> TelegramAutomation:
    """创建 TelegramAutomation 实例并重启 app 确保干净状态"""
    telegram = TelegramAutomation(manager)
    telegram.set_current_device(resolved)
    d = manager.get_u2(resolved)
    if d:
        d.app_stop("org.telegram.messenger")
        time.sleep(1)
    return telegram


@retry(max_attempts=2, delay=3.0, backoff=1.5,
       exceptions=(ConnectionError, OSError, TimeoutError, subprocess.TimeoutExpired,
                   BrokenPipeError, IOError),
       on_retry=lambda a, e: logger.info("任务重试 attempt %d: %s", a, e))
def _execute_with_retry(manager, resolved, task_type, params, config_path=None, _task_id: str = ""):
    """带重试的任务执行入口。"""
    # 在新线程中重新设置 task_context，以便 _make_progress_cb 能读取到 task_id
    if _task_id:
        try:
            set_task_context(task_id=_task_id, device_id=resolved)
        except Exception:
            pass

    # ★ P0 新增: 通讯录→LeadsStore 同步（不需要 UI 操作，只读 DB）
    if task_type == "contacts_sync_leads":
        try:
            from src.app_automation.contacts_manager import get_discovery_results
            from src.workflow.platform_actions_bridge import _sync_contact_discoveries_to_leads
            results = get_discovery_results(device_id=resolved)
            synced = _sync_contact_discoveries_to_leads(
                device_id=resolved,
                discovered_names=[r["tiktok_username"] for r in results
                                  if r.get("tiktok_username")],
                source="contacts_sync_periodic",
            )
            return True, "", {"synced": synced, "total_discovered": len(results)}
        except Exception as e:
            return False, f"通讯录同步失败: {e}", None

    if task_type == "telegram_send_message":
        username = params.get("username") or params.get("target") or ""
        message = params.get("message", "")
        if not username:
            return False, "params.username 或 params.target 必填", None
        tg = _fresh_telegram(manager, resolved)
        if not tg.search_and_open_user(username, resolved):
            return False, f"搜索用户 {username} 失败", None
        ok = tg.send_text_message(message, resolved)
        return ok, ("" if ok else "消息发送失败"), None

    elif task_type == "telegram_read_messages":
        username = params.get("username") or params.get("target") or ""
        count = params.get("count", 20)
        if not username:
            return False, "params.username 必填", None
        tg = _fresh_telegram(manager, resolved)
        if not tg.search_and_open_user(username, resolved):
            return False, f"搜索用户 {username} 失败", None
        msgs = tg.read_messages(resolved, count=count)
        # 去掉 raw 字段（太长），返回干净的消息列表
        clean = [{k: v for k, v in m.items() if k != "raw"} for m in msgs]
        return True, "", {"messages": clean, "count": len(clean)}

    elif task_type == "telegram_send_file":
        username = params.get("username") or params.get("target") or ""
        file_path = params.get("file_path", "")
        caption = params.get("caption", "")
        if not username:
            return False, "params.username 必填", None
        if not file_path:
            return False, "params.file_path 必填", None
        tg = _fresh_telegram(manager, resolved)
        if not tg.search_and_open_user(username, resolved):
            return False, f"搜索用户 {username} 失败", None
        ok = tg.send_file(file_path, resolved, caption=caption)
        return ok, ("" if ok else "文件发送失败"), None

    elif task_type == "telegram_workflow":
        username = params.get("username") or params.get("target") or ""
        message = params.get("message", "")
        if not username:
            return False, "params.username 必填", None
        tg = _fresh_telegram(manager, resolved)
        include_screenshot = params.get("include_screenshot", True)
        ok = tg.complete_workflow(username, message, include_screenshot=include_screenshot)
        return ok, ("" if ok else "workflow 执行失败"), None

    elif task_type == "whatsapp_send_message":
        contact = params.get("contact") or params.get("username") or params.get("target") or ""
        message = params.get("message", "")
        if not contact:
            return False, "params.contact 必填", None
        wa = WhatsAppAutomation(manager)
        wa.set_current_device(resolved)
        d = manager.get_u2(resolved)
        if d:
            d.app_stop("com.whatsapp")
            time.sleep(1)
        if not wa.search_and_open_user(contact, resolved):
            return False, f"搜索 WhatsApp 联系人 {contact} 失败", None
        ok = wa.send_text_message(message, resolved)
        return ok, ("" if ok else "WhatsApp 消息发送失败"), None

    elif task_type == "whatsapp_read_messages":
        contact = params.get("contact") or params.get("username") or params.get("target") or ""
        count = params.get("count", 20)
        if not contact:
            return False, "params.contact 必填", None
        wa = WhatsAppAutomation(manager)
        wa.set_current_device(resolved)
        d = manager.get_u2(resolved)
        if d:
            d.app_stop("com.whatsapp")
            time.sleep(1)
        if not wa.search_and_open_user(contact, resolved):
            return False, f"搜索 WhatsApp 联系人 {contact} 失败", None
        msgs = wa.read_messages(resolved, count=count)
        return True, "", {"messages": msgs, "count": len(msgs)}

    elif task_type == "linkedin_send_message":
        recipient = params.get("recipient") or params.get("username") or params.get("target") or ""
        message = params.get("message", "")
        if not recipient:
            return False, "params.recipient 必填", None
        li = LinkedInAutomation(manager)
        li.set_current_device(resolved)
        li.start_app(resolved)
        ok = li.send_message(recipient, message, resolved)
        return ok, ("" if ok else "LinkedIn 消息发送失败（可能对方不是1度连接）"), None

    elif task_type == "linkedin_read_messages":
        li = LinkedInAutomation(manager)
        li.set_current_device(resolved)
        li.start_app(resolved)
        msgs = li.read_messages(resolved, count=params.get("count", 20))
        return True, "", {"messages": msgs, "count": len(msgs)}

    elif task_type == "linkedin_post_update":
        content = params.get("content") or params.get("message") or ""
        if not content:
            return False, "params.content 必填", None
        li = LinkedInAutomation(manager)
        li.set_current_device(resolved)
        li.start_app(resolved)
        ok = li.post_update(content, resolved)
        return ok, ("" if ok else "LinkedIn 动态发布失败"), None

    elif task_type == "linkedin_search_profile":
        query = params.get("query") or params.get("name") or ""
        if not query:
            return False, "params.query 必填", None
        li = LinkedInAutomation(manager)
        li.set_current_device(resolved)
        li.start_app(resolved)
        profiles = li.search_profiles(query, resolved, max_results=params.get("max_results", 10))
        return True, "", {"profiles": profiles, "count": len(profiles)}

    elif task_type == "linkedin_send_connection":
        name = params.get("name") or params.get("query") or params.get("target") or ""
        note = params.get("note", "")
        if not name:
            return False, "params.name 必填", None
        li = LinkedInAutomation(manager)
        li.set_current_device(resolved)
        li.start_app(resolved)
        ok = li.send_connection_request(name, resolved, note=note)
        return ok, ("" if ok else "LinkedIn 连接请求发送失败"), None

    elif task_type == "linkedin_accept_connections":
        max_accept = params.get("max_accept", 10)
        li = LinkedInAutomation(manager)
        li.set_current_device(resolved)
        li.start_app(resolved)
        count = li.accept_connections(resolved, max_accept=max_accept)
        return True, "", {"accepted_count": count}

    elif task_type == "telegram_switch_account":
        account_name = params.get("account_name", "")
        if not account_name:
            return False, "params.account_name 必填", None
        tg = TelegramAutomation(manager)
        tg.set_current_device(resolved)
        ok = tg.switch_account(account_name, resolved)
        return ok, ("" if ok else f"切换到账号 {account_name} 失败"), {"account": account_name}

    elif task_type == "telegram_list_accounts":
        tg = TelegramAutomation(manager)
        tg.set_current_device(resolved)
        accounts = tg.list_accounts(resolved)
        return True, "", {"accounts": accounts}

    elif task_type == "telegram_forward":
        from_user = params.get("from_user", "")
        to_user = params.get("to_user", "")
        count = params.get("count", 1)
        if not from_user or not to_user:
            return False, "params.from_user 和 params.to_user 必填", None
        tg = _fresh_telegram(manager, resolved)
        if not tg.search_and_open_user(from_user, resolved):
            return False, f"搜索用户 {from_user} 失败", None
        return True, "", {"forwarded": 0, "note": "forward功能开发中"}

    elif task_type == "linkedin_like_post":
        li = LinkedInAutomation(manager)
        li.set_current_device(resolved)
        li.start_app(resolved)
        guard = get_compliance_guard()
        try:
            guard.check_and_record("linkedin", "like_post")
        except QuotaExceeded as e:
            return False, str(e), None
        return True, "", {"note": "like_post 功能开发中"}

    elif task_type == "linkedin_comment_post":
        comment = params.get("comment", "")
        if not comment:
            return False, "params.comment 必填", None
        li = LinkedInAutomation(manager)
        li.set_current_device(resolved)
        li.start_app(resolved)
        guard = get_compliance_guard()
        try:
            guard.check_and_record("linkedin", "comment_post")
        except QuotaExceeded as e:
            return False, str(e), None
        return True, "", {"note": "comment_post 功能开发中"}

    elif task_type.startswith("tiktok_"):
        return _execute_tiktok(manager, resolved, task_type, params)

    elif task_type == "batch_send":
        return _execute_batch_send(manager, resolved, params, config_path)

    elif task_type.startswith("facebook_"):
        return _execute_facebook(manager, resolved, task_type, params)

    elif task_type == "whatsapp_auto_reply":
        wa = _fresh_whatsapp(manager, resolved)
        duration = params.get("duration", 120)
        ok = wa.auto_reply_chat(resolved, duration=duration)
        return ok, ("" if ok else "WhatsApp 自动回复失败"), None

    elif task_type == "whatsapp_send_media":
        wa = _fresh_whatsapp(manager, resolved)
        contact = params.get("contact") or params.get("target", "")
        media_path = params.get("media_path", "")
        if not contact or not media_path:
            return False, "params.contact 和 params.media_path 必填", None
        if not wa.search_and_open_user(contact, resolved):
            return False, f"搜索联系人 {contact} 失败", None
        ok = wa.send_media(media_path, resolved)
        return ok, ("" if ok else "发送媒体失败"), None

    elif task_type == "whatsapp_list_chats":
        wa = _fresh_whatsapp(manager, resolved)
        chats = wa.list_chats(resolved)
        return True, "", {"chats": chats, "count": len(chats)}

    elif task_type == "telegram_auto_reply":
        tg = _fresh_telegram(manager, resolved)
        duration = params.get("duration", 120)
        ok = tg.auto_reply_monitor(resolved, duration=duration)
        return ok, ("" if ok else "Telegram 自动回复失败"), None

    elif task_type == "telegram_join_group":
        tg = _fresh_telegram(manager, resolved)
        group = params.get("group") or params.get("group_name", "")
        if not group:
            return False, "params.group 必填", None
        ok = tg.join_group(group, resolved)
        return ok, ("" if ok else "加入群组失败"), None

    elif task_type == "telegram_send_group":
        tg = _fresh_telegram(manager, resolved)
        group = params.get("group") or params.get("group_name", "")
        message = params.get("message", "")
        if not group:
            return False, "params.group 必填", None
        ok = tg.send_group_message(group, message, resolved)
        return ok, ("" if ok else "群组消息发送失败"), None

    elif task_type == "telegram_monitor_chat":
        tg = _fresh_telegram(manager, resolved)
        username = params.get("username") or params.get("target", "")
        duration = params.get("duration", 120)
        if not username:
            return False, "params.username 必填", None
        ok = tg.monitor_chat(username, resolved, duration=duration)
        return ok, ("" if ok else "监控聊天失败"), None

    elif task_type == "instagram_browse_feed":
        ig = _fresh_instagram(manager, resolved)
        if not ig.launch(resolved):
            return False, "Instagram 启动失败", None
        st = ig.browse_feed(
            scroll_count=int(params.get("scroll_count", 8)),
            like_probability=float(params.get("like_probability", 0.2)),
            device_id=resolved,
        )
        return True, "", {"browse_stats": st}

    elif task_type == "instagram_search_leads":
        ig = _fresh_instagram(manager, resolved)
        if not ig.launch(resolved):
            return False, "Instagram 启动失败", None
        q = params.get("keyword") or params.get("query", "")
        if not q:
            return False, "params.keyword 或 params.query 必填", None
        ids = ig.search_and_collect_leads(
            q, device_id=resolved, max_leads=int(params.get("max_leads", 10)))
        return True, "", {"lead_ids": ids, "count": len(ids)}

    elif task_type == "instagram_send_dm":
        ig = _fresh_instagram(manager, resolved)
        if not ig.launch(resolved):
            return False, "Instagram 启动失败", None
        to = params.get("recipient") or params.get("username", "")
        msg = params.get("message", "")
        if not to or not msg:
            return False, "params.recipient 与 params.message 必填", None
        ok = ig.send_dm(to, msg, device_id=resolved)
        return ok, ("" if ok else "Instagram DM 失败"), None

    elif task_type == "instagram_browse_hashtag":
        ig = _fresh_instagram(manager, resolved)
        if not ig.launch(resolved):
            return False, "Instagram 启动失败", None
        tag = params.get("hashtag") or params.get("keyword", "")
        if not tag:
            return False, "params.hashtag 必填", None
        st = ig.browse_hashtag(
            tag,
            scroll_count=int(params.get("scroll_count", 5)),
            like_probability=float(params.get("like_probability", 0.25)),
            device_id=resolved,
        )
        return True, "", {"browse_stats": st}

    elif task_type == "twitter_browse_timeline":
        tw = _fresh_twitter(manager, resolved)
        if not tw.launch(resolved):
            return False, "X/Twitter 启动失败", None
        st = tw.browse_timeline(
            scroll_count=int(params.get("scroll_count", 8)),
            like_probability=float(params.get("like_probability", 0.2)),
            retweet_probability=float(params.get("retweet_probability", 0.05)),
            device_id=resolved,
        )
        return True, "", {"browse_stats": st}

    elif task_type == "twitter_search_leads":
        tw = _fresh_twitter(manager, resolved)
        if not tw.launch(resolved):
            return False, "X/Twitter 启动失败", None
        q = params.get("keyword") or params.get("query", "")
        if not q:
            return False, "params.keyword 或 params.query 必填", None
        ids = tw.search_and_collect_leads(
            q, device_id=resolved, max_leads=int(params.get("max_leads", 10)))
        return True, "", {"lead_ids": ids, "count": len(ids)}

    elif task_type == "twitter_search_and_engage":
        tw = _fresh_twitter(manager, resolved)
        if not tw.launch(resolved):
            return False, "X/Twitter 启动失败", None
        kw = params.get("keyword", "")
        if not kw:
            return False, "params.keyword 必填", None
        st = tw.search_and_engage(
            kw,
            max_tweets=int(params.get("max_tweets", 5)),
            reply_probability=float(params.get("reply_probability", 0.3)),
            like_probability=float(params.get("like_probability", 0.5)),
            device_id=resolved,
        )
        return True, "", {"engage_stats": st}

    elif task_type == "twitter_send_dm":
        tw = _fresh_twitter(manager, resolved)
        if not tw.launch(resolved):
            return False, "X/Twitter 启动失败", None
        to = params.get("recipient") or params.get("username", "")
        msg = params.get("message", "")
        if not to or not msg:
            return False, "params.recipient 与 params.message 必填", None
        ok = tw.send_dm(to, msg, device_id=resolved)
        return ok, ("" if ok else "Twitter DM 失败"), None

    elif task_type == "proxy_pool_sync":
        # Phase 7 P0: 922S5 代理池定时巡检（无需设备）
        try:
            from src.device_control.proxy_pool_manager import run_proxy_pool_sync
            result = run_proxy_pool_sync(params)
            return True, "", result
        except Exception as e:
            logger.error("[Executor] proxy_pool_sync 失败: %s", e)
            return False, f"代理池巡检失败: {e}", None

    else:
        return False, f"不支持的任务类型: {task_type}", None


def _fresh_tiktok(manager, resolved):
    """Create TikTokAutomation instance (lazy import to avoid circular deps)."""
    from src.app_automation.tiktok import TikTokAutomation
    tt = TikTokAutomation(device_manager=manager)
    tt.set_current_device(resolved)
    return tt


def _fresh_facebook(manager, resolved):
    from src.app_automation.facebook import FacebookAutomation
    fb = FacebookAutomation(manager)
    fb.set_current_device(resolved)
    fb.launch(resolved)
    return fb


def _execute_facebook(manager, resolved, task_type, params):
    """Facebook 任务统一分发器 — 处理所有 facebook_* 任务类型。

    设计目标:
      - 与 TikTok 的 _execute_tiktok 风格对齐;
      - 单点封装 launch + 异常包装,避免每个 task_type 重复样板;
      - 支持 facebook_campaign_run 串行多步骤剧本（5 套预设的服务端实现）。
    """
    fb = _fresh_facebook(manager, resolved)

    try:
        from src.host.fb_add_friend_gate import check_add_friend_gate as _fb_add_friend_gate

        if task_type == "facebook_browse_feed":
            # P0-1: duration 是主驱动（每分钟 X 屏），scroll_count 仍可显式覆盖
            # P1-2: 读账号 phase，自动按 playbook 档位调整节奏
            duration = int(params.get("duration") or params.get("duration_minutes") or 15)
            scroll_count = params.get("scroll_count")
            like_prob = params.get("like_probability")
            phase = params.get("phase")
            if not phase:
                try:
                    from src.host.fb_account_phase import get_phase as _fb_get_phase
                    phase = (_fb_get_phase(resolved) or {}).get("phase") or "cold_start"
                except Exception:
                    phase = "cold_start"
            try:
                from src.app_automation.facebook import FbWarmupError
                stats = fb.browse_feed(
                    scroll_count=int(scroll_count) if scroll_count else None,
                    like_probability=float(like_prob) if like_prob is not None else None,
                    duration_minutes=duration,
                    phase=phase,
                    device_id=resolved,
                )
                return True, "", stats
            except FbWarmupError as e:
                return False, str(e), {
                    "card_type": "fb_warmup",
                    "phase": phase,
                    "error_code": e.code,
                    "error_hint": e.hint,
                    "scrolls": 0,
                    "duration_minutes": duration,
                }

        if task_type == "facebook_browse_feed_by_interest":
            # Sprint F: 读 fb_content_exposure 热榜 → m.facebook.com/search deep-link
            # → 分段 scroll+like（MIUI 不依赖 uiautomator dump）；无数据时退化为 browse_feed
            duration = int(params.get("duration") or params.get("duration_minutes") or 15)
            phase = params.get("phase")
            if not phase:
                try:
                    from src.host.fb_account_phase import get_phase as _fb_get_phase
                    phase = (_fb_get_phase(resolved) or {}).get("phase") or "cold_start"
                except Exception:
                    phase = "cold_start"
            if not hasattr(fb, "browse_feed_by_interest"):
                return False, "facebook.browse_feed_by_interest 尚未实现", None
            try:
                from src.app_automation.facebook import FbWarmupError
                stats = fb.browse_feed_by_interest(
                    persona_key=params.get("persona_key") or "",
                    interest_hours=int(params.get("interest_hours", 168)),
                    max_topics=int(params.get("max_topics", 4)),
                    like_boost=float(params.get("like_boost", 0.12)),
                    scroll_count=(int(params["scroll_count"])
                                  if params.get("scroll_count") else None),
                    duration_minutes=duration,
                    phase=phase,
                    device_id=resolved,
                )
                return True, "", stats
            except FbWarmupError as e:
                return False, str(e), {
                    "card_type": "fb_interest_feed",
                    "phase": phase,
                    "error_code": e.code,
                    "error_hint": e.hint,
                    "fallback_no_topics": False,
                    "duration_minutes": duration,
                }

        if task_type == "facebook_send_message":
            target = params.get("target") or params.get("username") or params.get("recipient", "")
            message = params.get("message", "")
            if not target:
                return False, "params.target 必填", None
            ok = fb.send_message(target, message, device_id=resolved)
            return ok, ("" if ok else "Facebook 消息发送失败"), None

        if task_type == "facebook_add_friend":
            target = params.get("target") or params.get("username") or params.get("profile_name", "")
            if not target:
                return False, "params.target 必填", None
            _gate_err, _gate_meta = _fb_add_friend_gate(resolved, params)
            if _gate_err:
                return False, _gate_err, _gate_meta
            note = params.get("note") or params.get("verification_note", "")
            safe_mode = params.get("safe_mode", True)
            # P0-2: 透传 persona_key 让 add_friend_with_note 空 note 时按 persona
            # 自动填日文/意大利文验证语；并把 phase override 带过去（router 层可能预计算）。
            _persona_key = params.get("persona_key") or ""
            _phase_override = params.get("phase") or params.get("phase_override") or ""
            if hasattr(fb, "add_friend_with_note"):
                _extra = _phase10_task_extras(params)
                ok = fb.add_friend_with_note(target, note=note,
                                             safe_mode=safe_mode,
                                             device_id=resolved,
                                             persona_key=_persona_key or None,
                                             phase=_phase_override or None,
                                             source=params.get("source", "") or params.get("group_name", ""),
                                             preset_key=(params.get("_preset_key", "") or params.get("preset_key", "")),
                                             force=bool(params.get("force_add_friend")),
                                             **_extra)
            else:
                ok = fb.add_friend(target, device_id=resolved)

            # 2026-04-23 P3-1: add_friend_with_note 内部已在锁内 record,
            # executor 层不再重复写入。仅失败时补一条 risk 记录供分析,
            # 因为 automation 失败路径不会 record(它只在 UI 成功时写 sent)。
            if not ok:
                try:
                    from src.host.fb_store import record_friend_request
                    source = params.get("source", "") or params.get("group_name", "")
                    preset_key = (params.get("_preset_key", "") or
                                  params.get("preset_key", ""))
                    record_friend_request(
                        resolved, target,
                        note=note,
                        source=source,
                        status="risk",
                        preset_key=preset_key,
                    )
                except Exception:
                    pass
            return ok, ("" if ok else "添加好友失败"), None

        # 2026-04-23: 搜索 → 加好友 → 打招呼(一体化,方案 A2)
        if task_type == "facebook_add_friend_and_greet":
            target = (params.get("target") or params.get("username")
                      or params.get("profile_name", ""))
            if not target:
                return False, "params.target 必填", None
            # 先过加好友 gate（phase / daily_cap）
            _gate_err, _gate_meta = _fb_add_friend_gate(resolved, params)
            if _gate_err:
                return False, _gate_err, _gate_meta
            note = params.get("note") or params.get("verification_note", "")
            greeting = params.get("greeting") or params.get("greeting_message", "")
            _persona_key = params.get("persona_key") or ""
            _phase_override = params.get("phase") or params.get("phase_override") or ""
            _preset_key = (params.get("_preset_key", "")
                           or params.get("preset_key", ""))
            if not hasattr(fb, "add_friend_and_greet"):
                return False, "facebook.add_friend_and_greet 尚未实现", None
            # 把 source / preset_key 下推,automation 层锁内 record 时用
            _src_val = params.get("source", "") or params.get("group_name", "")
            _extra = _phase10_task_extras(params)
            if params.get("force_add_friend"):
                _extra["force"] = True
            if params.get("ai_dynamic_greeting") is not None:
                _extra["ai_dynamic_greeting"] = bool(params.get("ai_dynamic_greeting"))
            if params.get("force_send_greeting") is not None:
                _extra["force_send_greeting"] = bool(params.get("force_send_greeting"))
            res = fb.add_friend_and_greet(
                target,
                note=note,
                greeting=greeting,
                device_id=resolved,
                persona_key=_persona_key or None,
                phase=_phase_override or None,
                preset_key=_preset_key,
                source=_src_val,
                greet_on_failure=bool(params.get("greet_on_failure", False)),
                **_extra,
            ) or {}
            # 2026-04-23 P3-1: automation 层已在锁内 record(ok 时 sent;
            # 不 ok 时不写)。这里仅补 risk 标记, 便于失败漏斗分析。
            if not res.get("add_friend_ok"):
                try:
                    from src.host.fb_store import record_friend_request
                    record_friend_request(
                        resolved, target,
                        note=note,
                        source=_src_val,
                        status="risk",
                        preset_key=_preset_key,
                    )
                except Exception:
                    pass
            ok = bool(res.get("add_friend_ok"))
            return ok, ("" if ok else "加好友+打招呼失败"), res

        if task_type == "facebook_send_greeting":
            # 独立打招呼任务(不配合 add_friend,用于老朋友主动问候 / 手动触发)
            target = (params.get("target") or params.get("username")
                      or params.get("profile_name", ""))
            if not target:
                return False, "params.target 必填", None
            if not hasattr(fb, "send_greeting_after_add_friend"):
                return False, "facebook.send_greeting_after_add_friend 尚未实现", None
            greeting = params.get("greeting") or params.get("greeting_message", "")
            _persona_key = params.get("persona_key") or ""
            _phase_override = params.get("phase") or params.get("phase_override") or ""
            _preset_key = (params.get("_preset_key", "")
                           or params.get("preset_key", ""))
            ok = fb.send_greeting_after_add_friend(
                target,
                greeting=greeting,
                device_id=resolved,
                persona_key=_persona_key or None,
                phase=_phase_override or None,
                # 独立调用时默认不在 profile 页 → 自动跑 search_people 链路
                assume_on_profile=bool(params.get("assume_on_profile", False)),
                preset_key=_preset_key,
                ai_decision=params.get("ai_decision") or "greeting",
            )
            return ok, ("" if ok else "打招呼发送失败"), None

        if task_type == "facebook_search_leads":
            keyword = params.get("keyword") or params.get("query", "")
            if not keyword:
                return False, "params.keyword 必填", None
            max_leads = int(params.get("max_leads", 10))
            leads = fb.search_and_collect_leads(keyword, device_id=resolved,
                                                max_leads=max_leads)
            return True, "", {"leads": leads, "count": len(leads)}

        if task_type == "facebook_join_group":
            group = params.get("group_name") or params.get("group", "")
            if not group:
                return False, "params.group_name 必填", None
            ok = fb.join_group(group, device_id=resolved)
            if ok:
                try:
                    from src.host.fb_store import upsert_group
                    preset_key = (params.get("_preset_key", "") or
                                  params.get("preset_key", ""))
                    upsert_group(resolved, group,
                                 country=params.get("target_country", ""),
                                 language=params.get("language", ""),
                                 status="joined",
                                 preset_key=preset_key)
                except Exception:
                    pass
            return ok, ("" if ok else "加入群组失败"), None

        # 群组相关 — Sprint 1 新增
        if task_type == "facebook_browse_groups":
            if not hasattr(fb, "browse_groups"):
                return False, "facebook.browse_groups 尚未实现（待 Sprint 1 完成）", None
            stats = fb.browse_groups(
                max_groups=int(params.get("max_groups", 5)),
                device_id=resolved,
            )
            return True, "", stats

        if task_type == "facebook_group_engage":
            if not hasattr(fb, "group_engage_session"):
                return False, "facebook.group_engage_session 尚未实现", None
            stats = fb.group_engage_session(
                group_name=params.get("group_name", ""),
                max_posts=int(params.get("max_posts", 5)),
                comment_probability=float(params.get("comment_probability", 0.2)),
                like_probability=float(params.get("like_probability", 0.4)),
                device_id=resolved,
                persona_key=params.get("persona_key") or None,
                phase=params.get("phase") or params.get("phase_override") or None,
            )
            return True, "", stats

        if task_type == "facebook_extract_members":
            if not hasattr(fb, "extract_group_members"):
                return False, "facebook.extract_group_members 尚未实现", None
            group_name = params.get("group_name", "")
            members = fb.extract_group_members(
                group_name=group_name,
                max_members=int(params.get("max_members", 30)),
                use_llm_scoring=bool(params.get("use_llm_scoring", False)),
                target_country=params.get("target_country", ""),
                device_id=resolved,
                persona_key=params.get("persona_key") or None,
                phase=params.get("phase") or params.get("phase_override") or None,
            )
            if members and group_name:
                try:
                    from src.host.fb_store import mark_group_visit
                    mark_group_visit(resolved, group_name,
                                     extracted_count=len(members))
                except Exception:
                    pass
            return True, "", {"members": members, "count": len(members)}

        # ── P2-4 Sprint B: 目标画像 Profile Hunt ─────────────────────────
        # 接收候选名字列表 → 挨个 search → snapshot → L1+L2 VLM 识别 →
        # 命中者可选 follow / add_friend。配额由 fb_target_personas.yaml 控制。
        if task_type == "facebook_profile_hunt":
            if not hasattr(fb, "profile_hunt"):
                return False, "facebook.profile_hunt 尚未实现", None
            candidates = params.get("candidates") or []
            if isinstance(candidates, str):
                # 兼容前端传字符串（换行 / 逗号分隔）
                import re as _re
                candidates = [c.strip() for c in _re.split(r"[,\n;]", candidates) if c.strip()]
            if not candidates:
                # 若未传 candidates，尝试从上游任务产出拉（如 extract_members）
                upstream = params.get("candidates_from_task_id") or ""
                if upstream:
                    try:
                        from src.host.task_store import get_task as _gt
                        t = _gt(upstream)
                        if t and t.get("result"):
                            import json as _j
                            _r = _j.loads(t["result"]) if isinstance(t["result"], str) else t["result"]
                            names = []
                            for m in (_r.get("members") or []):
                                if isinstance(m, dict):
                                    n = m.get("name") or m.get("display_name") or ""
                                    if n:
                                        names.append(n)
                                elif isinstance(m, str):
                                    names.append(m)
                            candidates = names
                    except Exception as _e:
                        logger.warning("[profile_hunt] 拉取上游候选失败: %s", _e)
            if not candidates:
                return False, "params.candidates 必填（或提供 candidates_from_task_id）", None

            max_targets = params.get("max_targets")
            if max_targets is not None:
                max_targets = int(max_targets)
            lo = float(params.get("inter_target_min_sec", 20.0))
            hi = float(params.get("inter_target_max_sec", 34.0))
            if hi < lo:
                hi = lo + 10.0
            stats = fb.profile_hunt(
                candidates=candidates,
                persona_key=params.get("persona_key"),
                action_on_match=(params.get("action_on_match") or "none"),
                note=(params.get("note") or ""),
                max_targets=max_targets,
                inter_target_sec=(lo, hi),
                shot_count=int(params.get("shot_count", 3)),
                task_id=(_get_current_task_id() or ""),
                device_id=resolved,
            )
            ok = bool(stats.get("processed", 0) > 0)
            return ok, ("" if ok else "未处理任何候选"), stats

        # 收件箱(Sprint 2 P0 完整实现 + Sprint 3 P0 preset_key 透传)
        _preset_key = params.get("_preset_key", "") or params.get("preset_key", "")
        _persona_key = params.get("persona_key") or None
        _phase_override = params.get("phase") or params.get("phase_override") or None
        if task_type == "facebook_check_inbox":
            if not hasattr(fb, "check_messenger_inbox"):
                return False, "facebook.check_messenger_inbox 尚未实现", None
            referral = params.get("referral_contact", "")
            stats = fb.check_messenger_inbox(
                auto_reply=bool(params.get("auto_reply", False)),
                max_conversations=int(params.get("max_conversations", 20)),
                referral_contact=referral,
                preset_key=_preset_key,
                device_id=resolved,
                persona_key=_persona_key,
                phase=_phase_override,
            )
            return True, "", stats

        if task_type == "facebook_check_message_requests":
            if not hasattr(fb, "check_message_requests"):
                return False, "facebook.check_message_requests 尚未实现", None
            stats = fb.check_message_requests(
                auto_review=bool(params.get("auto_review", True)),
                max_requests=int(params.get("max_requests", 20)),
                preset_key=_preset_key,
                device_id=resolved,
                persona_key=_persona_key,
                phase=_phase_override,
            )
            return True, "", stats

        if task_type == "facebook_check_friend_requests":
            if not hasattr(fb, "check_friend_requests_inbox"):
                return False, "facebook.check_friend_requests_inbox 尚未实现", None
            stats = fb.check_friend_requests_inbox(
                accept_all=bool(params.get("accept_all", False)),
                safe_accept=bool(params.get("safe_accept", True)),
                max_requests=int(params.get("max_requests", 20)),
                min_mutual_friends=int(params.get("min_mutual_friends", 1)),
                device_id=resolved,
                persona_key=_persona_key,
                phase=_phase_override,
            )
            return True, "", stats

        # 串行剧本（与 TikTok 的 tiktok_campaign_run 同构）
        if task_type == "facebook_campaign_run":
            return _run_facebook_campaign(fb, resolved, params)

        # Phase 12.3 (2026-04-25): facebook_recycle_dead_peers
        # 扫 canonical 含 referral_dead tag 且 referral_dead_at 早于 now - days,
        # 去 tag + 清 counter → peer 再次可被 dispatcher plan.
        # 前缀用 facebook_ 让 executor 路由把它送到 Facebook 分支 (函数本身不依赖 fb).
        if task_type == "facebook_recycle_dead_peers":
            return _line_pool_recycle_dead_peers(params)

        # Phase 11 (2026-04-25): fb_line_dispatch_from_reply
        # 扫近 N 小时 contact_events (greeting_replied/message_received) → 按
        # canonical.metadata (l2_verified / persona 匹配) 过滤 → allocate LINE
        # pool → 写 line_dispatch_log + 返 "派发计划" 给调用方 (B 机/运营).
        if task_type == "facebook_line_dispatch_from_reply":
            return _fb_line_dispatch_from_reply(resolved, params)

        # Phase 12 Alpha (2026-04-25): A 自立消费 line_dispatch_planned → 用
        # Messenger 直接把 referral 话术发给对方, 不等 B 协同就位.
        if task_type == "facebook_send_referral_replies":
            return _fb_send_referral_replies(fb, resolved, params)

        return False, f"不支持的 Facebook 任务类型: {task_type}", None

    except Exception as e:
        logger.exception("Facebook 任务执行异常: %s", task_type)
        return False, f"{task_type} 异常: {e}", None


def _line_pool_recycle_dead_peers(params: Dict[str, Any]) -> tuple:
    """Phase 12.3 (2026-04-25): 把"死太久"的 referral_dead peer 自动复活.

    扫所有带 referral_dead tag 的 canonical, metadata.referral_dead_at 早于
    ``now - days`` 的 → revive_referral (去 tag + 清 counter). 给 peer
    第二次机会 (FB 可能已经解开对该 peer 的发消息限制).

    Params:
      days: int = 7           # 多少天前标 dead 的才 recycle
      dry_run: bool = False   # 只列, 不真做
      limit: int = 500        # 每轮最多处理多少条 (防 DB 爆)
    """
    days = max(1, int(params.get("days", 7) or 7))
    dry_run = bool(params.get("dry_run", False))
    limit = max(1, min(int(params.get("limit", 500) or 500), 5000))

    from src.host.lead_mesh import (list_l2_verified_leads,
                                     revive_referral)
    import datetime as _dt

    # list_l2_verified_leads 只返 l2_verified 的; dead peers 大部分也是 L2 verified
    # (因为只有 L2 过的才会被 dispatcher plan + 之后被标 dead). 这里
    # include_tags=['referral_dead'] 精准定位, limit 放大一点.
    cutoff = _dt.datetime.utcnow() - _dt.timedelta(days=days)

    rows = list_l2_verified_leads(
        include_tags=["referral_dead"], limit=limit,
    )

    revived_ids: List[str] = []
    skipped_young: int = 0
    for r in rows:
        dead_at_iso = r.get("metadata", {}).get("referral_dead_at") or ""
        if dead_at_iso:
            try:
                dead_dt = _dt.datetime.strptime(
                    dead_at_iso, "%Y-%m-%dT%H:%M:%SZ")
            except Exception:
                dead_dt = None
            if dead_dt and dead_dt > cutoff:
                # 还没到 days 天, skip
                skipped_young += 1
                continue
        # 没 dead_at_iso (老数据) 或已够久 → revive
        if dry_run:
            revived_ids.append(r["canonical_id"])
            continue
        try:
            if revive_referral(r["canonical_id"]):
                revived_ids.append(r["canonical_id"])
        except Exception as e:
            logger.debug("[recycle] revive %s 失败: %s",
                          r["canonical_id"][:12], e)

    return True, "", {
        "scanned": len(rows), "revived": len(revived_ids),
        "skipped_young": skipped_young,
        "revived_canonical_ids": revived_ids[:50],  # cap response size
        "days_threshold": days, "dry_run": dry_run,
    }


def _fb_line_dispatch_from_reply(resolved: str,
                                  params: Dict[str, Any]
                                  ) -> tuple:
    """Phase 11 (2026-04-25) dispatcher.

    Flow:
      1. 扫 hours_window 内 greeting_replied + message_received events
      2. 去重: 每个 canonical_id 在 dedupe_hours 内最多派发一次
      3. 按 persona / l2_verified / is_japanese / min_score 过滤
      4. line_pool.allocate(region, persona_key, canonical_id, peer_name, ...)
      5. 组装 referral 话术 (chat_messages.yaml referral_line[] 模板 + {line_id})
      6. 写 line_dispatch_log (planned) + 可选 contact_event (line_dispatch_planned
         / wa_referral_sent, 可配)
      7. 返 stats {scanned, filtered_out, dispatched, no_account, dispatches}

    Params:
      hours_window: int = 6           # 扫近几小时事件
      dedupe_hours: int = 24          # 同一 canonical 去重窗口
      require_l2_verified: bool = True
      persona_key: str = ""           # 只对匹配 persona 的 lead 分配
      region: str = ""                # 按区域过滤 line pool
      min_score: float = 0
      limit: int = 20                 # 单次最多派发几条
      dispatch_mode: str = "messenger_text"  # messenger_text | line_direct_send
      write_contact_event: bool = False  # True 写 event 供 B 消费
      event_type: str = "line_dispatch_planned"  # 或 "wa_referral_sent" 保兼容
    """
    hours_window = int(params.get("hours_window", 6) or 6)
    dedupe_hours = int(params.get("dedupe_hours", 24) or 24)
    require_l2 = bool(params.get("require_l2_verified", True))
    persona_key = (params.get("persona_key") or "").strip()
    region = (params.get("region") or "").strip()
    min_score = float(params.get("min_score", 0) or 0)
    limit = max(1, min(int(params.get("limit", 20) or 20), 200))
    write_ce = bool(params.get("write_contact_event", False))
    dispatch_mode = (params.get("dispatch_mode") or "messenger_text").strip()
    if dispatch_mode not in {"messenger_text", "line_direct_send"}:
        dispatch_mode = "messenger_text"
    event_type = (params.get("event_type")
                   or "line_dispatch_planned").strip()
    # Phase 12.3: 通用 tags 过滤 (AND 包含 / NOT 排除)
    include_tags = {t.strip() for t in (params.get("include_tags") or [])
                     if isinstance(t, str) and t.strip()}
    exclude_tags = {t.strip() for t in (params.get("exclude_tags") or [])
                     if isinstance(t, str) and t.strip()}
    # Phase 12.3: dry_run — 不 allocate 不写 log 不写 event, 只列候选
    dry_run = bool(params.get("dry_run", False))

    from src.host.fb_store import (list_recent_contact_events_by_types,
                                    CONTACT_EVT_GREETING_REPLIED,
                                    CONTACT_EVT_MESSAGE_RECEIVED)
    from src.host import line_pool as _lp
    try:
        from src.host.fb_store import record_contact_event
    except Exception:
        record_contact_event = None  # type: ignore
    try:
        from src.app_automation.fb_content_assets import get_referral_snippet
    except Exception:
        get_referral_snippet = None  # type: ignore

    events = list_recent_contact_events_by_types(
        [CONTACT_EVT_GREETING_REPLIED, CONTACT_EVT_MESSAGE_RECEIVED],
        hours=hours_window, limit=limit * 5,
    )

    dispatches: List[Dict[str, Any]] = []
    seen_canonical: set = set()
    filtered_out = 0
    no_account = 0

    from src.host.lead_mesh import resolve_identity
    from src.host.lead_mesh.canonical import _connect as _lm_connect
    import json as _json

    # line_dispatch_log.created_at 用 datetime('now') 空格分隔格式, 用 SQL 原生
    # datetime('now', '-N hours') 做 cutoff 避免字符串格式不一致.
    dedupe_sql_offset = f"-{int(dedupe_hours)} hours"

    for ev in events:
        if len(dispatches) >= limit:
            break
        peer_name = ev.get("peer_name") or ""
        device_id = ev.get("device_id") or ""
        event_id = str(ev.get("id") or "")
        if not peer_name:
            filtered_out += 1
            continue

        try:
            cid = resolve_identity(
                platform="facebook",
                account_id=f"fb:{peer_name}",
                display_name=peer_name)
        except Exception:
            filtered_out += 1
            continue

        if cid in seen_canonical:
            filtered_out += 1
            continue

        # 去重: dedupe_hours 内已有 line_dispatch_log 就跳
        try:
            with _lm_connect() as conn:
                prev = conn.execute(
                    "SELECT 1 FROM line_dispatch_log WHERE canonical_id=?"
                    " AND status != 'skipped'"
                    " AND created_at >= datetime('now', ?) LIMIT 1",
                    (cid, dedupe_sql_offset),
                ).fetchone()
            if prev:
                seen_canonical.add(cid)
                filtered_out += 1
                continue
        except Exception:
            pass

        # 读 canonical metadata 决定是否值得引流
        meta: Dict[str, Any] = {}
        try:
            with _lm_connect() as conn:
                row = conn.execute(
                    "SELECT metadata_json, tags FROM leads_canonical"
                    " WHERE canonical_id=?", (cid,),
                ).fetchone()
                if row:
                    try:
                        meta = _json.loads(row["metadata_json"] or "{}")
                    except Exception:
                        meta = {}
                    tags_set = {t.strip() for t in
                                 (row["tags"] or "").split(",") if t.strip()}
                else:
                    tags_set = set()
        except Exception:
            tags_set = set()

        if require_l2 and "l2_verified" not in tags_set:
            filtered_out += 1
            continue
        # Phase 12.2: peer 被标 referral_dead 跳过 (永久 fail 过, 再试浪费)
        if "referral_dead" in tags_set:
            filtered_out += 1
            continue
        # Phase 12.3: 通用 include/exclude_tags 过滤
        if include_tags and not include_tags.issubset(tags_set):
            filtered_out += 1
            continue
        if exclude_tags and (exclude_tags & tags_set):
            filtered_out += 1
            continue
        try:
            if float(meta.get("l2_score", 0) or 0) < min_score:
                filtered_out += 1
                continue
        except (TypeError, ValueError):
            pass
        if persona_key and meta.get("l2_persona_key") != persona_key:
            filtered_out += 1
            continue

        # 分配 LINE 账号 — dry_run 模式只预览选中账号, 不更新 DB.
        # NB: list_accounts 的 owner_device_id 是精确匹配, allocate 实际用
        # "OR empty (通用池)" 语义. 预览时不过滤 owner, 再在 Python 层筛一次
        # 保持与 allocate 行为一致.
        if dry_run:
            from src.host.line_pool import list_accounts as _lp_list
            cand_all = _lp_list(
                status="active", region=region or None,
                persona_key=persona_key or None,
                limit=20,
            )
            candidates = [
                c for c in cand_all
                if not device_id
                    or c.get("owner_device_id", "") == ""
                    or c.get("owner_device_id") == device_id
            ]
            if not candidates:
                no_account += 1
                seen_canonical.add(cid)
                continue
            acc = candidates[0]
        else:
            acc = _lp.allocate(
                region=region or None,
                persona_key=persona_key or None,
                owner_device_id=device_id or None,
                canonical_id=cid, peer_name=peer_name,
                source_device_id=device_id, source_event_id=event_id,
            )
            if acc is None:
                no_account += 1
                seen_canonical.add(cid)
                continue

        seen_canonical.add(cid)

        # Phase 11.2: 组装 messenger 引流话术 (dispatch_mode=messenger_text)
        # line_direct_send 模式下 B 机/LINE automation 用自己的话术, 这里只传 line_id.
        # Phase 12.1: persona fallback 优先级反转 — lead.metadata 优先于 task.persona_key
        # (lead 自身 L2 分类的 persona 比 task-level 全局 persona 更贴 lead 真实画像).
        effective_persona = (meta.get("l2_persona_key") or persona_key or "")
        message_template = ""
        if dispatch_mode == "messenger_text" and get_referral_snippet:
            try:
                message_template = get_referral_snippet(
                    channel="line",
                    value=acc["line_id"],
                    persona_key=effective_persona,
                    peer_name=peer_name,
                    age_band=meta.get("age_band") or "",
                    gender=meta.get("gender") or "",
                )
            except Exception as e:
                logger.debug("[phase11] referral_snippet 失败: %s", e)

        dispatch = {
            "canonical_id": cid,
            "peer_name": peer_name,
            "source_device_id": device_id,
            "source_event_id": event_id,
            "source_event_type": ev.get("event_type"),
            "line_account_id": acc["id"],
            "line_id": acc["line_id"],
            "dispatch_mode": dispatch_mode,
            "message_template": message_template,
            "metadata": {
                "age_band": meta.get("age_band"),
                "gender": meta.get("gender"),
                "is_japanese": meta.get("is_japanese"),
                "l2_score": meta.get("l2_score"),
            },
        }
        dispatches.append(dispatch)

        if write_ce and record_contact_event is not None and not dry_run:
            try:
                record_contact_event(
                    device_id or resolved, peer_name,
                    event_type,
                    preset_key=f"line_pool:{acc['id']}",
                    meta={"line_id": acc["line_id"],
                          "line_account_id": acc["id"],
                          "dispatch_mode": dispatch_mode,
                          "message_template": message_template,
                          "dispatched_by": "agent_a_phase11",
                          "source_event_id": event_id,
                          "original_device_id": device_id,  # Phase 11.1 溯源
                          "canonical_id": cid})
            except Exception as e:
                logger.debug("[phase11] write %s 失败: %s", event_type, e)

    stats = {
        "scanned": len(events),
        "dispatched": len(dispatches),
        "filtered_out": filtered_out,
        "no_account": no_account,
        "dispatches": dispatches,
        "dry_run": dry_run,
    }
    return True, "", stats


# Phase 12.0.1: MessengerError code 分类 — 哪些值得 retry
_REFERRAL_TRANSIENT_CODES = frozenset({
    "messenger_unavailable",    # app 启动中
    "search_ui_missing",         # UI 加载慢
    "send_button_missing",       # UI glitch
})
_REFERRAL_PERMANENT_CODES = frozenset({
    "risk_detected",             # 风控
    "xspace_blocked",            # MIUI 双开
    "recipient_not_found",       # peer 搜不到
    "send_blocked_by_content",   # 内容被 FB 挡
})

# Phase 12.2: peer 级失败阈值 — 达阈值给 canonical 打 referral_dead tag,
# dispatcher 下次扫到会跳过. 按错误严重度分级:
#   recipient_not_found: peer 账号不存在/改名 → 1 次即 dead (retry 永远搜不到)
#   send_blocked_by_content: peer 过往封禁本文案 → 2 次 dead (换模板可能解)
# risk_detected/xspace_blocked 是 device/global 问题不计 peer 账.
_REFERRAL_PEER_FAIL_THRESHOLDS = {
    "recipient_not_found": 1,
    "send_blocked_by_content": 2,
}


def _referral_peer_fail_record(canonical_id: str,
                                err_code: str,
                                peer_name: str = "") -> bool:
    """Phase 12.2: 累加 canonical.metadata.referral_fail_count, 达到该
    err_code 对应阈值时打 referral_dead tag + 存 dead_reason/at.

    返回 True 表示本次调用触发了 referral_dead (状态由"活"转"死").
    """
    if not canonical_id or err_code not in _REFERRAL_PEER_FAIL_THRESHOLDS:
        return False
    threshold = _REFERRAL_PEER_FAIL_THRESHOLDS[err_code]
    try:
        from src.host.lead_mesh import update_canonical_metadata
        from src.host.lead_mesh.canonical import _connect as _lm_connect
        import json as _json
        import time as _tt
        with _lm_connect() as conn:
            row = conn.execute(
                "SELECT metadata_json, tags FROM leads_canonical"
                " WHERE canonical_id=?", (canonical_id,),
            ).fetchone()
            if not row:
                return False
            try:
                existing = _json.loads(row["metadata_json"] or "{}")
            except Exception:
                existing = {}
            existing_tags = {t.strip() for t in
                              (row["tags"] or "").split(",") if t.strip()}
            if "referral_dead" in existing_tags:
                return False  # 已经 dead, 不重复
        # 构造增量
        counter_key = f"referral_fail_count_{err_code}"
        prev_n = int(existing.get(counter_key, 0) or 0)
        new_n = prev_n + 1
        meta_patch = {counter_key: new_n}
        tags = []
        if new_n >= threshold:
            meta_patch["referral_dead_reason"] = err_code
            meta_patch["referral_dead_at"] = _tt.strftime(
                "%Y-%m-%dT%H:%M:%SZ", _tt.gmtime())
            meta_patch["referral_dead_peer_name"] = peer_name or ""
            tags.append("referral_dead")
        update_canonical_metadata(canonical_id, meta_patch,
                                   tags=tags or None)
        return new_n >= threshold
    except Exception as e:
        logger.debug("[phase12.2.peer_fail] canonical=%s err=%s 异常: %s",
                       canonical_id[:12] if canonical_id else "", err_code, e)
        return False


def _fb_send_referral_replies(fb, resolved: str,
                               params: Dict[str, Any]) -> tuple:
    """Phase 12 Alpha (2026-04-25): A 自立消费 line_dispatch_planned event.

    为什么需要这个 task (而不仅仅是 dispatcher 写事件等 B 消费):
      * B 机实装 line_dispatch_planned 消费逻辑需要协调, 不知何时就绪
      * A 端已有 facebook.send_message, 能直接把 message_template 发出去
      * 闭环立刻跑通, 不阻塞生产. B 将来就位后作为 fallback 路径

    Flow:
      1. 扫近 hours_window 小时 line_dispatch_planned events
      2. dispatch_mode=messenger_text 直发; line_direct_send 若 LINE auto 未就位,
         fallback 为 messenger_text (Phase 12.0.1)
      3. strict_device_match=True: 只发 original_device_id 匹配本机
      4. 24h 去重: 若该 peer 已有 wa_referral_sent → skip
      5. 发送前 random.uniform(min, max) sleep 做速率平滑 (防风控)
      6. 调 facebook.send_message(raise_on_error=True), 按 MessengerError.code
         分瞬时/永久: 瞬时 retry max_retry 次 interval retry_interval_sec
      7. 成功 → 写 wa_referral_sent + mark_dispatch_outcome(sent)
         失败 → mark_dispatch_outcome(failed, note=<err_code>)

    Params:
      hours_window: int = 2
      dedupe_hours: int = 24
      strict_device_match: bool = True
      limit: int = 10
      min_interval_sec: float = 30   # 两条 send 之间最短 sleep
      max_interval_sec: float = 90   # 最长 sleep (random 0-1 混合)
      max_retry: int = 2             # 瞬时错误 retry 次数 (不含首发)
      retry_interval_sec: float = 10
      fallback_line_direct_send: bool = True  # True 时 line_direct_send 降级为
                                                # messenger_text 发 (避免死信)
    """
    import random as _r
    import time as _time

    hours_window = int(params.get("hours_window", 2) or 2)
    dedupe_hours = int(params.get("dedupe_hours", 24) or 24)
    strict_device = bool(params.get("strict_device_match", True))
    limit = max(1, min(int(params.get("limit", 10) or 10), 100))
    min_iv = float(params.get("min_interval_sec", 30) or 0)
    max_iv = float(params.get("max_interval_sec", 90) or 0)
    if max_iv < min_iv:
        max_iv = min_iv
    max_retry = max(0, int(params.get("max_retry", 2) or 0))
    retry_iv = float(params.get("retry_interval_sec", 10) or 0)
    fallback_direct_send = bool(params.get("fallback_line_direct_send", True))
    # Phase 12.3: dry_run — 不真发, 只列 matched planned events + 会发什么 template.
    dry_run = bool(params.get("dry_run", False))

    from src.host.fb_store import (list_recent_contact_events_by_types,
                                    record_contact_event,
                                    count_contact_events,
                                    CONTACT_EVT_LINE_DISPATCH_PLANNED,
                                    CONTACT_EVT_WA_REFERRAL_SENT)
    from src.host import line_pool as _lp
    try:
        from src.app_automation.facebook import MessengerError
    except Exception:
        MessengerError = Exception  # 兜底, 不应命中
    import json as _json

    events = list_recent_contact_events_by_types(
        [CONTACT_EVT_LINE_DISPATCH_PLANNED],
        hours=hours_window, limit=limit * 5,
    )

    sent = 0
    failed = 0
    skipped_dedup = 0
    skipped_device = 0
    skipped_mode = 0
    outcomes: List[Dict[str, Any]] = []

    processed_count = 0  # 已真正尝试发送的条数 (用于速率平滑间隔判断)

    for ev in events:
        if sent + failed >= limit:
            break
        try:
            meta = _json.loads(ev.get("meta_json") or "{}") or {}
        except Exception:
            meta = {}

        dispatch_mode = meta.get("dispatch_mode") or "messenger_text"
        # Phase 12.0.1: line_direct_send 若无 LINE auto 消费者会成死信,
        # fallback 为 messenger_text 直发 LINE ID 文本.
        effective_mode = dispatch_mode
        if dispatch_mode == "line_direct_send":
            if fallback_direct_send:
                effective_mode = "messenger_text"
            else:
                skipped_mode += 1
                continue
        if effective_mode != "messenger_text":
            skipped_mode += 1
            continue

        peer_name = ev.get("peer_name") or ""
        message_template = meta.get("message_template") or ""
        line_account_id = int(meta.get("line_account_id") or 0)
        original_device_id = (meta.get("original_device_id")
                                or ev.get("device_id") or "")

        if not peer_name or not message_template:
            skipped_mode += 1
            continue

        # 设备匹配: 只在同一台 FB 账号的设备上继续发, 避免串线
        if strict_device and original_device_id and resolved != original_device_id:
            skipped_device += 1
            continue

        # 24h 去重 peer 级 (可能多次 planned 但只发一次 referral)
        try:
            n_sent = count_contact_events(
                device_id=resolved, peer_name=peer_name,
                event_type=CONTACT_EVT_WA_REFERRAL_SENT,
                hours=dedupe_hours,
            )
            if n_sent > 0:
                skipped_dedup += 1
                continue
        except Exception:
            pass

        # Phase 12.0.1 速率平滑: 第二条起 random sleep [min, max]
        if processed_count > 0 and max_iv > 0:
            sleep_for = _r.uniform(min_iv, max_iv)
            logger.debug("[referral.send] rate-limit sleep %.1fs", sleep_for)
            _time.sleep(sleep_for)
        processed_count += 1

        # Phase 12.0.1 in-task retry: 瞬时错误 retry 最多 max_retry 次,
        # 永久错误立即 failed. raise_on_error=True 拿到 MessengerError.code.
        # Phase 12.3 dry_run: 不调 fb.send_message, 只记"会发" 不实发, 不 mark outcome
        if dry_run:
            outcomes.append({
                "peer_name": peer_name, "line_account_id": line_account_id,
                "line_id": meta.get("line_id"),
                "planned_event_id": ev.get("id"),
                "sent": None, "err_code": "dry_run",
                "note": "dry_run_would_send",
                "effective_mode": effective_mode,
                "would_send_template": message_template,
            })
            processed_count += 1
            continue

        ok = False
        err_note = ""
        err_code = ""
        attempts = max_retry + 1
        for attempt in range(attempts):
            try:
                ok = bool(fb.send_message(
                    recipient=peer_name, message=message_template,
                    device_id=resolved, raise_on_error=True,
                ))
                err_code = ""
                break
            except MessengerError as me:
                err_code = getattr(me, "code", "") or ""
                err_note = f"code={err_code}|{str(me)[:80]}"
                if err_code in _REFERRAL_PERMANENT_CODES:
                    logger.info("[referral.send] %s 永久错误 %s, 不 retry",
                                 peer_name, err_code)
                    break
                # 瞬时错误 / 未分类 → retry
                if attempt + 1 < attempts:
                    logger.info("[referral.send] %s 瞬时错误 %s, retry %d/%d",
                                 peer_name, err_code, attempt + 1,
                                 max_retry)
                    _time.sleep(retry_iv)
                    continue
                # 耗尽 retry
                logger.info("[referral.send] %s 瞬时错误 %s retry 耗尽",
                             peer_name, err_code)
                break
            except Exception as e:
                err_note = f"send_message_exception:{type(e).__name__}:{str(e)[:80]}"
                err_code = "unknown_exception"
                logger.warning("[referral.send] %s 未知异常: %s",
                                 peer_name, e)
                break

        canonical_id_of_peer = meta.get("canonical_id") or ""
        became_dead = False

        if ok:
            sent += 1
            try:
                record_contact_event(
                    resolved, peer_name,
                    CONTACT_EVT_WA_REFERRAL_SENT,
                    preset_key=f"line_pool:{line_account_id}",
                    meta={"line_id": meta.get("line_id"),
                          "line_account_id": line_account_id,
                          "sent_by": "agent_a_phase12_alpha",
                          "effective_mode": effective_mode,
                          "original_mode": dispatch_mode,
                          "source_event_id": str(ev.get("id") or ""),
                          "source_planned_event_id": str(ev.get("id") or ""),
                          "canonical_id": canonical_id_of_peer})
            except Exception as e:
                logger.debug("[referral.send] write wa_referral_sent 失败: %s", e)
            if line_account_id:
                try:
                    _lp.mark_dispatch_outcome(
                        line_account_id, status="sent",
                        note=f"via=messenger peer={peer_name}")
                except Exception:
                    pass
            # Phase 12.2: success 写 canonical metadata + tag line_referred
            if canonical_id_of_peer:
                try:
                    import time as _t_phase12
                    from src.host.lead_mesh import update_canonical_metadata
                    update_canonical_metadata(
                        canonical_id_of_peer,
                        {
                            "line_referred_at": _t_phase12.strftime(
                                "%Y-%m-%dT%H:%M:%SZ", _t_phase12.gmtime()),
                            "line_id": meta.get("line_id"),
                            "line_account_id": line_account_id,
                            "referral_sent_via": effective_mode,
                        },
                        tags=["line_referred"],
                    )
                except Exception as e:
                    logger.debug("[referral.send] canonical line_referred "
                                   "写回失败: %s", e)
        else:
            failed += 1
            if line_account_id:
                try:
                    _lp.mark_dispatch_outcome(
                        line_account_id, status="failed",
                        note=err_note or "send_message_returned_false")
                except Exception:
                    pass
            # Phase 12.2: peer 级失败计数 + 达阈值 tag referral_dead.
            # 只 PERMANENT code 计 peer 账 (transient/unknown 不算 peer 问题).
            if (canonical_id_of_peer
                    and err_code in _REFERRAL_PEER_FAIL_THRESHOLDS):
                try:
                    became_dead = _referral_peer_fail_record(
                        canonical_id_of_peer, err_code, peer_name)
                except Exception as e:
                    logger.debug("[referral.send] peer fail 累计失败: %s", e)

        outcomes.append({
            "peer_name": peer_name, "line_account_id": line_account_id,
            "line_id": meta.get("line_id"),
            "planned_event_id": ev.get("id"),
            "sent": ok, "err_code": err_code, "note": err_note,
            "effective_mode": effective_mode,
            "became_dead": became_dead,
        })

    stats = {
        "scanned": len(events),
        "sent": sent,
        "failed": failed,
        "skipped_dedup": skipped_dedup,
        "skipped_device": skipped_device,
        "skipped_mode": skipped_mode,
        "outcomes": outcomes,
        "dry_run": dry_run,
    }
    return True, "", stats


_FB_CAMPAIGN_DEFAULT_STEPS = ["warmup", "group_engage", "extract_members",
                              "add_friends", "check_inbox"]


def _run_facebook_campaign(fb, resolved, params):
    """Facebook 5 套预设串行剧本的服务端实现。

    与 TikTok 的 tiktok_campaign_run 同构,但步骤适配 FB 业务模型:
      warmup           — feed 浏览 + 点赞养号
      group_engage     — 进群浏览 + 评论
      extract_members  — 群成员提取入库
      add_friends      — 对入库成员发好友请求(带验证语)
      check_inbox      — 处理 Messenger / Message Requests / Friend Requests

    P1-3a: 断点续跑 —— run_id = 当前 task_id（或 params.resume_from_run_id）。
          已在 fb_campaign_runs 表里标记 completed 的步骤会被跳过，
          整段任务继续往下跑；state_json 里的累计指标会带过来。
    """
    import random as _r
    from src.host.fb_add_friend_gate import check_add_friend_gate as _fb_add_friend_gate

    steps = params.get("steps") or _FB_CAMPAIGN_DEFAULT_STEPS
    target_country = params.get("target_country", "")
    target_groups = params.get("target_groups") or params.get("group_names") or []
    if isinstance(target_groups, str):
        target_groups = [g.strip() for g in target_groups.split(",") if g.strip()]

    # P1-3a: 读取/建立运行记录
    resume_from = params.get("resume_from_run_id") or ""
    try:
        from src.host.fb_campaign_store import (start_run as _cr_start,
                                                 update_step as _cr_update,
                                                 finish_run as _cr_finish)
        run_id = resume_from or (_get_current_task_id() or "")
    except Exception:
        _cr_start = _cr_update = _cr_finish = None
        run_id = ""

    resumed_state: Dict[str, Any] = {}
    if run_id and _cr_start:
        resumed_state = _cr_start(
            run_id=run_id,
            device_id=resolved,
            task_id=(_get_current_task_id() or ""),
            preset_key=str(params.get("preset_key") or ""),
            total_steps=len(steps),
        ) or {}

    already_done = set(resumed_state.get("steps_completed") or [])

    result = {
        "card_type": "fb_campaign",
        "run_id": run_id,
        "resumed": bool(resumed_state and already_done),
        "steps_completed": list(already_done),
        "steps_failed": list(resumed_state.get("steps_failed") or []),
        "target_country": target_country,
        "extracted_members": int(resumed_state.get("extracted_members") or 0),
        "friend_requests_sent": int(resumed_state.get("friend_requests_sent") or 0),
        "messages_replied": int(resumed_state.get("messages_replied") or 0),
    }
    # 透传已抽取成员，add_friends 可以接着发
    if resumed_state.get("last_extracted_members"):
        result["last_extracted_members"] = resumed_state["last_extracted_members"]

    for idx, step in enumerate(steps):
        if step in already_done:
            logger.info("[FB Campaign] 跳过已完成步骤: %s (run=%s)", step, run_id[:12])
            continue
        try:
            if step == "warmup":
                stats = fb.browse_feed(
                    scroll_count=int(params.get("warmup_scrolls", 15)),
                    like_probability=float(params.get("warmup_like_prob", 0.2)),
                    device_id=resolved,
                )
                result.setdefault("warmup_stats", stats)

            elif step == "group_engage":
                if not hasattr(fb, "group_engage_session"):
                    result["steps_failed"].append({"step": step,
                                                   "error": "未实现 group_engage_session"})
                    continue
                first_group = target_groups[0] if target_groups else params.get("group_name", "")
                stats = fb.group_engage_session(
                    group_name=first_group,
                    max_posts=int(params.get("group_max_posts", 5)),
                    comment_probability=float(params.get("comment_probability", 0.2)),
                    device_id=resolved,
                    persona_key=params.get("persona_key") or None,
                    phase=params.get("phase") or params.get("phase_override") or None,
                )
                result.setdefault("group_engage_stats", stats)

            elif step == "extract_members":
                if not hasattr(fb, "extract_group_members"):
                    result["steps_failed"].append({"step": step,
                                                   "error": "未实现 extract_group_members"})
                    continue
                first_group = target_groups[0] if target_groups else params.get("group_name", "")
                members = fb.extract_group_members(
                    group_name=first_group,
                    max_members=int(params.get("extract_max_members", 30)),
                    device_id=resolved,
                    persona_key=params.get("persona_key") or None,
                    phase=params.get("phase") or params.get("phase_override") or None,
                ) or []
                result["extracted_members"] += len(members)
                result["last_extracted_members"] = members[:5]

            elif step == "add_friends":
                _gerr, _gmeta = _fb_add_friend_gate(resolved, params)
                if _gerr:
                    result["steps_failed"].append({"step": step, "error": _gerr, "meta": _gmeta})
                    continue
                targets = params.get("add_friend_targets") \
                          or result.get("last_extracted_members") \
                          or []
                # 2026-04-24 P0 fail-fast: 上游 extract 步骤返回 0 人且没有手工
                # add_friend_targets 时, 后续 add_friends 空 loop 会让 result
                # 欺骗性地显示 success=True. 明确标记 skipped + 原因.
                if not targets:
                    _skip_meta = {
                        "extracted_members": result.get("extracted_members", 0),
                        "has_manual_targets": bool(params.get("add_friend_targets")),
                    }
                    result["steps_failed"].append({
                        "step": step,
                        "error": "no_targets_upstream_zero_members",
                        "meta": _skip_meta,
                    })
                    logger.warning("[FB Campaign] add_friends skip — 0 targets "
                                    "(extracted=%s, manual=%s)",
                                    _skip_meta["extracted_members"],
                                    _skip_meta["has_manual_targets"])
                    continue
                note = params.get("verification_note") or ""
                greeting = params.get("greeting") or params.get("greeting_message") or ""
                max_n = int(params.get("max_friends_per_run", 5))
                # 2026-04-23: 默认把 add_friend 和打招呼串起来 (方案 A2)
                # 关闭方式: params.send_greeting_inline = False → 仅发好友请求
                # phase=cold_start/cooldown 时 send_greeting 自动被 playbook 关闭
                greet_inline = bool(params.get("send_greeting_inline", True))
                _pk = params.get("persona_key") or None
                _ph = params.get("phase") or None
                _pr = str(params.get("_preset_key", "") or params.get("preset_key", ""))
                sent = 0
                greeted = 0
                greet_results: List[Dict[str, Any]] = []
                for t in targets[:max_n]:
                    name = t.get("name") if isinstance(t, dict) else str(t)
                    if not name:
                        continue
                    if greet_inline and hasattr(fb, "add_friend_and_greet"):
                        # campaign: 把 source 下推到 automation 层 record(锁内)
                        _camp_src = params.get("source", "") or ""
                        if not _camp_src:
                            _tg = params.get("target_groups")
                            if isinstance(_tg, list) and _tg:
                                _camp_src = str(_tg[0])
                            elif isinstance(params.get("group_name"), str):
                                _camp_src = params.get("group_name") or ""
                        _ai_g = params.get("ai_dynamic_greeting")
                        _fsg = params.get("force_send_greeting")
                        _p10_extra = _phase10_task_extras(params)
                        res = fb.add_friend_and_greet(
                            name,
                            note=note,
                            greeting=greeting,
                            device_id=resolved,
                            persona_key=_pk,
                            phase=_ph,
                            preset_key=_pr,
                            source=_camp_src,
                            force=bool(params.get("force_add_friend")),
                            ai_dynamic_greeting=(bool(_ai_g) if _ai_g is not None else None),
                            force_send_greeting=(bool(_fsg) if _fsg is not None else None),
                            **_p10_extra,
                        ) or {}
                        ok = bool(res.get("add_friend_ok"))
                        if ok:
                            sent += 1
                        if res.get("greet_ok"):
                            greeted += 1
                        greet_results.append({
                            "name": name,
                            "add_friend_ok": ok,
                            "greet_ok": bool(res.get("greet_ok")),
                            "greet_skipped_reason": res.get("greet_skipped_reason", ""),
                        })
                    elif hasattr(fb, "add_friend_with_note"):
                        # campaign 场景: source 从 target_groups/group_name 推断
                        _camp_src = params.get("source", "") or ""
                        if not _camp_src:
                            _tg = params.get("target_groups")
                            if isinstance(_tg, list) and _tg:
                                _camp_src = str(_tg[0])
                            elif isinstance(params.get("group_name"), str):
                                _camp_src = params.get("group_name") or ""
                        _p10_extra2 = _phase10_task_extras(params)
                        ok = fb.add_friend_with_note(name, note=note,
                                                     safe_mode=True,
                                                     device_id=resolved,
                                                     persona_key=_pk,
                                                     phase=_ph,
                                                     source=_camp_src,
                                                     preset_key=_pr,
                                                     force=bool(params.get("force_add_friend")),
                                                     **_p10_extra2)
                        if ok:
                            sent += 1
                    else:
                        ok = fb.add_friend(name, device_id=resolved)
                        if ok:
                            sent += 1
                    # 2026-04-23 P3-1: automation 层(add_friend_with_note / add_friend_and_greet)
                    # 在锁内已 record sent 状态; 这里仅为失败补 risk。
                    if not ok:
                        try:
                            from src.host.fb_store import record_friend_request
                            _src = params.get("source", "") or ""
                            if not _src:
                                _tg = params.get("target_groups")
                                if isinstance(_tg, list) and _tg:
                                    _src = str(_tg[0])
                                elif isinstance(params.get("group_name"), str):
                                    _src = params.get("group_name") or ""
                            record_friend_request(
                                resolved, name,
                                note=note,
                                source=_src,
                                status="risk",
                                preset_key=_pr,
                            )
                        except Exception:
                            pass
                    time.sleep(_r.uniform(60, 180))
                result["friend_requests_sent"] += sent
                if greet_inline:
                    result["greetings_sent"] = result.get("greetings_sent", 0) + greeted
                    result["greet_details"] = greet_results

            elif step == "send_greeting":
                # 独立 send_greeting step —— 不配合 add_friends,按 params.targets
                # 逐个 search_people + 发打招呼(老朋友复访 / 手动触发场景)。
                # 日上限 / phase 由 send_greeting_after_add_friend 内部判定,
                # 这里不再叠 gate,避免与 add_friend 上限双扣。
                targets = params.get("greeting_targets") \
                          or params.get("add_friend_targets") \
                          or result.get("last_extracted_members") \
                          or []
                greeting = params.get("greeting") or params.get("greeting_message") or ""
                _pk = params.get("persona_key") or None
                _ph = params.get("phase") or None
                _pr = str(params.get("_preset_key", "") or params.get("preset_key", ""))
                # 2026-04-23 优化: 间隔从 playbook.send_greeting.inter_greeting_sec 读
                # (回退 fallback 120~300s 保持旧行为)
                try:
                    from src.host.fb_playbook import resolve_send_greeting_params
                    _sg_cfg = resolve_send_greeting_params(phase=_ph) or {}
                    _gap = _sg_cfg.get("inter_greeting_sec") or (120, 300)
                    _max_n_cfg = int(_sg_cfg.get("max_greetings_per_run", 3))
                except Exception:
                    _gap = (120, 300)
                    _max_n_cfg = 3
                # params.max_greetings_per_run 优先（显式覆盖）否则走 playbook
                max_n = int(params.get("max_greetings_per_run") or _max_n_cfg or 3)
                greeted = 0
                for t in targets[:max_n]:
                    name = t.get("name") if isinstance(t, dict) else str(t)
                    if not name:
                        continue
                    if not hasattr(fb, "send_greeting_after_add_friend"):
                        result["steps_failed"].append({"step": step,
                                                       "error": "未实现 send_greeting_after_add_friend"})
                        break
                    ok = fb.send_greeting_after_add_friend(
                        name,
                        greeting=greeting,
                        device_id=resolved,
                        persona_key=_pk,
                        phase=_ph,
                        assume_on_profile=False,
                        preset_key=_pr,
                    )
                    if ok:
                        greeted += 1
                    time.sleep(_r.uniform(float(_gap[0]), float(_gap[1])))
                result["greetings_sent"] = result.get("greetings_sent", 0) + greeted

            elif step == "check_inbox":
                _pk = params.get("persona_key") or None
                _ph = params.get("phase") or None
                if hasattr(fb, "check_messenger_inbox"):
                    stats = fb.check_messenger_inbox(
                        auto_reply=bool(params.get("auto_reply", False)),
                        max_conversations=int(params.get("max_conversations", 20)),
                        device_id=resolved,
                        persona_key=_pk,
                        phase=_ph,
                    ) or {}
                    result["messages_replied"] += int(stats.get("replied", 0))
                if hasattr(fb, "check_message_requests"):
                    fb.check_message_requests(
                        max_requests=int(params.get("max_requests", 10)),
                        device_id=resolved,
                        persona_key=_pk,
                        phase=_ph,
                    )
                if hasattr(fb, "check_friend_requests_inbox"):
                    fb.check_friend_requests_inbox(
                        accept_all=bool(params.get("accept_friend_requests", False)),
                        max_requests=int(params.get("max_friend_requests", 20)),
                        device_id=resolved,
                        persona_key=_pk,
                        phase=_ph,
                    )
            else:
                result["steps_failed"].append({"step": step, "error": "未知步骤"})
                continue

            result["steps_completed"].append(step)
            # P1-3a: 每步落盘，任务中途挂掉也能续跑
            if run_id and _cr_update:
                try:
                    _cr_update(run_id, idx, step, result)
                except Exception:
                    pass
            time.sleep(_r.uniform(20, 45))

        except Exception as step_err:
            logger.error("[FB Campaign] 步骤 %s 失败: %s", step, step_err)
            result["steps_failed"].append({"step": step, "error": str(step_err)})
            if run_id and _cr_update:
                try:
                    _cr_update(run_id, idx, step, result)
                except Exception:
                    pass
            time.sleep(8)

    try:
        from src.host.event_stream import push_event as _push
        _push("facebook.campaign_done", {
            "device_id": resolved,
            "steps_completed": result["steps_completed"],
            "steps_failed": [s["step"] for s in result["steps_failed"]],
            "extracted_members": result["extracted_members"],
            "friend_requests_sent": result["friend_requests_sent"],
            "messages_replied": result["messages_replied"],
        }, resolved)
    except Exception:
        pass

    # P1-3a: 收尾 — 全成功 / 部分 / 全失败
    if run_id and _cr_finish:
        try:
            if result["steps_failed"] and not result["steps_completed"]:
                _cr_finish(run_id, "failed", result)
            elif result["steps_failed"]:
                _cr_finish(run_id, "partial", result)
            else:
                _cr_finish(run_id, "completed", result)
        except Exception:
            pass

    return True, "", result


def _fresh_instagram(manager, resolved):
    from src.app_automation.instagram import InstagramAutomation
    ig = InstagramAutomation(device_manager=manager)
    ig.set_current_device(resolved)
    return ig


def _fresh_twitter(manager, resolved):
    from src.app_automation.twitter import TwitterAutomation
    tw = TwitterAutomation(device_manager=manager)
    tw.set_current_device(resolved)
    return tw


def _fresh_whatsapp(manager, resolved):
    from src.app_automation.whatsapp import WhatsAppAutomation
    wa = WhatsAppAutomation(manager)
    wa.set_current_device(resolved)
    d = manager.get_u2(resolved)
    if d:
        wa.start_whatsapp(resolved)
    return wa


def _get_leads_tracker():
    """Get the LeadsFollowTracker singleton."""
    from src.leads.follow_tracker import LeadsFollowTracker
    return LeadsFollowTracker()


def _make_progress_cb():
    """Build a progress callback using thread-local task context."""
    from src.utils.log_config import _task_context
    tid = getattr(_task_context, "task_id", "")
    if not tid:
        return None
    _last = [0]

    def _cb(pct: int, msg: str):
        if pct - _last[0] < 5 and pct < 99:
            return
        _last[0] = pct
        try:
            update_task_progress(tid, pct, msg)
        except Exception:
            pass
    return _cb


def _check_tiktok_version(manager, resolved) -> None:
    """
    检测 TikTok 应用版本，若版本变更则自动清除过期选择器缓存。
    使用 DeviceStateStore 存储上次已知版本。结果轻量缓存到进程内 dict 避免过频。
    """
    _KEY = "tiktok_app_version"
    try:
        # 进程内缓存：同一设备 30 分钟内不重复检测
        _cache = getattr(_check_tiktok_version, "_cache", {})
        _check_tiktok_version._cache = _cache
        import time as _time
        now = _time.time()
        if now - _cache.get(resolved, 0) < 1800:
            return

        result = manager.execute_command(
            resolved,
            "shell pm dump com.zhiliaoapp.musically | grep versionName | head -1"
        )
        if not result:
            result = manager.execute_command(
                resolved,
                "shell pm dump com.ss.android.ugc.trill | grep versionName | head -1"
            )
        if not result:
            return

        # 解析版本号：versionName=43.8.3
        import re as _re
        m = _re.search(r"versionName=([^\s]+)", result)
        if not m:
            return
        current_ver = m.group(1).strip()

        from src.host.device_state import get_device_state_store
        ds = get_device_state_store("tiktok")
        stored_ver = ds.get(resolved, _KEY)

        if stored_ver and stored_ver != current_ver:
            logger.info("[版本检测] 设备 %s TikTok 更新: %s → %s，清除过期选择器",
                        resolved[:8], stored_ver, current_ver)
            try:
                from src.vision.auto_selector import get_auto_selector
                sel = get_auto_selector()
                for pkg in ("com.zhiliaoapp.musically", "com.ss.android.ugc.trill"):
                    sel.sweep_stale_selectors(pkg, stale_days=0)  # 版本变更时强制清全部
            except Exception as e:
                logger.debug("[版本检测] 清除选择器失败: %s", e)

        if stored_ver != current_ver:
            ds.set(resolved, _KEY, current_ver)

        _cache[resolved] = now

    except Exception as e:
        logger.debug("[版本检测] 跳过: %s", e)


def _execute_tiktok(manager, resolved, task_type, params):
    """TikTok task dispatcher — handles all tiktok_* task types."""
    tt = _fresh_tiktok(manager, resolved)

    # 版本检测：TikTok 更新后自动清除过期选择器（静默，不阻断任务）
    _check_tiktok_version(manager, resolved)

    target_account = params.pop("account", None)

    if not target_account:
        try:
            from .account_scheduler import get_account_scheduler
            sched = get_account_scheduler()
            sched.auto_discover_accounts(resolved, manager)
            auto = sched.select_account(resolved, task_type)
            if auto:
                target_account = auto
                logger.info("[多账号] 自动选择账号 @%s (设备 %s)",
                            target_account, resolved[:8])
        except Exception as e:
            logger.debug("[多账号] 自动调度跳过: %s", e)

    if target_account:
        current = tt.get_current_account(resolved)
        if current != target_account:
            if not tt.switch_account(target_account, resolved):
                return False, f"无法切换到账号 @{target_account}", None
        try:
            from .account_scheduler import get_account_scheduler
            get_account_scheduler().start_session(resolved, target_account)
        except Exception:
            pass
        params["_active_account"] = target_account

    from src.host.device_state import DeviceStateStore
    state_device_id = DeviceStateStore.account_device_id(
        resolved, target_account) if target_account else resolved

    if task_type == "tiktok_warmup":
        # 前端部分入口传 duration，与 duration_minutes 统一
        if params.get("duration_minutes") is None and params.get("duration") is not None:
            try:
                params["duration_minutes"] = int(params["duration"])
            except (TypeError, ValueError):
                pass

        from src.host.device_state import get_device_state_store
        ds = get_device_state_store("tiktok")
        ds.init_device(state_device_id)

        # VPN 检查 + 静默重连 — 失败则中止任务
        vpn_ok, vpn_msg = _ensure_vpn(resolved)
        if not vpn_ok:
            return False, vpn_msg, {"vpn_status": "failed"}

        # Check if device is in recovery mode — override params
        try:
            from src.behavior.adaptive_compliance import get_adaptive_compliance
            ac = get_adaptive_compliance()
            if ac.is_recovering(state_device_id):
                recovery_params = ac.get_recovery_warmup_params(state_device_id)
                logger.info("[恢复模式] 设备 %s 使用恢复养号参数: %s",
                         state_device_id[:12], recovery_params)
                target_country = params.get("target_country", "italy")
                _rec_tid = _get_current_task_id()

                def _rec_ckpt(st, elapsed):
                    if _rec_tid:
                        _save_task_checkpoint(_rec_tid, {
                            "task_type": "tiktok_warmup",
                            "stats": st, "elapsed_sec": elapsed,
                            "phase": recovery_params["phase"],
                            "recovery_mode": True,
                        })

                stats = tt.warmup_session(
                    duration_minutes=recovery_params["duration_minutes"],
                    target_country=target_country,
                    phase=recovery_params["phase"],
                    checkpoint_callback=_rec_ckpt,
                )
                ds.record_warmup(state_device_id, stats)
                ac.record_recovery_session(state_device_id)
                return True, "", {
                    "warmup_stats": stats,
                    "phase": "recovery",
                    "recovery_mode": True,
                    "recovery_session": ac.get_risk_profile(state_device_id).get(
                        "recovery", {}),
                }
        except Exception:
            pass

        phase = params.get("phase", "auto")
        if phase == "auto":
            phase = ds.determine_phase(state_device_id)

        ab_params = None
        try:
            from src.behavior.ab_experiment import get_experiment_manager
            ab_params = get_experiment_manager().get_device_params(resolved)
        except Exception:
            pass

        if ab_params:
            for k, v in ab_params.items():
                if not k.startswith("_") and k not in params:
                    params[k] = v
            logger.info("[A/B] 实验 '%s' 变体 '%s' 应用于 %s",
                        ab_params.get("_experiment"),
                        ab_params.get("_variant"), resolved[:8])

        adaptive = None
        try:
            from src.behavior.account_profile import get_profile_manager
            adaptive = get_profile_manager().get_adaptive_params(
                resolved, target_account or "")
        except Exception:
            pass

        if adaptive and not params.get("_no_adaptive") and not ab_params:
            duration = params.get("duration_minutes") or adaptive.get("duration_minutes", 30)
            target_country = params.get("target_country", "italy")
            for k in ("like_probability", "comment_browse_prob",
                       "comment_post_prob", "search_prob"):
                if k not in params and k in adaptive:
                    params[k] = adaptive[k]
            logger.info("[画像] 使用自适应参数: algo=%.2f trend=%s dur=%d",
                        adaptive.get("_algo_score", 0),
                        adaptive.get("_trend", "?"), duration)
        else:
            duration = params.get("duration_minutes", 30)
            target_country = params.get("target_country", "italy")

        _tc = params.get("target_countries") or []
        if isinstance(_tc, str):
            _tc = [c.strip() for c in _tc.split(',') if c.strip()]
        _tl = params.get("target_languages") or []
        if isinstance(_tl, str):
            _tl = [l.strip() for l in _tl.split(',') if l.strip()]
        _geo_filter = bool(_tc) or bool(params.get("geo_filter", False))

        resume_cp = None
        cp_raw = params.get("_checkpoint")
        if cp_raw and cp_raw.get("task_type") == "tiktok_warmup":
            resume_cp = cp_raw

        task_id = _get_current_task_id()

        def _warmup_ckpt(st, elapsed):
            if task_id:
                _save_task_checkpoint(task_id, {
                    "task_type": "tiktok_warmup",
                    "stats": st,
                    "elapsed_sec": elapsed,
                    "phase": phase,
                    "duration_minutes": duration,
                    "target_country": target_country,
                    "geo_stats": st.get("geo_stats", {}),
                })

        stats = tt.warmup_session(
            duration_minutes=duration,
            target_country=target_country,
            phase=phase,
            target_countries=_tc,
            target_languages=_tl,
            geo_filter=_geo_filter,
            progress_callback=_make_progress_cb(),
            checkpoint_callback=_warmup_ckpt,
            resume_checkpoint=resume_cp,
        )
        ds.record_warmup(state_device_id, stats)

        try:
            from src.behavior.account_profile import get_profile_manager
            pm = get_profile_manager()
            pm.record_session(resolved, target_account or "", stats, duration)
            algo_score = ds.get_algorithm_learning_score(state_device_id)
            pm.record_algo_score(resolved, target_account or "", algo_score)
        except Exception:
            pass

        try:
            from src.behavior.ab_experiment import get_experiment_manager
            em = get_experiment_manager()
            a_score = ds.get_algorithm_learning_score(state_device_id)
            em.record_session(resolved, stats, algo_score=a_score)
        except Exception:
            pass

        return True, "", {"warmup_stats": stats, "phase": phase,
                          "resumed": bool(resume_cp)}

    elif task_type == "tiktok_browse_feed":
        video_count = params.get("video_count", 10)
        like_prob = params.get("like_probability", 0.25)
        target_country = params.get("target_country", "italy")
        minutes = int(video_count * 8 / 60) + 1

        resume_cp = None
        cp_raw = params.get("_checkpoint")
        if cp_raw and cp_raw.get("task_type") == "tiktok_browse_feed":
            resume_cp = cp_raw

        task_id = _get_current_task_id()

        def _feed_ckpt(st, elapsed):
            if task_id:
                _save_task_checkpoint(task_id, {
                    "task_type": "tiktok_browse_feed",
                    "stats": st,
                    "elapsed_sec": elapsed,
                    "video_count": video_count,
                })

        stats = tt.warmup_session(
            duration_minutes=minutes,
            like_probability=like_prob,
            target_country=target_country,
            phase="interest_building",
            progress_callback=_make_progress_cb(),
            checkpoint_callback=_feed_ckpt,
            resume_checkpoint=resume_cp,
        )
        return True, "", {"feed_stats": {
            "watched": stats["watched"], "likes": stats["liked"],
            "comments": 0, "follows": 0, "rewatches": 0,
        }, "resumed": bool(resume_cp)}

    elif task_type == "tiktok_test_follow":
        from src.host.device_state import get_device_state_store
        ds = get_device_state_store("tiktok")
        ds.init_device(state_device_id)
        d = tt._u2(resolved)  # 使用 ADB fallback
        if not tt.launch(resolved):
            return False, "TikTok 启动失败", None
        can_follow = tt._random_test_follow(d, resolved)
        ds.mark_can_follow(state_device_id, can_follow)
        ds.record_follow_test(state_device_id)
        return True, "", {"can_follow": can_follow, "phase": ds.get_phase(state_device_id)}

    elif task_type == "tiktok_follow":
        # VPN 检查 — VPN 断连时仅警告，不中止（允许在本地网络继续运行）
        vpn_ok, vpn_msg = _ensure_vpn(resolved)
        if not vpn_ok:
            logger.warning("[关注] %s: VPN未连接，继续执行（无意大利出口）", resolved[:12])
            vpn_msg = "not_connected"

        # P4-C: 验证 IP 是否在意大利（仅 VPN 已连接时才做硬检查）
        if vpn_msg in ("connected", "reconnected"):
            geo_ok, geo_msg = _ensure_italy_ip(resolved)
            if not geo_ok:
                return False, geo_msg, {"geo_status": "wrong_country", "vpn_status": vpn_msg}

        from src.app_automation.target_filter import TargetProfile
        from src.host.device_state import get_device_state_store
        ds = get_device_state_store("tiktok")
        ds.init_device(state_device_id)
        # P4-A: 每日关注安全上限检查
        _daily_followed = ds.get_int(state_device_id, f"daily:{ds._today()}:followed")
        _follow_daily_limit = params.get("daily_limit", 50)
        if _daily_followed >= _follow_daily_limit:
            logger.warning("[SAFETY] %s: 今日关注 %d 已达上限 %d，中止任务",
                           state_device_id[:8], _daily_followed, _follow_daily_limit)
            return False, (f"今日关注已达安全上限 {_follow_daily_limit}"
                           f"（已关注 {_daily_followed}），任务中止保护账号"), {
                "daily_followed": _daily_followed, "limit": _follow_daily_limit}
        _tc_countries = params.get("target_countries") or []
        if isinstance(_tc_countries, str):
            _tc_countries = [c.strip() for c in _tc_countries.split(',') if c.strip()]
        _tc_languages = params.get("target_languages") or []
        if isinstance(_tc_languages, str):
            _tc_languages = [l.strip() for l in _tc_languages.split(',') if l.strip()]
        target = TargetProfile(
            country=params.get("country") or params.get("target_country", "italy"),
            language=params.get("language", "italian"),
            gender=params.get("gender", ""),
            min_age=params.get("min_age", 0),
            countries=_tc_countries,
            languages=_tc_languages,
        )
        max_follows = params.get("max_follows") or ds.get_follow_ramp_max(state_device_id)
        seed_accounts = params.get("seed_accounts") or None
        tracker = _get_leads_tracker()
        resume_cp = None
        cp_raw = params.get("_checkpoint")
        if cp_raw and cp_raw.get("task_type") == "tiktok_follow":
            resume_cp = cp_raw

        task_id = _get_current_task_id()

        def _follow_ckpt(res, seeds_tried):
            if task_id:
                _save_task_checkpoint(task_id, {
                    "task_type": "tiktok_follow",
                    "result": res,
                    "seeds_tried": seeds_tried,
                })

        result = tt.smart_follow(
            target=target,
            max_follows=max_follows,
            seed_accounts=seed_accounts,
            global_tracker=tracker,
            checkpoint_callback=_follow_ckpt,
            resume_checkpoint=resume_cp,
            progress_callback=_make_progress_cb(),
            comment_warmup=params.get("comment_warmup", False),
        )
        ds.record_follows(state_device_id, result.get("followed", 0))
        return True, "", {"follow_result": result, "resumed": bool(resume_cp)}

    elif task_type in ("tiktok_chat", "tiktok_check_and_chat_followbacks"):
        # P4-C: VPN + 意大利 IP 检查
        vpn_ok, vpn_msg = _ensure_vpn(resolved)
        if not vpn_ok:
            return False, vpn_msg, {"vpn_status": "failed"}
        if vpn_msg in ("connected", "reconnected"):
            geo_ok, geo_msg = _ensure_italy_ip(resolved)
            if not geo_ok:
                return False, geo_msg, {"geo_status": "wrong_country", "vpn_status": vpn_msg}

        from src.host.device_state import get_device_state_store
        ds = get_device_state_store("tiktok")
        # P4-A: 每日 DM 安全上限检查
        _daily_dms = ds.get_int(state_device_id, f"daily:{ds._today()}:dms")
        _dm_daily_limit = params.get("daily_dm_limit", 80)
        if _daily_dms >= _dm_daily_limit:
            logger.warning("[SAFETY] %s: 今日私信 %d 已达上限 %d，中止任务",
                           state_device_id[:8], _daily_dms, _dm_daily_limit)
            return False, (f"今日私信已达安全上限 {_dm_daily_limit}"
                           f"（已发送 {_daily_dms}），任务中止保护账号"), {
                "daily_dms": _daily_dms, "limit": _dm_daily_limit}
        messages = params.get("messages", [])
        if not messages:
            try:
                chat_path = config_file("chat_messages.yaml")
                if chat_path.exists():
                    with open(chat_path, "r", encoding="utf-8") as f:
                        data = yaml.safe_load(f)
                    # A/B variant selection
                    variants = data.get("message_variants", [])
                    if variants:
                        try:
                            from src.host.ab_stats import select_variant, record_sent
                            selected = select_variant(variants)
                            messages = selected.get("greeting_messages", selected.get("messages", []))
                            _ab_variant_id = selected.get("id", "default")
                            record_sent(_ab_variant_id, state_device_id)
                            logger.info("[A/B] Selected variant: %s for device %s", _ab_variant_id, state_device_id[:8])
                        except Exception:
                            messages = data.get("messages", [])
                            _ab_variant_id = "default"
                    else:
                        messages = data.get("messages", [])
                        _ab_variant_id = "default"
            except Exception:
                pass
        if not messages:
            return False, "未配置聊天话术 (messages)", None

        max_chats = params.get("max_chats", 10)
        _tl_chat = params.get("target_languages", [])
        tracker = _get_leads_tracker()
        # P8-C: 把当前 A/B 变体 ID 挂到 tracker 上，tiktok.py 的 record_dm 读取
        tracker._current_ab_variant = _ab_variant_id
        result = tt.check_and_chat_followbacks(
            messages, max_chats, target_languages=_tl_chat or None,
            global_tracker=tracker,
            progress_callback=_make_progress_cb(),
        )
        ds.record_chats(state_device_id, result.get("messaged", 0))
        return True, "", {"chat_result": result}

    elif task_type == "tiktok_send_dm":
        recipient = params.get("recipient") or params.get("username") or ""
        message = params.get("message", "")
        if not recipient:
            return False, "params.recipient 必填", None
        if not message:
            return False, "params.message 必填", None

        # Timezone-aware scheduling: defer if outside optimal window
        defer_if_outside = params.get("smart_timing", False)
        if defer_if_outside:
            from src.workflow.smart_schedule import best_send_time
            best_time = best_send_time(
                params.get("lead_timezone", "IT"),
                platform="tiktok",
            )
            if best_time:
                from datetime import datetime, timezone as tz
                now = datetime.now(tz.utc)
                delta = (best_time - now).total_seconds()
                if delta > 300:
                    from .task_store import create_task as _ct
                    deferred_params = dict(params)
                    deferred_params.pop("smart_timing", None)
                    _ct(task_type="tiktok_send_dm", device_id=resolved,
                        params=deferred_params)
                    return True, "", {
                        "deferred": True,
                        "best_send_time": best_time.isoformat(),
                        "reason": f"Deferred by {delta/60:.0f}min for optimal timing",
                    }

        # P5: 跨设备 DM 去重检查（可通过 params.force=true 绕过）
        if not params.get("force"):
            _tracker = _get_leads_tracker()
            if _tracker.was_dm_sent(recipient):
                logger.info("[DM去重] %s 已收过 DM，send_dm 跳过（传 force=true 可强制）", recipient)
                return True, "", {"skipped_dedup": True, "recipient": recipient}
        ok = tt.send_dm(recipient, message)
        return ok, ("" if ok else f"发送DM给 {recipient} 失败"), None

    elif task_type == "tiktok_check_inbox":
        # 收件箱检查不强制要求VPN（读写DM不依赖IP地理位置）
        _vpn_s = None
        try:
            from src.behavior.vpn_manager import check_vpn_status as _cvs
            _vpn_s = _cvs(resolved)
        except Exception:
            pass
        if _vpn_s and not _vpn_s.connected:
            logger.warning("[收件箱] %s: VPN未连接，仍继续执行（收件箱不强制VPN）", resolved[:12])

        # 从 config/ai.yaml 读取全局默认值（如果 yaml 中 auto_reply.enabled=true 则默认开启）
        _ai_default_reply = False
        try:
            _ai_cfg_path = config_file("ai.yaml")
            with open(_ai_cfg_path, encoding="utf-8") as _f:
                import yaml as _yaml
                _ai_yaml = _yaml.safe_load(_f) or {}
            _ai_default_reply = bool((_ai_yaml.get("auto_reply") or {}).get("enabled", False))
        except Exception:
            pass
        auto_reply = params.get("auto_reply", _ai_default_reply)
        max_conversations = params.get("max_conversations", 20)
        _tl_inbox = params.get("target_languages", [])
        if isinstance(_tl_inbox, str):
            _tl_inbox = [x.strip() for x in _tl_inbox.split(",") if x.strip()]
        result = tt.check_inbox(
            auto_reply=auto_reply,
            max_conversations=max_conversations,
            target_languages=_tl_inbox or None,
            progress_callback=_make_progress_cb(),
        )
        # Push real-time event so frontend refreshes conversation list
        try:
            from src.host.event_stream import push_event as _push_ev
            new_msgs = result.get("new_messages", 0) if isinstance(result, dict) else 0
            replied = result.get("auto_replied", 0) if isinstance(result, dict) else 0
            _push_ev("tiktok.inbox_checked", {
                "device_id": resolved,
                "new_messages": new_msgs,
                "auto_replied": replied,
            }, resolved)
            # Adaptive scheduling: if new messages found, schedule a quick follow-up check in 3 minutes
            _allow_followup = True
            try:
                from src.host.task_policy import policy_blocks_executor_inbox_followup
                _allow_followup = not policy_blocks_executor_inbox_followup()
            except Exception:
                pass
            if (
                _allow_followup
                and new_msgs > 0
                and params.get("auto_reply")
                and not params.get("_is_followup")
            ):
                try:
                    import threading as _threading
                    import time as _time
                    from src.host.api import task_store as _ts2, get_worker_pool as _gwp
                    def _quick_followup():
                        _time.sleep(180)  # 3 minutes
                        try:
                            from src.host.task_origin import with_origin as _wo

                            _tid = _ts2.create_task(
                                task_type="tiktok_check_inbox",
                                device_id=resolved,
                                params=_wo(
                                    {
                                        "auto_reply": True,
                                        "max_conversations": 50,
                                        "_is_followup": True,
                                    },
                                    "executor_followup_inbox",
                                ),
                            )
                            _gwp().submit(_tid, resolved, run_task, _tid, config_path or "")
                        except Exception:
                            pass
                    _threading.Thread(target=_quick_followup, daemon=True).start()
                except Exception:
                    pass
        except Exception:
            pass
        # 将 auto_reply DM 数量同步到 device_state（修复 dms_today 低估问题）
        if isinstance(result, dict):
            _auto_replied_n = result.get("auto_replied", 0)
            if _auto_replied_n > 0:
                try:
                    from src.host.device_state import get_device_state_store as _get_ds_inbox
                    _get_ds_inbox("tiktok").record_chats(state_device_id, _auto_replied_n)
                except Exception:
                    pass
        # N1 自动升级：NEEDS_REPLY 意图 → qualified + 话术补发（从 conversations 结果提取）
        try:
            _convs = result.get("conversations", []) if isinstance(result, dict) else []
            _needs_reply_contacts = [
                c.get("contact", "") for c in _convs
                if (c.get("intent") or "").upper() == "NEEDS_REPLY"
                and c.get("contact")
            ]
            if _needs_reply_contacts:
                from ..leads.store import get_leads_store as _gls_inbox
                _lstore = _gls_inbox()
                _qualified_n = 0
                _pitch_scheduled = 0
                for _contact in _needs_reply_contacts:
                    _lid_inbox = _lstore.find_by_platform_username("tiktok", _contact)
                    if _lid_inbox:
                        _lead_cur = _lstore.get_lead(_lid_inbox)
                        if _lead_cur and _lead_cur.get("status") in ("responded", "new", "contacted"):
                            _lstore.update_lead(_lid_inbox, status="qualified")
                            _qualified_n += 1
                        # 话术补发：如无已发 referral/auto_reply，补发引流话术
                        try:
                            _intrs = _lstore.get_interactions(_lid_inbox, platform="tiktok")
                            _has_pitch = any(
                                ix.get("direction") == "outbound" and
                                ix.get("action") in ("referral_sent", "auto_reply", "pitch", "send_dm")
                                for ix in _intrs
                            )
                            if not _has_pitch:
                                # ── 通用引流话术：支持任意 app + 语言检测 + AI生成 ──
                                _ref_msg = ""
                                try:
                                    import yaml as _yaml2
                                    _cfg2 = _yaml2.safe_load(open(
                                        config_file("chat_messages.yaml"),
                                        encoding="utf-8"))
                                    _dev_refs = (_cfg2 or {}).get("device_referrals", {}).get(resolved, {})
                                    # 通用：支持所有 app（不限 TG/WA）
                                    _all_refs_ib = {k: v for k, v in _dev_refs.items()
                                                    if v and not k.startswith("_")}
                                    _AP_P_IB = ["telegram","whatsapp","instagram","line",
                                                "wechat","viber","signal","facebook"]
                                    _AP_L_IB = {"telegram":"Telegram","whatsapp":"WhatsApp",
                                                "instagram":"Instagram","line":"Line","wechat":"WeChat",
                                                "viber":"Viber","signal":"Signal","facebook":"Facebook"}
                                    _c_p_ib = []
                                    for _ap in _AP_P_IB:
                                        if _ap in _all_refs_ib:
                                            _c_p_ib.append(f"{_AP_L_IB[_ap]}: {_all_refs_ib[_ap]}")
                                    for _ap, _v in _all_refs_ib.items():
                                        if _ap not in _AP_P_IB:
                                            _c_p_ib.append(f"{_ap.capitalize()}: {_v}")
                                    _contact_ib = " / ".join(_c_p_ib[:2])

                                    if _contact_ib:
                                        # 语言检测（基于对方最后一条入站消息）
                                        _last_in_ib = next(
                                            (ix for ix in _intrs if ix.get("direction") == "inbound"),
                                            None)
                                        _last_msg_ib = (_last_in_ib or {}).get("content", "")

                                        def _det_lang_ib(text):
                                            if not text: return "it"
                                            if any('\u4e00' <= c <= '\u9fff' for c in text): return "zh"
                                            if any('\u0600' <= c <= '\u06ff' for c in text): return "ar"
                                            _w = set(text.lower().split())
                                            _sc = {
                                                "it": len(_w & {"ciao","grazie","buongiorno","come","cosa","dove","ho","mi","ti","sono","che","per","sì","si","bene","ok","voglio","posso"}),
                                                "es": len(_w & {"hola","gracias","buenos","como","que","donde","quiero","puedo","por","me","te","no","hay","tengo"}),
                                                "en": len(_w & {"hello","hi","thanks","thank","you","the","is","what","how","where","can","my","your","have","yes","no","ok","hey","great"}),
                                                "fr": len(_w & {"bonjour","merci","salut","oui","non","comment","veux","peux","mon","ton","je","tu","est"}),
                                                "de": len(_w & {"hallo","danke","bitte","ja","nein","wie","was","ich","du","wir","ist","sind"}),
                                            }
                                            _b = max(_sc, key=lambda k: _sc[k])
                                            return _b if _sc[_b] > 0 else "it"

                                        _lang_ib = _det_lang_ib(_last_msg_ib)
                                        # 多语言模板（兜底，AI生成失败时使用）
                                        _ib_tpls = {
                                            "it": f"Ciao! Se vuoi restare in contatto, scrivimi: {_contact_ib} 📲",
                                            "en": f"Hey! Let's connect — reach me here: {_contact_ib} 📲",
                                            "es": f"¡Hola! Escríbeme aquí para mantenernos en contacto: {_contact_ib} 📲",
                                            "fr": f"Salut! Pour rester en contact, écris-moi: {_contact_ib} 📲",
                                            "de": f"Hallo! Schreib mir hier: {_contact_ib} 📲",
                                        }
                                        _ref_msg = _ib_tpls.get(_lang_ib, _ib_tpls["it"])

                                        # AI 生成更自然的引流消息（有上下文或非意大利语）
                                        if _last_msg_ib or _lang_ib != "it":
                                            try:
                                                from ..ai.llm_client import get_llm_client as _gllm_ib
                                                _llm_ib = _gllm_ib()
                                                _ln_map_ib = {"it":"Italian","en":"English",
                                                              "es":"Spanish","fr":"French",
                                                              "de":"German","zh":"Chinese"}
                                                _ln_ib = _ln_map_ib.get(_lang_ib, "the same language as the user")
                                                _sys_ib = "You generate short, natural TikTok DM referrals. Human tone. No emoji overuse."
                                                _usr_ib = (
                                                    f"User @{_contact} just showed strong interest on TikTok.\n"
                                                    + (f"Their message: 「{_last_msg_ib[:150]}」\n" if _last_msg_ib else "")
                                                    + f"Our contact: {_contact_ib}\n"
                                                    f"Language: {_ln_ib}\n"
                                                    "Write a 10-25 word referral DM. Include the contact info naturally. Friendly. Output only the message."
                                                )
                                                _ai_ib = _llm_ib.chat_with_system(
                                                    _sys_ib, _usr_ib, temperature=0.8,
                                                    max_tokens=60, use_cache=False)
                                                _ai_ib = (_ai_ib or "").strip().strip('"').strip("「」")
                                                if len(_ai_ib) > 8:
                                                    _ref_msg = _ai_ib
                                            except Exception:
                                                pass  # 保留模板兜底
                                except Exception:
                                    pass
                                if _ref_msg:
                                    from .task_store import create_task as _ct_pitch
                                    _tid_pitch = _ct_pitch(
                                        task_type="tiktok_send_dm",
                                        device_id=resolved,
                                        params={"recipient": _contact, "message": _ref_msg,
                                                "lead_id": _lid_inbox, "smart_timing": True},
                                        priority=80)
                                    from .api import get_worker_pool as _gwp_pitch
                                    from src.host.executor import run_task as _rt_pitch
                                    _gwp_pitch().submit(_tid_pitch, resolved, _rt_pitch, _tid_pitch, config_path or "")
                                    _pitch_scheduled += 1
                        except Exception:
                            pass
                if _qualified_n:
                    logger.info("[收件箱] 自动升级 %d 条 NEEDS_REPLY 线索为 qualified，补发话术 %d 条",
                                _qualified_n, _pitch_scheduled)
        except Exception:
            pass
        return True, "", {"inbox_result": result}

    elif task_type == "tiktok_follow_up":
        from ..workflow.conversation_fsm import check_all_follow_ups
        from src.host.device_state import get_device_state_store as _ds_fu
        _ds_follow_up = _ds_fu("tiktok")
        target_country = params.get("target_country", "italy")
        pending = check_all_follow_ups("tiktok",
                                        max_leads=params.get("max_leads", 30),
                                        target_country=target_country)

        # ── 语言检测工具（基于词表，零外部依赖，~0ms）──
        def _detect_lang_fu(text: str) -> str:
            if not text: return "it"
            if any('\u4e00' <= c <= '\u9fff' for c in text): return "zh"
            if any('\u0600' <= c <= '\u06ff' for c in text): return "ar"
            if any('\u0400' <= c <= '\u04ff' for c in text): return "ru"
            _w = set(text.lower().split())
            _scores = {
                "it": len(_w & {"ciao", "grazie", "buongiorno", "prego", "come", "cosa",
                                "dove", "quando", "voglio", "posso", "ho", "mi", "ti",
                                "sono", "che", "per", "del", "sì", "si", "bene", "ok"}),
                "es": len(_w & {"hola", "gracias", "buenos", "como", "que", "donde",
                                "quiero", "puedo", "por", "del", "me", "te", "hay", "tengo"}),
                "en": len(_w & {"hello", "hi", "thanks", "thank", "you", "the", "is",
                                "are", "what", "how", "where", "can", "my", "your",
                                "have", "has", "yes", "no", "ok", "hey", "great"}),
                "fr": len(_w & {"bonjour", "merci", "salut", "oui", "non", "comment",
                                "quoi", "veux", "peux", "mon", "ton", "nous", "vous", "je"}),
                "de": len(_w & {"hallo", "danke", "bitte", "ja", "nein", "wie", "was",
                                "ich", "du", "wir", "ist", "sind", "mein"}),
            }
            _best = max(_scores, key=lambda k: _scores[k])
            return _best if _scores[_best] > 0 else "it"

        # ── 加载设备引流配置（所有 app）──
        import yaml as _fu_yaml
        _fu_cfg = config_file("chat_messages.yaml")
        _fu_refs = {}
        try:
            if _fu_cfg.exists():
                _fu_refs = (_fu_yaml.safe_load(_fu_cfg.read_text(encoding="utf-8")) or {}).get(
                    "device_referrals", {}).get(resolved, {})
        except Exception: pass
        _AP_PRIO_FU = ["telegram", "whatsapp", "instagram", "line", "wechat", "viber", "signal"]
        _AP_LABELS_FU = {"telegram": "Telegram", "whatsapp": "WhatsApp", "instagram": "Instagram",
                         "line": "Line", "wechat": "WeChat", "viber": "Viber", "signal": "Signal"}
        _all_refs = {k: v for k, v in _fu_refs.items() if v and not k.startswith("_")}
        _contact_parts_fu = []
        for _ap in _AP_PRIO_FU:
            if _ap in _all_refs:
                _contact_parts_fu.append(f"{_AP_LABELS_FU[_ap]}: {_all_refs[_ap]}")
        for _ap, _v in _all_refs.items():
            if _ap not in _AP_PRIO_FU:
                _contact_parts_fu.append(f"{_ap.capitalize()}: {_v}")
        _contact_str_fu = " / ".join(_contact_parts_fu[:2])

        sent = 0
        for fu in pending:
            template = fu.get("template", "")
            lead_name = fu.get("lead_name", "")
            if not (template and lead_name):
                continue
            msg = template.replace("{name}", lead_name)
            # 注入联系方式（模板占位符 {contact} 或追加）
            if _contact_str_fu:
                if "{contact}" in msg:
                    msg = msg.replace("{contact}", _contact_str_fu)
                else:
                    msg = msg + " " + _contact_str_fu
            try:
                from ..leads.store import get_leads_store
                store = get_leads_store()

                # ── 获取线索最近入站消息（用于语言检测）──
                _recent_ixs = store.get_interactions(fu["lead_id"], platform="tiktok", limit=10)
                _last_inbound = next(
                    (ix for ix in _recent_ixs if ix.get("direction") == "inbound"), None)
                _last_msg_txt = (_last_inbound or {}).get("content", "")
                _detected_lang = _detect_lang_fu(_last_msg_txt)

                # ── AI 协同生成（语言自适应）──
                _ai_msg = ""
                _lang_names = {"it": "Italian", "en": "English", "es": "Spanish",
                               "fr": "French", "de": "German", "zh": "Chinese",
                               "ar": "Arabic", "ru": "Russian"}
                _lang_name = _lang_names.get(_detected_lang, "the same language as the user")
                try:
                    from ..ai.llm_client import get_llm_client as _get_llm_fu
                    _llm_fu = _get_llm_fu()
                    _sys_fu = (
                        "You are a TikTok DM specialist sending follow-up messages to social media leads. "
                        "Your job: write natural, friendly follow-up DMs that feel human, not robotic. "
                        "Always write in the specified language. Include the contact info naturally."
                    )
                    _usr_fu = (
                        f"Lead name: {lead_name}\n"
                        f"Their last message: 「{_last_msg_txt[:150]}」\n" if _last_msg_txt else
                        f"Lead name: {lead_name}\n"
                    ) + (
                        f"Our contact: {_contact_str_fu}\n"
                        f"Follow-up template (use as style guide, rewrite naturally): {template[:200]}\n"
                        f"Language: {_lang_name}\n"
                        f"Rules: 15-40 words, friendly, include contact info, no hashtags, no quotes.\n"
                        f"Output only the message."
                    )
                    _ai_msg = _llm_fu.chat_with_system(
                        _sys_fu, _usr_fu, temperature=0.82, max_tokens=90, use_cache=False)
                    _ai_msg = (_ai_msg or "").strip().strip('"').strip("「」")
                    if len(_ai_msg) < 5: _ai_msg = ""
                except Exception as _ai_e:
                    logger.debug("[follow_up] AI生成失败: %s", _ai_e)

                # 最终消息：AI > 模板+联系方式（同语言使用模板；非默认语言优先 AI）
                if _ai_msg and (_detected_lang != "it" or _last_msg_txt):
                    msg = _ai_msg
                # 否则保留模板消息（已注入联系方式）

                profiles = store.get_platform_profiles(fu["lead_id"])
                tt_profile = next(
                    (p for p in profiles if p.get("platform") == "tiktok"), None)
                if tt_profile and tt_profile.get("username"):
                    uname = tt_profile["username"]
                    # P5: 跨设备 DM 去重 — 已发过的跳过
                    _tracker = _get_leads_tracker()
                    if _tracker.was_dm_sent(uname):
                        logger.info("[DM去重] %s 已收过 DM，跳过 follow_up", uname)
                        continue
                    ok = tt.send_dm(uname, msg)
                    if ok:
                        sent += 1
                        # P6: 记入每日 DM 配额（follow_up 也消耗名额）
                        _ds_follow_up.record_chats(state_device_id, 1)
                        store.add_interaction(
                            fu["lead_id"], "tiktok", "follow_up",
                            direction="outbound", content=msg[:500],
                            metadata={"follow_up_number": fu["follow_up_number"],
                                      "state": fu["state"],
                                      "detected_lang": _detected_lang,
                                      "ai_generated": bool(_ai_msg)},
                        )
            except Exception as e:
                logger.debug("[follow_up] Failed for lead #%d: %s", fu["lead_id"], e)
        return True, "", {"pending": len(pending), "sent": sent}

    elif task_type == "tiktok_auto":
        result = _execute_tiktok_auto(tt, manager, resolved, params)
        return True, "", result

    elif task_type == "tiktok_status":
        tracker = _get_leads_tracker()
        stats = tracker.get_stats()
        device_follows = tracker.get_device_follows(resolved)
        return True, "", {
            "device_id": resolved,
            "device_follows": device_follows,
            "platform_stats": stats,
        }

    elif task_type == "tiktok_workflow":
        return False, "tiktok_workflow 由 WorkflowExecutor 处理", None

    elif task_type == "tiktok_scan_username":
        username = tt.scan_own_username(resolved)
        if username:
            from src.host.device_state import get_device_state_store
            ds = get_device_state_store("tiktok")
            ds.set(resolved, "tiktok_username", username)
        return True, "", {"username": username or "", "device_id": resolved}

    elif task_type == "tiktok_follow_user":
        target = params.get("target_username", "")
        if not target:
            return False, "target_username 必填", None
        ok = tt.follow_user(target, resolved)
        return ok, ("" if ok else f"关注 {target} 失败"), {"target": target, "followed": ok}

    elif task_type == "tiktok_interact_user":
        target = params.get("target_username", "")
        if not target:
            return False, "target_username 必填", None
        result = tt.interact_with_user(
            target, resolved,
            watch_seconds=params.get("watch_seconds", 15),
            do_like=params.get("do_like", True),
            do_comment=params.get("do_comment", False),
        )
        # 互动成功后记录时间戳，用于去重保护
        if result.get("ok"):
            try:
                import time as _t
                from src.host.device_state import get_device_state_store
                _ds = get_device_state_store("tiktok")
                _key = f"last_interact_{target.lstrip('@').lower()}"
                _ds.set(resolved, _key, str(int(_t.time())))
            except Exception:
                pass
        return True, "", result

    elif task_type == "tiktok_keyword_search":
        # 关键词搜索获客：搜索目标关键词 → 评论预热 → 关注精准用户
        _tc_kw = params.get("target_countries", [])
        if isinstance(_tc_kw, str):
            _tc_kw = [x.strip() for x in _tc_kw.split(",") if x.strip()]
        _tl_kw = params.get("target_languages", [])
        if isinstance(_tl_kw, str):
            _tl_kw = [x.strip() for x in _tl_kw.split(",") if x.strip()]
        _kw_list = params.get("keywords", None)
        if isinstance(_kw_list, str):
            _kw_list = [x.strip() for x in _kw_list.split(",") if x.strip()]
        _max_kw_follows = params.get("max_follows", 20)
        _comment_warmup_kw = params.get("comment_warmup", True)

        result = tt.keyword_search_session(
            target_countries=_tc_kw or None,
            target_languages=_tl_kw or None,
            keywords=_kw_list,
            max_follows=_max_kw_follows,
            comment_warmup=_comment_warmup_kw,
            device_id=resolved,
            progress_callback=_make_progress_cb(),
        )
        try:
            from src.host.device_state import get_device_state_store as _gdss_kw
            _ds_kw = _gdss_kw("tiktok")
            _ds_kw.record_follows(resolved, result.get("followed", 0))
        except Exception:
            pass
        try:
            from src.host.event_stream import push_event as _push_kw
            _push_kw("tiktok.keyword_search_done", {
                "device_id": resolved,
                "followed": result.get("followed", 0),
                "comment_warmed": result.get("comment_warmed", 0),
                "keywords_used": result.get("keywords_used", []),
            }, resolved)
        except Exception:
            pass
        return True, "", result

    elif task_type == "tiktok_live_engage":
        # 直播间互动：进入直播间发评论 → 关注活跃观众（P2-3: 支持 targeting 过滤）
        _tc_live = params.get("target_countries", [])
        if isinstance(_tc_live, str):
            _tc_live = [x.strip() for x in _tc_live.split(",") if x.strip()]
        # 兜底：target_country 单值转数组
        if not _tc_live and params.get("target_country"):
            _tc_live = [params["target_country"]]
        _tl_live = params.get("target_languages", [])
        if isinstance(_tl_live, str):
            _tl_live = [x.strip() for x in _tl_live.split(",") if x.strip()]

        result = tt.live_engage_session(
            target_countries=_tc_live or None,
            target_languages=_tl_live or None,
            max_live_rooms=params.get("max_live_rooms", 3),
            comments_per_room=params.get("comments_per_room", 2),
            follow_active_viewers=params.get("follow_active_viewers", True),
            # ★ P2-3: targeting 透传
            gender=params.get("gender", ""),
            min_age=int(params.get("min_age", 0) or 0),
            max_age=int(params.get("max_age", 0) or 0),
            device_id=resolved,
            progress_callback=_make_progress_cb(),
        )
        try:
            from src.host.device_state import get_device_state_store as _gdss_live
            _ds_live = _gdss_live("tiktok")
            total_followed = result.get("hosts_followed", 0) + result.get("viewers_followed", 0)
            if total_followed > 0:
                _ds_live.record_follows(resolved, total_followed)
        except Exception:
            pass
        try:
            from src.host.event_stream import push_event as _push_live
            _push_live("tiktok.live_engage_done", {
                "device_id": resolved,
                "rooms_visited": result.get("rooms_visited", 0),
                "comments_sent": result.get("comments_sent", 0),
                "hosts_followed": result.get("hosts_followed", 0),
                "viewers_followed": result.get("viewers_followed", 0),
                "viewers_filtered": result.get("viewers_filtered", 0),
            }, resolved)
        except Exception:
            pass
        return True, "", result

    elif task_type == "tiktok_comment_engage":
        # ★ P2-1: 评论区互动 — 搜索热门视频→评论→关注活跃评论者
        result = tt.comment_engage_session(
            target_country=params.get("target_country", "italy"),
            keyword=params.get("keyword", ""),
            max_videos=int(params.get("max_videos", 5)),
            comments_per_video=int(params.get("comments_per_video", 2)),
            follow_commenters=params.get("follow_commenters", True),
            gender=params.get("gender", ""),
            min_age=int(params.get("min_age", 0) or 0),
            max_age=int(params.get("max_age", 0) or 0),
            device_id=resolved,
            progress_callback=_make_progress_cb(),
        )
        try:
            from src.host.device_state import get_device_state_store as _gdss_ce
            _ds_ce = _gdss_ce("tiktok")
            if result.get("followed", 0) > 0:
                _ds_ce.record_follows(resolved, result["followed"])
        except Exception:
            pass
        try:
            from src.host.event_stream import push_event as _push_ce
            _push_ce("tiktok.comment_engage_done", {
                "device_id": resolved,
                "videos_visited": result.get("videos_visited", 0),
                "comments_sent": result.get("comments_sent", 0),
                "followed": result.get("followed", 0),
                "filtered": result.get("filtered", 0),
            }, resolved)
        except Exception:
            pass
        return True, "", result

    elif task_type == "tiktok_check_comment_replies":
        # ★ P3-3: 检查我方视频的评论回复并发送DM（定时调度触发）
        result = tt.check_comment_replies_session(
            max_replies=int(params.get("max_replies", 20)),
            target_languages=params.get("target_languages", None),
            device_id=resolved,
        )
        try:
            from src.host.event_stream import push_event as _push_ccr
            _push_ccr("tiktok.comment_replies_checked", {
                "device_id": resolved,
                "replies_found": result.get("replies_found", 0),
                "dms_sent": result.get("dms_sent", 0),
            }, resolved)
        except Exception:
            pass
        return True, "", result

    elif task_type == "tiktok_campaign_run":
        # ★ P2-4: 完整获客剧本串行执行（在单一任务内顺序执行所有步骤）
        # 比 campaign_playbook 创建多个独立任务更可靠，保证顺序且中途失败可记录
        steps = params.get("steps") or ["warmup", "live_engage", "follow", "check_inbox"]
        tc = params.get("target_country", "italy")
        gender = params.get("gender", "")
        min_age = int(params.get("min_age", 0) or 0)
        max_age = int(params.get("max_age", 0) or 0)

        campaign_result = {
            "steps_completed": [],
            "steps_failed": [],
            "target_country": tc,
            "gender": gender,
            "min_age": min_age,
            "max_age": max_age,
        }

        cb = _make_progress_cb()
        step_count = len(steps)

        for step_idx, step_name in enumerate(steps):
            if cb:
                pct = int((step_idx / step_count) * 90) + 5
                cb(pct, f"剧本步骤 {step_idx + 1}/{step_count}: {step_name}")

            log.info("[Campaign] 执行步骤 %d/%d: %s", step_idx + 1, step_count, step_name)
            try:
                if step_name == "warmup":
                    tt.warmup_session(
                        duration_minutes=params.get("warmup_minutes", 20),
                        target_country=tc,
                        device_id=resolved,
                        progress_callback=None,
                    )
                elif step_name == "live_engage":
                    tt.live_engage_session(
                        target_countries=[tc],
                        max_live_rooms=params.get("max_live_rooms", 3),
                        comments_per_room=2,
                        follow_active_viewers=True,
                        gender=gender,
                        min_age=min_age,
                        max_age=max_age,
                        device_id=resolved,
                    )
                elif step_name == "comment_engage":
                    tt.comment_engage_session(
                        target_country=tc,
                        max_videos=params.get("max_videos", 4),
                        comments_per_video=2,
                        follow_commenters=True,
                        gender=gender,
                        min_age=min_age,
                        max_age=max_age,
                        device_id=resolved,
                    )
                elif step_name == "follow":
                    tt.keyword_search_and_follow_session(
                        target_country=tc,
                        max_follows=params.get("max_follows", 30),
                        gender=gender,
                        min_age=min_age,
                        max_age=max_age,
                        device_id=resolved,
                        progress_callback=None,
                    )
                elif step_name in ("check_inbox", "inbox"):
                    tt.check_inbox_session(
                        auto_reply=True,
                        max_conversations=params.get("max_conversations", 20),
                        device_id=resolved,
                        progress_callback=None,
                    )
                campaign_result["steps_completed"].append(step_name)
                log.info("[Campaign] 步骤 %s 完成", step_name)
                # 步骤间休息 30-60s
                time.sleep(random.uniform(30, 60))

            except Exception as step_err:
                log.error("[Campaign] 步骤 %s 失败: %s", step_name, step_err)
                campaign_result["steps_failed"].append({"step": step_name, "error": str(step_err)})
                # 步骤失败后继续执行下一步（不中断整个剧本）
                time.sleep(10)

        if cb:
            cb(100, f"剧本完成: {len(campaign_result['steps_completed'])}/{step_count} 步骤")
        try:
            from src.host.event_stream import push_event as _push_campaign
            _push_campaign("tiktok.campaign_done", {
                "device_id": resolved,
                "steps_completed": campaign_result["steps_completed"],
                "steps_failed": [s["step"] for s in campaign_result.get("steps_failed", [])],
                "target_country": tc,
            }, resolved)
        except Exception:
            pass
        return True, "", campaign_result

    elif task_type == "tiktok_check_comment_replies":
        # P2-2: 检查评论回复通知，对回复者发DM
        _tl_cmtr = params.get("target_languages", [])
        if isinstance(_tl_cmtr, str):
            _tl_cmtr = [x.strip() for x in _tl_cmtr.split(",") if x.strip()]
        result = tt.check_comment_replies_session(
            max_replies=params.get("max_replies", 20),
            target_languages=_tl_cmtr or None,
            device_id=resolved,
            progress_callback=_make_progress_cb(),
        )
        try:
            from src.host.event_stream import push_event as _push_cmtr
            _push_cmtr("tiktok.comment_replies_checked", {
                "device_id": resolved,
                "checked": result.get("checked", 0),
                "dmed": result.get("dmed", 0),
            }, resolved)
        except Exception:
            pass
        return True, "", result

    # ★ P0 新增: 通讯录好友发现
    elif task_type == "tiktok_contact_discovery":
        from src.app_automation.contacts_manager import tiktok_find_contact_friends
        result = tiktok_find_contact_friends(
            tiktok_automation=tt,
            device_id=resolved,
            max_friends=params.get("max_friends", 15),
            auto_follow=params.get("auto_follow", True),
            auto_message=params.get("auto_message", False),
            target_language=params.get("target_language", "italian"),
        )
        # ★ 自动同步到 LeadsStore（闭环核心）
        if result.get("discovered_names"):
            try:
                from src.workflow.platform_actions_bridge import _sync_contact_discoveries_to_leads
                synced = _sync_contact_discoveries_to_leads(
                    device_id=resolved,
                    discovered_names=result["discovered_names"],
                )
                result["synced_to_leads"] = synced
            except Exception as e:
                logger.warning("[通讯录发现] LeadsStore 同步失败: %s", e)
        logger.info("[通讯录发现] 完成: found=%d followed=%d synced=%s",
                    result.get("found", 0), result.get("followed", 0),
                    result.get("synced_to_leads", 0))
        return True, "", result

    # ★ P0 新增: Drip Campaign 跟进任务
    elif task_type == "tiktok_drip_followup":
        username = params.get("username", "")
        lead_id = params.get("lead_id")
        context = params.get("context", "followup_day1")

        if not username:
            return False, "params.username 必填", None

        # 检查用户是否已回复（如已回复则跳过，不打扰）
        if params.get("cancel_if_replied") and lead_id:
            try:
                from src.leads.store import get_leads_store
                store = get_leads_store()
                interactions = store.get_interactions(lead_id, platform="tiktok", limit=50)
                inbound = [ix for ix in interactions if ix.get("direction") == "inbound"]
                if inbound:
                    logger.info("[Drip] 用户 %s 已回复，跳过 %s 跟进任务", username, context)
                    return True, "already_replied_skip", {"skipped": True, "reason": "user_replied"}
            except Exception:
                pass

        # 生成对应阶段的消息
        try:
            from src.ai.tiktok_chat_ai import TikTokChatAI
            ai = TikTokChatAI()
            if context == "referral":
                # Day3: 发引流消息
                message = ai.generate_natural_message(context="referral", language="italian")
            elif context == "last_chance":
                # Day7: 最后机会
                message = ai.generate_natural_message(context="followup_gentle", language="italian")
            else:
                # Day1: 互动型内容
                message = ai.generate_natural_message(context="engage_question", language="italian")
        except Exception:
            message = "Ciao! Come stai? 😊"

        ok = tt.send_dm(username=username, message=message)
        if ok and lead_id:
            try:
                from src.leads.store import get_leads_store
                get_leads_store().add_interaction(
                    lead_id, "tiktok", "drip_followup",
                    direction="outbound",
                    device_id=resolved,
                    metadata={"context": context, "message": message[:100]},
                )
            except Exception:
                pass

        logger.info("[Drip] %s 发送给 %s: %s", context, username, "✓" if ok else "✗")
        return ok, ("" if ok else "Drip 消息发送失败"), {"context": context, "sent": ok}

    # ★ P3-2: 高分 Lead 优先触达任务
    elif task_type == "tiktok_priority_outreach":
        max_leads = params.get("max_leads", 10)
        min_score = float(params.get("min_score", 50.0))
        try:
            from src.host.lead_priority_outreach import run_priority_outreach
            stats = run_priority_outreach(
                device_id=resolved,
                max_leads=max_leads,
                min_score=min_score,
            )
            logger.info("[PriorityOutreach] 完成: %s", stats)
            return True, "", stats
        except Exception as e:
            logger.error("[PriorityOutreach] 执行失败: %s", e)
            return False, str(e), None

    # AI 线索重评：与 job_scheduler 中 tiktok_ai_rescore 一致；tiktok_ai_restore 为任务中心/旧版别名
    elif task_type in ("tiktok_ai_rescore", "tiktok_ai_restore"):
        try:
            import json as _json
            import os
            import urllib.error
            import urllib.request

            limit = int(params.get("limit", 30))
            platform = params.get("platform", "tiktok")
            payload = _json.dumps({"limit": limit, "platform": platform}).encode("utf-8")
            from src.openclaw_env import local_api_base

            req = urllib.request.Request(
                f"{local_api_base()}/tiktok/leads/ai-rescore",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            key = (os.environ.get("OPENCLAW_API_KEY") or "").strip()
            if key:
                req.add_header("X-API-Key", key)
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = _json.loads(resp.read().decode())
            return True, "", data
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")[:300]
            logger.error("[tiktok_ai_rescore] HTTP %s: %s", e.code, err_body)
            return False, f"ai-rescore HTTP {e.code}: {err_body}", None
        except Exception as e:
            logger.error("[tiktok_ai_rescore/restore] %s", e)
            return False, str(e)[:200], None

    return False, f"不支持的 TikTok 任务类型: {task_type}", None


def _execute_tiktok_auto(tt, manager, resolved, params):
    """Full auto mode: VPN检查 → warmup → test follow → smart follow → chat + 引流。"""
    from src.app_automation.target_filter import TargetProfile
    from src.host.device_state import get_device_state_store
    import random

    def _auto_progress(pct: int, msg: str):
        tid = _get_current_task_id()
        if tid:
            try:
                update_task_progress(tid, pct, msg)
            except Exception:
                pass

    target_country = params.get("target_country", "italy")

    # 自动检测阶段（而不是从参数读）
    ds = get_device_state_store("tiktok")
    state_did = resolved  # 用真实设备 ID
    ds.init_device(state_did)
    phase = ds.determine_phase(state_did) if not params.get("phase") else params["phase"]
    can_follow = ds.can_follow(state_did)

    result = {"phase": phase, "warmup": None, "follow_test": None,
              "follows": None, "chat": None, "vpn": None}

    # Step 0: VPN 检查 + 静默重连 — 失败则中止任务
    vpn_ok, vpn_msg = _ensure_vpn(resolved)
    result["vpn"] = "connected" if vpn_ok else "failed"
    if not vpn_ok:
        raise RuntimeError(vpn_msg)  # 让上层 _execute_with_retry 捕获并标记任务失败

    _auto_progress(5, "VPN 已连接，开始养号")

    if phase == "cold_start":
        duration = random.randint(20, 40)
    elif phase == "interest_building":
        duration = random.randint(30, 50)
    else:
        duration = random.randint(30, 45)

    warmup_stats = tt.warmup_session(
        duration_minutes=duration,
        target_country=target_country,
        phase=phase,
    )
    result["warmup"] = warmup_stats
    _auto_progress(40, f"养号完成 ({duration}min)")

    if phase == "interest_building" and not can_follow:
        if random.random() < 0.3:
            d = tt._u2(resolved)  # ADB fallback
            if tt.launch(resolved):
                ok = tt._random_test_follow(d, resolved)
                result["follow_test"] = {"can_follow": ok}
                if ok:
                    can_follow = True
                    phase = "active"
                    result["phase"] = "active"
        _auto_progress(50, "关注测试完成")

    if phase == "active" and can_follow:
        target = TargetProfile(
            country=params.get("country") or params.get("target_country", "italy"),
            language=params.get("language", "italian"),
            gender=params.get("gender", ""),      # "" = 不限性别
            min_age=params.get("min_age", 0),     # 0 = 不限年龄
        )
        max_follows = params.get("max_follows", 15)
        tracker = _get_leads_tracker()
        follow_result = tt.smart_follow(
            target=target, max_follows=max_follows,
            global_tracker=tracker,
            progress_callback=_make_progress_cb(),
        )
        result["follows"] = follow_result
        _auto_progress(75, f"关注完成 ({follow_result.get('followed', 0)}人)")

        if random.random() < 0.6:
            tt.warmup_session(
                duration_minutes=random.randint(5, 15),
                target_country=target_country,
                phase="active",
            )
        _auto_progress(85, "二次养号完成")

        try:
            chat_path = config_file("chat_messages.yaml")
            messages = []
            if chat_path.exists():
                with open(chat_path, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f)
                # 分阶段消息: 回关时用纯问候，引流消息交给 follow_up 定时发
                msg_type = params.get("message_type", "greeting")
                if msg_type == "referral":
                    messages = data.get("messages", [])  # 含引流链接
                else:
                    messages = data.get("greeting_messages", data.get("messages", []))
            if messages:
                chat_result = tt.check_and_chat_followbacks(
                    messages, params.get("max_chats", 10),
                    global_tracker=tracker,
                )
                result["chat"] = chat_result
                _auto_progress(98, f"聊天完成 ({chat_result.get('messaged', 0)}条)")
        except Exception as e:
            logger.warning("TikTok auto chat failed: %s", e)

    return result


def _execute_batch_send(manager, resolved, params, config_path):
    """
    批量发送: 同一条消息发给多个用户。支持断点续传。
    params: {targets: ["@user1", "@user2"], message: "...", _checkpoint: {...}}
    """
    targets = params.get("targets") or []
    message = params.get("message", "")
    if not targets:
        return False, "params.targets 列表为空", None
    if not message:
        return False, "params.message 不能为空", None

    checkpoint = params.get("_checkpoint") or {}
    start_idx = checkpoint.get("completed_idx", 0)
    prior_results = checkpoint.get("results", [])

    if start_idx > 0:
        logger.info("批量发送从断点恢复: 跳过前 %d/%d 个目标",
                     start_idx, len(targets))

    results = list(prior_results)
    success_count = sum(1 for r in results if r.get("success"))

    task_id = _get_current_task_id()

    for i in range(start_idx, len(targets)):
        target = targets[i]
        tg = _fresh_telegram(manager, resolved)
        ok = False
        err = ""
        try:
            if tg.search_and_open_user(target, resolved):
                ok = tg.send_text_message(message, resolved)
                if not ok:
                    err = "发送失败"
            else:
                err = f"搜索用户 {target} 失败"
        except Exception as e:
            err = str(e)

        results.append({"target": target, "success": ok, "error": err})
        if ok:
            success_count += 1

        if task_id:
            _save_task_checkpoint(task_id, {
                "completed_idx": i + 1,
                "results": results,
                "total": len(targets),
            })
            update_task_progress(
                task_id,
                int((i + 1) / len(targets) * 100),
                f"已发送 {i + 1}/{len(targets)}",
            )

        time.sleep(1)

    all_ok = success_count == len(targets)
    summary = f"{success_count}/{len(targets)} 发送成功"
    if start_idx > 0:
        summary += f" (从第{start_idx + 1}个恢复)"
    return all_ok, ("" if all_ok else summary), {"batch_results": results, "summary": summary}


def _get_current_task_id() -> str:
    """Get the task_id of the currently executing task."""
    try:
        from src.utils.log_config import _task_context
        return getattr(_task_context, "task_id", "")
    except Exception:
        return ""


def _save_task_checkpoint(task_id: str, data: dict):
    """Save checkpoint for the current task."""
    try:
        from .task_store import save_checkpoint
        save_checkpoint(task_id, data)
    except Exception:
        pass


def run_task(task_id: str, config_path: Optional[str] = None) -> None:
    config_path = config_path or DEFAULT_DEVICES_YAML
    task = get_task(task_id)
    if not task or task.get("status") != "pending":
        return

    device_id = task.get("device_id")
    params = task.get("params") or {}
    task_type = task.get("type", "")
    resolved = None

    try:
        manager = get_device_manager(config_path)
        manager.discover_devices()
        resolved = _get_device_id(manager, device_id, config_path,
                                  task_type=task_type)
        if not resolved:
            set_task_result(task_id, False, error="无可用设备（请确认 ADB 已连接手机）")
            return

        # 统一门禁：tier × gate_mode 矩阵 + 预检/GEO（task_dispatch_gate）
        try:
            from src.host.task_dispatch_gate import evaluate_task_gate_detailed
            gate_ev = evaluate_task_gate_detailed(task, resolved, config_path)
            if not gate_ev.allowed:
                set_task_result(
                    task_id,
                    False,
                    error=gate_ev.reason,
                    extra={
                        "device_id": resolved,
                        "gate_evaluation": gate_ev.to_dict(),
                    },
                )
                metrics.inc_task(False)
                metrics.record_task_result(resolved, False)
                try:
                    from .event_stream import push_event
                    push_event(
                        "task.gate_blocked",
                        task_id=task_id,
                        device_id=resolved,
                        task_type=task_type,
                        reason=gate_ev.reason,
                        tier=gate_ev.tier,
                        gate_mode=gate_ev.gate_mode,
                        hint_code=getattr(gate_ev, "hint_code", "") or "",
                    )
                except Exception:
                    pass
                return
        except Exception as _ge:
            logger.exception("[gate] 评估异常，为安全起见中止任务: %s", _ge)
            set_task_result(task_id, False, error=f"[gate] 评估异常: {_ge}")
            metrics.inc_task(False)
            return

        set_task_running(task_id)
        set_task_context(task_id=task_id, device_id=task.get("device_id", ""))

        timeout = _TASK_TYPE_TIMEOUTS.get(task_type,
                    TIKTOK_TIMEOUT_SEC if task_type.startswith("tiktok_") else TASK_TIMEOUT_SEC)
        try:
            success, error_detail, extra_data = run_with_timeout(
                _execute_with_retry, timeout,
                manager, resolved, task_type, params, config_path, task_id
            )
        except TaskTimeout:
            set_task_result(task_id, False, error=f"任务执行超时 ({timeout}s)")
            metrics.inc_task(False, timeout=True)
            return

        screenshot_path = None
        try:
            screenshots_dir = logs_dir() / "screenshots"
            screenshots_dir.mkdir(parents=True, exist_ok=True)
            screenshot_path = str(screenshots_dir / f"task_{task_id}.png")
            manager.capture_screen(resolved, screenshot_path)
        except Exception as e:
            logger.warning("task screenshot failed: %s", e)

        extra = {"device_id": resolved}
        if extra_data:
            extra.update(extra_data)
        set_task_result(task_id, success, error=error_detail,
                        screenshot_path=screenshot_path, extra=extra)
        metrics.inc_task(success)
        metrics.record_task_result(resolved, success)
        try:
            from .smart_scheduler import get_smart_scheduler
            get_smart_scheduler().record_task_result(resolved, task_type, success)
        except Exception:
            pass
    except Exception as e:
        logger.exception("task %s run error", task_id)
        set_task_result(task_id, False, error=str(e))
        metrics.inc_task(False)
        if device_id:
            metrics.record_task_result(device_id, False)
            try:
                from .smart_scheduler import get_smart_scheduler
                get_smart_scheduler().record_task_result(device_id, task_type, False)
            except Exception:
                pass
    finally:
        if task_type.startswith("tiktok_"):
            try:
                from .account_scheduler import get_account_scheduler
                acct = params.get("_active_account") or params.get("account", "")
                if acct and resolved is not None:
                    get_account_scheduler().end_session(resolved, acct)
            except Exception:
                pass
        clear_task_context()
