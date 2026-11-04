# ============================================================
# 单次执行入口（供自动化调度调用，不进入无限循环）
# 职责：调度一次巡检 + 生成报告。
# 核心逻辑（采集/去重/推送/明细收集）统一在 main.run_once。
# ============================================================
import json
import logging
import os
from datetime import datetime

import config
from config import KEYWORDS, FILTER_MODE, REPORT_DIR as DAILY_REPORT_DIR
from filter import LEVEL_TARGET, LEVEL_FIELD, LEVEL_EDU
from main import run_once, setup_logging
from scraper import JobScraper
from storage import JobStorage
from pusher import WeComPusher

logger = logging.getLogger("job_monitor.automation")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 结构化 JSON 结果落盘目录（供调度读取，不绑定具体自动化 ID）
STATE_DIR = os.path.join(BASE_DIR, "state")


def build_markdown(report):
    """根据结构化结果渲染一份人类可读的日报（含可点击链接明细）。"""
    lines = []
    lines.append(f"# 招聘信息推送日报 · {report['run_at']}\n")
    lines.append("> 自动监控：本校就业信息平台 (91job)")
    lines.append(f"> 监控地址：{report['site']}")
    lines.append(f"> 本次执行时间：{report['run_at']}\n")

    total = report["total_pushed"]
    new_items = report.get("total_new_items", total)
    hooked = report.get("webhook_configured", True)
    lines.append("## 一、本轮结论\n")
    if new_items == 0:
        lines.append("✅ **系统正常巡检完成，本轮无新增招聘信息。**\n")
        lines.append("本次抓取到的所有条目均已在历史记录中（去重生效），未重复处理。\n")
    elif not hooked:
        lines.append(f"⚠️ **本轮新增 {new_items} 条，但还没配置企业微信 Webhook，未推送。**\n")
        lines.append("这些条目已记为已处理，补上 Webhook 后只推后续新增的。\n")
        lines.append("配置方法见 README「第 2 步」。\n")
    else:
        lines.append(f"✅ **本轮成功推送 {total} 条新招聘信息。**\n")

    lines.append("## 二、各栏目采集明细\n")
    last_col = "本轮推送" if hooked else "本轮新增"
    lines.append("| 栏目 | 抓取条数 | 有效条数(去过期) | 筛选丢弃 | 新增 | %s |" % last_col)
    lines.append("|------|---------|----------------|---------|------|---------|")
    for name, c in report["categories"].items():
        filtered = c.get("filtered", 0)
        lines.append(f"| {name} | {c['fetched']} | {c['valid']} | {filtered} | {c['new']} | {c['pushed']} |")
    lines.append("")

    lines.append("## 三、累计数据\n")
    verb = "推送" if hooked else "记录"
    lines.append(f"- 历史累计{verb}：**{report['stats']['total']}** 条")
    lines.append(f"- 今日{verb}：**{report['stats']['today']}** 条")
    kw = "未启用（全量推送）" if not report["keywords"] else "、".join(report["keywords"])
    lines.append(f"- 关键词过滤：{kw}")
    lines.append(f"- 岗位筛选模式：**{report['filter_mode']}**")
    if report["filter_mode"] != "all":
        lines.append("  - 🔴 重点企业 = config.TARGET_COMPANIES 里的目标企业（优先推送）")
        lines.append("  - 🟠 方向相关 = 命中 config.FIELD_KEYWORDS 的对口岗位")
        if report["filter_mode"] == "edu":
            lines.append("  - 🟡 学历匹配 = 硕士/博士研究生学历要求的其他行业岗位")
            lines.append("  - 未命中以上三项、且学历仅为本科/专科的岗位将被丢弃。\n")
        else:
            lines.append("  - 未命中以上两项的岗位将被丢弃，不推送。\n")
    else:
        lines.append("  - 筛选已关闭，全量推送所有岗位。\n")

    has_details = any(c["items"] for c in report["categories"].values())
    if has_details:
        lines.append("## 四、本轮新增明细\n")
        for name, c in report["categories"].items():
            if not c["items"]:
                continue
            lines.append(f"### {name}（{len(c['items'])} 条）\n")
            for it in c["items"]:
                level = it.get("match_level", "")
                if level == "target":
                    badge = "🔴"
                elif level == "field":
                    badge = "🟠"
                elif level == "education":
                    badge = "🟡"
                else:
                    badge = ""
                seg = f"- {badge} **{it.get('title', '')}**"
                if it.get("company"):
                    seg += f" · {it['company']}"
                if it.get("location"):
                    seg += f" · {it['location']}"
                if it.get("education"):
                    seg += f" · {it['education']}"
                if it.get("date"):
                    seg += f" · {it['date']}"
                if it.get("url"):
                    seg += f" — [详情]({it['url']})"
                lines.append(seg)
            lines.append("")

    return "\n".join(lines)


def main():
    setup_logging()
    logger.info("=== 单次巡检开始 ===")
    if not config.SITE_HOST:
        logger.error("config.SITE_HOST 未配置，无法抓取。先改 config.py，再跑 python selftest.py 检查。")
        print("配置未完成：请在 config.py 里填 SITE_HOST / SITE_CODE / 各栏目 lmid。")
        return None, None
    os.makedirs(STATE_DIR, exist_ok=True)
    os.makedirs(DAILY_REPORT_DIR, exist_ok=True)

    scraper = JobScraper()
    storage = JobStorage()
    pusher = WeComPusher()

    # 复用 main.run_once：采集 + 去重 + 推送，并收集明细
    result = run_once(scraper, storage, pusher, keywords=KEYWORDS, collect_details=True)
    storage.close()

    report = {
        "run_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "site": config.site_url(),
        "keywords": KEYWORDS,
        "filter_mode": FILTER_MODE,
        "categories": result["categories"],
        "stats": result["stats"],
        "total_pushed": result["total_new"],
        "total_new_items": result.get("total_new_items", result["total_new"]),
        "webhook_configured": pusher.enabled,
    }

    report_json = os.path.join(STATE_DIR, "last_run_report.json")
    with open(report_json, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    md = build_markdown(report)
    md_path = os.path.join(
        DAILY_REPORT_DIR, f"招聘推送日报_{datetime.now().strftime('%Y-%m-%d')}.md"
    )
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md)

    logger.info("报告已生成: %s | 本轮新增 %d 条", md_path, report["total_new_items"])
    if not report["webhook_configured"]:
        print(f"本轮新增: {report['total_new_items']} 条（未配 Webhook，未推送；筛选模式: {FILTER_MODE}）")
    else:
        print(f"本轮推送: {report['total_pushed']} 条 (筛选模式: {FILTER_MODE})")
    print(f"累计已推送: {report['stats']['total']} 条 | 今日: {report['stats']['today']} 条")
    for name, c in report["categories"].items():
        extra = f" 筛选丢弃{c.get('filtered', 0)}" if c.get("filtered") else ""
        print(f"  [{name}] 抓取{c['fetched']} 有效{c['valid']}{extra} 新增{c['new']} 推送{c['pushed']}")
    return report, md_path


if __name__ == "__main__":
    main()
