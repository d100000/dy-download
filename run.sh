#!/usr/bin/env bash
# 抖音下载器 · 本地一键启动
#   首次运行会自动创建虚拟环境并安装依赖。
#   用法:  ./run.sh              # 默认端口 3344
#          PORT=8010 ./run.sh    # 指定端口
#          ADMIN_PASSWORD=xxx ./run.sh
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-3344}"
HOST="${HOST:-127.0.0.1}"
PY="${PYTHON:-python3}"
VENV=".venv"

if [ ! -d "$VENV" ]; then
  echo "▶ 创建虚拟环境 $VENV ..."
  "$PY" -m venv "$VENV"
  "$VENV/bin/pip" install --quiet --upgrade pip
  echo "▶ 安装依赖 ..."
  "$VENV/bin/pip" install --quiet -r requirements.txt
fi

echo "▶ 启动服务：http://$HOST:$PORT   （管理后台 /admin_d）"
if [ -n "${ADMIN_PASSWORD:-}" ]; then
  echo "  管理密码：已通过 ADMIN_PASSWORD 配置（不会回显）"
else
  echo "  管理密码：仍为默认值，生产环境请设置 ADMIN_PASSWORD"
fi
exec "$VENV/bin/uvicorn" server:app --host "$HOST" --port "$PORT" --no-access-log
