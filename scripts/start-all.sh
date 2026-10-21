#!/usr/bin/env bash
# 企业业务本体平台 — Linux/macOS/WSL 一键启动脚本
# 启动依赖（Jena/Fuseki、Neo4j）+ 后端 FastAPI + 前端静态服务
# 用法：./scripts/start-all.sh

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BACKEND="$ROOT/backend"
FRONTEND="$ROOT/frontend"
DEPS="$ROOT/deps"
BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-8123}"
USE_DOCKER="${USE_DOCKER:-0}"

info() { echo -e "\033[36m[INFO]\033[0m $*"; }
ok() { echo -e "\033[32m[OK]\033[0m   $*"; }
warn() { echo -e "\033[33m[WARN]\033[0m $*"; }

wait_http() {
  local url="$1" timeout="${2:-120}"
  for ((i=0;i<timeout;i++)); do
    if curl -sf "$url" >/dev/null 2>&1; then return 0; fi
    sleep 1
  done
  return 1
}

find_dep() {
  ls -d "$DEPS/$1"* 2>/dev/null | sort -V | tail -1
}

# 1. 停止旧的前后端正进程
info "查找并停止旧的前后端正进程..."
pkill -f "python.*main.py" 2>/dev/null || true
pkill -f "python.*http.server.*$FRONTEND_PORT" 2>/dev/null || true

# 1.5 停止旧本地依赖进程
if [ "${SKIP_DEPS:-}" != "1" ]; then
  pkill -f "fuseki-server" 2>/dev/null || true
  pkill -f "neo4j.*console" 2>/dev/null || true
fi

DEP_PIDS=""

# 2. 依赖服务
if [ "${SKIP_DEPS:-}" != "1" ]; then
  if [ "$USE_DOCKER" == "1" ] && command -v docker &>/dev/null; then
    info "启动依赖服务（Docker 模式）..."
    [ "${STOP_DEPS:-}" == "1" ] && docker compose -f "$ROOT/docker-compose.yml" down 2>/dev/null || true
    docker compose -f "$ROOT/docker-compose.yml" up -d jena neo4j postgres

    info "等待 Jena/Fuseki (3030)..."
    wait_http "http://localhost:3030/" 120 && ok "Jena 就绪" || warn "Jena 未在 120 秒内就绪"

    info "等待 Neo4j (7474)..."
    wait_http "http://localhost:7474/" 120 && ok "Neo4j 就绪" || warn "Neo4j 未在 120 秒内就绪"
  else
    JENA_DIR=$(find_dep "apache-jena-fuseki-")
    NEO4J_DIR=$(find_dep "neo4j-community-")
    PG_DIR="$DEPS/pgsql"
    if [ -z "$JENA_DIR" ] || [ -z "$NEO4J_DIR" ] || [ ! -d "$PG_DIR" ]; then
      warn "未找到本地 Jena/Fuseki、Neo4j 或 Postgres。"
      warn "请先运行安装脚本：./scripts/setup-local-deps.sh"
      warn "或安装 Docker 后使用 USE_DOCKER=1。"
      warn "本次将仅启动前后端（部分功能降级）。"
    else
      info "启动本地 Jena/Fuseki..."
      cd "$JENA_DIR"
      nohup ./fuseki-server --mem --update /ontology > "$ROOT/jena.log" 2>&1 &
      DEP_PIDS="$DEP_PIDS $!"

      info "启动本地 Neo4j..."
      cd "$NEO4J_DIR"
      nohup ./bin/neo4j console > "$ROOT/neo4j.log" 2>&1 &
      DEP_PIDS="$DEP_PIDS $!"

      info "启动本地 Postgres..."
      nohup "$PG_DIR/bin/pg_ctl" -D "$PG_DIR/data" -l "$PG_DIR/postgres.log" start > /dev/null 2>&1 &
      DEP_PIDS="$DEP_PIDS $!"

      cd "$ROOT"
      info "等待 Jena/Fuseki (3030)..."
      wait_http "http://localhost:3030/" 120 && ok "Jena 就绪" || warn "Jena 未在 120 秒内就绪"

      info "等待 Neo4j (7474)..."
      wait_http "http://localhost:7474/" 120 && ok "Neo4j 就绪" || warn "Neo4j 未在 120 秒内就绪"

      info "等待 Postgres (5432)..."
      for ((i=0;i<60;i++)); do
        if nc -z localhost 5432 2>/dev/null; then ok "Postgres 就绪"; break; fi
        sleep 1
      done || warn "Postgres 未在 60 秒内就绪"
    fi
  fi
else
  info "SKIP_DEPS=1，跳过依赖服务。"
fi

# 3. 启动后端
info "启动后端 FastAPI (端口 $BACKEND_PORT)..."
(
  cd "$BACKEND"
  export JENA_BASE_URL="http://localhost:3030"
  export JENA_DATASET="ontology"
  export NEO4J_URI="bolt://localhost:7687"
  export NEO4J_USER="neo4j"
  export NEO4J_PASSWORD="neo4jsecret"
  export POSTGRES_DSN="postgresql://ont:ontsecret@localhost:5432/ont"
  nohup python main.py > "$ROOT/backend.log" 2>&1 &
)

# 4. 启动前端
info "启动前端静态服务 (端口 $FRONTEND_PORT)..."
(
  cd "$FRONTEND"
  nohup python -m http.server "$FRONTEND_PORT" > "$ROOT/frontend.log" 2>&1 &
)

# 5. 等待后端健康
info "等待后端健康检查..."
wait_http "http://localhost:$BACKEND_PORT/health" 60 && ok "后端已响应" || warn "后端未在 60 秒内响应"

# 6. 输出访问信息
echo ""
ok "所有服务已启动"
echo "  前端页面: http://localhost:$FRONTEND_PORT"
echo "  后端 API: http://localhost:$BACKEND_PORT"
echo "  后端文档: http://localhost:$BACKEND_PORT/docs"
echo "  健康检查: http://localhost:$BACKEND_PORT/health"
echo ""
echo "按 Ctrl+C 停止前后端（本地依赖进程不会自动停止，需手动 kill）"
wait

# 停止前后端
pkill -f "python.*main.py" 2>/dev/null || true
pkill -f "python.*http.server.*$FRONTEND_PORT" 2>/dev/null || true

# 停止 Docker 依赖
if [ "$USE_DOCKER" == "1" ] && command -v docker &>/dev/null; then
  docker compose -f "$ROOT/docker-compose.yml" down 2>/dev/null || true
fi
