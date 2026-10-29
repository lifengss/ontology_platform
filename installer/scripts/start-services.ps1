# start-services.ps1 — 按 runtime-config.json 启动全部服务（可重复执行：先按端口清理旧实例）
# 兼容 PowerShell 5.1。
param(
    [string]$InstallDir = ""
)
$ErrorActionPreference = "Stop"
if (-not $InstallDir) { $InstallDir = Split-Path -Parent $PSScriptRoot }
$cfgPath = Join-Path $PSScriptRoot "runtime-config.json"
if (-not (Test-Path $cfgPath)) { Write-Host "未找到 runtime-config.json，请先运行安装（install-deps.ps1）" -ForegroundColor Red; exit 1 }
$cfg = Get-Content $cfgPath -Raw | ConvertFrom-Json
$P = $cfg.ports
$logsDir = Join-Path $InstallDir "logs"
New-Item -ItemType Directory -Path $logsDir -Force | Out-Null

Write-Host "===== 启动 企业业务本体平台 =====" -ForegroundColor Cyan
Write-Host ("安装目录: {0}" -f $InstallDir)
Write-Host ("端口: backend={0} frontend={1} fuseki={2} neo4j={3}/{4} postgres={5}" -f $P.backend, $P.frontend, $P.fuseki, $P.neo4jHttp, $P.neo4jBolt, $P.postgres)

# Java 环境（config 中为斜杠路径，JAVA_HOME/PATH 还原反斜杠以兼容 .bat）
if ($cfg.java.home) {
    $javaHomeLocal = $cfg.java.home -replace '/', '\'
    $env:JAVA_HOME = $javaHomeLocal
    $env:PATH = "$javaHomeLocal\bin;$env:PATH"
}
# Postgres bin 入 PATH
$pgBin = Join-Path $InstallDir "deps\pgsql\bin"
$env:PATH = "$pgBin;$env:PATH"
$venvPython = Join-Path $InstallDir "runtime\venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) { Write-Host "未找到 venv Python：$venvPython（请重新运行安装）" -ForegroundColor Red; exit 1 }

