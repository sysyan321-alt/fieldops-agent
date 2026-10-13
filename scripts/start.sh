#!/usr/bin/env bash
# scripts/start.sh — FieldOps 一键启动/停止脚本
#
# 用法：
#   ./scripts/start.sh                  # 基础设施 + （首次自动）初始化数据 + 后端
#   ./scripts/start.sh --with-frontend  # 上面全部 + 前端 Vite（http://localhost:3000）
#   ./scripts/start.sh --init-data      # 强制重跑种子数据与知识库初始化
#   ./scripts/start.sh --no-reload      # 后端关闭热重载
#   ./scripts/start.sh --stop           # 停止基础设施容器
#   ./scripts/start.sh --status         # 查看容器/后端健康状态
#   ./scripts/start.sh --dry-run        # 只打印将执行的命令，不真跑
#
# 设计说明：
# 1) 脚本会 export .env.local，这样 docker compose 与 scripts/*.py（只读 os.getenv 的脚本）
#    拿到的是同一套数据库/模型配置，避免"容器里是 A、脚本连 B"的连接失败。
# 2) 种子数据与 Milvus 知识库都不是幂等的（seed 的 service_records 会主键冲突，
#    init_milvus 会 drop 集合重建），因此默认只在检测到"数据为空"时才执行。

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

ENV_FILE="$ROOT_DIR/.env.local"
ENV_EXAMPLE="$ROOT_DIR/.env.example"

WITH_FRONTEND=0
FORCE_INIT_DATA=0
NO_RELOAD=0
DRY_RUN=0
MODE="start"

CONTAINERS=(fieldops_postgres fieldops_redis fieldops_milvus)

# ── 输出helpers ────────────────────────────────────────────────
log()  { printf '\033[1;36m[FieldOps]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[FieldOps]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[FieldOps]\033[0m %s\n' "$*" >&2; exit 1; }

run() {
  if [ "$DRY_RUN" = "1" ]; then
    printf '\033[1;36m[FieldOps]\033[0m   $ %s\n' "$*"
    return 0
  fi
  "$@"
}

usage() {
  sed -n '4,11p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit 0
}

while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help)        usage ;;
    --with-frontend|--all) WITH_FRONTEND=1 ;;
    --init-data)      FORCE_INIT_DATA=1 ;;
    --skip-data)      FORCE_INIT_DATA=0 ;;
    --no-reload)      NO_RELOAD=1 ;;
    --stop)           MODE="stop" ;;
    --status)         MODE="status" ;;
    --dry-run)        DRY_RUN=1 ;;
    *) die "未知参数：$1（用 --help 查看用法）" ;;
  esac
  shift
done

# ── 1. 环境与解释器 ────────────────────────────────────────────
PYTHON="${PYTHON:-}"
if [ -z "$PYTHON" ]; then
  if [ -x "$ROOT_DIR/.venv/bin/python" ]; then
    PYTHON="$ROOT_DIR/.venv/bin/python"
  else
    PYTHON="$(command -v python3 || command -v python || true)"
  fi
fi

load_env() {
  if [ ! -f "$ENV_FILE" ]; then
    warn "未找到 .env.local，正在从 .env.example 生成…"
    cp "$ENV_EXAMPLE" "$ENV_FILE"
    die "请先填写 $ENV_FILE 中的 DB_USER / DB_PASSWORD / JWT_SECRET_KEY / LLM API Key，然后重新执行。"
  fi
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a

  local missing=()
  for key in DB_USER DB_PASSWORD JWT_SECRET_KEY; do
    eval "value=\${$key:-}"     # 兼容 macOS 自带 bash 3.2（不支持 ${!key:-}）
    [ -n "$value" ] || missing+=("$key")
  done
  [ "${#missing[@]}" -eq 0 ] || die ".env.local 缺少必填项：${missing[*]}"

  case "${DEFAULT_LLM_PROVIDER:-deepseek}" in
    qwen)     [ -n "${QWEN_API_KEY:-}" ]     || warn "DEFAULT_LLM_PROVIDER=qwen，但 QWEN_API_KEY 为空，LLM 调用会失败" ;;
    deepseek) [ -n "${DEEPSEEK_API_KEY:-}" ] || warn "DEFAULT_LLM_PROVIDER=deepseek，但 DEEPSEEK_API_KEY 为空" ;;
  esac
  [ "$PYTHON" != "" ] || die "未找到 python3，请安装 Python 3.11+ 或设置 PYTHON=/path/to/python"
}

