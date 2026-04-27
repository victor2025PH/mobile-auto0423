# cleanup_resume_stack — 5 PR 全 merged 进 main 后的合成分支退役 + tag 备份
#
# 使用场景: 阶段 11 P11.6 — 5 PR (#124/#129/#131/#132/#133) + #134/#135/#137
# 都 merged 进 main 后, 合成分支 feat-a-resume-2026-04-27 失去意义.
# 本脚本验证 PR merged 状态 → tag 备份 → 删除合成分支 (本地 + origin).
#
# 使用:
#   cleanup_resume_stack.bat            # 实跑 (verify + tag + delete)
#   cleanup_resume_stack.bat -DryRun    # 仅验证 + 列计划, 不真删
#
# 安全:
# - 5 PR 任一未 merged → ABORT (不删)
# - delete 前自动 tag stage-final-merged-2026-04-27 锁定历史
# - tag 也 push 到 origin 防本地丢

param([switch]$DryRun)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $ProjectRoot

# 必须 merged 的 PR 列表 (resume stack)
$prs = @(124, 129, 131, 132, 133, 134)
$resumeBranch = 'feat-a-resume-2026-04-27'
$tagName = 'stage-final-merged-2026-04-27'

Write-Host "==========================================="
Write-Host "  Cleanup Resume Stack (Phase 11 P11.6)"
Write-Host "==========================================="
if ($DryRun) { Write-Host "  Mode: DryRun (no delete)" -ForegroundColor DarkYellow }
else { Write-Host "  Mode: FULL (verify + tag + delete)" -ForegroundColor Cyan }
Write-Host ""

# Step 1: 验证 PR 全 merged
Write-Host "[1/4] Verify PR merge status..." -ForegroundColor Cyan
$notMerged = @()
foreach ($p in $prs) {
    $state = & gh pr view $p --json state --jq .state 2>$null
    $statusLine = "  PR #$p  state=$state"
    if ($state -eq 'MERGED') {
        Write-Host $statusLine -ForegroundColor Green
    } else {
        Write-Host $statusLine -ForegroundColor Red
        $notMerged += $p
    }
}

if ($notMerged.Count -gt 0) {
    Write-Host ""
    Write-Host "[ABORT] $($notMerged.Count) PR(s) not yet merged: #$($notMerged -join ', #')" -ForegroundColor Red
    Write-Host "        Cannot delete resume branch until ALL merged." -ForegroundColor DarkRed
    exit 1
}

# Step 2: 验证 main 含合成分支 commits (sanity check)
Write-Host ""
Write-Host "[2/4] Verify main contains resume branch commits..." -ForegroundColor Cyan
& git fetch origin --prune 2>&1 | Out-Null
$resumeShas = & git rev-list "origin/$resumeBranch" --not origin/main 2>$null
$missingCount = ($resumeShas | Measure-Object).Count
if ($missingCount -gt 0) {
    Write-Host "  [WARN] origin/$resumeBranch has $missingCount commits NOT in main" -ForegroundColor Yellow
    Write-Host "         (possibly P3 sibling-guard / sanitize commits squash-merged)" -ForegroundColor DarkYellow
    Write-Host "         Will tag for safety regardless." -ForegroundColor DarkYellow
} else {
    Write-Host "  [OK] all commits already in main" -ForegroundColor Green
}

# Step 3: tag 历史快照
Write-Host ""
Write-Host "[3/4] Tag history snapshot..." -ForegroundColor Cyan
$existingTag = & git tag -l $tagName 2>$null
if ($existingTag) {
    Write-Host "  [SKIP] tag '$tagName' already exists" -ForegroundColor DarkYellow
} else {
    if ($DryRun) {
        Write-Host "  [DryRun] would tag $tagName -> origin/$resumeBranch" -ForegroundColor DarkYellow
    } else {
        & git tag $tagName "origin/$resumeBranch" 2>&1 | Out-Null
        & git push origin $tagName 2>&1 | Out-Null
        Write-Host "  [OK] tag '$tagName' created + pushed" -ForegroundColor Green
    }
}

# Step 4: delete 合成分支 (origin + local)
Write-Host ""
Write-Host "[4/4] Delete resume branch..." -ForegroundColor Cyan
if ($DryRun) {
    Write-Host "  [DryRun] would delete:" -ForegroundColor DarkYellow
    Write-Host "    - local: $resumeBranch (if exists)"
    Write-Host "    - origin/$resumeBranch"
} else {
    # local
    $localExists = & git show-ref --verify --quiet "refs/heads/$resumeBranch"
    if ($LASTEXITCODE -eq 0) {
        & git branch -D $resumeBranch 2>&1 | Out-Null
        Write-Host "  [OK] deleted local: $resumeBranch" -ForegroundColor Green
    } else {
        Write-Host "  [SKIP] local branch '$resumeBranch' does not exist" -ForegroundColor DarkGray
    }
    # origin
    & git push origin --delete $resumeBranch 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "  [OK] deleted origin: $resumeBranch" -ForegroundColor Green
    } else {
        Write-Host "  [SKIP] origin branch already gone (or no permission)" -ForegroundColor DarkYellow
    }
}

Write-Host ""
Write-Host "[DONE] resume stack cleanup complete." -ForegroundColor Green
Write-Host "       History snapshot: tag '$tagName' (recoverable via 'git checkout $tagName')"
