# install-deps.ps1 — 本体平台安装期配置主脚本（由 NSIS ExecWait 调用）
# 职责：端口检测/重分配 → 依赖检测与安装（JDK/Python/Postgres/Neo4j）→ 运行时配置修补 → 启动停止脚本与快捷方式生成
# 兼容 PowerShell 5.1（无 ?./&&/Parallel）。所有生成的 .json/.ini/.bat 均为 UTF-8 无 BOM。
param(
    [string]$InstallDir = "",
    [string]$LogFile = ""
)

$ErrorActionPreference = "Stop"
if (-not $InstallDir) { $InstallDir = Split-Path -Parent $PSScriptRoot }
if (-not $LogFile) { $LogFile = Join-Path $InstallDir "logs\install.log" }
New-Item -ItemType Directory -Path (Split-Path $LogFile) -Force | Out-Null
$script:LogPath = $LogFile

function Log($msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $msg
    Write-Host $line
    Add-Content -Path $script:LogPath -Value $line -Encoding UTF8
}
function Fail($msg) {
    Log "ERROR: $msg"
    Write-Host ""
    Write-Host "安装配置失败：$msg（详见日志 $script:LogPath）" -ForegroundColor Red
    Write-Result "FAILED" $msg
    exit 1
}
# NSIS 安装器的权威结果文件（积极确认契约）：仅当全流程真正成功走到结尾才写 Status=OK，
# NSIS 只认 OK 判成功；失败时 NSIS 读出 Message 在对话框直接显示。
# 实测 NSIS 3.12 ReadINIStr 解码中文不可靠（GBK 必乱码），故 INI 只存纯 ASCII：
# Message 为报错的 ASCII 化（保留 exit 码/英文报错尾部/路径），完整中文报错见 install.log。
function Write-Result([string]$status, [string]$message) {
    $ini = Join-Path $InstallDir "logs\install-result.ini"
    $msg = $message -replace "[\r\n]+", " "
    $msg = [regex]::Replace($msg, '[^\x20-\x7E]', ' ')
    $msg = ($msg -replace ' {2,}', ' ').Trim()
    try {
        [System.IO.File]::WriteAllText($ini, "[Result]`r`nStatus=$status`r`nMessage=$msg`r`n", [System.Text.Encoding]::ASCII)
    } catch {
        try { [System.IO.File]::WriteAllText($ini, "[Result]`r`nStatus=$status`r`n", [System.Text.Encoding]::ASCII) } catch {}
    }
}
function Write-NoBom($path, $text) {
    [System.IO.File]::WriteAllText($path, $text, [System.Text.UTF8Encoding]::new($false))
}
function Test-PortInUse([int]$port) {
    try {
        $conns = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction Stop
        return ($null -ne $conns)
    } catch {
        # 兜底：TCP 连接探测（连接成功=有监听）
        try {
            $c = New-Object System.Net.Sockets.TcpClient
            $c.Connect("127.0.0.1", $port)
            $c.Close()
            return $true
        } catch { return $false }
    }
}
function Get-NextFreePort([int]$defaultPort, [int]$step = 200) {
    for ($i = 0; $i -lt $step; $i++) {
        $p = $defaultPort + $i
        if ($p -gt 65535) { break }
        if (-not (Test-PortInUse $p)) { return $p }
    }
    Fail "端口 $defaultPort 附近 $step 个端口全部被占用，无法自动分配"
    return $defaultPort
}
# 试运行探测：解释器是否"实际能跑"（import sys/encodings/site 成功且 prefix 指向 python 安装树）。
# 背景（2026-10-30 远程机实锤）：python.exe 是安装时最先释放的文件，"文件存在"≠安装完整；
# 标准库 Lib\ / python310.zip 未落盘时会 Fatal init_fs_encoding / No module named 'encodings'。
function Test-PythonRunnable([string]$exe) {
    if (-not (Test-Path $exe)) { return $false }
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    # 宿主环境（IDE/智能体 shim）注入的 PYTHONHOME/PYTHONPATH 会让解释器算错 prefix —— 探测时清空
    $oldHome = $env:PYTHONHOME; $oldPath = $env:PYTHONPATH
    $env:PYTHONHOME = $null; $env:PYTHONPATH = $null
    $probe = (& $exe -c "import sys,encodings,site;print(sys.prefix)" 2>&1 | Select-Object -First 1)
    $code = $LASTEXITCODE
    $env:PYTHONHOME = $oldHome; $env:PYTHONPATH = $oldPath
    return (($code -eq 0) -and ("$probe" -match 'python'))
}
# 在 conf 文件中替换（含注释行）或追加 key 行
function Set-ConfLine($path, $keyRegex, $newLine) {
    $content = [System.IO.File]::ReadAllText($path)
    if ([regex]::IsMatch($content, $keyRegex)) {
        $content = [regex]::Replace($content, $keyRegex, ($newLine + "`r`n"))
    } else {
        if (-not $content.EndsWith("`n")) { $content += "`r`n" }
        $content += $newLine + "`r`n"
    }
    [System.IO.File]::WriteAllText($path, $content, [System.Text.UTF8Encoding]::new($false))
}

