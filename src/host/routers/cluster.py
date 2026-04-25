# -*- coding: utf-8 -*-
"""Cluster / Multi-Host API — 集群管理路由。"""

import json as _json
import logging
import os
import urllib.error
import urllib.request
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket
from .auth import verify_api_key
from src.openclaw_env import DEFAULT_OPENCLAW_PORT
from src.host.device_registry import DEFAULT_DEVICES_YAML, PROJECT_ROOT, config_file, scripts_dir

logger = logging.getLogger(__name__)
router = APIRouter(prefix="", tags=["cluster"])

# ── 心跳驱动的自动编号（防抖2分钟，仅协调者响应此端点）────────────────────
import time as _time_c
_last_auto_assign_ts = 0.0
_AUTO_ASSIGN_DEBOUNCE = 120  # 最少间隔120秒


def _try_heartbeat_auto_assign(host_id: str) -> None:
    """心跳后检查是否有新设备需要自动编号。
    仅在段配置存在 + 有未编号设备 + 距上次至少2分钟时才触发。
    """
    global _last_auto_assign_ts
    now = _time_c.time()
    if now - _last_auto_assign_ts < _AUTO_ASSIGN_DEBOUNCE:
        return
    try:
        ranges_path = config_file("number_ranges.json")
        if not ranges_path.exists():
            return
        import json as _j2
        with open(ranges_path, encoding="utf-8") as _f:
            ranges = _j2.load(_f)
        if not ranges:
            return
        from .devices_core import _load_aliases_global, auto_assign_segments
        global_aliases = _load_aliases_global()
        unassigned = [did for did, info in global_aliases.items() if not info.get("number")]
        if not unassigned:
            return
        _last_auto_assign_ts = now  # 标记已触发，防止并发重入
        import threading

        def _bg():
            try:
                r = auto_assign_segments({})
                cnt = r.get("assigned", 0)
                if cnt > 0:
                    logger.info("[heartbeat-auto] 自动分配 %d 台新设备编号（触发来自 %s）", cnt, host_id)
            except Exception as e:
                logger.debug("[heartbeat-auto] 自动编号失败: %s", e)

        threading.Thread(target=_bg, daemon=True, name="heartbeat-auto-assign").start()
    except Exception:
        pass


def _http(req: urllib.request.Request, timeout: int = 15) -> bytes:
    """统一 HTTP 调用，确保连接关闭防止 CLOSE_WAIT 泄漏。"""
    req.add_header("Connection", "close")
    resp = urllib.request.urlopen(req, timeout=timeout)
    try:
        return resp.read()
    finally:
        resp.close()

_project_root = PROJECT_ROOT
_config_path = DEFAULT_DEVICES_YAML
_scripts_dir = scripts_dir()


# ── helpers ──────────────────────────────────────────────────────────────

import threading
import time

_worker_best_ip = {}  # host_id -> {"ip": "x.x.x.x", "checked_at": timestamp}
_worker_ip_lock = threading.Lock()


def _get_best_worker_url(device_id: str) -> dict:
    """找到设备所在 Worker 的最佳可达 IP。"""
    from ..multi_host import get_cluster_coordinator
    coord = get_cluster_coordinator()
    all_devs = coord.get_all_devices()

    target = None
    for d in all_devs:
        if d.get("device_id") == device_id:
            target = d
            break
    if not target:
        return None

    host_id = target.get("host_id", "")
    host_ip = target.get("host_ip", "")
    port = target.get("host_port", 8000)

    # 获取该 Worker 的所有 IP
    all_ips = []
    hosts = coord._hosts if hasattr(coord, '_hosts') else {}
    host_info = hosts.get(host_id)
    if host_info and hasattr(host_info, 'ips') and host_info.ips:
        all_ips = list(host_info.ips)

    # 确保 advertise_ip 也在列表中
    if host_ip and host_ip not in all_ips:
        all_ips.insert(0, host_ip)

    if not all_ips:
        return {"ip": host_ip, "port": port}

    # 检查缓存（60秒内有效）
    with _worker_ip_lock:
        cached = _worker_best_ip.get(host_id)
        if cached and time.time() - cached["checked_at"] < 60:
            return {"ip": cached["ip"], "port": port}

    # 逐个测试连通性（最多 3 秒超时）
    best_ip = all_ips[0]  # 默认第一个
    for ip in all_ips:
        try:
            url = f"http://{ip}:{port}/health"
            req = urllib.request.Request(url, method="GET")
            _http(req, timeout=3)
            best_ip = ip
            break  # 第一个能通的就用
        except Exception:
            continue

    with _worker_ip_lock:
        _worker_best_ip[host_id] = {"ip": best_ip, "checked_at": time.time()}

    return {"ip": best_ip, "port": port}


def _reachable_ip_for_host_info(host) -> str:
    """对集群 HostInfo 选取第一个能打开 /health 的 IP（与 _get_best_worker_url 共用 60s 缓存）。"""
    host_id = getattr(host, "host_id", "") or ""
    hip = (getattr(host, "host_ip", None) or "").strip()
    port = int(getattr(host, "port", None) or 8000)
    all_ips = []
    if hip:
        all_ips.append(hip)
    for ip in getattr(host, "ips", None) or []:
        if ip and ip not in all_ips:
            all_ips.append(ip)
    if not all_ips:
        return hip or "127.0.0.1"

    with _worker_ip_lock:
        cached = _worker_best_ip.get(host_id)
        if cached and time.time() - cached["checked_at"] < 60:
            return cached["ip"]

    best_ip = all_ips[0]
    for ip in all_ips:
        try:
            url = f"http://{ip}:{port}/health"
            req = urllib.request.Request(url, method="GET")
            _http(req, timeout=3)
            best_ip = ip
            break
        except Exception:
            continue

    with _worker_ip_lock:
        _worker_best_ip[host_id] = {"ip": best_ip, "checked_at": time.time()}
    return best_ip


