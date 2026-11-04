# -*- coding: utf-8 -*-
"""
个人求职工作台 · 本地服务端
零第三方依赖（仅用 Python 标准库），数据落盘到同目录下的 data.db（SQLite）。
包含：招聘岗位/招聘会/宣讲会/通知公告 抓取 + 候选池管理

用法：
    python server.py            # 启动并自动打开浏览器
    python server.py --no-open  # 只启动服务
    python server.py --port 9000
"""
import argparse
import json
import os
import shutil
import sqlite3
import sys
import threading
import time
import urllib.request
import urllib.error
import webbrowser
from datetime import datetime, date, timedelta
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

def _setup_console():
    """Windows 控制台默认 GBK 代码页，直接 print 中文会变成乱码。

    打包成 exe 双击运行时尤其明显。把代码页和标准流都切到 UTF-8；
    拿不到控制台（比如被重定向给管道）就跳过，不影响服务。
    """
    if os.name != 'nt':
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:
        pass
    for name in ('stdout', 'stderr'):
        stream = getattr(sys, name, None)
        try:
            if stream is not None and hasattr(stream, 'reconfigure'):
                stream.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass


def _app_dirs():
    """返回 (资源目录, 数据目录)。

    源码运行：两者都是 server.py 所在目录，行为和以前一致。
    打包运行：资源在 sys._MEIPASS（onedir 是 _internal，onefile 是临时解压目录），
    但数据必须落在 exe 同级 —— 打进临时目录的话，程序一关数据就没了。
    """
    if getattr(sys, 'frozen', False):
        res = getattr(sys, '_MEIPASS',
                      os.path.dirname(os.path.abspath(sys.executable)))
        data = os.path.dirname(os.path.abspath(sys.executable))
    else:
        res = data = os.path.dirname(os.path.abspath(__file__))
    return res, data


RES_DIR, DATA_DIR = _app_dirs()

DB_PATH = os.path.join(DATA_DIR, 'data.db')
BACKUP_DIR = os.path.join(DATA_DIR, 'backup')
BUNDLED_DB = os.path.join(RES_DIR, 'data.db')   # 便携版内置的示例库
APP_ID = 'job-workbench'
DEFAULT_PORT = 8765

_lock = threading.Lock()
_last_backup_date = None

# ============================================================
# 数据库 Schema
# ============================================================
SCHEMA = """
CREATE TABLE IF NOT EXISTS profile(
  id INTEGER PRIMARY KEY CHECK(id=1),
  name TEXT, phone TEXT, email TEXT, edu TEXT, research TEXT,
  targetJobs TEXT, cities TEXT, salary TEXT, tech TEXT,
  companies TEXT, avoid TEXT, extra TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS cards(
  id TEXT PRIMARY KEY, name TEXT, type TEXT, start TEXT, "end" TEXT,
  resume TEXT, story TEXT, sample INTEGER DEFAULT 0,
  extra TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS jobs(
  id TEXT PRIMARY KEY, company TEXT, title TEXT, jd TEXT, link TEXT,
  importance TEXT, tags TEXT, sample INTEGER DEFAULT 0,
  extra TEXT, created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS apps(
  id TEXT PRIMARY KEY, jobId TEXT, company TEXT, position TEXT,
  planDate TEXT, appliedDate TEXT, status TEXT, sample INTEGER DEFAULT 0,
  extra TEXT, updated_at TEXT, sort_order INTEGER
);
CREATE TABLE IF NOT EXISTS events(
  id TEXT PRIMARY KEY, title TEXT, type TEXT, date TEXT, time TEXT,
  jobId TEXT, remindVal INTEGER, remindUnit TEXT, sample INTEGER DEFAULT 0,
  extra TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
-- 候选池（招聘信息）
CREATE TABLE IF NOT EXISTS candidates(
  id TEXT PRIMARY KEY,
  title TEXT, company TEXT, city TEXT, salary TEXT,
  education TEXT, experience TEXT, source TEXT,
  publish_date TEXT, url TEXT, category TEXT,
  fair_date TEXT, fair_location TEXT,
  status TEXT DEFAULT 'pending',
  action TEXT DEFAULT '', action_job TEXT, action_event TEXT,
  scraped_at TEXT, processed_at TEXT,
  extra TEXT
);
-- 已抓取 ID 去重表
CREATE TABLE IF NOT EXISTS scraped_ids(
  item_id TEXT PRIMARY KEY, category TEXT, scraped_at TEXT
);
-- 自定义标签：normal=普通标签，rule=规则标签（如「最多投递 3 个岗位」）
CREATE TABLE IF NOT EXISTS tags(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE, color TEXT,
  kind TEXT DEFAULT 'normal', rule_type TEXT DEFAULT '', rule_value INTEGER DEFAULT 0,
  created_at TEXT
);
"""

CARD_COLS = ['id', 'name', 'type', 'start', 'end', 'resume', 'story', 'sample']
JOB_COLS = ['id', 'company', 'title', 'jd', 'link', 'importance', 'tags', 'sample', 'created_at']
APP_COLS = ['id', 'jobId', 'company', 'position', 'planDate', 'appliedDate', 'status', 'sample']
EVENT_COLS = ['id', 'title', 'type', 'date', 'time', 'jobId', 'remindVal', 'remindUnit', 'sample']
PROFILE_COLS = ['name', 'phone', 'email', 'edu', 'research', 'targetJobs',
                'cities', 'salary', 'tech', 'companies', 'avoid']
CAND_COLS = ['id', 'title', 'company', 'city', 'salary', 'education', 'experience',
             'source', 'publish_date', 'url', 'category', 'fair_date', 'fair_location',
             'status', 'action', 'action_job', 'action_event', 'scraped_at', 'processed_at']

# ============================================================
# 招聘信息抓取（可选模块）
#
# 默认是空配置，不连接任何真实站点，点「抓取」会立刻返回 0 条。
# 想接自己学校的 91job 就业平台时，按下面三处改：
#   1) SITE_CODE  院校代码（国标码，出现在站点 URL 末尾）
#   2) SITE_URL / SCRAPER_BASE / DETAIL_BASE  该站点的页面与列表接口地址
#   3) CATEGORIES 里对应栏目的 lmid（在浏览器 F12 的网络面板里能看到），
#      并把 enabled 改成 True
# 各校 91job 子站的接口路径基本一致，只换域名和院校代码即可。
# ============================================================
SITE_CODE = ''
SITE_URL = ''
SCRAPER_BASE = ''
DETAIL_BASE = ''
JOB_DETAIL_URL = DETAIL_BASE + '/sub-station/jobDetails?zpgwid={zpgwid}&xxdm=' + SITE_CODE
FAIR_DETAIL_URL = DETAIL_BASE + '/sub-station/recruitmentDetail?zphid={zphid}&xxdm=' + SITE_CODE
TALK_DETAIL_URL = DETAIL_BASE + '/sub-station/lectureDetail?xjhid={xjhid}&xxdm=' + SITE_CODE
NOTICE_DETAIL_URL = DETAIL_BASE + '/sub-station/announcementDetail?xwid={xwid}&xxdm=' + SITE_CODE

CATEGORIES = {
    '招聘岗位': {
        'enabled': False,
        'lmid': '',
        'rows': 200,
    },
    '招聘会': {
        'enabled': False,
        'lmid': '',
        'rows': 15,
    },
    '宣讲会': {
        'enabled': False,
        'lmid': '',
        'rows': 15,
    },
    '通知公告': {
        'enabled': False,
        'lmid': '',
        'rows': 10,
    },
}

