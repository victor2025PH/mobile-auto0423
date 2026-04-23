# -*- coding: utf-8 -*-
"""`scripts/check_a_activity.py` 单元测试 — 纯逻辑 + git/API mock。"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "check_a_activity.py"


# ─── CLI ─────────────────────────────────────────────────────────────────────

class TestCli:
    def test_no_fetch_skips_network(self):
        """--no-fetch 让脚本不跑 git fetch, 加快测试 + offline friendly。"""
        r = subprocess.run(
            [sys.executable, str(SCRIPT),
             "--no-fetch", "--no-color", "--hours", "1"],
            capture_output=True, text=True, timeout=60,
        )
        # 不管 exit code 是啥, 应该输出报告 (可能 warn 说没 token)
        assert r.returncode == 0
        assert "A 机活动报告" in r.stdout

    def test_bad_hours_rejected(self):
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "--hours", "abc", "--no-color"],
            capture_output=True, text=True, timeout=30,
        )
        assert r.returncode == 2


# ─── ActivityItem dataclass ──────────────────────────────────────────────────

class TestActivityItem:
    def test_render_includes_kind_and_summary(self):
        from scripts.check_a_activity import ActivityItem
        item = ActivityItem(kind="pr_merged",
                             summary="PR #17 MERGED",
                             detail="2026-04-24", urgency="urgent")
        txt = item.render()
        assert "pr_merged" in txt
        assert "PR #17" in txt
        assert "2026-04-24" in txt

    def test_default_urgency_info(self):
        from scripts.check_a_activity import ActivityItem
        item = ActivityItem(kind="x", summary="y")
        assert item.urgency == "info"


# ─── git_branches_with_prefix ────────────────────────────────────────────────

class TestGitBranchesWithPrefix:
    def test_real_repo_returns_list(self):
        from scripts.check_a_activity import git_branches_with_prefix
        branches = git_branches_with_prefix("feat-a-")
        # 至少有一个 (测试跑在本 repo, 有 feat-a-phase3 等)
        assert isinstance(branches, list)
        for b in branches:
            assert b.startswith("feat-a-")

    def test_nonexistent_prefix_returns_empty(self):
        from scripts.check_a_activity import git_branches_with_prefix
        assert git_branches_with_prefix("zzz-nonexistent-") == []


# ─── git_recent_commits_on_branch (用 origin/main..branch 独有) ──────────────

class TestGitRecentCommits:
    def test_returns_list_format(self):
        from scripts.check_a_activity import git_recent_commits_on_branch
        # 在实际 repo 里跑, 任何可能的分支
        branches_found = subprocess.run(
            ["git", "branch", "-r"], capture_output=True, text=True,
            timeout=10,
        ).stdout
        # 挑一个 feat-b- 分支跑
        for line in branches_found.splitlines():
            line = line.strip()
            if "feat-b-" in line and "HEAD" not in line:
                br = line.replace("origin/", "").split()[0]
                commits = git_recent_commits_on_branch(br, hours=720)
                assert isinstance(commits, list)
                for c in commits:
                    assert "sha" in c
                    assert "author" in c
                    assert "subject" in c
                return
        pytest.skip("no feat-b- branches in test repo")

    def test_nonexistent_branch_returns_empty(self):
        from scripts.check_a_activity import git_recent_commits_on_branch
        assert git_recent_commits_on_branch(
            "nonexistent-branch-xyz", hours=1) == []


# ─── git_file_changed_recently (排除 initial commit) ────────────────────────

class TestGitFileChanged:
    def test_returns_list(self):
        from scripts.check_a_activity import git_file_changed_recently
        # 查最近 10 年的 README 变化 (总有)
        r = git_file_changed_recently("README.md", hours=24 * 365 * 10)
        assert isinstance(r, list)

    def test_nonexistent_path_returns_empty(self):
        from scripts.check_a_activity import git_file_changed_recently
        r = git_file_changed_recently(
            "path/that/does/not/exist.xyz", hours=24)
        assert r == []


# ─── collect_activity (mock GitHub API) ──────────────────────────────────────

class TestCollectActivity:
    def test_no_token_produces_no_token_item(self):
        from scripts.check_a_activity import collect_activity
        items = collect_activity(hours=24, token=None)
        has_no_token = any(i.kind == "no_token" for i in items)
        assert has_no_token

    def test_mocked_api_pr_merged_detected(self):
        from scripts.check_a_activity import collect_activity
        import datetime as _dt
        now = _dt.datetime.utcnow()
        merged_iso = (now - _dt.timedelta(hours=2)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        fake_pr = {
            "number": 99,
            "title": "some PR",
            "head": {"ref": "feat-b-xyz"},
            "merged_at": merged_iso,
        }
        # github_api_get: /pulls 返 [pr], /pulls/99/reviews 返 [], /issues/99/comments 返 []
        def fake_api(path, token):
            if "/pulls?" in path:
                return [fake_pr]
            if "/reviews" in path:
                return []
            if "/comments" in path:
                return []
            return {}
        with patch("scripts.check_a_activity.github_api_get",
                   side_effect=fake_api):
            items = collect_activity(hours=24, token="fake_token")
        merged_items = [i for i in items if i.kind == "pr_merged"]
        assert len(merged_items) == 1
        assert "PR #99" in merged_items[0].summary

    def test_watch_pr_filter(self):
        from scripts.check_a_activity import collect_activity
        import datetime as _dt
        now = _dt.datetime.utcnow()
        merged_iso = (now - _dt.timedelta(hours=1)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        fake_prs = [
            {"number": 1, "title": "pr1", "head": {"ref": "feat-b-a"},
             "merged_at": merged_iso},
            {"number": 2, "title": "pr2", "head": {"ref": "feat-b-b"},
             "merged_at": merged_iso},
        ]
        def fake_api(path, token):
            if "/pulls?" in path:
                return fake_prs
            return []
        with patch("scripts.check_a_activity.github_api_get",
                   side_effect=fake_api):
            items = collect_activity(hours=24, token="t", watch_pr=2)
        # 只有 pr #2 被处理 — 仅看 API-driven 条目 (pr_merged/pr_comment/pr_review),
        # 不扫 shared_file_change / main_commit_by_a 里的 commit subject
        # (真实 main 的 commit 标题里可能含 "PR #17" 之类字样)
        pr_api_items = [i for i in items
                         if i.kind in ("pr_merged", "pr_comment", "pr_review")]
        for i in pr_api_items:
            assert "PR #1" not in i.summary

    def test_non_feat_b_pr_skipped(self):
        """head.ref 不以 feat-b- 开头的 PR 不应进入 GitHub API 部分。"""
        from scripts.check_a_activity import collect_activity
        import datetime as _dt
        merged_iso = (_dt.datetime.utcnow() - _dt.timedelta(hours=1)
                      ).strftime("%Y-%m-%dT%H:%M:%SZ")
        fake_prs = [
            {"number": 10, "title": "A PR",
             "head": {"ref": "feat-a-phase3"},
             "merged_at": merged_iso},
        ]
        def fake_api(path, token):
            if "/pulls?" in path:
                return fake_prs
            return []
        with patch("scripts.check_a_activity.github_api_get",
                   side_effect=fake_api):
            items = collect_activity(hours=24, token="t")
        # 不应出现 PR #10 相关的 pr_merged / pr_comment / pr_review
        pr_api_items = [i for i in items
                        if i.kind in ("pr_merged", "pr_comment", "pr_review")]
        assert all("PR #10" not in i.summary for i in pr_api_items)


# ─── render ──────────────────────────────────────────────────────────────────

class TestRender:
    def test_empty_activity_prompts_suggestion(self):
        from scripts.check_a_activity import render_report
        txt = render_report([], hours=24, watch_pr=None)
        assert "无 A 的活动" in txt or "建议" in txt

    def test_renders_grouped_by_kind(self):
        from scripts.check_a_activity import render_report, ActivityItem
        items = [
            ActivityItem(kind="branch_commit", summary="c1",
                         urgency="attention"),
            ActivityItem(kind="pr_merged", summary="PR #1 MERGED",
                         urgency="urgent"),
            ActivityItem(kind="branch_commit", summary="c2",
                         urgency="attention"),
        ]
        txt = render_report(items, hours=24, watch_pr=None)
        assert "branch_commit (2)" in txt
        assert "pr_merged (1)" in txt
        assert "c1" in txt
        assert "c2" in txt
        assert "PR #1" in txt


# ─── Review 看板模式 ────────────────────────────────────────────────────────

class TestReviewSummary:
    def test_ready_when_approved(self):
        from scripts.check_a_activity import ReviewSummary
        s = ReviewSummary(pr_number=10, latest_review_state="APPROVED")
        assert s.ready is True
        assert s.blocked is False

    def test_blocked_when_changes_requested(self):
        from scripts.check_a_activity import ReviewSummary
        s = ReviewSummary(pr_number=10,
                           latest_review_state="CHANGES_REQUESTED")
        assert s.ready is False
        assert s.blocked is True

    def test_neither_when_only_commented(self):
        from scripts.check_a_activity import ReviewSummary
        s = ReviewSummary(pr_number=10, latest_review_state="COMMENTED")
        assert s.ready is False
        assert s.blocked is False


class TestLatestReview:
    def test_picks_latest_by_submitted_at(self):
        from scripts.check_a_activity import _latest_review
        reviews = [
            {"state": "COMMENTED", "submitted_at": "2026-04-23T10:00:00Z",
             "user": {"login": "a"}},
            {"state": "APPROVED", "submitted_at": "2026-04-24T12:00:00Z",
             "user": {"login": "a"}},
            {"state": "CHANGES_REQUESTED",
             "submitted_at": "2026-04-23T20:00:00Z",
             "user": {"login": "a"}},
        ]
        r = _latest_review(reviews)
        assert r["state"] == "APPROVED"

    def test_empty_returns_none(self):
        from scripts.check_a_activity import _latest_review
        assert _latest_review([]) is None
        assert _latest_review(None) is None

    def test_skips_malformed_timestamps(self):
        from scripts.check_a_activity import _latest_review
        reviews = [
            {"state": "COMMENTED", "submitted_at": "not-a-date"},
            {"state": "APPROVED", "submitted_at": "2026-04-24T12:00:00Z",
             "user": {"login": "a"}},
        ]
        r = _latest_review(reviews)
        assert r["state"] == "APPROVED"


class TestFetchPrReviewSummary:
    def _call_side_effect(self, pr_meta, reviews):
        def fake_api(path, token):
            if path.endswith("/reviews"):
                return reviews
            return pr_meta
        return fake_api

    def test_approved_pr(self):
        from scripts.check_a_activity import fetch_pr_review_summary
        pr_meta = {"title": "feat foo", "state": "open",
                    "head": {"ref": "feat-b-foo"}, "merged_at": None}
        reviews = [{"state": "APPROVED",
                     "submitted_at": "2026-04-24T12:00:00Z",
                     "user": {"login": "victor2025PH"}}]
        with patch("scripts.check_a_activity.github_api_get",
                   side_effect=self._call_side_effect(pr_meta, reviews)):
            s = fetch_pr_review_summary(10, token="fake")
        assert s.pr_number == 10
        assert s.ready is True
        assert s.latest_review_state == "APPROVED"
        assert s.latest_review_user == "victor2025PH"
        assert s.review_count == 1

    def test_merged_pr(self):
        from scripts.check_a_activity import fetch_pr_review_summary
        pr_meta = {"title": "feat bar", "state": "closed",
                    "head": {"ref": "feat-b-bar"},
                    "merged_at": "2026-04-24T15:00:00Z"}
        with patch("scripts.check_a_activity.github_api_get",
                   side_effect=self._call_side_effect(pr_meta, [])):
            s = fetch_pr_review_summary(11, token="fake")
        assert s.state == "merged"

    def test_no_token_sets_error(self):
        from scripts.check_a_activity import fetch_pr_review_summary
        s = fetch_pr_review_summary(10, token=None)
        assert "token" in s.error

    def test_api_error_captured(self):
        from scripts.check_a_activity import fetch_pr_review_summary
        with patch("scripts.check_a_activity.github_api_get",
                   side_effect=RuntimeError("503 boom")):
            s = fetch_pr_review_summary(10, token="fake")
        assert "503" in s.error


class TestIsReviewDashboardReady:
    def test_all_approved_no_expect_file(self):
        from scripts.check_a_activity import (
            ReviewSummary, is_review_dashboard_ready,
        )
        summaries = [
            ReviewSummary(pr_number=1, latest_review_state="APPROVED"),
            ReviewSummary(pr_number=2, state="merged"),
        ]
        assert is_review_dashboard_ready(summaries, None) is True

    def test_one_changes_requested_blocks(self):
        from scripts.check_a_activity import (
            ReviewSummary, is_review_dashboard_ready,
        )
        summaries = [
            ReviewSummary(pr_number=1, latest_review_state="APPROVED"),
            ReviewSummary(pr_number=2,
                           latest_review_state="CHANGES_REQUESTED"),
        ]
        assert is_review_dashboard_ready(summaries, None) is False

    def test_expect_file_missing_blocks(self):
        from scripts.check_a_activity import (
            ReviewSummary, is_review_dashboard_ready,
        )
        summaries = [
            ReviewSummary(pr_number=1, latest_review_state="APPROVED"),
        ]
        assert is_review_dashboard_ready(summaries, False) is False

    def test_expect_file_present_ok(self):
        from scripts.check_a_activity import (
            ReviewSummary, is_review_dashboard_ready,
        )
        summaries = [
            ReviewSummary(pr_number=1, latest_review_state="APPROVED"),
        ]
        assert is_review_dashboard_ready(summaries, True) is True

    def test_empty_list_not_ready(self):
        """空列表不应报告"就绪" — 防止配置错误被误当 OK。"""
        from scripts.check_a_activity import is_review_dashboard_ready
        assert is_review_dashboard_ready([], None) is False


class TestRenderReviewDashboard:
    def test_approved_row(self):
        from scripts.check_a_activity import (
            ReviewSummary, render_review_dashboard,
        )
        s = ReviewSummary(pr_number=10, title="P7 §7.1",
                           head_ref="feat-b-chat-p7",
                           latest_review_state="APPROVED",
                           latest_review_user="a", review_count=1,
                           state="open")
        txt = render_review_dashboard([s], None, None)
        assert "PR#10" in txt
        assert "APPROVED" in txt
        assert "feat-b-chat-p7" in txt

    def test_expect_file_status_shown(self):
        from scripts.check_a_activity import (
            ReviewSummary, render_review_dashboard,
        )
        s = ReviewSummary(pr_number=10,
                           latest_review_state="APPROVED",
                           state="open")
        txt = render_review_dashboard(
            [s], "docs/REVIEW_RESULTS.md", False)
        assert "docs/REVIEW_RESULTS.md" in txt
        assert "未落地" in txt

    def test_summary_counts(self):
        from scripts.check_a_activity import (
            ReviewSummary, render_review_dashboard,
        )
        summaries = [
            ReviewSummary(pr_number=1, latest_review_state="APPROVED",
                           state="open"),
            ReviewSummary(pr_number=2, state="merged"),
            ReviewSummary(pr_number=3,
                           latest_review_state="CHANGES_REQUESTED",
                           state="open"),
            ReviewSummary(pr_number=4, state="open"),  # 未 review
        ]
        txt = render_review_dashboard(summaries, None, None)
        assert "APPROVED/MERGED: " in txt
        assert "CHANGES_REQUESTED" in txt
        assert "未 review" in txt


class TestReviewsCli:
    def test_exit_1_when_not_all_ready(self):
        """--reviews 模式下,非全绿应 exit 1, 便于 cron 门控。"""
        import scripts.check_a_activity as mod
        with patch.object(mod, "get_token", return_value=None), \
             patch.object(mod, "git_fetch", return_value=True):
            r = subprocess.run(
                [sys.executable, str(SCRIPT),
                 "--reviews", "99999999",
                 "--no-fetch", "--no-color"],
                capture_output=True, text=True, timeout=60,
            )
        # no-token → PR error → not ready → exit 1
        assert r.returncode == 1

    def test_reviews_empty_string_exit_2(self):
        r = subprocess.run(
            [sys.executable, str(SCRIPT),
             "--reviews", " , ,",
             "--no-fetch", "--no-color"],
            capture_output=True, text=True, timeout=30,
        )
        assert r.returncode == 2
