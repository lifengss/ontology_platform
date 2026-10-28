@echo off
rem Fuseki (Jena 5.1.0) 启动脚本（正式化自 tmp_verify_ingest/start_fuseki.bat，2026-10-28）
rem 数据集：ontology（TDB2 持久化到 backend/data/fuseki），端口 3030
rem 配置文件：backend/fuseki-config.ttl（TDB2 装配器 tdb2:DatasetTDB2；勿改回 tdb:DatasetTDB，
rem           否则在 TDB2 格式存储上会静默回退 in-memory，数据重启即丢）
rem 注意：启动前若 :3030 已被占用，先停掉监听进程（或直接用 scripts\restart_fuseki.ps1 一键重启）
cd /d D:\self_coding\ontology_platform\deps\apache-jena-fuseki-5.1.0
D:\self_coding\ontology_platform\deps\jdk-21.0.12.1\bin\java.exe -Xmx1200M -jar fuseki-server.jar --port 3030 --config "D:\self_coding\ontology_platform\backend\fuseki-config.ttl"