# 目标企业清单：招聘信息抓回来后，公司名命中这里的关键字才算「对口」。
# 按包含匹配，填简称即可。下面的名字全部虚构，是给示例用的，
# 换成一你自己关注的企业即可（一行一个，引号逗号隔开）。
TARGET_COMPANIES = [
    '星驰汽车','远航动力','宏途智能','云淞精工','蓝湾研究院',
    '北辰传动','长风客车','启明电驱',
]
VEHICLE_KEYWORDS = [
    '车辆','汽车','整车','乘用车','商用车','客车','卡车',
    '重卡','轻卡','专用车','特种车辆','底盘','发动机',
    '内燃机','变速器','变速箱','传动','制动','转向',
    '悬架','车灯','座椅','内饰','外饰','轮胎','橡胶',
    '汽车电子','电控','电机','电驱','电池','锂电池',
    '动力电池','新能源','电动汽车','混动','氢能','燃料电池',
    '自动驾驶','智能驾驶','无人驾驶','ADAS','车联网',
    '智能网联','车载','零部件','汽车部件','汽车配件',
    '动力总成','涡轮','液压','工程机械','汽车研发',
    '汽车工程','汽车研究','整车厂','Tier1','汽车检测',
    '汽车测试','三电',
]


# 规则标签：规则类型 -> 展示模板（%d 会被阈值替换）
RULE_TYPES = {
    'max_apply': '最多投递 %d 个岗位',
}


def now_str():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    # WAL 更快，但依赖 -shm 共享内存，exFAT / FAT32 的 U 盘和部分网络盘不支持。
    # 回读实际生效的模式：拿不到 wal 就退回 DELETE，避免在 U 盘上写坏库。
    try:
        mode = conn.execute('PRAGMA journal_mode=WAL').fetchone()[0]
        if str(mode).lower() != 'wal':
            conn.execute('PRAGMA journal_mode=DELETE')
    except sqlite3.DatabaseError:
        conn.execute('PRAGMA journal_mode=DELETE')
    return conn


def _migrate_tags(conn):
    """老库的 tags 表只有 id/name/color/created_at，补上规则字段。"""
    cols = {r[1] for r in conn.execute('PRAGMA table_info(tags)').fetchall()}
    if 'kind' not in cols:
        conn.execute("ALTER TABLE tags ADD COLUMN kind TEXT DEFAULT 'normal'")
    if 'rule_type' not in cols:
        conn.execute("ALTER TABLE tags ADD COLUMN rule_type TEXT DEFAULT ''")
    if 'rule_value' not in cols:
        conn.execute("ALTER TABLE tags ADD COLUMN rule_value INTEGER DEFAULT 0")
    conn.commit()


def init_db():
    conn = connect()
    conn.executescript(SCHEMA)
    _migrate_tags(conn)
    conn.commit()
    conn.close()


def maybe_backup():
    global _last_backup_date
    today = datetime.now().strftime('%Y-%m-%d')
    if _last_backup_date == today or not os.path.exists(DB_PATH):
        return
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        dst = os.path.join(BACKUP_DIR, 'data-%s.db' % today)
        if not os.path.exists(dst):
            src = connect()
            src.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            bk = sqlite3.connect(dst)
            src.backup(bk)
            bk.close()
            src.close()
        _last_backup_date = today
    except Exception as e:
        print('[warn] backup failed:', e)


def row_to_dict(row, cols, json_fields=()):
    d = {}
    for c in cols:
        v = row[c] if row[c] is not None else (''
                    if c not in ('sample', 'remindVal') else 0)
        d[c] = v
    extra = row['extra'] if 'extra' in row.keys() else None
    if extra:
        try:
            d.update(json.loads(extra))
        except Exception:
            pass
    for f in json_fields:
        if f not in row.keys():
            continue
        raw = row[f]
        try:
            d[f] = json.loads(raw) if raw else []
        except Exception:
            d[f] = []
    return d


# ============================================================
# 原有读写（profile/cards/jobs/apps/events/meta）
# ============================================================
def read_all(conn):
    profile = {}
    row = conn.execute('SELECT * FROM profile WHERE id=1').fetchone()
    if row:
        profile = {c: (row[c] or '') for c in PROFILE_COLS}
        if row['extra']:
            try:
                profile.update(json.loads(row['extra']))
            except Exception:
                pass

    cards = []
    for r in conn.execute('SELECT * FROM cards ORDER BY rowid'):
        cards.append(row_to_dict(r, CARD_COLS))

    jobs = []
    for r in conn.execute('SELECT * FROM jobs ORDER BY rowid'):
        jobs.append(row_to_dict(r, JOB_COLS, json_fields=('tags',)))

    apps = []
    for r in conn.execute('SELECT * FROM apps ORDER BY sort_order, rowid'):
        apps.append(row_to_dict(r, APP_COLS))

    events = []
    for r in conn.execute('SELECT * FROM events ORDER BY date, time'):
        events.append(row_to_dict(r, EVENT_COLS))

    meta = {r['key']: r['value'] for r in conn.execute('SELECT * FROM meta')}
    return {'profile': profile, 'cards': cards, 'jobs': jobs,
            'apps': apps, 'events': events, 'meta': meta}


def write_all(conn, data):
    now = now_str()
    cur = conn.cursor()

    p = data.get('profile') or {}
    extra_p = {k: v for k, v in p.items() if k not in PROFILE_COLS}
    cur.execute('DELETE FROM profile')
    cur.execute(
        'INSERT INTO profile(id,%s,extra,updated_at) VALUES(1,%s,?,?)'
        % (','.join(PROFILE_COLS), ','.join('?' * len(PROFILE_COLS))),
        [p.get(c, '') for c in PROFILE_COLS]
        + [json.dumps(extra_p, ensure_ascii=False), now])

    def replace(table, rows, cols, order_col=None):
        cur.execute('DELETE FROM %s' % table)
        cols_sql = ','.join('"%s"' % c for c in cols)
        for i, item in enumerate(rows or []):
            vals, extra = [], {}
            for c in cols:
                v = item.get(c)
                if c == 'tags':
                    vals.append(json.dumps(v or [], ensure_ascii=False))
                elif c == 'sample':
                    vals.append(1 if v else 0)
                elif c == 'remindVal':
                    vals.append(int(v) if v not in (None, '') else 0)
                else:
                    vals.append(v if v is not None else '')
            extra.update({k: v for k, v in item.items() if k not in cols})
            cur.execute(
                'INSERT INTO %s(%s,extra) VALUES(%s,?)'
                % (table, cols_sql, ','.join('?' * len(cols))),
                vals + [json.dumps(extra, ensure_ascii=False)])
            if order_col:
                cur.execute('UPDATE %s SET %s=? WHERE id=?' % (table, order_col),
                            (i, item.get('id')))

    replace('cards', data.get('cards'), CARD_COLS)
    replace('jobs', data.get('jobs'), JOB_COLS)
    replace('apps', data.get('apps'), APP_COLS, order_col='sort_order')
    replace('events', data.get('events'), EVENT_COLS)

    for k, v in (data.get('meta') or {}).items():
        cur.execute('INSERT INTO meta(key,value) VALUES(?,?) '
                    'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                    (k, json.dumps(v, ensure_ascii=False)
                     if not isinstance(v, str) else v))
    conn.commit()


def set_meta(conn, key, value):
    conn.execute('INSERT INTO meta(key,value) VALUES(?,?) '
                 'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                 (key, json.dumps(value, ensure_ascii=False)
                  if not isinstance(value, str) else value))
    conn.commit()


# ============================================================
# 爬虫模块（基于 urllib.request，零依赖）
# ============================================================
def _parse_date(value, fmt='%Y-%m-%d'):
    if not value:
        return None
    try:
        return datetime.strptime(value, fmt).date()
    except (ValueError, TypeError):
        return None


def _classify_job(title, company, education=''):
    text = '%s %s' % (title, company)
    text_lower = text.lower()
    edu_lower = education.lower()
    for kw in TARGET_COMPANIES:
        if kw.lower() in text_lower:
            return 'target'
    for kw in VEHICLE_KEYWORDS:
        if kw.lower() in text_lower:
            return 'vehicle'
    for kw in ('硕士', '博士', '研究生'):
        if kw in edu_lower:
            return 'education'
    return None


def _fetch(url, params=None, timeout=15):
    """urllib 请求，返回 JSON 或 None"""
    if params:
        qs = '&'.join('%s=%s' % (k, urllib.request.quote(str(v))) for k, v in params.items())
        url = '%s?%s' % (url, qs)
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Referer': SITE_URL,
        'Accept': 'application/json, text/plain, */*',
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            if data.get('success'):
                return data.get('result', [])
            return []
    except Exception as e:
        print('[scraper] 请求失败 %s: %s' % (url, e))
        return []


