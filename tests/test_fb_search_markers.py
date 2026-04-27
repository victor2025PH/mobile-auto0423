# -*- coding: utf-8 -*-
"""
fb_search_markers — Home / Search / Messenger 识别器单测.

P2 (2026-04-27): 圈层拓客真机重试暴露 FB Home 识别盲区, 加 marker 后须有
回归保护, 防未来 PR 误删 marker.
"""

from __future__ import annotations

from src.app_automation.fb_search_markers import (
    FB_FEED_STORY_ROOT_RID,
    FB_HOME_COMPOSER_I18N_SUBSTRINGS,
    FB_NEWS_FEED_RID,
    hierarchy_looks_like_fb_home,
    hierarchy_looks_like_fb_search_surface,
    hierarchy_looks_like_messenger_or_chats,
)


# ── Home 识别 ──────────────────────────────────────────────────────────

def test_english_whats_on_mind_recognized():
    xml = '<node text="What\'s on your mind?"/>'
    assert hierarchy_looks_like_fb_home(xml)


def test_japanese_legacy_placeholder_recognized():
    """旧版日语 placeholder '今なにしてる？' 仍识别为 home."""
    xml = "<node text='今なにしてる？'/>"
    assert hierarchy_looks_like_fb_home(xml)


def test_japanese_ab_new_placeholder_recognized():
    """2026-04-27 加 — IJ8HZLOR 真机暴露 FB A/B 新版日语 placeholder.

    旧 marker 不识别此文案 → _ensure_fb_home_ready_strict 死等超时,
    本测试防回归.
    """
    xml = "<node text='その気持ち、シェアしよう'/>"
    assert hierarchy_looks_like_fb_home(xml)


def test_zh_hans_placeholder_recognized():
    xml = "<node text='你在想什么？'/>"
    assert hierarchy_looks_like_fb_home(xml)


def test_news_feed_rid_recognized():
    """跨语言稳定 RID — 部分 ROM/账号下 placeholder 延迟时, RID 是更可靠的 marker."""
    xml = '<node resource-id="com.facebook.katana:id/news_feed"/>'
    assert hierarchy_looks_like_fb_home(xml)


def test_feed_story_root_rid_recognized():
    xml = '<node resource-id="com.facebook.katana:id/feed_story_root"/>'
    assert hierarchy_looks_like_fb_home(xml)


def test_home_tab_marker_recognized():
    xml = '<node content-desc="Home, tab 1 of 6"/>'
    assert hierarchy_looks_like_fb_home(xml)


def test_home_tab_marker_zh_recognized():
    xml = '<node content-desc="首页，第1/6个选项卡"/>'
    assert hierarchy_looks_like_fb_home(xml)


def test_empty_xml_not_home():
    assert not hierarchy_looks_like_fb_home("")


def test_arbitrary_xml_not_home():
    assert not hierarchy_looks_like_fb_home('<node text="random content"/>')


# ── Home / 搜索页 / Messenger 互斥性 ─────────────────────────────────────

def test_search_surface_only_not_misclassified_as_home():
    """纯搜索页 (EditText, 无 'What's on your mind?') 不能被误判为 home."""
    xml = '<node class="android.widget.EditText"/>'
    assert hierarchy_looks_like_fb_search_surface(xml)
    assert not hierarchy_looks_like_fb_home(xml)


def test_search_surface_with_whats_on_mind_is_home_not_search():
    """home 也常含 EditText (搜索按钮), 但只要看到 'What's on your mind?' 就是 home."""
    xml = '<node text="What\'s on your mind?"/><node class="android.widget.EditText"/>'
    assert hierarchy_looks_like_fb_home(xml)
    # search_surface 的判断要求 NO 'What's on your mind?', 故应 False
    assert not hierarchy_looks_like_fb_search_surface(xml)


def test_messenger_get_banner_does_not_match_home():
    xml = '<node text="Get Messenger"/>'
    assert hierarchy_looks_like_messenger_or_chats(xml)
    assert not hierarchy_looks_like_fb_home(xml)


def test_chats_fragment_does_not_match_home():
    xml = '<node resource-id="com.facebook.katana:id/chats_fragment"/>'
    assert hierarchy_looks_like_messenger_or_chats(xml)
    assert not hierarchy_looks_like_fb_home(xml)


# ── 常量稳定性 (防 PR 误删) ──────────────────────────────────────────

def test_jp_ab_new_placeholder_in_substrings_tuple():
    """防回归: 'その気持ち、シェアしよう' 必须在 i18n substrings tuple 里."""
    assert "その気持ち、シェアしよう" in FB_HOME_COMPOSER_I18N_SUBSTRINGS


def test_jp_legacy_placeholder_in_substrings_tuple():
    assert "今なにしてる？" in FB_HOME_COMPOSER_I18N_SUBSTRINGS


def test_resource_id_constants_well_formed():
    """RID 常量必须是 'id/...' 子串形式 (匹配 dump_hierarchy 的 resource-id 属性值)."""
    assert FB_NEWS_FEED_RID.startswith("id/")
    assert FB_FEED_STORY_ROOT_RID.startswith("id/")
