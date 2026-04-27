# -*- coding: utf-8 -*-
"""
preflight._check_network — TCP 443 nc fallback + cache TTL 单测.

P3 (2026-04-27): 圈层拓客真机重试 IJ8HZLOR 暴露 curl/ping 全 fail 但 chrome
真通的情况, 加 nc TCP 探测兜底. 本测试防 nc fallback 路径被未来 PR 误删.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from src.host.preflight import _CACHE_TTL, _check_network


def _stub_proc(stdout: str = "", returncode: int = 0):
    """模拟 _sp_run_text 返回的 CompletedProcess-like 对象."""
    return SimpleNamespace(stdout=stdout, stderr="", returncode=returncode)


# ── 现有路径回归 (curl 成功) ──────────────────────────────────────────

@patch("src.host.preflight._sp_run_text")
def test_curl_204_success(mock_run):
    """curl 第一次返回 204 → 立即 True, 不走 fallback."""
    mock_run.return_value = _stub_proc(stdout="204")
    ok, msg = _check_network("DEVICE1")
    assert ok
    assert "connected(curl)" in msg
    # curl 第一个 variant 就成功了, 只调用 1 次 _sp_run_text
    assert mock_run.call_count == 1


# ── 新增 nc fallback 路径 ────────────────────────────────────────────

@patch("src.host.preflight._sp_run_text")
def test_nc_toybox_fallback_when_curl_and_ping_fail(mock_run):
    """curl/HEAD/ping 全 fail, toybox nc 成功 → True (新 fallback 路径)."""
    call_seq = []

    def stub(cmd_args, **kwargs):
        # cmd_args 是 list, 真实命令在 cmd_args[3] (adb -s X shell <cmd>)
        # cmd_args = ["adb", "-s", device_id, "shell", actual_cmd]
        cmd = cmd_args[4] if len(cmd_args) >= 5 else ""
        call_seq.append(cmd)
        # curl 全部空 stdout (无 binary)
        if "curl" in cmd and "nc" not in cmd:
            return _stub_proc(stdout="")
        # ping 失败 (returncode != 0)
        if "ping" in cmd:
            return _stub_proc(stdout="", returncode=1)
        # toybox nc 成功 (实际命令前缀: "echo '' | toybox nc ...")
        if "toybox nc" in cmd:
            return _stub_proc(stdout="TCP_OK\n")
        return _stub_proc(stdout="", returncode=1)

    mock_run.side_effect = stub
    ok, msg = _check_network("DEVICE1")
    assert ok, f"应该通过 nc fallback 但 fail: {msg}"
    assert "tcp443" in msg.lower()
    assert "toybox-nc" in msg


@patch("src.host.preflight._sp_run_text")
def test_nc_system_bin_fallback_when_toybox_unavailable(mock_run):
    """toybox nc 不可用, /system/bin/nc 成功 → True."""
    def stub(cmd_args, **kwargs):
        # cmd_args = ["adb", "-s", device_id, "shell", actual_cmd]
        cmd = cmd_args[4] if len(cmd_args) >= 5 else ""
        if "/system/bin/nc" in cmd:
            return _stub_proc(stdout="TCP_OK\n")
        # 其他全 fail
        if "curl" in cmd or "ping" in cmd or "toybox nc" in cmd:
            return _stub_proc(stdout="", returncode=1)
        return _stub_proc(stdout="", returncode=1)

    mock_run.side_effect = stub
    ok, msg = _check_network("DEVICE1")
    assert ok, f"应通过 /system/bin/nc fallback 但 fail: {msg}"
    assert "sys-nc" in msg


@patch("src.host.preflight._sp_run_text")
def test_all_methods_fail_error_mentions_tcp_nc(mock_run):
    """curl/HEAD/ping/nc 全 fail → False, 错误信息含 'TCP 443 nc 探测也未通过'."""
    mock_run.return_value = _stub_proc(stdout="", returncode=1)
    ok, msg = _check_network("DEVICE1")
    assert not ok
    assert "TCP 443 nc 探测也未通过" in msg
    # 完整失败链条都该 mentioned (帮助 ops 诊断)
    assert "ICMP 探测也未通过" in msg


@patch("src.host.preflight._sp_run_text")
def test_nc_does_not_short_circuit_curl_success(mock_run):
    """curl 成功时不应该走到 nc 路径 (避免不必要 adb 调用)."""
    mock_run.return_value = _stub_proc(stdout="204")
    _check_network("DEVICE1")
    # 全部调用都不该是 nc
    for call in mock_run.call_args_list:
        # call[0][0] = cmd_args list, [4] = actual shell cmd
        cmd = call[0][0][4] if len(call[0][0]) >= 5 else ""
        assert "nc" not in cmd or "ncat" in cmd, f"nc 不该被触发: {cmd}"


# ── Cache TTL 改动 ───────────────────────────────────────────────────

def test_cache_ttl_extended_to_180s():
    """P3: TTL 90 → 180s 防同 task 链反复探测.

    180s 让一个 task 序列 (1-3min) 复用 cache 合理, 真断网时仍 180s 后重检.
    """
    assert _CACHE_TTL == 180


def test_cache_ttl_not_too_aggressive():
    """sanity: TTL 不应超过 600s (10min), 否则真断网误以为通."""
    assert _CACHE_TTL <= 600