def _scrape_category(name, cfg):
    """抓取单个栏目并标准化"""
    raw = _fetch(SCRAPER_BASE, {'lmid': cfg['lmid'], 'row': cfg['rows']})
    items = []
    if name == '招聘岗位':
        today = date.today()
        cutoff = date(today.year, 8, 1)  # 从8月1日开始抓取
        for company in raw:
            dwmc = company.get('dwmc', '未知公司')
            zzshsj = company.get('zzshsj', '')
            post_date = _parse_date(zzshsj)
            for pos in company.get('zpzw', []):
                pid = str(pos.get('zpgwid', ''))
                if post_date is None or post_date < cutoff:
                    continue
                title = pos.get('zwmc', '未知岗位')
                edu = pos.get('xlyq', '')
                loc = pos.get('gzdd', '')
                match_level = _classify_job(title, dwmc, edu)
                items.append({
                    'id': 'job_%s' % pid,
                    'item_id': pid,
                    'title': title,
                    'company': dwmc,
                    'city': loc,
                    'salary': '',
                    'education': edu,
                    'experience': '',
                    'source': '91job',
                    'publish_date': zzshsj,
                    'url': JOB_DETAIL_URL.format(zpgwid=pid),
                    'category': '招聘岗位',
                    'fair_date': '',
                    'fair_location': '',
                    'match_level': match_level,
                })
    elif name == '招聘会':
        today = date.today()
        for item in raw:
            fair_date = _parse_date(item.get('jbkssj', ''))
            if fair_date is None or fair_date < today:
                continue
            fid = str(item.get('zphid', ''))
            items.append({
                'id': 'fair_%s' % fid,
                'item_id': fid,
                'title': item.get('zphmc', ''),
                'company': item.get('gljg', ''),
                'city': '',
                'salary': '',
                'education': '',
                'experience': '',
                'source': '91job',
                'publish_date': item.get('jbkssj', ''),
                'url': FAIR_DETAIL_URL.format(zphid=fid),
                'category': '招聘会',
                'fair_date': item.get('jbkssj', ''),
                'fair_location': item.get('jbcd', ''),
                'match_level': None,
            })
    elif name == '宣讲会':
        today = date.today()
        for item in raw:
            talk_date = _parse_date(item.get('jbrq', ''))
            if talk_date is None or talk_date < today:
                continue
            tid = str(item.get('xjhid', ''))
            items.append({
                'id': 'talk_%s' % tid,
                'item_id': tid,
                'title': item.get('xjhmc', ''),
                'company': item.get('xjxx', ''),
                'city': '',
                'salary': '',
                'education': '',
                'experience': '',
                'source': '91job',
                'publish_date': item.get('jbrq', ''),
                'url': TALK_DETAIL_URL.format(xjhid=tid),
                'category': '宣讲会',
                'fair_date': item.get('jbrq', ''),
                'fair_location': item.get('jbdd', ''),
                'match_level': None,
            })
    elif name == '通知公告':
        today = date.today()
        cutoff = today - timedelta(days=30)
        for item in raw:
            pub_date = _parse_date(item.get('fbsj', ''))
            if pub_date is None or pub_date < cutoff:
                continue
            nid = str(item.get('xwid', ''))
            url = item.get('tzljdz') or NOTICE_DETAIL_URL.format(xwid=nid)
            items.append({
                'id': 'notice_%s' % nid,
                'item_id': nid,
                'title': item.get('xwbt', ''),
                'company': '',
                'city': '',
                'salary': '',
                'education': '',
                'experience': '',
                'source': '91job',
                'publish_date': item.get('fbsj', ''),
                'url': url,
                'category': '通知公告',
                'fair_date': '',
                'fair_location': '',
                'match_level': None,
            })
    return items


def do_scrape(conn):
    """执行一次完整抓取，返回 { total_new, categories: {name: {fetched, valid, new}}, items }"""
    now = now_str()
    total_new = 0
    categories = {}
    all_new = []
    cur = conn.cursor()
    for name, cfg in CATEGORIES.items():
        if not cfg.get('enabled'):
            continue
        items = _scrape_category(name, cfg)
        fetched = cfg['rows']
        # 去重：检查 scraped_ids 表
        new_items = []
        for item in items:
            iid = item['item_id']
            exists = cur.execute('SELECT 1 FROM scraped_ids WHERE item_id=?', (iid,)).fetchone()
            if not exists:
                cur.execute(
                    'INSERT INTO scraped_ids(item_id,category,scraped_at) VALUES(?,?,?)',
                    (iid, name, now))
                new_items.append(item)
        # 写入 candidates 表
        for item in new_items:
            try:
                cur.execute(
                    'INSERT INTO candidates(id,title,company,city,salary,education,experience,'
                    'source,publish_date,url,category,fair_date,fair_location,status,scraped_at,extra)'
                    ' VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (item['id'], item['title'], item['company'], item['city'],
                     item['salary'], item['education'], item['experience'],
                     item['source'], item['publish_date'], item['url'],
                     item['category'], item['fair_date'], item['fair_location'],
                     'pending', now,
                     json.dumps({'match_level': item.get('match_level')}, ensure_ascii=False)))
            except sqlite3.IntegrityError:
                pass  # 已存在，跳过
        conn.commit()
        categories[name] = {
            'fetched': fetched,
            'valid': len(items),
            'new': len(new_items),
        }
        total_new += len(new_items)
        all_new.extend(new_items)
    return {'total_new': total_new, 'categories': categories, 'items': all_new}


# ============================================================
# 浏览器插件：岗位卡片捕获
# ============================================================
# 为什么单独开一个端点，而不是复用 /api/state：
#   /api/state 是「整库覆写」——前端把内存里的 DB 全量 POST 上来，
#   后端对这 jobs/apps/cards/events 逐表 DELETE 再 INSERT。
#   插件在别的标签页抓岗位时，工作台页面往往正开着、内存里是旧数据，
#   一旦用户随手点一下页面，插件刚写进去的卡片就会被旧 DB 整表抹掉。
#   所以插件走这条只 upsert 单条 jobs 记录的通道，两条通道互不打扰。
#   candidates 本来就不在覆写范围内，直接复用它的写入逻辑。

PLUGIN_JOB_COLS = ['id', 'company', 'title', 'jd', 'link', 'importance',
                   'tags', 'sample', 'created_at', 'updated_at']

# 允许写入的扩展字段（写进 jobs.extra，读出来会被平铺回顶层给前端用）
PLUGIN_EXTRA_KEYS = [
    'city', 'salary', 'education', 'experience', 'department', 'headcount',
    'jobType', 'deadline', 'publishDate', 'source', 'sourceHost', 'sourcePage',
    'sourceAdapter', 'capturedAt', 'captureConfidence', 'batch', 'candidate_id',
]


def _clean_str(v, limit=20000):
    if v is None:
        return ''
    s = str(v).replace('\x00', '')
    return s[:limit]


def _norm_company(company):
    """公司名归一化：与插件侧 job-parser.js 的 normalizeCompany 保持同一口径。

    两个坑（2026-10-10 实测踩到）：
      ① 后缀必须整体匹配（「股份有限公司」），拆成「公司」「股份」轮着剥会在
         中间停住——「测试汽车股份」长度正好等于下限，「集团公司」「科技股份」
         这类组合后缀就剥不干净，同一家公司归不到一起。
      ② 末尾括号只在「≤6 字的分支/基地」这种短后缀上切除，否则
         「测试汽车（集团）」会被连「集团」一起切掉，和「测试汽车集团」对不上。
      ③ 剥到不足 3 字就停：「上海汽车集团股份有限公司技术中心」一路剥成
         「上海」会和其他上海公司撞车，制造误判。
    """
    import re
    c = _clean_str(company, 60)
    c = re.sub(r'^(【[^】]{0,12}】|\[[^\]]{0,12}\]|诚聘|急聘|招聘|热招|高薪|知名企业[:：]?)', '', c)
    c = re.sub(r'[（(][^）)]{0,6}[）)]\s*$', '', c)
    c = re.sub(r'[\s·•・\-—_]', '', c).lower()
    guard = 0
    while guard < 6:
        nxt = re.sub(
            r'(股份有限公司|有限责任公司|有限公司|股份|集团|控股|科技|技术|工业|实业|制造|'
            r'汽车|公司|厂|研究院|研究所|中心)$', '', c)
        if nxt == c or len(nxt) < 3:
            break
        c = nxt
        guard += 1
    return c


