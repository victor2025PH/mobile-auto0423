# -*- coding: utf-8 -*-
"""
Facebook 搜索路径 — hierarchy 子串与启动弹窗文案（单源）。

供 ``FacebookAutomation._tap_search_bar_preferred`` 等使用，避免 Home / 搜索页 /
Messenger 误判字符串散落在 ``facebook.py`` 内难以同步。
"""

from __future__ import annotations

from typing import Final, Tuple

# ── Home Feed（出现任一即视为在 Home）────────────────────────────────
FB_WHATS_ON_MIND: Final[str] = "What's on your mind?"
FB_HOME_TAB_MARKER: Final[str] = "Home, tab 1 of"
FB_FEED_LIST_MARKER: Final[str] = "id/feed_list"

# 发帖框提示语（多语言 katana 常见文案，与英文逻辑并列；按需再扩）
FB_HOME_COMPOSER_I18N_SUBSTRINGS: Tuple[str, ...] = (
    "A cosa stai pensando?",       # IT
    "\u00bfEn qu\u00e9 est\u00e1s pensando?",  # ES: ¿En qué estás pensando?
    "\u4eca\u306a\u306b\u3057\u3066\u308b\uff1f",  # JP: 今なにしてる？
    "\u4f60\u5728\u60f3\u4ec0\u4e48\uff1f",       # zh-Hans: 你在想什么？
)

# ── 搜索页（顶栏搜索：无发帖框 + 存在 EditText）────────────────────────
FB_EDITTEXT_CLASS_IN_XML: Final[str] = 'class="android.widget.EditText"'

# ── Messenger / Chats 误点检测 ─────────────────────────────────────────
FB_GET_MESSENGER_BANNER: Final[str] = "Get Messenger"
FB_CHATS_FRAGMENT_ID: Final[str] = "id/chats_fragment"
FB_MESSAGES_CALLS_SNIPPET: Final[str] = "Messages and calls are"

# ── 冷启动后 ``_force_back_to_home`` 内快速点掉的常见按钮文案 ─────────
FB_STARTUP_DISMISS_TARGET_TEXTS: Tuple[str, ...] = (
    "Not Now", "Skip", "Maybe Later", "OK", "Got it",
    "Continue", "Close", "Dismiss", "Cancel",
    "Allow", "While using the app", "Later",
)


def hierarchy_looks_like_fb_home(xml: str) -> bool:
    """当前 dump 是否像 FB Home Feed（宽松启发式）。"""
    if not xml:
        return False
    if (
        FB_WHATS_ON_MIND in xml
        or FB_HOME_TAB_MARKER in xml
        or FB_FEED_LIST_MARKER in xml
    ):
        return True
    return any(s in xml for s in FB_HOME_COMPOSER_I18N_SUBSTRINGS)


def hierarchy_looks_like_fb_search_surface(xml: str) -> bool:
    """是否像「已进入搜索输入界面」（非 Feed）。与 katana 新版 placeholder 对齐。"""
    if not xml:
        return False
    if FB_WHATS_ON_MIND in xml:
        return False
    if FB_EDITTEXT_CLASS_IN_XML not in xml:
        return False
    return True


def hierarchy_looks_like_messenger_or_chats(xml: str) -> bool:
    """是否误在 Messenger / Chats 等需先 back 的界面。"""
    if not xml:
        return False
    return (
        FB_GET_MESSENGER_BANNER in xml
        or FB_CHATS_FRAGMENT_ID in xml
        or (xml.count(FB_MESSAGES_CALLS_SNIPPET) >= 1)
    )
