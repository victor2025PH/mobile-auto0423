# -*- coding: utf-8 -*-
"""
任务执行前置检查流水线 (Pre-flight Pipeline)。

检查顺序：网络 → VPN → 账号状态
每步失败立即返回，不继续后续检查。
结果缓存 90 秒，避免频繁重复检查同一设备。
"""

import logging
import subprocess
import time
import threading

from src.utils.subprocess_text import run as _sp_run_text
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Tuple

PreflightMode = Literal["full", "network_only", "none"]

logger = logging.getLogger(__name__)

_cache: Dict[str, Tuple[dict, float]] = {}
_cache_lock = threading.Lock()
# 2026-04-27 P3: TTL 90 → 180s. 圈层拓客真机重试观察单 task 序列内 v3 (12:14:14)
# preflight fail 后 v4 (12:15:35, +81s 命中 cache) PASS. 90s TTL 太短导致同一
# task 链反复真探测, 抖动概率高. 180s 让一个 task 序列复用 cache 合理 (单任务
# 1-3min), 真断网时仍 180s 后重检, 兜底信任 _check_vpn 的反向校验.
_CACHE_TTL = 180


@dataclass
class PreflightResult:
    device_id: str
    passed: bool
    blocked_step: str = ""
    blocked_reason: str = ""
    network_ok: bool = False
    vpn_ok: bool = False
    account_ok: bool = False
    checked_at: float = field(default_factory=time.time)
    preflight_mode: str = "full"
    vpn_skipped_geo_match: bool = False
    vpn_skip_note: str = ""
    geo_snapshot: Optional[Dict[str, Any]] = None

    def to_dict(self) -> dict:
        return {
            "device_id": self.device_id,
            "passed": self.passed,
            "blocked_step": self.blocked_step,
            "blocked_reason": self.blocked_reason,
            "network_ok": self.network_ok,
            "vpn_ok": self.vpn_ok,
            "account_ok": self.account_ok,
            "checked_at": self.checked_at,
            "preflight_mode": self.preflight_mode,
            "vpn_skipped_geo_match": self.vpn_skipped_geo_match,
            "vpn_skip_note": self.vpn_skip_note,
            "geo_snapshot": self.geo_snapshot,
        }


def run_preflight(
    device_id: str,
    skip_cache: bool = False,
    mode: PreflightMode = "full",
    task_target_country: Optional[str] = None,
) -> PreflightResult:
    """运行预检流水线，结果缓存 90 秒。

    mode:
      full — 网络 → VPN（可因出口国与任务国一致而跳过）→ 账号
      network_only — 仅外网可达；不要求 VPN（用于 balanced+L1）
      none — 跳过检查，直接通过（用于 L0 或 dev 快速路径）

    task_target_country:
      仅在 mode=full 时生效：若出口 IP 国家与之一致，则不再强制本机 VPN 连接。
    """
    tgt = (task_target_country or "").strip().lower() or None
    cache_key = f"{device_id}|{mode}|{tgt or ''}"
    if not skip_cache:
        with _cache_lock:
            cached = _cache.get(cache_key)
            if cached and (time.time() - cached[1]) < _CACHE_TTL:
                data = cached[0].copy()
                return _preflight_from_cache_dict(data)

    if mode == "none":
        result = _do_preflight_none(device_id)
    elif mode == "network_only":
        result = _do_preflight_network_only(device_id)
    else:
        result = _do_preflight(device_id, task_target_country=tgt)

    with _cache_lock:
        _cache[cache_key] = (result.to_dict(), time.time())
    return result


