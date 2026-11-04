# ============================================================
# 岗位分级筛选
#
# 依据 config.TARGET_COMPANIES / FIELD_KEYWORDS / GRADUATE_EDU_KEYWORDS 分级：
#   target    → 命中重点企业（最优先推送）
#   field     → 命中方向关键词（专业对口）
#   education → 学历要求是硕博
#   None      → 无关（按 FILTER_MODE 决定丢不丢）
# ============================================================
import logging

import config
from config import FILTER_MODE, GRADUATE_EDU_KEYWORDS

logger = logging.getLogger("job_monitor.filter")

LEVEL_TARGET = "target"
LEVEL_FIELD = "field"
LEVEL_EDU = "education"
LEVEL_NONE = None

# 兼容旧名字，外部若还引用 LEVEL_VEHICLE 不会直接炸
LEVEL_VEHICLE = LEVEL_FIELD


def _match_keywords(text, keywords):
    """子串匹配（不区分大小写），返回命中的第一个关键词或 None"""
    if not text:
        return None
    lowered = text.lower()
    for kw in keywords or []:
        if kw and kw.lower() in lowered:
            return kw
    return None


def classify_job(job):
    """判断单条岗位的匹配级别。

    匹配文本 = 岗位标题 + 公司名；学历单独判断。
    优先级：target > field > education。
    """
    title = job.get("title", "")
    company = job.get("company", "")
    edu = job.get("education", "")
    text = "%s %s" % (title, company)

    if _match_keywords(text, config.TARGET_COMPANIES):
        return LEVEL_TARGET
    if _match_keywords(text, config.FIELD_KEYWORDS):
        return LEVEL_FIELD
    if _match_keywords(edu, GRADUATE_EDU_KEYWORDS):
        return LEVEL_EDU
    return LEVEL_NONE


def apply_filter(items, mode=None):
    """对岗位列表应用筛选，返回 (保留, 丢弃)。

    保留的每条会带上 match_level 字段，推送时据此加标记。
    丢弃的不进入去重与推送。

    模式（config.FILTER_MODE）：
      target_only : 只留重点企业
      field       : 重点企业 + 方向相关
      edu         : 重点企业 + 方向相关 + 学历匹配
      all         : 全留
    """
    mode = mode or FILTER_MODE
    kept, dropped = [], []
    for job in items:
        level = classify_job(job)
        job["match_level"] = level
        if mode == "all":
            kept.append(job)
        elif mode == "target_only":
            (kept if level == LEVEL_TARGET else dropped).append(job)
        elif mode == "field":
            (kept if level in (LEVEL_TARGET, LEVEL_FIELD) else dropped).append(job)
        else:  # edu，默认
            (kept if level in (LEVEL_TARGET, LEVEL_FIELD, LEVEL_EDU) else dropped).append(job)

    # 排序：重点企业 → 方向相关 → 学历匹配
    order = {LEVEL_TARGET: 0, LEVEL_FIELD: 1, LEVEL_EDU: 2}
    kept.sort(key=lambda j: order.get(j.get("match_level"), 3))
    return kept, dropped
