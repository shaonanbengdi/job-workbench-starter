# -*- coding: utf-8 -*-
"""岗位信息 AI 补全模块（求职工作台侧）。

职责：
  接收浏览器插件用规则抽取后的「半成品结果」（含页面精简文本），
  调大模型把缺失/低置信度的字段补齐，返回结构化 JSON。

设计要点：
  1. 零第三方依赖 —— 只用标准库 urllib，跟工作台其余部分保持一致。
  2. Provider 可切 —— 智谱 / 通义 / 月之暗面 / DeepSeek / OpenAI 兼容 / 本地 Ollama，
     都由统一配置驱动，不写死。
  3. API Key 只存本机 data.db 的 meta 表，**绝不进插件、绝不上传**。
  4. 只补不覆盖 —— 插件规则已经高置信度拿到的字段，AI 不许改。
  5. 失败静默降级 —— AI 挂了、超时了、返回垃圾，都不能影响用户正常抓取。

配置项（存在 meta 表，键名前缀 ai_）：
  ai_enabled     '1' / '0'
  ai_provider    'zhipu' | 'dashscope' | 'moonshot' | 'deepseek' | 'openai' | 'ollama' | 'custom'
  ai_api_key     密钥
  ai_base_url    覆盖默认 base（custom/ollama 用）
  ai_model       模型名
  ai_timeout     秒
"""

import json
import re
import urllib.error
import urllib.request

# ============================================================
# Provider 预设
# ============================================================

PROVIDERS = {
    'zhipu': {
        'name': '智谱 GLM',
        'base': 'https://open.bigmodel.cn/api/paas/v4/chat/completions',
        'model': 'glm-4-flash',
        'doc': 'https://open.bigmodel.cn/  →  API Keys',
        'note': 'glm-4-flash 免费额度大、中文强，推荐先用这个',
        'auth': 'bearer',
    },
    'dashscope': {
        'name': '阿里通义千问',
        'base': 'https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions',
        'model': 'qwen-plus',
        'doc': 'https://bailian.console.aliyun.com/  →  API-KEY',
        'note': 'openai 兼容模式；qwen-turbo 更便宜，qwen-plus 更准',
        'auth': 'bearer',
    },
    'moonshot': {
        'name': '月之暗面 Kimi',
        'base': 'https://api.moonshot.cn/v1/chat/completions',
        'model': 'moonshot-v1-8k',
        'doc': 'https://platform.moonshot.cn/console/api-keys',
        'note': '长文本友好，JD 特别长时表现好',
        'auth': 'bearer',
    },
    'deepseek': {
        'name': 'DeepSeek',
        'base': 'https://api.deepseek.com/chat/completions',
        'model': 'deepseek-chat',
        'doc': 'https://platform.deepseek.com/api_keys',
        'note': '便宜、中文好、结构化输出稳',
        'auth': 'bearer',
    },
    'openai': {
        'name': 'OpenAI 兼容接口',
        'base': 'https://api.openai.com/v1/chat/completions',
        'model': 'gpt-4o-mini',
        'doc': '任何 OpenAI 兼容的第三方中转都可以填这里',
        'note': '改 base_url 即可指向中转',
        'auth': 'bearer',
    },
    'ollama': {
        'name': '本地 Ollama',
        'base': 'http://127.0.0.1:11434/v1/chat/completions',
        'model': 'qwen2.5:7b',
        'doc': 'https://ollama.com/  本机跑，免费、不联网',
        'note': '需要先 ollama pull qwen2.5:7b；不填 Key',
        'auth': 'none',
    },
    'custom': {
        'name': '自定义',
        'base': '',
        'model': '',
        'doc': '填完整的 chat/completions 地址',
        'note': '任何 OpenAI 兼容端点',
        'auth': 'bearer',
    },
}

# 允许 AI 补的字段（与插件 job-parser 的字段名一致）
AI_FIELDS = [
    'title', 'company', 'salary', 'city', 'education',
    'experience', 'deadline', 'department', 'headcount',
    'jobType', 'link', 'jd',
]

# 字段中文名，喂给模型时用它，模型更容易对齐
FIELD_CN = {
    'title': '岗位名称',
    'company': '公司名称',
    'salary': '薪资',
    'city': '工作地点',
    'education': '学历要求',
    'experience': '经验要求',
    'deadline': '投递截止时间',
    'department': '所属部门',
    'headcount': '招聘人数',
    'jobType': '岗位性质（校园招聘/社会招聘/实习）',
    'link': '投递链接',
    'jd': '岗位描述全文',
}


# ============================================================
# 配置读写
# ============================================================

def load_config(conn):
    """从 meta 表读 AI 配置。"""
    raw = {}
    try:
        for r in conn.execute("SELECT key,value FROM meta WHERE key LIKE 'ai\\_%' ESCAPE '\\'"):
            raw[r['key']] = r['value']
    except Exception:
        return default_config()

    provider = (raw.get('ai_provider') or 'zhipu').strip()
    if provider not in PROVIDERS:
        provider = 'zhipu'
    preset = PROVIDERS[provider]

    cfg = {
        'enabled': raw.get('ai_enabled', '0') == '1',
        'provider': provider,
        'api_key': raw.get('ai_api_key', '') or '',
        'base_url': (raw.get('ai_base_url', '') or '').strip() or preset['base'],
        'model': (raw.get('ai_model', '') or '').strip() or preset['model'],
        'timeout': int(raw.get('ai_timeout') or 40),
        'auth': preset['auth'],
    }
    if cfg['timeout'] < 5:
        cfg['timeout'] = 5
    if cfg['timeout'] > 180:
        cfg['timeout'] = 180
    return cfg


def default_config():
    p = PROVIDERS['zhipu']
    return {
        'enabled': False, 'provider': 'zhipu', 'api_key': '',
        'base_url': p['base'], 'model': p['model'], 'timeout': 40, 'auth': p['auth'],
    }


