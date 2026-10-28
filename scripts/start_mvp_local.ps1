# MVP 本机启动脚本（Windows PowerShell）
# 不依赖 Docker，使用项目内 deps/ 自包含依赖包启动服务
# 要求：Python 3.10+、JDK 17+（Jena 需要）
#
# 依赖包自包含路径：
#   deps/apache-jena-fuseki-5.1.0/          -> Jena/Fuseki
#   deps/neo4j-community-*/                 -> Neo4j（由 zip 解压得到）
#   deps/pgsql/                             -> PostgreSQL + pgvector
#
# 若某依赖缺失，后端会自动降级到本地存储，不影响核心功能。

param(
    [switch]$SkipJena,
    [switch]$SkipNeo4j,
    [switch]$SkipPostgres,
    [switch]$StartBackendOnly
)

$ROOT = Split-Path -Parent $PSScriptRoot
$DATA = "$ROOT\backend\data"
$DEPS = "$ROOT\deps"
New-Item -ItemType Directory -Force -Path $DATA | Out-Null

# 优先使用项目内 JDK 21（Neo4j 2026.x 需要），否则回退系统 JAVA_HOME
$localJdk = Get-ChildItem -Path "$DEPS" -Filter "jdk-21*" -Directory -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $localJdk) {
    $localJdk = Get-ChildItem -Path "$DEPS" -Filter "jdk-*" -Directory -ErrorAction SilentlyContinue | Where-Object { $_.Name -match 'jdk-2[1-9]' } | Select-Object -First 1
}
if ($localJdk) {
    $env:JAVA_HOME = $localJdk.FullName
    $env:PATH = "$($localJdk.FullName)\bin;$env:PATH"
    Write-Host "使用项目内 JDK: $($localJdk.FullName)" -ForegroundColor Cyan
}

if ($StartBackendOnly) {
    Write-Host "仅启动后端（依赖请自行提前运行）" -ForegroundColor Cyan
    Start-Process -NoNewWindow -WorkingDirectory "$ROOT\backend" -FilePath "python" -ArgumentList "-m uvicorn main:app --host 0.0.0.0 --port 8000 --reload"
    return
}

# Jena/Fuseki（可写 TDB2 数据集，配置 backend/fuseki-config.ttl）
if (-not $SkipJena) {
    $jenaJar = Get-ChildItem -Path "$DEPS\apache-jena-fuseki-*" -Include "fuseki-server.jar","jena-fuseki-server-*.jar" -Recurse -ErrorAction SilentlyContinue | Where-Object { $_.FullName -notmatch '\\lib\\' } | Select-Object -First 1
    $jenaConfig = "$ROOT\backend\fuseki-config.ttl"
    if ($jenaJar) {
        # jar 所在目录即发行根目录（内含 webapp/），取 Parent 会导致 Jena 找不到 webapp 而启动失败
        $jenaBase = $jenaJar.Directory.FullName
        Write-Host "启动 Jena/Fuseki: $jenaBase" -ForegroundColor Cyan
        $env:FUSEKI_BASE = "$jenaBase\run"
        if (Test-Path $jenaConfig) {
            Start-Process -NoNewWindow -WorkingDirectory $jenaBase -FilePath "java" -ArgumentList "-jar `"$($jenaJar.FullName)`" --port 3030 --config `"$jenaConfig`""
        } else {
            # 无配置时回退 --loc（注意：Jena 5 对已存在目录会建只读数据集，写库将降级本地 TTL）
            $jenaData = "$DATA\fuseki"
            New-Item -ItemType Directory -Force -Path $jenaData | Out-Null
            Start-Process -NoNewWindow -WorkingDirectory $jenaBase -FilePath "java" -ArgumentList "-jar `"$($jenaJar.FullName)`" --loc=`"$jenaData`" --port=3030 /ontology"
        }
    } else {
        Write-Host "未找到 deps/apache-jena-fuseki-*/fuseki-server.jar，跳过 Jena（后端将使用本地 TTL 降级）" -ForegroundColor Yellow
    }
}

