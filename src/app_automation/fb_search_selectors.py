# -*- coding: utf-8 -*-
"""
Facebook 搜索相关 uiautomator2 selector 字典（单源）。

供 ``facebook.FacebookAutomation``、``scripts/w0_capture_direct.py`` 共用，
避免 Home 顶栏 / 搜索页 / People 筛选 的 selector 在多处拷贝漂移。

更新任一列表后，两边行为自动对齐；注释里保留与 katana 版本的对应关系说明。
"""

from __future__ import annotations

from typing import Any, Dict, Tuple

# u2 的 kwargs 类型（便于类型检查与 IDE）
Selector = Dict[str, Any]

# ── 从 Home 打开「搜索页」：顶栏 Search（2026-04-23 katana 实测为 Button，非 EditText）──
FB_HOME_SEARCH_BUTTON_SELECTORS: Tuple[Selector, ...] = (
    {"className": "android.widget.Button", "description": "Search"},
    {"description": "Search", "clickable": True},
)

# ── ``_fallback_search_tap``：主循环全失败后的兜底 ─────────────────────
FB_FALLBACK_SEARCH_TAP_SELECTORS: Tuple[Selector, ...] = (
    {"description": "Search"},
    {"resourceId": "com.facebook.katana:id/search_bar_text_view"},
    {"resourceId": "com.facebook.katana:id/search_bar"},
)

# ── 已进入搜索/半展开界面时的补充尝试（W0 直连脚本合并遍历用）──────────
FB_SEARCH_SURFACE_EXTRA_SELECTORS: Tuple[Selector, ...] = (
    {"resourceId": "com.facebook.katana:id/search_query_text_view"},
    {"resourceId": "com.facebook.katana:id/search_bar_text_view"},
    {"resourceId": "com.facebook.katana:id/search_bar"},
    {"resourceId": "com.facebook.katana:id/search_button"},
    {"className": "android.widget.EditText", "description": "Search Facebook"},
)

# ── ``search_people``：在搜索页内向 EditText 写入 query（set_text）────
# 2026-04-24 简化: 前两个 selector 在新版 FB katana 永远 0 candidates —
#   resource-id 被混淆成 "(name removed)",
#   EditText 实际 content-desc 为空 (hint/text 都是 'Search', 不是 'Search Facebook').
# 反而 poll 期间偶现假阳性匹配到错 EditText → set_text 无效位置导致搜索失败.
# 实测只用 {className: EditText} 单字段稳定 work (搜索页只有 1 个顶部 EditText).
FB_SEARCH_QUERY_EDITOR_SELECTORS: Tuple[Selector, ...] = (
    {"className": "android.widget.EditText"},
)

# ── ``search_people``：People 筛选条 ───────────────────────────────────
FB_PEOPLE_TAB_SELECTORS: Tuple[Selector, ...] = (
    {"descriptionContains": "People search results"},
    {"text": "People"},
    # 日文等界面可能只有 content-desc 含 People，无英文精确 text
    {"descriptionContains": "People"},
)
