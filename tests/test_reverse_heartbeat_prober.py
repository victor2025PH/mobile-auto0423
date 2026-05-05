# -*- coding: utf-8 -*-
"""Reverse heartbeat prober (Stage I, 2026-05-05) 单元测试.

主控主动 GET worker /devices, 把 worker HeartbeatSender 没在 push 但 server
活着的 host 注册成 online. 不依赖真网络, mock urlopen.
"""
from __future__ import annotations

import json
import time
import threading
from unittest.mock import MagicMock, patch

import pytest

from src.host import multi_host
from src.host.multi_host import (
    ClusterCoordinator,
    HostInfo,
    _ReverseHeartbeatProber,
    start_reverse_prober,
    stop_reverse_prober,
    reset_reverse_prober_for_tests,
)


@pytest.fixture
def fresh_coord(monkeypatch):
    """每 test 独立 coordinator instance + 不写 prod cluster_state.json."""
    coord = ClusterCoordinator.__new__(ClusterCoordinator)
    coord._lock = threading.Lock()
    coord._hosts = {}
    coord._secret = ""
    monkeypatch.setattr(multi_host, "_coordinator", coord)
    # 防写真 cluster_state.json
    monkeypatch.setattr(coord, "_persist_state", lambda: None)
    yield coord


# ── reverse_probe_worker 单元 ────────────────────────────────────────


def test_probe_unknown_host_returns_false(fresh_coord):
    assert fresh_coord.reverse_probe_worker("not_in_hosts") is False


def test_probe_no_ip_returns_false(fresh_coord):
    fresh_coord._hosts["w_noip"] = HostInfo(
        host_id="w_noip", host_name="W", host_ip="", port=8000)
    assert fresh_coord.reverse_probe_worker("w_noip") is False


def test_probe_coordinator_self_returns_false(fresh_coord):
    fresh_coord._hosts["coordinator"] = HostInfo(
        host_id="coordinator", host_name="主控",
        host_ip="192.168.0.118", port=18080)
    assert fresh_coord.reverse_probe_worker("coordinator") is False


def test_probe_success_registers_online(fresh_coord):
    fresh_coord._hosts["w03"] = HostInfo(
        host_id="w03", host_name="W03",
        host_ip="192.168.0.101", port=8000,
        last_heartbeat=0.0, online=False)

    fake_devices = [
        {"device_id": "FAKE_001", "status": "connected"},
        {"device_id": "FAKE_002", "status": "connected"},
    ]
    fake_resp = MagicMock()
    fake_resp.read.return_value = json.dumps(fake_devices).encode()
    fake_resp.__enter__ = lambda self: self
    fake_resp.__exit__ = lambda self, *a: False

    with patch("urllib.request.urlopen", return_value=fake_resp):
        ok = fresh_coord.reverse_probe_worker("w03", timeout=2.0)

    assert ok is True
    h = fresh_coord._hosts["w03"]
    assert h.online is True
    assert len(h.devices) == 2
    assert h.devices[0]["device_id"] == "FAKE_001"
    # last_heartbeat 应被更新到接近 now
    assert (time.time() - h.last_heartbeat) < 5


def test_probe_dict_response_format(fresh_coord):
    """worker /devices 返 {"devices": [...]} 的 dict 形式也能解."""
    fresh_coord._hosts["w03"] = HostInfo(
        host_id="w03", host_ip="192.168.0.101", port=8000)
    fake_resp = MagicMock()
    fake_resp.read.return_value = json.dumps(
        {"devices": [{"device_id": "X1"}]}).encode()
    fake_resp.__enter__ = lambda self: self
    fake_resp.__exit__ = lambda self, *a: False
    with patch("urllib.request.urlopen", return_value=fake_resp):
        assert fresh_coord.reverse_probe_worker("w03") is True
    assert fresh_coord._hosts["w03"].devices[0]["device_id"] == "X1"


def test_probe_string_devices_normalized(fresh_coord):
    """worker 返字符串 device_id 列表也能被规范化为 dict."""
    fresh_coord._hosts["w03"] = HostInfo(
        host_id="w03", host_ip="192.168.0.101", port=8000)
    fake_resp = MagicMock()
    fake_resp.read.return_value = b'["DID_A", "DID_B"]'
    fake_resp.__enter__ = lambda self: self
    fake_resp.__exit__ = lambda self, *a: False
    with patch("urllib.request.urlopen", return_value=fake_resp):
        assert fresh_coord.reverse_probe_worker("w03") is True
    devs = fresh_coord._hosts["w03"].devices
    assert len(devs) == 2
    assert devs[0]["device_id"] == "DID_A"
    assert devs[0]["status"] == "unknown"


def test_probe_http_failure_returns_false_keeps_offline(fresh_coord):
    fresh_coord._hosts["w03"] = HostInfo(
        host_id="w03", host_ip="192.168.0.101", port=8000,
        last_heartbeat=0.0, online=False)

    with patch("urllib.request.urlopen",
               side_effect=ConnectionRefusedError("nope")):
        assert fresh_coord.reverse_probe_worker("w03") is False

    # 失败的 host 保持 offline, 不引入 false positive
    assert fresh_coord._hosts["w03"].online is False
    assert fresh_coord._hosts["w03"].last_heartbeat == 0.0


def test_probe_invalid_json_returns_false(fresh_coord):
    fresh_coord._hosts["w03"] = HostInfo(
        host_id="w03", host_ip="192.168.0.101", port=8000)
    fake_resp = MagicMock()
    fake_resp.read.return_value = b"not json"
    fake_resp.__enter__ = lambda self: self
    fake_resp.__exit__ = lambda self, *a: False
    with patch("urllib.request.urlopen", return_value=fake_resp):
        assert fresh_coord.reverse_probe_worker("w03") is False


