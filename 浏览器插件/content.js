/* ============================================================================
 * content.js —— 页面内交互层
 *
 * 职责边界（很重要，避免和工作台重复实现一套逻辑）：
 *   · 本脚本只负责「界面」：悬浮按钮、确认卡片、把用户改完的数据 + 判重所需
 *     的现有岗位摘要交给后台。
 *   · 解析与判重放在 lib/job-parser.js（可单测的纯函数）。
 *   · 工作台地址探测与写库放在 background.js。
 *
 * 为什么要让用户在面板上确认一次，而不是「一键静默入库」：
 *   招聘站的字段命名五花八门，薪资/学历这类字段抽错的代价是投错岗位。
 *   面板把每个字段的置信度显示出来，低于 60 分的标黄，用户扫一眼就能改。
 * ==========================================================================*/
(function () {
  'use strict';

  if (window.__JOB_CAPTURE_INJECTED__) return;
  window.__JOB_CAPTURE_INJECTED__ = true;

  /* 构建标记：用来判断浏览器里跑的是不是最新的 content script。
   * 旧脚本没有这个常量 → 说明扩展没重载 / 页面没刷新，而不是工作台没开。 */
  var JC_BUILD = '1.3.8+harvest';

  /* ------------------------------------------------------------------ 状态 */
  var state = {
    booted: false,
    capturing: false,
    aiCapturing: false,   /* AI 全量识别进行中 */
    aiFullToken: null,    /* 当前全量识别的取消令牌 */
    panel: null,          /* 面板根节点 */
    current: null,        /* 本次抓取结果 */
    editing: false,       /* 是否处于编辑态 */
    dup: null,            /* 判重结果 */
    insight: null,        /* 公司与岗位解读结果（预览面板那个按钮产出） */
    insightBusy: false,
    insightToken: null,   /* 等待途中被关掉时用它，好了就别硬弹回来 */
    draggingFab: false,   /* 刚拖过浮标，紧接着那次 click 要忽略 */
    marked: [],           /* 列表页已打过标记的行，供「清除」复原 */
    filterBusy: false,
    lastFilter: null,     /* {items, picks} —— 下次点「筛岗位」前用来清场 */
    hint: null,           /* 'list' / 'detail' —— 上一次哪个动作成了，决定 dock 谁排上面；本地不猜 */
    workbench: { ok: null, base: '', port: null, error: '' },
  };

  var BTN_POS_KEY = 'jc_btn_pos';
  var BTN_HIDDEN_KEY = 'jc_btn_hidden';

  /* --------------------------------------------------------------- 基础工具 */
  /* svg/path 必须走 SVG 命名空间 —— document.createElement('svg') 建出来的是
   * HTMLUnknownElement，在 Chrome 里一个像素都不会画，图标就凭空消失了。 */
  var SVG_NS = 'http://www.w3.org/2000/svg';
  var SVG_TAGS = { svg: 1, path: 1, g: 1, circle: 1, rect: 1, line: 1, polyline: 1, polygon: 1 };

  function h(tag, attrs, children) {
    var isSvg = !!SVG_TAGS[tag];
    var e = isSvg ? document.createElementNS(SVG_NS, tag) : document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (k) {
        if (k === 'class') { if (isSvg) e.setAttribute('class', attrs[k]); else e.className = attrs[k]; }
        else if (k === 'text') e.textContent = attrs[k];
        else if (k === 'html') e.innerHTML = attrs[k];
        else if (k === 'style') e.setAttribute('style', attrs[k]);
        else if (k.indexOf('on') === 0 && typeof attrs[k] === 'function') e.addEventListener(k.slice(2), attrs[k]);
        else if (attrs[k] !== null && attrs[k] !== undefined) e.setAttribute(k, attrs[k]);
      });
    }
    (children || []).forEach(function (c) { if (c) e.appendChild(typeof c === 'string' ? document.createTextNode(c) : c); });
    return e;
  }

  function el(id) { return state.panel ? state.panel.querySelector('#' + id) : null; }

  /* --------------------------------------------------------------- 悬浮按钮
   * 两个动作各干各的：「存岗位」抓详情页入库，「筛岗位」在列表页按画像挑。
   *
   * **2026-10-12 起不再本地判断页面形态**：链接正则 + 结构兜底那套在真实门户上
   * 反复踩空（链接形态千奇百怪，ul>li 又凑不满同一父节点），用户明明在列表页
   * 却被告知"认不出来"。现在本地只做无差别收割候选行，
   * 是不是列表页由服务端问模型定。
   *
   * 按钮的先后顺序因此不再靠"猜"，而是**跟着你上一次成功的动作走**：
   * 筛成功过就把「筛岗位」放上面，存成功过就把「存岗位」放上面，否则一律平等。
   */
  var FILTER_ITEM_LIMIT = 200;     /* 「整页全送」的上限，跟服务端 FILTER_MAX_ITEMS 对齐 */

  /** 刷新 dock 的顺序与高亮。hint = null 时两个按钮平等，谁也不高亮 */
  function syncFab() {
    var b = document.getElementById('jcFloatingBtn');
    if (b) applyFabMode(b, state.hint || null);
  }

  /** 只管排顺序和高亮：两个按钮永远留在 dock 里，绝不按页面形态藏掉任何一个 */
  function applyFabMode(b, hint) {
    var cap = b.querySelector('.jc-dock-capture');
    var fil = b.querySelector('.jc-dock-filter');
    if (cap && fil && cap.parentNode === b) {
      var first = hint === 'list' ? fil : cap;
      var second = hint === 'list' ? cap : fil;
      if (b.firstChild !== first) b.insertBefore(first, b.firstChild);
      if (b.lastChild !== second) b.appendChild(second);
    }
    if (fil) {
      fil.title = '按你的求职目标筛出本页合适的岗位（Alt+Shift+F）';
      fil.classList[hint === 'list' ? 'add' : 'remove']('jc-dock-primary');
    }
    if (cap) {
      cap.title = '抓取本页岗位到求职工作台（Alt+Shift+J）';
      cap.classList[hint === 'detail' ? 'add' : 'remove']('jc-dock-primary');
    }
    b.title = '拖动可移动，右键可临时隐藏\n构建 ' + JC_BUILD;
  }

  function buildButton() {
    if (document.getElementById('jcFloatingBtn')) return;

    var btn = h('div', {
      id: 'jcFloatingBtn',
      class: 'jc-fab',
      role: 'toolbar',
      title: '岗位速存',
    }, []);

    btn.appendChild(dockBtn('capture', '存岗位',
      'M19 21l-7-4-7 4V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z',      /* 书签：存 */
      function () { startCapture({ origin: 'button' }); }));
    btn.appendChild(dockBtn('filter', '筛岗位',
      'M3 5h18l-7 8v6l-4 2v-8z',                                /* 漏斗：筛 */
      function () { startFilter({ origin: 'button' }); }));

    var pos = readPos();
    btn.style.left = pos.left + 'px';
    btn.style.top = pos.top + 'px';

    /* 拖动与点击共用一套 pointer 事件：位移没超过阈值就当点击，超过才当拖动。
     * 关键：setPointerCapture 只能在「真的开始拖了」之后调用 —— 一按下就抓指针，
     * 浏览器会把随后的 click 改送到容器上，里面那个按钮就再也收不到点击，
     * 用户看到的就是「点了没反应」（血债：dock 化之后踩过一次）。 */
    var drag = { active: false, moved: false, sx: 0, sy: 0, ox: 0, oy: 0 };
    btn.addEventListener('pointerdown', function (e) {
      if (e.button !== 0) return;
      drag.active = true; drag.moved = false;
      /* 每次按下都先解禁点击：上一轮拖完留下的标记如果没被 click 消费掉，
       * 会一直挡着，症状同样是「点了没反应」 */
      state.draggingFab = false;
      drag.sx = e.clientX; drag.sy = e.clientY;
      var r = btn.getBoundingClientRect();
      drag.ox = r.left; drag.oy = r.top;
    });
    btn.addEventListener('pointermove', function (e) {
      if (!drag.active) return;
      var dx = e.clientX - drag.sx, dy = e.clientY - drag.sy;
      if (!drag.moved && Math.abs(dx) + Math.abs(dy) < 4) return;
      if (!drag.moved) {
        drag.moved = true;
        btn.classList.add('jc-dragging');
        try { btn.setPointerCapture && btn.setPointerCapture(e.pointerId); } catch (err) {}
      }
      var left = clamp(drag.ox + dx, 4, window.innerWidth - FAB_W);
      var top = clamp(drag.oy + dy, 4, window.innerHeight - FAB_H);
      btn.style.left = left + 'px';
      btn.style.top = top + 'px';
      btn.style.right = 'auto';
      btn.style.bottom = 'auto';
    });
    btn.addEventListener('pointerup', function (e) {
      if (!drag.active) return;
      drag.active = false;
      btn.classList.remove('jc-dragging');
      try { btn.releasePointerCapture && btn.releasePointerCapture(e.pointerId); } catch (err) {}
      if (drag.moved) {
        var r = btn.getBoundingClientRect();
        writePos({ left: Math.round(r.left), top: Math.round(r.top) });
      }
      /* 拖完那一下的 click 还会落到按钮上，用这个标记挡掉。
       * 不用 setTimeout 复位 —— 它有可能赶在 click 之前跑完，等于没挡住；
       * 改为下次按下时解禁 + 容器上的 click 兜底复位两条路。 */
      state.draggingFab = drag.moved;
    });
    btn.addEventListener('pointercancel', function () {
      drag.active = false;
      btn.classList.remove('jc-dragging');
      state.draggingFab = false;
    });
    /* 兜底复位：click 一定会冒到容器上，比只靠 setTimeout 稳 */
    btn.addEventListener('click', function () { state.draggingFab = false; });
    btn.addEventListener('contextmenu', function (e) {
      e.preventDefault();
      hideButton();
    });

    document.documentElement.appendChild(btn);
    /* 常驻：浮标不再记"隐藏状态"。老用户以前右键隐藏过（localStorage 里留了 1），
       这里直接把那条记录删掉，否则改完版本还是看不见按钮。 */
    try { localStorage.removeItem(BTN_HIDDEN_KEY); } catch (e) {}
    return btn;
  }

  /** 标记 dock 上某个按钮「正在干活」：图标转起来，另一个按钮不受影响 */
  function setFabBusy(kind) {
    var btn = document.getElementById('jcFloatingBtn');
    if (!btn) return;
    if (btn.classList) btn.classList.add('jc-busy');
    var el = btn.querySelector('.jc-dock-' + kind);
    if (el && el.classList) el.classList.add('jc-dock-busy');
  }

  /** kind 省略时两个都清；给了就只清那一个（抓取和筛选理论上不会同时跑，但别互相踩） */
  function clearFabBusy(kind) {
    var btn = document.getElementById('jcFloatingBtn');
    if (!btn) return;
    var kinds = kind ? [kind] : ['capture', 'filter'];
    var busy = 0;
    kinds.forEach(function (k) {
      var el = btn.querySelector('.jc-dock-' + k);
      if (el && el.classList) el.classList.remove('jc-dock-busy');
    });
    ['capture', 'filter'].forEach(function (k) {
      var el = btn.querySelector('.jc-dock-' + k);
      if (el && el.classList && el.classList.contains('jc-dock-busy')) busy++;
    });
    if (!busy && btn.classList) btn.classList.remove('jc-busy');
  }

  /** dock 里的一个动作按钮：图标 + 三个字的标签，点击做事（拖动由外层容器负责） */
  function dockBtn(kind, label, pathD, onClick) {
    var b = h('div', {
      class: 'jc-dock-btn jc-dock-' + kind,
      role: 'button',
      tabindex: '0',
    }, [
      h('svg', { viewBox: '0 0 24 24', width: '16', height: '16', fill: 'none', stroke: 'currentColor', 'stroke-width': '2', 'stroke-linecap': 'round', 'stroke-linejoin': 'round' }, [
        h('path', { d: pathD }),
      ]),
      h('span', { class: 'jc-dock-text', text: label }),
    ]);
    /* 拖动结束的那次 pointerup 会落在按钮上，靠 drag.moved 挡掉误触；
       这里只用 click，避免和 pointer 事件重复触发两次动作。 */
    b.addEventListener('click', function () {
      if (state.draggingFab) return;
      onClick();
    });
    /* 键盘也能用（tabindex=0）：Tab 聚焦后回车/空格 */
    b.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' || e.key === ' ' || e.key === 'Spacebar') {
        e.preventDefault();
        onClick();
      }
    });
    return b;
  }

  function clamp(v, a, b) { return Math.max(a, Math.min(b, v)); }

  /* dock 是两个按钮竖排，高度约 80 —— 老的单颗药丸是 40，夹取边界要跟着改，
   * 否则拖到屏幕底部时下半截会被切掉 */
  var FAB_W = 92, FAB_H = 84;

  function readPos() {
    var d = { left: window.innerWidth - FAB_W - 16, top: window.innerHeight * 0.42 };
    try {
      var raw = localStorage.getItem(BTN_POS_KEY);
      if (raw) {
        var p = JSON.parse(raw);
        if (typeof p.left === 'number') d.left = clamp(p.left, 4, Math.max(4, window.innerWidth - FAB_W));
        if (typeof p.top === 'number') d.top = clamp(p.top, 4, Math.max(4, window.innerHeight - FAB_H));
      }
    } catch (e) { /* 位置读不出来就用默认位置，不影响功能 */ }
    return d;
  }
  function writePos(p) { try { localStorage.setItem(BTN_POS_KEY, JSON.stringify(p)); } catch (e) {} }
  /* 隐藏只管当前这一页（不落 localStorage）：按钮要常驻，换页/刷新就应该回来 */
  function hideButton() {
    var b = document.getElementById('jcFloatingBtn');
    if (b) b.style.display = 'none';
    toast('本页已隐藏浮标，刷新或换页后会自动回来', 'info', 3200);
  }
  function showButton() {
    var b = document.getElementById('jcFloatingBtn');
    if (!b) buildButton();
    else b.style.display = '';
    syncFab();
  }

  /* --------------------------------------------------------------- Toast */
  var toastTimer = null;
  function toast(msg, type, ms) {
    var box = document.getElementById('jcToast');
    if (!box) {
      box = h('div', { id: 'jcToast', class: 'jc-toast' });
      document.documentElement.appendChild(box);
    }
    box.className = 'jc-toast ' + (type || 'info') + ' jc-show';
    box.textContent = msg;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { box.className = 'jc-toast ' + (type || 'info'); }, ms || 2600);
  }

  /* --------------------------------------------------------- 抓取失败自检面板
   * 「连不上 / 请求被中断」是插件最难受的故障：可能是工作台没开，也可能是
   * 扩展没重载、代理拦了 127.0.0.1、请求超时……光一句 toast 根本分不清。
   * 这里把现场信息一次摆全：两个构建号、已连地址、端口、耗时、原始报错。
   */
  function showConnHelp(diag) {
    diag = diag || {};
    var old = document.getElementById('jcConnHelp');
    if (old && old.parentNode) old.parentNode.removeChild(old);

    var wb = state.workbench || {};
    var probe = diag.probe || {};
    var resp = diag.resp || {};
    var isAbort = diag.code === 'aborted';

    var rows = [
      ['页面脚本', JC_BUILD],
      ['后台脚本', resp.bgBuild || probe.bgBuild || '(未更新，需重载扩展)'],
      ['工作台地址', wb.ok === true ? (wb.base || '(已连上)') : '未连上'],
      ['监听端口', wb.port ? String(wb.port) : '(探测不到)'],
      ['已等待', resp.ms ? (resp.ms + ' ms') : '(未知)'],
      ['卡在阶段', resp.stage || '(未知)'],
      ['原始报错', diag.why || '(无)'],
    ];

    var steps = isAbort ? [
      '这是「请求发出去了但被中断」，不是连不上 —— 地址和端口能显示，说明探测是通的。',
      '最常见原因：后台脚本（background.js）还是旧版。去 chrome://extensions/ 点「重新加载」，然后 F5。',
      '看上面「后台脚本」一行：显示「未更新，需重载扩展」就是这个问题，重载后应显示 bg- 开头的版本号。',
      '若重载后仍中断，看「已等待」毫秒数：如果是 4000 上下，说明后台仍是旧代码；如果是 90000，是服务端模型太慢。',
    ] : [
      '工作台窗口还开着吗？关掉就没法转了 —— 重新运行 求职工作台/server.py。',
      '刚 reload 过扩展吗？改完代码必须在 chrome://extensions/ 点「重新加载」，然后 F5 刷新本页。',
      '浏览器/系统代理有没有把 127.0.0.1 也算进去？把 127.0.0.1, localhost 加进代理白名单。',
      '地址栏直接开 http://127.0.0.1:8765/api/health，能看到 {"ok":true,...} 就说明服务是活的。',
    ];

    var card = h('div', { class: 'jc-connhelp-card' }, [
      h('div', {
        class: 'jc-connhelp-title',
        text: isAbort ? '请求被中断' : '连不上本机工作台',
      }),
      h('div', {
        class: 'jc-connhelp-sub',
        text: isAbort ? '地址端口都通，是同一次请求中途断了 —— 先看「后台脚本」那行' : '按下面顺序看一遍，多数是前两条',
      }),
    ]);

    var tbl = h('div', { class: 'jc-connhelp-tbl' });
    rows.forEach(function (r) {
      tbl.appendChild(h('div', { class: 'jc-connhelp-row' }, [
        h('span', { class: 'jc-connhelp-k', text: r[0] }),
        h('span', { class: 'jc-connhelp-v', text: r[1] }),
      ]));
    });
    card.appendChild(tbl);

    var ol = h('div', { class: 'jc-connhelp-steps' });
    steps.forEach(function (s, i) {
      ol.appendChild(h('div', { class: 'jc-connhelp-step' }, [
        h('span', { class: 'jc-connhelp-num', text: String(i + 1) }),
        h('span', { class: 'jc-connhelp-txt', text: s }),
      ]));
    });
    card.appendChild(ol);

    card.appendChild(h('div', { class: 'jc-connhelp-foot' }, [
      h('button', {
        class: 'jc-btn', text: '重新探测',
        onclick: function () {
          sendBg({ type: 'JC_PROBE' }, function (r) {
            if (r && r.ok) {
              state.workbench = r;
              toast('连上了：' + r.base, 'success', 4000);
              var b = document.getElementById('jcConnHelp');
              if (b && b.parentNode) b.parentNode.removeChild(b);
            } else {
              toast('还是连不上：' + ((r && r.error) || '未知原因'), 'error', 6000);
              showConnHelp({ why: (r && r.error) || diag.why, code: 'network', probe: r || {} });
            }
          });
        },
      }),
      h('button', {
        class: 'jc-btn', text: '复制诊断',
        onclick: function () {
          var txt = rows.map(function (r) { return r[0] + '：' + r[1]; }).join('\n');
          try {
            if (navigator.clipboard) navigator.clipboard.writeText(txt);
            toast('诊断信息已复制', 'success', 2600);
          } catch (e) { toast('复制失败，请手动选中', 'error', 3000); }
        },
      }),
      h('button', {
        class: 'jc-btn jc-btn-ghost', text: '关闭',
        onclick: function () {
          var b = document.getElementById('jcConnHelp');
          if (b && b.parentNode) b.parentNode.removeChild(b);
        },
      }),
    ]));

    var wrap = h('div', { id: 'jcConnHelp', class: 'jc-connhelp' }, [card]);
    document.documentElement.appendChild(wrap);
  }

  /* --------------------------------------------------------------- 抓取入口
   * 只有 AI 一条路：整页文本 → 工作台 → 大模型 → 一次性出全部字段。
   * 本地规则匹配已全部删除，没有兜底、没有第二套口径。
   */
  function startCapture(opts) {
    opts = opts || {};
    if (state.capturing || state.aiCapturing) return;
    fullCapture({ origin: opts.origin || 'button', silent: false });
  }

  /* --------------------------------------------------------------- AI 全量识别
   * 规则那套不参与：整页文本 → 工作台 → 大模型 → 一次性出全部字段。
   * 代价是每次 5~20 秒且每次都烧 token；换来的是不再受"规则认不出的排版"限制。
   */
  var AI_WAIT_TIP = [
    '正在把整页文本摊开读一遍…',
    '正在抽岗位名与企业名…',
    '正在抽地点、薪资、学历…',
    '正在把 JD 原文挑出来…',
    '快好了，页面内容多的话会慢一点…',
  ];

  function showAiWait(tipIndex) {
    var box = document.getElementById('jcAiWait');
    if (!box) {
      box = h('div', { id: 'jcAiWait', class: 'jc-aiwait' }, [
        h('div', { class: 'jc-aiwait-card' }, [
          h('div', { class: 'jc-aiwait-spin' }),
          h('div', { class: 'jc-aiwait-title', text: '正在解析本页岗位' }),
          h('div', { class: 'jc-aiwait-tip', id: 'jcAiWaitTip', text: AI_WAIT_TIP[0] }),
          h('div', { class: 'jc-aiwait-sub', text: '整页一次性抽完，通常 5～20 秒' }),
          h('button', {
            class: 'jc-btn', id: 'jcAiWaitCancel', text: '取消',
            onclick: function () {
              if (state.aiFullToken) state.aiFullToken.cancelled = true;
              hideAiWait();
              toast('已取消识别', 'info');
            },
          }),
        ]),
      ]);
      document.documentElement.appendChild(box);
    }
    var tip = document.getElementById('jcAiWaitTip');
    if (tip) tip.textContent = AI_WAIT_TIP[(tipIndex || 0) % AI_WAIT_TIP.length];
  }

  function hideAiWait() {
    var box = document.getElementById('jcAiWait');
    if (box && box.parentNode) box.parentNode.removeChild(box);
    if (aiWaitTimer) { clearInterval(aiWaitTimer); aiWaitTimer = null; }
  }
  var aiWaitTimer = null;

  /**
   * fullCapture(opts)
   *   识别只有 AI 一条路。opts.origin 只用于埋点，opts.silent 控制是否弹提示。
   */
  function fullCapture(opts) {
    opts = opts || {};
    if (state.aiCapturing) return;

    var pageText = '';
    try { pageText = JobParser.aiCondenseText({ maxLen: 16000 }); } catch (e) { pageText = ''; }
    if (!pageText || pageText.length < 120) {
      if (!opts.silent) toast('这页能抓到的文本太少（' + pageText.length + ' 字），没料可依', 'error', 4200);
      return;
    }

    var token = { cancelled: false };
    state.aiFullToken = token;
    state.aiCapturing = true;
    setFabBusy('capture');

    var tipIdx = 0;
    showAiWait(0);
    aiWaitTimer = setInterval(function () {
      tipIdx++;
      if (document.getElementById('jcAiWait')) showAiWait(tipIdx);
    }, 3200);

    sendBg({
      type: 'JC_AI_COMPLETE',
      mode: 'full',
      pageText: pageText,
    }, function (resp) {
      state.aiCapturing = false;
      clearFabBusy();
      hideAiWait();
      if (token.cancelled) return;

      if (!resp || !resp.ok) {
        var code = (resp && resp.code) || '';
        if (code === 'disabled' || code === 'no_key') {
          toast('识别服务还没配好 —— 现在抓取全靠它，先去配置一下', 'error', 5200);
          if (!opts.silent) openAiSettings();
          return;
        }
        if (code === 'network' || code === 'aborted') {
          var why = (resp && resp.error) || '';
          /* aborted 和 network 完全不同：前者是请求发出去了但被中断，
             后者是根本发不出去。文案必须分开，否则一直查错方向。 */
          var head = (code === 'aborted') ? '请求被中断' : '连不上工作台';
          toast(head + '：' + (why || '原因不明'), 'error', 9000);
          /* 先把现场事实探一次，面板里的端口/地址才是当场的 */
          sendBg({ type: 'JC_PROBE' }, function (pr) {
            state.workbench = (pr && pr.ok)
              ? { ok: true, base: pr.base, port: pr.port }
              : { ok: false, base: '', port: null, error: (pr && pr.error) || '' };
            showConnHelp({
              why: why,
              code: code,
              resp: resp || {},
              probe: pr || {},
            });
          });
          return;
        }
        toast('识别失败：' + ((resp && resp.error) || '未知错误'), 'error', 6500);
        return;
      }

      var cap = resp.capture || {};
      var f = cap.fields || {};
      if (!f.title && !f.company) {
        toast('没能抽到岗位名和企业名，这页可能不是岗位详情页', 'error', 6000);
        return;
      }
      /* 投递链接不信 AI 抽的（经常幻觉或指向首页），直接固定为当前页面 URL，
         用户在面板里仍可手改 */
      f.link = location.href;

      /* AI 结果当成一次正常抓取结果走面板；meta/confidence 已由服务端补好 */
      var result = {
        ok: true,
        version: 2,
        fields: f,
        confidence: cap.confidence || { fields: {}, overall: 0, missing: [] },
        key: JobParser.dedupeKey(f.company, f.title),
        id: JobParser.recordId(f.company, f.title),
        meta: cap.meta || {},
      };
      result.meta.aiMode = 'full';
      result.meta.aiProvider = resp.provider || '';
      result.meta.aiModel = resp.model || '';

      state.current = result;
      sendBg({ type: 'JC_LIST_JOBS' }, function (r2) {
        state.workbench = (r2 && r2.workbench) || { ok: false, error: '后台无响应' };
        state.dup = (r2 && r2.ok && r2.jobs)
          ? JobParser.checkDuplicates(result, r2.jobs)
          : { level: 'unknown', matches: [], error: (r2 && r2.error) || '' };
        openPanel(result, {});
        toast('字段已自动填好，请核对后保存', 'success', 4200);
      });
    });
  }

  /* ------------------------------------------------------- 实时预览卡
   * 形状刻意跟工作台「岗位管理」里的那一行对齐：
   *   [插件角标] 岗位名
   *   城市 · 薪资 · 学历
   *   重要程度 pill | 标签 | 录入时间
   * 下面再补一张关键字段小表和 JD 摘要。
   * 目的是让用户在点保存前就看清楚「这张卡进了工作台长什么样」，
   * 而不是对着一堆输入框猜。
   */
  var IMP_STYLE = {
    focus: ['jc-pv-pill-focus', '重点关注'],
    can:   ['jc-pv-pill-can',   '可以考虑'],
    hold:  ['jc-pv-pill-hold',  '暂时搁置'],
    skip:  ['jc-pv-pill-skip',  '不考虑'],
  };

  function pvMetaLine(f) {
    var bits = [];
    if (f.city) bits.push(f.city);
    if (f.salary) bits.push(f.salary);
    if (f.education) bits.push(f.education);
    return bits;
  }

  function buildPreview(fields, conf, meta) {
    var wrap = h('div', { class: 'jc-preview-wrap' });
    wrap.appendChild(h('div', { class: 'jc-preview-head' }, [
      document.createTextNode('存入工作台后长这样'),
      h('span', { class: 'jc-preview-note', text: '随下方修改实时更新' }),
    ]));

    var body = h('div', { class: 'jc-preview-card', id: 'jcPvBody' });
    wrap.appendChild(body);
    /* 先把静态骨架搭好，refreshPreview 只改里面的文字 */
    renderPreviewInto(body, fields, conf, meta);
    return wrap;
  }

  function renderPreviewInto(body, fields, conf, meta) {
    body.innerHTML = '';

    body.appendChild(h('div', { class: 'jc-preview-tr-head' }, [
      h('span', { class: 'jc-preview-c-title', text: '岗位名' }),
      h('span', { class: 'jc-preview-c-imp', text: '重要程度' }),
      h('span', { class: 'jc-preview-c-tags', text: '标签' }),
      h('span', { class: 'jc-preview-c-time', text: '录入时间' }),
    ]));

    /* --- 岗位名行 --- */
    var titleCell = h('div', { class: 'jc-preview-c-title' });
    var titleLine = h('div');
    var badgeText = '插件';
    var badgeTitle = '由浏览器插件抓取'
      + (meta && meta.host ? ' · 来源 ' + meta.host : '')
      + (conf && conf.overall ? ' · 识别度 ' + conf.overall + '%' : '');
    titleLine.appendChild(h('span', { class: 'jc-pv-badge', text: badgeText, title: badgeTitle }));
    if (fields.title) {
      titleLine.appendChild(h('span', { class: 'jc-pv-title', text: fields.title }));
    } else {
      titleLine.appendChild(h('span', { class: 'jc-pv-title jc-pv-placeholder', text: '（还没填岗位名）' }));
    }
    titleCell.appendChild(titleLine);

    var bits = pvMetaLine(fields);
    if (bits.length) {
      var metaLine = h('div', { class: 'jc-pv-meta' });
      bits.forEach(function (b, i) {
        if (i) metaLine.appendChild(h('span', { class: 'jc-pv-sep', text: '·' }));
        metaLine.appendChild(document.createTextNode(b));
      });
      titleCell.appendChild(metaLine);
    }

    /* --- 重要程度 --- */
    var impKey = fields.importance || 'can';
    var impDef = IMP_STYLE[impKey] || IMP_STYLE.can;
    var impCell = h('div', { class: 'jc-preview-c-imp' }, [
      h('span', { class: 'jc-pv-pill ' + impDef[0], text: impDef[1] }),
    ]);

    /* --- 标签 --- */
    var tagCell = h('div', { class: 'jc-preview-c-tags' });
    var tags = (fields.tags || []).filter(Boolean);
    if (tags.length) {
      tags.slice(0, 4).forEach(function (t) {
        tagCell.appendChild(h('span', { class: 'jc-pv-tag', text: String(t) }));
      });
    } else {
      tagCell.appendChild(h('span', { class: 'jc-pv-empty', text: '—' }));
    }

    /* --- 录入时间 --- */
    var timeCell = h('div', { class: 'jc-preview-c-time', text: (meta && meta.extractedAt ? meta.extractedAt.slice(0, 10) : todayStr()) });

    body.appendChild(h('div', { class: 'jc-preview-tr' }, [titleCell, impCell, tagCell, timeCell]));

    /* --- 关键字段小表 --- */
    var kvRows = [
      ['企业', fields.company],
      ['工作地点', fields.city],
      ['薪资', fields.salary],
      ['学历', fields.education],
      ['经验', fields.experience],
      ['部门', fields.department],
      ['招聘人数', fields.headcount],
      ['岗位性质', fields.jobType],
      ['投递截止', fields.deadline],
      ['投递链接', fields.link],
    ].filter(function (r) { return r[1]; });

    if (kvRows.length) {
      var dl = h('dl', { class: 'jc-preview-kv' });
      kvRows.forEach(function (r) {
        dl.appendChild(h('dt', { text: r[0] }));
        dl.appendChild(h('dd', { title: String(r[1]), text: String(r[1]) }));
      });
      body.appendChild(dl);
    }

    /* --- JD 摘要 --- */
    if (fields.jd) {
      var jdBox = h('div', { class: 'jc-preview-jd' });
      jdBox.appendChild(h('span', { class: 'jc-preview-jd-label', text: 'JD 摘要（工作台里可展开看全文）' }));
      jdBox.appendChild(document.createTextNode(summarizeJd(fields.jd, 180)));
      body.appendChild(jdBox);
    }
  }

  function summarizeJd(text, max) {
    var t = String(text || '').replace(/\s+/g, ' ').trim();
    if (t.length <= max) return t;
    /* 尽量在标点处断开，不要切在半个词中间 */
    var cut = t.slice(0, max);
    var m = cut.match(/^[\s\S]*[。；;.!！?？]/);
    if (m && m[0].length > max * 0.6) return m[0] + ' …';
    return cut + '…';
  }

  function todayStr() {
    var d = new Date();
    function p(n) { return (n < 10 ? '0' : '') + n; }
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate());
  }

  /* 输入框一变就重画预览；不要动输入框本身，否则会打断中文输入法 */
  function refreshPreview() {
    var body = el('jcPvBody');
    if (!body || !state.panel) return;
    var inputs = state.inputs || {};
    function val(k) { return inputs[k] ? String(inputs[k].value || '').trim() : ''; }
    var base = state.current || {};
    var fields = {};
    Object.keys(base.fields || {}).forEach(function (k) { fields[k] = base.fields[k]; });
    ['title', 'company', 'salary', 'city', 'education', 'experience', 'deadline', 'link', 'department', 'headcount', 'jobType'].forEach(function (k) {
      fields[k] = val(k);
    });
    var jdEl = el('jcJd');
    if (jdEl) fields.jd = String(jdEl.value || '');
    renderPreviewInto(body, fields, base.confidence || {}, base.meta || {});
  }

  /* --------------------------------------------------------------- 确认面板 */
  function openPanel(result, opts) {
    opts = opts || {};
    closePanel();
    var fields = (result && result.fields) || {};
    var conf = (result && result.confidence) || { fields: {}, overall: 0, missing: [] };
    var meta = (result && result.meta) || {};

    /* 换了一家/一个岗位，上次那份解读就不该再挂着 —— 按钮也要退回「解读」措辞 */
    if (state.insight &&
        (state.insight.company !== fields.company || state.insight.title !== fields.title)) {
      state.insight = null;
    }

    var panel = h('div', { id: 'jcPanelOverlay', class: 'jc-overlay' });
    var card = h('div', { class: 'jc-panel', role: 'dialog' });

    /* --- 头部 --- */
    card.appendChild(h('div', { class: 'jc-head' }, [
      h('div', { class: 'jc-head-main' }, [
        h('div', { class: 'jc-head-title' }, [
          h('strong', { text: opts.empty ? '没能识别出岗位信息' : '确认岗位信息' }),
          !opts.empty && h('span', { class: 'jc-conf jc-conf-ai', text: '自动识别' }),
        ]),
        h('div', { class: 'jc-head-sub', text: '整页文本一次性抽取'
          + (meta.aiProvider ? ' · ' + meta.aiProvider : '')
          + (meta.aiModel ? ' ' + meta.aiModel : '')
          + (meta.host ? ' · ' + meta.host : '') }),
      ]),
      h('button', { class: 'jc-x', text: '✕', title: '关闭', onclick: closePanel }),
    ]));

    /* --- 工作台连通性提示 --- */
    var wb = state.workbench || {};
    if (wb.ok === false) {
      card.appendChild(h('div', { class: 'jc-warn' }, [
        h('strong', { text: '⚠ 没连上求职工作台。' }),
        h('span', { text: (wb.error ? ' ' + wb.error + ' ' : ' ') + '请先在本机运行 求职工作台/启动工作台.bat，再点「重试」。抓到的内容会暂存在这里，不会丢。' }),
        h('button', { class: 'jc-btn jc-btn-ghost', text: '重试', onclick: function () { retryWorkbench(this); } }),
      ]));
    }

    /* --- 重复检测 --- */
    if (state.dup && state.dup.level === 'exact') {
      card.appendChild(h('div', { class: 'jc-warn jc-warn-dup' }, [
        h('strong', { text: '⚠ 工作台里已经有这个岗位了：' }),
        h('span', { text: (state.dup.matches[0] ? state.dup.matches[0].job.company + ' · ' + state.dup.matches[0].job.title + '（' + state.dup.matches[0].reason + '）' : '') }),
        h('div', { class: 'jc-dup-actions' }, [
          h('button', { class: 'jc-btn jc-btn-ghost', text: '打开工作台看看', onclick: function () { sendBg({ type: 'JC_OPEN_WORKBENCH' }); } }),
          h('button', { class: 'jc-btn jc-btn-ghost', text: '仍然更新它', onclick: function () { state.forceUpdate = true; toast('保存时将覆盖已有卡片', 'info'); } }),
        ]),
      ]));
    } else if (state.dup && state.dup.level === 'similar') {
      card.appendChild(h('div', { class: 'jc-warn jc-warn-sim' }, [
        h('strong', { text: '可能重复：' }),
        h('span', { text: state.dup.matches.map(function (m) { return m.job.company + ' · ' + m.job.title; }).join('；') }),
      ]));
    }

    /* --- 实时预览：按工作台「岗位管理」里那条行的真实长相画一张卡 --- */
    var previewWrap = buildPreview(fields, conf, meta);
    card.appendChild(previewWrap);

    /* --- 字段区 --- */
    var form = h('div', { class: 'jc-form' });
    var ROWS = [
      ['title', '岗位名', 'text', true],
      ['company', '企业', 'text', true],
      ['salary', '薪资', 'text', false],
      ['city', '工作地点', 'text', false],
      ['education', '学历要求', 'text', false],
      ['experience', '经验要求', 'text', false],
      ['deadline', '投递截止', 'text', false],
      ['link', '投递链接', 'text', false],
    ];
    var inputs = {};
    ROWS.forEach(function (r) {
      var key = r[0], label = r[1], req = r[3];
      var score = (conf.fields && conf.fields[key]) || 0;
      var band = score === 0 ? 'empty' : (score < 60 ? 'low' : 'ok');
      var row = h('div', { class: 'jc-row jc-row-' + band });
      row.appendChild(h('label', { class: 'jc-label' }, [
        document.createTextNode(label),
        req ? h('i', { class: 'jc-req', text: '*' }) : null,
        h('span', { class: 'jc-score jc-score-' + band, text: score ? score + '%' : '未识别', title: '识别置信度，低于 60% 请人工核对' }),
      ]));
      var inp = h('input', { class: 'jc-input', type: 'text', value: fields[key] || '', placeholder: '未识别，可手动填写' });
      inp.addEventListener('input', function () { inputs[key].dirty = true; refreshPreview(); });
      inputs[key] = inp;
      row.appendChild(inp);
      form.appendChild(row);
    });

    /* 部门 / 人数 / 性质：次要字段折起来，默认不占地方 */
    var extraRows = h('div', { class: 'jc-extra', style: 'display:none' });
    [['department', '部门'], ['headcount', '招聘人数'], ['jobType', '岗位性质']].forEach(function (r) {
      var row = h('div', { class: 'jc-row' });
      row.appendChild(h('label', { class: 'jc-label', text: r[1] }));
      var inp = h('input', { class: 'jc-input', type: 'text', value: fields[r[0]] || '' });
      inp.addEventListener('input', refreshPreview);
      inputs[r[0]] = inp;
      row.appendChild(inp);
      extraRows.appendChild(row);
    });
    form.appendChild(extraRows);

    /* JD */
    var jdRow = h('div', { class: 'jc-row jc-row-jd' });
    var jdScore = (conf.fields && conf.fields.jd) || 0;
    jdRow.appendChild(h('label', { class: 'jc-label' }, [
      document.createTextNode('JD 原文'),
      h('span', { class: 'jc-score jc-score-ok', text: fields.jd ? (fields.jd.length + ' 字') : '未识别' }),
      h('button', { class: 'jc-mini', text: '切换', title: '切换 JD 编辑/预览', onclick: function () {
        var ta = el('jcJd'), pv = el('jcJdPreview');
        if (!ta || !pv) return;
        if (ta.style.display === 'none') { ta.style.display = ''; pv.style.display = 'none'; }
        else { ta.style.display = 'none'; pv.style.display = ''; }
      } }),
    ]));
    var ta = h('textarea', { class: 'jc-input jc-textarea', id: 'jcJd', rows: '7' });
    ta.value = fields.jd || '';
    ta.addEventListener('input', refreshPreview);
    var pv = h('div', { class: 'jc-jd-preview', id: 'jcJdPreview', style: 'display:none', text: fields.jd || '（空）' });
    jdRow.appendChild(ta);
    jdRow.appendChild(pv);
    form.appendChild(jdRow);

    if (conf.missing && conf.missing.length) {
      form.appendChild(h('div', { class: 'jc-hint', text: '未识别字段：' + conf.missing.join('、') + '。工作台卡片允许留空，也可以现在补上。' }));
    }
    card.appendChild(form);

    /* --- 底部操作 ---
     * 抓取只有自动抽取一条路，所以这里没有"补全"按钮（无从补起 —— 字段就这么来的）。
     * 三个动作分工：解读这家公司/岗位 → 重新解析 → 保存。 */
    var importTo = h('input', { type: 'checkbox', id: 'jcImportPool', checked: 'checked' });
    var foot = h('div', { class: 'jc-foot' }, [
      h('label', { class: 'jc-check', title: '同时写入「招聘信息」候选池，之后可以在工作台里点「纳入」正式建卡' }, [importTo, h('span', { text: '同时写入招聘信息' })]),
      h('span', { class: 'jc-foot-gap' }),
      h('button', {
        class: 'jc-btn jc-btn-ai',
        id: 'jcInsightBtn',
        text: state.insight ? '查看公司与岗位解读' : '公司与岗位解读',
        title: '查这家公司的底细，并按 JD 拆出它想要什么样的人、简历和面试怎么准备',
        onclick: function () { runInsight(false); },
      }),
      h('button', {
        class: 'jc-btn jc-btn-ai-ghost',
        id: 'jcAiFullBtn',
        text: '重新解析一遍',
        title: '把整页文本再抽一次。这次漏掉/抽错的字段可能补上，但结果会整体替换当前面板内容。',
        onclick: function () {
          if (!confirm('重抽会覆盖面板里你现在改过的内容，继续？')) return;
          closePanel();
          state.current = null;
          fullCapture({});
        },
      }),
      h('button', { class: 'jc-btn jc-btn-primary', id: 'jcSave', text: opts.empty ? '手动填写后保存' : '保存到工作台', onclick: doSave }),
      h('button', { class: 'jc-btn', text: '取消', onclick: closePanel }),
    ]);
    card.appendChild(foot);

    panel.appendChild(card);
    panel.addEventListener('click', function (e) { if (e.target === panel) closePanel(); });
    document.addEventListener('keydown', onEsc);

    document.documentElement.appendChild(panel);
    state.panel = panel;
    state.inputs = inputs;

    /* focus 到第一个未识别或必填为空的字段，省一次鼠标移动 */
    var firstBad = ROWS.filter(function (r) { return !fields[r[0]]; })[0];
    var focusEl = inputs[(firstBad && firstBad[0]) || 'title'];
    if (focusEl) setTimeout(function () { focusEl.focus(); focusEl.select && focusEl.select(); }, 30);
  }

  /* ------------------------------------------------------- AI 设置（面板内弹层） */
  function openAiSettings() {
    sendBg({ type: 'JC_GET_AI_CONFIG' }, function (resp) {
      var cfg = (resp && resp.config) || null;
      var wrap = h('div', { class: 'jc-overlay', id: 'jcAiOverlay' });
      var box = h('div', { class: 'jc-panel jc-panel-sm', role: 'dialog' });
      box.appendChild(h('div', { class: 'jc-head' }, [
        h('div', { class: 'jc-head-main' }, [
          h('div', { class: 'jc-head-title' }, [h('strong', { text: '识别设置' })]),
          h('div', { class: 'jc-head-sub', text: '抓取和岗位解读都走这里，不配好用不了 · Key 存在本机工作台，不会进浏览器扩展' }),
        ]),
        h('button', {
          class: 'jc-x', text: '✕', title: '关闭',
          onclick: function () { if (wrap.parentNode) wrap.parentNode.removeChild(wrap); },
        }),
      ]));

      var f = h('div', { class: 'jc-form' });

      /* 启用开关 */
      var enabled = h('input', { type: 'checkbox', id: 'jcAiEnabled' });
      enabled.checked = !!(cfg && cfg.enabled);
      f.appendChild(h('div', { class: 'jc-row' }, [
        h('label', { class: 'jc-check' }, [enabled, h('span', { text: '启用岗位自动识别与解读' })]),
      ]));

      /* 平台选择 */
      var sel = h('select', { class: 'jc-select', id: 'jcAiProvider' });
      var providers = (cfg && cfg.providers) || {};
      Object.keys(providers).forEach(function (k) {
        var opt = h('option', { value: k, text: providers[k].name + '（' + (providers[k].model || '') + '）' });
        if (cfg && cfg.provider === k) opt.selected = true;
        sel.appendChild(opt);
      });
      f.appendChild(aiRow('平台', sel));
      var note = h('div', { class: 'jc-hint', id: 'jcAiNote', text: '' });
      f.appendChild(note);

      /* Key */
      var keyInp = h('input', {
        class: 'jc-input', id: 'jcAiKey', type: 'password',
        placeholder: cfg && cfg.hasKey ? ('已配置：' + (cfg.keyMasked || '')) : '粘贴 API Key',
      });
      f.appendChild(aiRow('API Key', keyInp));

      /* 模型 */
      var modelInp = h('input', {
        class: 'jc-input', id: 'jcAiModel',
        value: (cfg && cfg.model) || '', placeholder: '留空用平台默认',
      });
      f.appendChild(aiRow('模型', modelInp));

      /* 地址（高级） */
      var baseInp = h('input', {
        class: 'jc-input', id: 'jcAiBase',
        value: (cfg && cfg.base_url) || '', placeholder: '留空用平台默认',
      });
      f.appendChild(aiRow('接口地址', baseInp));

      var result = h('div', { class: 'jc-hint', id: 'jcAiResult', text: '' });
      f.appendChild(result);

      var foot = h('div', { class: 'jc-foot' }, [
        h('button', { class: 'jc-btn', text: '测试连接', onclick: doTestAi }),
        h('span', { class: 'jc-foot-gap' }),
        h('button', { class: 'jc-btn', text: '取消', onclick: function () { if (wrap.parentNode) wrap.parentNode.removeChild(wrap); } }),
        h('button', { class: 'jc-btn jc-btn-primary', text: '保存', onclick: doSaveAiSettings }),
      ]);
      box.appendChild(f);
      box.appendChild(foot);

      wrap.appendChild(box);
      wrap.addEventListener('click', function (e) { if (e.target === wrap && wrap.parentNode) wrap.parentNode.removeChild(wrap); });
      document.documentElement.appendChild(wrap);

      function refreshNote() {
        var k = sel.value;
        var p = providers[k] || {};
        note.textContent = (p.note || '') + (p.doc ? '　文档：' + p.doc : '');
        if (!modelInp.value) modelInp.placeholder = p.model || '平台默认';
      }
      sel.addEventListener('change', refreshNote);
      refreshNote();

      function doSaveAiSettings() {
        var patch = {
          ai_enabled: enabled.checked ? '1' : '0',
          ai_provider: sel.value,
          ai_model: modelInp.value.trim(),
          ai_base_url: baseInp.value.trim(),
        };
        /* Key 留空表示不动（避免误清空） */
        if (keyInp.value.trim()) patch.ai_api_key = keyInp.value.trim();
        sendBg({ type: 'JC_SET_AI_CONFIG', patch: patch }, function (r) {
          if (r && r.ok) {
            toast('设置已保存', 'success');
            if (wrap.parentNode) wrap.parentNode.removeChild(wrap);
          } else {
            result.className = 'jc-hint jc-hint-warn';
            result.textContent = '保存失败：' + ((r && r.error) || '未知错误');
          }
        });
      }

      function doTestAi() {
        result.className = 'jc-hint';
        result.textContent = '正在测试…';
        /* 先把当前填的值存下来，测试才测的是"你现在填的这套" */
        var patch = {
          ai_enabled: enabled.checked ? '1' : '0',
          ai_provider: sel.value,
          ai_model: modelInp.value.trim(),
          ai_base_url: baseInp.value.trim(),
        };
        if (keyInp.value.trim()) patch.ai_api_key = keyInp.value.trim();
        sendBg({ type: 'JC_SET_AI_CONFIG', patch: patch }, function () {
          sendBg({ type: 'JC_TEST_AI' }, function (r) {
            if (r && r.ok) {
              result.className = 'jc-hint jc-hint-ok';
              result.textContent = '连接正常　' + (r.provider || '') + ' · ' + (r.model || '')
                + (r.reply ? '　返回：' + r.reply : '');
            } else {
              result.className = 'jc-hint jc-hint-warn';
              result.textContent = '连接失败：' + ((r && r.error) || '未知错误');
            }
          });
        });
      }
    });
  }

  function aiRow(label, inputEl) {
    return h('div', { class: 'jc-row' }, [
      h('label', { class: 'jc-label', text: label }),
      inputEl,
    ]);
  }

  /* ----------------------------------------------------- 列表页：按画像筛岗位
   * 详情页走 fullCapture（想办法把一条岗位存准），列表页走这里（想办法知道
   * 这一页哪些值得点开）。分工：
   *   · 浏览器侧：把"哪些是岗位行"圈出来 + 粗略切字段（JobParser.extractJobList）
   *   · 服务端：拿工作台里的求职画像逐条比，给出 推荐/可选 和一句理由
   *   · 浏览器侧收尾：命中的行在页面上直接标出来 + 底部一条工具栏
   * 为什么要分两头：画像只在服务端，插件拿不到；而 DOM 只在浏览器里有。
   */
  var FILTER_WAIT_TIP = [
    '正在读你的目标方向与城市偏好…',
    '正在逐条比对每行里的关键词…',
    '正在给命中的岗位写理由…',
    '快了，这一页条数多的话会慢一点…',
  ];
  var filterTimer = null;

  function startFilter(opts) {
    opts = opts || {};
    if (state.filterBusy) return;
    clearListMarks();

    /* 本地只收割，不判断：链接形态和"这页是不是列表页"一律交给服务端问模型。
     * 这里哪怕捞到的是导航、页脚、相关推荐也没关系 —— 模型会自己挑掉。 */
    var items = [];
    try { items = JobParser.harvestRows(document, { limit: FILTER_ITEM_LIMIT }); } catch (e) { items = []; }
    if (!items || !items.length) {
      toast('这页连一行候选都没捞到（页面可能还没渲染完，或整页是图片/iframe）—— 等列表出来再点一次', 'error', 6500);
      return;
    }

    state.filterBusy = true;
    state.lastFilter = { items: items, picks: [] };
    showFilterWait(items.length);
    setFabBusy('filter');
    var tipIdx = 0;
    filterTimer = setInterval(function () {
      tipIdx++;
      var tip = document.getElementById('jcFilterTip');
      if (tip) tip.textContent = FILTER_WAIT_TIP[tipIdx % FILTER_WAIT_TIP.length];
    }, 3600);

    /* node/anchor 是 DOM 引用，不能过后台 —— 只送纯数据过去 */
    var payload = items.map(function (it) {
      return {
        title: it.title, company: it.company, salary: it.salary,
        education: it.education, experience: it.experience,
        text: it.text, link: it.link,
      };
    });

    sendBg({
      type: 'JC_AI_FILTER',
      items: payload,
      meta: { url: location.href, title: document.title, host: location.host },
    }, function (resp) {
      state.filterBusy = false;
      hideFilterWait();
      if (filterTimer) { clearInterval(filterTimer); filterTimer = null; }

      if (!resp || !resp.ok) {
        var code = (resp && resp.code) || '';
        if (code === 'no_profile') {
          toast('工作台里还没填求职目标 —— 先去「我的画像」把方向/城市/技能填上', 'error', 6500);
          return;
        }
        if (code === 'disabled' || code === 'no_key') {
          toast('识别服务还没配好，先去设置里启用并填 Key', 'error', 5200);
          openAiSettings();
          return;
        }
        toast('没能筛出来：' + ((resp && resp.error) || '未知原因'), 'error', 6500);
        return;
      }

      /* isList 是模型判的：这页压根不是列表页（多半是详情页/公司主页/导航页），
       * 别拿"0 条匹配"糊弄人 —— 那是两回事 */
      if (resp.isList === false) {
        toast('这页不像岗位列表页：' + (resp.listReason || '（模型没给理由）')
          + ' —— 详情页请用「存岗位」', 'error', 7000);
        return;
      }

      state.hint = 'list';
      syncFab();

      var picks = resp.picks || [];
      state.lastFilter.picks = picks;
      if (!picks.length) {
        toast('这 ' + (resp.total || items.length) + ' 条里没有对得上你目标的岗位', 'info', 5200);
        return;
      }
      markList(picks, resp.summary || '', {
        hit: resp.hit || 0, maybe: resp.maybe || 0, total: resp.total || items.length,
      });
      toast('筛出 ' + (resp.hit || 0) + ' 条推荐 · ' + (resp.maybe || 0) + ' 条可选（共 '
        + (resp.total || items.length) + ' 条），已在页面标出', 'success', 6000);
    });
  }

  function showFilterWait(count) {
    hideFilterWait();
    var box = h('div', { id: 'jcFilterWait', class: 'jc-aiwait' }, [
      h('div', { class: 'jc-aiwait-card' }, [
        h('div', { class: 'jc-aiwait-spin' }),
        h('div', { class: 'jc-aiwait-title', text: '正在筛这一页的岗位' }),
        h('div', { class: 'jc-aiwait-tip', id: 'jcFilterTip', text: FILTER_WAIT_TIP[0] }),
        h('div', {
          class: 'jc-aiwait-sub',
          /* 只筛页面上已经渲染出来的那批：分页/滚动加载的站，翻页后要再点一次 */
          text: '共 ' + count + ' 条（只含页面上已渲染出来的），逐条比对你的画像，通常 10～40 秒',
        }),
      ]),
    ]);
    document.documentElement.appendChild(box);
  }

  function hideFilterWait() {
    var box = document.getElementById('jcFilterWait');
    if (box && box.parentNode) box.parentNode.removeChild(box);
    clearFabBusy('filter');
  }

  /** 把推荐/可选的行在页面上直接标出来 —— 眼睛不用在弹层和列表之间来回跳 */
  function markList(picks, summary, counts) {
    var items = (state.lastFilter && state.lastFilter.items) || [];
    var hits = [];
    picks.forEach(function (p) {
      var it = items[p.i];
      if (!it || !it.node || !it.node.classList) return;
      var strong = p.level !== '可选';
      var cls = strong ? 'jc-row-hit' : 'jc-row-maybe';
      it.node.classList.add(cls);
      var badge = h('span', {
        class: 'jc-hit-badge ' + (strong ? 'jc-hit-badge-strong' : 'jc-hit-badge-maybe'),
        text: p.level,
        title: p.reason + (summary ? '\n\n这一页：' + summary : ''),
      });
      /* 插到行首而不是绝对定位：遇到 flex/grid 布局也不会把原有元素顶歪 */
      try { it.node.insertBefore(badge, it.node.firstChild); } catch (e) {}
      state.marked.push({ node: it.node, cls: cls, badge: badge });
      hits.push(it.node);
    });
    buildFilterBar(counts, summary, hits);
  }

  function clearListMarks() {
    (state.marked || []).forEach(function (m) {
      if (m.node && m.node.classList) m.node.classList.remove(m.cls);
      if (m.badge && m.badge.parentNode) m.badge.parentNode.removeChild(m.badge);
    });
    state.marked = [];
    var bar = document.getElementById('jcFilterBar');
    if (bar && bar.parentNode) bar.parentNode.removeChild(bar);
  }

  /** 底部小工具栏：看清筛出多少，并能一条条跳过去、随时清掉标记 */
  function buildFilterBar(counts, summary, hits) {
    var old = document.getElementById('jcFilterBar');
    if (old && old.parentNode) old.parentNode.removeChild(old);

    var idx = -1;
    var bar = h('div', { id: 'jcFilterBar', class: 'jc-filter-bar' }, [
      h('span', {
        class: 'jc-filter-bar-txt',
        text: '推荐 ' + counts.hit + ' · 可选 ' + counts.maybe + ' · 共 ' + counts.total + ' 条',
        title: summary || '这一页的筛选结论',
      }),
      h('button', {
        class: 'jc-mini', text: '下一个', title: '依次跳到下一个命中项',
        onclick: function () {
          if (!hits.length) return;
          idx = (idx + 1) % hits.length;
          var n = hits[idx];
          try { n.scrollIntoView({ block: 'center' }); } catch (e) {}
          n.classList.remove('jc-row-flash');
          setTimeout(function () { n.classList.add('jc-row-flash'); }, 20);
          setTimeout(function () { n.classList.remove('jc-row-flash'); }, 1600);
        },
      }),
      h('button', { class: 'jc-mini', text: '清除', title: '去掉本页所有筛选标记', onclick: clearListMarks }),
    ]);
    document.documentElement.appendChild(bar);
  }

  /* ---------------------------------------------------- 公司与岗位解读
   * 面板底部「公司与岗位解读」按钮：
   *   把面板里此刻的企业名 / 岗位 / JD 原文交给工作台转发模型，
   *   回来三样东西 —— 这家公司什么来头、他们想要什么样的人、你该怎么准备。
   * 结果按这家存一份，再点直接重开，不重复烧请求。
   */
  var INSIGHT_WAIT_TIP = [
    '正在扒这家公司的公开信息…',
    '正在对齐它所在的行当与业务盘子…',
    '正在拆 JD 里的硬门槛和软要求…',
    '正在整理简历和面试的准备清单…',
  ];
  var INSIGHT_NOTE_STYLE = {
    '熟悉': ['jc-insight-note-ok', '信息较可靠'],
    '部分确定': ['jc-insight-note-mid', '部分需核实'],
    '存疑': ['jc-insight-note-warn', '多处要自行核实'],
  };
  var insightTimer = null;

  function readPanelFields() {
    var inputs = state.inputs || {};
    function val(k) { return inputs[k] ? String(inputs[k].value || '').trim() : ''; }
    var jdEl = el('jcJd');
    return {
      company: val('company'), title: val('title'), city: val('city'), salary: val('salary'),
      education: val('education'), experience: val('experience'), department: val('department'),
      jobType: val('jobType'), headcount: val('headcount'), deadline: val('deadline'),
      link: val('link'), jd: jdEl ? String(jdEl.value || '') : '',
    };
  }

  function runInsight(force) {
    if (state.insightBusy) return;
    if (state.insight && !force) { openInsight(); return; }

    var job = readPanelFields();
    if (!job.company) { toast('先填企业名，才知道要查哪家', 'error', 4000); focusKey('company'); return; }

    var token = { cancelled: false };
    state.insightToken = token;
    state.insightBusy = true;
    openInsight();                                  /* 先把等待态亮出来 */

    var idx = 0;
    insightTimer = setInterval(function () {
      idx++;
      var tip = document.getElementById('jcInsightTip');
      if (tip) tip.textContent = INSIGHT_WAIT_TIP[idx % INSIGHT_WAIT_TIP.length];
    }, 3400);

    sendBg({ type: 'JC_AI_INSIGHT', job: job }, function (resp) {
      state.insightBusy = false;
      if (insightTimer) { clearInterval(insightTimer); insightTimer = null; }
      if (!resp || !resp.ok) {
        closeInsight();
        var code = (resp && resp.code) || '';
        if (code === 'disabled' || code === 'no_key') {
          toast('识别服务还没配好，先去设置里启用并填 Key', 'error', 5200);
          openAiSettings();
          return;
        }
        toast('没拿到解读：' + ((resp && resp.error) || '未知原因'), 'error', 6500);
        return;
      }
      state.insight = {
        company: job.company,
        title: job.title,
        data: resp.insight || {},
        meta: { provider: resp.provider || '', model: resp.model || '' },
      };
      var btn = el('jcInsightBtn');
      if (btn) btn.textContent = '查看公司与岗位解读';
      /* 等待途中它被关掉了：别硬弹回来，留个提示让用户自己点开 */
      if (token.cancelled) {
        toast('解读好了，点「查看公司与岗位解读」打开', 'success', 4200);
        return;
      }
      openInsight();
    });
  }

  function closeInsight() {
    if (insightTimer) { clearInterval(insightTimer); insightTimer = null; }
    if (state.insightToken) state.insightToken.cancelled = true;
    var o = document.getElementById('jcInsightOverlay');
    if (o && o.parentNode) o.parentNode.removeChild(o);
  }

  function openInsight() {
    var old = document.getElementById('jcInsightOverlay');
    if (old && old.parentNode) old.parentNode.removeChild(old);

    var ins = state.insight;
    var busy = !!state.insightBusy;
    var wrap = h('div', { class: 'jc-overlay', id: 'jcInsightOverlay' });
    var box = h('div', { class: 'jc-panel jc-panel-insight', role: 'dialog' });

    var sub = busy
      ? '正在读这家公司和这份 JD'
      : ((ins ? ins.company : '') + (ins && ins.title ? ' · ' + ins.title : ''));
    box.appendChild(h('div', { class: 'jc-head' }, [
      h('div', { class: 'jc-head-main' }, [
        h('div', { class: 'jc-head-title' }, [h('strong', { text: '公司与岗位解读' })]),
        h('div', { class: 'jc-head-sub', text: sub }),
      ]),
      h('button', { class: 'jc-x', text: '✕', title: '关闭', onclick: closeInsight }),
    ]));

    var body = h('div', { class: 'jc-insight-body', id: 'jcInsightBody' });
    if (busy) renderInsightWaiting(body);
    else renderInsightInto(body, ins && ins.data, ins && ins.meta);
    box.appendChild(body);

    box.appendChild(h('div', { class: 'jc-foot' }, [
      busy ? h('span', { class: 'jc-insight-foot-tip', text: '关掉也不会中断，好了会提示你' })
           : h('span', { class: 'jc-foot-gap' }),
      h('span', { class: 'jc-foot-gap' }),
      h('button', {
        class: 'jc-btn', text: '复制全文', onclick: function () {
          copyInsight(this);
        },
      }),
      h('button', { class: 'jc-btn jc-btn-ai-ghost', text: '重新生成', onclick: function () { runInsight(true); } }),
      h('button', { class: 'jc-btn', text: '关闭', onclick: closeInsight }),
    ]));

    wrap.appendChild(box);
    wrap.addEventListener('click', function (e) { if (e.target === wrap) closeInsight(); });
    document.documentElement.appendChild(wrap);
  }

  function renderInsightWaiting(body) {
    body.appendChild(h('div', { class: 'jc-insight-wait' }, [
      h('div', { class: 'jc-aiwait-spin jc-insight-spin' }),
      h('div', null, [
        h('div', { class: 'jc-insight-wait-title', text: '正在拆这家公司和这份 JD' }),
        h('div', { class: 'jc-insight-wait-tip', id: 'jcInsightTip', text: INSIGHT_WAIT_TIP[0] }),
        h('div', { class: 'jc-insight-wait-sub', text: '要读的东西比抽字段多，一般 10～40 秒' }),
      ]),
    ]));
  }

  function insightItems(v) {
    if (v === null || v === undefined || v === '') return [];
    if (Array.isArray(v)) return v.filter(function (x) { return x !== null && x !== undefined; }).map(String);
    var s = String(v);
    /* 值是 JSON 数组字符串时（缓存/旧版本可能这么存），还原成数组 */
    if (s.charAt(0) === '[') {
      try {
        var arr = JSON.parse(s);
        if (Array.isArray(arr)) return insightItems(arr);
      } catch (e) { /* 不是合法 JSON 就当普通文本分行 */ }
    }
    return s.split(/[\n；;]+/).map(function (x) { return x.trim(); }).filter(Boolean);
  }

  function renderInsightInto(body, d, meta) {
    body.innerHTML = '';
    d = d || {};
    meta = meta || {};

    function chips(title, items, cls) {
      items = insightItems(items);
      if (!items.length) return;
      body.appendChild(h('div', { class: 'jc-insight-sec' }, [
        h('div', { class: 'jc-insight-sec-t', text: title }),
        h('div', { class: 'jc-insight-chips' }, items.map(function (t) {
          return h('span', { class: 'jc-insight-chip ' + (cls || ''), text: t });
        })),
      ]));
    }
    function bullets(title, items, ordered) {
      items = insightItems(items);
      if (!items.length) return;
      body.appendChild(h('div', { class: 'jc-insight-sec' }, [
        h('div', { class: 'jc-insight-sec-t', text: title }),
        h(ordered ? 'ol' : 'ul', { class: 'jc-insight-list' }, items.map(function (t) {
          return h('li', { text: t });
        })),
      ]));
    }

    /* 企业：简介在最上面，可靠性 + 标签跟着 */
    if (d.company) {
      var head = h('div', { class: 'jc-insight-sec' }, [
        h('div', { class: 'jc-insight-sec-t' }, [
          document.createTextNode('这家公司'),
          (function () {
            var st = INSIGHT_NOTE_STYLE[d.sourceNote] || INSIGHT_NOTE_STYLE['部分确定'];
            return h('span', { class: 'jc-insight-note ' + st[0], title: '企业信息的可靠程度', text: st[1] });
          })(),
        ]),
        h('p', { class: 'jc-insight-p', text: d.company }),
      ]);
      if (insightItems(d.companyTags).length) {
        head.appendChild(h('div', { class: 'jc-insight-chips' }, insightItems(d.companyTags).map(function (t) {
          return h('span', { class: 'jc-insight-chip jc-insight-chip-co', text: t });
        })));
      }
      body.appendChild(head);
    }

    /* 人才画像：一句话结论单独提亮，别混在列表里 */
    if (d.profile) {
      body.appendChild(h('div', { class: 'jc-insight-sec' }, [
        h('div', { class: 'jc-insight-sec-t', text: '他们想要什么样的人' }),
        h('div', { class: 'jc-insight-who', text: d.profile }),
      ]));
    }
    chips('硬门槛：过不了这关基本没戏', d.hard, 'jc-insight-chip-hard');
    chips('关键能力：JD 里点名的技术/工具', d.skills, 'jc-insight-chip-skill');
    chips('隐性偏好：没写但大概率看重', d.implicit, 'jc-insight-chip-implicit');
    bullets('怎么准备', d.advice, true);
    bullets('要留意的地方', d.risks, false);

    if (!body.children.length) {
      body.appendChild(h('div', { class: 'jc-insight-empty', text: '这次没解读出内容，换个模型或重试一次。' }));
    }

    var tail = '内容依据公开信息与 JD 整理，数字、用人口径以官网答复和面聊为准';
    if (meta.provider || meta.model) tail += ' · ' + (meta.provider || '') + ' ' + (meta.model || '');
    body.appendChild(h('div', { class: 'jc-insight-disclaimer', text: tail }));
  }

  function insightText(ins) {
    var d = (ins && ins.data) || {};
    var L = [];
    L.push('【' + ((ins && ins.company) || '这家公司') + '】' + ((ins && ins.title) || '这个岗位'));
    if (d.company) L.push('', d.company);
    if (insightItems(d.companyTags).length) L.push('标签：' + insightItems(d.companyTags).join(' / '));
    if (d.profile) L.push('', '想要的人：' + d.profile);
    function part(name, items, num) {
      items = insightItems(items);
      if (!items.length) return;
      L.push('', name + '：');
      items.forEach(function (t, i) { L.push((num ? (i + 1) + '. ' : '· ') + t); });
    }
    part('硬门槛', d.hard);
    part('关键能力', d.skills);
    part('隐性偏好', d.implicit);
    part('怎么准备', d.advice, true);
    part('要留意', d.risks);
    return L.join('\n');
  }

  function copyInsight(btn) {
    var txt = insightText(state.insight);
    var done = function () { toast('解读全文已复制', 'success'); };
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(txt).then(done, fallbackCopy);
      } else {
        fallbackCopy();
      }
    } catch (e) { fallbackCopy(); }
    function fallbackCopy() {
      var ta = h('textarea', { style: 'position:fixed;top:-1000px' });
      ta.value = txt;
      document.documentElement.appendChild(ta);
      ta.select();
      try { document.execCommand('copy'); done(); }
      catch (e2) { toast('复制失败，请手动选中', 'error'); }
      ta.parentNode.removeChild(ta);
    }
    if (btn) btn.disabled = false;
  }

  function onEsc(e) {
    if (e.key !== 'Escape') return;
    /* 解读浮层在上面：先只关它，别连面板一起收掉 */
    if (document.getElementById('jcInsightOverlay')) { closeInsight(); return; }
    closePanel();
  }

  function closePanel() {
    if (state.panel && state.panel.parentNode) state.panel.parentNode.removeChild(state.panel);
    state.panel = null;
    state.inputs = null;
    state.forceUpdate = false;
    document.removeEventListener('keydown', onEsc);
  }

  /* --------------------------------------------------------------- 保存 */
  function doSave() {
    var inputs = state.inputs || {};
    function val(k) { return inputs[k] ? String(inputs[k].value || '').trim() : ''; }
    var title = val('title'), company = val('company');
    if (!title) { toast('岗位名不能为空', 'error'); focusKey('title'); return; }
    if (!company) { toast('企业名不能为空', 'error'); focusKey('company'); return; }

    var base = state.current || { fields: {}, meta: {}, confidence: {} };
    var fields = {};
    Object.keys(base.fields || {}).forEach(function (k) { fields[k] = base.fields[k]; });
    ['title', 'company', 'salary', 'city', 'education', 'experience', 'deadline', 'link', 'department', 'headcount', 'jobType'].forEach(function (k) {
      fields[k] = val(k);
    });
    if (inputs.jcJd) fields.jd = String(inputs.jcJd.value || '');
    var jdEl = el('jcJd');
    if (jdEl) fields.jd = String(jdEl.value || '');
    fields.link = normalizeUrl(fields.link || location.href);

    /* 抓完后用户又改了企业名/岗位名 → dedupeKey 与 id 必须跟着重算，
       否则会以旧 key 入库、下次判重对不上。 */
    var rebuilt = {
      version: 2,
      fields: fields,
      meta: base.meta || {},
      confidence: base.confidence || { fields: {}, overall: 0 },
      key: JobParser.dedupeKey(fields.company, fields.title),
      id: JobParser.recordId(fields.company, fields.title),
      ok: true,
    };

    var payload = JobParser.buildPayload(rebuilt, {
      batch: '插件抓取-' + new Date().toISOString().slice(0, 10),
    });
    var wantPool = !!(el('jcImportPool') && el('jcImportPool').checked);

    setBusy(true, '正在写入工作台…');
    sendBg({
      type: 'JC_CAPTURE',
      payload: payload,
      toCandidate: wantPool,
      force: !!state.forceUpdate,
    }, function (resp) {
      setBusy(false);
      if (!resp) { toast('后台没有响应，请重新加载扩展', 'error'); return; }
      if (!resp.ok && resp.needsWorkbench) {
        state.workbench = { ok: false, base: '', port: null, error: resp.error || '' };
        toast('没连上工作台：' + (resp.error || ''), 'error', 5000);
        closePanel();
        openPanel(state.current, {});
        return;
      }
      if (!resp.ok) { toast('保存失败：' + (resp.error || '未知错误'), 'error', 5000); return; }
      closePanel();
      state.hint = 'detail';    /* 详情页这一套走通了，下次把「存岗位」排上面 */
      syncFab();
      var msg = resp.created ? '已新建岗位卡片' : '已更新已有岗位卡片';
      if (wantPool) msg += resp.candidateImported ? '，并写入招聘信息' : (resp.candidateSkipped ? '，招聘信息中已存在' : '');
      toast(msg + ' · ' + (company + ' ' + title).slice(0, 24), 'success', 4200);
      offerOpen(resp);
    });
  }

  function focusKey(k) {
    if (state.inputs && state.inputs[k]) { state.inputs[k].focus(); state.inputs[k].select && state.inputs[k].select(); }
  }

  function normalizeUrl(u) {
    u = String(u || '').trim();
    /* 不剥 hash：北森/SPA 门户的岗位详情路由就在 # 后面，剥了链接就废 */
    if (!u) return location.href;
    if (/^https?:\/\//i.test(u)) return u;
    if (u.indexOf('//') === 0) return location.protocol + u;
    if (u.indexOf('/') === 0) return location.origin + u;
    /* 表格里贴的可能是裸域名 */
    if (/^[\w-]+(\.[\w-]+)+([\/?].*)?$/.test(u)) return 'https://' + u;
    return u;
  }

  function setBusy(on, text) {
    var btn = el('jcSave');
    if (btn) { btn.disabled = !!on; btn.textContent = on ? (text || '处理中…') : '保存到工作台'; }
  }

  function offerOpen(resp) {
    var box = document.getElementById('jcToast');
    if (!box) return;
    box.innerHTML = '';
    box.appendChild(document.createTextNode('要现在去工作台看看吗？ '));
    var a = h('a', { href: '#', class: 'jc-toast-link', text: '打开工作台' });
    a.addEventListener('click', function (e) { e.preventDefault(); sendBg({ type: 'JC_OPEN_WORKBENCH' }); });
    box.appendChild(a);
    box.className = 'jc-toast success jc-show';
    setTimeout(function () { box.className = 'jc-toast success'; }, 6000);
  }

  function retryWorkbench(btn) {
    btn.disabled = true; btn.textContent = '重试中…';
    sendBg({ type: 'JC_PROBE' }, function (resp) {
      btn.disabled = false; btn.textContent = '重试';
      if (resp && resp.ok) {
        toast('已连上工作台：' + resp.base, 'success');
        state.workbench = { ok: true, base: resp.base, port: resp.port };
        sendBg({ type: 'JC_LIST_JOBS' }, function (r2) {
          if (r2 && r2.ok && r2.jobs) state.dup = JobParser.checkDuplicates(state.current, r2.jobs);
          openPanel(state.current, {});
        });
      } else {
        toast('还是连不上：' + ((resp && resp.error) || '未知原因'), 'error', 5000);
      }
    });
  }

  /* --------------------------------------------------------------- 与后台通信 */
  function sendBg(msg, cb) {
    try {
      chrome.runtime.sendMessage(msg, function (resp) {
        if (chrome.runtime.lastError) {
          cb && cb({ ok: false, error: chrome.runtime.lastError.message, needsWorkbench: false });
          return;
        }
        cb && cb(resp);
      });
    } catch (e) {
      /* 扩展被重载后旧 content script 会失去 runtime —— 明确告诉用户，别静默失败 */
      cb && cb({ ok: false, error: '扩展已更新，请刷新页面后重试', needsWorkbench: false });
    }
  }

  /* --------------------------------------------------------------- 消息入口 */
  chrome.runtime.onMessage.addListener(function (msg, sender, respond) {
    if (!msg) return;
    if (msg.type === 'JC_DO_CAPTURE') { startCapture({ origin: 'command' }); respond({ ok: true }); }
    else if (msg.type === 'JC_SHOW_BUTTON') { showButton(); respond({ ok: true }); }
    else if (msg.type === 'JC_HIDE_BUTTON') { hideButton(); respond({ ok: true }); }
    else if (msg.type === 'JC_FILTER_LIST') { startFilter({ origin: 'menu' }); respond({ ok: true }); }
    else if (msg.type === 'JC_OPEN_AI_SETTINGS') { openAiSettings(); respond({ ok: true }); }
    else if (msg.type === 'JC_PING') { respond({ ok: true, injected: true, build: JC_BUILD }); }
    return true;
  });

  /* --------------------------------------------------------------- 启动 */
  function boot() {
    /* 供测试驱动：把内部纯渲染函数暴露出去（不影响正常运行）。
   * renderPreviewHtml 返回预览卡的 HTML 字符串，供 Node 侧断言。 */
  window.__JC_TESTHOOK__ = {
    renderPreviewHtml: function (fields, conf, meta) {
      var body = h('div');
      renderPreviewInto(body, fields || {}, conf || {}, meta || {});
      return serialize(body);
    },
    summarizeJd: summarizeJd,
    refreshPreview: refreshPreview,
    closePanel: closePanel,
    fullCapture: fullCapture,
    startCapture: startCapture,
    getState: function () { return state; },
    AI_WAIT_TIP: AI_WAIT_TIP,
    renderInsightHtml: function (data, meta) {
      var body = h('div');
      renderInsightInto(body, data || {}, meta || {});
      return serialize(body);
    },
    renderInsightWaitingHtml: function () {
      var body = h('div');
      renderInsightWaiting(body);
      return serialize(body);
    },
    insightText: insightText,
    buildFab: buildButton,
    applyFabMode: applyFabMode,
    openPanel: openPanel,
    startFilter: startFilter,
    clearListMarks: clearListMarks,
    openInsight: openInsight,
    closeInsight: closeInsight,
  };

  function serialize(node) {
    if (node == null) return '';
    if (node.nodeType === 3) return String(node.textContent);
    var tag = (node.tagName || 'div').toLowerCase();
    var attrs = '';
    if (node.id) attrs += ' id="' + node.id + '"';
    if (node.className) attrs += ' class="' + node.className + '"';
    if (node.attrs && node.attrs.title) attrs += ' title="' + node.attrs.title + '"';
    var inner = (node.children || []).map(serialize).join('');
    if (node.textContent && !(node.children || []).length) inner += String(node.textContent);
    return '<' + tag + attrs + '>' + inner + '</' + tag + '>';
  }

  if (state.booted) return;
    state.booted = true;
    buildButton();
    syncFab();
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot, { once: true });
  } else {
    boot();
  }

  /* SPA 换页（北森/飞书/HCM 都是前端路由）：按钮要留下来，但页面上残留的面板要关掉 */
  var lastUrl = location.href;
  setInterval(function () {
    if (location.href !== lastUrl) {
      lastUrl = location.href;
      if (state.panel) closePanel();
      clearListMarks();          /* 上一页打的行标记留在 DOM 里就是脏东西 */
      state.hint = null;         /* 换页了，别把上一页的排序偏好带过来 */
      buildButton();
      syncFab();
    }
  }, 1200);
})();