def _norm_title(title):
    """岗位名归一化：去括号注释与编号，去掉一切空白。"""
    import re
    t = _clean_str(title, 80)
    t = re.sub(r'^[【\[（(][^】\]）)]{0,20}[】\]）)]', '', t)
    t = re.sub(r'[（(][^）)]{0,12}[）)]', '', t)   # 含「（测试）」「（J12345）」「（急聘）」
    t = re.sub(r'^(急聘|诚聘|热招|高薪招聘|招聘)[·:：\-]?', '', t)
    t = re.sub(r'(急聘|热招|长期有效)$', '', t)
    return re.sub(r'[\s]', '', t).lower()


def _title_similar(a, b):
    """岗位名相似度（字符 bigram 的 Dice 系数），与插件侧 titleSimilarity 同算法。"""
    x, y = _norm_title(a), _norm_title(b)
    if not x or not y:
        return 0.0
    if x == y:
        return 1.0
    if x in y or y in x:
        ratio = min(len(x), len(y)) / max(len(x), len(y))
        return 0.7 + 0.3 * ratio
    gx = {x[i:i + 2] for i in range(len(x) - 1)} or {x}
    gy = {y[i:i + 2] for i in range(len(y) - 1)} or {y}
    inter = len(gx & gy)
    return (2 * inter) / (len(gx) + len(gy))


def _min_sort_order(cur):
    """jobs 的 sortOrder 存在 extra JSON 里（不在列上），取全表最小值。
    新卡用 min-1 排到手动排序最前；库里有 sortOrder=0 的卡时照样得到负数，不影响。"""
    min_so = 0
    for r in cur.execute('SELECT extra FROM jobs').fetchall():
        try:
            e = json.loads(r['extra'] or '{}')
        except Exception:
            continue
        so = e.get('sortOrder')
        if isinstance(so, (int, float)) and so < min_so:
            min_so = so
    return min_so


def capture_job(conn, data):
    """插件入库：单条岗位卡片 upsert。

    写入范围严格限定在 jobs（+ 可选 candidates），不碰 cards/apps/events，
    因此不会和前端整库覆写互相打架。
    """
    job = data.get('job') or {}
    company = _clean_str(job.get('company'), 60).strip()
    title = _clean_str(job.get('title'), 80).strip()
    if not company or not title:
        return {'ok': False, 'error': '企业与岗位名不能为空'}, 400

    now = now_str()
    force = bool(data.get('force'))
    jid = _clean_str(job.get('id'), 80).strip()
    if not jid:
        import hashlib
        jid = 'job_jobcap_' + hashlib.md5(
            ('%s%s' % (company, title)).encode('utf-8')).hexdigest()[:12]

    # 扩展字段
    extra = {}
    for k in PLUGIN_EXTRA_KEYS:
        if k in job and job[k] not in (None, ''):
            extra[k] = job[k]
    extra['viaPlugin'] = True
    extra['source'] = _clean_str(job.get('source'), 40) or '浏览器插件'
    extra['lastCaptureAt'] = _clean_str(job.get('capturedAt'), 30) or now

    tags = job.get('tags') or []
    if not isinstance(tags, list):
        tags = [str(tags)]
    tags = [str(t)[:40] for t in tags if str(t).strip()][:12]

    importance = _clean_str(job.get('importance'), 10)
    if importance not in ('focus', 'can', 'hold', 'skip'):
        importance = 'can'

    jd = _clean_str(job.get('jd'), 40000)
    link = _clean_str(job.get('link'), 1000)

    cur = conn.cursor()
    row = cur.execute('SELECT * FROM jobs WHERE id=?', (jid,)).fetchone()
    matched_by = ''   # 认亲成功时记录下来，返回给插件用于提示；各分支都必须有它

    # 同一岗位被再次抓到时，即使 id 对不上（用户改了企业简称、站点换了编号），
    # 也要认出来并更新原卡，而不是新建一张重复卡。
    if not row and not force:
        want_co = _norm_company(company)
        best, best_score = None, 0.0
        for r in cur.execute('SELECT * FROM jobs').fetchall():
            if _norm_company(r['company']) != want_co:
                continue
            score = _title_similar(r['title'], title)
            if score > best_score:
                best, best_score = r, score
        # 同一家公司 + 岗位名相似度 ≥ 0.86 视为同一岗位；
        # 阈值取高一点，宁可多建一张卡让用户自己合并，也不要错误覆盖已有卡片
        if best is not None and best_score >= 0.86:
            matched_by = '同公司+岗位名相似度 %.0f%%' % (best_score * 100)
            row = best
            jid = best['id']

    if row:
        old_extra = {}
        try:
            old_extra = json.loads(row['extra'] or '{}')
        except Exception:
            pass
        merged = dict(old_extra)
        merged.update(extra)
        # 已有人工填写的字段不被空值覆盖
        for k, v in extra.items():
            if v in (None, '') and old_extra.get(k):
                merged[k] = old_extra[k]
        # 累加抓取次数，便于事后核对
        merged['captureCount'] = int(old_extra.get('captureCount') or 0) + 1
        merged['firstCaptureAt'] = old_extra.get('firstCaptureAt') or merged.get('capturedAt') or now
        # 老卡若缺录入时间戳，用首次抓取时间补上（前端「录入时间」列读 createdAt）
        if not merged.get('createdAt'):
            merged['createdAt'] = merged.get('firstCaptureAt') or now

        new_jd = jd or row['jd'] or ''
        new_link = link or row['link'] or ''
        new_tags = tags or []
        try:
            new_tags = json.loads(row['tags'] or '[]')
        except Exception:
            pass
        for t in tags:
            if t not in new_tags:
                new_tags.append(t)
        new_tags = new_tags[:14]

        cur.execute(
            'UPDATE jobs SET company=?, title=?, jd=?, link=?, tags=?, extra=?, updated_at=? WHERE id=?',
            (company, title, new_jd, new_link,
             json.dumps(new_tags, ensure_ascii=False),
             json.dumps(merged, ensure_ascii=False), now, jid))
        action, created = 'updated', False
    else:
        extra['captureCount'] = 1
        extra['firstCaptureAt'] = extra['lastCaptureAt']
        # 新卡排到最前（sortOrder 取全表最小值再减一），并补录入时间戳
        extra['sortOrder'] = _min_sort_order(cur) - 1
        extra['createdAt'] = now
        cur.execute(
            'INSERT INTO jobs(id,company,title,jd,link,importance,tags,sample,extra,created_at,updated_at)'
            ' VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            (jid, company, title, jd, link, importance,
             json.dumps(tags, ensure_ascii=False), 0,
             json.dumps(extra, ensure_ascii=False), now, now))
        action, created = 'created', True

    # 可选：同步写一条候选池记录（候选池不在整库覆写范围内，安全）
    cand_imported = cand_skipped = 0
    cand = data.get('candidate')
    if cand and isinstance(cand, dict):
        cid = _clean_str(cand.get('id'), 80).strip() or jid
        exists = cur.execute('SELECT 1 FROM candidates WHERE id=?', (cid,)).fetchone()
        if exists:
            cand_skipped = 1
        else:
            try:
                cur.execute(
                    'INSERT INTO candidates(id,title,company,city,salary,education,experience,'
                    'source,publish_date,url,category,fair_date,fair_location,status,scraped_at,extra)'
                    ' VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (cid, _clean_str(cand.get('title'), 120), _clean_str(cand.get('company'), 80),
                     _clean_str(cand.get('city'), 60), _clean_str(cand.get('salary'), 60),
                     _clean_str(cand.get('education'), 40), _clean_str(cand.get('experience'), 40),
                     _clean_str(cand.get('source'), 40) or '招聘信息·浏览器插件',
                     _clean_str(cand.get('publish_date'), 30), _clean_str(cand.get('url'), 1000),
                     _clean_str(cand.get('category'), 20) or '招聘岗位',
                     '', '', 'pending', now,
                     json.dumps(cand.get('extra') or {}, ensure_ascii=False)))
                cand_imported = 1
            except sqlite3.IntegrityError:
                cand_skipped = 1

    conn.commit()
    return {
        'ok': True,
        'action': action,
        'created': created,
        'updated': not created,
        'job_id': jid,
        'company': company,
        'title': title,
        'matched_by': matched_by,
        'candidate_imported': cand_imported,
        'candidate_skipped': cand_skipped,
        'updated_at': now,
    }, 200