def _get_worker_url_by_host_id(host_id: str):
    """在线 Worker 的可达 {ip, port}；host 离线或不存在时返回 None。"""
    from ..multi_host import get_cluster_coordinator

    coord = get_cluster_coordinator()
    with coord._lock:
        host = coord._hosts.get(host_id)
    if not host or not getattr(host, "online", False):
        return None
    ip = _reachable_ip_for_host_info(host)
    port = int(host.port or 8000)
    return {"ip": ip, "port": port}


def _verify_cluster_secret(body: dict, request_headers: dict = None):
    """Verify heartbeat authenticity via HMAC-SHA256 or legacy plain secret."""
    from ..multi_host import load_cluster_config
    import hmac, hashlib, time as _time
    cfg = load_cluster_config()
    secret = cfg.get("shared_secret", "")
    if not secret:
        return  # No secret configured, allow all

    # New: HMAC signature in body
    sig = body.get("_sig")
    ts = body.get("_ts")
    host_id = body.get("host_id", "")

    if sig and ts:
        # Validate timestamp (±60s to allow clock drift)
        try:
            ts_val = float(ts)
            if abs(_time.time() - ts_val) > 60:
                raise HTTPException(403, "Heartbeat timestamp expired")
        except (ValueError, TypeError):
            raise HTTPException(403, "Invalid timestamp")

        # Validate HMAC: sign(secret, host_id + ":" + ts)
        expected = hmac.new(
            secret.encode(),
            f"{host_id}:{ts}".encode(),
            hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(sig, expected):
            raise HTTPException(403, "Invalid cluster signature")
        return

    # Legacy: plain secret in body (backward compat)
    if body.get("secret") != secret:
        raise HTTPException(403, "Invalid cluster secret")


def _cluster_proxy_post(device_id: str, path: str, body: dict):
    """Generic POST proxy to the worker that owns the device."""
    target_host = _get_best_worker_url(device_id)
    if not target_host:
        raise HTTPException(404, "Device not found in cluster")
    url = f"http://{target_host['ip']}:{target_host['port']}{path}"
    try:
        payload = _json.dumps(body).encode()
        req = urllib.request.Request(url, data=payload,
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        return _json.loads(_http(req, timeout=15).decode())
    except Exception as e:
        raise HTTPException(502, f"Cluster proxy failed: {e}")


# ── endpoints ────────────────────────────────────────────────────────────


@router.post("/cluster/heartbeat")
def cluster_heartbeat(body: dict):
    """Receive heartbeat from a worker host."""
    _verify_cluster_secret(body)
    from ..multi_host import get_cluster_coordinator
    result = get_cluster_coordinator().receive_heartbeat(body)
    # 心跳驱动：检测新设备并自动编号（防抖2分钟）
    _try_heartbeat_auto_assign(body.get("host_id", ""))
    return result


@router.get("/cluster/overview")
def cluster_overview():
    """Get aggregated cluster status."""
    from ..multi_host import get_cluster_coordinator
    return get_cluster_coordinator().get_overview()


@router.get("/cluster/devices")
def cluster_all_devices():
    """Get unified device list across all hosts."""
    from ..multi_host import get_cluster_coordinator
    return {"devices": get_cluster_coordinator().get_all_devices()}


@router.get("/cluster/execution-policies")
def cluster_execution_policies():
    """聚合本机与各 Worker 的 /tasks/meta/execution-policy（经主控中转，避免浏览器直连 Worker CORS）。"""
    import os

    from src.host.task_policy import load_task_execution_policy

    pol = load_task_execution_policy()
    local = {
        "manual_execution_only": bool(pol.get("manual_execution_only")),
        "disable_db_scheduler": bool(pol.get("disable_db_scheduler", True)),
        "disable_json_scheduled_jobs": bool(pol.get("disable_json_scheduled_jobs", True)),
        "disable_reconnect_task_recovery": bool(pol.get("disable_reconnect_task_recovery", True)),
        "disable_auto_tiktok_check_inbox": bool(pol.get("disable_auto_tiktok_check_inbox")),
        "disable_executor_inbox_followup": bool(pol.get("disable_executor_inbox_followup")),
        "disable_strategy_optimizer": bool(pol.get("disable_strategy_optimizer")),
        "disable_event_driven_auto_tasks": bool(pol.get("disable_event_driven_auto_tasks")),
    }
    nodes = [
        {
            "role": "coordinator",
            "host_id": "__local__",
            "host_name": "本机（当前节点）",
            "host_ip": "",
            "online": True,
            "policy": local,
        }
    ]
    key = os.environ.get("OPENCLAW_API_KEY", "").strip()
    hdrs: dict = {}
    if key:
        hdrs["X-API-Key"] = key
    try:
        from ..multi_host import get_cluster_coordinator

        coord = get_cluster_coordinator()
        for host in coord._hosts.values():
            hid = host.host_id
            name = host.host_name or hid[:8]
            hip = (host.host_ip or "").strip()
            port = int(host.port or 8000)
            if not getattr(host, "online", False):
                nodes.append(
                    {
                        "role": "worker",
                        "host_id": hid,
                        "host_name": name,
                        "host_ip": hip,
                        "port": port,
                        "online": False,
                        "error": "offline",
                    }
                )
                continue
            probe_ip = _reachable_ip_for_host_info(host)
            url = f"http://{probe_ip}:{port}/tasks/meta/execution-policy"
            try:
                req = urllib.request.Request(url, headers=hdrs, method="GET")
                raw = _http(req, timeout=4)
                p2 = _json.loads(raw.decode())
                nodes.append(
                    {
                        "role": "worker",
                        "host_id": hid,
                        "host_name": name,
                        "host_ip": hip,
                        "reachable_ip": probe_ip,
                        "port": port,
                        "online": True,
                        "policy": {
                            "manual_execution_only": bool(p2.get("manual_execution_only")),
                            "disable_db_scheduler": bool(p2.get("disable_db_scheduler", True)),
                            "disable_json_scheduled_jobs": bool(p2.get("disable_json_scheduled_jobs", True)),
                            "disable_reconnect_task_recovery": bool(
                                p2.get("disable_reconnect_task_recovery", True)
                            ),
                            "disable_auto_tiktok_check_inbox": bool(
                                p2.get("disable_auto_tiktok_check_inbox")
                            ),
                            "disable_executor_inbox_followup": bool(
                                p2.get("disable_executor_inbox_followup")
                            ),
                            "disable_strategy_optimizer": bool(p2.get("disable_strategy_optimizer")),
                            "disable_event_driven_auto_tasks": bool(
                                p2.get("disable_event_driven_auto_tasks")
                            ),
                        },
                    }
                )
            except Exception as e:
                nodes.append(
                    {
                        "role": "worker",
                        "host_id": hid,
                        "host_name": name,
                        "host_ip": hip,
                        "reachable_ip": probe_ip,
                        "port": port,
                        "online": True,
                        "error": str(e)[:200],
                    }
                )
    except Exception:
        pass
    return {"nodes": nodes}


@router.post("/cluster/dispatch")
def cluster_dispatch_task(body: dict):
    """
    Dispatch a task to the best host in the cluster.
    Routes to the host with the target device, or the least loaded host.
    """
    from ..multi_host import get_cluster_coordinator
    coord = get_cluster_coordinator()
    task_type = body.get("type", "")
    device_id = body.get("device_id", "")

    target = coord.select_host_for_task(task_type, device_id)
    if not target:
        raise HTTPException(status_code=503,
                            detail="No online hosts available")

    target_url = target["url"]
    try:
        payload = _json.dumps(body).encode()
        req = urllib.request.Request(
            f"{target_url}/tasks",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        result = _json.loads(_http(req, timeout=15).decode())
        result["routed_to"] = target
        return result
    except Exception as e:
        raise HTTPException(status_code=502,
                            detail=f"Failed to dispatch to {target_url}: {e}")


@router.post("/cluster/join")
def cluster_join(body: dict):
    """Start sending heartbeats to a coordinator."""
    coordinator_url = body.get("coordinator_url", "")
    local_port = body.get("local_port", DEFAULT_OPENCLAW_PORT)
    if not coordinator_url:
        raise HTTPException(status_code=400,
                            detail="coordinator_url required")
    from ..multi_host import start_heartbeat_sender
    sender = start_heartbeat_sender(coordinator_url, local_port)
    return {"status": "joined", "coordinator": coordinator_url}


@router.delete("/cluster/hosts/{host_id}")
def cluster_remove_host(host_id: str):
    """Remove a host from the cluster."""
    from ..multi_host import get_cluster_coordinator
    get_cluster_coordinator().remove_host(host_id)
    return {"status": "removed", "host_id": host_id}


@router.get("/cluster/devices/{device_id}/screenshot",
            )
def cluster_device_screenshot(device_id: str, max_h: int = 360,
                              quality: int = 40):
    """Proxy screenshot request to the worker that owns the device."""
    from fastapi.responses import Response as _Resp
    from src.device_control.device_manager import get_device_manager
    from ..routers.devices_control import device_screenshot

    target_host = _get_best_worker_url(device_id)

    if not target_host:
        manager = get_device_manager(_config_path)
        info = manager.get_device_info(device_id)
        if info:
            return device_screenshot(device_id, max_h=max_h, quality=quality)
        raise HTTPException(404, "Device not found in cluster")

    url = (f"http://{target_host['ip']}:{target_host['port']}"
           f"/devices/{device_id}/screenshot?max_h={max_h}&quality={quality}")
    try:
        req = urllib.request.Request(url, method="GET")
        jpeg_data = _http(req, timeout=20)
        return _Resp(content=jpeg_data, media_type="image/jpeg",
                     headers={"Cache-Control": "no-cache",
                              "X-Proxied-From": target_host["ip"]})
    except Exception as e:
        raise HTTPException(502, f"Screenshot proxy failed: {e}")


@router.post("/cluster/devices/{device_id}/shell",
             )
def cluster_device_shell(device_id: str, body: dict):
    """Proxy shell command to the worker that owns the device."""
    target_host = _get_best_worker_url(device_id)
    if not target_host:
        raise HTTPException(404, "Device not found in cluster")
    url = f"http://{target_host['ip']}:{target_host['port']}/devices/{device_id}/shell"
    try:
        payload = _json.dumps(body).encode()
        req = urllib.request.Request(url, data=payload,
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        return _json.loads(_http(req, timeout=15).decode())
    except Exception as e:
        raise HTTPException(502, f"Shell proxy failed: {e}")


@router.post("/cluster/devices/{device_id}/input/tap",
             )
def cluster_device_tap(device_id: str, body: dict):
    return _cluster_proxy_post(device_id, f"/devices/{device_id}/input/tap", body)


@router.post("/cluster/devices/{device_id}/input/swipe",
             )
def cluster_device_swipe(device_id: str, body: dict):
    return _cluster_proxy_post(device_id, f"/devices/{device_id}/input/swipe", body)


@router.post("/cluster/devices/{device_id}/input/key",
             )
def cluster_device_key(device_id: str, body: dict):
    return _cluster_proxy_post(device_id, f"/devices/{device_id}/input/key", body)


@router.post("/cluster/devices/{device_id}/input/text",
             )
def cluster_device_text(device_id: str, body: dict):
    return _cluster_proxy_post(device_id, f"/devices/{device_id}/input/text", body)


@router.post("/cluster/refresh-devices")
def refresh_cluster_devices():
    """手动触发从所有 Worker 刷新设备列表。"""
    from ..multi_host import get_cluster_coordinator
    coord = get_cluster_coordinator()
    if not coord:
        raise HTTPException(status_code=400, detail="非集群模式或未初始化")
    count = coord.refresh_all_devices()
    return {"ok": True, "total_devices": count}


@router.get("/cluster/config")
def cluster_get_config():
    """Get current cluster configuration."""
    from ..multi_host import load_cluster_config
    return load_cluster_config()


@router.websocket("/cluster/devices/{device_id}/stream/ws")
async def cluster_stream_proxy(websocket: WebSocket, device_id: str):
    """WebSocket tunnel: proxy scrcpy stream from remote worker to browser.

    Coordinator receives browser WS, opens WS to worker, relays frames.
    """
    import asyncio
    import websockets

    target = _get_best_worker_url(device_id)

    if not target:
        await websocket.close(code=4004, reason="Device not in cluster")
        return

    await websocket.accept()
    worker_url = f"ws://{target['ip']}:{target['port']}/devices/{device_id}/stream/ws"

    try:
        async with websockets.connect(worker_url) as worker_ws:
            async def browser_to_worker():
                try:
                    while True:
                        data = await websocket.receive()
                        if "text" in data:
                            await worker_ws.send(data["text"])
                        elif "bytes" in data:
                            await worker_ws.send(data["bytes"])
                except Exception:
                    pass

            async def worker_to_browser():
                try:
                    async for msg in worker_ws:
                        if isinstance(msg, bytes):
                            await websocket.send_bytes(msg)
                        else:
                            await websocket.send_text(msg)
                except Exception:
                    pass

            await asyncio.gather(browser_to_worker(), worker_to_browser())
    except ImportError:
        await websocket.send_text(
            '{"error":"websockets library not installed, '
            'install with: pip install websockets"}')
        await websocket.close()
    except Exception as e:
        try:
            await websocket.send_text(f'{{"error":"{e}"}}')
            await websocket.close()
        except Exception:
            pass


@router.post("/cluster/batch")
def cluster_batch_task(body: dict):
    """Dispatch a task to all online devices across all hosts."""
    from ..multi_host import get_cluster_coordinator
    coord = get_cluster_coordinator()
    task_type = body.get("type") or body.get("task_type", "")
    target = body.get("target", "all")
    params = body.get("params", {})
    if not task_type:
        raise HTTPException(400, "type/task_type required")

    overview = coord.get_overview()
    hosts = [h for h in overview.get("hosts", []) if h.get("online")]
    results = []
    for h in hosts:
        if target.startswith("host:") and h.get("host_name") != target[5:]:
            continue
        url = f"http://{h['host_ip']}:{h['port']}/tasks/batch"
        try:
            payload = _json.dumps({
                "type": task_type, "params": params,
                "device_ids": [d.get("device_id") for d in
                               coord.get_all_devices()
                               if d.get("host_id") == h.get("host_id")
                               and d.get("status") == "connected"],
            }).encode()
            req = urllib.request.Request(
                url, data=payload,
                headers={"Content-Type": "application/json"},
                method="POST")
            r = _json.loads(_http(req, timeout=15).decode())
            results.append({
                "host": h.get("host_name", h["host_ip"]),
                "status": "ok", "result": r,
            })
        except Exception as e:
            results.append({
                "host": h.get("host_name", h["host_ip"]),
                "status": "error", "error": str(e),
            })
    return {"ok": True, "results": results, "hosts_targeted": len(hosts)}


async def run_cluster_batch_install_apk(request: Request):
    """
    主控将 APK 按设备所属 Worker 分组，转发到各机的 POST /batch/install-apk。
    Body 与 /batch/install-apk 相同：apk_data(base64)、filename、device_ids。
    仅处理集群心跳中的在线设备；本机直连设备请用 /batch/install-apk。
    """
    from collections import defaultdict

    from ..api import _audit
    from ..multi_host import get_cluster_coordinator

    body = await request.json()
    apk_b64 = body.get("apk_data", "")
    filename = body.get("filename", "app.apk") or "app.apk"
    device_ids_body = body.get("device_ids")
    if not apk_b64:
        raise HTTPException(400, "No APK data")
    if not device_ids_body or not isinstance(device_ids_body, list):
        raise HTTPException(400, "device_ids required (list)")

    wanted = [str(x).strip() for x in device_ids_body if x and str(x).strip()]
    if not wanted:
        raise HTTPException(400, "device_ids empty")

    coord = get_cluster_coordinator()
    all_cluster_devs = coord.get_all_devices()
    by_id = {}
    for d in all_cluster_devs:
        did = d.get("device_id")
        if did and did not in by_id:
            by_id[did] = d

    host_to_ids = defaultdict(list)
    results = {}

    for did in wanted:
        dev = by_id.get(did)
        if not dev:
            results[did] = {
                "success": False,
                "message": "设备不在集群心跳列表中（仅支持 Worker 上报的设备）",
            }
            continue
        st = dev.get("status", "")
        if st not in ("connected", "online"):
            results[did] = {"success": False, "message": f"设备离线 ({st})"}
            continue
        hid = dev.get("host_id", "") or ""
        if not hid:
            results[did] = {"success": False, "message": "缺少 host_id"}
            continue
        host_to_ids[hid].append(did)

    for hid in list(host_to_ids.keys()):
        host_to_ids[hid] = list(dict.fromkeys(host_to_ids[hid]))

    key = os.environ.get("OPENCLAW_API_KEY", "").strip()
    forward_base = {"apk_data": apk_b64, "filename": filename}
    host_summaries = []

    def _host_label(hid, id_list):
        d0 = by_id.get(id_list[0]) if id_list else None
        return (d0 or {}).get("host_name") or ""

    for host_id, ids in host_to_ids.items():
        hname = _host_label(host_id, ids)
        target = _get_worker_url_by_host_id(host_id)
        if not target:
            logger.warning("[cluster_apk] host_id=%s unreachable, devices=%s", (host_id or "")[:16], ids)
            for did in ids:
                results[did] = {"success": False, "message": "Worker 离线或不可达"}
            host_summaries.append(
                {
                    "host_id": host_id,
                    "host_name": hname,
                    "device_ids": ids,
                    "ok": 0,
                    "total": len(ids),
                    "error": "Worker 离线或不可达",
                }
            )
            continue
        url = f"http://{target['ip']}:{target['port']}/batch/install-apk"
        body_fwd = dict(forward_base)
        body_fwd["device_ids"] = ids
        try:
            logger.info(
                "[cluster_apk] forward host_id=%s worker_url=%s device_count=%d",
                (host_id or "")[:16],
                url[:120],
                len(ids),
            )
            payload = _json.dumps(body_fwd).encode()
            headers = {"Content-Type": "application/json"}
            if key:
                headers["X-API-Key"] = key
            req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
            raw = _http(req, timeout=600)
            r2 = _json.loads(raw.decode())
            r_res = r2.get("results") or {}
            ok_n = sum(1 for did in ids if (r_res.get(did) or {}).get("success"))
            logger.info(
                "[cluster_apk] host_id=%s worker_ok=%d/%d",
                (host_id or "")[:16],
                ok_n,
                len(ids),
            )
            for did in ids:
                row = r_res.get(did)
                if row:
                    results[did] = row
                else:
                    results[did] = {
                        "success": False,
                        "message": "Worker 未返回该设备结果",
                    }
            host_summaries.append(
                {
                    "host_id": host_id,
                    "host_name": hname,
                    "device_ids": ids,
                    "ok": ok_n,
                    "total": len(ids),
                    "worker_url": url[:160],
                }
            )
        except urllib.error.HTTPError as e:
            err_body = ""
            try:
                err_body = e.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                pass
            err_short = f"HTTP {e.code} {e.reason}"
            if err_body.strip().startswith("{"):
                try:
                    ej = _json.loads(err_body)
                    if isinstance(ej, dict) and ej.get("detail") is not None:
                        err_short += f": {str(ej['detail'])[:160]}"
                except Exception:
                    err_short += f" {err_body[:120]}"
            else:
                err_short += f" {err_body[:120]}"
            err_short = err_short[:280]
            logger.warning(
                "[cluster_apk] host_id=%s http_error=%s",
                (host_id or "")[:16],
                err_short,
            )
            for did in ids:
                results[did] = {
                    "success": False,
                    "message": f"转发失败: {err_short}",
                }
            host_summaries.append(
                {
                    "host_id": host_id,
                    "host_name": hname,
                    "device_ids": ids,
                    "ok": 0,
                    "total": len(ids),
                    "error": err_short,
                    "worker_url": url[:160],
                }
            )
        except Exception as e:
            err = str(e)[:220]
            logger.warning("[cluster_apk] host_id=%s forward_error=%s", (host_id or "")[:16], err)
            for did in ids:
                results[did] = {"success": False, "message": f"转发失败: {err}"}
            host_summaries.append(
                {
                    "host_id": host_id,
                    "host_name": hname,
                    "device_ids": ids,
                    "ok": 0,
                    "total": len(ids),
                    "error": err,
                    "worker_url": url[:160],
                }
            )

    total = len(results)
    success = sum(1 for v in results.values() if v.get("success"))
    try:
        _audit(
            "cluster_batch_install_apk",
            detail=f"filename={filename}, devices={total}, ok={success}",
        )
    except Exception:
        pass
    out = {"total": total, "success": success, "results": results}
    if host_summaries:
        out["hosts"] = host_summaries
    return out


@router.post("/cluster/batch/install-apk")
async def cluster_batch_install_apk(request: Request):
    return await run_cluster_batch_install_apk(request)


@router.post("/cluster/config")
def cluster_set_config(body: dict):
    """Update cluster configuration and optionally restart cluster."""
    from ..multi_host import load_cluster_config, save_cluster_config, auto_start_cluster
    cfg = load_cluster_config()
    cfg.update(body)
    save_cluster_config(cfg)
    auto_start_cluster()
    return {"status": "updated", "config": cfg}


@router.post("/cluster/execute-script")
def cluster_execute_script(body: dict):
    """Execute a script across all cluster hosts.
    body: {filename, type, target: 'all'|'host:name', variables?}"""
    from ..multi_host import get_cluster_coordinator
    coord = get_cluster_coordinator()
    filename = body.get("filename", "")
    script_type = body.get("type", "adb")
    target = body.get("target", "all")
    variables = body.get("variables", {})

    path = _scripts_dir / filename
    if not path.exists():
        raise HTTPException(404, "Script not found")
    content = path.read_text(encoding="utf-8")

    overview = coord.get_overview()
    hosts = [h for h in overview.get("hosts", []) if h.get("online")]
    results = {}

    for h in hosts:
        if target.startswith("host:") and h.get("host_name") != target[5:]:
            continue
        host_devices = [d for d in coord.get_all_devices()
                        if d.get("host_id") == h.get("host_id")
                        and d.get("status") == "connected"]
        url = f"http://{h['host_ip']}:{h['port']}/scripts/execute"
        try:
            # First upload script to the host
            upload_url = f"http://{h['host_ip']}:{h['port']}/scripts/upload"
            upload_payload = _json.dumps({"filename": filename, "content": content}).encode()
            req = urllib.request.Request(upload_url, data=upload_payload,
                                         headers={"Content-Type": "application/json"}, method="POST")
            _http(req, timeout=10)

            exec_payload = _json.dumps({
                "filename": filename, "type": script_type,
                "device_ids": [d["device_id"] for d in host_devices],
                "variables": variables,
            }).encode()
            req2 = urllib.request.Request(url, data=exec_payload,
                                          headers={"Content-Type": "application/json"}, method="POST")
            r = _json.loads(_http(req2, timeout=60).decode())
            results[h.get("host_name", h["host_ip"])] = {
                "status": "ok", "total": r.get("total", 0), "results": r.get("results", {}),
            }
        except Exception as e:
            results[h.get("host_name", h["host_ip"])] = {"status": "error", "error": str(e)}
    return {"ok": True, "hosts": results}


@router.get("/cluster/stats")
def cluster_stats():
    """Get aggregated cluster performance stats."""
    from ..multi_host import get_cluster_coordinator
    coord = get_cluster_coordinator()
    overview = coord.get_overview()
    all_devs = coord.get_all_devices()
    hosts = overview.get("hosts", [])
    online_hosts = [h for h in hosts if h.get("online")]
    online_devs = [d for d in all_devs if d.get("status") in ("connected", "online")]
    return {
        "total_hosts": len(hosts),
        "online_hosts": len(online_hosts),
        "total_devices": len(all_devs),
        "online_devices": len(online_devs),
        "hosts": [{
            "name": h.get("host_name", h.get("host_ip")),
            "ip": h.get("host_ip"),
            "port": h.get("port", 8000),
            "online": h.get("online", False),
            "devices": len([d for d in all_devs if d.get("host_id") == h.get("host_id")]),
            "online_devices": len([d for d in online_devs if d.get("host_id") == h.get("host_id")]),
            "last_heartbeat": h.get("last_heartbeat"),
        } for h in hosts],
    }


@router.post("/cluster/batch-reconnect")
def cluster_batch_reconnect():
    """Trigger reconnect for all offline devices across all cluster hosts."""
    from ..multi_host import get_cluster_coordinator
    coord = get_cluster_coordinator()
    overview = coord.get_overview()
    hosts = [h for h in overview.get("hosts", []) if h.get("online")]

    results = {}
    for h in hosts:
        url = f"http://{h['host_ip']}:{h.get('port', 8000)}/devices/batch-reconnect"
        try:
            req = urllib.request.Request(
                url, data=b'{}',
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            body = _json.loads(_http(req, timeout=15))
            results[h.get("host_name", h["host_ip"])] = {
                "status": "ok", "detail": body}
        except Exception as e:
            results[h.get("host_name", h["host_ip"])] = {
                "status": "error", "detail": str(e)}

    return {"hosts_contacted": len(hosts), "results": results}


# ── OTA 远程更新 ──────────────────────────────────────────────

_update_cache = {"path": None, "time": 0}


@router.get("/cluster/update-package")
def cluster_update_package():
    """主控端：打包最新代码供 Worker 下载。"""
    import zipfile
    import io
    import time as _time
    from fastapi.responses import Response as _Resp

    # 5分钟内缓存
    if _update_cache["path"] and _time.time() - _update_cache["time"] < 300:
        cached = Path(_update_cache["path"])
        if cached.exists():
            return _Resp(content=cached.read_bytes(),
                         media_type="application/zip",
                         headers={"Content-Disposition": "attachment; filename=openclaw-update.zip"})

    skip_ext = {'.pyc', '.pyo', '.db', '.log', '.png', '.jpg', '.sqlite', '.sqlite3', '.apk', '.ipa', '.zip'}
    skip_dirs = {'__pycache__', '.git', 'node_modules', 'logs', 'data', 'apk_repo'}

    buf = io.BytesIO()
    count = 0
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        # 项目代码
        proj = _project_root
        for dirpath, dirnames, filenames in os.walk(proj):
            dirnames[:] = [d for d in dirnames if d not in skip_dirs]
            for fn in filenames:
                if any(fn.endswith(e) for e in skip_ext):
                    continue
                full = os.path.join(dirpath, fn)
                arc = os.path.relpath(full, proj.parent)
                zf.write(full, arc)
                count += 1
        # 守护脚本
        deploy_dir = _project_root.parent / "deploy"
        if deploy_dir.exists():
            for fn in os.listdir(deploy_dir):
                fp = deploy_dir / fn
                if fp.is_file():
                    zf.write(str(fp), f"deploy/{fn}")
                    count += 1

    data = buf.getvalue()
    # 写缓存文件
    cache_path = _project_root / "data" / "openclaw-update.zip"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(data)
    _update_cache["path"] = str(cache_path)
    _update_cache["time"] = _time.time()

    logger.info("[OTA] 生成更新包: %d 文件, %.0f KB", count, len(data) / 1024)
    return _Resp(content=data, media_type="application/zip",
                 headers={"Content-Disposition": "attachment; filename=openclaw-update.zip"})


@router.get("/cluster/update-package/info")
def cluster_update_info():
    """主控端：返回更新包元信息。"""
    import time as _time
    return {
        "version": "1.0.0",
        "timestamp": _time.time(),
        "coordinator": _project_root.name,
    }


@router.post("/cluster/pull-update")
def cluster_pull_update(body: dict = None):
    """Worker 端：从主控拉取最新代码并自动更新（不含 config/）。"""
    import zipfile
    import io
    import time as _time
    import shutil

    body = body or {}
    coordinator_url = body.get("coordinator_url", "")
    if not coordinator_url:
        try:
            from ..multi_host import load_cluster_config
            cfg = load_cluster_config()
            coordinator_url = cfg.get("coordinator_url", "")
        except Exception:
            pass
    if not coordinator_url:
        raise HTTPException(400, "未配置 coordinator_url")

    url = coordinator_url.rstrip("/") + "/cluster/update-package"
    logger.info("[OTA] 从 %s 拉取更新...", url)

    try:
        req = urllib.request.Request(url, method="GET")
        data = _http(req, timeout=60)
    except Exception as e:
        raise HTTPException(502, f"下载更新包失败: {e}")

    # 解压到临时目录
    tmp_dir = _project_root / "data" / "_update_tmp"
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir(parents=True)

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        zf.extractall(tmp_dir)

    # 覆盖文件（不覆盖 config/ 和 data/）
    updated = 0
    src_base = tmp_dir / "mobile-auto-project"
    if not src_base.exists():
        # 如果压缩包根目录就是项目文件
        src_base = tmp_dir

    protect = {'config', 'data', 'logs'}
    for dirpath, dirnames, filenames in os.walk(src_base):
        dirnames[:] = [d for d in dirnames if d not in {'__pycache__', 'data', 'logs'}]
        rel = os.path.relpath(dirpath, src_base)
        top_dir = rel.split(os.sep)[0] if rel != '.' else ''
        if top_dir in protect:
            continue
        dst_dir = _project_root / rel
        dst_dir.mkdir(parents=True, exist_ok=True)
        for fn in filenames:
            src_file = os.path.join(dirpath, fn)
            dst_file = dst_dir / fn
            shutil.copy2(src_file, dst_file)
            # 更新 mtime 为当前时间，确保 Python 重新编译而不是用旧 .pyc
            os.utime(str(dst_file), None)
            updated += 1

    # 复制守护脚本
    deploy_src = tmp_dir / "deploy"
    if deploy_src.exists():
        deploy_dst = _project_root.parent / "deploy" if (_project_root.parent / "deploy").exists() else _project_root.parent
        for fn in os.listdir(deploy_src):
            src_f = deploy_src / fn
            dst_f = (deploy_dst if deploy_dst.name == 'deploy' else deploy_dst) / fn
            if src_f.is_file():
                shutil.copy2(str(src_f), str(dst_f))
                updated += 1

    # 清理临时目录
    shutil.rmtree(tmp_dir, ignore_errors=True)

    # 清理 __pycache__，防止 Python 用旧的 .pyc 覆盖新 .py（时间戳问题）
    pycache_cleared = 0
    for dirpath, dirnames, _ in os.walk(_project_root):
        dirnames[:] = [d for d in dirnames if d not in {'data', 'logs'}]
        if os.path.basename(dirpath) == '__pycache__':
            shutil.rmtree(dirpath, ignore_errors=True)
            pycache_cleared += 1
    if pycache_cleared:
        logger.info("[OTA] 已清理 %d 个 __pycache__ 目录", pycache_cleared)

    logger.info("[OTA] 更新完成: %d 文件已覆盖", updated)

    # 自动重启：优先直接用当前 Python 可执行文件重启，回退到哨兵文件
    auto_restart = body.get("auto_restart", True)
    restarting = False
    if auto_restart and updated > 0:
        sentinel = _project_root / ".restart-required"
        reason = f"OTA更新 {updated} 文件 @ {__import__('datetime').datetime.now().isoformat()}"
        sentinel.write_text(reason, encoding="utf-8")
        restarting = True
        logger.info("[OTA] 已写入哨兵文件，等待 service_wrapper 重启")
        # 直接重启：用当前 sys.executable 启动新进程，再退出自身
        try:
            import sys as _sys, subprocess as _sp, threading as _th, os as _os
            _server_py = str(_project_root / "server.py")
            def _do_restart():
                import time as _t; _t.sleep(1)
                _sp.Popen([_sys.executable, _server_py],
                          creationflags=getattr(_sp, "CREATE_NEW_CONSOLE", 0))
                _os._exit(0)
            _th.Thread(target=_do_restart, daemon=True).start()
        except Exception as _e:
            logger.warning("[OTA] 直接重启失败，依赖哨兵文件: %s", _e)

    return {
        "ok": True,
        "updated_files": updated,
        "size_kb": round(len(data) / 1024),
        "restarting": restarting,
        "message": f"已更新 {updated} 个文件" + ("，正在自动重启..." if restarting else ""),
    }


@router.post("/cluster/push-update-all")
def push_update_to_all_workers():
    """主控端：向所有在线 Worker 推送更新（服务端中转，避免浏览器 CORS）。"""
    from src.host.multi_host import get_cluster_coordinator
    import socket

    coord = get_cluster_coordinator()
    if not coord:
        raise HTTPException(400, "非集群模式")

    # 获取主控的局域网 IP
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        local_ip = "192.168.0.118"

    import yaml
    cluster_cfg_path = _project_root / "config" / "cluster.yaml"
    port = DEFAULT_OPENCLAW_PORT
    try:
        with open(cluster_cfg_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        port = cfg.get("local_port", DEFAULT_OPENCLAW_PORT)
    except Exception:
        pass

    coordinator_url = f"http://{local_ip}:{port}"

    overview = coord.get_overview()
    hosts = overview.get("hosts", [])
    results = []
    for h in hosts:
        if not h.get("online"):
            continue
        host_ip = h.get("host_ip", "")
        host_port = h.get("port", 8000)
        host_name = h.get("host_name", h.get("host_id", "?"))
        try:
            url = f"http://{host_ip}:{host_port}/cluster/pull-update"
            data = _json.dumps({"coordinator_url": coordinator_url}).encode()
            req = urllib.request.Request(url, data=data, method="POST",
                                        headers={"Content-Type": "application/json"})
            result = _json.loads(_http(req, timeout=90).decode())
            results.append({
                "host": host_name, "ip": host_ip,
                "ok": True, "updated_files": result.get("updated_files", 0),
            })
            logger.info("[OTA] %s: 更新成功 (%d 文件)", host_name, result.get("updated_files", 0))
        except Exception as e:
            results.append({"host": host_name, "ip": host_ip, "ok": False, "error": str(e)[:100]})
            logger.warning("[OTA] %s: 更新失败: %s", host_name, e)

    ok_count = sum(1 for r in results if r["ok"])
    fail_count = sum(1 for r in results if not r["ok"])
    return {
        "ok": ok_count > 0,
        "results": results,
        "summary": f"{ok_count} 成功, {fail_count} 失败",
    }


@router.post("/cluster/restart")
def cluster_restart():
    """本机重启：先尝试直接重启进程，fallback 写哨兵文件。"""
    import threading

    sentinel = _project_root / ".restart-required"
    reason = f"手动重启 @ {__import__('datetime').datetime.now().isoformat()}"
    sentinel.write_text(reason, encoding="utf-8")

    # 先启动新进程，再退出当前进程
    def _do_restart():
        import time, subprocess, os, sys
        time.sleep(2)  # 让 HTTP 响应先返回
        logger.info("[重启] 正在启动新进程...")
        python = sys.executable
        script = str(_project_root / "server.py")
        try:
            subprocess.Popen(
                [python, script],
                creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
                cwd=str(_project_root),
            )
            logger.info("[重启] 新进程已启动，当前进程即将退出")
            time.sleep(1)
        except Exception as e:
            logger.error("[重启] 启动新进程失败: %s", e)
        os._exit(0)

    threading.Thread(target=_do_restart, daemon=True).start()
    logger.info("[重启] 已触发重启（3秒后执行）")
    return {"ok": True, "message": "重启中，3秒后生效"}


@router.post("/cluster/restart-worker/{host_id}")
def restart_remote_worker(host_id: str):
    """主控端：远程触发指定 Worker 重启。"""
    from src.host.multi_host import get_cluster_coordinator

    coord = get_cluster_coordinator()
    if not coord:
        raise HTTPException(400, "当前不是 Coordinator")

    target = None
    for hid, info in coord._hosts.items():
        if hid == host_id or getattr(info, "host_name", "") == host_id:
            target = info
            break
    if not target:
        raise HTTPException(404, f"未找到主机: {host_id}")

    host_ip = target.host_ip
    port = target.port or 8000

    # 尝试多个 IP
    urls = []
    if hasattr(target, "all_ips") and target.all_ips:
        for ip in target.all_ips:
            urls.append(f"http://{ip}:{port}/cluster/restart")
    urls.append(f"http://{host_ip}:{port}/cluster/restart")

    for url in urls:
        try:
            req = urllib.request.Request(url, data=b'{}', method="POST",
                                        headers={"Content-Type": "application/json"})
            result = _json.loads(_http(req, timeout=10).decode())
            logger.info("[远程重启] %s: 成功", host_id)
            return {"ok": True, "host_id": host_id, "host_ip": host_ip, "result": result}
        except Exception as e:
            logger.warning("[远程重启] %s 尝试 %s 失败: %s", host_id, url, e)
            continue

    raise HTTPException(502, f"无法连接到 Worker {host_id}")


@router.post("/cluster/download-file", dependencies=[Depends(verify_api_key)])
def cluster_download_file(body: dict):
    """从指定 URL 下载文件到项目目录（仅允许写入 apk_repo/ 和 data/ 目录）。

    Body: {"url": "http://...", "dest": "apk_repo/xxx.apk"}
    """
    import urllib.request as _req
    dest_rel = body.get("dest", "")
    url = body.get("url", "")
    if not url or not dest_rel:
        raise HTTPException(400, "url 和 dest 均为必填")

    # 安全限制：只允许写入 apk_repo/、data/、config/（限 .yaml/.json）
    dest_norm = dest_rel.replace("\\", "/")
    allowed = (
        dest_norm.startswith("apk_repo/") or
        dest_norm.startswith("data/") or
        (dest_norm.startswith("config/") and dest_norm.endswith((".yaml", ".json")))
    )
    if not allowed:
        raise HTTPException(403, "只允许写入 apk_repo/、data/ 或 config/*.yaml 目录")

    dest_path = _project_root / dest_rel.replace("\\", "/")
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    logger.info("[下载文件] %s → %s", url, dest_path)
    try:
        _req.urlretrieve(url, str(dest_path))
    except Exception as e:
        raise HTTPException(502, f"下载失败: {e}")

    size_kb = round(dest_path.stat().st_size / 1024)
    logger.info("[下载文件] 完成: %s (%d KB)", dest_path.name, size_kb)
    return {"ok": True, "dest": str(dest_path), "size_kb": size_kb, "filename": dest_path.name}


# ── Cluster Lock Service endpoints (200 设备跨机锁) ─────────────────────


def _is_coordinator_role() -> bool:
    """仅 coordinator 节点才暴露 lock service. worker 应通过 HTTP 调主控."""
    try:
        cfg_path = config_file("cluster.yaml")
        if not cfg_path.exists():
            return True  # standalone 默认按 coordinator 处理
        import yaml
        with open(cfg_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        return str(cfg.get("role", "standalone")).lower() in ("coordinator", "standalone")
    except Exception:
        return True


@router.post("/cluster/lock/acquire", dependencies=[Depends(verify_api_key)])
def cluster_lock_acquire(body: dict):
    """申请跨 worker 设备锁.

    Body: {worker_id, device_id, resource="default", priority=50,
           ttl_sec=300, wait_timeout_sec=180}
    Response: {granted: bool, lock_id?, wait_ms, evicted_lock?, reason?}
    """
    if not _is_coordinator_role():
        raise HTTPException(400, "lock service 仅在 coordinator 节点可用")
    worker_id = (body.get("worker_id") or "").strip()
    device_id = (body.get("device_id") or "").strip()
    if not worker_id or not device_id:
        raise HTTPException(400, "worker_id 和 device_id 必填")
    resource = (body.get("resource") or "default").strip()
    priority = int(body.get("priority") or 50)
    ttl_sec = float(body.get("ttl_sec") or 300.0)
    wait_timeout_sec = float(body.get("wait_timeout_sec") or 180.0)

    from src.host.cluster_lock import get_lock_service
    res = get_lock_service().acquire(
        worker_id=worker_id,
        device_id=device_id,
        resource=resource,
        priority=priority,
        ttl_sec=ttl_sec,
        wait_timeout_sec=wait_timeout_sec,
    )
    return {
        "granted": res.granted,
        "lock_id": res.lock_id,
        "wait_ms": round(res.wait_ms, 1),
        "evicted_lock": res.evicted_lock,
        "reason": res.reason,
    }


@router.post("/cluster/lock/heartbeat", dependencies=[Depends(verify_api_key)])
def cluster_lock_heartbeat(body: dict):
    """续 lease.

    Body: {lock_id, extend_ttl_sec?}
    Response: {ok: bool, lock?: {...}}
    """
    if not _is_coordinator_role():
        raise HTTPException(400, "lock service 仅在 coordinator 节点可用")
    lock_id = (body.get("lock_id") or "").strip()
    if not lock_id:
        raise HTTPException(400, "lock_id 必填")
    extend = body.get("extend_ttl_sec")
    extend_f = float(extend) if extend is not None else None

    from src.host.cluster_lock import get_lock_service
    lock = get_lock_service().heartbeat(lock_id, extend_ttl_sec=extend_f)
    if lock is None:
        return {"ok": False, "reason": "lock_not_found_or_expired"}
    return {"ok": True, "lock": lock}


@router.post("/cluster/lock/release", dependencies=[Depends(verify_api_key)])
def cluster_lock_release(body: dict):
    """释放锁.

    Body: {lock_id}
    Response: {ok: bool}
    """
    if not _is_coordinator_role():
        raise HTTPException(400, "lock service 仅在 coordinator 节点可用")
    lock_id = (body.get("lock_id") or "").strip()
    if not lock_id:
        raise HTTPException(400, "lock_id 必填")

    from src.host.cluster_lock import get_lock_service
    ok = get_lock_service().release(lock_id)
    return {"ok": ok}


@router.get("/cluster/locks", dependencies=[Depends(verify_api_key)])
def cluster_lock_list(worker_id: str = "", device_id: str = ""):
    """列出当前 active locks. 可按 worker_id / device_id 过滤."""
    if not _is_coordinator_role():
        raise HTTPException(400, "lock service 仅在 coordinator 节点可用")
    from src.host.cluster_lock import get_lock_service
    svc = get_lock_service()
    locks = svc.list_locks(
        worker_id=worker_id or None,
        device_id=device_id or None,
    )
    return {
        "locks": locks,
        "metrics": svc.metrics(),
    }
