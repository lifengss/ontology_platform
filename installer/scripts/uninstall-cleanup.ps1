# uninstall-cleanup.ps1 -- pre-uninstall cleanup (2026-10-30 remote-machine 3 pitfalls, ASCII only)
# Called by NSIS Uninstall section BEFORE RMDir /r. Fixes:
#   1) residual processes locking the install dir (esp. cmd.exe left by start-platform.bat
#      with its CWD stuck in the install dir) -> kill them, then NSIS RMDir can succeed
#   2) shortcuts whose Chinese name does not match (PS 5.1 encoding) -> delete by .lnk TARGET path
param([string]$InstallDir = "")

$ErrorActionPreference = 'SilentlyContinue'
if ($InstallDir -eq "") { exit 0 }
$dir = $InstallDir.TrimEnd('\')
if (-not (Test-Path $dir)) { exit 0 }

# --- 1) stop services (best effort) ---
$stop = Join-Path $dir 'scripts\stop-services.ps1'
if (Test-Path $stop) {
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $stop -InstallDir $dir | Out-Null
}

# --- 2) kill processes locking the install dir ---
# NOTE: the NSIS uninstaller (Uninstall.exe) runs THIS script via ExecWait. Never kill it,
# nor our own PowerShell ($PID), nor msiexec. Only kill:
#   - processes whose ExecutablePath is under $dir (except Uninstall.exe / msiexec)
#   - cmd/powershell/conhost whose CommandLine references $dir (covers the CWD-locked cmd.exe)
$lower = $dir.ToLower()
$selfPid = $PID
$killNames = @('cmd.exe', 'powershell.exe', 'pwsh.exe', 'conhost.exe', 'java.exe', 'python.exe', 'pythonw.exe')
try { $procs = Get-CimInstance Win32_Process } catch { $procs = @() }
foreach ($p in $procs) {
    if ($null -eq $p -or $p.ProcessId -eq $selfPid) { continue }
    $name = "$($p.Name)"
    if ($name -ieq 'Uninstall.exe' -or $name -ieq 'Un_A.exe' -or $name -ieq 'msiexec.exe') { continue }
    $exe = "$($p.ExecutablePath)"; $cl = "$($p.CommandLine)".ToLower()
    $underDir = ($exe -and $exe.ToLower().StartsWith($lower))
    $cwdHint = ($killNames -contains $name.ToLower() -and $cl.Contains($lower))
    if ($underDir -or $cwdHint) {
        try { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue } catch {}
    }
}
Start-Sleep -Milliseconds 800

# --- 3) delete shortcuts by resolved TARGET path (avoids Chinese-name encoding mismatch) ---
$publicDesktop = Join-Path $env:PUBLIC 'Desktop'
$publicStart = Join-Path $env:ProgramData 'Microsoft\Windows\Start Menu\Programs'
$userDesktop = [Environment]::GetFolderPath('Desktop')
$userStart = [Environment]::GetFolderPath('Programs')
$scanDirs = @($userDesktop, $publicDesktop, $userStart, $publicStart) | Where-Object { $_ } | Select-Object -Unique
$ws = New-Object -ComObject WScript.Shell
foreach ($d in $scanDirs) {
    if (-not (Test-Path $d)) { continue }
    Get-ChildItem $d -Filter *.lnk -Recurse -ErrorAction SilentlyContinue | ForEach-Object {
        try {
            $t = $ws.CreateShortcut($_.FullName).TargetPath
            if ($t -and $t.ToLower().StartsWith($lower)) { Remove-Item $_.FullName -Force -ErrorAction SilentlyContinue }
        } catch {}
    }
}
exit 0
