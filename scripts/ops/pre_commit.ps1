# OpenClaw pre-commit hook
# Installed via install_hooks.bat -> .git/hooks/pre-commit (calls this).
# Blocks commit on common mistakes:
#   1. Committing to main directly (CLAUDE.md "no direct commits to main").
#   2. Local main is behind origin/main (sibling Claude likely merged a PR).
#   3. Trying to commit runtime config files (config/cluster_state.json etc).
#
# Bypass (rare, intentional): commit with --no-verify

$ErrorActionPreference = 'Continue'

# Repository root (this script lives at scripts/ops/pre_commit.ps1)
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $ProjectRoot

$err = 0
function Block { param([string]$msg) Write-Host "[BLOCK] $msg" -ForegroundColor Red; $script:err = 1 }
function Note  { param([string]$msg) Write-Host "[INFO]  $msg" -ForegroundColor Cyan }

# ---- 1. Refuse direct commits to main ----
$branch = (& git branch --show-current 2>$null).Trim()
if ($branch -eq 'main') {
    Block "Committing to main is forbidden by CLAUDE.md."
    Write-Host "        Fix:  branch_create.bat   (one-shot helper)" -ForegroundColor DarkRed
    Write-Host "        Bypass (rare): git commit --no-verify" -ForegroundColor DarkGray
}

# ---- 2. Refuse staged runtime config files ----
$staged = & git diff --cached --name-only 2>$null
$runtimeConfigs = @(
    'config/cluster_state.json',
    'config/device_aliases.json',
    'config/device_registry.json',
    'config/notify_config.json',
    'config/launch.env',         # local override, not in git
    'config/central_push_queue.db',
    'config/central_push_queue.db-shm',
    'config/central_push_queue.db-wal'
)
foreach ($f in $staged) {
    if ($runtimeConfigs -contains $f.Trim()) {
        Block "Staged runtime file: $f (these are written by server.py at runtime)."
        Write-Host "        Unstage: git restore --staged $f" -ForegroundColor DarkRed
    }
}

# ---- 3. Warn (not block) if local main is behind origin/main ----
# Don't block: fetching every commit slows pre-commit too much.
# Soft warning so user knows to sync_with_main.bat -Rebase eventually.
$hasOriginMain = (& git rev-parse --verify --quiet "refs/remotes/origin/main") 2>$null
if ($hasOriginMain) {
    $behind = [int](& git rev-list --count "main..origin/main" 2>$null)
    if ($behind -gt 0) {
        Note "local main is $behind commit(s) behind origin/main (cached, not fetched)."
        Note "  consider: sync_with_main.bat -Rebase  (after this commit)"
    }
}

# ---- 4. Print summary ----
if ($err -ne 0) {
    Write-Host ""
    Write-Host "[pre-commit] commit BLOCKED (use --no-verify to bypass if intentional)" -ForegroundColor Red
    exit 1
} else {
    Write-Host "[pre-commit] OK" -ForegroundColor DarkGreen
    exit 0
}