# Neo4j（项目内自包含：先检查解压目录，再检查 zip）
if (-not $SkipNeo4j) {
    $neo4jHome = Get-ChildItem -Path "$DEPS" -Filter "neo4j-community-*" -Directory -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $neo4jHome) {
        $neo4jZip = Get-ChildItem -Path "$DEPS\neo4j-community-*.zip" -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($neo4jZip) {
            if ($neo4jZip.Length -lt 50MB) {
                Write-Host "Neo4j zip 文件过小（$($neo4jZip.Length) 字节），可能已损坏，跳过 Neo4j（后端将使用本地 JSONL 降级）" -ForegroundColor Red
            } else {
                Write-Host "解压 Neo4j: $($neo4jZip.FullName)" -ForegroundColor Cyan
                Expand-Archive -Path $neo4jZip.FullName -DestinationPath $DEPS -Force
                $neo4jHome = Get-ChildItem -Path "$DEPS" -Filter "neo4j-community-*" -Directory -ErrorAction SilentlyContinue | Select-Object -First 1
            }
        }
    }
    if ($neo4jHome -and ((Test-Path "$($neo4jHome.FullName)\bin\neo4j.bat") -or (Test-Path "$($neo4jHome.FullName)\bin\neo4j.ps1"))) {
        Write-Host "启动 Neo4j Community: $($neo4jHome.FullName)" -ForegroundColor Cyan
        # 设置初始密码（仅首次有效；若已设置则跳过错误）
        $adminPass = $env:NEO4J_PASSWORD
        if (-not $adminPass) { $adminPass = "neo4jsecret" }
        $neo4jAdmin = "$($neo4jHome.FullName)\bin\neo4j-admin.bat"
        if (Test-Path $neo4jAdmin) {
            Start-Process -NoNewWindow -Wait -WorkingDirectory "$($neo4jHome.FullName)\bin" -FilePath $neo4jAdmin -ArgumentList "dbms set-initial-password `"$adminPass`"" -ErrorAction SilentlyContinue
        }
        # 优先使用 PowerShell 版启动脚本，可继承当前会话的 JAVA_HOME/PATH
        if (Test-Path "$($neo4jHome.FullName)\bin\neo4j.ps1") {
            Start-Process -NoNewWindow -WorkingDirectory "$($neo4jHome.FullName)\bin" -FilePath "powershell" -ArgumentList "-ExecutionPolicy Bypass -File `"$($neo4jHome.FullName)\bin\neo4j.ps1`" console"
        } else {
            Start-Process -NoNewWindow -WorkingDirectory "$($neo4jHome.FullName)\bin" -FilePath "neo4j.bat" -ArgumentList "console"
        }
    } else {
        Write-Host "未找到 deps/neo4j-community-*/bin/neo4j.bat，跳过 Neo4j（后端将使用本地 JSONL 降级）" -ForegroundColor Yellow
    }
}

# Postgres（项目内自包含）
if (-not $SkipPostgres) {
    $pgCtl = "$DEPS\pgsql\bin\pg_ctl.exe"
    if (Test-Path $pgCtl) {
        $pgData = "$DATA\pgdata"
        New-Item -ItemType Directory -Force -Path $pgData | Out-Null
        # 如果数据目录未初始化，则 initdb
        if (-not (Test-Path "$pgData\PG_VERSION")) {
            Write-Host "初始化 PostgreSQL 数据目录: $pgData" -ForegroundColor Cyan
            $initdb = "$DEPS\pgsql\bin\initdb.exe"
            Start-Process -NoNewWindow -Wait -WorkingDirectory "$DEPS\pgsql\bin" -FilePath $initdb -ArgumentList "-D `"$pgData`" -U ont --auth=trust --no-locale --encoding=UTF8"
            # 创建数据库 ont
            Start-Process -NoNewWindow -Wait -WorkingDirectory "$DEPS\pgsql\bin" -FilePath "$DEPS\pgsql\bin\createdb.exe" -ArgumentList "-h localhost -p 5432 -U ont ont"
        }
        Write-Host "启动 PostgreSQL" -ForegroundColor Cyan
        Start-Process -NoNewWindow -WorkingDirectory "$DEPS\pgsql\bin" -FilePath $pgCtl -ArgumentList "start -D `"$pgData`" -l `"$DATA\postgres.log`""
    } else {
        Write-Host "未找到 deps/pgsql/bin/pg_ctl.exe，跳过 Postgres（向量索引不可用）" -ForegroundColor Yellow
    }
}

# 后端
Write-Host "启动后端: http://localhost:8000" -ForegroundColor Green
Start-Process -NoNewWindow -WorkingDirectory "$ROOT\backend" -FilePath "python" -ArgumentList "-m uvicorn main:app --host 0.0.0.0 --port 8000 --reload"