def uncapture_job(conn, data):
    """插件入库的撤销。

    注意：不能真删——前端页面上可能正开着旧 DB，一旦用户操作，
    整库覆写会把删掉的记录「复活」回来。所以撤销 = 打标记，
    前端渲染时跳过 viaPlugin 且已撤销的记录。
    """
    jid = _clean_str(data.get('job_id') or data.get('id'), 80).strip()
    if not jid:
        return {'ok': False, 'error': '缺少 job_id'}, 400
    cur = conn.cursor()
    row = cur.execute('SELECT * FROM jobs WHERE id=?', (jid,)).fetchone()
    if not row:
        return {'ok': False, 'error': '岗位卡片不存在'}, 404
    extra = {}
    try:
        extra = json.loads(row['extra'] or '{}')
    except Exception:
        pass
    if not extra.get('viaPlugin'):
        return {'ok': False, 'error': '该卡片不是插件创建的，为避免误删请在工作台里手动删除'}, 400
    extra['revoked'] = True
    extra['revokedAt'] = now_str()
    cur.execute('UPDATE jobs SET extra=?, updated_at=? WHERE id=?',
                (json.dumps(extra, ensure_ascii=False), now_str(), jid))
    conn.commit()
    return {'ok': True, 'job_id': jid, 'revoked': True}, 200


