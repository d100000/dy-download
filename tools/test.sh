#!/usr/bin/env bash
# 全套离线回归：Python 临时数据库 + Node 浏览器逻辑沙箱。
set -euo pipefail
cd "$(dirname "$0")/.."
TEST_PYTHON="${PYTHON:-.venv/bin/python}"
"$TEST_PYTHON" -m pip check
if [ "${COVERAGE:-0}" = 1 ]; then
  "$TEST_PYTHON" -m coverage run --source=server,douyin_dl -m unittest discover -s tests -t . -p 'test_*.py'
  "$TEST_PYTHON" -m coverage report
else
  "$TEST_PYTHON" -m unittest discover -s tests -t . -p 'test_*.py'
fi
node --test tests/*.js
