$ErrorActionPreference = 'Continue'
$root = 'd:\self_coding\ontology_platform'

# 1) 清理 8000 / 5432 上的残留 http.server
foreach ($p in 8000, 5432) {
  $conns = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue
  if ($conns) {
    $conns | Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object {
      Write-Output ("KILL port=$p pid=" + $_)
      Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue
    }
  }
}
Start-Sleep -Seconds 2

# 2) Fuseki 5.1.0 (JDK21, 使用 backend/fuseki-config.ttl 配置)
Start-Process -NoNewWindow -WorkingDirectory "$root\backend" `
  -FilePath "$root\deps\jdk-21.0.12.1\bin\java.exe" `
  -ArgumentList "-Xmx1200M", "-jar", "$root\deps\apache-jena-fuseki-5.1.0\fuseki-server.jar", "--port", "3030", "--config", "$root\backend\fuseki-config.ttl" `
  -RedirectStandardOutput "$root\deps\_fuseki.log" -RedirectStandardError "$root\deps\_fuseki_err.log"
Write-Output 'FUSEKI_LAUNCHED'

# 3) Neo4j Community (JAVA_HOME 指向 deps 内 JDK21)
$env:JAVA_HOME = "$root\deps\jdk-21.0.12.1"
Start-Process -NoNewWindow -WorkingDirectory "$root\deps\neo4j-community-2026.09.0" `
  -FilePath "$root\deps\neo4j-community-2026.09.0\bin\neo4j.bat" -ArgumentList 'console' `
  -RedirectStandardOutput "$root\deps\_neo4j.log" -RedirectStandardError "$root\deps\_neo4j_err.log"
Write-Output 'NEO4J_LAUNCHED'

# 4) PostgreSQL 16 (数据目录 backend/data/pgdata)
& "$root\deps\pgsql\bin\pg_ctl.exe" -D "$root\backend\data\pgdata" -l "$root\backend\data\pg.log" start
Write-Output 'PG_START_ISSUED'

# 5) FastAPI 后端
$env:PYTHONUTF8 = '1'
Start-Process -NoNewWindow -WorkingDirectory "$root\backend" -FilePath 'python' `
  -ArgumentList '-m', 'uvicorn', 'main:app', '--host', '0.0.0.0', '--port', '8000' `
  -RedirectStandardOutput "$root\backend\uvicorn_out.log" -RedirectStandardError "$root\backend\uvicorn_err.log"
Write-Output 'BACKEND_LAUNCHED'
