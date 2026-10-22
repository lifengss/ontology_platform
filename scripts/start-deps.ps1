# Start the three backend dependency services (Jena/Fuseki 3030, Neo4j 7474/7687, Postgres 5432)
# and verify reachability of each. Idempotent: a reachable service is skipped.
# If any service is still unreachable at the end -> exit code 1 (treat as startup/test FAILURE.
# Only a dedicated fallback/degradation test may intentionally run against degraded services).
# Usage: . scripts\start-deps.ps1 [-Force]
#
# NOTE: messages are ASCII-only on purpose. PowerShell 5.1 reads .ps1 without BOM as ANSI,
# and multi-byte UTF-8 Chinese text corrupts line parsing (seen in the field).

param(
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$ROOT = Split-Path -Parent $PSScriptRoot
$DEPS = Join-Path $ROOT "deps"
$JENA = Join-Path $DEPS "apache-jena-fuseki-5.1.0"
$NEO4J = Get-ChildItem -Path $DEPS -Filter "neo4j-community-*" -Directory -ErrorAction SilentlyContinue |
    Sort-Object Name -Descending | Select-Object -First 1
$PG = Join-Path $DEPS "pgsql"
# Canonical PG cluster is deps/pgsql/data, created by setup-local-deps.ps1
# (initdb -U ont --auth=trust + createdb ont). Do NOT use backend/data/pgdata:
# start_mvp_local.ps1 historically initialized that dir without the 'ont' database,
# which made the DSN-based /health probe report postgres=unavailable.
$PGDATA = Join-Path $PG "data"
if (-not (Test-Path (Join-Path $PGDATA "PG_VERSION"))) {
    Write-Host "initializing PG cluster at $PGDATA ..." -ForegroundColor Cyan
    $null = & cmd /c "`"$PG\bin\initdb.exe`" -D `"$PGDATA`" -U ont --auth=trust --no-instructions --encoding=UTF8 --locale=C >nul 2>&1"
}
$JENA_CFG = Join-Path $ROOT "backend\fuseki-config.ttl"

function Test-Http($url) {
    try {
        $r = Invoke-WebRequest -Uri $url -TimeoutSec 3 -UseBasicParsing -ErrorAction Stop
        return $r.StatusCode -lt 500
    } catch { return $false }
}
function Test-Tcp($target, $port) {
    # NOTE: parameter must not be named $host (reserved read-only automatic variable in PowerShell)
    try {
        $tcp = New-Object System.Net.Sockets.TcpClient
        $tcp.Connect($target, $port)
        $tcp.Close()
        return $true
    } catch { return $false }
}
function Test-PgDb {
    # Real DSN-equivalent check (user ont -> database ont), same semantics as the
    # backend /health psycopg2 probe. A bare TCP open is NOT enough: the 'ont'
    # database itself may be missing.
    $out = & cmd /c "`"$PG\bin\psql.exe`" -h 127.0.0.1 -p 5432 -U ont -d ont -tAc "select 1" 2>nul"
    return ("$out".Trim() -eq '1')
}
function Wait-Until($script, $timeoutSec, $label) {
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $lastTick = 0
    while ($sw.Elapsed.TotalSeconds -lt $timeoutSec) {
        if (& $script) { Write-Host "[OK]   $label ready ($([int]$sw.Elapsed.TotalSeconds)s)" -ForegroundColor Green; return $true }
        if ([int]$sw.Elapsed.TotalSeconds - $lastTick -ge 15) {
            $lastTick = [int]$sw.Elapsed.TotalSeconds
            Write-Host "       ... $label waiting ($lastTick s / $timeoutSec s)" -ForegroundColor DarkGray
        }
        Start-Sleep -Seconds 2
    }
    Write-Host "[FAIL] $label not ready within ${timeoutSec}s" -ForegroundColor Red
    return $false
}

# JDK (Jena 5 / Neo4j 2026 require 21+; prefer the bundled one)
$localJdk = Get-ChildItem -Path $DEPS -Filter "jdk-*" -Directory -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -match 'jdk-2[1-9]' } | Sort-Object Name -Descending | Select-Object -First 1
$javaExe = "java"
if ($localJdk) {
    $env:JAVA_HOME = $localJdk.FullName
    $env:PATH = "$($localJdk.FullName)\bin;$env:PATH"
    $javaExe = Join-Path $localJdk.FullName "bin\java.exe"
    Write-Host "JDK: $($localJdk.FullName)" -ForegroundColor Cyan
}

# ---- -Force: stop existing dependency processes ----
if ($Force) {
    Get-CimInstance Win32_Process -Filter "Name='java.exe'" -ErrorAction SilentlyContinue | ForEach-Object {
        if ($_.CommandLine -match 'fuseki-server|neo4j') {
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
            Write-Host "[INFO] stopped java dependency PID $($_.ProcessId)" -ForegroundColor Yellow
        }
    }
    if (Test-Tcp "127.0.0.1" 5432) {
        & "$PG\bin\pg_ctl.exe" stop -D "$PGDATA" -m fast | Out-Null
        Write-Host "[INFO] stopped Postgres" -ForegroundColor Yellow
    }
}

$allOk = $true

# Probe via 127.0.0.1 (avoids IPv6/localhost DNS ordering quirks).
# NOTE: probe the Fuseki ROOT url. 'GET /ontology' (no trailing slash) returns 400 by design,
# which would make a running Fuseki look unreachable.
# ---- 1. Jena/Fuseki (3030) ----
if (Test-Http "http://127.0.0.1:3030/") {
    Write-Host "[SKIP] Jena/Fuseki already up" -ForegroundColor Cyan
} else {
    Write-Host "starting Jena/Fuseki..." -ForegroundColor Cyan
    $env:FUSEKI_BASE = Join-Path $JENA "run"
    Start-Process -WindowStyle Hidden -WorkingDirectory $JENA -FilePath $javaExe `
        -ArgumentList "-jar `"$JENA\fuseki-server.jar`" --port 3030 --config `"$JENA_CFG`""
    # TDB2 recovery after an unclean shutdown can exceed 2 min -> generous budget
    if (-not (Wait-Until { Test-Http "http://127.0.0.1:3030/" } 240 "Jena/Fuseki(3030)")) { $allOk = $false }
}

# ---- 2. Neo4j (7474 HTTP / 7687 bolt) ----
if (Test-Http "http://127.0.0.1:7474/") {
    Write-Host "[SKIP] Neo4j already up" -ForegroundColor Cyan
} else {
    if (-not $NEO4J) {
        Write-Host "[FAIL] deps/neo4j-community-* not found" -ForegroundColor Red
        $allOk = $false
    } else {
        Write-Host "starting Neo4j..." -ForegroundColor Cyan
        # initial password (only effective on first run; errors are expected afterwards).
        # cmd /c + full stream discard: neo4j-admin prints JDK warnings to stderr, which
        # aborts the script under $ErrorActionPreference='Stop' when captured directly.
        $null = & cmd /c "`"$($NEO4J.FullName)\bin\neo4j-admin.bat`" dbms set-initial-password neo4jsecret >nul 2>&1"
        Start-Process -WindowStyle Hidden -WorkingDirectory "$($NEO4J.FullName)\bin" -FilePath "powershell.exe" `
            -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$($NEO4J.FullName)\bin\neo4j.ps1`" console"
        if (-not (Wait-Until { Test-Http "http://127.0.0.1:7474/" } 180 "Neo4j(7474)")) { $allOk = $false }
    }
}

# ---- 3. Postgres (5432, database 'ont') ----
if (Test-PgDb) {
    Write-Host "[SKIP] Postgres already up (ont db reachable)" -ForegroundColor Cyan
} else {
    if (-not (Test-Tcp "127.0.0.1" 5432)) {
        Write-Host "starting Postgres..." -ForegroundColor Cyan
        & "$PG\bin\pg_ctl.exe" start -D "$PGDATA" -l "$ROOT\backend\data\postgres.log" | Out-Null
        if (-not (Wait-Until { Test-Tcp "127.0.0.1" 5432 } 60 "Postgres(5432)")) { $allOk = $false }
    }
    # self-heal: ensure the 'ont' database exists (setup-local-deps.ps1 creates it)
    if (-not (Test-PgDb)) {
        Write-Host "creating database 'ont'..." -ForegroundColor Cyan
        $null = & cmd /c "`"$PG\bin\createdb.exe`" -h 127.0.0.1 -p 5432 -U ont ont >nul 2>&1"
    }
    if (-not (Wait-Until { Test-PgDb } 30 "Postgres(ont db)")) { $allOk = $false }
}

# ---- summary ----
Write-Host ""
$j = Test-Http "http://127.0.0.1:3030/"
$n = (Test-Http "http://127.0.0.1:7474/") -and (Test-Tcp "127.0.0.1" 7687)
$p = Test-PgDb
Write-Host ("  Jena    : " + $(if ($j) { "REACHABLE" } else { "UNREACHABLE" })) -ForegroundColor $(if ($j) { 'Green' } else { 'Red' })
Write-Host ("  Neo4j   : " + $(if ($n) { "REACHABLE (http+bolt)" } else { "UNREACHABLE" })) -ForegroundColor $(if ($n) { 'Green' } else { 'Red' })
Write-Host ("  Postgres: " + $(if ($p) { "REACHABLE" } else { "UNREACHABLE" })) -ForegroundColor $(if ($p) { 'Green' } else { 'Red' })
if ($allOk -and $j -and $n -and $p) {
    Write-Host "All 3 dependency services reachable." -ForegroundColor Green
    exit 0
} else {
    Write-Host "Unreachable dependency detected: startup/test FAILED (only dedicated degradation tests may run degraded)." -ForegroundColor Red
    exit 1
}
