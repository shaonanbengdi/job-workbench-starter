@echo off
chcp 936 >nul
cd /d "%~dp0"
echo ==========================================
echo   招聘信息监控推送 - 配置自检
echo ==========================================
echo.
echo 先做配置检查和离线流程测试。
echo 想连站点实测就选 y（需要先填好 config.py）。
echo.
set /p ONLINE=是否连站点实测？(y/N):
if /i "%ONLINE%"=="y" (
    if exist ".venv\Scripts\python.exe" (
        ".venv\Scripts\python.exe" selftest.py --online
    ) else (
        python selftest.py --online
    )
) else (
    if exist ".venv\Scripts\python.exe" (
        ".venv\Scripts\python.exe" selftest.py
    ) else (
        python selftest.py
    )
)
echo.
pause