function Stop-ByPort([int]$port) {
    $conns = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if ($conns) {
        $pids = $conns | Select-Object -ExpandProperty OwningProcess -Unique
        foreach ($procId in $pids) {
            try { Stop-Process -Id $procId -Force -ErrorAction Stop; Write-Host "  已停止占用端口 $port 的进程 PID=$procId" } catch {}
        }
    }
}
function Wait-Port([int]$port, [int]$seconds = 60) {
    for ($i = 0; $i -lt $seconds; $i++) {
        try {
            $c = New-Object System.Net.Sockets.TcpClient
            $c.Connect("127.0.0.1", $port)
            $c.Close()
            return $true
        } catch { Start-Sleep -Seconds 1 }
    }
    return $false
}
# 等待 Neo4j 就绪：进度条 + 快速失败。
# 真机事故（2026-10-29）：配置错误致 Neo4j 秒退，旧逻辑盲等 300s 且无输出（用户以为死机）。
# 现行为：每 10s 打印一条进度；进程存活 15s 宽限（bat→cmd→java 启动延迟）后若 java 进程
# 已退出 → 立即判定启动失败并打印启动日志尾部，不再空等。
function Show-LogTail([string]$f) {
    if (Test-Path $f) {
        $sz = (Get-Item $f).Length
        if ($sz -gt 0) {
            Write-Host ("  日志尾部（{0}）：" -f $f) -ForegroundColor Yellow
            Get-Content $f -Tail 15 | ForEach-Object { Write-Host "    $_" }
        }
    }
}
function Wait-Neo4j([string]$dir, [int]$port, [string]$consoleLog, [int]$seconds) {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $lastTick = -1
    while ($sw.Elapsed.TotalSeconds -lt $seconds) {
        try {
            $c = New-Object System.Net.Sockets.TcpClient
            $c.Connect("127.0.0.1", $port)
            $c.Close()
            return $true
        } catch {}
        if ($sw.Elapsed.TotalSeconds -ge 15) {
            $alive = Get-CimInstance Win32_Process -Filter "Name='java.exe'" -ErrorAction SilentlyContinue |
                Where-Object { $_.CommandLine -and ($_.CommandLine -like "*$dir*") }
            if (-not $alive) {
                Write-Host ("  Neo4j 进程已退出（耗时 {0:N0}s）——判定为启动失败，不再等待" -f $sw.Elapsed.TotalSeconds) -ForegroundColor Red
                Show-LogTail $consoleLog
                Show-LogTail (Join-Path $dir "logs\debug.log")
                return $false
            }
        }
        $tick = [int]($sw.Elapsed.TotalSeconds / 10)
        if ($tick -ne $lastTick) {
            $lastTick = $tick
            $fill = [Math]::Min(20, [int]($sw.Elapsed.TotalSeconds / $seconds * 20))
            Write-Host ("  Neo4j 启动中 [{0}{1}] {2:N0}s/{3}s（首次启动较慢，请耐心等待）" -f ('#' * $fill), ('-' * (20 - $fill)), $sw.Elapsed.TotalSeconds, $seconds)
        }
        Start-Sleep -Seconds 1
    }
    Write-Host ("Neo4j 启动超时（{0}s）" -f $seconds) -ForegroundColor Red
    Show-LogTail $consoleLog
    Show-LogTail (Join-Path $dir "logs\debug.log")
    return $false
}
# 等待 Fuseki 就绪：端口 + 数据集真实探活（ping）。
# 真机事故（2026-10-29）：TDB 数据目录不可写（C:\Program Files\... 下 Users 仅读+执行）→
# 数据集初始化失败但端口照常监听 → 旧逻辑 Wait-Port 判"就绪" → 后端探针报 jena unavailable。
# 端口就绪 ≠ 服务可用：ping 返回 200 才算就绪；失败时做写权限诊断给出明确原因。
function Wait-Fuseki([int]$port, [string]$installDir, [string]$consoleLog, [string]$ttl) {
    # 数据集名从安装后的 ttl 读取（与后端 JENA_DATASET=ontology 保持一致）
    $ds = "ontology"
    if (Test-Path $ttl) {
        $m = [regex]::Match([System.IO.File]::ReadAllText($ttl), 'fuseki:name\s+"([^"]+)"')
        if ($m.Success) { $ds = $m.Groups[1].Value }
    }
    if (-not (Wait-Port $port 60)) {
        Write-Host "  Fuseki 端口未监听（60s）——判定启动失败" -ForegroundColor Red
        Show-LogTail $consoleLog
        Show-LogTail (Join-Path $installDir "logs\fuseki-console.err.log")
        return $false
    }
    $pingUrl = "http://127.0.0.1:" + $port + "/" + $ds + "/ping"
    Write-Host "  Fuseki 端口已监听，数据集 $ds 真实探活（$pingUrl）..."
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $lastTick = -1
    $lastStatus = 0
    while ($sw.Elapsed.TotalSeconds -lt 60) {
        try {
            $req = [System.Net.HttpWebRequest]::Create($pingUrl)
            $req.Method = "GET"
            $req.Timeout = 3000
            $req.ReadWriteTimeout = 3000
            $resp = $req.GetResponse()
            $code = [int]$resp.StatusCode
            $resp.Close()
            if ($code -eq 200) { return $true }
            $lastStatus = $code
        } catch [System.Net.WebException] {
            $r = $_.Exception.Response
            if ($r) { $lastStatus = [int]$r.StatusCode; $r.Close() }
        } catch {}
        $tick = [int]($sw.Elapsed.TotalSeconds / 10)
        if ($tick -ne $lastTick) {
            $lastTick = $tick
            Write-Host ("  Fuseki 探活中... {0:N0}s/60s（上次状态={1}）" -f $sw.Elapsed.TotalSeconds, $lastStatus)
        }
        Start-Sleep -Seconds 2
    }
    Write-Host ("Fuseki 数据集不可用（端口监听中，但 ping 状态={0}）——不判就绪" -f $lastStatus) -ForegroundColor Red
    # 根因诊断：TDB 数据目录是否可写（真机事故根因）
    $tdb = Join-Path $installDir "backend\data\fuseki"
    $writable = $false
    try {
        New-Item -ItemType Directory -Path $tdb -Force -ErrorAction Stop | Out-Null
        $probeFile = Join-Path $tdb (".wprobe_" + [Guid]::NewGuid().ToString("N").Substring(0, 8))
        [System.IO.File]::WriteAllText($probeFile, "ok")
        Remove-Item $probeFile -Force
        $writable = $true
    } catch {}
    if (-not $writable) {
        Write-Host ("  根因: TDB 数据目录不可写: {0}" -f $tdb) -ForegroundColor Yellow
        Write-Host "  （C:\Program Files\... 默认 Users 仅读+执行——重跑安装器完成 7.5 步授权，或手动 icacls 授予当前用户完全控制后重启）" -ForegroundColor Yellow
    }
    Show-LogTail $consoleLog
    Show-LogTail (Join-Path $installDir "logs\fuseki-console.err.log")
    return $false
}