def _preflight_from_cache_dict(data: dict) -> PreflightResult:
    """兼容旧缓存条目（缺新字段时补默认）。"""
    base = dict(data)
    base.setdefault("vpn_skipped_geo_match", False)
    base.setdefault("vpn_skip_note", "")
    base.setdefault("geo_snapshot", None)
    return PreflightResult(
        device_id=base["device_id"],
        passed=base["passed"],
        blocked_step=base.get("blocked_step", ""),
        blocked_reason=base.get("blocked_reason", ""),
        network_ok=base.get("network_ok", False),
        vpn_ok=base.get("vpn_ok", False),
        account_ok=base.get("account_ok", False),
        checked_at=base.get("checked_at", time.time()),
        preflight_mode=base.get("preflight_mode", "full"),
        vpn_skipped_geo_match=base.get("vpn_skipped_geo_match", False),
        vpn_skip_note=base.get("vpn_skip_note", ""),
        geo_snapshot=base.get("geo_snapshot"),
    )


def _geo_result_to_snapshot(geo: Any) -> Dict[str, Any]:
    """序列化 GeoCheckResult，供门禁复用，避免二次 ip-api 查询。"""
    return {
        "expected_country": geo.expected_country,
        "detected_country": geo.detected_country,
        "detected_country_code": geo.detected_country_code,
        "public_ip": geo.public_ip,
        "matches": geo.matches,
        "error": (geo.error or "").strip(),
    }


def _run_preflight_geo_once(device_id: str, task_country: str) -> Any:
    """full 预检内单次 GEO（与门禁期望国一致时可复用）。"""
    from src.behavior.geo_check import check_device_geo
    from src.device_control.device_manager import get_device_manager
    from src.host.device_registry import DEFAULT_DEVICES_YAML

    dm = get_device_manager(DEFAULT_DEVICES_YAML)
    return check_device_geo(device_id, task_country, dm)


def _do_preflight(device_id: str, task_target_country: Optional[str] = None) -> PreflightResult:
    res = PreflightResult(device_id=device_id, passed=False, preflight_mode="full")

    # Step 1: 网络
    net_ok, net_msg = _check_network(device_id)
    res.network_ok = net_ok
    if not net_ok:
        res.blocked_step = "network"
        res.blocked_reason = net_msg
        logger.warning("[preflight] %s 网络未通: %s", device_id[:8], net_msg)
        return res

    # Step 2: VPN（若本地出口已在目标国，则不必强制 v2ray）
    skip_vpn = False
    if task_target_country:
        try:
            geo = _run_preflight_geo_once(device_id, task_target_country)
            res.geo_snapshot = _geo_result_to_snapshot(geo)
            if geo.error:
                logger.debug(
                    "[preflight] 出口国预检未判定: %s — %s",
                    device_id[:8],
                    geo.error,
                )
            elif geo.matches:
                note = (
                    f"出口≈{geo.detected_country} IP={geo.public_ip} "
                    f"与任务国 {task_target_country} 一致"
                )
                logger.info("[preflight] %s %s，跳过 VPN 硬门槛", device_id[:8], note)
                skip_vpn = True
                res.vpn_ok = True
                res.vpn_skipped_geo_match = True
                res.vpn_skip_note = note
        except Exception as e:
            logger.debug("[preflight] 出口国比对异常: %s", e)

    if not skip_vpn:
        vpn_ok, vpn_msg = _check_vpn(device_id)
        res.vpn_ok = vpn_ok
        if not vpn_ok:
            res.blocked_step = "vpn"
            res.blocked_reason = vpn_msg
            logger.warning("[preflight] %s VPN未连接: %s", device_id[:8], vpn_msg)
            return res

    # Step 3: 账号（进程存活检查，fail-open）
    acc_ok, acc_msg = _check_account(device_id)
    res.account_ok = acc_ok
    if not acc_ok:
        res.blocked_step = "account"
        res.blocked_reason = acc_msg
        logger.warning("[preflight] %s 账号异常: %s", device_id[:8], acc_msg)
        return res

    res.passed = True
    logger.info("[preflight] %s 全部通过 ✓", device_id[:8])
    return res


