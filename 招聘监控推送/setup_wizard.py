# -*- coding: utf-8 -*-
"""
引导配置向导 —— 第一次用这个文件夹时跑它，逐项问、自动写回 config.py。

为什么需要它：config.py 里的站点参数（学校域名、学校代码、栏目 ID）和
企业微信 Webhook 因脱敏全部留空了，光看文件不知道该填什么。
这个向导会边问边解释去哪找，填完自动校验、自动备份。

三种用法：
    python setup_wizard.py             # 交互式，一步步问
    python setup_wizard.py --check     # 只看当前配置状态，不改
    python setup_wizard.py --reset     # 用默认值重来（会备份原 config.py）

也可以让 WorkBuddy 帮你跑：把本文件丢给它，说"帮我配置这个招聘监控"，
它会按 GUIDE_PROMPT.md 里的流程问你、改配置、跑自检。
"""
import io
import os
import re
import shutil
import sys
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.py")


# ---------------------------------------------------------------- 读写 config

def read_config():
    """读成文本，便于就地替换（保留注释和结构）"""
    with io.open(CONFIG_PATH, encoding="utf-8") as f:
        return f.read()


def get_value(text, name):
    """取一个顶层字符串常量的值"""
    m = re.search(r'^%s\s*=\s*["\'](.*?)["\']' % re.escape(name), text, re.M)
    return m.group(1) if m else None


def set_value(text, name, value):
    """替换一个顶层字符串常量的值，找不到就跳过"""
    pat = re.compile(r'^(%s\s*=\s*)["\'].*?["\']' % re.escape(name), re.M)
    if pat.search(text):
        return pat.sub(lambda m: '%s"%s"' % (m.group(1), value), text, count=1)
    # 退一步：匹配可能是空字符串的赋值
    pat2 = re.compile(r'^(%s\s*=\s*)$' % re.escape(name), re.M)
    return pat2.sub(lambda m: '%s"%s"' % (m.group(1), value), text, count=1)


def get_categories(text):
    """取 CATEGORIES 里每个栏目的 (enabled, lmid)"""
    out = {}
    block = re.search(r'^CATEGORIES\s*=\s*\{(.*?)^\}', text, re.M | re.S)
    if not block:
        return out
    body = block.group(1)
    for m in re.finditer(r'"(招聘岗位|招聘会|宣讲会|通知公告)"\s*:\s*\{(.*?)\}', body, re.S):
        name, inner = m.group(1), m.group(2)
        en = re.search(r'"enabled"\s*:\s*(True|False)', inner)
        lm = re.search(r'"lmid"\s*:\s*"([^"]*)"', inner)
        out[name] = (en.group(1) == "True" if en else False, lm.group(1) if lm else "")
    return out


def set_category_lmid(text, name, lmid):
    """只改某个栏目的 lmid"""
    pat = re.compile(
        r'("%s"\s*:\s*\{[^}]*?"lmid"\s*:\s*)["\'][^"\']*["\']' % re.escape(name), re.S)
    return pat.sub(lambda m: '%s"%s"' % (m.group(1), lmid), text, count=1)


def backup_config():
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = os.path.join(BASE_DIR, "config.py.bak_%s" % stamp)
    shutil.copy2(CONFIG_PATH, dst)
    return dst


# ---------------------------------------------------------------- 交互

def ask(prompt, default="", required=False, hint=""):
    """问一个问题。回车取默认值；required 时空值会重问。"""
    if hint:
        for line in hint.strip().splitlines():
            print("    " + line)
    suffix = " [%s]" % default if default else ""
    while True:
        try:
            val = input("%s%s: " % (prompt, suffix)).strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已取消。")
            sys.exit(1)
        if not val:
            val = default
        if val or not required:
            return val
        print("    ↑ 这项必须填，不能留空。")


def section(title):
    print("\n" + "-" * 56)
    print("  " + title)
    print("-" * 56)


def show_status():
    """打印当前配置状态"""
    text = read_config()
    print("\n当前配置：")
    for key, label in [("SITE_HOST", "站点域名"), ("SITE_CODE", "学校代码"),
                       ("SITE_WZID", "站点 ID"), ("WECOM_WEBHOOK_URL", "Webhook")]:
        v = get_value(text, key) or ""
        if key == "WECOM_WEBHOOK_URL" and v:
            v = v.split("key=")[0] + "key=****"
        print("  %-10s %s" % (label, v if v else "（未填）"))
    cats = get_categories(text)
    print("  栏目：")
    for name, (en, lmid) in cats.items():
        state = "开启" if en else "关闭"
        print("    %-6s %s  lmid=%s" % (name, state, lmid if lmid else "（未填）"))


def parse_host(raw):
    """从用户粘的任意网址里抠出纯域名。

    用户多半会直接整条网址贴进来（含 https:// 和后面的路径），
    这里统一拆干净，免得存进去之后拼出的接口地址是错的。
    """
    v = raw.strip()
    if not v:
        return "", ""
    v = re.sub(r'^[a-zA-Z]+://', '', v)      # 去协议
    v = v.split('/')[0].split('?')[0]        # 去路径和查询串
    v = v.split('@')[-1]                     # 去可能的 user@
    v = v.rstrip('/')
    # 顺手从路径里捞学校代码，省得再问一次
    code = ""
    m = re.search(r'/home/(\d+)', raw)
    if m:
        code = m.group(1)
    return v, code


