#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键打出 Windows 便携版：PyInstaller 打包 → 附使用说明 → 压成 zip。

用法（仓库根目录）：
    python packaging/build.py              # 完整流程
    python packaging/build.py --no-zip     # 只打包，不压缩

环境要求：
    pip install pyinstaller
    国内网络直连 PyPI 常失败，可加镜像：
    python -m pip install pyinstaller -i https://mirrors.aliyun.com/pypi/simple

产物：
    dist/求职工作台/              解压即用的程序目录
    dist/求职工作台-便携版.zip     发给别人的文件
"""

import datetime
import os
import shutil
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC = os.path.join(ROOT, 'packaging', '工作台.spec')
DIST = os.path.join(ROOT, 'dist')
APP_DIR_NAME = '求职工作台'
APP_DIR = os.path.join(DIST, APP_DIR_NAME)

README_TXT = """求职工作台 · 便携版
================================================

双击「求职工作台.exe」即可启动，浏览器会自动打开操作界面。
程序不需要安装 Python，也不需要联网（识别岗位功能除外，见下）。

【数据在哪】
  数据存在本文件同级的 data.db 里，backup/ 是每天自动备份。
  整个文件夹可以拷到 U 盘或另一台电脑，数据跟着一起走。
  首次启动会用内置示例数据建库，之后不再覆盖。

【怎么清空示例数据】
  仪表盘右下角「数据管理」→「清空示例」。
  只删示例条目，你自己录入的不受影响。

【怎么关闭】
  关掉那个黑色命令行窗口，服务就停了。
  页面还开着没关系，只是连不上了，重新双击 exe 就行。

【岗位抓取插件（在 浏览器插件 文件夹里）】
  工作台自己不能去招聘网站抓岗位，要靠这个浏览器扩展。
  它就在本文件夹下，装一次以后一直有效，不影响工作台开关。

  安装：
  1. 浏览器打开 chrome://extensions/  （Edge 是 edge://extensions/）
  2. 打开右上角「开发者模式」
  3. 点「加载已解压的扩展程序」
  4. 选择本文件夹下的 浏览器插件 文件夹
     （要选里面有 manifest.json 的那一层，不是它的上级）

  用法：
  · 招聘页右侧会出现「存岗位」「筛岗位」两个按钮，可拖到任意位置
  · 快捷键 Alt+Shift+J 存岗位、Alt+Shift+F 筛岗位
  · 也可以右键菜单操作

  注意：工作台没启动时点按钮会提示连不上，先把 exe 打开再抓。

  如果本便携版放在 U 盘上：从 U 盘加载扩展后，一旦拔掉 U 盘，
  浏览器会报扩展失效，插回来重启浏览器即可。想避免这个，就把
  浏览器插件 文件夹拷到本地硬盘某个固定位置，再从那里加载。

【附带的招聘监控推送（可选）】
  这不是工作台的一部分，是另一个独立小工具：定时抓学校就业网的
  招聘信息，按你的方向筛选后推送到企业微信群。

  目录就在本文件夹下：招聘监控推送
  它是 Python 脚本，需要电脑装 Python 3.9 以上（本便携包的 Python
  是打包在 exe 里的，脚本用不了，得单独装一个）。

  第一次用：把 招聘监控推送 文件夹拖进 WorkBuddy，说一句
  「帮我配置这个招聘监控」，它会带你填配置。也可以双击里面的
  4_引导配置.bat 自己在命令行里配。

  不需要这个功能就删掉那个文件夹，不影响工作台。

【识别功能需要配置】
  插件抓岗位时靠大模型读页面内容，要先填自己的 API Key：
  插件面板 → 识别设置。不填也能用工作台的其他所有功能。

【端口被占用怎么办】
  换一个：在命令行窗口里这样启动
      求职工作台.exe --port 8766
  插件会自动找 8765~8784 之间的端口。

【常见问题】
  · 打不开杀毒软件报警：这是 PyInstaller 打包的常见误报，
    可以添加信任，或者改用源码版（python server.py）。
  · 双击一闪而过：多半是端口全被占用，用 --port 换个端口试试。
  · 数据好像没保存：确认你把整个文件夹一起搬走了，而不是只拷了 exe。

