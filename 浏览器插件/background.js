/* ============================================================================
 * background.js —— MV3 Service Worker
 *
 * 职责：
 *   1. 探测本机求职工作台的端口（8765 起，最多试探 20 个端口）
 *   2. 右键菜单 / 快捷键 → 注入 content script → 触发抓取
 *   3. 代替 content script 请求 http://127.0.0.1:<port>（跨域 + 权限都在这里解决）
 *   4. 把工作台现有岗位摘要回传给页面，供判重
 *
 * 为什么不把 127.0.0.1 的请求放在 content script 里：
 *   页面自身的 CSP / 跨域策略会干扰，而且每次注入都要处理一遍失败兜底。
 *   放在后台只需要处理一次，前端拿到的永远是干净的结果。
 * ==========================================================================*/
'use strict';

/* service worker 的构建号。content.js 靠页面刷新就能更新，background.js 必须
 * 在 chrome://extensions/ 重载扩展 —— 两者可能不同步，所以各带各的版本号，
 * 出问题时一眼能看出是谁没更新。 */
var BG_BUILD = 'bg-1.3.8';

var DEFAULT_PORT = 8765;
var PORT_TRIES = 20;
var PROBE_TTL = 30 * 1000; /* 探测结果缓存 30s，避免每次抓取都试探一遍 */

var probeCache = { ok: false, base: '', port: null, at: 0, error: '' };

/* --------------------------------------------------------------- 工具 */
function fetchWithTimeout(url, opts, ms) {
  var ctrl = typeof AbortController !== 'undefined' ? new AbortController() : null;
  var timer = setTimeout(function () { ctrl && ctrl.abort(); }, ms || 2500);
  var o = Object.assign({ cache: 'no-store' }, opts || {});
  if (ctrl) o.signal = ctrl.signal;
  return fetch(url, o).then(function (r) { clearTimeout(timer); return r; })
    .catch(function (e) { clearTimeout(timer); throw e; });
}

function jsonFetch(url, opts, timeoutMs) {
  /* 保险：超时是第三个参数，但历史上被误写进 opts（如 {timeout: 60000}），
   * 结果静默退回默认 4s，AI 请求必被自己掐断。这里把误放的位置也认出来。 */
  if (opts && typeof opts === 'object' && opts.timeout && !timeoutMs) {
    timeoutMs = opts.timeout;
    opts = Object.assign({}, opts);
    delete opts.timeout;
  }
  return fetchWithTimeout(url, opts, timeoutMs || 4000).then(function (r) {
    return r.text().then(function (t) {
      var d;
      try { d = JSON.parse(t); } catch (e) { throw new Error('返回不是 JSON（HTTP ' + r.status + '）'); }
      if (!r.ok) throw new Error((d && d.error) || ('HTTP ' + r.status));
      return d;
    });
  });
}

/* --------------------------------------------------------------- 端口探测 */
/* 99% 的情况工作台就在 8765。所以策略是「第一个端口用更长的超时、当成主判断」，
 * 这样「连不上」能在 ~1.5s 内快速给出结论，而不是干等 20 个端口扫完。 */
