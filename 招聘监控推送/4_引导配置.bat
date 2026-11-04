@echo off
chcp 936 >nul
cd /d "%~dp0"
echo ==========================================
echo   招聘信息监控推送 - 引导配置
echo ==========================================
echo.
echo 接下来会问几个问题，帮你把 config.py 填好。
echo 不确定的直接回车跳过，之后可以再运行本文件补。
echo.
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" setup_wizard.py
) else (
    python setup_wizard.py
)
echo.
pause