def main():
    if "--check" in sys.argv:
        show_status()
        print("\n想改就运行：python setup_wizard.py")
        return 0

    text = read_config()
    has_existing = bool(get_value(text, "SITE_HOST") or get_value(text, "WECOM_WEBHOOK_URL"))

    print("=" * 56)
    print("  招聘信息监控推送 · 配置向导")
    print("=" * 56)
    print("""
这个文件夹从别人那里拿来时，站点和企业微信的信息是空的
（原作者的信息已经抹掉，避免把私人配置带出去）。
下面几步会把它们填回来，大约 3 分钟。

不确定的地方直接回车跳过，之后可以再跑一次本向导。
""")

    if has_existing:
        show_status()
        if ask("看起来已经配置过了，要重新配置吗？(y/N)", "N").lower() != "y":
            print("\n没改动。要检查配置是否可用，运行：python selftest.py")
            return 0

    # ---- 第 1 步：站点
    section("第 1 步 / 共 3 步：你的学校就业网")
    print("""
    91job 是各高校共用的就业平台，每所学校有自己的域名和编号。
    打开你学校的就业信息网，看地址栏，形如：
        https://xxxxx.91job.org.cn/sub-station/home/12345
                        ↑域名                      ↑学校代码
""")
    raw = ask("学校就业网地址（整条粘过来就行）",
              get_value(text, "SITE_HOST") or "", required=True,
              hint="例：https://xxxxx.91job.org.cn/sub-station/home/12345")
    host, guessed_code = parse_host(raw)
    if host != raw:
        print("    已自动提取域名：%s" % host)

    code = ask("学校代码", guessed_code or get_value(text, "SITE_CODE") or "",
               required=True,
               hint="网址最后那串数字，例：12345" + ("（已从地址里取到）" if guessed_code else ""))

    wzid = ask("站点 ID（不确定就回车跳过）", get_value(text, "SITE_WZID") or "",
               hint="F12 → Network → 任一 getLbsj 请求 → 看 wzid 参数。留空一般也能用")

    text = set_value(text, "SITE_HOST", host)
    text = set_value(text, "SITE_CODE", code)
    text = set_value(text, "SITE_WZID", wzid)

    # ---- 第 2 步：栏目 ID
    section("第 2 步 / 共 3 步：各栏目的 lmid")
    print("""
    每个栏目（岗位/招聘会/宣讲会/公告）都有自己的 lmid，各校不同。
    取法：
      1. 浏览器打开学校就业网的「招聘岗位」栏目
      2. 按 F12 打开开发者工具，切到 Network（网络）
      3. 刷新页面，在列表里找名字含 getLbsj 的请求
      4. 点开它，看 Query String 里的 lmid，复制那串值
    招聘会、宣讲会、公告同理，各自打开对应栏目再抓一次。
    不知道的栏目直接回车跳过，之后可以在 config.py 里补。
""")
    names = ["招聘岗位", "招聘会", "宣讲会", "通知公告"]
    cats = get_categories(text)
    for name in names:
        cur = cats.get(name, (True, ""))[1]
        val = ask("  %s 的 lmid" % name, cur, hint="")
        if val:
            text = set_category_lmid(text, name, val)

    # ---- 第 3 步：企业微信
    section("第 3 步 / 共 3 步：企业微信推送（可选）")
    print("""
    不配也能用 —— 那样只生成日报、把内容打印在屏幕上，不发消息。
    想推到手机上的企业微信群，这样拿地址：
      1. 企业微信里建一个群（可以只有自己）
      2. 群右上角「…」→ 群机器人 → 添加机器人 → 新建
      3. 复制它给的 Webhook 地址，形如：
         https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxxxxxx
""")
    hook = ask("Webhook 地址（回车跳过）", get_value(text, "WECOM_WEBHOOK_URL") or "",
               hint="以 https://qyapi.weixin.qq.com/ 开头")
    if hook and "key=" not in hook:
        print("    ↑ 这不像机器人地址（缺 key= 参数），仍会写进去，但多半发不出去")
    text = set_value(text, "WECOM_WEBHOOK_URL", hook)

    # ---- 写回
    bak = backup_config()
    with io.open(CONFIG_PATH, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    print("\n已写入 config.py（原文件备份为 %s）" % os.path.basename(bak))

    show_status()

    # ---- 自检
    section("下一步：验证")
    print("""
    建议按顺序跑：

      1. python selftest.py              检查配置 + 离线跑一遍筛选
      2. python selftest.py --online     真连站点，看每个栏目抓到几条
      3. python selftest.py --push       给企业微信发一条测试消息

    三个都通过就说明配好了。之后每次巡检跑：
      python run_automation.py
""")
    if ask("现在就跑一次离线自检吗？(Y/n)", "Y").lower() != "n":
        print("\n" + "=" * 56)
        os.system('%s "%s"'
                  % (sys.executable, os.path.join(BASE_DIR, "selftest.py")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
