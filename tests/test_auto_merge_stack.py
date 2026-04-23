# -*- coding: utf-8 -*-
"""`scripts/auto_merge_stack.py` 单元测试 — 纯逻辑 + API mock。

真的 apply_merges 会动 GitHub 分支, 不测, 用 `--apply` 手动验证。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "auto_merge_stack.py"


# ─── CLI 参数保护 ──────────────────────────────────────────────────────────

class TestCli:
    def test_help_exits_0(self):
        """--help 不打网络, 秒退, 能打印 description。"""
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "--help"],
            capture_output=True, text=True, timeout=30,
        )
        assert r.returncode == 0
        assert "自动合并" in r.stdout or "auto" in r.stdout.lower()


# ─── _parse_ids ──────────────────────────────────────────────────────────────

class TestParseIds:
    def test_basic(self):
        from scripts.auto_merge_stack import _parse_ids
        assert _parse_ids("1,2,3") == {1, 2, 3}

    def test_with_spaces(self):
        from scripts.auto_merge_stack import _parse_ids
        assert _parse_ids(" 10 , 20 ") == {10, 20}

    def test_skips_non_int(self):
        from scripts.auto_merge_stack import _parse_ids
        assert _parse_ids("1,foo,3") == {1, 3}

    def test_empty_tokens_ignored(self):
        from scripts.auto_merge_stack import _parse_ids
        assert _parse_ids(",,1,") == {1}


# ─── MergePlan dataclass ───────────────────────────────────────────────────

class TestMergePlan:
    def test_default_state(self):
        from scripts.auto_merge_stack import MergePlan
        p = MergePlan(pr_number=1, branch="feat-b-x", base="main")
        assert p.status == "pending"
        assert p.approved_by_a is False
        assert p.has_changes_requested is False

    def test_to_dict_roundtrip(self):
        from scripts.auto_merge_stack import MergePlan
        p = MergePlan(pr_number=42, branch="b", base="main",
                       latest_review_state="APPROVED", merge_sha="abc123")
        d = p.to_dict()
        assert d["pr_number"] == 42
        assert d["merge_sha"] == "abc123"


# ─── collect_plans 拓扑排序 ──────────────────────────────────────────────────

class TestCollectPlans:
    def _pr(self, num, head, base, title="t"):
        return {"number": num, "title": title,
                "head": {"ref": head}, "base": {"ref": base}}

    def test_empty(self):
        from scripts.auto_merge_stack import collect_plans
        with patch("scripts.auto_merge_stack.github_api", return_value=[]):
            assert collect_plans("fake") == []

    def test_base_main_first(self):
        from scripts.auto_merge_stack import collect_plans
        prs = [
            self._pr(3, "feat-b-p4", "feat-b-p3"),
            self._pr(1, "feat-b-p3", "main"),
            self._pr(2, "feat-b-other", "main"),
        ]
        with patch("scripts.auto_merge_stack.github_api",
                   return_value=prs):
            plans = collect_plans("t")
        # p3 和 p-other 都 base=main → 在前 (顺序任意), p4 在后
        idx = {p.branch: i for i, p in enumerate(plans)}
        assert idx["feat-b-p3"] < idx["feat-b-p4"]

    def test_only_filter(self):
        from scripts.auto_merge_stack import collect_plans
        prs = [
            self._pr(1, "feat-b-a", "main"),
            self._pr(2, "feat-b-b", "main"),
            self._pr(3, "feat-b-c", "main"),
        ]
        with patch("scripts.auto_merge_stack.github_api",
                   return_value=prs):
            plans = collect_plans("t", only_prs={1, 3})
        assert {p.pr_number for p in plans} == {1, 3}

    def test_orphan_base_placed_end(self):
        from scripts.auto_merge_stack import collect_plans
        prs = [
            self._pr(1, "feat-b-a", "main"),
            self._pr(2, "feat-b-b", "feat-b-already-merged"),
        ]
        with patch("scripts.auto_merge_stack.github_api",
                   return_value=prs):
            plans = collect_plans("t")
        assert plans[0].branch == "feat-b-a"
        assert plans[1].branch == "feat-b-b"

    def test_non_b_filtered(self):
        from scripts.auto_merge_stack import collect_plans
        prs = [
            self._pr(1, "feat-b-x", "main"),
            self._pr(2, "feat-a-phase3", "main"),
        ]
        with patch("scripts.auto_merge_stack.github_api",
                   return_value=prs):
            plans = collect_plans("t", head_prefix="feat-b-")
        assert len(plans) == 1
        assert plans[0].branch == "feat-b-x"


# ─── check_readiness ────────────────────────────────────────────────────────

class TestCheckReadiness:
    def _pr(self, state="open", mergeable=True,
             mergeable_state="clean", base="main"):
        return {"number": 10, "title": "x", "state": state,
                "mergeable": mergeable, "mergeable_state": mergeable_state,
                "head": {"ref": "feat-b-x"}, "base": {"ref": base}}

    def _api_factory(self, pr_meta, reviews):
        def fake_api(path, token, method="GET", body=None):
            if path.endswith("/reviews"):
                return reviews
            return pr_meta
        return fake_api

    def test_ready_when_approved_and_clean(self):
        from scripts.auto_merge_stack import MergePlan, check_readiness
        plan = MergePlan(pr_number=10, branch="feat-b-x", base="main")
        reviews = [{"state": "APPROVED",
                     "submitted_at": "2026-04-24T12:00:00Z",
                     "user": {"login": "a"}}]
        with patch("scripts.auto_merge_stack.github_api",
                   side_effect=self._api_factory(self._pr(), reviews)):
            check_readiness(plan, "t", merged_branches=set())
        assert plan.status == "ready"
        assert plan.approved_by_a is True

    def test_blocked_when_changes_requested(self):
        from scripts.auto_merge_stack import MergePlan, check_readiness
        plan = MergePlan(pr_number=10, branch="feat-b-x", base="main")
        reviews = [
            {"state": "APPROVED",
              "submitted_at": "2026-04-23T10:00:00Z",
              "user": {"login": "a"}},
            {"state": "CHANGES_REQUESTED",
              "submitted_at": "2026-04-24T12:00:00Z",  # 更新
              "user": {"login": "a"}},
        ]
        with patch("scripts.auto_merge_stack.github_api",
                   side_effect=self._api_factory(self._pr(), reviews)):
            check_readiness(plan, "t", merged_branches=set())
        assert plan.status == "blocked"
        assert plan.has_changes_requested is True
        assert "CHANGES_REQUESTED" in plan.error

    def test_blocked_when_not_approved(self):
        from scripts.auto_merge_stack import MergePlan, check_readiness
        plan = MergePlan(pr_number=10, branch="feat-b-x", base="main")
        with patch("scripts.auto_merge_stack.github_api",
                   side_effect=self._api_factory(self._pr(), [])):
            check_readiness(plan, "t", merged_branches=set())
        assert plan.status == "blocked"
        assert "APPROVED" in plan.error

    def test_skipped_when_pr_closed(self):
        from scripts.auto_merge_stack import MergePlan, check_readiness
        plan = MergePlan(pr_number=10, branch="feat-b-x", base="main")
        with patch("scripts.auto_merge_stack.github_api",
                   side_effect=self._api_factory(
                       self._pr(state="closed"), [])):
            check_readiness(plan, "t", merged_branches=set())
        assert plan.status == "skipped"

    def test_blocked_when_dirty(self):
        from scripts.auto_merge_stack import MergePlan, check_readiness
        plan = MergePlan(pr_number=10, branch="feat-b-x", base="main")
        reviews = [{"state": "APPROVED",
                     "submitted_at": "2026-04-24T12:00:00Z",
                     "user": {"login": "a"}}]
        with patch("scripts.auto_merge_stack.github_api",
                   side_effect=self._api_factory(
                       self._pr(mergeable_state="dirty"), reviews)):
            check_readiness(plan, "t", merged_branches=set())
        assert plan.status == "blocked"
        assert "dirty" in plan.error

    def test_retarget_set_when_parent_merged(self):
        """base 在 merged_branches → 标 retarget_base_to=main。"""
        from scripts.auto_merge_stack import MergePlan, check_readiness
        plan = MergePlan(pr_number=10, branch="feat-b-p4",
                          base="feat-b-p3")
        reviews = [{"state": "APPROVED",
                     "submitted_at": "2026-04-24T12:00:00Z",
                     "user": {"login": "a"}}]
        with patch("scripts.auto_merge_stack.github_api",
                   side_effect=self._api_factory(
                       self._pr(base="feat-b-p3"), reviews)):
            check_readiness(plan, "t",
                             merged_branches={"feat-b-p3"})
        assert plan.retarget_base_to == "main"
        assert plan.status == "ready"

    def test_commented_with_approve_marker_is_approved(self):
        """A 的 approve-equivalent COMMENTED 识别为 APPROVED (GitHub 不让自审)。"""
        from scripts.auto_merge_stack import MergePlan, check_readiness
        plan = MergePlan(pr_number=10, branch="feat-b-x", base="main")
        reviews = [{
            "state": "COMMENTED",
            "submitted_at": "2026-04-23T21:17:24Z",
            "user": {"login": "victor2025PH"},
            "body": "## ✅ A 侧 review 通过 (approve-equivalent)\n\n..."
        }]
        with patch("scripts.auto_merge_stack.github_api",
                   side_effect=self._api_factory(self._pr(), reviews)):
            check_readiness(plan, "t", merged_branches=set())
        assert plan.approved_by_a is True
        assert plan.status == "ready"

    def test_commented_without_marker_blocks(self):
        from scripts.auto_merge_stack import MergePlan, check_readiness
        plan = MergePlan(pr_number=10, branch="feat-b-x", base="main")
        reviews = [{
            "state": "COMMENTED",
            "submitted_at": "2026-04-23T21:17:24Z",
            "user": {"login": "other"},
            "body": "looks interesting"
        }]
        with patch("scripts.auto_merge_stack.github_api",
                   side_effect=self._api_factory(self._pr(), reviews)):
            check_readiness(plan, "t", merged_branches=set())
        assert plan.approved_by_a is False
        assert plan.status == "blocked"

    def test_is_approve_equivalent_pure(self):
        from scripts.auto_merge_stack import _is_approve_equivalent
        assert _is_approve_equivalent({"state": "APPROVED"}) is True
        assert _is_approve_equivalent({
            "state": "COMMENTED",
            "body": "## ✅ A 侧 review 通过 (approve-equivalent)",
        }) is True
        assert _is_approve_equivalent({
            "state": "COMMENTED", "body": "nope"}) is False
        assert _is_approve_equivalent({
            "state": "CHANGES_REQUESTED",
            "body": "approve-equivalent jk"}) is False

    def test_dismissed_review_not_counted_as_approved(self):
        """DISMISSED review 不当 APPROVED。"""
        from scripts.auto_merge_stack import MergePlan, check_readiness
        plan = MergePlan(pr_number=10, branch="feat-b-x", base="main")
        reviews = [
            {"state": "APPROVED",
              "submitted_at": "2026-04-23T10:00:00Z",
              "user": {"login": "a"}},
            {"state": "DISMISSED",
              "submitted_at": "2026-04-24T12:00:00Z",
              "user": {"login": "a"}},
        ]
        with patch("scripts.auto_merge_stack.github_api",
                   side_effect=self._api_factory(self._pr(), reviews)):
            check_readiness(plan, "t", merged_branches=set())
        # latest 是 DISMISSED → approved_users 空
        assert plan.approved_by_a is False
        assert plan.status == "blocked"


# ─── render ────────────────────────────────────────────────────────────────

class TestRender:
    def test_empty(self):
        from scripts.auto_merge_stack import render
        txt = render([], applied=False)
        assert "无匹配" in txt

    def test_ready_row(self):
        from scripts.auto_merge_stack import MergePlan, render
        p = MergePlan(pr_number=10, branch="feat-b-x", base="main",
                       title="feat x", status="ready",
                       approved_by_a=True, mergeable_state="clean")
        txt = render([p], applied=False)
        assert "PR#10" in txt
        assert "ready" in txt
        assert "feat-b-x" in txt
        assert "APPROVED" in txt

    def test_blocked_shows_error(self):
        from scripts.auto_merge_stack import MergePlan, render
        p = MergePlan(pr_number=10, branch="feat-b-x", base="main",
                       status="blocked", error="未 APPROVED")
        txt = render([p], applied=False)
        assert "blocked" in txt
        assert "APPROVED" in txt

    def test_merged_shows_sha(self):
        from scripts.auto_merge_stack import MergePlan, render
        p = MergePlan(pr_number=10, branch="feat-b-x", base="main",
                       status="merged", merge_sha="abc12345ef")
        txt = render([p], applied=True)
        assert "merged" in txt
        assert "abc12345" in txt

    def test_retarget_indicator_shown(self):
        from scripts.auto_merge_stack import MergePlan, render
        p = MergePlan(pr_number=10, branch="feat-b-p4",
                       base="feat-b-p3", status="ready",
                       approved_by_a=True, mergeable_state="clean",
                       retarget_base_to="main")
        txt = render([p], applied=False)
        assert "base→main" in txt or "base" in txt

    def test_summary_counts(self):
        from scripts.auto_merge_stack import MergePlan, render
        plans = [
            MergePlan(pr_number=1, branch="a", base="main", status="ready",
                       approved_by_a=True, mergeable_state="clean"),
            MergePlan(pr_number=2, branch="b", base="main", status="ready",
                       approved_by_a=True, mergeable_state="clean"),
            MergePlan(pr_number=3, branch="c", base="main", status="blocked",
                       error="x"),
        ]
        txt = render(plans, applied=False)
        assert "ready" in txt
        assert "blocked" in txt


# ─── apply_merges (核心合并逻辑, 全部 mock API) ──────────────────────────────

class TestApplyMerges:
    def test_happy_path_two_prs(self):
        """两个 PR base=main 全绿 → 都 merged。"""
        from scripts.auto_merge_stack import MergePlan, apply_merges

        plan1 = MergePlan(pr_number=1, branch="feat-b-a", base="main")
        plan2 = MergePlan(pr_number=2, branch="feat-b-b", base="main")

        pr_meta_template = {
            "state": "open", "mergeable": True,
            "mergeable_state": "clean",
            "head": {"ref": "x"}, "base": {"ref": "main"},
            "title": "t",
        }
        reviews = [{"state": "APPROVED",
                     "submitted_at": "2026-04-24T12:00:00Z",
                     "user": {"login": "a"}}]

        def fake_api(path, token, method="GET", body=None):
            if "/merge" in path and method == "PUT":
                return {"merged": True, "sha": f"merged-{path}"}
            if path.endswith("/reviews"):
                return reviews
            if "/pulls/" in path:
                return pr_meta_template
            return []

        with patch("scripts.auto_merge_stack.github_api",
                   side_effect=fake_api), \
             patch("scripts.auto_merge_stack.git_fetch_main",
                   return_value=True):
            r = apply_merges([plan1, plan2], "t",
                              merge_method="merge",
                              continue_on_error=False)
        assert r[0].status == "merged"
        assert r[1].status == "merged"

    def test_halts_on_blocked(self):
        """第一个 blocked, 第二个应 skipped。"""
        from scripts.auto_merge_stack import MergePlan, apply_merges

        plan1 = MergePlan(pr_number=1, branch="feat-b-a", base="main")
        plan2 = MergePlan(pr_number=2, branch="feat-b-b", base="main")

        def fake_api(path, token, method="GET", body=None):
            if path.endswith("/reviews"):
                return []  # 没 approval
            if "/pulls/" in path:
                return {"state": "open", "mergeable": True,
                        "mergeable_state": "clean",
                        "head": {"ref": "x"}, "base": {"ref": "main"},
                        "title": "t"}
            return []

        with patch("scripts.auto_merge_stack.github_api",
                   side_effect=fake_api), \
             patch("scripts.auto_merge_stack.git_fetch_main",
                   return_value=True):
            r = apply_merges([plan1, plan2], "t",
                              merge_method="merge",
                              continue_on_error=False)
        assert r[0].status == "blocked"
        assert r[1].status == "skipped"
        assert "halted" in r[1].error

    def test_continue_on_error_keeps_trying(self):
        from scripts.auto_merge_stack import MergePlan, apply_merges

        plan1 = MergePlan(pr_number=1, branch="feat-b-a", base="main")
        plan2 = MergePlan(pr_number=2, branch="feat-b-b", base="main")

        # plan1 blocked (no approval), plan2 approved clean → merged
        reviews_by_pr = {1: [], 2: [{
            "state": "APPROVED",
            "submitted_at": "2026-04-24T12:00:00Z",
            "user": {"login": "a"},
        }]}

        def fake_api(path, token, method="GET", body=None):
            if "/merge" in path and method == "PUT":
                return {"merged": True, "sha": "ok"}
            if path.endswith("/reviews"):
                pr_num = int(path.split("/pulls/")[1].split("/")[0])
                return reviews_by_pr.get(pr_num, [])
            if "/pulls/" in path:
                return {"state": "open", "mergeable": True,
                        "mergeable_state": "clean",
                        "head": {"ref": "x"}, "base": {"ref": "main"},
                        "title": "t"}
            return []

        with patch("scripts.auto_merge_stack.github_api",
                   side_effect=fake_api), \
             patch("scripts.auto_merge_stack.git_fetch_main",
                   return_value=True):
            r = apply_merges([plan1, plan2], "t",
                              merge_method="merge",
                              continue_on_error=True)
        assert r[0].status == "blocked"
        assert r[1].status == "merged"
