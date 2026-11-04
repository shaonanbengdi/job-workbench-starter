# ============================================================
# 推送模块 — 企业微信机器人
# ============================================================
import logging
import time

from config import WECOM_WEBHOOK_URL

# requests 延迟到真正发送时才导入：没装时离线自检、筛选、拼消息都能正常用

logger = logging.getLogger("job_monitor.pusher")


class WeComPusher:
    """企业微信群机器人推送
    限制: 每分钟最多20条消息, 每条消息最大4096字节
    """

    def __init__(self, webhook_url=None):
        self.url = (webhook_url or WECOM_WEBHOOK_URL or "").strip()
        self._send_count = 0
        self._minute_start = time.time()
        if not self.url:
            logger.warning(
                "没配置 WECOM_WEBHOOK_URL：本轮只生成日报与控制台输出，不会推送到企业微信")

    @property
    def enabled(self):
        return bool(self.url)

    def _check_rate_limit(self):
        """频率控制: 每分钟最多18条(留余量), 超过则等待"""
        elapsed = time.time() - self._minute_start
        if elapsed < 60 and self._send_count >= 18:
            wait = 60 - elapsed + 2
            logger.info("频率限制, 等待 %.0fs...", wait)
            time.sleep(wait)
            self._send_count = 0
            self._minute_start = time.time()
        elif elapsed >= 60:
            self._send_count = 0
            self._minute_start = time.time()

    def send_markdown(self, content, max_retries=3):
        """发送 Markdown 消息，带 1–2 次指数退避重试。
        max_retries=3 表示首发 + 最多 2 次重试，退避间隔 1s / 2s。"""
        import requests
        self._check_rate_limit()
        last_err = None
        for attempt in range(max_retries):
            try:
                resp = requests.post(
                    self.url,
                    json={"msgtype": "markdown", "markdown": {"content": content}},
                    timeout=10,
                )
            except Exception as e:
                last_err = e
                logger.warning("推送请求异常: %s (尝试 %d/%d)", e, attempt + 1, max_retries)
            else:
                # 仅在实际收到响应时才计入频率计数
                self._send_count += 1
                try:
                    result = resp.json()
                except Exception:
                    result = {}
                if result.get("errcode") == 0:
                    return True
                last_err = result.get("errmsg")
                logger.warning(
                    "推送返回错误 errcode=%s msg=%s (尝试 %d/%d)",
                    result.get("errcode"), result.get("errmsg"), attempt + 1, max_retries,
                )
            # 重试前退避（最后一轮不再等待）
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
        logger.error("推送最终失败: %s", last_err)
        return False

    def push_batch(self, category, icon, items, format_item_fn):
        """批量推送: 自动处理频率限制和消息长度限制"""
        if not items:
            return 0
        if not self.enabled:
            # 没配 Webhook 时把内容打到控制台，方便先看效果再决定推不推
            logger.info("[%s] 未配置 Webhook，以下 %d 条仅打印不推送：", category, len(items))
            print("\n—— %s%s ——" % (icon, category))
            print(self._build_message(category, icon, items, format_item_fn))
            return 0

        # 先判断能否装进一条消息 (每条 max 3900 bytes)
        msg = self._build_message(category, icon, items, format_item_fn)
        if len(msg.encode("utf-8")) <= 3900:
            return len(items) if self.send_markdown(msg) else 0

        # 超长则分批发送, 每批最多10条, 批间间隔4s
        pushed = 0
        chunk_size = 10
        for i in range(0, len(items), chunk_size):
            chunk = items[i : i + chunk_size]
            msg = self._build_message(
                category, icon, chunk, format_item_fn,
                f"{i + 1}-{min(i + chunk_size, len(items))}/{len(items)}"
            )
            if self.send_markdown(msg):
                pushed += len(chunk)
            time.sleep(4)  # 批次间隔
        return pushed

    def _build_message(self, category, icon, items, format_item_fn, page=""):
        """构建消息文本"""
        header = f"**【{category}】新增 {len(items)} 条"
        if page:
            header += f" ({page})"
        header += "**\n"
        for item in items:
            header += format_item_fn(item)
        return header


def format_job(item):
    """格式化招聘岗位: 紧凑一行 + 可点击详情链接
    按筛选级别添加标记：
      target    → 🔴【重点企业】（config.TARGET_COMPANIES 命中）
      field     → 🟠【方向相关】（config.FIELD_KEYWORDS 命中）
      education → 🟡【学历匹配】（学历要求含硕士/博士/研究生）
    """
    level = item.get("match_level", "")
    if level == "target":
        badge = "🔴【重点企业】 "
    elif level == "field":
        badge = "🟠【方向相关】 "
    elif level == "education":
        badge = "🟡【学历匹配】 "
    else:
        badge = ""
    parts = [f"> **{badge}{item['title']}**"]
    parts.append(f"  {item['company']}")
    if item["location"]:
        parts.append(f" | {item['location']}")
    if item["education"]:
        parts.append(f" | {item['education']}")
    parts.append("\n")
    if item.get("url"):
        parts.append(f"> [查看详情]({item['url']})\n")
    return "".join(parts)


def format_fair(item):
    """格式化招聘会 + 可点击详情链接"""
    line = f"> **{item['title']}**\n> {item['date']} | {item['location']}\n"
    if item.get("url"):
        line += f"> [查看详情]({item['url']})\n"
    return line


def format_talk(item):
    """格式化宣讲会 + 可点击详情链接"""
    line = f"> **{item['title']}**\n> {item['date']} | {item['location']}\n"
    if item.get("url"):
        line += f"> [查看详情]({item['url']})\n"
    return line


def format_notice(item):
    """格式化通知公告 + 可点击详情链接"""
    line = f"> **{item['title']}** | {item['date']}\n"
    if item.get("url"):
        line += f"> [查看详情]({item['url']})\n"
    return line