function probeWorkbench(force) {
  var now = Date.now();
  if (!force && probeCache.ok && now - probeCache.at < PROBE_TTL) {
    return Promise.resolve({ ok: true, base: probeCache.base, port: probeCache.port, cached: true });
  }
  var port = DEFAULT_PORT;
  var lastErr = '';
  var firstPortErr = '';
  function tryNext() {
    if (port >= DEFAULT_PORT + PORT_TRIES) {
      probeCache = { ok: false, base: '', port: null, at: Date.now(),
        error: '在 ' + DEFAULT_PORT + '–' + (DEFAULT_PORT + PORT_TRIES - 1) +
               ' 端口都没连上工作台' + (firstPortErr ? '（' + firstPortErr + '）' : '') +
               '。确认工作台在跑，且浏览器/系统代理没把 127.0.0.1 也代理走。' };
      return Promise.resolve({ ok: false, error: probeCache.error });
    }
    /* 主端口给 2500ms（够慢启动的服务回应），其余端口 700ms 快扫 */
    var isMain = (port === DEFAULT_PORT);
    var base = 'http://127.0.0.1:' + port + '/';
    return fetchWithTimeout(base + 'api/health', {}, isMain ? 2500 : 700)
      .then(function (r) { return r.ok ? r.json() : Promise.reject(new Error('HTTP ' + r.status)); })
      .then(function (d) {
        if (!d || !d.ok) throw new Error('端口 ' + port + ' 有响应但不是求职工作台');
        probeCache = { ok: true, base: base, port: port, at: Date.now(), error: '' };
        return { ok: true, base: base, port: port, db: d.db };
      })
      .catch(function (e) {
        /* 记下每个端口的具体失败原因，用于最终报错 —— 别把诊断信息吞掉 */
        var m = (e && e.message) || String(e);
        if (/Failed to fetch|NetworkError/i.test(m)) m = '连接被拒绝（该端口没人在听）';
        else if (/abort/i.test(m)) m = '超时（2500ms 内没回应）';
        if (!lastErr) lastErr = '端口 ' + port + '：' + m;
        if (isMain) firstPortErr = '端口 ' + port + '：' + m;
        port++;
        return tryNext();
      });
  }
  return tryNext();
}

function getBase() {
  return probeWorkbench(false).then(function (r) {
    if (r.ok) return r.base;
    /* 缓存里的旧 base 也可能还活着，再确认一次 */
    if (probeCache.base) {
      return fetchWithTimeout(probeCache.base + 'api/health', {}, 1200)
        .then(function (x) { return x.ok ? probeCache.base : Promise.reject(new Error(r.error)); })
        .catch(function () { throw new Error(r.error); });
    }
    throw new Error(r.error);
  });
}

/* --------------------------------------------------------------- 注入 content script */
function ensureInjected(tabId) {
  return new Promise(function (resolve) {
    chrome.tabs.sendMessage(tabId, { type: 'JC_PING' }, function () {
      if (!chrome.runtime.lastError) { resolve(true); return; }
      /* 没注入过（或页面刷新过）→ 注入 */
      chrome.scripting.insertCSS({ target: { tabId: tabId }, files: ['content.css'] }).catch(function () {});
      chrome.scripting.executeScript({ target: { tabId: tabId }, files: ['lib/job-parser.js'] })
        .then(function () {
          return chrome.scripting.executeScript({ target: { tabId: tabId }, files: ['content.js'] });
        })
        .then(function () { setTimeout(function () { resolve(true); }, 60); })
        .catch(function (e) {
          /* chrome://、扩展商店页、PDF 预览等页面禁止注入，这里必须给出人话提示 */
          resolve({ ok: false, error: '这个页面不允许扩展注入（' + (e && e.message ? e.message : '受限页面') + '）' });
        });
    });
  });
}

function triggerCapture(tab) {
  if (!tab || !tab.id) return Promise.resolve({ ok: false, error: '没有取到当前标签页' });
  var url = tab.url || '';
  if (/^(chrome|edge|about|devtools|chrome-extension|view-source):/i.test(url)) {
    return Promise.resolve({ ok: false, error: '浏览器内置页面不支持抓取岗位' });
  }
  return ensureInjected(tab.id).then(function (r) {
    if (r && r.ok === false) return r;
    return new Promise(function (resolve) {
      chrome.tabs.sendMessage(tab.id, { type: 'JC_DO_CAPTURE' }, function (resp) {
        if (chrome.runtime.lastError) { resolve({ ok: false, error: chrome.runtime.lastError.message }); return; }
        resolve(resp || { ok: true });
      });
    });
  });
}