def _do_preflight_network_only(device_id: str) -> PreflightResult:
    """仅外网：不要求 VPN（出口可为路由器/WiFi 等）。"""
    res = PreflightResult(device_id=device_id, passed=False, preflight_mode="network_only")
    net_ok, net_msg = _check_network(device_id)
    res.network_ok = net_ok
    if not net_ok:
        res.blocked_step = "network"
        res.blocked_reason = net_msg
        logger.warning("[preflight] %s network_only 网络未通: %s", device_id[:8], net_msg)
        return res
    res.vpn_ok = False
    res.account_ok = True
    res.passed = True
    logger.info("[preflight] %s network_only 通过（未要求 VPN）✓", device_id[:8])
    return res


def _do_preflight_none(device_id: str) -> PreflightResult:
    """不做 ADB 探测（仅用于策略显式放行；仍带 device_id 便于日志）。"""
    return PreflightResult(
        device_id=device_id,
        passed=True,
        blocked_step="",
        blocked_reason="preflight_mode_none",
        network_ok=True,
        vpn_ok=True,
        account_ok=True,
        preflight_mode="none",
    )


def check_device_network_connectivity(device_id: str) -> Tuple[bool, str]:
    """与预检一致的外网探测，供 executor 等模块复用。"""
    return _check_network(device_id)


def _check_network(device_id: str) -> Tuple[bool, str]:
    """外网探测：优先 HTTP 204，其次 ICMP，并给出可操作的失败原因。"""
    url = "http://connectivitycheck.gstatic.com/generate_204"
    curl_variants = (
        ("curl", f"curl -s --connect-timeout 5 -o /dev/null -w '%{{http_code}}' {url}"),
        ("toybox", f"toybox curl -s --connect-timeout 5 -o /dev/null -w '%{{http_code}}' {url}"),
        ("sys", f"/system/bin/curl -s --connect-timeout 5 -o /dev/null -w '%{{http_code}}' {url}"),
    )
    try:
        code = ""
        last_label = "curl"
        for label, curl_cmd in curl_variants:
            last_label = label
            r = _sp_run_text(
                ["adb", "-s", device_id, "shell", curl_cmd],
                capture_output=True,
                timeout=12,
            )
            code = r.stdout.strip().strip("'").strip()
            if code == "204":
                return True, f"connected({label})"
        # 部分 ROM 对 generate_204 返回异常码，尝试 HEAD（优先标准 curl）
        for label, head_cmd in (
            ("curl", f"curl -s --connect-timeout 5 -I {url} 2>/dev/null | head -n1"),
            ("toybox", f"toybox curl -s --connect-timeout 5 -I {url} 2>/dev/null | head -n1"),
        ):
            r_head = _sp_run_text(
                ["adb", "-s", device_id, "shell", head_cmd],
                capture_output=True,
                timeout=12,
            )
            h1 = (r_head.stdout or "").strip()
            if "204" in h1 or "No Content" in h1:
                return True, f"connected(http_head,{label})"

        for host in ("8.8.8.8", "1.1.1.1"):
            r2 = _sp_run_text(
                ["adb", "-s", device_id, "shell", f"ping -c 1 -W 3 {host}"],
                capture_output=True,
                timeout=8,
            )
            if r2.returncode == 0:
                return True, f"connected(ping {host})"

        # 2026-04-27 P3 新增 — TCP 443 nc 探测 (跨 ICMP 不转 / curl 不存在的 ROM):
        # 圈层拓客真机重试 IJ8HZLOR 实测 chrome 加载 fb.com 真通, 但 curl/ping
        # 全 fail (设备无 curl + VPN 不转 ICMP). nc 是 TCP 层探测, 反映业务真实路径.
        # 多 host fallback: gstatic.com 走 DNS, 8.8.8.8 直接 IP (绕 DNS 故障).
        # 多 nc binary: toybox nc (Android 6+ 标配) 和 /system/bin/nc (旧版备选).
        #
        # 命令语法 (真机 IJ8HZLOR 实测正确):
        #   echo '' | toybox nc -w 5 -W 1 HOST 443
        # 说明:
        #   echo '' 喂空 stdin 让 nc 不等用户输入立即建连;
        #   -w 5  connection timeout 5s (建连超时);
        #   -W 1  idle timeout 1s (连上后 1s 没数据就退出, returncode=0 = 连过).
        #   注: Android toybox nc 不支持 -z (port scan mode), 只能用 stdin 重定向.
        for nc_label, nc_bin in (("toybox-nc", "toybox nc"),
                                  ("sys-nc", "/system/bin/nc")):
            for host in ("connectivitycheck.gstatic.com", "8.8.8.8"):
                try:
                    r3 = _sp_run_text(
                        ["adb", "-s", device_id, "shell",
                         f"echo '' | {nc_bin} -w 5 -W 1 {host} 443 > /dev/null 2>&1 && echo TCP_OK"],
                        capture_output=True,
                        timeout=10,
                    )
                    if "TCP_OK" in (r3.stdout or ""):
                        return True, f"connected(tcp443 via {nc_label} -> {host})"
                except Exception:
                    continue

        parts = []
        if not code:
            parts.append(
                "HTTP 状态码为空（常见于无 curl/toybox、DNS 异常或被策略拦截）"
            )
        else:
            parts.append(f"HTTP 状态码={code!r}（最后尝试={last_label}）")
        parts.append("ICMP 探测也未通过")
        parts.append("TCP 443 nc 探测也未通过")
        return False, (
            "无法访问外网（"
            + "；".join(parts)
            + "）。请检查 SIM 卡或 Wi‑Fi、关闭飞行模式，并在设备页运行「诊断」。"
        )
    except subprocess.TimeoutExpired:
        return False, "网络检查超时，请检查设备连接或 USB 稳定性"
    except Exception as e:
        return False, f"网络检查异常: {e}"


