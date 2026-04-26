# OpenClaw sync-with-main helper
# sibling Claude 协同事故防呆: fetch + report ahead/behind, optionally rebase.
#
# Default behavior (safe):
#   - git fetch origin main
#   - report ahead/behind vs main and origin/main
#   - DO NOT modify branches (read-only)
#
# With -Rebase:
#   - additionally rebase current feat-* branch onto origin/main
#   - REFUSES to rebase main itself (use -Pull to update local main)
#   - if there are merge conflicts, leaves you in rebase state to resolve
#
# With -Pull (only on main):
#   - git pull --ff-only origin main (fast-forward update local main)
#
# Usage:
#   sync_with_main.bat              # report only (safe)
#   sync_with_main.bat -Rebase      # rebase current branch onto origin/main
#   sync_with_main.bat -Pull        # fast-forward local main from origin

param(
    [switch]$Rebase,
    [switch]$Pull,
    [switch]$AutoStash,   # BBB: pass --autostash to git rebase (auto stash+pop dirty)
    [switch]$CherryPick,  # FFF: cherry-pick fresh commits to new branch off origin/main
                          #      (use when main has squash-merged this branch's commits)
    [string]$BranchName = ""   # FFF: name for new cherry-pick branch (default: auto)
)

$ErrorActionPreference = 'Continue'
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $ProjectRoot

# Mutex: -Rebase / -Pull / -CherryPick are exclusive (pick one apply mode)
$_modes = @($Rebase, $Pull, $CherryPick) | Where-Object { $_ } | Measure-Object | Select-Object -ExpandProperty Count
if ($_modes -gt 1) {
    Write-Host "[ERROR] -Rebase / -Pull / -CherryPick are mutually exclusive (pick one)." -ForegroundColor Red
    exit 2
}

Write-Host "==========================================="
Write-Host "  OpenClaw sync-with-main"
Write-Host "==========================================="
Write-Host ""

# ---- 1. fetch ----
Write-Host "[1/4] git fetch origin main"
$fetchOutput = & git fetch origin main 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "   [ERROR] fetch failed:" -ForegroundColor Red
    $fetchOutput | ForEach-Object { Write-Host "      $_" -ForegroundColor DarkRed }
    exit 1
}
Write-Host "   [OK]   fetched origin/main"

# ---- 2. inspect state ----
$currentBranch = (& git branch --show-current 2>$null).Trim()
Write-Host ""
Write-Host "[2/4] State inspection"
Write-Host ("   current branch: {0}" -f $currentBranch)

$mainAhead = [int](& git rev-list --count "origin/main..main" 2>$null)
$mainBehind = [int](& git rev-list --count "main..origin/main" 2>$null)
Write-Host ("   local main vs origin/main: ahead={0} behind={1}" -f $mainAhead, $mainBehind)

if ($currentBranch -ne 'main') {
    $branchAhead = [int](& git rev-list --count "main..HEAD" 2>$null)
    $branchBehind = [int](& git rev-list --count "HEAD..main" 2>$null)
    $branchAheadOrigin = [int](& git rev-list --count "origin/main..HEAD" 2>$null)
    $branchBehindOrigin = [int](& git rev-list --count "HEAD..origin/main" 2>$null)
    Write-Host ("   {0} vs main:        ahead={1} behind={2}" -f $currentBranch, $branchAhead, $branchBehind)
    Write-Host ("   {0} vs origin/main: ahead={1} behind={2}" -f $currentBranch, $branchAheadOrigin, $branchBehindOrigin)
}

# ---- 3. recommendation ----
Write-Host ""
Write-Host "[3/4] Recommendation"

$needPull = $mainBehind -gt 0
$needRebase = ($currentBranch -ne 'main') -and (([int](& git rev-list --count "HEAD..origin/main" 2>$null)) -gt 0)

if (-not $needPull -and -not $needRebase) {
    Write-Host "   [OK]   already up to date with origin/main" -ForegroundColor Green
}
if ($needPull) {
    Write-Host ("   [SUGGEST] local main is behind origin/main by {0}; run sync_with_main.bat -Pull" -f $mainBehind) -ForegroundColor Cyan
}
if ($needRebase) {
    Write-Host ("   [SUGGEST] {0} is behind origin/main; run sync_with_main.bat -Rebase" -f $currentBranch) -ForegroundColor Cyan
}

