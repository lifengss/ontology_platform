# build.ps1 — 打包主流程：组装 payload（干净快照+精选依赖+运行时包）→ makensis → 发行 zip
# 用法：powershell -NoProfile -ExecutionPolicy Bypass -File build.ps1 [-SkipNsis] [-SkipZip]
param(
    [switch]$SkipNsis,
    [switch]$SkipZip
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$inst = $PSScriptRoot
$payload = Join-Path $inst "payload"
$downloads = Join-Path $inst "downloads"

function RoboCopy($src, $dst, $excludeDirs, $excludeFiles) {
    $a = @($src, $dst, "/E", "/NFL", "/NDL", "/NJH", "/NJS", "/NP", "/R:1", "/W:1")
    if ($excludeDirs) { $a += "/XD"; $a += $excludeDirs }
    if ($excludeFiles) { $a += "/XF"; $a += $excludeFiles }
    $proc = Start-Process -FilePath "robocopy" -ArgumentList $a -NoNewWindow -Wait -PassThru
    if ($proc.ExitCode -ge 8) { throw "robocopy 失败 $src -> $dst (exit=$($proc.ExitCode))" }
}

Write-Host "===== [1/6] 清空并重建 payload ====="
if (Test-Path $payload) { Remove-Item $payload -Recurse -Force }
New-Item -ItemType Directory -Path (Join-Path $payload "runtime\wheelhouse"), (Join-Path $payload "scripts"), (Join-Path $payload "docs") -Force | Out-Null

Write-Host "===== [2/6] 复制 backend / frontend（干净快照，排除运行时数据/机密/开发文件）====="
RoboCopy (Join-Path $root "backend") (Join-Path $payload "backend") @("data", "logs", "secrets", "__pycache__", ".pytest_cache") @(".env", "startup.err")
RoboCopy (Join-Path $root "frontend") (Join-Path $payload "frontend") $null @("nginx.conf")

Write-Host "===== [3/6] 精选依赖 deps ====="
# Jena/Fuseki：白名单——只拷运行所需（避开带空格目录名的排除法坑）
$jenaSrc = Join-Path $root "deps\apache-jena-fuseki-5.1.0"
$jenaDst = Join-Path $payload "deps\apache-jena-fuseki-5.1.0"
foreach ($d in @("bin", "webapp")) { RoboCopy (Join-Path $jenaSrc $d) (Join-Path $jenaDst $d) $null $null }
foreach ($f in @("fuseki-server.jar", "fuseki-server.bat", "fuseki-server", "LICENSE", "log4j2.properties", "NOTICE", "README")) {
    Copy-Item (Join-Path $jenaSrc $f) (Join-Path $jenaDst $f) -ErrorAction SilentlyContinue
}
# Postgres：白名单——只拷 bin/lib/share（排除 data/pgAdmin/doc/include/symbols/StackBuilder）
foreach ($d in @("bin", "lib", "share")) { RoboCopy (Join-Path $root "deps\pgsql\$d") (Join-Path $payload "deps\pgsql\$d") $null $null }
# JDK 与 Neo4j：直接带发行 zip（安装期按需解压）
Copy-Item (Join-Path $root "deps\jdk21.zip") (Join-Path $payload "runtime\")
Copy-Item (Join-Path $root "deps\neo4j-community-2026.09.0-windows.zip") (Join-Path $payload "runtime\")

Write-Host "===== [4/6] 运行时包：Python 安装包 + wheelhouse ====="
$pyInstallerSrc = Get-ChildItem $downloads -Filter "python-3.10*.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $pyInstallerSrc) { throw "缺少捆绑 Python 安装包，请先下载到 $downloads（python-3.10.x-amd64.exe）" }
Copy-Item $pyInstallerSrc.FullName (Join-Path $payload "runtime\")
$wh = Join-Path $downloads "wheelhouse"
if (-not (Test-Path $wh)) { throw "缺少 wheelhouse，请先执行：python -m pip download -r backend\requirements.txt -d $wh" }
$whFiles = Get-ChildItem $wh -File
if ($whFiles.Count -lt 10) { throw "wheelhouse 文件数异常（$($whFiles.Count)）" }
Copy-Item $whFiles.FullName (Join-Path $payload "runtime\wheelhouse\")
# 计算 wheelhouse 实际支持的 Python minor 集：对每个含 cp 专属 wheel（-cp3XX-cp3XX-）的包
# 取跨包 cp 标签交集（任一包缺该版本 wheel 则该版本离线装不上）；abi3 仅构成下限、纯 wheel 不限。
# 结果写入 wheelhouse-target.json，install-deps.ps1 据此过滤系统 Python 候选（>=3.10，不必非得 3.10）。
$cpPkgMinors = @{}
foreach ($wf in $whFiles) {
    if ($wf.Name -match '-cp3(\d+)-cp3\1-') {
        $pkg = ($wf.Name -split '-')[0]
        $mn = [int]$Matches[1]
        if (-not $cpPkgMinors.ContainsKey($pkg)) { $cpPkgMinors[$pkg] = New-Object System.Collections.Generic.HashSet[int] }
        [void]$cpPkgMinors[$pkg].Add($mn)
    }
}
$supportedMinors = $null
foreach ($pkg in $cpPkgMinors.Keys) {
    if ($null -eq $supportedMinors) { $supportedMinors = New-Object System.Collections.Generic.HashSet[int]; [void]$supportedMinors.UnionWith($cpPkgMinors[$pkg]) }
    else { [void]$supportedMinors.IntersectWith($cpPkgMinors[$pkg]) }
}
if ($null -eq $supportedMinors -or $supportedMinors.Count -eq 0) { $supportedMinors = New-Object System.Collections.Generic.HashSet[int]; [void]$supportedMinors.Add(10) }
$minorsSorted = @($supportedMinors | Sort-Object)
$whTargetJson = '{"major": 3, "minors": [' + (($minorsSorted | ForEach-Object { "$_" }) -join ', ') + ']}'
[System.IO.File]::WriteAllText((Join-Path $payload "runtime\wheelhouse-target.json"), $whTargetJson, (New-Object System.Text.UTF8Encoding($false)))
Write-Host ("wheelhouse: {0} 个文件，支持 Python 3.{1}" -f $whFiles.Count, ($minorsSorted -join '/'))

Write-Host "===== [5/6] 脚本与产品文档 ====="
Copy-Item (Join-Path $inst "scripts\*.ps1") (Join-Path $payload "scripts\")
Copy-Item (Join-Path $inst "scripts\post-install.bat") (Join-Path $payload "scripts\")
Copy-Item (Join-Path $inst "docs\*.md") (Join-Path $payload "docs\")
# 快捷方式图标（源 installer\icons\，生成器: <workspace>\scripts\make-shortcut-icons.ps1）
New-Item -ItemType Directory -Force -Path (Join-Path $payload "icons") | Out-Null
Copy-Item (Join-Path $inst "icons\*.ico") (Join-Path $payload "icons\")
# 产品说明（安装目录根）
Copy-Item (Join-Path $inst "docs\产品说明.md") (Join-Path $payload "README-产品说明.md")

# payload 体积
$ps = (Get-ChildItem $payload -Recurse -File | Measure-Object Length -Sum).Sum
Write-Host ("payload 完成：{0:N1} MB，文件 {1} 个" -f ($ps / 1MB), (Get-ChildItem $payload -Recurse -File).Count)

if (-not $SkipNsis) {
    Write-Host "===== [6/6] makensis 编译 ====="
    $makensis = "C:\Program Files (x86)\NSIS\makensis.exe"
    if (-not (Test-Path $makensis)) { throw "未找到 makensis：$makensis" }
    # makensis 不会自建输出目录，缺 dist 会报 "Can't open output file"
    New-Item -ItemType Directory -Force -Path (Join-Path $inst "dist") | Out-Null
    Push-Location $inst
    try { & $makensis "setup.nsi" } finally { Pop-Location }
    if ($LASTEXITCODE -ne 0) { throw "makensis 编译失败（exit=$LASTEXITCODE）" }
    $exe = Get-ChildItem (Join-Path $inst "dist") -Filter "*.exe" | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    Write-Host ("安装器产出：{0}（{1:N1} MB）" -f $exe.FullName, ($exe.Length / 1MB))
}

if (-not $SkipZip) {
    Write-Host "===== 发行 zip ====="
    $distDir = Join-Path $inst "dist"
    $exeName = Get-ChildItem $distDir -Filter "*.exe" | Sort-Object LastWriteTime -Descending | Select-Object -First 1 | ForEach-Object { $_.Name }
    Copy-Item (Join-Path $inst "docs") (Join-Path $distDir "docs") -Recurse -Force
    $zip = Join-Path $distDir "OntologyPlatform-1.0.2-windows-dist.zip"
    if (Test-Path $zip) { Remove-Item $zip -Force }
    $py = Get-Command python -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($py) {
        & python -c ("import zipfile,os; d=r'$distDir'; z=zipfile.ZipFile(r'$zip','w',zipfile.ZIP_DEFLATED,compresslevel=1); [z.write(os.path.join(d,f),f) for f in [r'$exeName','docs/产品说明.md'] if os.path.exists(os.path.join(d,f))]; z.close(); print('zip ok')")
    } else {
        Compress-Archive -Path (Join-Path $distDir $exeName) -DestinationPath $zip -Force
    }
    Write-Host ("发行 zip：{0}（{1:N1} MB）" -f $zip, ((Get-Item $zip).Length / 1MB))
}
Write-Host "===== build 完成 ====="
