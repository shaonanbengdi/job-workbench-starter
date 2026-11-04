@echo off
chcp 936 >nul
cd /d "%~dp0"
echo ==========================================
echo   招聘信息监控推送 - 安装依赖
echo ==========================================
echo.
echo 需要 Python 3.9 以上，且 python 命令可用。
echo.
python -m venv .venv
if errorlevel 1 (
    echo [错误] 创建虚拟环境失败。确认装过 Python 并勾选了 "Add to PATH"。
    pause
    exit /b 1
)
echo 正在安装 requests ...
".venv\Scripts\python.exe" -m pip install --upgrade pip -q
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [错误] 依赖安装失败，多半是网络问题。可手动换镜像：
    echo   .venv\Scripts\python.exe -m pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple
    pause
    exit /b 1
)
echo.
echo 依赖装好了。下一步：用记事本打开 config.py 填写站点与 Webhook，
echo 然后双击 2_配置自检.bat 检查。
echo.
pause