# ---- 4. apply (if requested) ----
Write-Host ""
Write-Host "[4/4] Apply"

if ($Pull) {
    if ($currentBranch -ne 'main') {
        Write-Host "   [ERROR] -Pull requires being on main. Currently on $currentBranch." -ForegroundColor Red
        Write-Host "           git checkout main && sync_with_main.bat -Pull" -ForegroundColor DarkRed
        exit 1
    }
    Write-Host "   git pull --ff-only origin main"
    & git pull --ff-only origin main 2>&1 | ForEach-Object { Write-Host "      $_" -ForegroundColor DarkGray }
    if ($LASTEXITCODE -ne 0) {
        Write-Host "   [ERROR] pull failed (likely diverged); manual resolve needed" -ForegroundColor Red
        exit 1
    }
    Write-Host "   [OK]   main fast-forwarded to origin/main" -ForegroundColor Green
} elseif ($Rebase) {
    if ($currentBranch -eq 'main') {
        Write-Host "   [ERROR] -Rebase is for feat-* branches, not main. Use -Pull instead." -ForegroundColor Red
        exit 1
    }
    # Check for uncommitted changes
    $dirty = & git status --porcelain 2>$null | Where-Object { $_ -notmatch '^\?\?' }
    if ($dirty -and -not $AutoStash) {
        Write-Host "   [ERROR] working tree has uncommitted changes; commit or stash first." -ForegroundColor Red
        Write-Host "           Or pass -AutoStash to auto stash+rebase+pop." -ForegroundColor DarkRed
        exit 1
    }
    if ($dirty -and $AutoStash) {
        Write-Host "   [INFO] working tree dirty; using --autostash" -ForegroundColor Cyan
    }
    $rebaseArgs = @('rebase')
    if ($AutoStash) { $rebaseArgs += '--autostash' }
    $rebaseArgs += 'origin/main'
    Write-Host ("   git {0}  ({1} -> origin/main)" -f ($rebaseArgs -join ' '), $currentBranch)
    & git @rebaseArgs 2>&1 | ForEach-Object { Write-Host "      $_" -ForegroundColor DarkGray }
    if ($LASTEXITCODE -ne 0) {
        Write-Host ""
        Write-Host "   [CONFLICT] rebase has conflicts." -ForegroundColor Yellow
        # GGG: This commonly happens when main has squash-merged this branch's
        # earlier commits. Replaying the originals hits the squash content.
        Write-Host "   This often happens when main squash-merged your previous commits." -ForegroundColor DarkYellow
        Write-Host "   Recommended fix (squash-merge case):" -ForegroundColor DarkYellow
        Write-Host "      git rebase --abort" -ForegroundColor DarkYellow
        Write-Host "      sync_with_main.bat -CherryPick    (smart: detects squash via patch-id)" -ForegroundColor DarkYellow
        Write-Host "   Or resolve manually:" -ForegroundColor DarkGray
        Write-Host "      edit conflicted files / git add / git rebase --continue / --abort" -ForegroundColor DarkGray
        exit 1
    }
    Write-Host "   [OK]   rebased onto origin/main" -ForegroundColor Green
    $newAhead = [int](& git rev-list --count "origin/main..HEAD" 2>$null)
    Write-Host ("          now {0} commit(s) ahead of origin/main, ready to push" -f $newAhead) -ForegroundColor Cyan
} elseif ($CherryPick) {
    # FFF: smart cherry-pick using `git cherry` to detect squash-merged commits
    # by patch-id. Creates new branch off origin/main and applies only fresh ones.
    if ($currentBranch -eq 'main') {
        Write-Host "   [ERROR] -CherryPick is for feat-* branches, not main." -ForegroundColor Red
        exit 1
    }

    # git cherry origin/main HEAD format: "+ <sha>" = fresh, "- <sha>" = already in main (by patch-id)
    $cherryOutput = & git cherry origin/main HEAD 2>$null
    $freshCommits = @()
    $squashedCommits = @()
    foreach ($line in $cherryOutput) {
        if ($line -match '^\+\s+([a-f0-9]+)$')      { $freshCommits += $matches[1] }
        elseif ($line -match '^\-\s+([a-f0-9]+)$')  { $squashedCommits += $matches[1] }
    }

    Write-Host ""
    Write-Host ("   Found: {0} fresh commit(s), {1} already-in-main (by patch-id)" -f $freshCommits.Count, $squashedCommits.Count)
    if ($squashedCommits.Count -gt 0) {
        Write-Host "          (squashed-to-main commits will be skipped automatically)" -ForegroundColor DarkGray
    }

    if ($freshCommits.Count -eq 0) {
        Write-Host "   [OK]   nothing fresh to cherry-pick. Branch is fully merged." -ForegroundColor Green
        Write-Host ("          Safe to delete: git branch -D {0}" -f $currentBranch) -ForegroundColor DarkGray
        exit 0
    }

    # Pick destination branch name
    if ($BranchName) {
        $cleanName = $BranchName -replace '[^a-zA-Z0-9-]', '-'
        $newBranch = "feat-ops-$cleanName-$(Get-Date -Format 'yyyy-MM-dd')"
    } else {
        $newBranch = "feat-ops-resync-$(Get-Date -Format 'yyyy-MM-dd-HHmm')"
    }
    $exists = & git rev-parse --verify --quiet "refs/heads/$newBranch" 2>$null
    if ($exists) {
        Write-Host ("   [ERROR] new branch '{0}' already exists. Pass -BranchName <unique>." -f $newBranch) -ForegroundColor Red
        exit 1
    }

    # Refuse if working tree dirty
    $dirty = & git status --porcelain 2>$null | Where-Object { $_ -notmatch '^\?\?' }
    if ($dirty) {
        Write-Host "   [ERROR] working tree has uncommitted changes; commit or stash first." -ForegroundColor Red
        exit 1
    }

    Write-Host ""
    Write-Host ("   Creating '{0}' off origin/main..." -f $newBranch)
    & git checkout -b $newBranch origin/main 2>&1 | ForEach-Object { Write-Host "      $_" -ForegroundColor DarkGray }
    if ($LASTEXITCODE -ne 0) {
        Write-Host "   [ERROR] checkout failed (see above)" -ForegroundColor Red
        exit 1
    }

    Write-Host ""
    Write-Host ("   Cherry-picking {0} fresh commit(s) (oldest first)..." -f $freshCommits.Count)
    foreach ($sha in $freshCommits) {
        $shortSha = $sha.Substring(0, 7)
        $subject = (& git log -1 --pretty=%s $sha 2>$null).Trim()
        Write-Host ("      pick {0}: {1}" -f $shortSha, $subject) -ForegroundColor DarkCyan
        & git cherry-pick $sha 2>&1 | ForEach-Object { Write-Host "         $_" -ForegroundColor DarkGray }
        if ($LASTEXITCODE -ne 0) {
            Write-Host ""
            Write-Host ("   [CONFLICT] cherry-pick {0} failed. Resolve:" -f $shortSha) -ForegroundColor Yellow
            Write-Host "      1) edit conflicted files / git add <files>" -ForegroundColor DarkGray
            Write-Host "      2) git cherry-pick --continue" -ForegroundColor DarkGray
            Write-Host "      or abort: git cherry-pick --abort && git checkout - && git branch -D $newBranch" -ForegroundColor DarkGray
            exit 1
        }
    }

    Write-Host ""
    Write-Host ("   [OK]   cherry-picked {0} commit(s) onto {1}" -f $freshCommits.Count, $newBranch) -ForegroundColor Green
    Write-Host ""
    Write-Host "Next steps:" -ForegroundColor DarkCyan
    Write-Host ("  - git push -u origin {0} && gh pr create" -f $newBranch)
    if ($squashedCommits.Count -gt 0) {
        Write-Host ("  - obsolete: git branch -D {0}" -f $currentBranch) -ForegroundColor DarkGray
    }
} else {
    Write-Host "   [SKIP] no -Rebase / -Pull / -CherryPick flag (read-only mode)" -ForegroundColor DarkGray
}

exit 0
