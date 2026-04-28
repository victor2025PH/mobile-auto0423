# -*- coding: utf-8 -*-
"""src.host.daily_report 单元测试."""
from __future__ import annotations

import datetime as _dt
import os
import tempfile
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from src.host.daily_report import (
    _date_window_iso,
    format_daily_text,
    get_daily_4_numbers,
)


# ─── _date_window_iso ───────────────────────────────────────────────────

class TestDateWindow:
    def test_today_is_current_utc(self):
        start, end = _date_window_iso(None)
        # start 是当日 00:00:00, end 是次日 00:00:00
        assert start.endswith("00:00:00")
        assert end.endswith("00:00:00")
        # 24 小时差
        s = _dt.datetime.strptime(start, "%Y-%m-%d %H:%M:%S")
        e = _dt.datetime.strptime(end, "%Y-%m-%d %H:%M:%S")
        assert (e - s).total_seconds() == 86400

    def test_explicit_date(self):
        start, end = _date_window_iso("2026-04-27")
        assert start == "2026-04-27 00:00:00"
        assert end == "2026-04-28 00:00:00"

    def test_invalid_format_raises(self):
        with pytest.raises(ValueError):
            _date_window_iso("2026/04/27")
        with pytest.raises(ValueError):
            _date_window_iso("April 27 2026")


# ─── format_daily_text ──────────────────────────────────────────────────

class TestFormatDailyText:
    def _sample(self):
        return {
            "date": "2026-04-27",
            "device_id": "",
            "friend_requests_sent": 1500,
            "friend_accepted": 450,
            "accept_rate": 0.30,
            "greeting_sent": 450,
            "greeting_replied": 70,
            "reply_rate": 0.1556,
            "line_invites_sent": 25,
            "invite_rate": 0.3571,
            "raw_events_in_window": 2050,
        }

    def test_contains_all_4_numbers(self):
        out = format_daily_text(self._sample())
        assert "1500" in out
        assert "450" in out
        assert "70" in out
        assert "25" in out

    def test_contains_percentages(self):
        out = format_daily_text(self._sample())
        assert "30.0%" in out
        assert "15.6%" in out
        assert "35.7%" in out

    def test_shows_all_devices_when_empty(self):
        out = format_daily_text(self._sample())
        assert "全部" in out

    def test_shows_specific_device(self):
        s = self._sample()
        s["device_id"] = "ABCDEF1234"
        out = format_daily_text(s)
        assert "ABCDEF1234" in out


# ─── get_daily_4_numbers (集成) ─────────────────────────────────────────

class TestGetDaily4Numbers:
    @pytest.fixture
    def temp_db(self, tmp_path, monkeypatch):
        """指向独立 sqlite, 注入 4 类 events 后跑统计.

        DB_PATH 是 module-level 常量, monkeypatch 直接换它即可让 _connect()
        指向临时 db. 同时清空表保证测试隔离.
        """
        from pathlib import Path
        from src.host import database as dbm

        db_path = tmp_path / "test_daily.db"
        monkeypatch.setattr(dbm, "DB_PATH", db_path)
        # init schema (会用刚 patch 的 DB_PATH)
        dbm.init_db()
        yield str(db_path)

    def test_zero_events_returns_zeros(self, temp_db):
        stats = get_daily_4_numbers(date_str="2026-04-27")
        assert stats["friend_requests_sent"] == 0
        assert stats["friend_accepted"] == 0
        assert stats["accept_rate"] == 0.0
        assert stats["line_invites_sent"] == 0

    def test_inserted_events_counted(self, temp_db):
        from src.host.database import _connect
        with _connect() as conn:
            # 注入 5 个 add_friend_sent + 2 个 add_friend_accepted
            for i in range(5):
                conn.execute(
                    "INSERT INTO fb_contact_events (device_id, peer_name, "
                    "event_type, at) VALUES (?, ?, ?, ?)",
                    ("dev1", f"peer_{i}", "add_friend_sent",
                     "2026-04-27 10:00:00"),
                )
            for i in range(2):
                conn.execute(
                    "INSERT INTO fb_contact_events (device_id, peer_name, "
                    "event_type, at) VALUES (?, ?, ?, ?)",
                    ("dev1", f"peer_{i}", "add_friend_accepted",
                     "2026-04-27 11:00:00"),
                )
        stats = get_daily_4_numbers(date_str="2026-04-27")
        assert stats["friend_requests_sent"] == 5
        assert stats["friend_accepted"] == 2
        # accept_rate = 2/5 = 0.4
        assert stats["accept_rate"] == 0.4

    def test_device_filter(self, temp_db):
        from src.host.database import _connect
        with _connect() as conn:
            # dev1: 3 sent
            for i in range(3):
                conn.execute(
                    "INSERT INTO fb_contact_events (device_id, peer_name, "
                    "event_type, at) VALUES (?, ?, ?, ?)",
                    ("dev1", f"a{i}", "add_friend_sent",
                     "2026-04-27 10:00:00"),
                )
            # dev2: 7 sent
            for i in range(7):
                conn.execute(
                    "INSERT INTO fb_contact_events (device_id, peer_name, "
                    "event_type, at) VALUES (?, ?, ?, ?)",
                    ("dev2", f"b{i}", "add_friend_sent",
                     "2026-04-27 10:00:00"),
                )
        s_all = get_daily_4_numbers(date_str="2026-04-27")
        s_dev1 = get_daily_4_numbers(date_str="2026-04-27",
                                       device_id="dev1")
        s_dev2 = get_daily_4_numbers(date_str="2026-04-27",
                                       device_id="dev2")
        assert s_all["friend_requests_sent"] == 10
        assert s_dev1["friend_requests_sent"] == 3
        assert s_dev2["friend_requests_sent"] == 7

    def test_date_window_excludes_other_days(self, temp_db):
        from src.host.database import _connect
        with _connect() as conn:
            # 4-26 & 4-28 各 1 条, 4-27 不应包含
            for d in ["2026-04-26", "2026-04-28"]:
                conn.execute(
                    "INSERT INTO fb_contact_events (device_id, peer_name, "
                    "event_type, at) VALUES (?, ?, ?, ?)",
                    ("dev1", f"p_{d}", "add_friend_sent",
                     f"{d} 12:00:00"),
                )
        stats = get_daily_4_numbers(date_str="2026-04-27")
        assert stats["friend_requests_sent"] == 0

    def test_line_invites_takes_max_of_two_event_types(self, temp_db):
        """line_invites_sent = max(line_dispatch_planned, wa_referral_sent).

        因为有两套统计口径并存, 取 max 而非 sum 避免重复计算.
        """
        from src.host.database import _connect
        with _connect() as conn:
            # wa_referral_sent: 5
            for i in range(5):
                conn.execute(
                    "INSERT INTO fb_contact_events (device_id, peer_name, "
                    "event_type, at) VALUES (?, ?, ?, ?)",
                    ("dev1", f"r{i}", "wa_referral_sent",
                     "2026-04-27 14:00:00"),
                )
            # line_dispatch_planned: 3 (新口径, 取 max → 5 不变)
            for i in range(3):
                conn.execute(
                    "INSERT INTO fb_contact_events (device_id, peer_name, "
                    "event_type, at) VALUES (?, ?, ?, ?)",
                    ("dev1", f"d{i}", "line_dispatch_planned",
                     "2026-04-27 15:00:00"),
                )
        stats = get_daily_4_numbers(date_str="2026-04-27")
        assert stats["line_invites_sent"] == 5
