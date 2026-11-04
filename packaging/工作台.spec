# -*- mode: python ; coding: utf-8 -*-
"""便携版打包配置（PyInstaller onedir）。

产物结构：
    dist/求职工作台/
        求职工作台.exe        <- 双击启动，数据写在它旁边
        data.db               <- 首次运行从 _internal 里复制出来
        backup/
        _internal/            <- Python 运行时 + index.html + 内置示例库

为什么 onedir 而不是 onefile：onefile 每次启动都要把运行时解压到 %TEMP%，
两秒起步，且自解压行为更容易被杀软拦。

用法（在仓库根目录）：
    python -m PyInstaller packaging/工作台.spec --noconfirm
"""

import os

# spec 里拿不到 __file__，用 PyInstaller 注入的 SPECPATH 定位仓库根
ROOT = os.path.dirname(SPECPATH.rstrip('\\/'))
# 本仓库：源在 demo/ 子目录。公开仓库：源直接摊在根目录，没有 demo/。
SRC = os.path.join(ROOT, 'demo')
if not os.path.isdir(SRC):
    SRC = ROOT
ICON = os.path.join(ROOT, 'packaging', 'assets', 'app.ico')

a = Analysis(
    [os.path.join(SRC, 'server.py')],
    pathex=[SRC],
    binaries=[],
    datas=[
        # 前端单文件：由 SimpleHTTPRequestHandler 按 RES_DIR 提供
        (os.path.join(SRC, 'index.html'), '.'),
        # 内置示例库：首次运行时复制到 exe 同级，之后不再覆盖
        (os.path.join(SRC, 'data.db'), '.'),
    ],
    # server.py 里是函数内 import，静态分析扫不到
    hiddenimports=['ai_extract'],
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        'tkinter', 'unittest', 'pydoc', 'doctest', 'setuptools', 'pip',
        'wheel', 'pkg_resources', 'test', 'lib2to3', 'xmlrpc',
    ],
    noarchive=False,
    optimize=1,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='求职工作台',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX 压缩会显著提高杀软误报率，宁可不压
    console=True,       # 保留控制台：能看到访问地址，出问题也有线索
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON if os.path.exists(ICON) else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='求职工作台',
)
