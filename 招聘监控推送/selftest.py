# -*- coding: utf-8 -*-
"""
配置自检 —— 改完 config.py 先跑这个，别等到推送失败才排查。

用法：
    python selftest.py             # 检查配置 + 离线跑一遍筛选流程
    python selftest.py --online    # 额外真连一次站点，看每个栏目抓到多少条
    python selftest.py --push      # 额外给企业微信发一条测试消息

不需要联网的部分（配置检查、筛选、消息拼装）会先跑，出问题当场能看出来。
"""
import sys

PASS, FAIL, WARN = "  [OK]  ", "  [!!]  ", "  [--]  "


def check_config():
    """逐项检查 config 是否填全，返回问题数"""
    import config

    print("\n=== 一、配置检查 ===")
    problems = 0

    if config.SITE_HOST:
        print(PASS + "SITE_HOST = %s" % config.SITE_HOST)
        if config.SITE_HOST.startswith(("http://", "https://")):
            print(WARN + "SITE_HOST 不要带 http:// 或 https://，只填域名")
            problems += 1
        if config.SITE_HOST.endswith("/"):
            print(WARN + "SITE_HOST 结尾不要带斜杠")
            problems += 1
    else:
        print(FAIL + "SITE_HOST 未填 —— 抓不了，看 README「第 1 步」")
        problems += 1

    if config.SITE_CODE:
        print(PASS + "SITE_CODE = %s" % config.SITE_CODE)
    else:
        print(FAIL + "SITE_CODE 未填")
        problems += 1

    filled = 0
    for name, cfg in config.CATEGORIES.items():
        if not cfg.get("enabled"):
            print(WARN + "栏目 [%s] 已关闭" % name)
            continue
        if cfg.get("lmid"):
            print(PASS + "栏目 [%s] lmid = %s" % (name, cfg["lmid"]))
            filled += 1
        else:
            print(FAIL + "栏目 [%s] 没填 lmid —— 这个栏目抓不到" % name)
            problems += 1
    if filled == 0:
        print(FAIL + "至少要有 1 个栏目填了 lmid")
        problems += 1

    if config.WECOM_WEBHOOK_URL:
        masked = config.WECOM_WEBHOOK_URL
        if "key=" in masked:
            masked = masked.split("key=")[0] + "key=****"
        print(PASS + "Webhook 已配置（%s）" % masked)
    else:
        print(WARN + "没配 WECOM_WEBHOOK_URL —— 能抓能筛，但只打印不推送")

    if config.TARGET_COMPANIES:
        print(PASS + "重点企业 %d 个" % len(config.TARGET_COMPANIES))
    else:
        print(WARN + "TARGET_COMPANIES 是空的 —— 不会出现🔴标记")
    if config.FIELD_KEYWORDS:
        print(PASS + "方向关键词 %d 个" % len(config.FIELD_KEYWORDS))
    else:
        print(WARN + "FIELD_KEYWORDS 是空的 —— 不会出现🟠标记")
    print(PASS + "筛选模式 = %s" % config.FILTER_MODE)

    return problems


def check_filter():
    """用内置假数据跑一遍筛选 + 消息拼装，不需要联网"""
    print("\n=== 二、筛选与消息拼装（离线）===")
    import config
    from filter import apply_filter
    from pusher import format_job

    # 三条假岗位，分别应对三个分级
    samples = [
        {"title": "整车气动 CFD 仿真工程师", "company": config.TARGET_COMPANIES[0]
         if config.TARGET_COMPANIES else "某主机厂", "education": "硕士",
         "location": "上海", "url": "https://example.com/1", "item_id": "t1"},
        {"title": "热管理仿真工程师", "company": "某零部件公司", "education": "本科",
         "location": "常州", "url": "https://example.com/2", "item_id": "t2"},
        {"title": "行政专员", "company": "某贸易公司", "education": "本科",
         "location": "南京", "url": "https://example.com/3", "item_id": "t3"},
    ]
    kept, dropped = apply_filter(samples, mode=config.FILTER_MODE)
    print(PASS + "输入 %d 条 → 保留 %d，丢弃 %d（模式 %s）"
          % (len(samples), len(kept), len(dropped), config.FILTER_MODE))
    for j in kept:
        print("      · [%s] %s · %s" % (j.get("match_level"), j["title"], j["company"]))
    if not kept:
        print(WARN + "一条都没留下 —— 检查 FILTER_MODE 与两份名单是否太严")

    if kept:
        print("\n  — 推送消息长这样（前一条）—")
        for line in format_job(kept[0]).splitlines():
            print("  | " + line)
    return 0


def check_online():
    """真连站点，看每个栏目抓到多少条"""
    print("\n=== 三、连接站点（联网）===")
    import config
    from scraper import JobScraper

    try:
        scraper = JobScraper()
    except ImportError:
        print(FAIL + "没装 requests，执行：pip install -r requirements.txt")
        return 1

    total = 0
    for name, cfg in config.CATEGORIES.items():
        if not cfg.get("enabled") or not cfg.get("lmid"):
            continue
        items = scraper.fetch(cfg["lmid"], cfg.get("rows", 20))
        total += len(items)
        if items:
            print(PASS + "[%s] 返回 %d 条" % (name, len(items)))
            if name == "招聘岗位" and items:
                first = items[0]
                print("      样例：%s / %s"
                      % (first.get("dwmc", "?"),
                         (first.get("zpzw") or [{}])[0].get("zwmc", "(无岗位名)")))
        else:
            print(FAIL + "[%s] 0 条 —— lmid 可能不对，或者该栏目当前没有内容" % name)

    if total == 0:
        print("\n  一条都没抓到，按这个顺序查：")
        print("  1. SITE_HOST / SITE_CODE 是不是自己学校的")
        print("  2. lmid 是不是从自己的站点抓的（不同学校不一样）")
        print("  3. 浏览器能打开就业网吗？公司网络/WiFi 是否限制")
        return 1
    return 0


def check_push():
    """发一条测试消息到企业微信"""
    print("\n=== 四、测试推送（联网）===")
    import config
    from pusher import WeComPusher

    if not config.WECOM_WEBHOOK_URL:
        print(FAIL + "没配 WECOM_WEBHOOK_URL，跳过")
        return 1

    msg = ("**招聘监控 · 测试消息**\n"
           "> 收到这条说明 Webhook 配置正确，后续巡检可以正常推送。\n")
    ok = WeComPusher().send_markdown(msg)
    if ok:
        print(PASS + "发送成功，去群里看有没有消息")
    else:
        print(FAIL + "发送失败 —— Webhook 地址错、机器人被移出群，或网络不通")
    return 0 if ok else 1


def main():
    args = sys.argv[1:]
    print("=" * 56)
    print("  招聘监控推送 · 配置自检")
    print("=" * 56)

    problems = check_config()
    check_filter()

    failures = 0
    if "--online" in args:
        failures += check_online()
    else:
        print("\n（想看能不能连上站点，加 --online）")

    if "--push" in args:
        failures += check_push()
    else:
        print("（想测企业微信能不能收到，加 --push）")

    print("\n" + "=" * 56)
    if problems:
        print("配置还有 %d 处没填好，先补完再跑。" % problems)
    elif failures:
        print("配置齐全，但联网环节有失败项，看上面 [!!] 的提示。")
    else:
        print("一切正常，可以开始用了。")
    print("=" * 56)
    return 1 if (problems or failures) else 0


if __name__ == "__main__":
    sys.exit(main())
