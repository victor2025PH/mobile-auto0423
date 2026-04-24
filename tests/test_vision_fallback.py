# -*- coding: utf-8 -*-
"""`VisionFallback` — invalidate API + cache 基础逻辑。

`find_element` 的 LLM call 不测 (真 LLM 或 mock 复杂), 只测:
  * `invalidate(target, context)` 清 cache
  * `_cache_key` 稳定 (same inputs → same key)
  * cache TTL 逻辑 (set 后命中, 超时后 miss)
  * budget counting
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest


def _make_vf(hourly_budget=20, cache_ttl_sec=300.0):
    from src.ai.vision_fallback import VisionFallback, VisionConfig
    client = MagicMock()
    return VisionFallback(
        client=client,
        config=VisionConfig(hourly_budget=hourly_budget,
                            cache_ttl_sec=cache_ttl_sec),
    )


class TestInvalidate:
    """`invalidate(target, context)` API — 2026-04-24 新增 for Level 4 VLM
    click-verify fail 场景。"""

    def test_invalidate_removes_cached_entry(self):
        from src.ai.vision_fallback import VisionResult
        vf = _make_vf()
        # 直接写 cache (绕开 find_element)
        key = vf._cache_key("search bar", "ctx")
        vf._set_cache(key, VisionResult(coordinates=(100, 200)))
        assert vf._get_cache(key) is not None
        assert vf.invalidate("search bar", "ctx") is True
        assert vf._get_cache(key) is None

    def test_invalidate_returns_false_when_not_cached(self):
        vf = _make_vf()
        assert vf.invalidate("never-cached", "") is False

    def test_invalidate_precise_match_only(self):
        """不同 target 的 cache 不互相干扰。"""
        from src.ai.vision_fallback import VisionResult
        vf = _make_vf()
        vf._set_cache(
            vf._cache_key("A", "ctx1"), VisionResult(coordinates=(1, 1)))
        vf._set_cache(
            vf._cache_key("B", "ctx2"), VisionResult(coordinates=(2, 2)))
        assert vf.invalidate("A", "ctx1") is True
        # B 保留
        assert vf._get_cache(vf._cache_key("B", "ctx2")) is not None
        # A 已空
        assert vf._get_cache(vf._cache_key("A", "ctx1")) is None

    def test_invalidate_default_context(self):
        """context 省略默认为 ''"""
        from src.ai.vision_fallback import VisionResult
        vf = _make_vf()
        vf._set_cache(
            vf._cache_key("target", ""), VisionResult(coordinates=(5, 5)))
        assert vf.invalidate("target") is True


class TestCacheKey:
    def test_same_inputs_same_key(self):
        vf = _make_vf()
        k1 = vf._cache_key("search", "context1")
        k2 = vf._cache_key("search", "context1")
        assert k1 == k2

    def test_different_target_different_key(self):
        vf = _make_vf()
        assert vf._cache_key("A", "ctx") != vf._cache_key("B", "ctx")

    def test_different_context_different_key(self):
        vf = _make_vf()
        assert vf._cache_key("A", "ctx1") != vf._cache_key("A", "ctx2")


class TestCacheTTL:
    def test_cache_hit_within_ttl(self):
        from src.ai.vision_fallback import VisionResult
        vf = _make_vf(cache_ttl_sec=300.0)
        key = vf._cache_key("t", "c")
        vf._set_cache(key, VisionResult(coordinates=(1, 2)))
        assert vf._get_cache(key) is not None

    def test_cache_miss_after_ttl_expired(self):
        from src.ai.vision_fallback import VisionResult
        vf = _make_vf(cache_ttl_sec=0.01)
        key = vf._cache_key("t", "c")
        vf._set_cache(key, VisionResult(coordinates=(1, 2)))
        time.sleep(0.05)
        assert vf._get_cache(key) is None


class TestBudget:
    def test_record_call_counts(self):
        vf = _make_vf(hourly_budget=20)
        assert vf.budget_remaining == 20
        vf._record_call()
        vf._record_call()
        assert vf.budget_remaining == 18
        assert vf._check_budget() is True

    def test_budget_exhausted(self):
        vf = _make_vf(hourly_budget=2)
        vf._record_call()
        vf._record_call()
        assert vf.budget_remaining == 0
        assert vf._check_budget() is False

    def test_stats_shape(self):
        vf = _make_vf(hourly_budget=10)
        vf._record_call()
        s = vf.stats()
        assert s["hourly_used"] == 1
        assert s["hourly_budget"] == 10
        assert s["budget_remaining"] == 9
        assert "cache_size" in s
