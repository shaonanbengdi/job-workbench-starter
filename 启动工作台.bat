@echo off
chcp 65001 >nul
title 求职工作台 - 本地服务
cd /d "%~dp0"

set "PY="
py -3 --version >nul 2>nul
if not errorlevel 1 set "PY=py -3"
if not defined PY (
  python --version >nul 2>nul
  if not errorlevel 1 set "PY=python"
)

if not defined PY (
  echo.
  echo   没有找到 Python 3。
  echo   请到 https://www.python.org/downloads/ 安装，安装时勾选 "Add Python to PATH"。
  echo.
  pause
  exit /b 1
)

echo 使用 %PY% 启动工作台...
echo 启动后浏览器会自动打开 http://127.0.0.1:8765
echo 关闭本窗口即停止服务。
echo.
%PY% server.py

echo.
echo 服务已停止，按任意键关闭窗口。
pause >nul
