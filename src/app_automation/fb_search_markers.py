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
# 2026-04-24 中文 katana bottom tab content-desc: '首页，第1/6个选项卡'
FB_HOME_TAB_MARKER_ZH: Final[str] = "首页，第1"

# 2026-04-27 加 — IJ8HZLOR 圈层拓客真机重试发现 FB Home 跨语言稳定 resource-id.
# 这两个 RID 跨所有语言 (日/英/中) 都存在, 比 placeholder text 更可靠;
# 部分 ROM/账号下 placeholder 可能延迟出现 / A/B 替换文案, RID 是 home feed
# 容器层标识, 出现即等于"home feed 容器已 mount". (从 _ensure_fb_home_ready_strict
# 内嵌 fallback marker 提到此处, 让所有调用 hierarchy_looks_like_fb_home 的地方
# 同步受益.)
FB_NEWS_FEED_RID: Final[str] = "id/news_feed"
FB_FEED_STORY_ROOT_RID: Final[str] = "id/feed_story_root"

# 发帖框提示语（多语言 katana 常见文案，与英文逻辑并列；按需再扩）
FB_HOME_COMPOSER_I18N_SUBSTRINGS: Tuple[str, ...] = (
    "A cosa stai pensando?",       # IT
    "¿En qué estás pensando?",  # ES: ¿En qué estás pensando?
    "今なにしてる？",  # JP: 今なにしてる？(旧版)
    # 2026-04-27 — IJ8HZLOR 圈层拓客真机重试暴露 FB A/B 新版日语 home placeholder.
    # 旧 marker "今なにしてる？" 不识别 → _ensure_fb_home_ready_strict 死等超时.
    "その気持ち、シェアしよう",  # JP: その気持ち、シェアしよう (A/B 新版)
    "你在想什么？",       # zh-Hans: 你在想什么？
    "在想些什么？",        # zh-Hans alt
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
        or FB_HOME_TAB_MARKER_ZH in xml
        or FB_FEED_LIST_MARKER in xml
        or FB_NEWS_FEED_RID in xml
        or FB_FEED_STORY_ROOT_RID in xml
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
