# -*- coding: utf-8 -*-
"""壁纸部署失败细分原因单测 — 锁定 helper Receiver result code 分类、
WallpaperDeployResult dataclass 行为、threading.local 双联体语义。

背景：PR #136 让 helper APK Receiver 的 result code 能被同步看到; 本 PR 把
result data 透传成结构化 (kind, detail) 写到 alias.wallpaper_error_*, 让前端
chip hover 一眼看出根因 (perm_denied / decode_failed / file_not_found / ...)
而不是笼统的 "deploy_failed".
"""
import threading

from src.utils.wallpaper_generator import (
    WP_ERR_OK,
    WP_ERR_DECODE,
    WP_ERR_FILE_NOT_FOUND,
    WP_ERR_IO,
    WP_ERR_RESULT_OTHER,
    WP_ERR_PUSH,
    WallpaperDeployResult,
    _classify_helper_result,
    _set_last_deploy_result,
    get_last_deploy_result,
)


# ── _classify_helper_result: am broadcast 同步输出 → (kind, detail) ──

def test_classify_result_0_ok():
    out = 'Broadcasting: Intent { ... }\nBroadcast completed: result=0, data="OK"'
    kind, detail = _classify_helper_result(out)
    assert kind == WP_ERR_OK
    assert detail == "OK"


def test_classify_result_2_file_not_found():
    out = 'Broadcast completed: result=2, data="File not found: /sdcard/x.png"'
    kind, detail = _classify_helper_result(out)
    assert kind == WP_ERR_FILE_NOT_FOUND
    assert "File not found" in detail


def test_classify_result_3_decode_failed_with_perm_hint():
    """result=3 通常因 READ_EXTERNAL_STORAGE/READ_MEDIA_IMAGES 缺失 — detail 必须含权限提示。"""
    out = 'Broadcast completed: result=3, data="Failed to decode: /sdcard/x.png"'
    kind, detail = _classify_helper_result(out)
    assert kind == WP_ERR_DECODE
    assert "Failed to decode" in detail
    assert "READ_EXTERNAL_STORAGE" in detail or "权限" in detail or "READ_MEDIA_IMAGES" in detail


def test_classify_result_4_io_error():
    out = 'Broadcast completed: result=4, data="Error: Disk full"'
    kind, detail = _classify_helper_result(out)
    assert kind == WP_ERR_IO
    assert "Disk full" in detail


def test_classify_result_unknown_falls_back_to_other():
    out = 'Broadcast completed: result=99, data="something weird"'
    kind, detail = _classify_helper_result(out)
    assert kind == WP_ERR_RESULT_OTHER


def test_classify_no_result_marker_uses_raw_text():
    """没有 result= 段时也得给个 detail 避免 None。"""
    out = 'some adb error before broadcast'
    kind, detail = _classify_helper_result(out)
    assert kind == WP_ERR_RESULT_OTHER
    assert detail  # 非空


# ── WallpaperDeployResult dataclass: __bool__ + factory ──

def test_result_success_truthy():
    r = WallpaperDeployResult.success()
    assert r.ok is True
    assert bool(r) is True
    assert r.kind == WP_ERR_OK


def test_result_failure_falsy_with_detail():
    r = WallpaperDeployResult.failure(WP_ERR_DECODE, "Failed to decode: ...")
    assert r.ok is False
    assert bool(r) is False
    assert r.kind == WP_ERR_DECODE
    assert "decode" in r.detail


def test_result_default_detail_empty():
    r = WallpaperDeployResult.failure(WP_ERR_PUSH)
    assert r.detail == ""


# ── threading.local 双联体: deploy_wallpaper bool + get_last_deploy_result ──

def test_tls_set_get_roundtrip():
    _set_last_deploy_result("DID-A", WallpaperDeployResult.success())
    got = get_last_deploy_result("DID-A")
    assert got is not None
    assert got.ok is True


def test_tls_per_device_isolated():
    _set_last_deploy_result("DID-A", WallpaperDeployResult.success())
    _set_last_deploy_result(
        "DID-B", WallpaperDeployResult.failure(WP_ERR_DECODE, "x"))
    a = get_last_deploy_result("DID-A")
    b = get_last_deploy_result("DID-B")
    assert a.ok is True
    assert b.ok is False
    assert b.kind == WP_ERR_DECODE


def test_tls_unknown_device_returns_none():
    assert get_last_deploy_result("NEVER-SEEN-DID") is None


def test_tls_thread_local_no_cross_thread_leak():
    """两个线程分别 set 不同结果, 互不可见 (threading.local 隔离)。"""
    seen = {}

    def worker_a():
        _set_last_deploy_result(
            "SAME-DID", WallpaperDeployResult.failure(WP_ERR_PUSH, "thread A"))
        seen["a"] = get_last_deploy_result("SAME-DID")

    def worker_b():
        _set_last_deploy_result(
            "SAME-DID", WallpaperDeployResult.success())
        seen["b"] = get_last_deploy_result("SAME-DID")

    t_a = threading.Thread(target=worker_a)
    t_b = threading.Thread(target=worker_b)
    t_a.start(); t_a.join()
    t_b.start(); t_b.join()
    # 各自看到自己 set 的结果, 不串
    assert seen["a"].kind == WP_ERR_PUSH
    assert seen["a"].detail == "thread A"
    assert seen["b"].ok is True
