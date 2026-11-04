@echo off
chcp 936 >nul
cd /d "%~dp0"
echo ==========================================
echo   招聘信息监控推送 - 执行一次巡检
echo ==========================================
echo.
echo 抓取 - 筛选 - 推送 - 生成日报。跑完就退出。
echo.
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" run_automation.py
) else (
    python run_automation.py
)
echo.
echo 日报在 reports 目录，日志在 job_monitor.log。
pause
