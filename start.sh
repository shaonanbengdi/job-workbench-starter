#!/usr/bin/env bash
# 求职工作台 · macOS / Linux 启动脚本
set -e
cd "$(dirname "$0")"

if command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v python >/dev/null 2>&1; then
  PY=python
else
  echo "没有找到 Python 3，请先安装：https://www.python.org/downloads/"
  exit 1
fi

echo "使用 $PY 启动工作台，浏览器将自动打开 http://127.0.0.1:8765"
exec "$PY" server.py
