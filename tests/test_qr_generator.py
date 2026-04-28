# -*- coding: utf-8 -*-
"""src.utils.qr_generator 单元测试."""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from src.utils.qr_generator import (
    build_line_qr,
    cleanup_old_qrs,
    line_id_to_qr_url,
    normalize_line_id,
)


# ─── normalize_line_id ──────────────────────────────────────────────────

class TestNormalizeLineId:
    def test_strips_at_sign(self):
        assert normalize_line_id("@store123") == "store123"

    def test_lowercases(self):
        assert normalize_line_id("StoreABC") == "storeabc"

    def test_replaces_unsafe_chars(self):
        assert normalize_line_id("store/123") == "store_123"

    def test_extracts_from_url(self):
        # 从 line.me URL 抽 id
        result = normalize_line_id("https://line.me/R/ti/p/~mystore")
        assert "mystore" in result

    def test_empty_returns_empty(self):
        assert normalize_line_id("") == ""
        assert normalize_line_id(None) == ""

    def test_caps_length(self):
        long_id = "a" * 200
        result = normalize_line_id(long_id)
        assert len(result) <= 64


# ─── line_id_to_qr_url ──────────────────────────────────────────────────

class TestLineIdToQrUrl:
    def test_at_form(self):
        assert line_id_to_qr_url("@store123") == "https://line.me/R/ti/p/~store123"

    def test_plain_form(self):
        assert line_id_to_qr_url("store123") == "https://line.me/R/ti/p/~store123"

    def test_full_url_passthrough(self):
        url = "https://line.me/R/ti/p/~mystore"
        assert line_id_to_qr_url(url) == url

    def test_old_deep_link_passthrough(self):
        url = "https://line.me/ti/p/abc123"
        assert line_id_to_qr_url(url) == url

    def test_empty_returns_empty(self):
        assert line_id_to_qr_url("") == ""
        assert line_id_to_qr_url(None) == ""


# ─── build_line_qr ──────────────────────────────────────────────────────

class TestBuildLineQr:
    def test_creates_png_file(self):
        path = build_line_qr("@teststore_unit_001")
        assert path is not None
        assert os.path.exists(path)
        assert path.endswith(".png")
        # 文件非空
        assert os.path.getsize(path) > 100

    def test_cache_hit_returns_same_path(self):
        path1 = build_line_qr("@teststore_cache_check")
        path2 = build_line_qr("@teststore_cache_check")
        assert path1 == path2

    def test_force_regen_works(self):
        path = build_line_qr("@teststore_force_regen", force_regen=True)
        assert path is not None
        # 同一 id 拿同一路径
        path2 = build_line_qr("@teststore_force_regen", force_regen=True)
        assert path == path2

    def test_different_ids_different_paths(self):
        p1 = build_line_qr("@teststore_a")
        p2 = build_line_qr("@teststore_b")
        assert p1 != p2

    def test_url_form_works(self):
        path = build_line_qr("https://line.me/R/ti/p/~urltest_001")
        assert path is not None
        assert os.path.exists(path)

    def test_empty_returns_none(self):
        assert build_line_qr("") is None
        assert build_line_qr(None) is None

    def test_box_size_param(self):
        # 小 box_size → 文件更小
        small = build_line_qr("@boxtest_small", box_size=5,
                                force_regen=True)
        big = build_line_qr("@boxtest_big", box_size=20,
                              force_regen=True)
        assert os.path.getsize(small) < os.path.getsize(big)


# ─── cleanup_old_qrs ────────────────────────────────────────────────────

class TestCleanupOldQrs:
    def test_returns_int(self):
        # 不应抛, 即使没文件
        n = cleanup_old_qrs(older_than_seconds=999999)
        assert isinstance(n, int)
        assert n >= 0

    def test_deletes_old_files(self, tmp_path, monkeypatch):
        # 用 monkeypatch 把缓存目录指到 tmp_path, 注入旧时间戳的文件
        from src.utils import qr_generator as qrg
        monkeypatch.setattr(qrg, "_qr_cache_dir", lambda: tmp_path)
        # 生成 1 个老文件 (mtime 设到 30 天前)
        old = tmp_path / "old_test.png"
        old.write_bytes(b"\x89PNG\r\n\x1a\n")
        old_ts = time.time() - 30 * 24 * 3600
        os.utime(old, (old_ts, old_ts))
        # 生成 1 个新文件 (mtime 现在)
        new = tmp_path / "new_test.png"
        new.write_bytes(b"\x89PNG\r\n\x1a\n")

        n = cleanup_old_qrs(older_than_seconds=7 * 24 * 3600)
        assert n == 1
        assert not old.exists()
        assert new.exists()
