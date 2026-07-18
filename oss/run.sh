#!/usr/bin/env bash
# 一键启动开源基础版
set -euo pipefail
cd "$(dirname "$0")"
PORT="${PORT:-8000}"
[ -d .venv ] || { python3 -m venv .venv; .venv/bin/pip install -q -r requirements.txt; }
echo "▶ http://127.0.0.1:$PORT   (可选 PROXY=socks5://... 走代理)"
exec .venv/bin/uvicorn server:app --host 0.0.0.0 --port "$PORT"