def save_config(conn, data):
    """写 AI 配置到 meta 表。空字符串表示"清空该项"。"""
    allowed = {'ai_enabled', 'ai_provider', 'ai_api_key', 'ai_base_url',
               'ai_model', 'ai_timeout'}
    n = 0
    for k, v in (data or {}).items():
        if k not in allowed:
            continue
        if k == 'ai_enabled':
            v = '1' if str(v) in ('1', 'true', 'True', 'yes') else '0'
        elif k == 'ai_provider':
            v = str(v).strip()
            if v not in PROVIDERS:
                return {'ok': False, 'error': '未知 provider：' + v}, 400
        elif k == 'ai_timeout':
            try:
                v = str(max(5, min(180, int(v))))
            except Exception:
                return {'ok': False, 'error': 'ai_timeout 必须是整数'}, 400
        else:
            v = '' if v is None else str(v)
        conn.execute('INSERT INTO meta(key,value) VALUES(?,?) '
                     'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (k, v))
        n += 1
    conn.commit()
    return {'ok': True, 'saved': n}, 200


def public_config(cfg):
    """给前端的配置快照——**Key 打码**，只回传末 4 位供辨认。"""
    key = cfg.get('api_key') or ''
    if len(key) > 8:
        masked = key[:4] + '·' * 6 + key[-4:]
    elif key:
        masked = '·' * len(key)
    else:
        masked = ''
    return {
        'enabled': cfg['enabled'],
        'provider': cfg['provider'],
        'model': cfg['model'],
        'base_url': cfg['base_url'],
        'timeout': cfg['timeout'],
        'keyMasked': masked,
        'hasKey': bool(key),
        'providers': {k: {'name': v['name'], 'model': v['model'],
                          'doc': v['doc'], 'note': v['note'],
                          'base': v['base']} for k, v in PROVIDERS.items()},
    }


# ============================================================
# Prompt 构造
# ============================================================

SYSTEM_PROMPT = (
    '你是招聘网页信息抽取器。用户会给你一段从招聘网页抓下来的文本，'
    '以及一份已经用规则抽取好的字段（可能有缺漏或错误）。\n'
    '你的任务：只补全"缺失"或"明显不对"的字段，把结果以 JSON 返回。\n'
    '铁律：\n'
    '1. 只依据给定文本，绝不编造。文本里没有的信息，值必须是空字符串。\n'
    '2. 已经给出且看起来正确的字段，原样返回，不要改写、不要润色、不要翻译。\n'
    '3. 输出必须是单个 JSON 对象，不要 markdown 代码块，不要任何解释文字。\n'
    '4. 所有值都是字符串。\n'
    '5. 若某字段在原文中无法确定，留空字符串，不要猜。\n'
    '6. 关于 jd：只能包含岗位本身的「岗位职责/工作内容」与「任职要求/任职资格」，'
    '原样保留换行与分条编号；网站导航、面包屑、「相关职位」「热门职位」'
    '「猜你喜欢」等推荐位、公司宣传语、页脚版权联系方式一律**不属于 jd，必须剔除**。'
    '找不到明确的职责或要求段落时，jd 留空字符串，不要把整页文本倒进来。'
)


FULL_PROMPT = (
    '你是招聘网页信息抽取器。用户给你一段从招聘网页抓下来的文本，'
    '你要把里面的岗位信息全部抽出来，以 JSON 返回。\n'
    '铁律：\n'
    '1. 只依据给定文本，绝不编造。文本里没有的信息，值必须是空字符串。\n'
    '2. 不要输出任何解释文字，直接输出单个 JSON 对象，不要 markdown 代码块。\n'
    '3. 所有值都是字符串。\n'
    '4. 拿不准的字段留空字符串，不要猜。\n\n'
    '关于 jd 字段（最容易出错，务必看仔细）：\n'
    '  jd 只能包含这个岗位本身的「岗位职责 / 工作内容」与「任职要求 / 任职资格」两部分。\n'
    '  必须原样保留换行与分条编号，不要改写、不要总结、不要翻译。\n'
    '  以下内容一律**不属于 jd，必须剔除**：\n'
    '    · 网站导航、面包屑、登录注册、扫码关注\n'
    '    · 「相关职位」「热门职位」「猜你喜欢」「为您推荐」「相似职位」「其他职位」'
    '「浏览过」「投递过」等推荐位\n'
    '    · 公司简介、福利待遇罗列中的通用宣传语\n'
    '    · 页脚、版权、联系方式、友情链接\n'
    '    · 薪资/学历/经验/地点这类已经在别的字段里抽走的单行信息\n'
    '  如果文本里找不到明确的岗位职责或任职要求段落，jd 就留空字符串，'
    '宁可为空也**不要把整页文本倒进来**。\n\n'
    '字段说明：\n'
    '  title       岗位名称，不要带【急聘】【热门】这类前缀，不要带岗位编号\n'
    '  company     用人企业全称\n'
    '  salary      薪资原文，如"15-25K·14薪"、"面议"\n'
    '  city        工作地点，如"苏州"、"上海·嘉定区"\n'
    '  education   学历要求原文，如"硕士及以上"、"本科"\n'
    '  experience  经验要求原文，如"应届毕业生"、"3年以上"\n'
    '  deadline    投递截止时间，如"2026-11-30"\n'
    '  department  所属部门/事业部\n'
    '  headcount   招聘人数，如"3人"、"若干"\n'
    '  jobType     岗位性质，只能填：校园招聘 / 社会招聘 / 实习\n'
    '  link        本岗位的投递链接（文本里有就给，没有留空）\n'
    '  jd          见上文，只要岗位职责 + 任职要求'
)


def build_full_messages(page_text, meta=None, max_text=14000):
    """全量模式的 messages：不给规则基线，整页文本直接交给模型。"""
    meta = meta or {}
    text = (page_text or '')[:max_text]
    user = (
        '【页面信息】\n'
        '页面标题：%s\nURL：%s\n\n'
        '【页面文本】\n"""\n%s\n"""\n\n'
        '请输出一个 JSON 对象，键固定为：%s。\n'
        '文本里没有的留空字符串。'
    ) % (
        meta.get('title') or '',
        meta.get('url') or '',
        text,
        ', '.join(AI_FIELDS),
    )
    return [
        {'role': 'system', 'content': FULL_PROMPT},
        {'role': 'user', 'content': user},
    ]


def build_messages(capture, page_text, only_fields=None, max_text=12000):
    """构造发给模型的 messages。

    capture   : 插件传来的规则抽取结果 {fields:{...}, meta:{...}, confidence:{...}}
    page_text : 页面精简文本（插件侧已裁过）
    only_fields: 只补这些字段（默认自动挑缺失/低分的）
    """
    fields = (capture or {}).get('fields') or {}
    conf = ((capture or {}).get('confidence') or {}).get('fields') or {}
    meta = (capture or {}).get('meta') or {}

    if not only_fields:
        only_fields = pick_target_fields(fields, conf)

    text = (page_text or '')[:max_text]

    have_lines = []
    for k in AI_FIELDS:
        v = fields.get(k)
        if v is None or v == '':
            continue
        s = str(v)
        if k == 'jd':
            s = s[:600] + ('…（截断）' if len(s) > 600 else '')
        have_lines.append('  %s(%s): %s' % (FIELD_CN.get(k, k), k, s))

    want_lines = ['  %s(%s)' % (FIELD_CN.get(k, k), k) for k in only_fields]

    user = (
        '【页面信息】\n'
        '来源：%s\n页面标题：%s\nURL：%s\n\n'
        '【规则已抽到的字段】\n%s\n\n'
        '【需要你补的字段】\n%s\n\n'
        '【页面文本】\n"""\n%s\n"""\n\n'
        '请只输出一个 JSON 对象，键为上面"需要你补的字段"里的英文键名。'
        '拿不准的留空字符串。'
    ) % (
        meta.get('adapterName') or '通用',
        meta.get('title') or '',
        meta.get('url') or '',
        '\n'.join(have_lines) if have_lines else '  （无）',
        '\n'.join(want_lines),
        text,
    )
    return [
        {'role': 'system', 'content': SYSTEM_PROMPT},
        {'role': 'user', 'content': user},
    ]


def pick_target_fields(fields, conf, low_threshold=60):
    """挑出值得让 AI 补的字段：缺失的 + 低置信度的。

    jd 单独判断：太短（< 200 字）或来源是降级整页正文时也要补。
    """
    targets = []
    for k in AI_FIELDS:
        v = fields.get(k)
        missing = (v is None or str(v).strip() == '')
        score = conf.get(k, 0) or 0
        if missing:
            targets.append(k)
        elif score and score < low_threshold:
            targets.append(k)
        elif k == 'jd':
            if len(str(v)) < 200:
                targets.append(k)
            elif '降级' in str(fields.get('_jdSource') or ''):
                targets.append(k)
    # 顺序按重要度，别让模型漏掉关键字段
    order = ['title', 'company', 'salary', 'city', 'education',
             'experience', 'deadline', 'jobType', 'department', 'headcount', 'link', 'jd']
    return [k for k in order if k in targets]


# ============================================================
# 调用
# ============================================================

def call_model(cfg, messages, temperature=0.1, max_tokens=None):
    """发一次 chat/completions 请求，返回 (content_text, error_msg)。"""
    if not cfg.get('enabled'):
        return None, 'AI 未启用'
    if cfg.get('auth') != 'none' and not cfg.get('api_key'):
        return None, '未配置 API Key'
    if not cfg.get('base_url'):
        return None, '未配置接口地址'

    body = {
        'model': cfg['model'],
        'messages': messages,
        'temperature': temperature,
        'stream': False,
    }
    if max_tokens:
        body['max_tokens'] = max_tokens
    req = urllib.request.Request(
        cfg['base_url'],
        data=json.dumps(body, ensure_ascii=False).encode('utf-8'),
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    if cfg.get('auth') == 'bearer' and cfg.get('api_key'):
        req.add_header('Authorization', 'Bearer ' + cfg['api_key'])

    try:
        with urllib.request.urlopen(req, timeout=cfg.get('timeout', 40)) as resp:
            raw = resp.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        detail = ''
        try:
            detail = e.read().decode('utf-8', 'replace')[:300]
        except Exception:
            pass
        return None, 'HTTP %s %s' % (e.code, detail)
    except urllib.error.URLError as e:
        return None, '连接失败：%s' % (e.reason,)
    except Exception as e:
        return None, '请求异常：%s' % (e,)

    try:
        data = json.loads(raw)
    except Exception:
        return None, '返回不是 JSON：' + raw[:200]

    # 兼容 OpenAI 格式
    try:
        choices = data.get('choices') or []
        if choices:
            msg = choices[0].get('message') or {}
            content = msg.get('content')
            if content:
                return content, None
        # 有些平台把结果放在 output_text
        if data.get('output_text'):
            return data['output_text'], None
        # 出错
        err = data.get('error') or data.get('message')
        if err:
            if isinstance(err, dict):
                err = err.get('message') or json.dumps(err, ensure_ascii=False)
            return None, '接口报错：' + str(err)[:300]
    except Exception as e:
        return None, '解析响应失败：%s' % (e,)
    return None, '响应里没有内容'


JSON_BLOCK = re.compile(r'\{[\s\S]*\}')


def recover_truncated_json(text):
    """模型输出被 max_tokens 截断时的兜底：把已经写完整的 picks 元素逐个救回来。

    截断的 JSON 少一个右括号，json.loads 直接失败，但前面几十条 picks 是完好的 ——
    丢掉它们等于整页白筛（用户得重等四五十秒）。只在文本里出现 "picks" 时启用。
    """
    if not text:
        return None
    key = text.find('"picks"')
    if key < 0:
        return None
    lb = text.find('[', key)
    if lb < 0:
        return None
    dec, pos, items = json.JSONDecoder(), lb + 1, []
    while True:
        while pos < len(text) and text[pos] in ' \t\r\n,':
            pos += 1
        if pos >= len(text) or text[pos] != '{':
            break
        try:
            obj, pos = dec.raw_decode(text, pos)
        except Exception:
            break
        items.append(obj)
    return {'picks': items} if items else None


def parse_json_reply(text):
    """从模型回复里抠出 JSON 对象。容错：去掉 ```json 围栏、截取最外层花括号。"""
    if not text:
        return None
    t = text.strip()
    # 去 markdown 围栏
    t = re.sub(r'^```(?:json)?\s*', '', t)
    t = re.sub(r'\s*```$', '', t)
    try:
        return json.loads(t)
    except Exception:
        pass
    m = JSON_BLOCK.search(t)
    if not m:
        return recover_truncated_json(t)
    try:
        return json.loads(m.group(0))
    except Exception:
        # 常见毛病：尾逗号
        fixed = re.sub(r',\s*([}\]])', r'\1', m.group(0))
        try:
            return json.loads(fixed)
        except Exception:
            return recover_truncated_json(t)


def clean_value(v, field):
    """把模型返回的值洗成插件能用的字符串。"""
    if v is None:
        return ''
    if isinstance(v, (list, tuple)):
        v = '，'.join(str(x) for x in v if x is not None)
    elif isinstance(v, dict):
        v = json.dumps(v, ensure_ascii=False)
    s = str(v).strip()
    # 模型爱写"未知/无/未提供"表示没有
    if s in ('未知', '无', '未提供', '不明确', 'null', 'None', 'N/A', 'n/a', '-', '--'):
        return ''
    # 去掉模型自带的引号
    if len(s) >= 2 and s[0] == s[-1] and s[0] in '"\'':
        s = s[1:-1].strip()
    LIMITS = {'title': 80, 'company': 60, 'salary': 40, 'city': 40,
              'education': 40, 'experience': 40, 'deadline': 40,
              'department': 60, 'headcount': 20, 'jobType': 20,
              'link': 1000, 'jd': 40000}
    return s[:LIMITS.get(field, 200)]


# 服务端 JD 清洗：模型偶尔仍会把导航/推荐位写进 jd，这里再拦一道
#
# 两类噪声行，处理方式不同：
#   · DROP_LINE  —— 自己就是噪声（导航/页脚/「相关职位」这种小标题）→ 删
#   · NOISE_HEAD —— 噪声区的**开头标记**（如"相关职位"），它下面跟着的
#                   岗位名清单也属于噪声 → 删它**以及紧随其后的几行**
JD_DROP_LINE = re.compile(
    r'(浏览过|投递过|注册|登录|立即申请|返回顶部|版权所有|Copyright|联系我们|'
    r'关于我们|网站地图|友情链接|隐私政策|用户协议|扫码|下载APP|关注我们|'
    r'分享到|上一篇|下一篇|职位搜索)',
    re.I,
)
# 命中这些行就进入"噪声区"，直到遇到一个 JD 小标题或空太多才退出
JD_NOISE_HEAD = re.compile(
    r'(相关职位|热门职位|猜你喜欢|为您推荐|相似职位|其他职位|更多职位|'
    r'全部职位|热门推荐|推荐职位|浏览了|看了又看|其他岗位|热门岗位)',
    re.I,
)
# 锚点：遇到它说明噪声区结束、又回到 JD 正文了
JD_RESUME = re.compile(
    r'(岗位职责|工作职责|职位描述|岗位描述|职位职责|工作内容|主要职责|'
    r'任职要求|岗位要求|任职资格|任职条件|职位要求|资格要求|应聘要求)',
)


def sanitize_jd(text):
    """把 JD 里的页眉页脚/推荐位行剔掉。

    逐行处理，维护一个"当前是否在噪声区"的状态：
      · 命中 JD_NOISE_HEAD → 进入噪声区，本行丢弃，后面连续的非 JD 行也丢
      · 噪声区内命中 JD_RESUME（如"任职要求"）→ 退出噪声区，把这行留下
      · 噪声区内出现空行 → 结束噪声区（推荐位清单不会跨空行）
    最后压掉连续空行；删完不足 40 字就当没有，返回空串。
    """
    if not text:
        return ''
    lines = str(text).replace('\r\n', '\n').replace('\r', '\n').split('\n')
    kept = []
    in_noise = False
    for ln in lines:
        stripped = ln.strip()
        if JD_NOISE_HEAD.search(stripped):
            in_noise = True          # 开一段噪声区（本行不保留）
            continue
        if in_noise:
            if not stripped:
                in_noise = False     # 空行 = 噪声区结束
                kept.append('')
                continue
            if JD_RESUME.search(stripped):
                in_noise = False     # 又回到 JD 正文
                kept.append(ln)
                continue
            continue                 # 噪声区内的其他行一律丢
        if JD_DROP_LINE.search(stripped):
            continue
        kept.append(ln)
    out = '\n'.join(kept)
    out = re.sub(r'\n{3,}', '\n\n', out).strip()
    return out if len(out) >= 40 else ''


def clean_record(ai_fields):
    """全量模式用：把模型返回的对象整体洗一遍。

    跟 clean_value 的区别是这里没有"哪些字段被问过"的名单 ——
    全量模式下模型给什么就用什么，所以必须自己按 AI_FIELDS 白名单过滤，
    免得模型自作主张塞进一堆没用的键。
    """
    out = {}
    for k in AI_FIELDS:
        if k not in ai_fields:
            continue
        v = clean_value(ai_fields.get(k), k)
        if not v:
            continue
        if k == 'jd':
            v = sanitize_jd(v)
            if not v:
                continue
        out[k] = v
    return out


def merge_full(capture, ai_fields):
    """全量模式的合并：**AI 说什么就是什么**。

    跟 merge_result 的"只补空缺、不覆盖高分"是两套口径，别混用。
    这里保留规则结果里的元信息（适配器名、host、抓取时间、原链接），
    但字段值全部以 AI 为准；AI 没给的字段如果规则有，也保留 ——
    免得模型漏答一项就把本来识别对的字段清空了。
    返回 (新 capture, 采纳字段列表, 保留的规则字段列表)。
    """
    import copy
    out = copy.deepcopy(capture or {})
    out.setdefault('fields', {})
    out.setdefault('meta', {})
    fields = out['fields']
    conf = (out.get('confidence') or {}).get('fields') or {}

    adopted, kept = [], []
    for k, v in clean_record(ai_fields).items():
        fields[k] = v
        adopted.append(k)
        conf[k] = 85          # AI 全量识别，前端不再标黄；真错了用户自己改
    for k in AI_FIELDS:
        if k in adopted:
            continue
        if fields.get(k):
            kept.append(k)

    out['confidence'] = {'fields': conf}
    out['meta']['aiAssisted'] = True
    out['meta']['aiFields'] = adopted
    out['meta']['aiKept'] = kept
    out['meta']['aiMode'] = 'full'
    return out, adopted, kept


def merge_result(capture, ai_fields, only_fields):
    """把 AI 补的字段合并进 capture。

    关键：**只填空缺和低置信度**，高置信度的规则结果不许被 AI 覆盖。
    返回 (新 capture, 实际采纳的字段列表, 被跳过的字段列表)。
    """
    import copy
    out = copy.deepcopy(capture or {})
    out.setdefault('fields', {})
    conf = ((out.get('confidence') or {}).get('fields')) or {}
    fields = out['fields']

    adopted, skipped = [], []
    for k in only_fields:
        if k not in ai_fields:
            continue
        val = clean_value(ai_fields.get(k), k)
        if not val:
            continue
        cur = fields.get(k)
        cur_empty = (cur is None or str(cur).strip() == '')
        cur_score = conf.get(k, 0) or 0
        # AI 只补空缺或低分项。高分项即使 AI 给了也不覆盖。
        if cur_empty or cur_score < 60:
            fields[k] = val
            adopted.append(k)
        else:
            skipped.append(k)

    # 打了 AI 标记，前端可以显示"AI 补全"
    out.setdefault('meta', {})
    if adopted:
        out['meta']['aiAssisted'] = True
        out['meta']['aiFields'] = adopted
        out['meta']['aiProvider'] = out['meta'].get('aiProvider') or ''
    return out, adopted, skipped


def _cfg_ready(cfg):
    """检查配置是否可用，返回 (ok, 响应字典)。"""
    if not cfg.get('enabled'):
        return False, {'ok': False, 'error': 'AI 未启用', 'code': 'disabled'}
    if cfg.get('auth') != 'none' and not cfg.get('api_key'):
        return False, {'ok': False, 'error': '未配置 API Key', 'code': 'no_key'}
    return True, None


def _norm_text(v):
    if v is None:
        return ''
    if not isinstance(v, str):
        return str(v)
    return v


def complete_full(conn, payload):
    """全量模式：规则不参与，整页文本交模型一次性抽全部字段。

    跟 complete() 的差别在合并口径 —— 这里不判断"这字段是不是已经识别到了"，
    AI 给什么用什么。相应地模型看的文本也更长（默认 14000 字）。
    """
    cfg = load_config(conn)
    ok, bad = _cfg_ready(cfg)
    if not ok:
        return bad, 200

    payload = payload or {}
    page_text = _norm_text(payload.get('pageText'))
    if not page_text.strip():
        return {'ok': False, 'error': '没有可用的页面文本', 'code': 'no_text'}, 400

    try:
        max_text = int(payload.get('maxText') or 14000)
    except Exception:
        max_text = 14000
    max_text = max(2000, min(60000, max_text))

    messages = build_full_messages(page_text, payload.get('meta') or {}, max_text)
    content, err = call_model(cfg, messages)
    if err:
        return {'ok': False, 'error': err, 'code': 'api_error'}, 200

    parsed = parse_json_reply(content)
    if not isinstance(parsed, dict):
        return {'ok': False, 'error': '模型返回没法解析成 JSON',
                'raw': (content or '')[:500], 'code': 'bad_json'}, 200

    if 'fields' in parsed and isinstance(parsed['fields'], dict):
        parsed = parsed['fields']
    # 模型可能用了中文键名
    rev = {v: k for k, v in FIELD_CN.items()}
    if not any(k in parsed for k in AI_FIELDS):
        parsed = {rev.get(kk, kk): vv for kk, vv in parsed.items()}

    cleaned = clean_record(parsed)
    if not cleaned.get('title') and not cleaned.get('company'):
        return {'ok': False, 'code': 'empty',
                'error': 'AI 没能从这页提取出岗位名和企业名（页面可能不是岗位详情页）',
                'raw': (content or '')[:300]}, 200

    merged, adopted, kept = merge_full(payload.get('capture') or {}, parsed)
    return {
        'ok': True,
        'mode': 'full',
        'adopted': adopted,
        'kept': kept,
        'provider': cfg['provider'],
        'model': cfg['model'],
        'capture': merged,
    }, 200


def complete(conn, payload):
    """主入口：给插件调用的补全流程。

    payload: {
      capture: {...},           插件规则结果
      pageText: '...',          页面精简文本
      fields: ['city','salary'],可选，指定只补哪些
      mode: 'full',             可选，全量识别（不走规则基线）
      maxText: 12000            可选
    }
    返回 (响应字典, HTTP 状态码)
    """
    payload = payload or {}
    if str(payload.get('mode') or '') == 'full':
        return complete_full(conn, payload)

    cfg = load_config(conn)
    ok, bad = _cfg_ready(cfg)
    if not ok:
        return bad, 200

    capture = payload.get('capture') or {}
    page_text = _norm_text(payload.get('pageText'))
    if not page_text.strip():
        return {'ok': False, 'error': '没有可用的页面文本', 'code': 'no_text'}, 400

    fields = capture.get('fields') or {}
    conf = (capture.get('confidence') or {}).get('fields') or {}
    only = payload.get('fields') or pick_target_fields(fields, conf)
    if not only:
        return {'ok': True, 'adopted': [], 'skipped': [], 'reason': '没有需要补的字段',
                'capture': capture}, 200

    try:
        max_text = int(payload.get('maxText') or 12000)
    except Exception:
        max_text = 12000
    max_text = max(2000, min(40000, max_text))

    messages = build_messages(capture, page_text, only, max_text)
    content, err = call_model(cfg, messages)
    if err:
        return {'ok': False, 'error': err, 'code': 'api_error'}, 200

    parsed = parse_json_reply(content)
    if not isinstance(parsed, dict):
        return {'ok': False, 'error': '模型返回没法解析成 JSON',
                'raw': (content or '')[:500], 'code': 'bad_json'}, 200

    # 模型可能把结果包在 fields 里
    if 'fields' in parsed and isinstance(parsed['fields'], dict):
        parsed = parsed['fields']
    if not any(k in parsed for k in only):
        # 模型全用了中文键名，反查一次
        rev = {v: k for k, v in FIELD_CN.items()}
        parsed = {rev.get(kk, kk): vv for kk, vv in parsed.items()}

    merged, adopted, skipped = merge_result(capture, parsed, only)
    return {
        'ok': True,
        'adopted': adopted,
        'skipped': skipped,
        'asked': only,
        'provider': cfg['provider'],
        'model': cfg['model'],
        'capture': merged,
    }, 200


# ============================================================
# 公司与岗位解读
# ============================================================
# 插件预览界面的「公司与岗位解读」按钮走这条：拿企业名 + JD，
# 产出三块内容 —— 企业底细 / 这个岗位想要什么样的人 / 求职者该怎么准备。
#
# 跟字段抽取（complete）的区别：
#   抽取要"像复印机"，温度低、不许编；解读要"像顾问"，可以推理、给判断，
#   但**企业信息仍然不许凭空造**，拿不准必须写在 source_note 里让用户知道。

INSIGHT_SYSTEM = (
    '你是一位给求职者做岗位尽调的行业顾问。用户给你企业名称和岗位 JD，'
    '你负责说清三件事：这家公司是什么来头、这个岗位到底想要什么样的人、'
    '求职者该怎么准备。\n'
    '铁律：\n'
    '1. 企业信息只写你有把握的公开常识。涉及融资、营收、排名、人数这类'
    '具体数字，没把握就别写数字，直接说"公开信息有限，建议面试时问 HR"。'
    '编造数字是最严重的错误。\n'
    '2. 不要排比句，不要"平台大""前景好""发展空间广阔"这类空话。'
    '每一条要么落到具体事实，要么落到具体动作。\n'
    '3. 建议必须可执行：写清改什么、说什么、问什么，不要写"提升综合能力"。\n'
    '4. 用中文，输出单个 JSON 对象，不要 markdown 代码块，不要任何解释文字。\n'
)

INSIGHT_SPEC = (
    '输出 JSON，键固定如下（值为字符串或字符串数组）：\n'
    '  company       企业简介，3~5 句，200 字以内：主营什么、在行业里的位置、'
    '性质与规模、跟这个岗位相关的业务盘子\n'
    '  company_tags  企业标签数组，3~6 个，如"整车厂""港股上市""常州本部"\n'
    '  source_note   企业信息可靠程度，只能填：熟悉 / 部分确定 / 存疑；'
    '填"存疑"时必须在本简介里点明哪部分拿不准\n'
    '  profile       一句话人才画像，50 字以内，直接说他们真正要的是哪种人\n'
    '  hard          硬性门槛数组：学历、专业、年限、证书、必须会用的工具\n'
    '  skills        关键能力数组：JD 里写的具体技术 / 工具 / 业务场景\n'
    '  implicit      隐性偏好数组：JD 没明写、但这个岗位大概率看重的经历\n'
    '  advice        求职建议数组，4~6 条：简历怎么改、项目怎么摆、'
    '面试准备什么、该问什么问题\n'
    '  risks         注意事项数组，2~4 条：加班强度、派遣外包、'
    '服务期/竞业、岗位职责与 JD 不符之类的信号；没有明显信号就留空数组\n'
)

INSIGHT_KEYS = ['company', 'company_tags', 'source_note', 'profile',
                'hard', 'skills', 'implicit', 'advice', 'risks']


def build_insight_messages(job, max_jd=6000):
    """构造解读请求的 messages。

    job: {company, title, city, salary, education, experience,
          department, jobType, headcount, deadline, link, jd}
    """
    job = job or {}
    lines = []
    for label, key in (('企业', 'company'), ('岗位', 'title'), ('地点', 'city'),
                       ('薪资', 'salary'), ('学历', 'education'), ('经验', 'experience'),
                       ('部门', 'department'), ('性质', 'jobType'),
                       ('人数', 'headcount'), ('截止', 'deadline')):
        v = str(job.get(key) or '').strip()
        if v:
            lines.append('  %s：%s' % (label, v))
    jd = str(job.get('jd') or '').strip()[:max_jd]

    user = (
        '【岗位】\n%s\n\n'
        '【原文 JD】\n"""\n%s\n"""\n\n'
        '%s\n\n'
        'JD 很短或缺失时，就按岗位名称与常识给判断，'
        '并在 source_note 里写"存疑"。'
    ) % (
        '\n'.join(lines) if lines else '  （未给出）',
        jd if jd else '（本页没抓到 JD 原文）',
        INSIGHT_SPEC,
    )
    return [
        {'role': 'system', 'content': INSIGHT_SYSTEM},
        {'role': 'user', 'content': user},
    ]


def _as_list(v, limit=8, item_limit=140):
    """把模型返回的东西洗成字符串列表。

    模型三种毛病都要接住：该给数组给了字符串、带了序号前缀、
    用「无 / 未知」占位。这里统一剥掉。
    """
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        items = [str(x) for x in v]
    else:
        s = str(v).strip()
        # 模型常见的偷懒：`["a","b"]` 整个塞进字符串里，先还原成真列表
        if s.startswith('['):
            try:
                loaded = json.loads(s)
                items = [str(x) for x in loaded] if isinstance(loaded, list) else [s]
            except Exception:
                items = [s]
        else:
            s = s.replace('\r', '\n')
            items = re.split(r'[\n；;]+', s)
    out = []
    for it in items:
        s = re.sub(r'^\s*[0-9]+[.、)）]\s*', '', str(it)).strip(' 　-•·*\t"\'')
        if not s or s in ('无', '未知', '未提供', '不明确', 'null', 'None', 'N/A'):
            continue
        if len(s) > item_limit:
            s = s[:item_limit] + '…'
        out.append(s)
    return out[:limit]


def _as_text(v, limit):
    s = '' if v is None else str(v).strip()
    if s in ('未知', '无', '未提供', 'null', 'None', 'N/A'):
        s = ''
    if len(s) > limit:
        s = s[:limit] + '…'
    return s


def clean_insight(parsed):
    """把模型返回的对象整理成前端直接能渲染的形状。"""
    parsed = parsed or {}
    if not isinstance(parsed, dict):
        return {}
    if 'insight' in parsed and isinstance(parsed['insight'], dict):
        parsed = parsed['insight']
    # 模型偶尔跑偏，用中文键名再兜一次
    CN_MAP = {'公司简介': 'company', '企业简介': 'company', '企业标签': 'company_tags',
              '可靠程度': 'source_note', '人才画像': 'profile', '硬性门槛': 'hard',
              '硬门槛': 'hard', '关键能力': 'skills', '隐性偏好': 'implicit',
              '求职建议': 'advice', '建议': 'advice', '注意事项': 'risks'}
    if not any(k in parsed for k in INSIGHT_KEYS):
        parsed = {CN_MAP.get(k, k): v for k, v in parsed.items()}

    note = _as_text(parsed.get('source_note'), 20)
    if note not in ('熟悉', '部分确定', '存疑'):
        note = '部分确定'
    return {
        'company': _as_text(parsed.get('company'), 600),
        'companyTags': _as_list(parsed.get('company_tags'), 6, 30),
        'sourceNote': note,
        'profile': _as_text(parsed.get('profile'), 200),
        'hard': _as_list(parsed.get('hard'), 8),
        'skills': _as_list(parsed.get('skills'), 10),
        'implicit': _as_list(parsed.get('implicit'), 6),
        'advice': _as_list(parsed.get('advice'), 6, 200),
        'risks': _as_list(parsed.get('risks'), 4),
    }


def insight(conn, payload):
    """公司与岗位解读。

    payload: {job: {company, title, jd, ...}}
    返回 (响应字典, HTTP 状态码)
    """
    cfg = load_config(conn)
    ok, bad = _cfg_ready(cfg)
    if not ok:
        return bad, 200

    job = (payload or {}).get('job') or {}
    if not str(job.get('company') or '').strip():
        return {'ok': False, 'error': '先填企业名才知道要查哪家', 'code': 'no_company'}, 400

    # 输出比抽取长得多，超时单独放宽；用户在配置里给的时间更短时以配置为准会被掐断
    cfg = dict(cfg)
    cfg['timeout'] = max(int(cfg.get('timeout') or 40), 90)

    messages = build_insight_messages(job)
    content, err = call_model(cfg, messages, temperature=0.4, max_tokens=1800)
    if err:
        return {'ok': False, 'error': err, 'code': 'api_error'}, 200

    parsed = parse_json_reply(content)
    cleaned = clean_insight(parsed)
    if not cleaned.get('company') and not cleaned.get('profile'):
        return {'ok': False, 'error': '返回内容没法整理成解读结果',
                'raw': (content or '')[:300], 'code': 'bad_json'}, 200

    return {
        'ok': True,
        'insight': cleaned,
        'provider': cfg['provider'],
        'model': cfg['model'],
    }, 200


# ============================================================
# 列表页筛选
# ============================================================
# 详情页解决"这一条存得准不准"，列表页解决"这一页哪些值得点开"。
# 判断要用求职者的画像（目标岗位/城市/技能/明确不要），
# 画像存在工作台 profile 表，**只在本机**，所以筛选必须在服务端做。

FILTER_SYSTEM = (
    '你是站在求职者这一边的岗位筛选助手。给你这个人的求职画像和从某个页面上'
    '无差别收割下来的一批"候选行"（每条只有岗位名、企业、行内的城市/薪资/学历这些信息），'
    '你要先判断这是不是岗位列表页，再从里面挑出值得点开细看的，并说清为什么。\n'
    '铁律：\n'
    '0. **先判页面**：候选行里真正是岗位（有岗位名，通常有企业/城市/薪资之一）的'
    '少于 2 条，就判 is_list=false —— 那多半是岗位详情页、公司主页、导航页或资讯页，'
    '只是在页面上顺带捞到了几个链接。判 false 时 picks 给空数组，'
    'list_reason 一句话说清这页其实是什么（≤30 字）。\n'
    '1. 只依据【画像】和【列表行】里看得见的信息判断。行里没写的不要脑补 —— '
    '没写职责就不猜职责，没写地点就不猜地点。\n'
    '2. 只有画像里"明确不要"的企业/城市/方向，才直接判不匹配、不返回。'
    '【期望城市】【目标企业】【薪资要求】都只是**偏好，不是硬门槛** —— '
    '岗位在别的城市、企业不在名单里、薪资没写或偏低，只要方向对得上就照常给「推荐」，'
    '把这些出入写进 reason 提一句就行，不要因此降档。\n'
    '3. 判档只看**方向**：岗位名或行内信息能落到画像的目标方向上（含上下游与相邻工种，'
    '如热管理/冷却/空调、气动/CFD/流场、NVH/振动噪声/声学、整车性能/整车集成），'
    '就给「推荐」。**不要**因为城市不符、企业不在名单、学历或经验年限对不上、'
    '行里信息少而降到「可选」—— 这些只写进 reason，不影响等级。'
    '只有方向只是沾边（看得出跟车/机械相关，但落不到目标方向上）才给「可选」。\n'
    '4. reason 一句话、30 字以内，必须落到具体的字：'
    '要么说命中了画像里的哪个词，要么说清是哪个条件有出入（只作提示，不影响等级）。'
    '禁止写"很适合你""推荐投递"这类没信息量的话。\n'
    '5. 只输出 推荐 / 可选 的条目。完全不沾边的（后端开发、财务人事这类非技术职能，'
    '以及宣讲会通知、导航、联系我们这种根本不是岗位的行）不返回。\n'
    '6. 输出单个 JSON 对象，不要 markdown 代码块，不要解释文字。\n'
)

FILTER_SPEC = (
    '输出 JSON：\n'
    '  is_list      true / false —— 这页是不是岗位列表页（按上面第 0 条判）\n'
    '  list_reason  判 false 时必填，一句话说清这页其实是什么（≤30 字）；判 true 时给空串\n'
    '  picks        数组，每项 {"i": 上面方括号里的序号, '
    '"level": "推荐" 或 "可选", "reason": "≤30 字的具体理由"}\n'
    '               方向对口一律用"推荐"（这一档应当占大多数）；只有方向沾边才用"可选"\n'
    '               picks 最多 60 条；对口的超过 60 条时，只给方向最贴近的 60 条\n'
    '  summary      一句话总结这页的情况，40 字以内'
)

# 2026-10-13 放宽判档（用户反馈"太严格"）：
#   旧版把"信息不足→可选""非期望城市→可选"写进铁律，实测把某整车厂热管理岗（年限不符）、
#   某第三方检测机构气动岗（城市不在列表）、外地 NVH 岗（城市+信息少）全压成"可选"，9 条只挑出 4 条。
#   新版：期望城市/目标企业/薪资降为偏好，判档只看方向对不对口。


def clean_list_verdict(parsed):
    """取模型对"这页是不是岗位列表页"的判定。缺字段时按 true 处理（老行为，不打断筛选）。"""
    if not isinstance(parsed, dict):
        return True, ''
    v = parsed.get('is_list')
    if isinstance(v, str):
        v = v.strip().lower()
        if v in ('false', 'no', '0', '不是', '否'):
            v = False
        elif v in ('true', 'yes', '1', '是'):
            v = True
    if v is False:
        return False, _as_text(parsed.get('list_reason'), 60)
    return True, ''

FILTER_MAX_ITEMS = 200       # 再多也只是烧 token，模型注意力也撑不住

# profile 表里要用的字段 → 喂给模型时的标签
PROFILE_FIELDS = [
    ('edu', '学历'),
    ('research', '研究方向'),
    ('targetJobs', '目标岗位'),
    ('cities', '期望城市'),
    ('companies', '目标企业'),
    ('tech', '技能工具'),
    ('salary', '薪资要求'),
    ('avoid', '明确不要'),
]


def profile_text(conn):
    """从 profile 表读出求职画像，拼成喂给模型的文本。空则返回 ''。"""
    try:
        row = conn.execute('SELECT * FROM profile WHERE id=1').fetchone()
    except Exception:
        return ''
    if not row:
        return ''
    try:
        row = dict(row)
    except Exception:
        return ''
    lines = []
    for key, label in PROFILE_FIELDS:
        v = str(row.get(key) or '').strip()
        if v:
            lines.append('%s：%s' % (label, v))
    return '\n'.join(lines)


def build_filter_messages(profile, items, meta=None):
    """构造列表筛选的 messages。items 是 [{'title','company','salary',...}]。"""
    meta = meta or {}
    lines = []
    for i, it in enumerate(items or []):
        it = it or {}
        bits = []
        for k in ('title', 'company', 'salary', 'education', 'experience'):
            v = str(it.get(k) or '').strip()
            if v:
                bits.append(v)
        tail = str(it.get('text') or '').strip()[:160]
        seg = ' | '.join(bits)
        if tail and tail not in seg:
            seg = seg + ' | ' + tail if seg else tail
        lines.append('[%d] %s' % (i, seg))

    user = (
        '【求职画像】\n%s\n\n'
        '【列表页】%s\n共 %d 条：\n%s\n\n'
        '%s\n'
    ) % (
        profile,
        (meta.get('title') or '') + ((' · ' + meta.get('host')) if meta.get('host') else ''),
        len(lines),
        '\n'.join(lines),
        FILTER_SPEC,
    )
    return [
        {'role': 'system', 'content': FILTER_SYSTEM},
        {'role': 'user', 'content': user},
    ]


def clean_filter_result(parsed, total):
    """把模型返回的 picks 洗成前端能用的样子：序号有效、level 合法、reason 有长度上限。"""
    if not isinstance(parsed, dict):
        return [], ''
    picks_raw = parsed.get('picks') or []
    if isinstance(picks_raw, dict):
        picks_raw = [picks_raw]
    if not isinstance(picks_raw, (list, tuple)):
        picks_raw = []

    out, seen = [], {}
    for p in picks_raw:
        if not isinstance(p, dict):
            continue
        try:
            i = int(p.get('i'))
        except Exception:
            continue
        if i < 0 or i >= total or i in seen:
            continue
        level = str(p.get('level') or '').strip()
        # 约定是「不匹配的不返回」；模型还是返回了就丢掉，绝不降级成可选
        if level not in ('推荐', '可选'):
            continue
        reason = _as_text(p.get('reason'), 60)
        if not reason:
            continue
        seen[i] = 1
        out.append({'i': i, 'level': level, 'reason': reason})

    out.sort(key=lambda x: (0 if x['level'] == '推荐' else 1, x['i']))
    return out, _as_text(parsed.get('summary'), 80)


def filter_jobs(conn, payload):
    """列表页筛选主入口。

    payload: {items:[{title,company,salary,education,experience,text}], meta:{url,title,host}}
    返回 (响应字典, HTTP 状态码)
    """
    cfg = load_config(conn)
    ok, bad = _cfg_ready(cfg)
    if not ok:
        return bad, 200

    profile = profile_text(conn)
    if not profile:
        return {'ok': False, 'code': 'no_profile',
                'error': '工作台里还没填求职目标 —— 先去「我的画像」把学历/方向/城市/技能填上，'
                         '不然没法替你筛'}, 200

    items = (payload or {}).get('items') or []
    if not isinstance(items, list):
        items = []
    total = len(items)
    if total > FILTER_MAX_ITEMS:
        items = items[:FILTER_MAX_ITEMS]
    if not items:
        return {'ok': False, 'error': '这一页没抽到岗位行', 'code': 'no_items'}, 400

    # 一次要读几十上百行，超时按洞察那档放宽
    cfg = dict(cfg)
    cfg['timeout'] = max(int(cfg.get('timeout') or 40), 90)

    messages = build_filter_messages(profile, items, (payload or {}).get('meta') or {})
    # 判档放宽后一页可能命中几十条，2000 会在 JSON 中途被截断（解析失败=整页白筛），抬到 4000
    content, err = call_model(cfg, messages, temperature=0.2, max_tokens=4000)
    if err:
        return {'ok': False, 'error': err, 'code': 'api_error'}, 200

    parsed = parse_json_reply(content)
    picks, summary = clean_filter_result(parsed, len(items))
    is_list, list_reason = clean_list_verdict(parsed)
    return {
        'ok': True,
        'isList': is_list,
        'listReason': list_reason or '',
        'picks': picks,
        'summary': summary or '',
        'total': len(items),
        'hit': sum(1 for p in picks if p['level'] == '推荐'),
        'maybe': sum(1 for p in picks if p['level'] == '可选'),
        'provider': cfg['provider'],
        'model': cfg['model'],
    }, 200


def test_connection(conn):
    """配置页的「测试连接」按钮。发一个最小请求，验证 Key / 地址 / 模型是否可用。"""
    cfg = load_config(conn)
    if cfg.get('auth') != 'none' and not cfg.get('api_key'):
        return {'ok': False, 'error': '还没填 API Key'}, 200
    if not cfg.get('base_url'):
        return {'ok': False, 'error': '还没填接口地址'}, 200

    messages = [
        {'role': 'system', 'content': '你是一个测试助手。'},
        {'role': 'user', 'content': '请只回复两个字：正常'},
    ]
    content, err = call_model(cfg, messages)
    if err:
        return {'ok': False, 'error': err, 'provider': cfg['provider'],
                'model': cfg['model']}, 200
    return {'ok': True, 'provider': cfg['provider'], 'model': cfg['model'],
            'reply': (content or '').strip()[:80]}, 200


def extract_for_test(cfg, capture, page_text):
    """给测试脚本用的纯函数入口，不碰数据库。"""
    fields = (capture or {}).get('fields') or {}
    conf = (capture.get('confidence') or {}).get('fields') or {}
    only = pick_target_fields(fields, conf)
    messages = build_messages(capture, page_text, only)
    content, err = call_model(cfg, messages)
    if err:
        return {'ok': False, 'error': err}
    parsed = parse_json_reply(content)
    if not isinstance(parsed, dict):
        return {'ok': False, 'error': 'bad json', 'raw': content}
    merged, adopted, skipped = merge_result(capture, parsed, only)
    return {'ok': True, 'adopted': adopted, 'skipped': skipped,
            'asked': only, 'capture': merged}
