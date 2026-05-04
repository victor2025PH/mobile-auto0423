"""scrcpy_manager._resolve_adb 路径解析顺序单测.

事故背景: schtasks/Service 拉起 worker 时只继承 system PATH, 而 adb 装在 user PATH
下时, subprocess.run(["adb", ...]) 直接 WinError 2 → scrcpy 整条流死锁. 修法把 adb
解析与 PATH 解耦: env override → which → 已知 Windows 安装位置 → 裸 "adb".
"""
from __future__ import annotations

import os
from unittest.mock import patch

from src.host.scrcpy_manager import _resolve_adb


def test_env_override_wins_when_file_exists(tmp_path):
    fake_adb = tmp_path / "fake_adb.exe"
    fake_adb.write_bytes(b"")
    with patch.dict(os.environ, {"OPENCLAW_ADB_PATH": str(fake_adb)}, clear=False):
        with patch("src.host.scrcpy_manager.shutil.which") as mock_which:
            assert _resolve_adb() == str(fake_adb)
            mock_which.assert_not_called()


def test_all_fail_returns_bare_adb_string():
    env = {k: v for k, v in os.environ.items() if k != "OPENCLAW_ADB_PATH"}
    with patch.dict(os.environ, env, clear=True):
        with patch("src.host.scrcpy_manager.shutil.which", return_value=None), \
             patch("src.host.scrcpy_manager.os.path.isfile", return_value=False):
            assert _resolve_adb() == "adb"