# ============================================================
# HTTP Handler
# ============================================================
class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=RES_DIR, **kw)   # 前端静态文件在资源目录

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        try:
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            # 浏览器插件的 service worker 以 chrome-extension:// 为 Origin，
            # 从 MV3 起这个跨源请求不做放行就拿不到响应体。
            # 服务只监听 127.0.0.1，且数据全部在本机，放行本机来源是安全的。
            self._cors()
            self.end_headers()
            self.wfile.write(body)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            # 客户端（多为插件侧超时/用户取消）在响应写完前就挂了连接。
            # 这是正常现象：算已经算完了，只是没人接。不该打成 500 + 堆栈，
            # 那会把日志刷花、淹没真正的错误。
            pass

    def _cors(self):
        origin = self.headers.get('Origin') or ''
        if origin.startswith('chrome-extension://') or origin.startswith('moz-extension://'):
            self.send_header('Access-Control-Allow-Origin', origin)
        else:
            self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')

    def end_headers(self):
        # 静态资源（content.css / job-parser.js 等被插件拉取的资源）同样需要放行
        if getattr(self, '_cors_sent', False) is False and self.command == 'GET' and \
                self.path.startswith('/') and 'api' not in self.path:
            try:
                self._cors()
            except Exception:
                pass
        self._cors_sent = True
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header('Content-Length', '0')
        self._cors()
        self.end_headers()

    def _body(self):
        n = int(self.headers.get('Content-Length') or 0)
        if not n:
            return {}
        return json.loads(self.rfile.read(n).decode('utf-8'))

    def log_message(self, fmt, *args):
        msg = fmt % args
        if '/api/' in msg:
            sys.stdout.write('  %s %s\n' % (now_str(), msg))
            sys.stdout.flush()

    def log_error(self, fmt, *args):
        # SimpleHTTPRequestHandler 默认把每个请求异常都打出来。客户端提前断开
        # （插件超时、用户取消、刷新页面）会触发 ConnectionAbortedError，
        # 这是正常现象而不是故障 —— 静默掉，别用几百行堆栈淹掉真正的错误。
        msg = fmt % args
        if 'ConnectionAbortedError' in msg or 'ConnectionResetError' in msg \
                or 'BrokenPipeError' in msg or 'WinError 10053' in msg:
            return
        super().log_error(fmt, *args)

    def do_GET(self):
        path = self.path.split('?')[0]
        qs = self.path.split('?')[1] if '?' in self.path else ''

        if path == '/api/all':
            with _lock:
                maybe_backup()
                conn = connect()
                try:
                    data = read_all(conn)
                finally:
                    conn.close()
            data['db'] = {'path': DB_PATH,
                          'size': os.path.getsize(DB_PATH)
                                  if os.path.exists(DB_PATH) else 0,
                          'updated': now_str()}
            return self._json(data)

        if path == '/api/candidates':
            # URL 解码查询参数
            params = {}
            for x in qs.split('&'):
                if '=' in x:
                    k, v = x.split('=', 1)
                    params[urllib.request.unquote(k)] = urllib.request.unquote(v)
            status = params.get('status', '')
            city = params.get('city', '')
            source = params.get('source', '')
            category = params.get('category', '')
            match_level = params.get('match_level', '')
            education = params.get('education', '')
            company = params.get('company', '')
            keyword = params.get('keyword', '')
            with _lock:
                conn = connect()
                try:
                    sql = 'SELECT * FROM candidates WHERE 1=1'
                    binds = []
                    if status:
                        sql += ' AND status=?'
                        binds.append(status)
                    if city:
                        sql += ' AND city LIKE ?'
                        binds.append('%%%s%%' % city)
                    if source:
                        sql += ' AND source=?'
                        binds.append(source)
                    if category:
                        sql += ' AND category=?'
                        binds.append(category)
                    if education:
                        sql += ' AND education LIKE ?'
                        binds.append('%%%s%%' % education)
                    if company:
                        sql += ' AND company LIKE ?'
                        binds.append('%%%s%%' % company)
                    if keyword:
                        sql += ' AND (title LIKE ? OR company LIKE ?)'
                        binds.extend(['%%%s%%' % keyword, '%%%s%%' % keyword])
                    if match_level:
                        if match_level == 'none':
                            # 匹配 match_level 为 null 或不存在的情况
                            sql += " AND (extra IS NULL OR extra = '' OR extra NOT LIKE '%match_level%')"
                        else:
                            sql += " AND extra LIKE ?"
                            binds.append('%"match_level": "' + match_level + '"%')
                    sql += ' ORDER BY scraped_at DESC'
                    rows = conn.execute(sql, binds).fetchall()
                    items = []
                    for r in rows:
                        d = row_to_dict(r, CAND_COLS)
                        if r['extra']:
                            try:
                                d.update(json.loads(r['extra']))
                            except Exception:
                                pass
                        items.append(d)
                    # 统计（基于当前筛选条件）
                    count_sql = sql.replace('SELECT *', 'SELECT COUNT(*)')
                    total = conn.execute(count_sql, binds).fetchone()[0]
                    # 各匹配级别统计（单独查询，不受筛选影响）
                    target_count = conn.execute("SELECT COUNT(*) FROM candidates WHERE extra LIKE '%\"match_level\": \"target\"%'").fetchone()[0]
                    vehicle_count = conn.execute("SELECT COUNT(*) FROM candidates WHERE extra LIKE '%\"match_level\": \"vehicle\"%'").fetchone()[0]
                    edu_match_count = conn.execute("SELECT COUNT(*) FROM candidates WHERE extra LIKE '%\"match_level\": \"education\"%'").fetchone()[0]
                    stats = {
                        'total': conn.execute('SELECT COUNT(*) FROM candidates').fetchone()[0],
                        'pending': conn.execute("SELECT COUNT(*) FROM candidates WHERE status='pending'").fetchone()[0],
                        'included': conn.execute("SELECT COUNT(*) FROM candidates WHERE status='included'").fetchone()[0],
                        'excluded': conn.execute("SELECT COUNT(*) FROM candidates WHERE status='excluded'").fetchone()[0],
                        'filtered': total,
                        # 各匹配级别统计
                        'target': target_count,
                        'vehicle': vehicle_count,
                        'education_match': edu_match_count,
                        # 各学历统计
                        'edu_bachelor': conn.execute("SELECT COUNT(*) FROM candidates WHERE education LIKE '%本科%'").fetchone()[0],
                        'edu_master': conn.execute("SELECT COUNT(*) FROM candidates WHERE education LIKE '%硕士%'").fetchone()[0],
                        'edu_phd': conn.execute("SELECT COUNT(*) FROM candidates WHERE education LIKE '%博士%'").fetchone()[0],
                    }
                finally:
                    conn.close()
            return self._json({'items': items, 'stats': stats})

        if path == '/api/tags':
            with _lock:
                conn = connect()
                try:
                    rows = conn.execute('SELECT * FROM tags ORDER BY name').fetchall()
                    tags = [{
                        'id': r['id'], 'name': r['name'], 'color': r['color'] or '#6b7280',
                        'kind': r['kind'] or 'normal',
                        'ruleType': r['rule_type'] or '',
                        'ruleValue': r['rule_value'] or 0,
                    } for r in rows]
                finally:
                    conn.close()
            return self._json({'tags': tags, 'ruleTypes': RULE_TYPES})

        if path == '/api/autofill':
            params = {}
            for x in qs.split('&'):
                if '=' in x:
                    k, v = x.split('=', 1)
                    params[urllib.request.unquote(k)] = urllib.request.unquote(v)
            url = params.get('url', '')
            if not url:
                return self._json({'ok': False, 'error': 'url 参数缺失'}, 400)
            try:
                req = urllib.request.Request(url, headers={
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                    'Accept': 'text/html,application/xhtml+xml',
                })
                with urllib.request.urlopen(req, timeout=10) as resp:
                    html = resp.read().decode('utf-8', errors='ignore')
                # 简单提取标题和描述
                import re
                title_m = re.search(r'<title[^>]*>(.*?)</title>', html, re.S|re.I)
                desc_m = re.search(r'<meta[^>]*name=["\']description["\'][^>]*content=["\'](.*?)["\']', html, re.S|re.I)
                if not desc_m:
                    desc_m = re.search(r'<meta[^>]*content=["\'](.*?)["\'][^>]*name=["\']description["\']', html, re.S|re.I)
                result = {
                    'title': (title_m.group(1).strip() if title_m else ''),
                    'description': (desc_m.group(1).strip() if desc_m else ''),
                    'url': url,
                }
                return self._json({'ok': True, **result})
            except Exception as e:
                return self._json({'ok': False, 'error': '抓取失败: %s' % str(e)}, 500)

        if path == '/api/health':
            return self._json({'ok': True, 'app': APP_ID, 'db': DB_PATH})

        if path == '/api/ai/config':
            # AI 补全配置（Key 打码回传）
            try:
                import ai_extract
                with _lock:
                    conn = connect()
                    try:
                        cfg = ai_extract.load_config(conn)
                    finally:
                        conn.close()
                return self._json({'ok': True, 'config': ai_extract.public_config(cfg)})
            except Exception as e:
                return self._json({'ok': False, 'error': str(e)}, 500)

        if path == '/api/campus-nav':
            # 校招快捷导航：读取同目录 campus_nav.json（由 秋招调研 生成，只读）
            fp = os.path.join(DATA_DIR, 'campus_nav.json')  # 用户自备数据，跟着数据库走
            if not os.path.exists(fp):
                return self._json({'ok': False, 'error': 'campus_nav.json 不存在'}, 404)
            try:
                with open(fp, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            except Exception as e:
                return self._json({'ok': False, 'error': '读取失败: %s' % str(e)}, 500)
            data['ok'] = True
            data['file'] = {'path': fp, 'size': os.path.getsize(fp),
                            'mtime': datetime.fromtimestamp(os.path.getmtime(fp)).strftime('%Y-%m-%d %H:%M:%S')}
            return self._json(data)

        if path == '/api/talks':
            # 宣讲会日程：读取同目录 talks.json（由 招聘信息监控推送/build_talk_schedule.py 生成，只读）
            fp = os.path.join(DATA_DIR, 'talks.json')       # 同上
            if not os.path.exists(fp):
                return self._json({'ok': False, 'error': 'talks.json 不存在，请先运行 build_talk_schedule.py'}, 404)
            try:
                with open(fp, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            except Exception as e:
                return self._json({'ok': False, 'error': '读取失败: %s' % str(e)}, 500)
            data['ok'] = True
            data['file'] = {'path': fp, 'size': os.path.getsize(fp),
                            'mtime': datetime.fromtimestamp(os.path.getmtime(fp)).strftime('%Y-%m-%d %H:%M:%S')}
            return self._json(data)

        if path == '/api/backup/list':
            backups = []
            if os.path.exists(BACKUP_DIR):
                for f in sorted(os.listdir(BACKUP_DIR), reverse=True):
                    if f.endswith('.db'):
                        fp = os.path.join(BACKUP_DIR, f)
                        size = os.path.getsize(fp)
                        mtime = datetime.fromtimestamp(os.path.getmtime(fp)).strftime('%Y-%m-%d %H:%M:%S')
                        backups.append({'name': f, 'size': size, 'mtime': mtime})
            # 也检查当前数据库信息
            db_size = os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0
            db_mtime = datetime.fromtimestamp(os.path.getmtime(DB_PATH)).strftime('%Y-%m-%d %H:%M:%S') if os.path.exists(DB_PATH) else ''
            return self._json({'backups': backups, 'current': {'size': db_size, 'mtime': db_mtime, 'path': DB_PATH}})

        if path in ('/', '/index.html'):
            self.path = '/index.html'
        return super().do_GET()

    def do_POST(self):
        path = self.path.split('?')[0]
        try:
            data = self._body()
        except Exception as e:
            return self._json({'ok': False, 'error': 'bad json: %s' % e}, 400)

        with _lock:
            maybe_backup()
            conn = connect()
            try:
                if path == '/api/state':
                    write_all(conn, data)
                    return self._json({'ok': True, 'updated': now_str()})

                if path == '/api/meta':
                    for k, v in (data or {}).items():
                        set_meta(conn, k, v)
                    return self._json({'ok': True})

                if path == '/api/import':
                    write_all(conn, data)
                    set_meta(conn, 'seeded', True)
                    return self._json({'ok': True, 'imported': {
                        'cards': len(data.get('cards') or []),
                        'jobs': len(data.get('jobs') or []),
                        'apps': len(data.get('apps') or []),
                        'events': len(data.get('events') or [])}})

                if path == '/api/reset':
                    conn.executescript(
                        'DELETE FROM profile;DELETE FROM cards;DELETE FROM jobs;'
                        'DELETE FROM apps;DELETE FROM events;DELETE FROM meta;')
                    conn.commit()
                    return self._json({'ok': True})

                if path == '/api/scrape':
                    result = do_scrape(conn)
                    return self._json({'ok': True, **result})

                if path == '/api/jobs/capture':
                    # 浏览器插件通道：只 upsert 单条 jobs（+ 可选 candidates），
                    # 不触发 write_all 的整库覆写，因此工作台页面开着也安全。
                    res, code = capture_job(conn, data or {})
                    return self._json(res, code)

                if path == '/api/jobs/uncapture':
                    res, code = uncapture_job(conn, data or {})
                    return self._json(res, code)

                if path == '/api/ai/complete':
                    # 插件把规则抽取结果 + 页面精简文本送来，工作台代调大模型。
                    # API Key 只在本机，插件侧永远拿不到。
                    try:
                        import ai_extract
                        res, code = ai_extract.complete(conn, data or {})
                        return self._json(res, code)
                    except Exception as e:
                        return self._json({'ok': False, 'error': 'AI 补全异常: %s' % str(e)}, 500)

                if path == '/api/ai/insight':
                    # 预览界面的「公司与岗位解读」：企业底细 + 人才画像 + 求职建议。
                    # 走的是同一个模型配置，Key 同样只在本机。
                    try:
                        import ai_extract
                        res, code = ai_extract.insight(conn, data or {})
                        return self._json(res, code)
                    except Exception as e:
                        return self._json({'ok': False, 'error': '岗位解读失败: %s' % str(e)}, 500)

                if path == '/api/ai/filter':
                    # 列表页筛选：这批岗位值不值得点开。求职画像只在服务端，
                    # 所以判断必须在这里做，插件只负责把岗位行抽出来送来。
                    try:
                        import ai_extract
                        res, code = ai_extract.filter_jobs(conn, data or {})
                        return self._json(res, code)
                    except Exception as e:
                        return self._json({'ok': False, 'error': '列表筛选失败: %s' % str(e)}, 500)

                if path == '/api/ai/config':
                    try:
                        import ai_extract
                        res, code = ai_extract.save_config(conn, data or {})
                        return self._json(res, code)
                    except Exception as e:
                        return self._json({'ok': False, 'error': str(e)}, 500)

                if path == '/api/ai/test':
                    try:
                        import ai_extract
                        res, code = ai_extract.test_connection(conn)
                        return self._json(res, code)
                    except Exception as e:
                        return self._json({'ok': False, 'error': '测试失败: %s' % str(e)}, 500)

                if path == '/api/candidates/action':
                    action = data.get('action', '')  # 'include' | 'exclude' | 'reset'
                    ids = data.get('ids', [])
                    to_app = data.get('toApp', False)   # 纳入投递计划
                    to_event = data.get('toEvent', False)  # 加入日程
                    now = now_str()
                    cur = conn.cursor()
                    updated = 0
                    job_created = 0
                    app_created = 0
                    for cid in ids:
                        if action == 'reset':
                            status = 'pending'
                        else:
                            status = 'included' if action == 'include' else 'excluded'
                        cur.execute('UPDATE candidates SET status=?, processed_at=? WHERE id=?',
                                    (status, now, cid))
                        if cur.rowcount:
                            updated += 1
                        if action == 'include':
                            row = conn.execute('SELECT * FROM candidates WHERE id=?', (cid,)).fetchone()
                            if row:
                                # 1. 先创建岗位卡片（jobs 表）
                                jid = 'job_%s' % cid
                                exists_job = cur.execute('SELECT 1 FROM jobs WHERE id=?', (jid,)).fetchone()
                                if not exists_job:
                                    # 从 extra 中提取 match_level 作为 tag
                                    extra_data = {}
                                    try:
                                        extra_data = json.loads(row['extra'] or '{}')
                                    except Exception:
                                        pass
                                    match_level = extra_data.get('match_level', '')
                                    tags = []
                                    if match_level == 'target':
                                        tags.append('重点企业')
                                    elif match_level == 'vehicle':
                                        tags.append('车辆相关')
                                    elif match_level == 'education':
                                        tags.append('学历匹配')
                                    # 从 category 生成 tag
                                    if row['category']:
                                        tags.append(row['category'])
                                    # 插件抓来的候选：把来源与抓取字段一并带进岗位卡，
                                    # 否则用户在候选池点「纳入」后，城市/薪资/学历就丢了
                                    job_extra = {'candidate_id': cid, 'city': row['city'],
                                                 'education': row['education'], 'source': row['source'],
                                                 'publish_date': row['publish_date']}
                                    if row['salary']:
                                        job_extra['salary'] = row['salary']
                                    if row['experience']:
                                        job_extra['experience'] = row['experience']
                                    for k in ('岗位来源', '来源域名', '岗位性质', '截止时间',
                                              '部门', '页面适配器', '置信度', '批次', '抓取时间'):
                                        if extra_data.get(k) not in (None, ''):
                                            job_extra[k] = extra_data[k]
                                    if extra_data.get('岗位来源') == '浏览器插件':
                                        job_extra['source'] = '浏览器插件'
                                        if '浏览器插件' not in tags:
                                            tags.append('浏览器插件')
                                    # 新卡排到最前 + 录入时间戳（与插件 capture 通道同一口径）
                                    job_extra['sortOrder'] = _min_sort_order(cur) - 1
                                    job_extra['createdAt'] = now
                                    cur.execute(
                                        'INSERT INTO jobs(id,company,title,jd,link,importance,tags,sample,extra,created_at,updated_at)'
                                        ' VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                                        (jid, row['company'], row['title'],
                                         '',  # jd 原文
                                         row['url'] or '',
                                         'can',  # 默认重要程度：可投
                                         json.dumps(tags, ensure_ascii=False),
                                         0,
                                         json.dumps(job_extra, ensure_ascii=False),
                                         now, now))
                                    job_created += 1
                                # 2. 如果需要，创建投递计划（apps 表）
                                if to_app:
                                    aid = 'app_%s' % cid
                                    exists_app = cur.execute('SELECT 1 FROM apps WHERE id=?', (aid,)).fetchone()
                                    if not exists_app:
                                        cur.execute(
                                            'INSERT INTO apps(id,jobId,company,position,planDate,appliedDate,status,sample,updated_at)'
                                            ' VALUES(?,?,?,?,?,?,?,?,?)',
                                            (aid, jid, row['company'], row['title'],
                                             now[:10], '', 'plan', 0, now))
                                        app_created += 1
                                # 3. 如果需要，创建日程事件
                                if to_event:
                                    eid = 'evt_%s' % cid
                                    cur.execute(
                                        'INSERT OR REPLACE INTO events(id,title,type,date,time,jobId,remindVal,remindUnit,sample,updated_at)'
                                        ' VALUES(?,?,?,?,?,?,?,?,?,?)',
                                        (eid, row['title'][:50], 'reminder',
                                         row['fair_date'] or now[:10], '09:00',
                                         jid, 1, 'days', 0, now))
                    conn.commit()
                    return self._json({'ok': True, 'updated': updated,
                                       'job_created': job_created, 'app_created': app_created})

                if path == '/api/candidates/import':
                    items = data.get('items', [])
                    now = now_str()
                    cur = conn.cursor()
                    imported = 0
                    skipped = 0
                    for item in items:
                        cid = item.get('id', '')
                        if not cid:
                            cid = 'imp_%s_%s' % (item.get('company', ''), item.get('title', '')[:20])
                            import hashlib
                            cid = 'imp_' + hashlib.md5(('%s%s' % (item.get('company', ''), item.get('title', ''))).encode()).hexdigest()[:12]
                        exists = cur.execute('SELECT 1 FROM candidates WHERE id=?', (cid,)).fetchone()
                        if exists:
                            skipped += 1
                            continue
                        cur.execute(
                            'INSERT INTO candidates(id,title,company,city,salary,education,experience,'
                            'source,publish_date,url,category,fair_date,fair_location,status,scraped_at,extra)'
                            ' VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                            (cid, item.get('title', ''), item.get('company', ''),
                             item.get('city', ''), item.get('salary', ''),
                             item.get('education', ''), item.get('experience', ''),
                             item.get('source', '手动导入'), item.get('publish_date', ''),
                             item.get('url', ''), item.get('category', '手动导入'),
                             item.get('fair_date', ''), item.get('fair_location', ''),
                             'pending', now,
                             json.dumps(item.get('extra', {}), ensure_ascii=False)))
                        imported += 1
                    conn.commit()
                    return self._json({'ok': True, 'imported': imported, 'skipped': skipped})

                if path == '/api/tags':
                    action = data.get('action', '')
                    now = now_str()
                    cur = conn.cursor()

                    def _rule_fields(payload):
                        """解析并校验规则字段，非法时抛 ValueError。"""
                        kind = (payload.get('kind') or 'normal').strip()
                        rtype = (payload.get('ruleType') or '').strip()
                        try:
                            rval = int(payload.get('ruleValue', 0) or 0)
                        except (TypeError, ValueError):
                            rval = 0
                        if kind != 'rule':
                            return 'normal', '', 0
                        if rtype not in RULE_TYPES:
                            raise ValueError('请选择规则类型')
                        if rval <= 0:
                            raise ValueError('规则阈值需为正整数')
                        return 'rule', rtype, rval

                    if action == 'create':
                        name = data.get('name', '').strip()
                        color = data.get('color', '#6b7280').strip()
                        if not name:
                            return self._json({'ok': False, 'error': '标签名不能为空'}, 400)
                        try:
                            kind, rtype, rval = _rule_fields(data)
                        except ValueError as e:
                            return self._json({'ok': False, 'error': str(e)}, 400)
                        try:
                            cur.execute('INSERT INTO tags(name,color,kind,rule_type,rule_value,created_at)'
                                        ' VALUES(?,?,?,?,?,?)', (name, color, kind, rtype, rval, now))
                            conn.commit()
                            return self._json({'ok': True, 'id': cur.lastrowid, 'name': name,
                                               'color': color, 'kind': kind,
                                               'ruleType': rtype, 'ruleValue': rval})
                        except Exception:
                            return self._json({'ok': False, 'error': '标签名已存在'}, 400)
                    elif action == 'update':
                        tid = data.get('id')
                        name = data.get('name', '').strip()
                        color = data.get('color', '').strip()
                        if not tid:
                            return self._json({'ok': False, 'error': '缺少 id'}, 400)
                        if 'kind' in data:
                            try:
                                kind, rtype, rval = _rule_fields(data)
                            except ValueError as e:
                                return self._json({'ok': False, 'error': str(e)}, 400)
                            cur.execute('UPDATE tags SET kind=?,rule_type=?,rule_value=? WHERE id=?',
                                        (kind, rtype, rval, tid))
                        if name:
                            cur.execute('UPDATE tags SET name=? WHERE id=?', (name, tid))
                        if color:
                            cur.execute('UPDATE tags SET color=? WHERE id=?', (color, tid))
                        conn.commit()
                        return self._json({'ok': True})
                    elif action == 'delete':
                        tid = data.get('id')
                        if not tid:
                            return self._json({'ok': False, 'error': '缺少 id'}, 400)
                        cur.execute('DELETE FROM tags WHERE id=?', (tid,))
                        conn.commit()
                        return self._json({'ok': True})
                    return self._json({'ok': False, 'error': '未知 action'}, 400)

                if path == '/api/cards/copy':
                    src_id = data.get('id', '')
                    if not src_id:
                        return self._json({'ok': False, 'error': '缺少源卡片 id'}, 400)
                    row = conn.execute('SELECT * FROM cards WHERE id=?', (src_id,)).fetchone()
                    if not row:
                        return self._json({'ok': False, 'error': '源卡片不存在'}, 404)
                    new_id = 'card_' + now_str().replace('-', '').replace(' ', '').replace(':', '')[:14]
                    conn.execute(
                        'INSERT INTO cards(id,name,type,start,"end",resume,story,sample,extra,updated_at)'
                        ' VALUES(?,?,?,?,?,?,?,?,?,?)',
                        (new_id, row['name'] + '（副本）', row['type'], row['start'], row['end'],
                         row['resume'], row['story'], 0, row['extra'] or '{}', now_str()))
                    conn.commit()
                    return self._json({'ok': True, 'new_id': new_id, 'name': row['name'] + '（副本）'})

                if path == '/api/backup/create':
                    # 手动创建备份
                    now = now_str()
                    backup_name = 'backup_' + now.replace('-', '').replace(' ', '_').replace(':', '') + '.db'
                    backup_path = os.path.join(BACKUP_DIR, backup_name)
                    try:
                        os.makedirs(BACKUP_DIR, exist_ok=True)
                        # 使用 SQLite backup API 安全复制
                        src = sqlite3.connect(DB_PATH)
                        src.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                        dst = sqlite3.connect(backup_path)
                        src.backup(dst)
                        dst.close()
                        src.close()
                        size = os.path.getsize(backup_path)
                        return self._json({'ok': True, 'name': backup_name, 'size': size, 'time': now})
                    except Exception as e:
                        return self._json({'ok': False, 'error': '备份失败: %s' % str(e)}, 500)

                if path == '/api/backup/restore':
                    backup_name = data.get('name', '')
                    if not backup_name:
                        return self._json({'ok': False, 'error': '缺少备份文件名'}, 400)
                    backup_path = os.path.join(BACKUP_DIR, backup_name)
                    if not os.path.exists(backup_path):
                        return self._json({'ok': False, 'error': '备份文件不存在'}, 404)
                    try:
                        # 先创建当前数据库的备份（以防恢复失败）
                        safety_name = 'pre_restore_' + now_str().replace('-', '').replace(' ', '_').replace(':', '') + '.db'
                        safety_path = os.path.join(BACKUP_DIR, safety_name)
                        src = sqlite3.connect(DB_PATH)
                        src.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                        dst = sqlite3.connect(safety_path)
                        src.backup(dst)
                        dst.close()
                        src.close()
                        # 从备份恢复
                        src = sqlite3.connect(backup_path)
                        dst = sqlite3.connect(DB_PATH)
                        src.backup(dst)
                        dst.close()
                        src.close()
                        return self._json({'ok': True, 'restored': backup_name, 'safety_backup': safety_name})
                    except Exception as e:
                        return self._json({'ok': False, 'error': '恢复失败: %s' % str(e)}, 500)

            except Exception as e:
                import traceback
                traceback.print_exc()
                return self._json({'ok': False, 'error': str(e)}, 500)
            finally:
                conn.close()
        return self._json({'ok': False, 'error': 'not found'}, 404)