/* --------------------------------------------------------------- 徽标反馈 */
var badgeTimer = null;
function flashBadge(tabId, text, color, ms) {
  if (!tabId) return;
  try {
    chrome.action.setBadgeText({ tabId: tabId, text: text });
    chrome.action.setBadgeBackgroundColor({ tabId: tabId, color: color });
  } catch (e) { return; }
  clearTimeout(badgeTimer);
  badgeTimer = setTimeout(function () {
    try { chrome.action.setBadgeText({ tabId: tabId, text: '' }); } catch (e) {}
  }, ms || 2500);
}

function notifyTab(tabId, msg) {
  if (!tabId) return;
  chrome.tabs.sendMessage(tabId, { type: 'JC_TOAST', msg: msg }).catch(function () {});
}

/* --------------------------------------------------------------- 写库 */
function doCapture(payload, opts) {
  opts = opts || {};
  return getBase().then(function (base) {
    return jsonFetch(base + 'api/jobs/capture', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        job: payload.job,
        candidate: opts.toCandidate === false ? null : payload.candidate,
        meta: payload.meta,
        force: !!opts.force,
        upsert: true,
      }),
    });
  }).then(function (d) {
    return {
      ok: true, base: getCachedBase(),
      created: !!d.created, updated: !!d.updated,
      candidateImported: !!d.candidate_imported, candidateSkipped: !!d.candidate_skipped,
      jobId: d.job_id || (payload.job && payload.job.id), reason: d.reason || '',
    };
  }).catch(function (e) {
    return { ok: false, needsWorkbench: true, error: e.message };
  });
}

function getCachedBase() { return probeCache.base || ''; }

/* AI 识别（唯一识别通道）：把页面文本交给本机工作台，由工作台代调大模型。
 * API Key 只存在工作台侧，插件永远接触不到。
 *
 * 超时链：模型最快 1s、慢能到 20s+，服务端 ai_timeout 默认 40s（可配到 180s）。
 * 所以插件这头必须比服务端宽 —— 给 90s。历史上这里只给 4s，请求全被自己掐断。
 */
var AI_TIMEOUT_MS = 90000;

/* MV3 的 service worker 30 秒没事件就被回收，正在飞的长请求会被 abort
 * （前端看到的正是 `signal is aborted without reason`）。
 * AI 识别 5~20s 恰好踩在这个边界上，所以请求期间必须有东西持续唤醒 worker。 */
var keepAliveTimer = null;
function keepAliveStart() {
  if (keepAliveTimer) return;
  keepAliveTimer = setInterval(function () {
    try { chrome.runtime.getPlatformInfo(function () {}); } catch (e) {}
  }, 10000);
}
function keepAliveStop() {
  if (keepAliveTimer) { clearInterval(keepAliveTimer); keepAliveTimer = null; }
}

function aiComplete(pageText) {
  var t0 = Date.now();
  var stage = 'probe';
  keepAliveStart();
  return getBase().then(function (base) {
    stage = 'ai/complete@' + base;
    return jsonFetch(base + 'api/ai/complete', {
      method: 'POST',
      body: JSON.stringify({ pageText: pageText || '', mode: 'full', maxText: 16000 }),
    }, AI_TIMEOUT_MS);
  }).then(function (d) {
    keepAliveStop();
    if (!d || !d.ok) {
      return { ok: false, error: (d && d.error) || '识别失败',
               code: (d && d.code) || '', raw: (d && d.raw) || '',
               bgBuild: BG_BUILD, ms: Date.now() - t0 };
    }
    return {
      ok: true,
      provider: d.provider || '',
      model: d.model || '',
      capture: d.capture,
      bgBuild: BG_BUILD, ms: Date.now() - t0,
    };
  }).catch(function (e) {
    keepAliveStop();
    var m = (e && e.message) || '请求失败';
    var ms = Date.now() - t0;
    var code = 'network';
    /* abort 有两种来源，必须分开，否则永远查不出真凶 */
    if (/abort/i.test(m)) {
      m = '请求被中断（等待 ' + ms + 'ms，超时上限 ' + AI_TIMEOUT_MS + 'ms，阶段：' + stage + '）';
      code = 'aborted';
    } else {
      m = '网络错误（' + m + '，阶段：' + stage + '）';
    }
    return { ok: false, error: m, code: code, bgBuild: BG_BUILD, ms: ms, stage: stage };
  });
}

