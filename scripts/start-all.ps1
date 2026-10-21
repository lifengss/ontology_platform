# 企业业务本体平台 - Windows 一键启动脚本
# 启动依赖（Jena/Fuseki、Neo4j、Postgres）+ 后端 FastAPI + 前端静态服务
# 用法：右键“使用 PowerShell 运行” 或在 PowerShell 中执行 .\\scripts\\start-all.ps1

param(
    [switch]$SkipDeps,      # 只启动前后端，不启动依赖
    [switch]$StopDeps,      # 先停止并删除现有容器再启动（Docker 模式）
    [switch]$UseDocker,     # 强制使用 Docker 启动依赖
    [string]$BackendPort = "8000",
    [string]$FrontendPort = "8123"
)

$ErrorActionPreference = "Stop"

$ROOT = Split-Path -Parent $PSScriptRoot
$BACKEND = Join-Path $ROOT "backend"
$FRONTEND = Join-Path $ROOT "frontend"
$DEPS = Join-Path $ROOT "deps"

function Info($msg) { Write-Host "[INFO] $msg" -ForegroundColor Cyan }
function OK($msg) { Write-Host "[OK]   $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "[WARN] $msg" -ForegroundColor Yellow }

function Wait-Http($url, $timeoutSec = 120) {
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    while ($sw.Elapsed.TotalSeconds -lt $timeoutSec) {
        try {
            $r = Invoke-RestMethod -Uri $url -TimeoutSec 2 -ErrorAction SilentlyContinue
            if ($r) { return $true }
        } catch {}
        Start-Sleep -Milliseconds 500
    }
    return $false
}

function Get-LocalJenaPath {
    $candidates = Get-ChildItem -Path $DEPS -Filter "apache-jena-fuseki-*" -Directory -ErrorAction SilentlyContinue
    if ($candidates) { return $candidates | Sort-Object Name -Descending | Select-Object -First 1 }
    return $null
}

function Get-LocalNeo4jPath {
    $candidates = Get-ChildItem -Path $DEPS -Filter "neo4j-community-*" -Directory -ErrorAction SilentlyContinue
    if ($candidates) { return $candidates | Sort-Object Name -Descending | Select-Object -First 1 }
    return $null
}

function Get-LocalPostgresPath {
    $p = Join-Path $DEPS "pgsql"
    if (Test-Path $p) { return $p }
    return $null
}

function Run-Silently($cmd) {
    $ea = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    & $cmd 2>&1 | Out-Null
    $ErrorActionPreference = $ea
}

# 1. 停止旧的前后端正进程
Info "查找并停止旧的前后端正进程..."
Get-Process -Name python -ErrorAction SilentlyContinue | ForEach-Object {
    try {
        $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId=$($_.Id)").CommandLine
        if ($cmd -match "main\\.py" -or $cmd -match "http\\.server\\s+$FrontendPort") {
            Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
            OK "已停止 PID $($_.Id)"
        }
    } catch {}
}

# 1.5 停止旧本地依赖进程（Java）
if (-not $SkipDeps) {
    Get-Process -Name java -ErrorAction SilentlyContinue | ForEach-Object {
        try {
            $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId=$($_.Id)").CommandLine
            if ($cmd -match "fuseki-server" -or $cmd -match "neo4j" -or $cmd -match "neo4j\\.jar") {
                Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
                OK "已停止本地依赖 PID $($_.Id)"
            }
        } catch {}
    }
}

$depProcs = @()

# 2. 依赖服务
if (-not $SkipDeps) {
    $dockerAvailable = [bool](Get-Command docker -ErrorAction SilentlyContinue)

    if ($UseDocker -and -not $dockerAvailable) {
        Warn "UseDocker 已设置但未检测到 docker 命令，将尝试本地模式。"
        $UseDocker = $false
    }

    if ($UseDocker) {
        if ($StopDeps) {
            Info "停止并移除旧容器..."
            docker compose -f "$ROOT\\docker-compose.yml" down 2>&1 | Out-Null
        }
        Info "启动依赖服务（Docker 模式）..."
        docker compose -f "$ROOT\\docker-compose.yml" up -d jena neo4j postgres 2>&1 | Out-Null

        Info "等待 Jena/Fuseki (3030)..."
        if (Wait-Http "http://localhost:3030/" 120) { OK "Jena 就绪" } else { Warn "Jena 未在 120 秒内就绪" }

        Info "等待 Neo4j (7474)..."
        if (Wait-Http "http://localhost:7474/" 120) { OK "Neo4j 就绪" } else { Warn "Neo4j 未在 120 秒内就绪" }

        Info "等待 Postgres (5432)..."
        $pgReady = $false
        for ($i = 0; $i -lt 60; $i++) {
            try {
                $tcp = New-Object System.Net.Sockets.TcpClient
                $tcp.Connect("localhost", 5432)
                $tcp.Close()
                $pgReady = $true; break
            } catch { Start-Sleep -Seconds 1 }
        }
        if ($pgReady) { OK "Postgres 就绪" } else { Warn "Postgres 未在 60 秒内就绪" }
    } else {
        $jena = Get-LocalJenaPath
        $neo4j = Get-LocalNeo4jPath
        $pg = Get-LocalPostgresPath
        if (-not $jena -or -not $pg) {
            Warn "未找到本地 Jena/Fuseki 或 Postgres。"
            Warn "请先运行安装脚本：.\\scripts\\setup-local-deps.ps1"
            Warn "或安装 Docker 后使用 -UseDocker 参数。"
            Warn "本次将仅启动前后端（部分功能降级）。"
        } else {
            Info "启动本地 Jena/Fuseki..."
            $fusekiCmd = Join-Path $jena.FullName "fuseki-server.bat"
            $fusekiProc = Start-Process -FilePath $fusekiCmd -ArgumentList "--mem","--update","/ontology" `
                -WorkingDirectory $jena.FullName -WindowStyle Hidden -PassThru
            $depProcs += $fusekiProc

            if ($neo4j) {
                Info "启动本地 Neo4j..."
                $neo4jCmd = Join-Path $neo4j.FullName "bin\\neo4j.bat"
                $neo4jProc = Start-Process -FilePath $neo4jCmd -ArgumentList "console" `
                    -WorkingDirectory $neo4j.FullName -WindowStyle Hidden -PassThru
                $depProcs += $neo4jProc
            } else {
                Warn "未找到本地 Neo4j，跳过。"
            }

            Info "启动本地 Postgres..."
            $pgData = Join-Path $pg "data"
            $pgCtl = Join-Path $pg "bin\\pg_ctl.exe"
            $pgProc = Start-Process -FilePath $pgCtl -ArgumentList "-D","$pgData","-l","$pg\\postgres.log","start" `
                -WindowStyle Hidden -PassThru
            $depProcs += $pgProc

            Info "等待 Jena/Fuseki (3030)..."
            if (Wait-Http "http://localhost:3030/" 120) { OK "Jena 就绪" } else { Warn "Jena 未在 120 秒内就绪" }

            if ($neo4j) {
                Info "等待 Neo4j (7474)..."
                if (Wait-Http "http://localhost:7474/" 120) { OK "Neo4j 就绪" } else { Warn "Neo4j 未在 120 秒内就绪" }
            }

            Info "等待 Postgres (5432)..."
            $pgReady = $false
            for ($i = 0; $i -lt 60; $i++) {
                try {
                    $tcp = New-Object System.Net.Sockets.TcpClient
                    $tcp.Connect("localhost", 5432)
                    $tcp.Close()
                    $pgReady = $true; break
                } catch { Start-Sleep -Seconds 1 }
            }
            if ($pgReady) { OK "Postgres 就绪" } else { Warn "Postgres 未在 60 秒内就绪" }
        }
    }
} else {
    Info "SkipDeps 已设置，跳过依赖服务。"
}

# 3. 启动后端
Info "启动后端 FastAPI (端口 $BackendPort)..."
$backendEnv = [System.Environment]::GetEnvironmentVariables("Process")
$backendEnv["JENA_BASE_URL"] = "http://localhost:3030"
$backendEnv["JENA_DATASET"] = "ontology"
$backendEnv["NEO4J_URI"] = "bolt://localhost:7687"
$backendEnv["NEO4J_USER"] = "neo4j"
$backendEnv["NEO4J_PASSWORD"] = "neo4jsecret"
$backendEnv["POSTGRES_DSN"] = "postgresql://ont:ontsecret@localhost:5432/ont"

$backendProc = Start-Process -FilePath python -ArgumentList (Join-Path $BACKEND "main.py") `
    -WorkingDirectory $BACKEND -WindowStyle Hidden -PassThru -Environment $backendEnv

# 4. 启动前端
Info "启动前端静态服务 (端口 $FrontendPort)..."
$frontendProc = Start-Process -FilePath python -ArgumentList "-m","http.server",$FrontendPort `
    -WorkingDirectory $FRONTEND -WindowStyle Hidden -PassThru

# 5. 等待后端健康
Info "等待后端健康检查..."
if (Wait-Http "http://localhost:$BackendPort/health" 60) { OK "后端已响应" } else { Warn "后端未在 60 秒内响应" }

# 6. 输出访问信息
Write-Host ""
OK "所有服务已启动"
Write-Host "  前端页面: http://localhost:$FrontendPort" -ForegroundColor White
Write-Host "  后端 API: http://localhost:$BackendPort" -ForegroundColor White
Write-Host "  后端文档: http://localhost:$BackendPort/docs" -ForegroundColor White
Write-Host "  健康检查: http://localhost:$BackendPort/health" -ForegroundColor White
Write-Host ""
Write-Host "按任意键将停止前后端并退出..." -ForegroundColor Gray
[void][System.Console]::ReadKey($true)

# 7. 停止前后端
Info "停止前后端进程..."
Stop-Process -Id $backendProc.Id -Force -ErrorAction SilentlyContinue
Stop-Process -Id $frontendProc.Id -Force -ErrorAction SilentlyContinue

# 8. 停止本地依赖
if ($depProcs.Count -gt 0) {
    Info "停止本地依赖进程..."
    $depProcs | ForEach-Object { Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue }
}

# 9. 停止 Docker 依赖（如果用了 Docker）
if ($UseDocker -and (Get-Command docker -ErrorAction SilentlyContinue)) {
    Info "停止 Docker 依赖..."
    docker compose -f "$ROOT\\docker-compose.yml" down 2>&1 | Out-Null
}

OK "已清理，退出"
