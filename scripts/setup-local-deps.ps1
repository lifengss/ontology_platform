# 企业业务本体平台 - 本地依赖一键安装脚本（Windows）
# 无需 Docker，直接下载并解压 Jena/Fuseki + Neo4j + Postgres 到 deps/ 目录
# 用法：.\\scripts\\setup-local-deps.ps1

param(
  [string]$JenaVersion = "5.1.0",
  [string]$Neo4jVersion = "5.20.0",
  [string]$PostgresVersion = "16.4-1"
)

$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$ROOT = Split-Path -Parent $PSScriptRoot
$DEPS = Join-Path $ROOT "deps"

function Info($msg) { Write-Host "[INFO] $msg" -ForegroundColor Cyan }
function OK($msg) { Write-Host "[OK]   $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "[WARN] $msg" -ForegroundColor Yellow }

function Test-Java {
  $javaCmd = Get-Command java -ErrorAction SilentlyContinue
  if (-not $javaCmd) {
    Warn "未检测到 Java，请安装 JDK 17 或更高版本后重试。"
    exit 1
  }
  $lines = cmd /c "java -version 2>&1"
  $verLine = $lines | Where-Object { $_ -match 'version\s+"(\d+)' } | Select-Object -First 1
  if ($verLine -and ($verLine -match 'version\s+"(\d+)')) {
    [int]$ver = $Matches[1]
    if ($ver -ge 17) {
      OK "检测到 Java $ver"
      return
    }
  }
  Warn "未检测到 Java 17+，请安装 JDK 17 或更高版本后重试。"
  exit 1
}

function Expand-Zip($src, $dst) {
  if (-not (Test-Path $dst)) { New-Item -ItemType Directory -Path $dst | Out-Null }
  Expand-Archive -Path $src -DestinationPath $dst -Force
}

function Download-File($urls, $outFile) {
  $lastErr = $null
  foreach ($url in $urls) {
    Info "尝试下载 $url"
    try {
      Invoke-WebRequest -Uri $url -OutFile $outFile -UseBasicParsing -TimeoutSec 600
      return
    } catch {
      $lastErr = $_
      Warn "下载失败: $url"
    }
  }
  throw "所有下载源均失败: $lastErr"
}

function Run-Silently($cmd) {
  $ea = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  & $cmd 2>&1 | Out-Null
  $ErrorActionPreference = $ea
}

New-Item -ItemType Directory -Path $DEPS -ErrorAction SilentlyContinue | Out-Null

Test-Java

# Jena Fuseki
$jenaBase = "apache-jena-fuseki-$JenaVersion"
$jenaZip = "$jenaBase.zip"
$jenaUrl = "https://archive.apache.org/dist/jena/binaries/$jenaZip"
$jenaDest = Join-Path $DEPS $jenaBase

if (Test-Path $jenaDest) {
  OK "Jena/Fuseki 已存在于 $jenaDest"
} else {
  Info "下载 Jena/Fuseki $JenaVersion ..."
  Download-File -urls @($jenaUrl) -outFile (Join-Path $DEPS $jenaZip)
  Info "解压 Jena/Fuseki ..."
  Expand-Zip -src (Join-Path $DEPS $jenaZip) -dst $DEPS
  Remove-Item (Join-Path $DEPS $jenaZip)
  OK "Jena/Fuseki 安装完成"
}

# Neo4j
$neo4jBase = "neo4j-community-$Neo4jVersion"
$neo4jZip = "$neo4jBase-windows.zip"
$neo4jUrls = @(
  "https://mirrors.huaweicloud.com/neo4j/$Neo4jVersion/$neo4jZip",
  "https://dist.neo4j.org/$neo4jZip",
  "https://github.com/neo4j/neo4j/releases/download/$Neo4jVersion/$neo4jZip"
)
$neo4jDest = Join-Path $DEPS $neo4jBase

if (Test-Path $neo4jDest) {
  OK "Neo4j 已存在于 $neo4jDest"
} else {
  Info "下载 Neo4j Community $Neo4jVersion ..."
  Download-File -urls $neo4jUrls -outFile (Join-Path $DEPS $neo4jZip)
  Info "解压 Neo4j ..."
  Expand-Zip -src (Join-Path $DEPS $neo4jZip) -dst $DEPS
  Remove-Item (Join-Path $DEPS $neo4jZip)

  $neo4jAdmin = Join-Path $neo4jDest "bin\\neo4j-admin.bat"
  if (Test-Path $neo4jAdmin) {
    Info "初始化 Neo4j 密码..."
    Run-Silently { & $neo4jAdmin dbms set-initial-password neo4jsecret }
  }
  OK "Neo4j 安装完成"
}

# Postgres (Windows zip binaries)
$pgBase = "postgresql-$PostgresVersion-windows-x64-binaries"
$pgZip = "$pgBase.zip"
$pgUrls = @(
  "https://get.enterprisedb.com/postgresql/$pgZip"
)
$pgDest = Join-Path $DEPS "pgsql"
$pgData = Join-Path $pgDest "data"

if (Test-Path $pgDest) {
  OK "Postgres 已存在于 $pgDest"
} else {
  Info "下载 Postgres $PostgresVersion ..."
  Download-File -urls $pgUrls -outFile (Join-Path $DEPS $pgZip)
  Info "解压 Postgres ..."
  Expand-Zip -src (Join-Path $DEPS $pgZip) -dst $DEPS

  $inner = Get-ChildItem -Path $DEPS -Filter "pgsql" -Directory | Select-Object -First 1
  if (-not $inner) {
    $inner = Get-ChildItem -Path $DEPS -Filter "pgsql-*" -Directory | Select-Object -First 1
    if ($inner) { Rename-Item -Path $inner.FullName -NewName "pgsql" }
  }
  Remove-Item (Join-Path $DEPS $pgZip)

  Info "初始化 Postgres 数据库..."
  $initdb = Join-Path $pgDest "bin\\initdb.exe"
  Run-Silently { & $initdb -D $pgData -U ont --auth=trust --no-instructions --encoding=UTF8 --locale=C }

  Info "启动 Postgres 以创建 ont 数据库..."
  $pgCtl = Join-Path $pgDest "bin\\pg_ctl.exe"
  Run-Silently { & $pgCtl -D $pgData -l "$pgDest\\postgres.log" start }

  $createdb = Join-Path $pgDest "bin\\createdb.exe"
  Run-Silently { & $createdb -U ont ont }

  OK "Postgres 安装完成"
}

Write-Host ""
OK "本地依赖准备完毕"
Write-Host "  Jena/Fuseki: $jenaDest" -ForegroundColor White
Write-Host "  Neo4j:       $neo4jDest" -ForegroundColor White
Write-Host "  Postgres:    $pgDest" -ForegroundColor White
Write-Host ""
Write-Host "现在可以运行 .\\scripts\\start-all.ps1 启动完整服务（无需 Docker）" -ForegroundColor Cyan