/* 公司与岗位解读：企业底细 + 人才画像 + 求职建议。
 * 输出比字段抽取长得多，模型常常 30s 起步 —— 超时比 aiComplete 再放宽一档。
 */
var INSIGHT_TIMEOUT_MS = 120000;

function aiInsight(job) {
  var t0 = Date.now();
  var stage = 'probe';
  keepAliveStart();
  return getBase().then(function (base) {
    stage = 'ai/insight@' + base;
    return jsonFetch(base + 'api/ai/insight', {
      method: 'POST',
      body: JSON.stringify({ job: job || {} }),
    }, INSIGHT_TIMEOUT_MS);
  }).then(function (d) {
    keepAliveStop();
    if (!d || !d.ok) {
      return { ok: false, error: (d && d.error) || '解读失败',
               code: (d && d.code) || '', bgBuild: BG_BUILD, ms: Date.now() - t0 };
    }
    return { ok: true, insight: d.insight || {},
             provider: d.provider || '', model: d.model || '',
             bgBuild: BG_BUILD, ms: Date.now() - t0 };
  }).catch(function (e) {
    keepAliveStop();
    var m = (e && e.message) || '请求失败';
    var ms = Date.now() - t0;
    var code = 'network';
    if (/abort/i.test(m)) {
      m = '解读请求被中断（等了 ' + ms + 'ms，上限 ' + INSIGHT_TIMEOUT_MS + 'ms）';
      code = 'aborted';
    } else {
      m = '网络错误（' + m + '，阶段：' + stage + '）';
    }
    return { ok: false, error: m, code: code, bgBuild: BG_BUILD, ms: ms, stage: stage };
  });
}

/* 列表页筛选：带上页码/host 一起发，服务端结合画像判断哪些值得点开。
 * 一次要读几十上百行，跟解读同一档超时。 */
function aiFilter(payload) {
  var t0 = Date.now();
  var stage = 'probe';
  keepAliveStart();
  return getBase().then(function (base) {
    stage = 'ai/filter@' + base;
    return jsonFetch(base + 'api/ai/filter', {
      method: 'POST',
      body: JSON.stringify(payload || {}),
    }, INSIGHT_TIMEOUT_MS);
  }).then(function (d) {
    keepAliveStop();
    if (!d || !d.ok) {
      return { ok: false, error: (d && d.error) || '筛选失败',
               code: (d && d.code) || '', bgBuild: BG_BUILD, ms: Date.now() - t0 };
    }
    return {
      ok: true, picks: d.picks || [], summary: d.summary || '',
      total: d.total || 0, hit: d.hit || 0, maybe: d.maybe || 0,
      /* isList / listReason 必须一起转发：这页是不是列表页是模型判的，
       * 在这里漏掉，前端就永远拿不到 false，只能拿"0 条匹配"糊弄用户 */
      isList: d.isList !== false,
      listReason: d.listReason || '',
      provider: d.provider || '', model: d.model || '',
      bgBuild: BG_BUILD, ms: Date.now() - t0,
    };
  }).catch(function (e) {
    keepAliveStop();
    var m = (e && e.message) || '请求失败';
    var ms = Date.now() - t0;
    var code = 'network';
    if (/abort/i.test(m)) {
      m = '筛选请求被中断（等了 ' + ms + 'ms，上限 ' + INSIGHT_TIMEOUT_MS + 'ms）';
      code = 'aborted';
    } else {
      m = '网络错误（' + m + '，阶段：' + stage + '）';
    }
    return { ok: false, error: m, code: code, bgBuild: BG_BUILD, ms: ms, stage: stage };
  });
}