# 1) 先清理本安装目录可能残留的旧实例（按当前配置端口 + 按命令行匹配本目录进程）
Write-Host "[1/5] 清理旧实例（若存在）..."
foreach ($port in @($P.backend, $P.frontend, $P.fuseki, $P.neo4jHttp, $P.neo4jBolt, $P.postgres)) { Stop-ByPort $port }
# 兜底：上次安装/运行若分配过不同端口，按端口杀不到；凡命令行包含本安装目录的
# java/python 进程均属于本安装，直接清理（开发态进程在别的目录，不受影响）
Get-CimInstance Win32_Process -Filter "Name='java.exe' OR Name='javaw.exe' OR Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and ($_.CommandLine -like "*$InstallDir*") } | ForEach-Object {
        Write-Host "  已停止残留进程 PID=$($_.ProcessId) ($($_.Name))"
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
Start-Sleep -Seconds 2
$stillBusy = @()
foreach ($port in @($P.backend, $P.frontend, $P.fuseki, $P.neo4jHttp, $P.neo4jBolt, $P.postgres)) {
    $conns = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if ($conns) { $stillBusy += $port }
}
if ($stillBusy.Count -gt 0) {
    Write-Host "端口仍被其他程序占用：$($stillBusy -join ', ')。请关闭占用程序或重新运行安装器自动重分配端口。" -ForegroundColor Red
    exit 1
}