# ── 2. docker compose 封装 ─────────────────────────────────────
if docker compose version >/dev/null 2>&1; then
  COMPOSE_BIN=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE_BIN=(docker-compose)
else
  die "未找到 docker compose / docker-compose，请先安装 Docker Desktop"
fi

container_state() {
  docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$1" 2>/dev/null || echo "missing"
}

wait_infra() {
  local timeout="${1:-180}" start=$SECONDS
  log "等待基础设施就绪（最多 ${timeout}s）…"
  while :; do
    local pending=()
    for c in "${CONTAINERS[@]}"; do
      [ "$(container_state "$c")" = "healthy" ] || pending+=("$c")
    done
    if [ "${#pending[@]}" -eq 0 ]; then
      log "基础设施就绪：${CONTAINERS[*]}"
      return 0
    fi
    if [ $((SECONDS - start)) -ge "$timeout" ]; then
      warn "等待超时，以下容器未 healthy：${pending[*]}"
      "${COMPOSE_BIN[@]}" --env-file "$ENV_FILE" ps || true
      die "请查看容器日志：docker compose logs <服务名>"
    fi
    sleep 3
  done
}

# ── 3. 首次初始化（幂等检测，避免重复破坏数据）─────────────────
check_deps() {
  if ! "$PYTHON" -c 'import fastapi, uvicorn, sqlalchemy' >/dev/null 2>&1; then
    warn "当前 Python 缺少后端依赖：请执行 $PYTHON -m pip install -r requirements.txt，或用 PYTHON=/path/to/python 指定已装好依赖的解释器"
  fi
  if ! "$PYTHON" -c 'import torch, transformers' >/dev/null 2>&1; then
    warn "未检测到 torch/transformers（BGE 模型依赖）：SOP 问答的 RAG 检索会失败，可补装 transformers torch sentencepiece"
  fi
}

ensure_seed_data() {
  local count
  count="$("${COMPOSE_BIN[@]}" --env-file "$ENV_FILE" exec -T postgres \
    psql -U "$DB_USER" -d "${DB_NAME:-fieldops}" -tAc \
    "SELECT count(*) FROM sites" 2>/dev/null || echo "0")"
  if [ "$FORCE_INIT_DATA" = "0" ] && [ "${count:-0}" != "0" ]; then
    log "种子数据已存在（sites=${count}），跳过"
    return 0
  fi
  log "导入种子数据（点位/设备/服务记录）…"
  run "$PYTHON" scripts/seed_data.py
}

ensure_knowledge_base() {
  local entities
  # 打印 chunk 数：0=集合缺失或为空（需要初始化）；unknown=探测失败（不做破坏性重建）
  entities="$("$PYTHON" - <<'PY' 2>/dev/null || echo "unknown"
from pymilvus import MilvusClient
from backend.config import get_settings
from backend.core.knowledge_base import COLLECTION_NAME
try:
    s = get_settings()
    client = MilvusClient(uri=f"http://{s.milvus_host}:{s.milvus_port}")
    if not client.has_collection(COLLECTION_NAME):
        print(0)
    else:
        client.load_collection(COLLECTION_NAME)
        rows = client.query(collection_name=COLLECTION_NAME, filter="",
                            output_fields=["count(*)"])
        print(int(rows[0]["count(*)"]))
except Exception:
    print("unknown")
PY
)"
  if [ "$FORCE_INIT_DATA" = "0" ]; then
    case "$entities" in
      unknown)
        warn "无法确认 Milvus 知识库状态（连接失败？），已跳过初始化以免误删数据；需要重建请显式执行 --init-data"
        return 0 ;;
      0) : ;;   # 集合缺失或为空：走下面的初始化
      *)
        log "Milvus 知识库已有 ${entities} 条 chunk，跳过重建"
        return 0 ;;
    esac
  fi
  log "重建 Milvus 知识库并导入 data/sop（会先 drop 旧集合）…"
  run "$PYTHON" scripts/init_milvus.py
  if ! run "$PYTHON" scripts/build_knowledge_base.py --batch-dir data/sop; then
    warn "知识库构建失败（多为 LLM Key 或 BGE 模型权重缺失），后端仍会启动，SOP 问答将检索不到内容。"
  fi
}