function listJobs() {  return getBase().then(function (base) {
    return jsonFetch(base + 'api/all');
  }).then(function (d) {
    return { ok: true, jobs: d.jobs || [], workbench: { ok: true, base: getCachedBase(), port: probeCache.port } };
  }).catch(function (e) {
    return { ok: false, error: e.message, jobs: [], workbench: { ok: false, base: '', port: null, error: e.message } };
  });
}

/* --------------------------------------------------------------- 消息路由 */
chrome.runtime.onMessage.addListener(function (msg, sender, respond) {
  if (!msg || !msg.type) return;

  if (msg.type === 'JC_PING') { respond({ ok: true, version: chrome.runtime.getManifest().version, bgBuild: BG_BUILD }); return true; }

  if (msg.type === 'JC_PROBE') {
    probeWorkbench(true).then(function (r) {
      respond(Object.assign({ bgBuild: BG_BUILD }, r));
    });
    return true;
  }

  if (msg.type === 'JC_LIST_JOBS') {
    listJobs().then(respond);
    return true;
  }

  if (msg.type === 'JC_AI_COMPLETE') {
    aiComplete(msg.pageText).then(respond);
    return true;
  }

  if (msg.type === 'JC_AI_INSIGHT') {
    aiInsight(msg.job).then(respond);
    return true;
  }

  if (msg.type === 'JC_AI_FILTER') {
    aiFilter({ items: msg.items || [], meta: msg.meta || {} }).then(respond);
    return true;
  }

  if (msg.type === 'JC_GET_AI_CONFIG') {
    getBase().then(function (base) { return jsonFetch(base + 'api/ai/config'); })
      .then(function (d) { respond({ ok: true, config: d.config || null }); })
      .catch(function (e) { respond({ ok: false, error: e.message, config: null }); });
    return true;
  }

  if (msg.type === 'JC_SET_AI_CONFIG') {
    getBase().then(function (base) {
      return jsonFetch(base + 'api/ai/config', {
        method: 'POST',
        body: JSON.stringify(msg.patch || {}),
      });
    }).then(function (d) {
      respond(d && d.ok ? { ok: true, saved: d.saved } : { ok: false, error: (d && d.error) || '保存失败' });
    }).catch(function (e) { respond({ ok: false, error: e.message }); });
    return true;
  }

  if (msg.type === 'JC_TEST_AI') {
    getBase().then(function (base) {
      return jsonFetch(base + 'api/ai/test', { method: 'POST', body: '{}' }, 45000);
    }).then(respond)
      .catch(function (e) { respond({ ok: false, error: e.message }); });
    return true;
  }

  if (msg.type === 'JC_CAPTURE') {
    doCapture(msg.payload, { toCandidate: msg.toCandidate, force: msg.force }).then(function (r) {
      var tabId = sender && sender.tab ? sender.tab.id : null;
      if (r.ok) flashBadge(tabId, r.created ? '✓' : '↻', r.created ? '#16a34a' : '#2563eb', 2600);
      else flashBadge(tabId, '!', '#dc2626', 2600);
      respond(r);
    });
    return true;
  }

  if (msg.type === 'JC_OPEN_WORKBENCH') {
    getBase().then(function (base) { chrome.tabs.create({ url: base.replace(/\/$/, '') + '/#jobs' }); })
      .catch(function () {
        chrome.tabs.create({ url: 'http://127.0.0.1:' + DEFAULT_PORT + '/#jobs' });
      });
    respond({ ok: true });
    return true;
  }

  if (msg.type === 'JC_TOAST') { return; }
});

