# ============================================================
# 主调度模块 — 定时采集 + 去重 + 推送
# 调度策略：每天从 9:00 开始，每隔 8 小时检查一次
# ============================================================
import logging
import os
import time
from datetime import datetime, timedelta

import config
from config import (
    CATEGORIES, KEYWORDS, SCHEDULE_START_HOUR, SCHEDULE_INTERVAL_HOURS,
    FILTER_MODE, LOG_PATH,
)
from filter import apply_filter, LEVEL_TARGET, LEVEL_FIELD, LEVEL_EDU
from scraper import JobScraper
from storage import JobStorage
from pusher import (
    WeComPusher,
    format_job,
    format_fair,
    format_talk,
    format_notice,
)

logger = logging.getLogger("job_monitor.main")


def setup_logging(log_file=None):
    """配置日志：同时输出到文件(含时间戳)与控制台。
    以 'job_monitor' 为根 logger，子模块 logger 自动继承。"""
    if log_file is None:
        log_file = LOG_PATH
    log_dir = os.path.dirname(log_file)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
    root = logging.getLogger("job_monitor")
    if root.handlers:
        return root
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    try:
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except Exception as e:
        # 文件日志不可用时退化为仅控制台
        print(f"无法写入日志文件 {log_file}: {e}")
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    root.addHandler(ch)
    root.propagate = False  # 避免把 requests 等第三方日志带进来
    return root


def filter_by_keywords(jobs, keywords):
    if not keywords:
        return jobs
    filtered = []
    for job in jobs:
        text = job["title"] + job["company"]
        if any(kw.lower() in text.lower() for kw in keywords):
            filtered.append(job)
    return filtered