def test_probe_secret_header_passed(fresh_coord):
    fresh_coord._secret = "TEST_SECRET_123"
    fresh_coord._hosts["w03"] = HostInfo(
        host_id="w03", host_ip="192.168.0.101", port=8000)
    fake_resp = MagicMock()
    fake_resp.read.return_value = b'[]'
    fake_resp.__enter__ = lambda self: self
    fake_resp.__exit__ = lambda self, *a: False

    captured = {}

    def _fake_urlopen(req, timeout=None):
        captured["headers"] = dict(req.header_items())
        return fake_resp

    with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
        fresh_coord.reverse_probe_worker("w03")

    # X-Cluster-Secret header 应被加上 (case-insensitive 查)
    keys_lower = {k.lower() for k in captured.get("headers", {})}
    assert "x-cluster-secret" in keys_lower


# ── _ReverseHeartbeatProber 后台线程 ─────────────────────────────────


def test_prober_skips_online_hosts(fresh_coord, monkeypatch):
    """已 online 的 host (last_heartbeat 新鲜) 不应被 probe."""
    now = time.time()
    fresh_coord._hosts["w_online"] = HostInfo(
        host_id="w_online", host_ip="192.168.0.101", port=8000,
        last_heartbeat=now, online=True)

    probed = []
    monkeypatch.setattr(
        fresh_coord, "reverse_probe_worker",
        lambda hid, timeout=5.0: probed.append(hid) or True,
    )

    pr = _ReverseHeartbeatProber(interval=10.0, startup_delay=0)
    pr._tick()

    assert probed == []


def test_prober_probes_stale_hosts(fresh_coord, monkeypatch):
    """last_heartbeat 超过 _HOST_TIMEOUT 的 host 应被 probe."""
    fresh_coord._hosts["w_stale"] = HostInfo(
        host_id="w_stale", host_ip="192.168.0.101", port=8000,
        last_heartbeat=0.0, online=False)
    fresh_coord._hosts["w_fresh"] = HostInfo(
        host_id="w_fresh", host_ip="192.168.0.102", port=8000,
        last_heartbeat=time.time(), online=True)

    probed = []
    monkeypatch.setattr(
        fresh_coord, "reverse_probe_worker",
        lambda hid, timeout=5.0: probed.append(hid) or False,
    )

    pr = _ReverseHeartbeatProber(interval=10.0, startup_delay=0)
    pr._tick()

    assert probed == ["w_stale"]


def test_prober_skips_coordinator(fresh_coord, monkeypatch):
    fresh_coord._hosts["coordinator"] = HostInfo(
        host_id="coordinator", host_ip="192.168.0.118", port=18080,
        last_heartbeat=0.0, online=False)

    probed = []
    monkeypatch.setattr(
        fresh_coord, "reverse_probe_worker",
        lambda hid, timeout=5.0: probed.append(hid) or True,
    )
    pr = _ReverseHeartbeatProber(interval=10.0, startup_delay=0)
    pr._tick()
    assert probed == []


def test_prober_tick_swallows_exception(fresh_coord, monkeypatch):
    """单 host probe 抛异常不影响线程."""
    fresh_coord._hosts["w03"] = HostInfo(
        host_id="w03", host_ip="192.168.0.101", port=8000,
        last_heartbeat=0.0, online=False)

    monkeypatch.setattr(
        fresh_coord, "reverse_probe_worker",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    pr = _ReverseHeartbeatProber(interval=10.0, startup_delay=0)
    # 不应 propagate
    pr._tick()
    assert pr._iterations == 1


# ── start/stop lifecycle ────────────────────────────────────────────


def test_start_then_stop_clean(fresh_coord, monkeypatch):
    monkeypatch.delenv("OPENCLAW_DISABLE_REVERSE_PROBE", raising=False)
    reset_reverse_prober_for_tests()

    t = start_reverse_prober(interval=0.05, startup_delay=0)
    assert t is not None
    assert t.is_alive()
    time.sleep(0.15)  # 让它跑几次 tick
    assert t._iterations >= 2

    ok = stop_reverse_prober(timeout_sec=2.0)
    assert ok is True
    assert not t.is_alive()


def test_env_disable_returns_none(monkeypatch):
    monkeypatch.setenv("OPENCLAW_DISABLE_REVERSE_PROBE", "1")
    reset_reverse_prober_for_tests()
    t = start_reverse_prober()
    assert t is None


def test_idempotent_start(fresh_coord, monkeypatch):
    monkeypatch.delenv("OPENCLAW_DISABLE_REVERSE_PROBE", raising=False)
    reset_reverse_prober_for_tests()

    t1 = start_reverse_prober(interval=10.0, startup_delay=0)
    t2 = start_reverse_prober(interval=10.0, startup_delay=0)
    assert t1 is t2  # 同一 instance 不重复启

    stop_reverse_prober()


def test_reset_for_tests_stops_thread(fresh_coord, monkeypatch):
    """Stage C.2 教训: reset 必须真 stop+join, 不能只清单例."""
    monkeypatch.delenv("OPENCLAW_DISABLE_REVERSE_PROBE", raising=False)
    reset_reverse_prober_for_tests()  # clean from prior test

    t = start_reverse_prober(interval=0.05, startup_delay=0)
    assert t.is_alive()

    reset_reverse_prober_for_tests()
    time.sleep(0.1)
    assert not t.is_alive(), "reset_for_tests 应真 stop+join thread"