================================================
"""


def sh(cmd):
    print('+ ' + ' '.join(cmd))
    r = subprocess.run(cmd, cwd=ROOT)
    if r.returncode != 0:
        sys.exit('命令失败，退出码 %d' % r.returncode)


def main():
    no_zip = '--no-zip' in sys.argv

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        sys.exit('没装 PyInstaller。先执行：\n'
                 '  python -m pip install pyinstaller\n'
                 '连不上就换镜像：\n'
                 '  python -m pip install pyinstaller -i https://mirrors.aliyun.com/pypi/simple')

    # 源码可能在 demo/ 子目录（本仓库），也可能就在根目录（公开仓库）
    src_dir = os.path.join(ROOT, 'demo')
    if not os.path.isdir(src_dir):
        src_dir = ROOT
    src_db = os.path.join(src_dir, 'data.db')
    if not os.path.exists(src_db):
        sys.exit('缺少 data.db 示例库。先运行 python %s/tools/make_demo_db.py 生成。'
                 % os.path.relpath(src_dir, ROOT).replace('\\', '/'))

    # 清干净，避免上一次的残留混进来
    for p in (DIST, os.path.join(ROOT, 'build')):
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)

    sh([sys.executable, '-m', 'PyInstaller', SPEC,
        '--noconfirm', '--distpath', DIST, '--workpath', os.path.join(ROOT, 'build')])

    if not os.path.isdir(APP_DIR):
        sys.exit('打包没产出预期目录：%s' % APP_DIR)

    # 浏览器插件是 MV3 扩展，浏览器只认文件夹、不能塞进 exe，
    # 现代 Chrome 也不允许双击装 crx。所以原样放到 exe 旁边，
    # 用户「加载已解压的扩展程序」时直接指向这个目录。
    ext_src = os.path.join(src_dir, '浏览器插件')
    if os.path.isdir(ext_src):
        ext_dst = os.path.join(APP_DIR, '浏览器插件')
        shutil.copytree(ext_src, ext_dst,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        n = sum(len(fs) for _, _, fs in os.walk(ext_dst))
        print('已附带浏览器插件：%d 个文件' % n)
    else:
        print('[warn] 没找到 浏览器插件/ 目录，包里将不含插件')

    # 招聘监控推送是独立的 Python 小工具（需要 requests），不打进 exe，
    # 原样放旁边。它是可选的，不影响工作台本身运行。
    mon_src = os.path.join(src_dir, '招聘监控推送')
    if os.path.isdir(mon_src):
        mon_dst = os.path.join(APP_DIR, '招聘监控推送')
        shutil.copytree(mon_src, mon_dst,
                        ignore=shutil.ignore_patterns(
                            '__pycache__', '*.pyc', '.venv', 'job_monitor.db',
                            'job_monitor.log', 'reports', 'state', 'config.py.bak_*'))
        n = sum(len(fs) for _, _, fs in os.walk(mon_dst))
        print('已附带招聘监控推送：%d 个文件' % n)
    else:
        print('[warn] 没找到 招聘监控推送/ 目录')

    # 使用说明放在程序目录里，也放一份在外面方便直接看到
    for target in (os.path.join(APP_DIR, '使用说明.txt'),
                   os.path.join(DIST, '使用说明.txt')):
        with open(target, 'w', encoding='utf-8') as f:
            f.write(README_TXT)

    size = sum(os.path.getsize(os.path.join(dp, f))
               for dp, _, fs in os.walk(APP_DIR) for f in fs)
    print('\n程序目录 : %s' % APP_DIR)
    print('体积     : %.1f MB' % (size / 1024 / 1024))

    if no_zip:
        return

    stamp = datetime.date.today().strftime('%Y%m%d')
    zip_path = os.path.join(DIST, '求职工作台-便携版-%s.zip' % stamp)
    print('正在压缩…')
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for dp, _, files in os.walk(APP_DIR):
            for name in files:
                full = os.path.join(dp, name)
                # zip 内保留「求职工作台/」这一层，解压出来就是一个文件夹
                arc = os.path.join(APP_DIR_NAME, os.path.relpath(full, APP_DIR))
                z.write(full, arc)
        # 使用说明已经躺在 APP_DIR 里，上面那段 walk 会带上，别再写一遍

    print('压缩包   : %s' % zip_path)
    print('体积     : %.1f MB' % (os.path.getsize(zip_path) / 1024 / 1024))
    print('\n发给别人时只发这个 zip 就行。')


if __name__ == '__main__':
    main()