def run_once(scraper, storage, pusher, keywords=None, collect_details=False):
    """执行一次完整的采集→去重→推送流程（核心可复用函数）。

    返回结构化结果 dict，供调度与报告使用：
        {
          "total_new": int,
          "categories": { 栏目名: {fetched, valid, new, pushed, items[]} },
          "stats": {"total": int, "today": int},
        }
    collect_details=True 时，categories[...]["items"] 会包含已推送条目的明细。
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logger.info("开始采集 %s", now)

    raw_data = scraper.fetch_all()
    total_new = 0          # 真正推送出去的条数
    total_new_items = 0    # 新条目数（未配 Webhook 时也算，日报要用）
    categories = {}

    for name, raw_items in raw_data.items():
        cfg = CATEGORIES[name]
        filtered_out = 0
        match_stats = {LEVEL_TARGET: 0, LEVEL_FIELD: 0, LEVEL_EDU: 0}
        if name == "招聘岗位":
            items = scraper.normalize_jobs(raw_items)
            items = filter_by_keywords(items, keywords or KEYWORDS)
            if keywords or KEYWORDS:
                logger.info("[%s] 关键词过滤后：%d 条", name, len(items))
            # 分级筛选（重点企业优先 + 方向相关 + 学历匹配）
            items, dropped = apply_filter(items)
            filtered_out = len(dropped)
            for j in items:
                if j.get("match_level") in match_stats:
                    match_stats[j["match_level"]] += 1
            if FILTER_MODE != "all":
                logger.info(
                    "[%s] 筛选后：%d 条（重点企业 %d | 方向相关 %d | 学历匹配 %d | 丢弃 %d）",
                    name, len(items), match_stats[LEVEL_TARGET],
                    match_stats[LEVEL_FIELD], match_stats[LEVEL_EDU], filtered_out,
                )
            fmt_fn = format_job
        elif name == "招聘会":
            items = scraper.normalize_fairs(raw_items)
            fmt_fn = format_fair
        elif name == "宣讲会":
            items = scraper.normalize_talks(raw_items)
            fmt_fn = format_talk
        elif name in ("通知公告", "信息速递"):
            items = scraper.normalize_notices(raw_items)
            fmt_fn = format_notice
        else:
            continue

        new_items = storage.get_new_items(items, name)
        logger.info("[%s] 有效 %d | 新增 %d", name, len(items), len(new_items))

        cat = {
            "fetched": len(raw_items),
            "valid": len(items),
            "filtered": filtered_out,
            "match": match_stats,
            "new": len(new_items),
            "pushed": 0,
            "handled": 0,
            "items": [],
        }

        if new_items:
            pushed = pusher.push_batch(name, cfg["icon"], new_items, fmt_fn)
            # 没有 Webhook 时不推送，但仍要把这批新条目记为"已处理"，
            # 否则下一轮又算新条目，日报里会反复出现同一批。
            handled = pushed if pusher.enabled else len(new_items)
            for item in new_items[:handled]:
                storage.mark_pushed(item["item_id"], name, item.get("title", "")[:50])
            cat["pushed"] = pushed
            cat["handled"] = handled
            total_new += pushed
            total_new_items += handled
            if pusher.enabled:
                logger.info("[%s] 已推送 %d 条", name, pushed)
            else:
                logger.info("[%s] 未配置 Webhook，%d 条仅记录不推送", name, handled)

            if collect_details:
                for it in new_items[:handled]:
                    cat["items"].append({
                        "title": it.get("title", ""),
                        "company": it.get("company", ""),
                        "location": it.get("location", ""),
                        "education": it.get("education", ""),
                        "date": it.get("date", it.get("deadline", "")),
                        "url": it.get("url", ""),
                        "match_level": it.get("match_level", ""),
                    })

        categories[name] = cat

    stats = storage.get_stats()
    if pusher.enabled:
        logger.info("本轮推送 %d 条 | 历史累计 %d | 今日 %d",
                    total_new, stats["total"], stats["today"])
    else:
        logger.info("本轮新增 %d 条（未配 Webhook，未推送）| 历史累计 %d",
                    total_new_items, stats["total"])
    return {"total_new": total_new, "total_new_items": total_new_items,
            "categories": categories, "stats": stats}


def next_run_time():
    """计算下一次运行时间：从今天 9:00 开始每 8 小时对齐"""
    now = datetime.now()
    start = now.replace(hour=SCHEDULE_START_HOUR, minute=0, second=0, microsecond=0)

    # 找下一个 >= now 的整点时间点
    if now < start:
        return start
    # 已过今天 9:00，计算下一个 8h 节点
    delta = (now - start).total_seconds()
    periods = int(delta // (SCHEDULE_INTERVAL_HOURS * 3600))
    next_time = start + timedelta(hours=(periods + 1) * SCHEDULE_INTERVAL_HOURS)
    return next_time


def main():
    setup_logging()
    if not config.SITE_HOST:
        logger.error("config.SITE_HOST 未配置，无法抓取。先改 config.py，再跑 python selftest.py 检查。")
        return
    scraper = JobScraper()
    storage = JobStorage()
    pusher = WeComPusher()

    keywords = KEYWORDS
    if keywords:
        logger.info("已启用关键词过滤: %s", keywords)

    active = [n for n, c in CATEGORIES.items() if c["enabled"]]
    logger.info("监控栏目: %s", ", ".join(active))
    logger.info("调度策略: 每天 %d:00 起，每 %d 小时一次", SCHEDULE_START_HOUR, SCHEDULE_INTERVAL_HOURS)

    try:
        while True:
            next_time = next_run_time()
            wait_sec = (next_time - datetime.now()).total_seconds()
            if wait_sec > 0:
                logger.info(
                    "下次运行: %s（等待 %.1fh）",
                    next_time.strftime("%Y-%m-%d %H:%M:%S"), wait_sec / 3600,
                )
                time.sleep(wait_sec)

            run_once(scraper, storage, pusher, keywords)
            time.sleep(2)  # 短暂休眠避免连续触发
    except KeyboardInterrupt:
        logger.info("收到中断信号，正在退出...")
    finally:
        storage.close()


if __name__ == "__main__":
    main()
