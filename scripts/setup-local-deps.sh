#!/usr/bin/env bash
# 企业业务本体平台 — 本地依赖一键安装脚本（Linux/macOS/WSL）
# 无需 Docker，直接下载并解压 Jena/Fuseki + Neo4j + Postgres 到 deps/ 目录
# 用法：./scripts/setup-local-deps.sh

set -euo pipefail

JENA_VERSION="${JENA_VERSION:-5.1.0}"
NEO4J_VERSION="${NEO4J_VERSION:-5.20.0}"
POSTGRES_VERSION="${POSTGRES_VERSION:-16.4-1}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEPS="$ROOT/deps"

info() { echo -e "\033[36m[INFO]\033[0m $*"; }
ok() { echo -e "\033[32m[OK]\033[0m   $*"; }
warn() { echo -e "\033[33m[WARN]\033[0m $*"; }

mkdir -p "$DEPS"

# 检查 Java
if ! command -v java &>/dev/null; then
  warn "未检测到 Java，请安装 JDK 17+ 后重试。"
  exit 1
fi
JAVA_VER=$(java -version 2>&1 | grep -oE '"[0-9]+' | head -1 | tr -d '"')
if [ "$JAVA_VER" -lt 17 ]; then
  warn "需要 Java 17+，当前版本 $JAVA_VER"
  exit 1
fi
ok "检测到 Java $JAVA_VER"

# Jena Fuseki
JENA_BASE="apache-jena-fuseki-$JENA_VERSION"
JENA_TAR="$JENA_BASE.tar.gz"
JENA_URL="https://archive.apache.org/dist/jena/binaries/$JENA_TAR"
JENA_DEST="$DEPS/$JENA_BASE"

if [ -d "$JENA_DEST" ]; then
  ok "Jena/Fuseki 已存在于 $JENA_DEST"
else
  info "下载 Jena/Fuseki $JENA_VERSION ..."
  curl -fsSL "$JENA_URL" -o "$DEPS/$JENA_TAR"
  info "解压 Jena/Fuseki ..."
  tar -xzf "$DEPS/$JENA_TAR" -C "$DEPS"
  rm "$DEPS/$JENA_TAR"
  ok "Jena/Fuseki 安装完成"
fi

# Neo4j
NEO4J_BASE="neo4j-community-$NEO4J_VERSION"
NEO4J_TAR="$NEO4J_BASE-unix.tar.gz"
NEO4J_URL="https://dist.neo4j.org/$NEO4J_TAR"
NEO4J_DEST="$DEPS/$NEO4J_BASE"

if [ -d "$NEO4J_DEST" ]; then
  ok "Neo4j 已存在于 $NEO4J_DEST"
else
  info "下载 Neo4j Community $NEO4J_VERSION ..."
  curl -fsSL "$NEO4J_URL" -o "$DEPS/$NEO4J_TAR"
  info "解压 Neo4j ..."
  tar -xzf "$DEPS/$NEO4J_TAR" -C "$DEPS"
  rm "$DEPS/$NEO4J_TAR"

  if [ -x "$NEO4J_DEST/bin/neo4j-admin" ]; then
    info "初始化 Neo4j 密码..."
    "$NEO4J_DEST/bin/neo4j-admin" dbms set-initial-password neo4jsecret 2>/dev/null || true
  fi
  ok "Neo4j 安装完成"
fi

# Postgres (Linux x86_64 binaries)
PG_BASE="postgresql-$POSTGRES_VERSION-linux-x64-binaries"
PG_TAR="$PG_BASE.tar.gz"
PG_URL="https://get.enterprisedb.com/postgresql/$PG_TAR"
PG_DEST="$DEPS/pgsql"
PG_DATA="$PG_DEST/data"

if [ -d "$PG_DEST" ]; then
  ok "Postgres 已存在于 $PG_DEST"
else
  info "下载 Postgres $POSTGRES_VERSION ..."
  curl -fsSL "$PG_URL" -o "$DEPS/$PG_TAR"
  info "解压 Postgres ..."
  tar -xzf "$DEPS/$PG_TAR" -C "$DEPS"
  # 移动到 deps/pgsql
  mv "$DEPS/pgsql"* "$PG_DEST" 2>/dev/null || true
  rm "$DEPS/$PG_TAR"

  info "初始化 Postgres 数据库..."
  "$PG_DEST/bin/initdb" -D "$PG_DATA" -U ont --auth=trust --no-instructions --encoding=UTF8

  info "启动 Postgres 以创建 ont 数据库..."
  "$PG_DEST/bin/pg_ctl" -D "$PG_DATA" -l "$PG_DEST/postgres.log" start

  "$PG_DEST/bin/createdb" -U ont ont

  ok "Postgres 安装完成"
fi

echo ""
ok "本地依赖准备完毕"
echo "  Jena/Fuseki: $JENA_DEST"
echo "  Neo4j:       $NEO4J_DEST"
echo "  Postgres:    $PG_DEST"
echo ""
echo -e "\033[36m现在可以运行 ./scripts/start-all.sh 启动完整服务（无需 Docker）\033[0m"
