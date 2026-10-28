# 重启 Fuseki（Jena 5.1.0，:3030）：停监听进程 → start_fuseki.bat → 轻量 SPARQL 探测就绪
# 用法：powershell -NoProfile -ExecutionPolicy Bypass -File scripts\restart_fuseki.ps1
$root = Split-Path -Parent $PSScriptRoot

$conn = Get-NetTCPConnection -LocalPort 3030 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($conn) {
    Stop-Process -Id $conn.OwningProcess -Force
    Start-Sleep -Seconds 2
    Write-Host ("stopped Fuseki PID={0}" -f $conn.OwningProcess)
} else {
    Write-Host "no listener on :3030"
}

Start-Process -FilePath (Join-Path $root "scripts\start_fuseki.bat") -WindowStyle Minimized

# 就绪探测：Jena 5.1.0 根路径返回 404，必须探测数据集 query 端点的轻量 SPARQL
$ok = $false
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 1
    try {
        $r = Invoke-WebRequest -Uri "http://localhost:3030/ontology/query" -Method POST `
            -Body "query=SELECT ?s WHERE {?s ?p ?o} LIMIT 1" `
            -ContentType "application/x-www-form-urlencoded" -TimeoutSec 2 -UseBasicParsing
        if ($r.StatusCode -eq 200) { $ok = $true; break }
    } catch { }
}
if ($ok) {
    Write-Host "fuseki ready on :3030"
} else {
    Write-Host "fuseki NOT ready after 30s, check deps\apache-jena-fuseki-5.1.0\_fuseki*.log"
    exit 1
}
