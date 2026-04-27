# verify_fb_contact_events — 验证 Phase 6 production sanitize fix 真实生效
#
# 背景:
#   PR #134 (sanitize fix) 删了 _is_valid_peer_name 的 ASCII 启发误杀规则,
#   理论上让英文名用户 (Alice/Bob/Mike/Sarah) 进入 fb_contact_events.
#   本脚本查主控 SQLite (data/openclaw.db) 验证近 24h 写入分布.
#
# 使用 (主控 cwd 跑):
#   verify_fb_contact_events.bat            # 默认看主控 SQLite 24h
#   verify_fb_contact_events.bat -Hours 6   # 看近 6h
#   verify_fb_contact_events.bat -Top 30    # 列出 last 30 行
#
# 期望输出 (P1.7 fix 生效后):
#   [OK] 24h_total > 0
#   [OK] ascii_names > 0  (英文名真的写入了 — Alice/Bob/Sarah 等)
#   [OK] reject_count 比 fix 前显著降低 (理想 0)

param(
    [int]$Hours = 24,
    [int]$Top = 10
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $ProjectRoot

$dbPath = Join-Path $ProjectRoot "data\openclaw.db"
if (-not (Test-Path $dbPath)) {
    Write-Host "[ERROR] $dbPath not found" -ForegroundColor Red
    exit 1
}

Write-Host "==========================================="
Write-Host "  fb_contact_events Verification (last ${Hours}h)"
Write-Host "==========================================="
Write-Host "  DB: $dbPath"
Write-Host ""

# Use python sqlite3 module (most portable on Windows)
$pyScript = @"
import sqlite3, json, sys, os
db_path = r'$dbPath'
hours = $Hours
top_n = $Top

conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row

# Statistics
cutoff = f"datetime('now', '-{hours} hours')"
total = conn.execute(f"SELECT COUNT(*) FROM fb_contact_events WHERE at >= {cutoff}").fetchone()[0]

# ASCII vs non-ASCII peer_name (英文 vs 中日韩文)
ascii_count = conn.execute(f"""
    SELECT COUNT(*) FROM fb_contact_events
    WHERE at >= {cutoff}
      AND peer_name GLOB '*[A-Za-z]*'
      AND peer_name NOT GLOB '*[一-龥ぁ-んァ-ヶ가-힣]*'
""").fetchone()[0]

non_ascii_count = total - ascii_count

# Reject count (sanitize 拒了多少)
try:
    reject_total = conn.execute("SELECT total FROM peer_name_reject_metrics LIMIT 1").fetchone()
    reject_total = reject_total[0] if reject_total else 0
except Exception:
    reject_total = -1  # 表不存在

# Last N events
rows = conn.execute(f"""
    SELECT at, event_type, peer_name, device_id, template_id
    FROM fb_contact_events
    WHERE at >= {cutoff}
    ORDER BY at DESC
    LIMIT {top_n}
""").fetchall()

# Output JSON for PowerShell consumption
result = {
    "total_24h": total,
    "ascii_count": ascii_count,
    "non_ascii_count": non_ascii_count,
    "reject_total_alltime": reject_total,
    "recent_events": [dict(r) for r in rows],
}
sys.stdout.reconfigure(encoding='utf-8')
print(json.dumps(result, ensure_ascii=False, indent=2))
"@

$jsonOutput = python -c $pyScript 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] Python query failed:" -ForegroundColor Red
    Write-Host $jsonOutput
    exit 1
}

try {
    $data = $jsonOutput | ConvertFrom-Json
} catch {
    Write-Host "[ERROR] JSON parse failed. Raw output:" -ForegroundColor Red
    Write-Host $jsonOutput
    exit 1
}

# Summary
Write-Host "[Summary]" -ForegroundColor Cyan
Write-Host ("  Total events ({0}h):    {1}" -f $Hours, $data.total_24h)
$tone = if ($data.ascii_count -gt 0) { 'Green' } else { 'DarkYellow' }
Write-Host ("  ASCII (英文) names:     {0}" -f $data.ascii_count) -ForegroundColor $tone
Write-Host ("  Non-ASCII (中日) names: {0}" -f $data.non_ascii_count)
$rTone = if ($data.reject_total_alltime -eq 0) { 'Green' }
         elseif ($data.reject_total_alltime -lt 0) { 'DarkGray' }
         else { 'Yellow' }
$rText = if ($data.reject_total_alltime -lt 0) { "(metrics 表未建)" } else { $data.reject_total_alltime }
Write-Host ("  Reject count (all-time): {0}" -f $rText) -ForegroundColor $rTone
Write-Host ""

# Recent events
Write-Host "[Recent ${Top} events]" -ForegroundColor Cyan
if ($data.recent_events.Count -eq 0) {
    Write-Host "  (no events in last ${Hours}h)" -ForegroundColor DarkYellow
} else {
    $data.recent_events | ForEach-Object {
        $isAscii = $_.peer_name -cmatch '^[A-Za-z\s\.\-_]+$'
        $marker = if ($isAscii) { '[EN]' } else { '[CJK]' }
        Write-Host ("  {0} {1,-12} {2,-22} {3} ({4})" -f `
            $marker, $_.event_type, $_.peer_name, $_.device_id, $_.at)
    }
}
Write-Host ""

# Verdict
Write-Host "[Verdict]" -ForegroundColor Cyan
if ($data.total_24h -eq 0) {
    Write-Host "  [SKIP] 0 events in last ${Hours}h, 真业务还未跑过. 跑 1 个 facebook_add_friend / send_greeting task 后再来看." -ForegroundColor DarkYellow
} elseif ($data.ascii_count -gt 0) {
    Write-Host "  [OK] PR #134 sanitize fix 已生效 — $($data.ascii_count) 个英文名 peer 已写入" -ForegroundColor Green
    Write-Host "       (fix 前 ASCII 启发会全部拒掉, 此数应为 0)" -ForegroundColor DarkGreen
} else {
    Write-Host "  [INFO] 仅 $($data.non_ascii_count) 个中日韩名写入, 无英文名 — 可能业务对象都是中日用户" -ForegroundColor Yellow
    Write-Host "       (不一定意味 fix 失败, 取决于业务真实分布)" -ForegroundColor DarkYellow
}
