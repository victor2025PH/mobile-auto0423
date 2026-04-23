# -*- coding: utf-8 -*-
"""A 机活动检查 — 替代"人工转发+等回复"低效沟通。

核心洞察: 两台 Claude 通过同一 GitHub repo 同步, A 推送 commit 后 B 只需
``git fetch`` 就能读到。问题是没有**通知机制**。本工具自动化这个 poll
过程, 让 B 不再需要 user 人工转发 "A 已完成" 这类消息。

检查项:
  * A 的分支最近 24h 有没有新 commit
  * B→A 的 review request PRs (#6-#17) 有没有被 A 回应 (comment / review / merge)
  * 关键共享契约文件 (INTEGRATION_CONTRACT.md / database.py / fb_store.py) 是否有 A 的新改动
  * 特定请求 PR (如 PR #17 audit_logs fix) 是否已合入 main

用法:
    python scripts/check_a_activity.py                  # 默认 24h 窗口
    python scripts/check_a_activity.py --hours 72       # 扩到 3 天
    python scripts/check_a_activity.py --watch-pr 17    # 专注某个 PR
    python scripts/check_a_activity.py --json           # 程序消费
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
BLUE = "\033[34m"
CYAN = "\033[36m"
RESET = "\033[0m"
BOLD = "\033[1m"

if os.environ.get("NO_COLOR") or (sys.platform == "win32" and
                                    not os.environ.get("ANSICON")):
    try:
        os.system("")
    except Exception:
        GREEN = YELLOW = RED = BLUE = CYAN = RESET = BOLD = ""


REPO = "victor2025PH/mobile-auto0423"
A_BRANCH_PREFIXES = ("feat-a-",)
A_USERS = ("victor2025PH",)  # A 账号
# 重要共享契约文件 (若 A 改动应提醒 B)
SHARED_FILES_TO_WATCH = [
    "docs/INTEGRATION_CONTRACT.md",
    "src/host/database.py",
    "src/host/fb_store.py",
    "src/host/fb_concurrency.py",
]


# ─────────────────────────────────────────────────────────────────────────────
# Git 本地层
# ─────────────────────────────────────────────────────────────────────────────

def git_fetch() -> bool:
    try:
        subprocess.run(["git", "fetch", "origin", "--quiet"],
                        check=True, timeout=30)
        return True
    except Exception as e:
        print(f"{YELLOW}[WARN]{RESET} git fetch 失败: {e}", file=sys.stderr)
        return False


def git_branches_with_prefix(prefix: str) -> List[str]:
    try:
        r = subprocess.run(
            ["git", "branch", "-r", "--list", f"origin/{prefix}*"],
            capture_output=True, text=True, check=True, timeout=15,
        )
        lines = [l.strip().replace("origin/", "") for l in r.stdout.splitlines()]
        return [l for l in lines if l and "HEAD" not in l]
    except Exception:
        return []


def git_recent_commits_on_branch(branch: str, hours: int,
                                   main_ref: str = "origin/main"
                                   ) -> List[Dict[str, str]]:
    """返回该分支相对于 main 独有的最近 N 小时 commits。

    用 ``origin/main..branch`` 过滤掉已在 main 的 commits, 避免栈式 PR
    多分支重复报同一 commit (例如 feat-a-phase3 的 commits 也在
    feat-a-phase5-mesh 和 feat-a-reply-to-b 里)。
    """
    since = _dt.datetime.utcnow() - _dt.timedelta(hours=hours)
    since_iso = since.strftime("%Y-%m-%dT%H:%M:%S+0000")
    try:
        r = subprocess.run(
            ["git", "log", f"{main_ref}..origin/{branch}",
             f"--since={since_iso}",
             "--pretty=format:%H|%an|%ar|%s"],
            capture_output=True, text=True, check=True, timeout=15,
        )
    except Exception:
        return []
    commits = []
    for line in r.stdout.splitlines():
        if not line or "|" not in line:
            continue
        parts = line.split("|", 3)
        if len(parts) != 4:
            continue
        sha, author, when, subject = parts
        commits.append({"sha": sha[:7], "full_sha": sha, "author": author,
                        "when": when, "subject": subject[:80]})
    return commits


def git_main_recent_commits(hours: int) -> List[Dict[str, str]]:
    return git_recent_commits_on_branch("main", hours)


def git_file_changed_recently(path: str, hours: int,
                               branch: str = "main"
                               ) -> List[Dict[str, str]]:
    """某路径在 N 小时内的新 commits (排除 initial commit 无 parent 的)。

    返回 [{sha, when, subject}, ...], 空列表表示无实际变化。
    """
    since = _dt.datetime.utcnow() - _dt.timedelta(hours=hours)
    since_iso = since.strftime("%Y-%m-%dT%H:%M:%S+0000")
    try:
        r = subprocess.run(
            ["git", "log", f"origin/{branch}",
             f"--since={since_iso}",
             # %P parent SHAs (空 = initial commit)
             "--pretty=format:%H|%P|%ar|%s",
             "--", path],
            capture_output=True, text=True, check=True, timeout=15,
        )
    except Exception:
        return []
    commits: List[Dict[str, str]] = []
    for line in r.stdout.splitlines():
        if not line or "|" not in line:
            continue
        parts = line.split("|", 3)
        if len(parts) != 4:
            continue
        sha, parents, when, subject = parts
        if not parents.strip():
            continue  # initial commit, 跳过 (不算"变化")
        commits.append({"sha": sha[:7], "when": when,
                        "subject": subject[:80]})
    return commits


# ─────────────────────────────────────────────────────────────────────────────
# GitHub API 层
# ─────────────────────────────────────────────────────────────────────────────

def get_token() -> Optional[str]:
    try:
        r = subprocess.run(
            ["git", "credential", "fill"],
            input="protocol=https\nhost=github.com\n\n",
            capture_output=True, text=True, timeout=10, check=True,
        )
    except Exception:
        return None
    for line in r.stdout.splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1].strip()
    return None


def github_api_get(path: str, token: Optional[str]) -> Any:
    headers = {"Accept": "application/vnd.github+json",
               "User-Agent": "check-a-activity"}
    if token:
        headers["Authorization"] = f"token {token}"
    req = urllib.request.Request(
        f"https://api.github.com{path}", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{path}: {e.code} {e.read().decode()[:200]}")


# ─────────────────────────────────────────────────────────────────────────────
# Activity items
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ActivityItem:
    kind: str  # branch_commit / pr_comment / pr_review / pr_merged / shared_file_change
    summary: str
    detail: str = ""
    urgency: str = "info"  # info / attention / urgent

    def render(self) -> str:
        color = {"info": "", "attention": YELLOW,
                 "urgent": RED}.get(self.urgency, "")
        tag = f"[{self.kind:20}]"
        return f"  {color}{tag}{RESET} {self.summary}" + (
            f"\n      {BLUE}→ {self.detail}{RESET}" if self.detail else "")


def collect_activity(hours: int, token: Optional[str],
                     watch_pr: Optional[int] = None) -> List[ActivityItem]:
    items: List[ActivityItem] = []

    # 1. A 的分支最近 commits (去重 SHA, 避免栈式分支重复报同一 commit)
    seen_shas: set = set()
    branch_commits: List[tuple] = []  # (branch, commit dict)
    for prefix in A_BRANCH_PREFIXES:
        for br in git_branches_with_prefix(prefix):
            commits = git_recent_commits_on_branch(br, hours)
            for c in commits:
                if c["full_sha"] in seen_shas:
                    continue
                seen_shas.add(c["full_sha"])
                branch_commits.append((br, c))
    # 按时间倒序 (git log 默认已经是)
    for br, c in branch_commits:
        items.append(ActivityItem(
            kind="branch_commit",
            summary=f"{br}: {c['subject']}",
            detail=f"{c['sha']} by {c['author']} {c['when']}",
            urgency="attention",
        ))

    # 2. main 分支最近 A 的 commits
    for c in git_main_recent_commits(hours):
        if any(u in c["author"] for u in A_USERS):
            items.append(ActivityItem(
                kind="main_commit_by_a",
                summary=f"main: {c['subject']}",
                detail=f"{c['sha']} by {c['author']} {c['when']}",
                urgency="urgent",  # A 合进 main 是大事
            ))

    # 3. 重点共享契约文件在 main 分支上的新变化 (排除 initial commit)
    for f in SHARED_FILES_TO_WATCH:
        changes = git_file_changed_recently(f, hours, "main")
        for c in changes:
            items.append(ActivityItem(
                kind="shared_file_change",
                summary=f"main 的 {f}: {c['subject']}",
                detail=f"{c['sha']} {c['when']} — B 可能需要 git rebase origin/main",
                urgency="urgent",  # 共享文件进入 main 真值得 urgent
            ))

    # 4. GitHub API: B→A 的 PR 有没有 A 的互动
    if token:
        # B 自己账号是 claude-messenger-bot 本地 name, GitHub API 里 PR author
        # 实际可能是 victor2025PH 因为 git credential 是 victor2025PH 的 token。
        # 所以 "B's PR" 实际通过 head 分支前缀 feat-b- 识别, review/comment
        # 的 reviewer 通过 login 非 B 过滤。这里简化: 扫所有 feat-b- 开头的
        # PR, 看最近 N 小时有没有 review/comment 活动。
        try:
            prs = github_api_get(
                f"/repos/{REPO}/pulls?state=all&per_page=50", token)
        except Exception as e:
            items.append(ActivityItem(
                kind="github_api_error",
                summary="GitHub API 调用失败",
                detail=str(e)[:100],
                urgency="attention",
            ))
            prs = []

        cutoff = _dt.datetime.utcnow() - _dt.timedelta(hours=hours)
        for p in prs:
            head_ref = (p.get("head") or {}).get("ref", "")
            if not head_ref.startswith("feat-b-"):
                continue
            num = p["number"]

            if watch_pr and num != watch_pr:
                continue

            # reviews
            try:
                reviews = github_api_get(
                    f"/repos/{REPO}/pulls/{num}/reviews", token) or []
            except Exception:
                reviews = []
            for r in reviews:
                sub = r.get("submitted_at", "")
                if not sub:
                    continue
                try:
                    ts = _dt.datetime.strptime(sub, "%Y-%m-%dT%H:%M:%SZ")
                except ValueError:
                    continue
                if ts < cutoff:
                    continue
                user = (r.get("user") or {}).get("login", "")
                state = r.get("state", "")
                items.append(ActivityItem(
                    kind="pr_review",
                    summary=f"PR #{num} {state} by {user}",
                    detail=f"{sub} — {p['title'][:60]}",
                    urgency="urgent" if state == "CHANGES_REQUESTED" else "attention",
                ))

            # comments (issue_comments API, PR comments 走同一端点)
            try:
                comments = github_api_get(
                    f"/repos/{REPO}/issues/{num}/comments", token) or []
            except Exception:
                comments = []
            for c in comments:
                created = c.get("created_at", "")
                if not created:
                    continue
                try:
                    ts = _dt.datetime.strptime(created, "%Y-%m-%dT%H:%M:%SZ")
                except ValueError:
                    continue
                if ts < cutoff:
                    continue
                user = (c.get("user") or {}).get("login", "")
                body = c.get("body", "")[:80]
                items.append(ActivityItem(
                    kind="pr_comment",
                    summary=f"PR #{num} comment by {user}",
                    detail=f"{created} — {body}",
                    urgency="urgent",
                ))

            # merged state
            if p.get("merged_at"):
                try:
                    merged = _dt.datetime.strptime(p["merged_at"],
                                                     "%Y-%m-%dT%H:%M:%SZ")
                except ValueError:
                    merged = None
                if merged and merged >= cutoff:
                    items.append(ActivityItem(
                        kind="pr_merged",
                        summary=f"PR #{num} MERGED 🎉",
                        detail=f"{p['merged_at']} — {p['title'][:60]}",
                        urgency="urgent",
                    ))
    else:
        items.append(ActivityItem(
            kind="no_token",
            summary="GitHub token 不可用 (git credential fill 没给), PR 活动未查",
            detail="装 GitHub CLI 或配 git credential helper",
            urgency="attention",
        ))

    return items


# ─────────────────────────────────────────────────────────────────────────────
# Render
# ─────────────────────────────────────────────────────────────────────────────

def render_report(items: List[ActivityItem], hours: int,
                   watch_pr: Optional[int]) -> str:
    lines: List[str] = []
    lines.append(f"\n{BOLD}=== A 机活动报告 (最近 {hours}h){RESET}")
    if watch_pr:
        lines.append(f"{BOLD}重点关注 PR #{watch_pr}{RESET}")

    if not items:
        lines.append(f"\n  {YELLOW}(本窗口内无 A 的活动){RESET}")
        lines.append(f"\n  建议: 增大 --hours 或发消息提醒 A, 或执行:")
        lines.append(f"    git fetch origin")
        lines.append(f"    git log origin/main --since='{hours} hours ago' --pretty=oneline")
        return "\n".join(lines)

    # 分组
    by_kind: Dict[str, List[ActivityItem]] = {}
    for it in items:
        by_kind.setdefault(it.kind, []).append(it)

    for kind, group in by_kind.items():
        header_color = CYAN
        if any(it.urgency == "urgent" for it in group):
            header_color = RED
        lines.append(f"\n{BOLD}{header_color}## {kind} ({len(group)}){RESET}")
        for it in group:
            lines.append(it.render())

    # 汇总
    urgent_n = sum(1 for it in items if it.urgency == "urgent")
    attn_n = sum(1 for it in items if it.urgency == "attention")
    lines.append(f"\n{BOLD}=== 汇总{RESET}")
    lines.append(f"  总事件: {len(items)}")
    if urgent_n:
        lines.append(f"  {RED}urgent: {urgent_n}{RESET} (A 合入 main / review / comment, 需关注)")
    if attn_n:
        lines.append(f"  {YELLOW}attention: {attn_n}{RESET}")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="A 机活动检查 (替代人工转发)")
    parser.add_argument("--hours", type=int, default=24,
                        help="查最近 N 小时 (默认 24)")
    parser.add_argument("--watch-pr", type=int, default=None,
                        help="只关注特定 PR 编号")
    parser.add_argument("--json", action="store_true",
                        help="输出 JSON 供程序消费")
    parser.add_argument("--no-fetch", action="store_true",
                        help="跳过 git fetch (如果刚 fetch 过)")
    parser.add_argument("--no-color", action="store_true")
    args = parser.parse_args()

    if args.no_color:
        global GREEN, YELLOW, RED, BLUE, CYAN, RESET, BOLD
        GREEN = YELLOW = RED = BLUE = CYAN = RESET = BOLD = ""

    if not args.no_fetch:
        git_fetch()

    token = get_token()
    items = collect_activity(args.hours, token, watch_pr=args.watch_pr)

    if args.json:
        out = [{"kind": i.kind, "summary": i.summary,
                "detail": i.detail, "urgency": i.urgency}
               for i in items]
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    print(render_report(items, args.hours, args.watch_pr))
    # exit code: 有 urgent 返 0 (给 user 看, 不当失败)
    return 0


if __name__ == "__main__":
    sys.exit(main())
