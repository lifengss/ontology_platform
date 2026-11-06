# repair-python-bundled.ps1
# 自愈脚本：当常规静默安装无论如何修不好时，完全绕开安装引擎（MSI 0xc00000fd 栈溢出/LaunchCondition 死锁），
# 直接从 Windows Installer 缓存里取 Python 3.10.* 组件 MSI 载荷，用 msiexec /a（管理安装=纯文件解包，
# 不写注册表、不跑 LaunchCondition、不跑 CustomAction）解包，robocopy 部署到 runtime\python，ensurepip 补 pip。
#
# 退出码：0 成功 / 3 找不到载荷或 COM 不可用 / 4 解包未产出 python.exe / 5 部署后解释器仍不可运行
# 纯 ASCII（避开 PowerShell 5.1 的 BOM/编码陷阱）。
param(
    [string]$TargetDir = ""
)

$ErrorActionPreference = 'Stop'
function Test-Runnable([string]$exe) {
    if (-not (Test-Path $exe)) { return $false }
    $oldHome = $env:PYTHONHOME; $oldPath = $env:PYTHONPATH
    $env:PYTHONHOME = $null; $env:PYTHONPATH = $null
    try {
        $out = (& $exe -c "import sys,encodings,site;print(sys.prefix)" 2>$null | Out-String).Trim()
        $code = $LASTEXITCODE
        $env:PYTHONHOME = $oldHome; $env:PYTHONPATH = $oldPath
        return (($code -eq 0) -and ($out -match 'python'))
    } catch {
        $env:PYTHONHOME = $oldHome; $env:PYTHONPATH = $oldPath
        return $false
    }
}

if ($TargetDir -eq "") {
    Write-Host "用法: repair-python-bundled.ps1 -TargetDir <runtime\python 绝对路径>"
    exit 3
}
$pyTarget = $TargetDir
$pyExe = Join-Path $pyTarget "python.exe"
if (Test-Runnable $pyExe) { Write-Host "REPAIR_OK (already runnable)"; exit 0 }

try { $wi = New-Object -ComObject WindowsInstaller.Installer }
catch { Write-Host "COM_UNAVAILABLE"; exit 3 }

$staging = Join-Path $env:TEMP ("py310x_" + [Guid]::NewGuid().ToString("N").Substring(0,8))
New-Item -ItemType Directory -Path $staging -Force | Out-Null
$found = $false
foreach ($msi in (Get-ChildItem $env:WINDIR\Installer -Filter *.msi -ErrorAction SilentlyContinue)) {
    try {
        $db = $wi.OpenDatabase($msi.FullName, 0)
        $v = $db.OpenView("SELECT Value FROM Property WHERE Property='ProductName'")
        $v.Execute()
        $row = $v.Fetch()
        $name = if ($row) { $row.StringData(1) } else { '' }
        if ($name -like 'Python 3.10*') {
            $found = $true
            Write-Host ("提取载荷: {0}" -f $name)
            $ext = Join-Path $staging ("ext_" + [Guid]::NewGuid().ToString("N").Substring(0,8))
            New-Item -ItemType Directory -Path $ext -Force | Out-Null
            & msiexec.exe /a "$($msi.FullName)" "TARGETDIR=$ext" /qn | Out-Null
            # 把解包产物合入 staging 根（多组件叠加）
            robocopy $ext $staging /E /NFL /NDL /NJH /NJS /NC /NS | Out-Null
        }
    } catch { continue }
}

if (-not $found) { Write-Host "NO_PAYLOAD"; exit 3 }
if (-not (Test-Path (Join-Path $staging "python.exe"))) { Write-Host "NO_PYTHON_EXE"; exit 4 }

# 部署到目标 runtime\python
if (-not (Test-Path $pyTarget)) { New-Item -ItemType Directory -Path $pyTarget -Force | Out-Null }
robocopy $staging $pyTarget /E /NFL /NDL /NJH /NJS /NC /NS | Out-Null
# 补 pip
try { & (Join-Path $pyTarget "python.exe") -m ensurepip --default-pip 2>$null | Out-Null } catch {}

if (Test-Runnable $pyExe) { Write-Host "REPAIR_OK"; exit 0 }
Write-Host "STILL_NOT_RUNNABLE"; exit 5
