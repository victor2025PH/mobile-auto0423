# -*- coding: utf-8 -*-
"""LINE QR 码生成器 (Phase 22, 2026-04-27).

为"文字 + QR 双轨"LINE 引流方案的图片轨提供能力支撑。

设计:
  - 输入: line_id (e.g. "@store123" / "store123" / 完整 line.me URL)
  - 输出: PNG 文件路径 (在系统 temp 目录下)
  - 缓存: 同 line_id 24h 内复用 (避免重复生成 + 减少磁盘抖动)

调用方:
  from src.utils.qr_generator import build_line_qr
  png_path = build_line_qr("@store123")
  # → /tmp/openclaw_line_qr/store123_<hash>.png

关键决策:
  * QR 内容用 https://line.me/R/ti/p/~<id> (带 ~ 是 LINE 官方加好友 deep link)
  * 即使 FB 屏蔽 line.me 文字链接, QR 图片里的 URL 不被扫描屏蔽
  * 图片大小默认 box_size=10 / border=2, 生成 ~290x290 px PNG, 文件 < 5KB
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# QR 缓存 24h
_QR_CACHE_TTL_S = 24 * 3600
_QR_DIR_NAME = "openclaw_line_qr"

# 仅允许 LINE ID 字符: 字母数字 + . _ - (与 LineChannel._LINE_ID_RE 一致)
_LINE_ID_SAFE_CHARS = re.compile(r"[^A-Za-z0-9._\-]")


def _qr_cache_dir() -> Path:
    """返回 QR 缓存目录, 不存在则创建."""
    d = Path(tempfile.gettempdir()) / _QR_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def normalize_line_id(raw: str) -> str:
    """归一化 line_id, 去 @ + lower + 去非法字符 (用于文件名).

    注意: 这只用于生成文件名 hash, 不改变写入 QR 的实际值.
    """
    s = (raw or "").strip()
    if not s:
        return ""
    # line.me URL 形式 → 取最后一段 (去前导 ~ / 查询串)
    if "line.me" in s.lower() or "line-apps.com" in s.lower():
        s = s.split("?", 1)[0].split("#", 1)[0].rstrip("/")
        last = s.rsplit("/", 1)[-1]
        s = last.lstrip("~")
    if s.startswith("@"):
        s = s[1:]
    s = _LINE_ID_SAFE_CHARS.sub("_", s).lower()
    return s[:64]  # 控制长度


def line_id_to_qr_url(line_id: str) -> str:
    """把 line_id 拼成 QR 用的 LINE 加好友 deep link.

    输入形式:
      * "@store123" → "https://line.me/R/ti/p/~store123"
      * "store123"  → "https://line.me/R/ti/p/~store123"
      * "https://line.me/R/ti/p/~store123" (已是完整 URL) → 原样返回
      * "https://line.me/ti/p/xxxx" (旧式深链)            → 原样返回
    """
    s = (raw := raw_strip(line_id))
    if not s:
        return ""
    if s.startswith("http://") or s.startswith("https://"):
        return s
    # 去 @
    if s.startswith("@"):
        s = s[1:]
    return f"https://line.me/R/ti/p/~{s}"


def raw_strip(s: str) -> str:
    """简单 strip helper, 单独抽出便于单测."""
    return (s or "").strip()


def build_line_qr(line_id: str,
                   *,
                   box_size: int = 10,
                   border: int = 2,
                   force_regen: bool = False) -> Optional[str]:
    """生成 LINE QR PNG, 返回文件路径; 失败返 None.

    Args:
        line_id: LINE ID 或完整 line.me URL
        box_size: QR 单元格像素, 默认 10 → 约 290x290 px
        border: 白边格子数, 默认 2
        force_regen: True 时忽略缓存, 强制重新生成 (调试用)

    Returns:
        PNG 绝对路径 (str). 异常时返 None, 调用方降级走文字 only.
    """
    if not line_id:
        return None

    norm = normalize_line_id(line_id)
    if not norm:
        logger.debug("[qr_generator] empty normalized line_id: %r", line_id)
        return None

    # 文件名 = norm + 内容 hash (避免不同 URL 形式撞文件名)
    qr_url = line_id_to_qr_url(line_id)
    if not qr_url:
        return None
    h = hashlib.sha256(qr_url.encode("utf-8")).hexdigest()[:10]
    fname = f"{norm}_{h}.png"
    out_path = _qr_cache_dir() / fname

    # 缓存命中检查
    if not force_regen and out_path.exists():
        try:
            mtime = out_path.stat().st_mtime
            if (time.time() - mtime) < _QR_CACHE_TTL_S:
                return str(out_path)
        except OSError:
            pass  # 异常 fall through 到重新生成

    # 重新生成
    try:
        import qrcode  # type: ignore
        img = qrcode.make(qr_url,
                            box_size=int(box_size),
                            border=int(border))
        img.save(str(out_path))
        logger.info("[qr_generator] generated %s → %s (url=%s)",
                     line_id, out_path.name, qr_url)
        return str(out_path)
    except Exception as e:
        logger.warning("[qr_generator] build_line_qr failed line_id=%r: %s",
                         line_id, e)
        return None


def cleanup_old_qrs(*, older_than_seconds: int = 7 * 24 * 3600) -> int:
    """清理 N 秒前生成的 QR 文件, 防止 temp 目录膨胀.

    Args:
        older_than_seconds: 默认 7 天

    Returns:
        删除文件数
    """
    try:
        d = _qr_cache_dir()
        threshold = time.time() - int(older_than_seconds)
        n_deleted = 0
        for p in d.iterdir():
            if not p.is_file() or not p.name.endswith(".png"):
                continue
            try:
                if p.stat().st_mtime < threshold:
                    p.unlink()
                    n_deleted += 1
            except OSError:
                continue
        return n_deleted
    except Exception as e:
        logger.debug("[qr_generator] cleanup failed: %s", e)
        return 0
