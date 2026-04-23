# -*- coding: utf-8 -*-
"""Shared test fixtures."""

import logging
import os
import tempfile
import pytest
from pathlib import Path

# 单测会启动 daemon/池化线程，pytest 结束并关闭 capture 的 stdout 后，这些线程仍可能写
# logging；StreamHandler.emit 会失败并触发 handleError 刷屏。默认关闭；调试时可设
# 环境变量 PYTEST_LOG_RAISE=1 恢复标准库的 raiseExceptions 行为。
if not os.environ.get("PYTEST_LOG_RAISE", "").strip() in ("1", "true", "yes", "y"):
    logging.raiseExceptions = False

os.environ.setdefault("OPENCLAW_API_KEY", "")
# 跳过设备 HealthMonitor 后台线程，避免 pytest 结束时 I/O on closed file（生产不设即可）
os.environ.setdefault("OPENCLAW_DISABLE_DEVICE_HEALTH_MONITOR", "1")
# 跳过启动时壁纸自动编号守护线程（单测/CI）
os.environ.setdefault("OPENCLAW_DISABLE_AUTO_WALLPAPER_THREAD", "1")
# AI 指令创建任务前的设备预检：默认关闭以免单测依赖真机/ADB
os.environ.setdefault("OPENCLAW_CHAT_PREFLIGHT", "0")
# 分流单测走规则，不调用真实 LLM（与生产 chat.yaml 的 llm_first 区分）
os.environ.setdefault("OPENCLAW_CHAT_TRIAGE_STRATEGY", "rules_first")
# 合并解析单测关闭，避免 Mock ChatAI 误走 parse_unified
os.environ.setdefault("OPENCLAW_UNIFIED_PARSE", "0")


@pytest.fixture
def tmp_db(tmp_path):
    """临时 SQLite 数据库，每个测试独立。"""
    import src.host.database as db_mod
    original = db_mod.DB_PATH
    db_mod.DB_PATH = tmp_path / "test.db"
    db_mod.init_db()
    yield db_mod.DB_PATH
    db_mod.DB_PATH = original