Log "===== 本体平台安装配置开始 InstallDir=$InstallDir ====="
$runtime = Join-Path $InstallDir "runtime"
$depsDir = Join-Path $InstallDir "deps"
$scriptsDir = $PSScriptRoot

# ---------------------------------------------------------------------------
# 0. 撤销指向本安装目录的历史"重启后删除"登记（PendingFileRenameOperations）
#    场景（2026-10-30 远程机实锤）：卸载时安装目录删不净 → 某工具把整目录登记为延迟删除 →
#    重装完成后，下次开机 smss 在系统早期把刚装好的整个安装目录删掉（表现为"重启后安装目录消失"）。
#    对策：仅移除指向 $InstallDir 本体的条目（兼容 \??\ 前缀 / 8.3 短名 / 正斜杠变体）；
#    其他路径的登记一律不动。非致命：任何失败只告警，不阻断安装。
# ---------------------------------------------------------------------------
$pfroKey = 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager'
try {
    $pfro = (Get-ItemProperty -LiteralPath $pfroKey -Name PendingFileRenameOperations -ErrorAction Stop).PendingFileRenameOperations
    if ($pfro) {
        $norm = $InstallDir.TrimEnd('\').ToLower()
        $keep = New-Object System.Collections.Generic.List[string]
        $removed = 0
        for ($i = 0; $i -lt $pfro.Count; $i++) {
            $e = $pfro[$i]
            $p = $e
            if ($p.StartsWith('\??\')) { $p = $p.Substring(4) } elseif ($p.StartsWith('\??/')) { $p = $p.Substring(4) }
            $hit = $false
            if ($p -and $p.ToLower().Replace('/', '\') -eq $norm) { $hit = $true }
            if (-not $hit -and $p -and (Test-Path -LiteralPath $p)) {
                try { $hit = ((Resolve-Path -LiteralPath $p).Path.ToLower()) -eq $norm } catch {}
            }
            if ($hit) {
                $removed++
                # 删除/重命名登记为成对项（源路径 + 紧随的目标路径，删除时目标为空串）——一并移除避免悬挂空项
                if (($i + 1) -lt $pfro.Count -and $pfro[$i + 1] -eq '') { $i++ }
                continue
            }
            $keep.Add($e)
        }
        if ($removed -gt 0) {
            if ($keep.Count -eq 0) { Remove-ItemProperty -LiteralPath $pfroKey -Name PendingFileRenameOperations }
            else { Set-ItemProperty -LiteralPath $pfroKey -Name PendingFileRenameOperations -Value ([string[]]$keep.ToArray()) -Type MultiString }
            Log "已撤销 $removed 条指向 $InstallDir 的历史『重启后删除』登记（PendingFileRenameOperations），重装内容将不再被开机删除"
        } else {
            Log "未发现指向本安装目录的历史『重启后删除』登记"
        }
    }
} catch {
    Log "WARNING: 读取/清理 PendingFileRenameOperations 失败（不影响安装继续）：$($_.Exception.Message)"
}

# ---------------------------------------------------------------------------
# 1. 端口检测与自动重分配（默认：backend 8000 / frontend 8123 / fuseki 3030 / neo4j 7474+7687 / postgres 5432）
# ---------------------------------------------------------------------------
$ports = @{}
$portDefaults = @{ "backend" = 8000; "frontend" = 8123; "fuseki" = 3030; "neo4jHttp" = 7474; "neo4jBolt" = 7687; "postgres" = 5432 }
Log "--- 端口检测 ---"
foreach ($k in @("fuseki", "neo4jHttp", "neo4jBolt", "postgres", "backend", "frontend")) {
    $def = $portDefaults[$k]
    if (Test-PortInUse $def) {
        $new = Get-NextFreePort $def
        $ports[$k] = $new
        Log "端口 $def 被占用 → 自动重分配 $k = $new"
    } else {
        $ports[$k] = $def
        Log "端口 $def 空闲 → $k = $def"
    }
}

# ---------------------------------------------------------------------------
# 2. Java 检测（Neo4j 2026.x 需 JDK 21+；Jena 需 17+）：系统已有 21+ 则跳过捆绑 JDK
# ---------------------------------------------------------------------------
Log "--- Java 检测 ---"
$javaMode = "bundled"
$javaHome = $null
$javaExeCmd = Get-Command java -ErrorAction SilentlyContinue
if ($javaExeCmd) {
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $vout = & java -version 2>&1 | Out-String
    $ErrorActionPreference = $prevEap
    if ($vout -match 'version "(\d+)(\.\d+)?"') {
        $jmajor = [int]$Matches[1]
        Log "检测到系统 Java $jmajor"
        if ($jmajor -ge 21) {
            $javaMode = "system"
            $javaHome = Split-Path (Split-Path $javaExeCmd.Source)
            Log "使用系统 Java（跳过捆绑 JDK 解压）：$javaHome"
        }
    }
}
if ($javaMode -eq "bundled") {
    $jdkZip = Join-Path $runtime "jdk21.zip"
    $jdkDir = Join-Path $depsDir "jdk-21"
    if (-not (Test-Path $jdkZip)) { Fail "缺少捆绑 JDK 包 $jdkZip" }
    if (Test-Path (Join-Path $jdkDir "bin\java.exe")) {
        $javaHome = $jdkDir
        Log "捆绑 JDK 已存在，跳过解压：$jdkDir"
    } else {
        Log "未检测到系统 Java 21+，解压捆绑 JDK 21（约 1-2 分钟）..."
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $tmpExtract = Join-Path $depsDir "_jdk_extract"
        if (Test-Path $tmpExtract) { Remove-Item $tmpExtract -Recurse -Force }
        [System.IO.Compression.ZipFile]::ExtractToDirectory($jdkZip, $tmpExtract)
        $inner = Get-ChildItem $tmpExtract -Directory | Select-Object -First 1
        Move-Item $inner.FullName $jdkDir
        Remove-Item $tmpExtract -Recurse -Force
        $javaHome = $jdkDir
        Log "捆绑 JDK 就绪：$jdkDir"
    }
}
if (-not $javaHome) { Fail "Java 检测未产出 javaHome（系统 <21 且捆绑 JDK 缺失）" }
# 防御性校验：最终使用的 java 必须 ≥21（Neo4j 2026.x 要求；防捆绑 zip 误标版本）
$prevEap = $ErrorActionPreference
$ErrorActionPreference = "Continue"
$jvOut = & (Join-Path $javaHome "bin\java.exe") -version 2>&1 | Out-String
$ErrorActionPreference = $prevEap
if ($jvOut -match 'version "(\d+)' -and [int]$Matches[1] -lt 21) {
    Fail "选定 Java 版本过低（$jvOut），Neo4j 2026.x 需 21+"
}
$env:JAVA_HOME = $javaHome
$env:PATH = "$javaHome\bin;$env:PATH"

# ---------------------------------------------------------------------------
# 3. Python 检测（要求 3.10，与捆绑 wheelhouse 的 cp310 二进制轮子匹配）
#    系统已有 3.10+pip → 建 venv（跳过捆绑 Python 安装）；否则静默安装捆绑 Python 3.10.11
# ---------------------------------------------------------------------------
Log "--- Python 检测 ---"
# 宿主环境（IDE/智能体 shim 等）可能注入 PYTHONHOME/PYTHONPATH，会让解释器算错 sys.prefix
# （实测 PYTHONPATH=...\vendor\shim 可放大此类故障）——本脚本全程清空（含后续 venv 创建/pip）
$env:PYTHONHOME = $null
$env:PYTHONPATH = $null
$pythonExe = $null
$pyMode = ""
$targetMinor = 10
$whTargetFile = Join-Path $runtime "wheelhouse-target.json"
if (Test-Path $whTargetFile) {
    $whTarget = Get-Content $whTargetFile -Raw | ConvertFrom-Json
    $targetMinor = $whTarget.minor
}
$candidates = @()
$pyCmd = Get-Command py -ErrorAction SilentlyContinue
if ($pyCmd) {
    try {
        $pyList = & py -0p 2>$null | Out-String
        foreach ($m in [regex]::Matches($pyList, ("^\s*\*?-?3\.({0})-\d+\s+(\S+python\.exe)" -f $targetMinor))) {
            $candidates += $m.Groups[2].Value
        }
    } catch {}
}
$pyCmd2 = Get-Command python -ErrorAction SilentlyContinue
if ($pyCmd2) { $candidates += $pyCmd2.Source }
foreach ($c in $candidates) {
    if (-not $c -or -not (Test-Path $c)) { continue }
    try {
        $ver = & $c -c "import sys;print(sys.version_info[1])" 2>$null | Select-Object -First 1
        $pip = & $c -m pip --version 2>$null
        if ([int]$ver -eq $targetMinor -and $LASTEXITCODE -eq 0) {
            $pythonExe = $c
            $pyMode = "system"
            break
        }
    } catch {}
}
if ($pythonExe) {
    Log "检测到系统 Python 3.$targetMinor（含 pip）→ 使用系统解释器（跳过捆绑 Python 安装）：$pythonExe"
} else {
    $pyMode = "bundled"
    $pyTarget = Join-Path $runtime "python"
    $pyExeBundled = Join-Path $pyTarget "python.exe"
    if (Test-PythonRunnable $pyExeBundled) {
        Log "捆绑 Python 已就绪（试运行通过），跳过安装：$pyTarget"
    } else {
        # 复用判据=试运行而非"文件存在"（2026-10-30 远程机实锤：上次装到一半的残缺目录
        # python.exe 在但标准库缺失，旧逻辑反复跳过 → venv 必失败）。不可运行则清空重装。
        if (Test-Path $pyTarget) {
            Log "检测到无法运行的残留捆绑 Python（多为上次安装中断），清空重装：$pyTarget"
            Remove-Item $pyTarget -Recurse -Force
        }
        $pyInstaller = Join-Path $runtime ("python-3.10.{0}-amd64.exe" -f $targetMinor)
        # 兼容小版本文件名差异：runtime 目录下任一 python-*-amd64.exe
        if (-not (Test-Path $pyInstaller)) {
            $found = Get-ChildItem $runtime -Filter "python-*-amd64.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
            if ($found) { $pyInstaller = $found.FullName }
        }
        if (-not (Test-Path $pyInstaller)) { Fail "缺少捆绑 Python 安装包（runtime\python-*-amd64.exe）" }
        Log "未检测到匹配 Python 3.$targetMinor，静默安装捆绑 Python（约 1-2 分钟）..."
        $pyInstallLog = Join-Path $InstallDir "logs\python-install.log"
        # InstallAllUsers=1：安装器已以 admin 运行（机器级上下文），对任何 TargetDir 都成立；
        # 历史教训：按用户(0)模式配 Program Files 的 TargetDir 会让 Python 引导器提前退出
        # （瞬间返回 0 却未真正安装）。1.0.2 起默认安装目录已改 LOCALAPPDATA\Programs，
        # 机器级安装器写该目录同样正常。
        $pyArgs = @("/quiet", "InstallAllUsers=1", "TargetDir=$pyTarget", "Include_pip=1", "Include_test=0", "PrependPath=0", "Shortcuts=0", "/Log=$pyInstallLog")
        & $pyInstaller @pyArgs
        $pyExit = $LASTEXITCODE
        # 完成判据 = 解释器实际能跑（试运行探测），而非"进程退出 + python.exe 存在"：
        # 引导进程 exit 早于全部文件落盘，python.exe 是最先出现的文件，仅凭文件存在会在
        # 标准库 Lib\ / python310.zip 尚未释放时误判完成 → venv 创建即 Fatal init_fs_encoding。
        # 轮询：每 5 秒探测一次，最多 6 分钟。
        $pyProcName = [System.IO.Path]::GetFileNameWithoutExtension($pyInstaller)
        $deadline = (Get-Date).AddMinutes(6)
        $pyOk = $false
        while ((Get-Date) -lt $deadline) {
            if ((-not (Get-Process -Name $pyProcName -ErrorAction SilentlyContinue)) -and (Test-PythonRunnable $pyExeBundled)) { $pyOk = $true; break }
            Start-Sleep -Seconds 5
        }
        if (-not $pyOk) {
            $probe = (& $pyExeBundled -c "import sys,encodings,site;print(sys.prefix)" 2>&1 | Out-String).Trim()
            Fail ("捆绑 Python 安装失败（安装程序 exit={0}，6 分钟内试运行探测未通过；探测输出：{1}；MSI 日志：{2}）" -f $pyExit, $probe.Substring(0, [Math]::Min(120, $probe.Length)), $pyInstallLog)
        }
        Log "捆绑 Python 就绪（试运行通过）：$pyTarget（installer exit=$pyExit）"
    }
    $pythonExe = $pyExeBundled
}
# venv（两种模式统一走 runtime\venv；已存在则跳过）
$venvDir = Join-Path $runtime "venv"
$venvPython = Join-Path $venvDir "Scripts\python.exe"
$depsMarker = Join-Path $venvDir ".deps-installed"
if (Test-Path $depsMarker) {
    Log "Python venv 与依赖已就绪，跳过：$venvDir"
} else {
    if (-not (Test-Path $venvPython)) {
        Log "创建 Python venv..."
        $env:PYTHONHOME = $null; $env:PYTHONPATH = $null   # 二次保险（见 Python 检测段说明）
        & $pythonExe -m venv $venvDir
        if ($LASTEXITCODE -ne 0) {
            $probe = (& $pythonExe -c "import sys,encodings;print(sys.prefix)" 2>&1 | Out-String).Trim()
            Fail ("venv 创建失败：解释器 {0} 试运行探测：{1}（多为残缺安装——Lib\ / python310.zip 缺失；删除 runtime\python 后重跑安装即可）" -f $pythonExe, $probe.Substring(0, [Math]::Min(160, $probe.Length)))
        }
    }
    Log "离线安装后端依赖（wheelhouse）..."
    & $venvPython -m pip install --no-index --find-links (Join-Path $runtime "wheelhouse") -r (Join-Path $InstallDir "backend\requirements.txt") --quiet
    if ($LASTEXITCODE -ne 0) { Fail "离线安装后端依赖失败（wheelhouse 不完整？）" }
    Set-Content -Path $depsMarker -Value "ok" -Encoding ascii
    Log "venv 就绪：$venvDir"
}

# ---------------------------------------------------------------------------
# 4. Postgres：全新数据目录 initdb（ont 用户/库）；已初始化则跳过
#    数据目录/日志目录放 C:\ProgramData（不放安装目录）：
#      a) initdb 建目录后须把 DACL 收紧为 0700（重写 DACL），Program Files 受保护树下
#         新建目录继承 TrustedInstaller/SYSTEM/Users(I) 等 ACE，重写会被拒绝（即使提升态）；
#      b) EDB postgres 拒绝高完整性令牌，服务以非提升态启动，数据/日志目录须普通用户可写。
# ---------------------------------------------------------------------------
Log "--- Postgres 初始化 ---"
$pgRoot = Join-Path $depsDir "pgsql"
$pgDataRoot = Join-Path $env:ProgramData "OntologyPlatform"
$pgData = Join-Path $pgDataRoot "pgdata"
$pgBin = Join-Path $pgRoot "bin"
New-Item -ItemType Directory -Path $pgDataRoot -Force | Out-Null
if (-not (Test-Path (Join-Path $pgBin "initdb.exe"))) { Fail "缺少捆绑 Postgres：$pgRoot" }
$env:PATH = "$pgBin;$env:PATH"
if (Test-Path (Join-Path $pgData "PG_VERSION")) {
    Log "Postgres 数据目录已初始化，跳过 initdb：$pgData"
} else {
    if (Test-Path $pgData) {
        try {
            Remove-Item $pgData -Recurse -Force -ErrorAction Stop
        } catch {
            Fail ("无法清空残留 Postgres 数据目录（可能被占用/杀毒软件锁定）：{0}；{1}" -f $pgData, $_.Exception.Message)
        }
    }
    Log ("initdb（superuser=ont, trust 认证）→ {0} ..." -f $pgData)
    $initdbLog = Join-Path $InstallDir "logs\initdb.log"
    $initdbExe = Join-Path $pgBin "initdb.exe"
    $initdbParams = @("-D", $pgData, "-U", "ont", "--auth=trust", "--no-instructions", "--encoding=UTF8", "--locale=C")
    $initdbOut = & $initdbExe @initdbParams 2>&1
    $initdbExit = $LASTEXITCODE
    $initdbOut | ForEach-Object { Log ("initdb| " + $_.ToString()) }
    if ($initdbExit -ne 0 -or -not (Test-Path (Join-Path $pgData "PG_VERSION"))) {
        # 全新机器常见：杀毒软件瞬时锁定刚创建的数据目录。等 10 秒清目录重试一次。
        Log "initdb 首次失败（exit=$initdbExit），10 秒后清空目录重试一次..."
        Start-Sleep -Seconds 10
        if (Test-Path $pgData) { Remove-Item $pgData -Recurse -Force -ErrorAction SilentlyContinue }
        $initdbOut = & $initdbExe @initdbParams 2>&1
        $initdbExit = $LASTEXITCODE
        $initdbOut | ForEach-Object { Log ("initdb-retry| " + $_.ToString()) }
    }
    if ($initdbExit -ne 0 -or -not (Test-Path (Join-Path $pgData "PG_VERSION"))) {
        $tail = ($initdbOut | ForEach-Object { $_.ToString() } | Where-Object { $_.Trim() -ne "" } | Select-Object -Last 5) -join " | "
        Add-Content -Path $initdbLog -Value (($initdbOut | ForEach-Object { $_.ToString() }) -join "`r`n") -Encoding UTF8
        Fail ("initdb 失败（exit={0}）。报错尾部：{1}；完整日志：{2}" -f $initdbExit, $tail, $initdbLog)
    }
    Log "initdb 完成"
}
# 端口与日志写入 postgresql.conf（日志目录同在 ProgramData，非提升态 postgres 可写）
$pgLogDir = Join-Path $pgDataRoot "logs\postgres"
New-Item -ItemType Directory -Path $pgLogDir -Force | Out-Null
Set-ConfLine (Join-Path $pgData "postgresql.conf") "(?m)^#?\s*port\s*=.*$" ("port = {0}" -f $ports["postgres"])
Set-ConfLine (Join-Path $pgData "postgresql.conf") "(?m)^#?\s*logging_collector\s*=.*$" "logging_collector = on"
# 注意：EDB postgres 对配置路径中的反斜杠有转义 bug（会把 \x 当转义吃掉），
# 必须用正斜杠写入（postgres 在 Windows 原生支持斜杠路径）。
Set-ConfLine (Join-Path $pgData "postgresql.conf") "(?m)^#?\s*log_directory\s*=.*$" ("log_directory = '{0}'" -f ($pgLogDir -replace '\\', '/'))
# 注：ont 库不在安装期创建——EDB postgres 拒绝以 UAC 提升身份运行，
#     安装器是提升态，无法启动服务；改由 start-services.ps1（普通用户身份）
#     首次启动 postgres 后幂等 createdb。
Log "Postgres 初始化完成（ont 库将在首次启动时创建）"

# ---------------------------------------------------------------------------
# 5. Neo4j：解压捆绑发行包（已解压则跳过）→ 端口/tmpdir 配置 → 初始密码
# ---------------------------------------------------------------------------
Log "--- Neo4j 初始化 ---"
$n4jName = "neo4j-community-2026.09.0"
$n4jDir = Join-Path $depsDir $n4jName
$n4jZip = Join-Path $runtime ($n4jName + "-windows.zip")
if (-not (Test-Path (Join-Path $n4jDir "conf\neo4j.conf"))) {
    if (-not (Test-Path $n4jZip)) { Fail "缺少捆绑 Neo4j 发行包 $n4jZip" }
    Log "解压捆绑 Neo4j（约 1-2 分钟）..."
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    if (Test-Path $n4jDir) { Remove-Item $n4jDir -Recurse -Force }
    [System.IO.Compression.ZipFile]::ExtractToDirectory($n4jZip, $depsDir)
    if (-not (Test-Path (Join-Path $n4jDir "conf\neo4j.conf"))) { Fail "Neo4j 解压后未找到 conf\neo4j.conf" }
    Log "Neo4j 解压完成：$n4jDir"
} else {
    Log "Neo4j 已存在，跳过解压：$n4jDir"
}
$n4jConf = Join-Path $n4jDir "conf\neo4j.conf"
$env:PATH = "$n4jDir\bin;$env:PATH"
# 端口
Set-ConfLine $n4jConf "(?m)^#?\s*server\.http\.listen_address\s*=.*$" ("server.http.listen_address=:{0}" -f $ports["neo4jHttp"])
Set-ConfLine $n4jConf "(?m)^#?\s*server\.bolt\.listen_address\s*=.*$" ("server.bolt.listen_address=:{0}" -f $ports["neo4jBolt"])
# tmpdir 改道 ProgramData 专用目录（2026-10-29 真机事故修复）：
# 旧版放在安装目录 "C:\Program Files\..."——路径含空格且 -D 参数未加引号 → JVM 把参数拆散，
# "Files/OntologyPlatform/.../tmp" 被误当主类 → ClassNotFoundException，Neo4j 秒退，
# 启动脚本却在死等 300 秒超时（用户表现为"卡 5 分钟无反应"）。
# 对策：无空格路径（天然免引号）+ 普通用户可写（与 postgres 数据目录同根）。
$n4jTmp = Join-Path $env:ProgramData "OntologyPlatform\neo4j-tmp"
New-Item -ItemType Directory -Path $n4jTmp -Force | Out-Null
$confContent = [System.IO.File]::ReadAllText($n4jConf)
$confContent = [regex]::Replace($confContent, "(?m)^#?\s*server\.jvm\.additional=.*(?:`r?`n|\Z)", "")
$n4jTmpSlash = $n4jTmp -replace '\\', '/'
$confContent = $confContent.TrimEnd() + ("`r`nserver.jvm.additional=-Djava.io.tmpdir={0}`r`n" -f $n4jTmpSlash)
[System.IO.File]::WriteAllText($n4jConf, $confContent, [System.Text.UTF8Encoding]::new($false))
# 初始密码（仅首次：以 auth.ini 为准——发行 zip 自带空 data\databases 骨架，不能据此判断）
$authIni = Join-Path $n4jDir "data\dbms\auth.ini"
if (-not (Test-Path $authIni)) {
    Log "设置 Neo4j 初始密码..."
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $pwOut = & (Join-Path $n4jDir "bin\neo4j-admin.bat") dbms set-initial-password neo4jsecret 2>&1 | Out-String
    $pwCode = $LASTEXITCODE
    $ErrorActionPreference = $prevEap
    if ($pwCode -ne 0) { Fail "neo4j set-initial-password 失败：$pwOut" }
    if (-not (Test-Path $authIni)) { Fail "set-initial-password 返回 0 但未生成 auth.ini" }
    Log "Neo4j 初始密码设置完成"
} else {
    Log "Neo4j 密码已设置，跳过"
}

# ---------------------------------------------------------------------------
# 6. 平台配置修补（幂等）：前端 API 基址 / stdio 桥 / Fuseki TDB2 路径
# ---------------------------------------------------------------------------
Log "--- 平台配置修补 ---"
$bport = [string]$ports["backend"]
# 6.1 frontend/app.js：全部裸 8000 字面量替换为实际后端端口（共 4 处：API_BASE×2 / mcpBaseUrl×2）
$appJs = Join-Path $InstallDir "frontend\app.js"
if (Test-Path $appJs) {
    $js = [System.IO.File]::ReadAllText($appJs)
    $jsNew = [regex]::Replace($js, "(?<!\d)8000(?!\d)", $bport)
    if ($jsNew -cne $js) {
        [System.IO.File]::WriteAllText($appJs, $jsNew, [System.Text.UTF8Encoding]::new($false))
        Log "app.js API_BASE → $bport"
    } else {
        Log "app.js API_BASE 已是 $bport，跳过"
    }
}
# 6.2 frontend/index.html：MCP 说明文案中的 :8000
$idxHtml = Join-Path $InstallDir "frontend\index.html"
if (Test-Path $idxHtml) {
    $h = [System.IO.File]::ReadAllText($idxHtml)
    $h = [regex]::Replace($h, ":8000(?!\d)", (":{0}" -f $bport))
    [System.IO.File]::WriteAllText($idxHtml, $h, [System.Text.UTF8Encoding]::new($false))
}
# 6.3 mcp_stdio_bridge*.py：HOST, PORT 固定 8000
foreach ($f in @("mcp_stdio_bridge.py", "mcp_stdio_bridge_v2.py")) {
    $p = Join-Path $InstallDir "backend\$f"
    if (Test-Path $p) {
        $t = [System.IO.File]::ReadAllText($p)
        $t = [regex]::Replace($t, '("127\.0\.0\.1"\s*,\s*)8000', ('$1' + $bport))
        [System.IO.File]::WriteAllText($p, $t, [System.Text.UTF8Encoding]::new($false))
    }
}
# 6.4 fuseki-config.ttl：TDB2 路径改到安装目录
$ttl = Join-Path $InstallDir "backend\fuseki-config.ttl"
if (Test-Path $ttl) {
    $fdir = (Join-Path $InstallDir "backend\data\fuseki") -replace '\\', '/'
    $t2 = [System.IO.File]::ReadAllText($ttl)
    $t2New = [regex]::Replace($t2, "(?m)^.*tdb2:location.*$", ("    tdb2:location `"{0}`" ." -f $fdir))
    if ($t2New -cne $t2) {
        [System.IO.File]::WriteAllText($ttl, $t2New, [System.Text.UTF8Encoding]::new($false))
        Log "fuseki-config.ttl TDB2 → $fdir"
    } else {
        Log "fuseki-config.ttl TDB2 已是 $fdir，跳过"
    }
}
New-Item -ItemType Directory -Path (Join-Path $InstallDir "backend\data") -Force | Out-Null
# Neo4j/Fuseki/后端 运行期在安装目录内写数据与日志；双击 bat 的常态启动是非提升态，
# Program Files 下 Users 仅 RX 写不进去。给整个安装树授 Users 组 Modify
# （用 well-known SID 避免中文系统组名差异；(OI)(CI) 使后续新建文件/目录自动继承）。
icacls $InstallDir /grant "*S-1-5-32-545:(OI)(CI)(M)" /T /C /Q | Out-Null
if ($LASTEXITCODE -ne 0) {
    Log "WARNING: icacls 授予安装目录写权限失败 exit=$LASTEXITCODE（非提升态启动可能无法写安装目录）"
} else {
    Log "已授予安装目录 Users 写权限（支持非提升态启动 Neo4j/Fuseki/后端写入）"
}

# ---------------------------------------------------------------------------
# 7. 生成 runtime-config.json / ports.ini / 启动停止 bat
# ---------------------------------------------------------------------------
Log "--- 生成运行时配置与启动脚本 ---"
# 注意：PS 5.1 双引号 here-string 不支持 $ports["key"] 裸索引（会展开成类型名垃圾串），
# 必须先取出标量变量再插值。
$cBackend = [string]$ports["backend"]
$cFrontend = [string]$ports["frontend"]
$cFuseki = [string]$ports["fuseki"]
$cNeo4jHttp = [string]$ports["neo4jHttp"]
$cNeo4jBolt = [string]$ports["neo4jBolt"]
$cPg = [string]$ports["postgres"]
$cfg = @"
{
  "installDir": "$($InstallDir -replace '\\', '/')",
  "ports": {
    "backend": $cBackend,
    "frontend": $cFrontend,
    "fuseki": $cFuseki,
    "neo4jHttp": $cNeo4jHttp,
    "neo4jBolt": $cNeo4jBolt,
    "postgres": $cPg
  },
  "jena": { "baseUrl": "http://localhost:$cFuseki", "dataset": "ontology" },
  "neo4j": { "user": "neo4j", "password": "neo4jsecret" },
  "postgres": { "user": "ont", "dbname": "ont", "dataDir": "$($pgData -replace '\\', '/')" },
  "java": { "mode": "$javaMode", "home": "$(if ($javaMode -eq 'bundled') { ($javaHome -replace '\\', '/') } else { '' })" },
  "python": { "mode": "$pyMode", "venv": "$($venvDir -replace '\\', '/')" },
  "installedAt": "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
}
"@
Write-NoBom (Join-Path $scriptsDir "runtime-config.json") $cfg
Write-NoBom (Join-Path $scriptsDir "ports.ini") ("[ports]`r`nbackend={0}`r`nfrontend={1}`r`nfuseki={2}`r`nneo4jHttp={3}`r`nneo4jBolt={4}`r`npostgres={5}`r`n" -f $ports["backend"], $ports["frontend"], $ports["fuseki"], $ports["neo4jHttp"], $ports["neo4jBolt"], $ports["postgres"])
# 启动/停止 bat（无 BOM + CRLF）
$startBat = @(
    "@echo off",
    "chcp 65001 >nul",
    "title 企业业务本体平台 - 启动",
    "powershell -NoProfile -ExecutionPolicy Bypass -File ""%~dp0scripts\start-services.ps1""",
    "echo.",
    "pause"
) -join "`r`n"
$stopBat = @(
    "@echo off",
    "chcp 65001 >nul",
    "title 企业业务本体平台 - 停止",
    "powershell -NoProfile -ExecutionPolicy Bypass -File ""%~dp0scripts\stop-services.ps1""",
    "echo.",
    "pause"
) -join "`r`n"
Write-NoBom (Join-Path $InstallDir "start-platform.bat") $startBat
Write-NoBom (Join-Path $InstallDir "stop-platform.bat") $stopBat

# ---------------------------------------------------------------------------
# 8. 桌面快捷方式（启动 + 停止）——纯装饰性：任何失败只告警不弄挂安装
#    根因（远程机 RDP 实锤）：WScript.Shell Save() 偶发 UnauthorizedAccessException，
#    属安全软件/资源管理器对 .lnk 的瞬时占用，与权限配置无关（同机脚本复现一次性成功）。
#    对策：每个位置重试 3 次（间隔 2s）；位置链 用户桌面 → 公共桌面
#    （C:\Users\Public\Desktop，所有用户桌面可见）→ 安装目录；全程记日志。
$ws = New-Object -ComObject WScript.Shell
$desktopA = $ws.SpecialFolders("Desktop")
if ([string]::IsNullOrWhiteSpace($desktopA)) { $desktopA = [Environment]::GetFolderPath("Desktop") }
Log ("快捷方式目标链: 用户桌面={0} 公共桌面=C:\Users\Public\Desktop 身份={1}" -f $desktopA, ([Security.Principal.WindowsIdentity]::GetCurrent().Name))
function New-Shortcut([string]$lnkName, [string]$batName, [string]$iconFile, [string]$desc) {
    $iconPath = Join-Path $InstallDir (Join-Path "icons" $iconFile)
    $dirs = @($desktopA, "C:\Users\Public\Desktop", $InstallDir)
    foreach ($dir in $dirs) {
        $p = Join-Path $dir $lnkName
        for ($i = 1; $i -le 3; $i++) {
            try {
                $l = $ws.CreateShortcut($p)
                $l.TargetPath = Join-Path $InstallDir $batName
                $l.WorkingDirectory = $InstallDir
                $l.Description = $desc
                if (Test-Path $iconPath) { $l.IconLocation = "$iconPath,0" }
                $l.Save()
                Log "快捷方式就绪: $p"
                return $p
            } catch {
                Log ("WARNING: 快捷方式保存失败 {0}（第 {1}/3 次）: {2}" -f $p, $i, $_.Exception.Message)
                Start-Sleep -Seconds 2
            }
        }
    }
    Log ("WARNING: 快捷方式 {0} 全部位置创建失败，请改用 {1}\{2} 直接启动/停止" -f $lnkName, $InstallDir, $batName)
    return $null
}
$startLnkPath = New-Shortcut "本体平台-启动.lnk" "start-platform.bat" "start.ico" "启动企业业务本体平台（前后端 + 依赖服务）"
$stopLnkPath = New-Shortcut "本体平台-停止.lnk" "stop-platform.bat" "stop.ico" "停止企业业务本体平台全部服务"
# 安装信息（供 NSIS 卸载段读取）：快捷方式记实际最终路径（可能回退到公共桌面/安装目录）
Write-NoBom (Join-Path $scriptsDir "install-config.ini") ("[Paths]`r`nInstDir={0}`r`n`r`n[Shortcuts]`r`nStartLnk=本体平台-启动.lnk`r`nStopLnk=本体平台-停止.lnk`r`nStartLnkPath={1}`r`nStopLnkPath={2}`r`n" -f $InstallDir, $startLnkPath, $stopLnkPath)

# ---------------------------------------------------------------------------
# 7.5 安装收尾：给当前用户授予安装目录完全控制（非提升态启动的写入前提）
#     "C:\Program Files\..." 默认 Users 仅读+执行；非提升的 Neo4j/Fuseki/后端写不了
#     自己的 data/logs/run 与 TDB 目录，且第 6 节授的 (M) 不含 Delete。
#     给当前身份（双击安装器=日后双击启动快捷方式的同一用户）授 (F) 兜全。
$grantUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name
Log ("给当前用户 {0} 授予安装目录完全控制（(OI)(CI)(F)）..." -f $grantUser)
$prevEap = $ErrorActionPreference
$ErrorActionPreference = "Continue"
icacls.exe $InstallDir /grant "${grantUser}:(OI)(CI)(F)" /T /Q | Out-Null
$grantCode = $LASTEXITCODE
$ErrorActionPreference = $prevEap
if ($grantCode -ne 0) {
    Log "WARNING: 授予当前用户完全控制未完全成功 exit=$grantCode（个别文件被占用；服务首启若报写权限错误，可手动对该目录 icacls 补授）"
} else {
    Log "当前用户对安装目录拥有完全控制（非提升态 Neo4j/Fuseki/后端可写入）"
}

# ---------------------------------------------------------------------------
# 9. 摘要
# ---------------------------------------------------------------------------
Log "===== 安装配置完成 ====="
Log "前端页面: http://localhost:$($ports['frontend'])"
Log "后端 API: http://localhost:$($ports['backend'])   文档: http://localhost:$($ports['backend'])/docs"
Log "服务端口: fuseki=$($ports['fuseki']) neo4j=$($ports['neo4jHttp'])/$($ports['neo4jBolt']) postgres=$($ports['postgres'])"
Log "启动: 桌面「本体平台-启动」  停止: 桌面「本体平台-停止」"
Write-Result "OK" ("Install configuration completed. frontend=http://localhost:" + $ports['frontend'] + " backend=http://localhost:" + $ports['backend'])
exit 0
