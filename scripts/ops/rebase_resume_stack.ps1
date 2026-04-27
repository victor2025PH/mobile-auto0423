# rebase_resume_stack — 一键 rebase 5 个 resume PR 到 origin/main
#
# 使用场景: PR #134 (regression fix) merge 进 main 后, 5 个老 PR
# (#124/#129/#131/#132/#133) 需要 rebase main 拉到新 fix 让 CI 重跑.
# 本脚本自动化 fetch → worktree 隔离 rebase → force-with-lease push.
#
# 使用:
#   rebase_resume_stack.bat            # 实跑 (rebase + force push)
#   rebase_resume_stack.bat -DryRun    # 仅模拟 rebase, 不 push
#   rebase_resume_stack.bat -NoPush    # rebase 但不 push (本地预演)
#
# 安全: --force-with-lease 仅在 origin head 仍是脚本期望的版本时 push,
#       sibling 期间有人 push 同分支会拒, 不覆盖.
# 隔离: 用 git worktree 不动当前 cwd, service 不受影响.

param(
    [switch]$DryRun,
    [switch]$NoPush
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $ProjectRoot

$prs = @(
    @{ Branch = 'feat-a-fb-drawer-ui-2026-04-27';      PR = 124; Title = 'drawer UI' },
    @{ Branch = 'feat-a-fb-task-ux-p0-2026-04-27';     PR = 129; Title = 'task center P0' },
    @{ Branch = 'feat-ops-quota-ux-2026-04-27';        PR = 131; Title = 'quota UX' },
    @{ Branch = 'feat-a-fb-error-classify-2026-04-27'; PR = 132; Title = 'error classify' },
    @{ Branch = 'feat-ops-sibling-guard-2026-04-27';   PR = 133; Title = 'sibling guard' }
)

Write-Host "==========================================="
Write-Host "  Rebase Resume Stack (5 PRs)"
Write-Host "==========================================="
if ($DryRun) { Write-Host "  Mode: DryRun (no push)" -ForegroundColor DarkYellow }
elseif ($NoPush) { Write-Host "  Mode: NoPush" -ForegroundColor DarkYellow }
else { Write-Host "  Mode: FULL (rebase + force-with-lease push)" -ForegroundColor Cyan }
Write-Host ""

Write-Host "[1/3] git fetch origin --prune..." -ForegroundColor Cyan
& git fetch origin --prune 2>&1 | Out-Host
$mainSha = (& git rev-parse origin/main).Trim()
Write-Host "      origin/main = $($mainSha.Substring(0,8))"
Write-Host ""

Write-Host "[2/3] Rebase each PR onto origin/main..." -ForegroundColor Cyan
$results = @()
$temp = $env:TEMP

foreach ($pr in $prs) {
    $branch = $pr.Branch
    $prnum = $pr.PR
    $title = $pr.Title
    Write-Host ""
    Write-Host "--- PR #$prnum  $title  ($branch) ---" -ForegroundColor White

    $wt = Join-Path $temp ("restack_" + [System.IO.Path]::GetRandomFileName())

    try {
        & git worktree add $wt "origin/$branch" 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) {
            Write-Host "  [WT-FAIL] worktree add failed" -ForegroundColor Red
            $results += [PSCustomObject]@{ PR = $prnum; Branch = $branch; Status = 'WT-FAIL' }
            continue
        }

        $beforeSha = (& git -C $wt rev-parse HEAD).Trim()

        & git -C $wt rebase origin/main 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) {
            $conflictFiles = & git -C $wt diff --name-only --diff-filter=U
            Write-Host "  [CONFLICT]" -ForegroundColor Red
            $conflictFiles | ForEach-Object { Write-Host "    - $_" -ForegroundColor DarkRed }
            & git -C $wt rebase --abort 2>&1 | Out-Null
            $results += [PSCustomObject]@{ PR = $prnum; Branch = $branch; Status = 'CONFLICT' }
            continue
        }

        $aheadAfter = (& git -C $wt rev-list --count "origin/main..HEAD").Trim()
        Write-Host "  [REBASED] $aheadAfter commits ahead of main" -ForegroundColor Green

        if ($DryRun -or $NoPush) {
            Write-Host "  [SKIP-PUSH] (DryRun/NoPush)" -ForegroundColor DarkYellow
            $results += [PSCustomObject]@{ PR = $prnum; Branch = $branch; Status = 'REBASED'; Ahead = $aheadAfter }
            continue
        }

        & git -C $wt push --force-with-lease="${branch}:${beforeSha}" origin "HEAD:refs/heads/$branch" 2>&1 | Out-Host
        if ($LASTEXITCODE -eq 0) {
            Write-Host "  [PUSHED]" -ForegroundColor Green
            $results += [PSCustomObject]@{ PR = $prnum; Branch = $branch; Status = 'OK'; Ahead = $aheadAfter }
        } else {
            Write-Host "  [PUSH-REJECTED] origin head changed (sibling pushed?), skipped" -ForegroundColor Yellow
            $results += [PSCustomObject]@{ PR = $prnum; Branch = $branch; Status = 'PUSH-REJECTED' }
        }
    }
    finally {
        & git worktree remove --force $wt 2>&1 | Out-Null
        if (Test-Path $wt) {
            Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $wt
        }
    }
}

Write-Host ""
Write-Host "[3/3] Summary:" -ForegroundColor Cyan
$results | Format-Table -AutoSize | Out-Host

$conflicts = @($results | Where-Object { $_.Status -eq 'CONFLICT' })
$pushFails = @($results | Where-Object { $_.Status -in 'PUSH-FAIL', 'PUSH-REJECTED', 'WT-FAIL' })
$ok = @($results | Where-Object { $_.Status -in 'OK', 'REBASED' })

if ($conflicts.Count -gt 0) {
    Write-Host ""
    Write-Host "[ATTN] $($conflicts.Count) PR with conflict, manual rebase needed:" -ForegroundColor Yellow
    $conflicts | ForEach-Object { Write-Host "  - PR #$($_.PR) ($($_.Branch))" }
}
if ($pushFails.Count -gt 0) {
    Write-Host ""
    Write-Host "[ATTN] $($pushFails.Count) PR push rejected:" -ForegroundColor Yellow
    $pushFails | ForEach-Object { Write-Host "  - PR #$($_.PR) status=$($_.Status)" }
}

Write-Host ""
if ($conflicts.Count -eq 0 -and $pushFails.Count -eq 0) {
    Write-Host "[OK] All $($ok.Count)/$($prs.Count) PRs rebased." -ForegroundColor Green
    if (-not ($DryRun -or $NoPush)) {
        Write-Host ""
        Write-Host "Next: wait CI rerun ~5min, then review approve + squash merge."
        $ok | ForEach-Object {
            Write-Host ("   PR #{0}: https://github.com/victor2025PH/mobile-auto0423/pull/{0}" -f $_.PR)
        }
    }
    exit 0
} else {
    Write-Host "[PARTIAL] Some PRs need manual handling - see ATTN above" -ForegroundColor Yellow
    exit 1
}