# ============================================================
# 启动
# ============================================================
def pick_port(start):
    import socket
    port = start
    for _ in range(20):
        with socket.socket() as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(('127.0.0.1', port))
                return port
            except OSError:
                port += 1
    return None


def ensure_data():
    """首次运行：把内置示例库复制到数据目录。

    只有打包版才有内置库（源码运行时 DB_PATH 和 BUNDLED_DB 是同一个文件，
    不能也不需要复制）。已经存在 data.db 就一律不碰。
    """
    if os.path.exists(DB_PATH) or not os.path.exists(BUNDLED_DB):
        return
    if os.path.abspath(BUNDLED_DB) == os.path.abspath(DB_PATH):
        return
    try:
        shutil.copy2(BUNDLED_DB, DB_PATH)
        print('[init] 已从内置示例库初始化：%s' % DB_PATH)
    except Exception as e:
        print('[warn] 初始化示例库失败，将建空库：%s' % e)


def detect_running(start=DEFAULT_PORT, tries=20):
    """在 start 起的端口范围里找已经跑着的本工作台。

    先看端口有没有人监听，没人在听就直接跳过 —— 这样正常情况几乎不耗时，
    只有真被占用的那个端口才值得发一次 HTTP 问话。
    """
    import socket
    for port in range(start, start + tries):
        with socket.socket() as s:
            s.settimeout(0.25)
            if s.connect_ex(('127.0.0.1', port)) != 0:
                continue
        try:
            with urllib.request.urlopen(
                    'http://127.0.0.1:%d/api/health' % port, timeout=0.8) as r:
                data = json.loads(r.read().decode('utf-8'))
            # app 字段是新加的，老实例只有 db 字段，两种都认
            if data.get('app') == APP_ID or 'db' in data:
                return 'http://127.0.0.1:%d/' % port
        except Exception:
            continue   # 端口被别的程序占着，不是我们的人
    return None


