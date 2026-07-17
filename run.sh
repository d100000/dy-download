#!/usr/bin/env bash
# 抖音下载器 · 本地一键启动
#   首次运行会自动创建虚拟环境并安装依赖。
#   用法:  ./run.sh              # 默认端口 3344
#          PORT=8010 ./run.sh    # 指定端口
#          ADMIN_PASSWORD=xxx ./run.sh
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-3344}"
PY="${PYTHON:-python3}"
VENV=".venv"

if [ ! -d "$VENV" ]; then
  echo "▶ 创建虚拟环境 $VENV ..."
  "$PY" -m venv "$VENV"
  "$VENV/bin/pip" install --quiet --upgrade pip
  echo "▶ 安装依赖 ..."
  "$VENV/bin/pip" install --quiet -r requirements.txt
fi

echo "▶ 启动服务：http://127.0.0.1:$PORT   （管理后台 /admin）"
echo "  管理密码：${ADMIN_PASSWORD:-douyin-admin（默认，生产请用 ADMIN_PASSWORD 覆盖）}"
exec "$VENV/bin/uvicorn" server:app --host 0.0.0.0 --port "$PORT"
