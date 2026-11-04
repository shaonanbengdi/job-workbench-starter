# ============================================================
# 招聘信息监控推送 — 配置文件
#
# 用之前必须改三处，其余可留默认：
#   1. SITE_CODE / SITE_HOST / SITE_WZID / CATEGORIES 里的 lmid —— 换成你自己学校的
#   2. WECOM_WEBHOOK_URL —— 换成你自己群机器人的地址
#   3. TARGET_COMPANIES / FIELD_KEYWORDS —— 换成你找工作相关的企业与方向
#
# 改完跑一次 `python selftest.py`，它会逐项检查配置对不对。
# ============================================================
import os

# 项目根目录（以本文件位置为准，从任意工作目录运行都指向同一套数据）
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ----------------------------------------
# 企业微信机器人 Webhook
# 获取方式：企业微信群 → 右上角「…」→ 群机器人 → 添加 → 复制 Webhook 地址
# 形如 https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxxxxxx-xxxx-xxxx
# 留空则只生成日报、不推送（可以先这么试跑）
# ----------------------------------------
WECOM_WEBHOOK_URL = ""

# ----------------------------------------
# 路径配置（均基于项目根目录，便于整体搬迁）
# ----------------------------------------
DB_PATH = os.path.join(BASE_DIR, "job_monitor.db")          # 去重数据库
REPORT_DIR = os.path.join(BASE_DIR, "reports")              # 日报输出目录
LOG_PATH = os.path.join(BASE_DIR, "job_monitor.log")        # 运行日志

# ----------------------------------------
# 目标站点（91job 智慧就业平台）
#
# 这几项从你自己学校站点的网址和页面请求里拿，取法见 README：
#   SITE_HOST : 域名，例如 xxx.91job.org.cn
#   SITE_CODE : 学校代码，一般是网址里的那串 5 位数字
#   SITE_WZID : 站点 ID，抓包看请求参数里的 wzid
# ----------------------------------------
SITE_HOST = ""          # 例："xxx.91job.org.cn"（不要带 https:// 和结尾斜杠）
SITE_CODE = ""          # 例："12345"
SITE_WZID = ""          # 例："A1B2C3D4E5F60718293A4B5C6D7E8F90"（假的，仅供参考格式）
SITE_URL = ""           # 学校就业网首页，用于请求头 Referer；留空则用 https://{SITE_HOST}

# ----------------------------------------
# 采集栏目
#
# lmid 是栏目标识，每个学校、每个栏目都不一样，必须自己抓。
# 取法：打开学校就业网对应栏目，F12 → Network → 找 getLbsj 请求 → 看 lmid 参数。
# rows 是每次抓多少条，只影响单次抓取量，去重逻辑保证不会重复推送。
# enabled=False 的栏目不抓。
# ----------------------------------------
CATEGORIES = {
    "招聘岗位": {
        "enabled": True,
        "lmid": "",          # ← 必填：岗位列表栏目
        "rows": 50,
        "icon": "【岗位】",
    },
    "招聘会": {
        "enabled": True,
        "lmid": "",          # ← 必填：招聘会栏目
        "rows": 20,
        "icon": "【招聘会】",
    },
    "宣讲会": {
        "enabled": True,
        "lmid": "",          # ← 必填：宣讲会栏目
        "rows": 20,
        "icon": "【宣讲会】",
    },
    "通知公告": {
        "enabled": True,
        "lmid": "",          # ← 必填：公告栏目
        "rows": 10,
        "icon": "【公告】",
    },
}

# ----------------------------------------
# 关键词过滤（留空 = 不做这层过滤）
# 仅对「招聘岗位」生效，匹配岗位标题 + 公司名
# 用于在分级筛选之前先粗筛一遍，例如只要含"校招""实习"的
# ----------------------------------------
KEYWORDS = []

# ----------------------------------------
# 岗位分级筛选
#
# 三级标记，按优先级从高到低：
#   🔴 重点企业  —— 命中 TARGET_COMPANIES（你自己最想去的那批）
#   🟠 方向相关  —— 命中 FIELD_KEYWORDS（你这个专业对口的行业/岗位词）
#   🟡 学历匹配  —— 学历要求含硕士/博士/研究生
#
# 下面两份名单都是示例，**必须换成你自己的**。
# 匹配是子串匹配、不区分大小写，所以写简称就够，别写全称。
# ----------------------------------------

# 重点企业（写企业名里的关键词即可，"某某汽车"能匹配"某某汽车集团股份有限公司"）
TARGET_COMPANIES = [
    # —— 示例，换成你的目标企业 ——
    "星驰汽车", "远航动力", "宏途智能",
]

# 方向关键词（判断"这个岗位跟我的专业沾不沾边"）
# 示例按车辆工程方向给，其他专业照这个思路改成自己的
FIELD_KEYWORDS = [
    # 产品 / 行业
    "汽车", "整车", "乘用车", "商用车", "客车", "底盘", "发动机",
    "变速器", "传动", "制动", "悬架", "汽车电子", "电驱", "电池",
    "新能源", "电动汽车", "混动", "氢能", "燃料电池",
    "自动驾驶", "智能驾驶", "ADAS", "车联网", "车载",
    "零部件", "动力总成", "工程机械", "农机",
    # 工种 / 方向
    "CAE", "CFD", "NVH", "仿真", "热管理", "结构", "试验", "标定",
]

# 学历匹配关键词（岗位学历要求里出现即算匹配）
GRADUATE_EDU_KEYWORDS = ["硕士", "博士", "研究生"]

# 筛选模式
#   target_only : 只推重点企业
#   field       : 重点企业 + 方向相关（较严）
#   edu         : 重点企业 + 方向相关 + 学历匹配（推荐，宽松些）
#   all         : 不筛选，全量推送
FILTER_MODE = "edu"

# ----------------------------------------
# 常驻调度参数（只有直接运行 main.py 才用到）
# 用 WorkBuddy 定时任务或系统计划任务时，这些不影响 ——
# 那种方式每次只跑 run_automation.py 一次就退出。
# 每天从 9:00 起，每 8 小时一次（9:00 / 17:00 / 1:00）
# ----------------------------------------
SCHEDULE_START_HOUR = 9
SCHEDULE_INTERVAL_HOURS = 8


def site_url():
    """学校就业网首页，Referer 用。SITE_URL 没填就按域名拼。"""
    if SITE_URL:
        return SITE_URL
    return "https://%s" % SITE_HOST if SITE_HOST else ""


def api_base():
    """列表接口前缀。"""
    return "https://%s/web/wsjysc/wzsy" % SITE_HOST if SITE_HOST else ""


def detail_url(kind, item_id):
    """详情页链接。kind 取 job / fair / talk / notice。

    各校 91job 详情页路由是同一套模板，只需换域名与学校代码。
    """
    if not SITE_HOST:
        return ""
    routes = {
        "job": ("jobDetails", "zpgwid"),
        "fair": ("recruitmentDetail", "zphid"),
        "talk": ("lectureDetail", "xjhid"),
        "notice": ("announcementDetail", "xwid"),
    }
    route, param = routes[kind]
    return "https://%s/sub-station/%s?%s=%s&xxdm=%s" % (
        SITE_HOST, route, param, item_id, SITE_CODE)
