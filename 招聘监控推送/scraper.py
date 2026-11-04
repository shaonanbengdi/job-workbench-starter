# ============================================================
# 采集模块 — 91job 智慧就业平台 API
#
# 接口各校通用：GET {api_base}/getLbsj?lmid=<栏目ID>&row=<条数>
# 返回 {"success": true, "result": [...]}，不同栏目字段名不同，见各 normalize_* 方法。
# ============================================================
import logging
from datetime import date, datetime, timedelta

import config
from config import CATEGORIES

logger = logging.getLogger("job_monitor.scraper")


class JobScraper:
    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
    }

    def __init__(self):
        # 延迟导入：没装 requests 时不影响其他模块 import 和离线自检
        import requests
        self._requests = requests

    def _headers(self):
        h = dict(self.HEADERS)
        referer = config.site_url()
        if referer:
            h["Referer"] = referer
        return h

    def _parse_date(self, value, fmt="%Y-%m-%d"):
        """解析日期；为空或格式异常返回 None（调用方视为已过期）"""
        if not value:
            return None
        try:
            return datetime.strptime(value, fmt).date()
        except (ValueError, TypeError):
            logger.warning("日期解析失败，按已过期处理: %r", value)
            return None

    def fetch(self, lmid, rows=20):
        """拉取一个栏目的列表数据"""
        base = config.api_base()
        if not base or not lmid:
            logger.warning("SITE_HOST 或 lmid 未配置，跳过抓取")
            return []
        try:
            resp = self._requests.get(
                "%s/getLbsj" % base,
                params={"lmid": lmid, "row": rows},
                headers=self._headers(), timeout=15,
            )
            data = resp.json()
            if data.get("success"):
                return data.get("result", [])
            logger.warning("接口返回错误: lmid=%s msg=%s", lmid, data.get("message", "unknown"))
            return []
        except Exception as e:
            logger.warning("请求失败 lmid=%s: %s", lmid, e)
            return []

    def fetch_all(self):
        """采集所有启用且填了 lmid 的栏目"""
        all_data = {}
        for name, cfg in CATEGORIES.items():
            if not cfg.get("enabled"):
                continue
            if not cfg.get("lmid"):
                logger.warning("栏目 [%s] 没填 lmid，已跳过", name)
                continue
            logger.info("抓取 [%s] (lmid=%s)...", name, cfg["lmid"])
            result = self.fetch(cfg["lmid"], cfg.get("rows", 20))
            all_data[name] = result
            logger.info("  → %d 条", len(result))
        return all_data

    def normalize_jobs(self, raw_items):
        """标准化岗位数据，只保留近 30 天发布的。

        注意 zzshsj 是岗位在平台上的发布/审核日期，不是报名截止日。
        早期写法把它当截止日做 `date < today` 硬过滤，结果把非当日发布的
        岗位（含刚发布的）全判成过期，新岗位永远推不出去。
        这里改成"近 30 天内发布"的窗口：陈旧岗位靠它挡掉，
        重复推送交给 storage 去重。
        """
        today = date.today()
        cutoff = today - timedelta(days=30)
        jobs, expired = [], 0
        for company in raw_items:
            dwmc = company.get("dwmc", "未知公司")
            zzshsj = company.get("zzshsj", "")
            post_date = self._parse_date(zzshsj)
            for pos in company.get("zpzw", []):
                jid = str(pos.get("zpgwid", ""))
                job = {
                    "item_id": jid,
                    "company": dwmc,
                    "title": pos.get("zwmc", "未知岗位"),
                    "education": pos.get("xlyq", ""),
                    "location": pos.get("gzdd", ""),
                    "count": pos.get("zprs", ""),
                    "deadline": zzshsj,
                    "url": config.detail_url("job", jid),
                }
                if post_date is None or post_date < cutoff:
                    expired += 1
                else:
                    jobs.append(job)
        if expired:
            logger.info("已过滤 %d 条过期/无效岗位", expired)
        return jobs

    def normalize_fairs(self, raw_items):
        """标准化招聘会，过滤已过期。日期解析失败视为过期。"""
        today = date.today()
        fairs, expired = [], 0
        for item in raw_items:
            fair_date = self._parse_date(item.get("jbkssj", ""))
            if fair_date is None or fair_date < today:
                expired += 1
                continue
            fid = item.get("zphid", "")
            fairs.append({
                "item_id": fid,
                "title": item.get("zphmc", ""),
                "date": item.get("jbkssj", ""),
                "location": item.get("jbcd", ""),
                "organizer": item.get("gljg", ""),
                "url": config.detail_url("fair", fid),
            })
        if expired:
            logger.info("已过滤 %d 条过期招聘会", expired)
        return fairs

    def normalize_talks(self, raw_items):
        """标准化宣讲会，过滤已过期。日期解析失败视为过期。"""
        today = date.today()
        talks, expired = [], 0
        for item in raw_items:
            talk_date = self._parse_date(item.get("jbrq", ""))
            if talk_date is None or talk_date < today:
                expired += 1
                continue
            tid = item.get("xjhid", "")
            talks.append({
                "item_id": tid,
                "title": item.get("xjhmc", ""),
                "date": item.get("jbrq", ""),
                "location": item.get("jbdd", ""),
                "school": item.get("xjxx", ""),
                "company": item.get("dwmc", ""),
                "url": config.detail_url("talk", tid),
            })
        if expired:
            logger.info("已过滤 %d 条过期宣讲会", expired)
        return talks

    def normalize_notices(self, raw_items):
        """标准化通知公告，只保留最近 30 天。"""
        today = date.today()
        cutoff = today - timedelta(days=30)
        notices, expired = [], 0
        for item in raw_items:
            pub_date = self._parse_date(item.get("fbsj", ""))
            if pub_date is None or pub_date < cutoff:
                expired += 1
                continue
            nid = item.get("xwid", "")
            notices.append({
                "item_id": nid,
                "title": item.get("xwbt", ""),
                "date": item.get("fbsj", ""),
                "url": item.get("tzljdz") or config.detail_url("notice", nid),
            })
        if expired:
            logger.info("已过滤 %d 条过期/超期公告", expired)
        return notices