# 2) 依赖服务
Write-Host "[2/5] 启动 Fuseki / Neo4j / Postgres ..."
$jenaDir = Join-Path $InstallDir "deps\apache-jena-fuseki-5.1.0"
$ttl = Join-Path $InstallDir "backend\fuseki-config.ttl"
# 控制台输出重定向到日志：隐藏窗口 + 不重定向 → 数据集初始化失败的报错无人可见
# （真机事故：TDB 目录不可写 → 数据集 503，端口照常监听，旧逻辑误判就绪）
$fusekiConsoleLog = Join-Path $logsDir "fuseki-console.log"
Start-Process (Join-Path $jenaDir "fuseki-server.bat") -ArgumentList "--port $($P.fuseki) --config `"$ttl`"" -WorkingDirectory $jenaDir -WindowStyle Hidden -RedirectStandardOutput $fusekiConsoleLog -RedirectStandardError (Join-Path $logsDir "fuseki-console.err.log")
Write-Host "  Fuseki :$($P.fuseki) 启动中（端口 + 数据集 ping 真实探活；端口就绪 ≠ 服务可用）..."
if (-not (Wait-Fuseki $P.fuseki $InstallDir $fusekiConsoleLog $ttl)) { exit 1 }
Write-Host "  Fuseki 就绪（数据集 ping OK）"

$n4jDir = Get-ChildItem (Join-Path $InstallDir "deps") -Directory -Filter "neo4j-community-*" | Select-Object -First 1
if (-not $n4jDir) { Write-Host "未找到 Neo4j 目录" -ForegroundColor Red; exit 1 }
# 控制台输出重定向到日志：隐藏窗口跑 neo4j.bat console，失败时若不重定向就无人可见
# 报错（真机事故：-Djava.io.tmpdir 未加引号 → ClassNotFoundException 静默退出）。
$n4jConsoleLog = Join-Path $n4jDir.FullName "logs\neo4j-console.log"
Start-Process (Join-Path $n4jDir.FullName "bin\neo4j.bat") -ArgumentList "console" -WorkingDirectory $n4jDir.FullName -WindowStyle Hidden -RedirectStandardOutput $n4jConsoleLog -RedirectStandardError (Join-Path $n4jDir.FullName "logs\neo4j-console.err.log")
Write-Host "  Neo4j :$($P.neo4jHttp) 启动中（首次启动通常 1-3 分钟，下方有进度条；失败会立即报错并打印日志）..."
if (-not (Wait-Neo4j $n4jDir.FullName $P.neo4jHttp $n4jConsoleLog 300)) { exit 1 }
Write-Host "  Neo4j 就绪"

$pgData = $cfg.postgres.dataDir
if (-not (Test-Path (Join-Path $pgData "PG_VERSION"))) { Write-Host "Postgres 数据目录未初始化，请先运行安装" -ForegroundColor Red; exit 1 }
# Postgres 启动策略（EDB 版 postgres 拒绝在高完整性令牌下运行）：
#   - 非提升态（双击 bat 的常态）：直接启动 postgres.exe
#   - 提升态（右键"以管理员身份运行"）：经一次性计划任务以 LIMITED（中完整性）身份运行
# 数据/日志同在 C:\ProgramData\OntologyPlatform（非提升态可写，见 install-deps.ps1 第 4 节注释）。
$pgDataRoot = Join-Path $env:ProgramData "OntologyPlatform"
$pgLogDir = Join-Path $pgDataRoot "logs\postgres"
New-Item -ItemType Directory -Force -Path $pgLogDir | Out-Null
$pgExe = Join-Path $pgBin "postgres.exe"
$pgErrLog = Join-Path $pgLogDir "postgres-err.log"
$curUser = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$isElevated = (New-Object System.Security.Principal.WindowsPrincipal($curUser)).IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)
if ($isElevated) {
    $tn = "OntologyPlatform-PG-$($P.postgres)"
    $tr = 'cmd.exe /c "' + $pgExe + '" -D "' + $pgData + '" >> "' + $pgErrLog + '" 2>&1'
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $null = & schtasks.exe /Create /F /TN $tn /SC ONCE /ST 00:00:00 /RL LIMITED /TR $tr 2>&1
    if ($LASTEXITCODE -ne 0) { $ErrorActionPreference = $prevEap; Write-Host "创建降权计划任务失败" -ForegroundColor Red; exit 1 }
    $null = & schtasks.exe /Run /TN $tn 2>&1
    if ($LASTEXITCODE -ne 0) {
        $ErrorActionPreference = $prevEap
        & schtasks.exe /Delete /F /TN $tn 2>&1 | Out-Null
        Write-Host "触发降权计划任务失败" -ForegroundColor Red; exit 1
    }
    $ErrorActionPreference = $prevEap
    Write-Host "  Postgres :$($P.postgres) 经计划任务降权启动（日志 $pgLogDir\）..."
    if (-not (Wait-Port $P.postgres 60)) {
        & schtasks.exe /Delete /F /TN $tn 2>&1 | Out-Null
        Write-Host "Postgres 启动超时，详见 $pgLogDir\ 与 $pgErrLog" -ForegroundColor Red; exit 1
    }
    & schtasks.exe /Delete /F /TN $tn 2>&1 | Out-Null
} else {
    Start-Process -FilePath $pgExe -ArgumentList ('-D "{0}"' -f $pgData) -WorkingDirectory $pgBin -RedirectStandardError $pgErrLog -WindowStyle Hidden
    Write-Host "  Postgres :$($P.postgres) 等待中（日志 $pgLogDir\ 与 $pgErrLog）..."
    if (-not (Wait-Port $P.postgres 60)) { Write-Host "Postgres 启动超时，详见 $logsDir\postgres\ 与 $pgErrLog" -ForegroundColor Red; exit 1 }
}
if (-not (Wait-Port $P.postgres 60)) { Write-Host "Postgres 启动超时，详见 $pgLogDir" -ForegroundColor Red; exit 1 }
Write-Host "  Postgres 就绪"
# 幂等确保 ont 库存在（安装期是提升态无法启动 postgres，故推迟到首次启动时创建）
$prevEap = $ErrorActionPreference
$ErrorActionPreference = "Continue"
$dbExists = & (Join-Path $pgBin "psql.exe") -p $P.postgres -U ont -tAc "SELECT 1 FROM pg_database WHERE datname = 'ont'" 2>$null | Out-String
$ErrorActionPreference = $prevEap
if ("$dbExists" -notmatch "1") {
    Write-Host "  首次启动，创建 ont 库..."
    & (Join-Path $pgBin "createdb.exe") -p $P.postgres -U ont ont
    if ($LASTEXITCODE -ne 0) { Write-Host "createdb ont 失败" -ForegroundColor Red; exit 1 }
    Write-Host "  ont 库已创建"
}

# 3) 后端（端口经 uvicorn 命令行注入，不改源码）
Write-Host "[3/5] 启动后端 FastAPI :$($P.backend) ..."
$env:JENA_BASE_URL = $cfg.jena.baseUrl
$env:JENA_DATASET = $cfg.jena.dataset
$env:NEO4J_URI = "bolt://localhost:$($P.neo4jBolt)"
$env:NEO4J_USER = $cfg.neo4j.user
$env:NEO4J_PASSWORD = $cfg.neo4j.password
$env:POSTGRES_DSN = "postgresql://$($cfg.postgres.user):ontsecret@localhost:$($P.postgres)/$($cfg.postgres.dbname)"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
Start-Process $venvPython -ArgumentList "-m","uvicorn","main:app","--host","0.0.0.0","--port",$P.backend -WorkingDirectory (Join-Path $InstallDir "backend") -WindowStyle Hidden
Write-Host "  等待 /health ..."
$ok = $false
for ($i = 0; $i -lt 90; $i++) {
    Start-Sleep -Seconds 1
    try {
        $r = Invoke-WebRequest -Uri "http://localhost:$($P.backend)/health" -UseBasicParsing -TimeoutSec 3 -ErrorAction Stop
        if ($r.StatusCode -eq 200) { $ok = $true; break }
    } catch {}
}
if (-not $ok) { Write-Host "后端启动超时，详见 $logsDir" -ForegroundColor Red; exit 1 }
Write-Host "  后端就绪：http://localhost:$($P.backend)（API 文档 /docs）"

# 4) 前端静态服务
Write-Host "[4/5] 启动前端 :$($P.frontend) ..."
Start-Process $venvPython -ArgumentList "-m","http.server",$P.frontend -WorkingDirectory (Join-Path $InstallDir "frontend") -WindowStyle Hidden
if (-not (Wait-Port $P.frontend 30)) { Write-Host "前端启动超时" -ForegroundColor Red; exit 1 }
Write-Host "  前端就绪：http://localhost:$($P.frontend)"

# 5) 完成
Write-Host "[5/5] 全部服务已启动" -ForegroundColor Green
Write-Host ""
Write-Host "  平台入口 : http://localhost:$($P.frontend)"
Write-Host "  后端 API : http://localhost:$($P.backend)   Swagger: http://localhost:$($P.backend)/docs"
Write-Host "  Fuseki   : http://localhost:$($P.fuseki)/fuseki"
Write-Host "  Neo4j    : http://localhost:$($P.neo4jHttp)"
Write-Host ""
Write-Host "停止服务：运行安装目录下的 stop-platform.bat"
exit 0
