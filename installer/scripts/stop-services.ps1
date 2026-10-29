# stop-services.ps1 — 按 runtime-config.json 端口停止全部服务（Postgres 优先 pg_ctl stop）
# 兼容 PowerShell 5.1。未监听端口静默跳过。
param(
    [string]$InstallDir = ""
)
$ErrorActionPreference = "Stop"
if (-not $InstallDir) { $InstallDir = Split-Path -Parent $PSScriptRoot }
$cfgPath = Join-Path $PSScriptRoot "runtime-config.json"
if (-not (Test-Path $cfgPath)) { Write-Host "未找到 runtime-config.json（未安装？）" -ForegroundColor Yellow; exit 0 }
$cfg = Get-Content $cfgPath -Raw | ConvertFrom-Json
$P = $cfg.ports
$pgBin = Join-Path $InstallDir "deps\pgsql\bin"
$env:PATH = "$pgBin;$env:PATH"

Write-Host "===== 停止 企业业务本体平台 =====" -ForegroundColor Cyan

# Postgres：pg_ctl 优雅停止（保证 WAL 落盘），失败再按端口 kill
$pgData = $cfg.postgres.dataDir
if (Test-Path (Join-Path $pgData "PG_VERSION")) {
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $r = & (Join-Path $pgBin "pg_ctl.exe") -D $pgData status 2>&1 | Out-String
    $ErrorActionPreference = $prevEap
    if ($r -match "PID[:\s]*(\d+)" -and $r -notmatch "no server running") {
        & (Join-Path $pgBin "pg_ctl.exe") -D $pgData stop -m fast -w | Out-Null
        Write-Host "Postgres :$($P.postgres) 已停止（pg_ctl）"
    } else {
        Write-Host "Postgres 未在运行"
    }
} else {
    Write-Host "Postgres 数据目录不存在，跳过"
}

# 其余服务：按端口找监听进程 kill（同一服务多端口去重）
$seen = @{}
foreach ($name in @("backend", "frontend", "fuseki", "neo4jHttp", "neo4jBolt", "postgres")) {
    $port = $P.$name
    $conns = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if (-not $conns) { Write-Host ("{0,-10} :{1} 未在监听" -f $name, $port); continue }
    $pids = $conns | Select-Object -ExpandProperty OwningProcess -Unique
    foreach ($procId in $pids) {
        if ($seen.ContainsKey($procId)) { continue }
        $seen[$procId] = $true
        try {
            $proc = Get-Process -Id $procId -ErrorAction Stop
            Stop-Process -Id $procId -Force -ErrorAction Stop
            Write-Host ("{0,-10} :{1} 已停止（{2}, PID={3}）" -f $name, $port, $proc.ProcessName, $procId)
        } catch {
            Write-Host ("{0,-10} :{1} 进程 PID={2} 停止失败" -f $name, $port, $procId)
        }
    }
}
Write-Host "===== 停止完成 ====="
exit 0
