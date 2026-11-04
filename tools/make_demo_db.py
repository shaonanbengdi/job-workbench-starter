#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成一份脱敏的演示数据库（data.db）。

用途：项目对外分发时不带任何真实个人信息与企业投递记录，但打开就要有内容，
让使用者一眼看懂每个视图是干什么的。这里的数据全部虚构：

  人物  陈晨 / 13800000000 / chenchen@example.com
  企业  星驰汽车、远航动力、宏途智能、云淞精工、蓝湾研究院（均不存在）

用法：
    python tools/make_demo_db.py            # 覆盖生成 ./data.db
    python tools/make_demo_db.py --force    # 已存在时也覆盖

不要把这个脚本当成"重置数据"用——它会删掉 data.db 后重建，真实数据请先备份。
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import date, datetime, timedelta

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, 'data.db')

# 与 server.py 中的 SCHEMA 保持一致；表结构升级时这里要同步。
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
CREATE TABLE IF NOT EXISTS scraped_ids(
  item_id TEXT PRIMARY KEY, category TEXT, scraped_at TEXT
);
CREATE TABLE IF NOT EXISTS tags(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE, color TEXT,
  kind TEXT DEFAULT 'normal', rule_type TEXT DEFAULT '', rule_value INTEGER DEFAULT 0,
  created_at TEXT
);
"""


def stamp(d=None, t='10:00:00'):
    d = d or date.today()
    return '%s %s' % (d.isoformat(), t)


def days(n):
    return date.today() + timedelta(days=n)


# ---------------------------------------------------------------- 示例数据

PROFILE = {
    'name': '陈晨',
    'phone': '13800000000',
    'email': 'chenchen@example.com',
    'edu': '硕士研三（2027 届），车辆工程',
    'research': '整车外流场气动减阻、电池包热管理',
    'targetJobs': 'CFD / 气动 / 热管理 / NVH',
    'cities': '上海、苏州、合肥',
    'salary': '20-28k × 14',
    'tech': 'Star-CCM+、Fluent、OpenFOAM、ANSA、CATIA、Python/MATLAB',
    'companies': '星驰汽车、远航动力、宏途智能、云淞精工',
    'avoid': '不接受长期驻外；初创公司谨慎',
    'extra': '{}',
}

# (key, 企业, 岗位, importance, tags, jd, extra)
JOBS = [
    ('job_demo_001', '星驰汽车', '整车气动 CFD 仿真工程师', 'focus',
     ['CFD', '气动', 'Star-CCM+'],
     '岗位职责：\n'
     '1. 负责整车外流场 CFD 仿真分析，开展气动减阻与风噪优化；\n'
     '2. 使用 Star-CCM+ 完成几何清理、网格划分、求解与后处理，输出分析报告；\n'
     '3. 参与风洞试验方案制定与仿真-试验对标，推动模型修正。\n'
     '任职要求：\n'
     '1. 车辆工程、流体力学、工程热物理等相关专业硕士及以上；\n'
     '2. 熟练使用 Star-CCM+ 或 Fluent，理解湍流模型与网格无关性验证；\n'
     '3. 有整车气动或风噪项目经验者优先。',
     {'city': '上海', 'department': '整车性能开发部', 'jobType': '校招', 'education': '硕士',
      'experience': '应届', 'source': '示例数据', 'sortOrder': 0}),

    ('job_demo_002', '星驰汽车', '热管理仿真工程师', 'focus',
     ['热管理', 'Fluent', '电池'],
     '岗位职责：\n'
     '1. 负责电驱与电池热管理系统的三维热仿真与一维系统建模；\n'
     '2. 参与冷却回路方案设计与台架试验对标；\n'
     '3. 建立热仿真流程规范与材料库。\n'
     '任职要求：\n'
     '1. 硕士及以上，工程热物理、热能工程、车辆工程相关专业；\n'
     '2. 熟悉 Fluent / Star-CCM+ 传热计算，了解 AMESim 或 GT-SUITE 者优先；\n'
     '3. 具备 Python 数据处理能力。',
     {'city': '上海', 'department': '热管理开发部', 'jobType': '校招', 'education': '硕士',
      'experience': '应届', 'source': '示例数据', 'sortOrder': 1}),

    ('job_demo_003', '远航动力', '电池热管理工程师（液冷方向）', 'focus',
     ['热管理', '液冷', 'CFD'],
     '岗位职责：\n'
     '1. 负责动力电池液冷板流道设计与 CFD 仿真优化；\n'
     '2. 完成电芯温差、压降与快充工况的热平衡评估；\n'
     '3. 支持热管理台架试验与整车标定。\n'
     '任职要求：\n'
     '1. 硕士学历，传热学、流体力学相关背景；\n'
     '2. 熟练使用 Fluent，能独立完成流固耦合传热计算；\n'
     '3. 有电池包或储能液冷项目经验者优先。',
     {'city': '常州', 'department': '电池系统研究院', 'jobType': '校招', 'education': '硕士',
      'experience': '应届', 'source': '示例数据', 'sortOrder': 2}),

    ('job_demo_004', '宏途智能装备', '流体仿真工程师', 'can',
     ['仿真', 'Fluent', '工程机械'],
     '岗位职责：\n'
     '1. 负责工程机械整机与部件的流场、散热与气动仿真；\n'
     '2. 参与冷却系统匹配与风道优化；\n'
     '3. 编写仿真规范与试验对标报告。\n'
     '任职要求：\n'
     '1. 流体力学、动力工程相关专业硕士；\n'
     '2. 熟悉 Fluent / OpenFOAM，具备复杂几何网格处理能力；\n'
     '3. 能适应短期出差与样机测试。',
     {'city': '徐州', 'department': '研发中心', 'jobType': '校招', 'education': '硕士',
      'experience': '应届', 'source': '示例数据', 'sortOrder': 3}),

    ('job_demo_005', '云淞精工', 'CAE 结构仿真工程师', 'can',
     ['CAE', '结构', 'Abaqus'],
     '岗位职责：\n'
     '1. 负责汽车零部件强度、刚度与疲劳寿命仿真分析；\n'
     '2. 参与轻量化方案设计与拓扑优化；\n'
     '3. 支持样件试验失效分析。\n'
     '任职要求：\n'
     '1. 机械、车辆、力学相关专业硕士；\n'
     '2. 熟练使用 Abaqus / HyperMesh，了解 OptiStruct 者优先；\n'
     '3. 具备一定的材料力学理论基础。',
     {'city': '苏州', 'department': 'CAE 部', 'jobType': '校招', 'education': '硕士',
      'experience': '应届', 'source': '示例数据', 'sortOrder': 4}),

    ('job_demo_006', '蓝湾研究院', 'NVH 仿真工程师', 'can',
     ['NVH', '声学', '仿真'],
     '岗位职责：\n'
     '1. 负责整车与子系统 NVH 仿真分析，包括模态、传递路径与声学包；\n'
     '2. 参与风噪与路噪问题诊断；\n'
     '3. 支持整车 NVH 目标分解与验证。\n'
     '任职要求：\n'
     '1. 声学、机械、车辆工程相关专业硕士；\n'
     '2. 熟悉 Nastran / Virtual.Lab / Actran 等工具；\n'
     '3. 有模态试验或声学包开发经验者优先。',
     {'city': '合肥', 'department': 'NVH 研究所', 'jobType': '校招', 'education': '硕士',
      'experience': '应届', 'source': '示例数据', 'sortOrder': 5}),

    ('job_demo_007', '星驰汽车', '空气动力学试验工程师', 'can',
     ['风洞', '试验', '气动'],
     '岗位职责：\n'
     '1. 负责风洞试验方案设计、模型准备与数据处理；\n'
     '2. 维护风洞测试设备与标定流程；\n'
     '3. 编写试验报告并支撑仿真模型修正。\n'
     '任职要求：\n'
     '1. 车辆工程、流体力学相关专业硕士；\n'
     '2. 了解风洞试验原理与测量手段；\n'
     '3. 具备 Python 或 MATLAB 数据处理能力。',
     {'city': '上海', 'department': '试验中心', 'jobType': '校招', 'education': '硕士',
      'experience': '应届', 'source': '示例数据', 'sortOrder': 6}),
]

# (jobId, 状态, 计划日偏移, 投递日偏移 or None)
APPS = [
    ('job_demo_001', 'interview', -12, -10),
    ('job_demo_002', 'applied', -8, -6),
    ('job_demo_003', 'offer', -20, -18),
    ('job_demo_004', 'written', -5, -3),
    ('job_demo_005', 'thanks', -22, -20),
    ('job_demo_006', 'plan', 2, None),
    ('job_demo_007', 'applied', -4, -2),
]

# (标题, 类型, 日期偏移, 时间, jobId, 提醒值, 提醒单位)
EVENTS = [
    ('星驰汽车 整车气动岗 一面', 'interview', 1, '14:00', 'job_demo_001', 1, 'days'),
    ('远航动力 网申截止', 'deadline', 3, '23:59', 'job_demo_003', 2, 'days'),
    ('宏途智能 在线测评截止', 'written', 5, '20:00', 'job_demo_004', 1, 'days'),
    ('星驰汽车 热管理岗 技术面', 'interview', 7, '10:30', 'job_demo_002', 1, 'days'),
    ('云淞精工 投递后跟进', 'follow', 9, '09:30', 'job_demo_005', 0, 'days'),
    ('蓝湾研究院 简历投递', 'reminder', 11, '19:00', 'job_demo_006', 1, 'days'),
]

# (名称, 类型, 起始, 结束, 简历版, 仅自己可见备注)
CARDS = [
    ('card_demo_001', '某车型气动减阻优化', '科研项目', '2025-03', '2026-06',
     '基于 Star-CCM+ 对某整车模型开展外流场仿真与气动减阻优化；\n'
     '针对尾部与底盘区域设计 3 轮改型，整车风阻系数降低 0.012（约 4%）；\n'
     '搭建 Python 批处理流程，单轮迭代周期由 5 天缩短至 3 天。',
     '【仅自己可见】初期网格质量不达标导致残差震荡，改用多面体网格+局部加密后稳定。'
     '面试要能讲清尾流结构变化，而不只是报一个减阻数字。'),

    ('card_demo_002', '电池包液冷系统热仿真（实习）', '实习', '2025-07', '2025-12',
     '在新能源企业热管理部门实习，负责电池包液冷板 Fluent 热仿真；\n'
     '优化流道走向后电芯最大温差由 8.2℃ 降至 4.5℃，满足快充工况要求；\n'
     '输出热仿真规范 1 份，参与台架对标，仿真与实测误差控制在 8% 以内。',
     '【仅自己可见】对标误差主要来自接触热阻与边界条件简化，'
     '被追问时如实说明并给出改进思路，不要硬编数据。'),

    ('card_demo_003', '方程式赛车空力套件设计', '竞赛', '2024-09', '2025-06',
     '负责赛车前翼、尾翼与扩散器设计与 OpenFOAM 仿真验证；\n'
     '整车下压力提升 35%，阻力增量控制在 6% 以内；\n'
     '团队获全国二等奖，个人负责 CFD 分析与风洞试验方案。',
     '【仅自己可见】风洞只做了缩比模型，雷诺数差异被评委追问过，'
     '准备好尺度效应的应答口径。'),

    ('card_demo_004', '粗糙度函数驱动的壁面滑移建模', '论文', '2024-10', '2026-03',
     '将 DNS 粗糙度函数映射为壁面滑移边界条件，用于三维 CFD 减阻预测；\n'
     '在典型工况下减阻率预测偏差小于 1 个百分点；\n'
     '论文发表于一区期刊（第一作者）。',
     '【仅自己可见】审稿人质疑参数适用范围，补充了 s+ 区间敏感性分析后接收。'),
]

# (标题, 企业, 城市, 薪资, 学历, 经验, 来源, 类别, 状态, 匹配度)
CANDIDATES = [
    ('星驰汽车 2027 届校招 · 整车性能类', '星驰汽车', '上海', '20-30k', '硕士', '应届', '示例数据', '主机厂', 'pending', 'high'),
    ('远航动力 2027 届校招 · 热管理', '远航动力', '常州', '18-26k', '硕士', '应届', '示例数据', '龙头供应商', 'pending', 'high'),
    ('宏途智能 流体仿真工程师', '宏途智能装备', '徐州', '15-22k', '硕士', '应届', '示例数据', '商用车/工程机械/轨交', 'included', 'medium'),
    ('云淞精工 CAE 工程师（结构方向）', '云淞精工', '苏州', '14-20k', '硕士', '应届', '示例数据', '龙头供应商', 'pending', 'medium'),
    ('蓝湾研究院 NVH 仿真岗', '蓝湾研究院', '合肥', '18-25k', '硕士', '应届', '示例数据', '科研院所/检测', 'pending', 'high'),
    ('星驰汽车 热管理仿真工程师', '星驰汽车', '上海', '22-30k', '硕士', '应届', '示例数据', '主机厂', 'included', 'high'),
    ('某高校 2027 届秋季双选会', '校园招聘会', '本市', '', '', '', '示例数据', '招聘会', 'pending', ''),
    ('某企业 校园宣讲会（整车研发专场）', '星驰汽车', '本市', '', '', '', '示例数据', '宣讲会', 'pending', ''),
    ('远航动力 电池热管理（液冷）', '远航动力', '常州', '20-28k', '硕士', '应届', '示例数据', '龙头供应商', 'pending', 'high'),
    ('宏途智能 冷却系统匹配工程师', '宏途智能装备', '徐州', '16-23k', '硕士', '应届', '示例数据', '商用车/工程机械/轨交', 'pending', 'medium'),
]

TAGS = [
    ('最多投递2个', '#2563eb', 'rule', 'max_apply', 2),
    ('最多投递3个', '#dc2626', 'rule', 'max_apply', 3),
    ('最多投递4个', '#2563eb', 'rule', 'max_apply', 4),
]


def build(force=False):
    if os.path.exists(DB_PATH) and not force:
        print('data.db 已存在，未覆盖。确需重建请加 --force', file=sys.stderr)
        return 1

    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    for suffix in ('-wal', '-shm'):
        p = DB_PATH + suffix
        if os.path.exists(p):
            os.remove(p)

    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)

    now = stamp(t=datetime.now().strftime('%H:%M:%S'))

    conn.execute(
        'INSERT INTO profile(id,name,phone,email,edu,research,targetJobs,cities,salary,tech,'
        'companies,avoid,extra,updated_at) VALUES(1,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (PROFILE['name'], PROFILE['phone'], PROFILE['email'], PROFILE['edu'],
         PROFILE['research'], PROFILE['targetJobs'], PROFILE['cities'], PROFILE['salary'],
         PROFILE['tech'], PROFILE['companies'], PROFILE['avoid'], PROFILE['extra'], now))

    for i, (jid, company, title, imp, tags, jd, extra) in enumerate(JOBS):
        created = stamp(days(-25 + i * 2), '09:%02d:00' % (10 + i))
        extra = dict(extra)
        extra.setdefault('createdAt', created)
        conn.execute(
            'INSERT INTO jobs(id,company,title,jd,link,importance,tags,sample,extra,'
            'created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            (jid, company, title, jd, '', imp, json.dumps(tags, ensure_ascii=False),
             1, json.dumps(extra, ensure_ascii=False), created, created))

    for i, (jid, status, plan_off, applied_off) in enumerate(APPS):
        company, position = next((j[1], j[2]) for j in JOBS if j[0] == jid)
        plan = days(plan_off).isoformat()
        applied = days(applied_off).isoformat() if applied_off is not None else ''
        conn.execute(
            'INSERT INTO apps(id,jobId,company,position,planDate,appliedDate,status,sample,'
            'extra,updated_at,sort_order) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            ('app_' + jid, jid, company, position, plan, applied, status, 1,
             json.dumps({'updatedAt': now}, ensure_ascii=False), now, i))

    for i, (title, etype, off, tm, jid, rv, ru) in enumerate(EVENTS):
        conn.execute(
            'INSERT INTO events(id,title,type,date,time,jobId,remindVal,remindUnit,sample,'
            'extra,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            ('evt_demo_%03d' % (i + 1), title, etype, days(off).isoformat(), tm, jid,
             rv, ru, 1, '{}', now))

    for cid, name, ctype, start, end, resume, story in CARDS:
        conn.execute(
            'INSERT INTO cards(id,name,type,start,"end",resume,story,sample,extra,updated_at)'
            ' VALUES(?,?,?,?,?,?,?,?,?,?)',
            (cid, name, ctype, start, end, resume, story, 1, '{}', now))

    for i, (title, company, city, salary, edu, exp, source, cat, status, level) in enumerate(CANDIDATES):
        cid = 'cand_demo_%03d' % (i + 1)
        is_fair = cat in ('宣讲会', '招聘会')
        conn.execute(
            'INSERT INTO candidates(id,title,company,city,salary,education,experience,source,'
            'publish_date,url,category,fair_date,fair_location,status,action,scraped_at,extra)'
            ' VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (cid, title, company, city, salary, edu, exp, source,
             days(-3 - i).isoformat(), '',
             cat, days(4 + i).isoformat() if is_fair else '',
             '线上' if is_fair else '',
             status, '', now, json.dumps({'match_level': level}, ensure_ascii=False)))

    for name, color, kind, rtype, rval in TAGS:
        conn.execute('INSERT INTO tags(name,color,kind,rule_type,rule_value,created_at)'
                     ' VALUES(?,?,?,?,?,?)', (name, color, kind, rtype, rval, now))

    meta = {
        'seeded': 'true',
        'ai_enabled': '0',
        'ai_provider': 'deepseek',
        'ai_model': 'deepseek-chat',
        'ai_base_url': 'https://api.deepseek.com/chat/completions',
        'ai_api_key': '',
    }
    conn.executemany('INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)', meta.items())
    conn.commit()

    counts = {t: conn.execute('SELECT COUNT(*) FROM %s' % t).fetchone()[0]
              for t in ('profile', 'cards', 'jobs', 'apps', 'events', 'candidates', 'tags')}
    conn.close()

    print('已生成演示库：%s' % DB_PATH)
    for k, v in counts.items():
        print('  %-11s %d' % (k, v))
    print('  meta.ai_api_key 已置空，AI 功能默认关闭')
    return 0


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description='生成脱敏演示数据库')
    ap.add_argument('--force', action='store_true', help='已存在 data.db 时强制覆盖')
    sys.exit(build(ap.parse_args().force))
