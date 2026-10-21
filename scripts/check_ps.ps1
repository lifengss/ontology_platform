$err = @()
[System.Management.Automation.PSParser]::Tokenize((Get-Content $PSScriptRoot\start_mvp_local.ps1 -Raw), [ref]$err)
if ($err.Count) {
    $err | ForEach-Object { Write-Host ('Line {0}: {1}' -f $_.Token.StartLine, $_.Message) }
} else {
    Write-Host 'OK'
}
