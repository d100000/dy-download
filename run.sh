#!/usr/bin/env bash
# 抖音下载器 · 本地一键启动
#   首次运行会自动创建虚拟环境并安装依赖。
#   用法:  ./run.sh              # 默认端口 3344
#          PORT=8010 ./run.sh    # 指定端口
#          ADMIN_PASSWORD=xxx ./run.sh
set -euo pipefail
cd "$(dirname "$0")"

# 本地密码保存在 Git 忽略的 .env.local；显式环境变量始终优先。
# 只读取 ADMIN_PASSWORD 字段，不执行配置文件中的其他文本。
if [ "${ADMIN_PASSWORD+x}" != x ] && [ -f .env.local ]; then
  while IFS= read -r local_config_line || [ -n "$local_config_line" ]; do
    case "$local_config_line" in
      ADMIN_PASSWORD=*)
        export ADMIN_PASSWORD="${local_config_line#ADMIN_PASSWORD=}"
        break
        ;;
    esac
  done < .env.local
fi

PORT="${PORT:-3344}"
HOST="${HOST:-127.0.0.1}"
PY="${PYTHON:-python3}"
VENV=".venv"

if [ "${ADMIN_PASSWORD+x}" = x ] && [ -z "${ADMIN_PASSWORD}" ]; then
  echo "✖ ADMIN_PASSWORD 不能为空，请设置强密码后再启动。" >&2
  exit 1
fi

# 一旦监听超出本机回环，管理密码必须 fail-closed。
# 服务端也会再校验一次，这里提前给出可操作的启动错误。
if [ "$HOST" != "127.0.0.1" ] && [ "$HOST" != "localhost" ] \
   && [ "$HOST" != "::1" ] && [ "$HOST" != "[::1]" ]; then
  if [ -z "${ADMIN_PASSWORD:-}" ] || [ "${ADMIN_PASSWORD}" = "douyin-admin" ] \
     || [ "${#ADMIN_PASSWORD}" -lt 12 ]; then
    echo "✖ 非本机监听需要至少 12 位且非默认的 ADMIN_PASSWORD。" >&2
    exit 1
  fi
  export REQUIRE_ADMIN_PASSWORD=1
fi

if [ ! -d "$VENV" ]; then
  echo "▶ 创建虚拟环境 $VENV ..."
  "$PY" -m venv "$VENV"
  "$VENV/bin/pip" install --quiet --upgrade pip
  echo "▶ 安装依赖 ..."
  "$VENV/bin/pip" install --quiet -r requirements.txt
fi

if [ ! -x "$VENV/bin/uvicorn" ]; then
  echo "✖ 虚拟环境缺少 uvicorn，请删除 .venv 后重新运行，或手动安装 requirements.txt。" >&2
  exit 1
fi

echo "▶ 启动服务：http://$HOST:$PORT   （管理后台 /admin_d）"
if [ -n "${ADMIN_PASSWORD:-}" ]; then
  echo "  管理密码：已通过 ADMIN_PASSWORD 配置（不会回显）"
else
  echo "  管理密码：仍为默认值，生产环境请设置 ADMIN_PASSWORD"
fi
exec "$VENV/bin/uvicorn" server:app --host "$HOST" --port "$PORT" --no-access-log