def _check_vpn(device_id: str) -> Tuple[bool, str]:
    try:
        from src.behavior.vpn_manager import check_vpn_status, reconnect_vpn_silent
        s = check_vpn_status(device_id)
        if s.connected:
            return True, "connected"
        if reconnect_vpn_silent(device_id):
            return True, "reconnected"
        return False, "VPN未连接且自动重连失败，请手动检查V2RayNG"
    except Exception as e:
        logger.warning("[preflight] VPN检查异常: %s", e)
        return False, f"VPN检查异常: {e}"


def _check_account(device_id: str) -> Tuple[bool, str]:
    """TikTok进程存活检查（fail-open：进程未运行不是硬错误）。"""
    try:
        r = _sp_run_text(
            ["adb", "-s", device_id, "shell",
             "pidof com.zhiliaoapp.musically || pidof com.ss.android.ugc.trill"],
            capture_output=True,
            timeout=6,
        )
        pid = r.stdout.strip()
        if pid:
            return True, f"running(pid={pid})"
        return True, "not_running(will_start)"
    except Exception as e:
        logger.warning("[preflight] 账号检查异常: %s", e)
        return True, f"check_skipped({e})"


def invalidate_cache(device_id: str):
    """手动使某设备的预检缓存失效（含 network_only / none 分键）。"""
    with _cache_lock:
        to_del = [k for k in list(_cache.keys()) if k == device_id or str(k).startswith(f"{device_id}|")]
        for k in to_del:
            _cache.pop(k, None)


def get_all_readiness(device_ids: List[str]) -> List[dict]:
    """批量获取多台设备的就绪状态（走缓存）。"""
    results = []
    for did in device_ids:
        try:
            r = run_preflight(did)
            results.append(r.to_dict())
        except Exception as e:
            results.append({
                "device_id": did, "passed": False,
                "blocked_step": "error", "blocked_reason": str(e),
                "network_ok": False, "vpn_ok": False, "account_ok": False,
            })
    return results