# ── 4. 各模式实现 ──────────────────────────────────────────────
do_stop() {
  log "停止基础设施容器…"
  run "${COMPOSE_BIN[@]}" --env-file "$ENV_FILE" stop
  log "已停止（数据卷保留，重启不丢数据）"
}

do_status() {
  "${COMPOSE_BIN[@]}" --env-file "$ENV_FILE" ps
  for c in "${CONTAINERS[@]}"; do
    printf '%-26s %s\n' "$c" "$(container_state "$c")"
  done
  local url="http://localhost:${APP_PORT:-8000}/health"
  if curl -fsS --max-time 3 "$url" >/dev/null 2>&1; then
    log "后端健康：${url} → $(curl -fsS --max-time 3 "$url")"
  else
    warn "后端未响应：${url}（未启动或仍在启动中）"
  fi
}

start_backend() {
  local args=(backend.main:app --host "${APP_HOST:-0.0.0.0}" --port "${APP_PORT:-8000}")
  [ "$NO_RELOAD" = "0" ] && args+=(--reload)
  if [ "$WITH_FRONTEND" = "1" ]; then
    log "后台启动后端（日志：/tmp/fieldops_backend.log）…"
    if [ "$DRY_RUN" = "0" ]; then
      "$PYTHON" -m uvicorn "${args[@]}" >/tmp/fieldops_backend.log 2>&1 &
      BACKEND_PID=$!
      trap 'kill "$BACKEND_PID" 2>/dev/null || true' EXIT
    else
      run "$PYTHON" -m uvicorn "${args[@]}"
    fi
    if [ "$DRY_RUN" = "0" ]; then
      for _ in $(seq 1 40); do
        curl -fsS --max-time 2 "http://localhost:${APP_PORT:-8000}/health" >/dev/null 2>&1 && break
        sleep 1
      done
    fi
  else
    log "启动后端：http://localhost:${APP_PORT:-8000}（Ctrl+C 停止）"
    run "$PYTHON" -m uvicorn "${args[@]}"
  fi
}

start_frontend() {
  if [ ! -d "$ROOT_DIR/frontend/node_modules" ]; then
    log "安装前端依赖（frontend/npm install）…"
    run npm --prefix "$ROOT_DIR/frontend" install
  fi
  log "启动前端：http://localhost:3000（局域网地址看 Vite 输出的 Network 行）"
  run npm --prefix "$ROOT_DIR/frontend" run dev
}

# ── 5. 主流程 ──────────────────────────────────────────────────
case "$MODE" in
  stop)   load_env; do_stop; exit 0 ;;
  status) load_env; do_status; exit 0 ;;
esac

load_env
log "Python：$PYTHON"
[ "$DRY_RUN" = "1" ] && warn "dry-run 模式：只打印命令，不实际执行"
check_deps

log "启动基础设施（postgres/redis/milvus/etcd/minio）…"
run "${COMPOSE_BIN[@]}" --env-file "$ENV_FILE" up -d

if [ "$DRY_RUN" = "1" ]; then
  run "$PYTHON" scripts/seed_data.py
  run "$PYTHON" scripts/init_milvus.py
  run "$PYTHON" scripts/build_knowledge_base.py --batch-dir data/sop
  start_backend
  [ "$WITH_FRONTEND" = "1" ] && start_frontend
  exit 0
fi

docker info >/dev/null 2>&1 || die "Docker 未运行：请先启动 Docker Desktop"

wait_infra

ensure_seed_data
ensure_knowledge_base

start_backend
[ "$WITH_FRONTEND" = "1" ] && start_frontend

log "全部就绪。默认账号：supervisor / Supervisor@123（值班主管）"
