# -*- coding: utf-8 -*-
"""端到端壁纸校验单测 — 锁定 _get_wallpaper_id / _verify_wallpaper_changed 解析。

历史背景：旧 _try_helper_apk_wallpaper 用 `am broadcast --async` 派发，主机收到 ADB
"成功"就 return True，但 Receiver 内部 BitmapFactory.decodeFile 因权限缺失返回 null
（result=3）也被记为成功 → wallpaper_error 被错误清除 → dashboard 假显健康。
现以 dumpsys wallpaper 的 monotonic id 作端到端铁证。本测试锁定解析正确性，
防 Android 版本/多 user/多 display 输出差异引入回归。
"""
from unittest.mock import MagicMock

from src.utils.wallpaper_generator import (
    _get_wallpaper_id,
    _verify_wallpaper_changed,
)


def _mock_manager(stdout: str, ok: bool = True):
    m = MagicMock()
    m._run_adb = MagicMock(return_value=(ok, stdout))
    return m


SAMPLE_FULL = """\
mDefaultWallpaperComponent=ComponentInfo{com.miui.miwallpaper/.ImageWallpaper}
mImageWallpaper=ComponentInfo{com.miui.miwallpaper/.ImageWallpaper}
System wallpaper state:
 User 0: id=2
 Display state:
  displayId=0
  mWidth=1600  mHeight=1600
Lock wallpaper state:
 User 0: id=3
  mCropHint=Rect(0, 0 - 0, 0)
Fallback wallpaper state:
 User 0: id=1
  mCropHint=Rect(0, 0 - 0, 0)
"""

SAMPLE_SYSTEM_ONLY = """\
System wallpaper state:
 User 0: id=9
  mWidth=720  mHeight=1600
Fallback wallpaper state:
 User 0: id=1
"""

SAMPLE_MULTI_USER = """\
System wallpaper state:
 User 0: id=5
 User 10: id=2
Lock wallpaper state:
 User 0: id=7
"""


def test_get_wallpaper_id_full_parse():
    mgr = _mock_manager(SAMPLE_FULL)
    assert _get_wallpaper_id(mgr, "X") == (2, 3)


def test_get_wallpaper_id_system_only_lock_none():
    """root method 仅设 system wallpaper, lock 段缺失时 lock_id=None。"""
    mgr = _mock_manager(SAMPLE_SYSTEM_ONLY)
    sys_id, lock_id = _get_wallpaper_id(mgr, "X")
    assert sys_id == 9
    assert lock_id is None


def test_get_wallpaper_id_picks_first_user_per_section():
    """多 user 场景只取每段第一个 User 0 行（user 0 是主用户）。"""
    mgr = _mock_manager(SAMPLE_MULTI_USER)
    assert _get_wallpaper_id(mgr, "X") == (5, 7)


def test_get_wallpaper_id_adb_fail():
    """ADB 失败返回 (None, None), 上层应 skip verify 而不是误判失败。"""
    mgr = _mock_manager("", ok=False)
    assert _get_wallpaper_id(mgr, "X") == (None, None)


def test_verify_wallpaper_changed_system_id_growth():
    mgr = _mock_manager(SAMPLE_FULL)  # after: sys=2 lock=3
    assert _verify_wallpaper_changed(mgr, "X", before=(1, 2)) is True


def test_verify_wallpaper_changed_lock_only_growth():
    """root 路径只设 system, helper APK 只设 lock 等部分场景, 任一 id 涨即真成功。"""
    mgr = _mock_manager(SAMPLE_FULL)  # after: sys=2 lock=3
    assert _verify_wallpaper_changed(mgr, "X", before=(2, 1)) is True


def test_verify_wallpaper_changed_no_growth_is_failure():
    """sys 和 lock 都没涨 = 假成功, 必须返回 False 让 fallback 接力。"""
    mgr = _mock_manager(SAMPLE_FULL)  # after: sys=2 lock=3
    assert _verify_wallpaper_changed(mgr, "X", before=(2, 3)) is False


def test_verify_wallpaper_changed_baseline_unreadable_lenient():
    """baseline 读不到时宽松放行, 不阻塞合法部署。"""
    mgr = _mock_manager(SAMPLE_FULL)
    assert _verify_wallpaper_changed(mgr, "X", before=(None, None)) is True


def test_verify_wallpaper_changed_after_unreadable_lenient():
    """after 读不到时宽松放行 (dumpsys 偶发失败不应误判合法部署)。"""
    mgr = _mock_manager("", ok=False)
    assert _verify_wallpaper_changed(mgr, "X", before=(1, 1)) is True