/* --------------------------------------------------------------- 触发入口 */
chrome.commands.onCommand.addListener(function (cmd) {
  if (cmd === 'capture-job') {
    chrome.tabs.query({ active: true, currentWindow: true }, function (tabs) {
      triggerCapture(tabs && tabs[0]).then(function (r) {
        var tabId = tabs && tabs[0] ? tabs[0].id : null;
        if (r && r.ok === false) {
          flashBadge(tabId, '!', '#dc2626', 3000);
          notifyTab(tabId, { type: 'error', text: r.error });
        }
      });
    });
    return;
  }
  /* 列表页筛选：让页面脚本按行抽取后自己发 JC_AI_FILTER */
  if (cmd === 'filter-jobs') {
    chrome.tabs.query({ active: true, currentWindow: true }, function (tabs) {
      var tab = tabs && tabs[0];
      if (!tab || !tab.id) return;
      ensureInjected(tab.id).then(function () {
        chrome.tabs.sendMessage(tab.id, { type: 'JC_FILTER_LIST' }, function () { void chrome.runtime.lastError; });
      });
    });
  }
});

chrome.contextMenus.onClicked.addListener(function (info, tab) {
  if (info.menuItemId === 'jc-capture-job') triggerCapture(tab);
  else if (info.menuItemId === 'jc-show-button') {
    ensureInjected(tab.id).then(function () { chrome.tabs.sendMessage(tab.id, { type: 'JC_SHOW_BUTTON' }); });
  } else if (info.menuItemId === 'jc-hide-button') {
    chrome.tabs.sendMessage(tab.id, { type: 'JC_HIDE_BUTTON' }, function () { void chrome.runtime.lastError; });
  } else if (info.menuItemId === 'jc-filter-list') {
    ensureInjected(tab.id).then(function () {
      chrome.tabs.sendMessage(tab.id, { type: 'JC_FILTER_LIST' }, function () { void chrome.runtime.lastError; });
    });
  } else if (info.menuItemId === 'jc-ai-settings') {
    /* 设置面板在页面里渲染；先把内容脚本注入好再让它开面板 */
    ensureInjected(tab.id).then(function () {
      chrome.tabs.sendMessage(tab.id, { type: 'JC_OPEN_AI_SETTINGS' }, function () { void chrome.runtime.lastError; });
    });
  }
});

chrome.action.onClicked.addListener(function (tab) {
  triggerCapture(tab).then(function (r) {
    if (r && r.ok === false) {
      flashBadge(tab && tab.id, '!', '#dc2626', 3000);
      notifyTab(tab && tab.id, { type: 'error', text: r.error });
    }
  });
});

function buildMenus() {
  chrome.contextMenus.removeAll(function () {
    chrome.contextMenus.create({
      id: 'jc-capture-job',
      title: '存到求职工作台（自动识别本页岗位）',
      contexts: ['page', 'selection', 'link'],
    });
    chrome.contextMenus.create({
      id: 'jc-filter-list',
      title: '按我的求职目标筛这一页…',
      contexts: ['page'],
    });
    chrome.contextMenus.create({ id: 'jc-show-button', title: '显示悬浮按钮', contexts: ['page'] });
    chrome.contextMenus.create({ id: 'jc-hide-button', title: '隐藏悬浮按钮', contexts: ['page'] });
    chrome.contextMenus.create({
      id: 'jc-ai-settings',
      title: '识别设置…',
      contexts: ['page', 'action'],
    });
  });
}

chrome.runtime.onInstalled.addListener(function (details) {
  buildMenus();
  /* 装完就往工作台打一次招呼，让用户立刻知道通没通 */
  probeWorkbench(true).then(function (r) {
    if (r.ok) {
      chrome.action.setBadgeText({ text: '' });
      chrome.action.setTitle({ title: '岗位速存 · 已连上工作台（' + r.base + '）' });
    } else {
      chrome.action.setBadgeText({ text: '!' });
      chrome.action.setBadgeBackgroundColor({ color: '#dc2626' });
      chrome.action.setTitle({ title: '岗位速存 · 未连上工作台，请先启动 求职工作台' });
    }
  });
});

chrome.runtime.onStartup.addListener(function () {
  buildMenus();
  probeWorkbench(true);
});