def main():
    _setup_console()
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=DEFAULT_PORT)
    ap.add_argument('--no-open', action='store_true')
    ap.add_argument('--allow-multi', action='store_true',
                    help='已经有工作台在跑时也强制再起一个')
    args = ap.parse_args()

    if not args.allow_multi:
        running = detect_running(args.port)
        if running:
            print('已经有一个工作台在运行：%s' % running)
            print('（想再开一个用 --allow-multi）')
            if not args.no_open:
                webbrowser.open(running)
            return

    ensure_data()
    init_db()
    port = pick_port(args.port)
    if port is None:
        print('[error] 端口 %d 及后续 20 个端口均被占用' % args.port)
        sys.exit(1)

    url = 'http://127.0.0.1:%d/' % port
    print('=' * 52)
    print('  个人求职工作台 · 本地服务已启动')
    print('  访问地址 : %s' % url)
    print('  数据库   : %s' % DB_PATH)
    print('  自动备份 : %s' % BACKUP_DIR)
    print('  关闭窗口即停止服务')
    print('=' * 52)

    if not args.no_open:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    try:
        ThreadingHTTPServer(('127.0.0.1', port), Handler).serve_forever()
    except KeyboardInterrupt:
        print('\n服务已停止')


if __name__ == '__main__':
    main()
