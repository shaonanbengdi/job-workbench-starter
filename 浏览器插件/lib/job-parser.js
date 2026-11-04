/* ============================================================================
 * job-parser.js —— 岗位提取核心库（AI 专用精简版）
 *
 * 2026-10-10 起字段识别全部交给大模型，本地规则匹配已彻底删除。
 * 本文件现在只做三件事：
 *   1. 把页面文本压成喂模型的精简文本（aiCondenseText 及其帮手）
 *   2. 保存链路要的归一化与判重（normalize* / dedupeKey / checkDuplicates / buildPayload）
 *   3. 少量文本工具（clean / collapse / clip / md5）
 *
 * 已删除：parseSalary、parseEducation、parseExperience、parseCity、parseDate、
 *        ADAPTERS 与全部站点适配器、fromJsonLd/fromTitleTag/fromMetaDescription/fromUrl、
 *        extract() 与 scoreFields/labeledValue、detectTalentType 等一切本地识别逻辑。
 *
 * 零依赖、纯函数；浏览器挂 globalThis.JobParser，node 走 module.exports。
 * ==========================================================================*/
(function (root, factory) {
  var api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  root.JobParser = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  var DEFAULT_SOURCE = '浏览器插件';
  var CATEGORY = '招聘岗位';
  var IMPORTANCE_VALUES = ['focus', 'can', 'hold', 'skip'];
  var MD5_CACHE = {};

  /* JD 小标题词典：extractJdSections 用它定位正文起点 */
  var JD_SECTION_WORDS = [
    '岗位职责', '工作职责', '职位描述', '岗位描述', '职位职责', '工作内容', '主要职责',
    '任职要求', '岗位要求', '任职资格', '任职条件', '职位要求', '资格要求', '应聘要求',
    'job description', 'responsibilities', 'requirements', 'qualifications',
  ];
  var JD_SECTION_RE = new RegExp(
    '(' + JD_SECTION_WORDS.map(function (w) {
      return w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    }).join('|') + ')\\s*[:：]?\\s*', 'i');

  /* JD 容器候选/排除关键词 */
  var JD_CONTAINER_HINTS = [
    'job-detail', 'jobDetail', 'job_description', 'jobDescription', 'job-desc', 'jobContent',
    'position-detail', 'positionDetail', 'position-desc', 'post-detail',
    'detail-content', 'detailContent', 'detail_content', 'article-content', 'articleContent',
    'rich-text', 'richText', 'jd-content', 'job-require', 'jobRequire',
    'recruit-detail', 'desc-content', 'content-detail', 'main-content', 'mainContent',
  ];
  var JD_NEGATIVE_HINTS = [
    'nav', 'header', 'footer', 'sidebar', 'menu', 'breadcrumb', 'crumb', 'comment',
    'recommend', 'related', 'hot', 'ad-', 'advert', 'banner', 'login', 'register',
    'subscribe', 'share', 'cookie', 'toolbar', 'search', 'pagination', 'page-nav',
  ];

  /* 噪声行：命中就截断/丢弃 */
  var AI_NOISE = /(相关职位|热门职位|猜你喜欢|为您推荐|相似职位|其他职位|浏览过|投递过|注册|登录|立即申请|返回顶部|版权所有|联系我们|关于我们|网站地图|友情链接|隐私政策|用户协议|扫码|下载APP|关注我们|分享到|上一篇|下一篇)/;
  /* 保留行：短文本块兜底时只留命中这些词的块 */
  var AI_KEEP_LABEL = /(岗位|职位|工作|招聘|薪|待遇|学历|专业|经验|年限|地点|城市|区域|截止|有效期|部门|人数|性质|类型|要求|职责|描述|内容|任职|资格|条件|福利|公司|企业|单位|投递|链接|简历|联系)/;

  /* 公司名归一化用 */
  var COMPANY_SUFFIX = /(股份有限公司|有限责任公司|有限公司|股份|集团|控股|科技|技术|工业|实业|制造|汽车|公司|厂|研究院|研究所|中心)$/;
  var COMPANY_NOISE = /^(【[^】]{0,12}】|\[[^\]]{0,12}\]|诚聘|急聘|招聘|热招|高薪|知名企业[:：]?)/;

  /* ===================== 函数区 ===================== */

  function clean(s) {
    if (s === null || s === undefined) return '';
    return String(s)
      .replace(/\u00a0/g, ' ')          // 不换行空格
      .replace(/[\u200b-\u200f\ufeff]/g, '') // 零宽等控制字符
      .replace(/[ \t]+/g, ' ')
      .replace(/\s*\n\s*/g, '\n')
      .replace(/\n{3,}/g, '\n\n')
      .trim();
  }

  function collapse(s) {
    return clean(s).replace(/\n+/g, ' ').replace(/\s{2,}/g, ' ').trim();
  }

  /* 截断时不要在半个词/半个括号里断开 */
  function clip(s, n) {
    s = clean(s);
    if (s.length <= n) return s;
    var cut = s.slice(0, n);
    var m = /[（(【[「『][^）)】\]」』]*$/.exec(cut);
    if (m) cut = cut.slice(0, cut.length - m[0].length);
    return cut.replace(/[\s，,、;；:：\-—]+$/, '') + '…';
  }

  function uniq(arr) {
    var seen = {}, out = [];
    for (var i = 0; i < arr.length; i++) {
      var v = arr[i];
      if (!v) continue;
      var k = String(v).trim();
      if (!k || seen[k]) continue;
      seen[k] = 1; out.push(k);
    }
    return out;
  }

  function host(url) {
    var m = /^https?:\/\/([^/?#]+)/i.exec(url || '');
    return m ? m[1].toLowerCase() : '';
  }

  /* 去掉端口与 www.，用于适配器域名匹配 */
  function bareHost(url) {
    return host(url).replace(/:\d+$/, '').replace(/^www\./, '');
  }

  function hostEndsWith(h, suffix) {
    return h === suffix || ('/' + h).indexOf('/' + suffix) === h.length - suffix.length + 1 - 1 ||
           h.slice(-(suffix.length + 1)) === '.' + suffix;
  }

  function isSameSite(h, domain) {
    if (!h || !domain) return false;
    if (h === domain) return true;
    return h.length > domain.length && h.slice(-(domain.length + 1)) === '.' + domain;
  }

  function md5(str) {
    if (MD5_CACHE[str]) return MD5_CACHE[str];
    var out = md5hex(unescape(encodeURIComponent(str)));
    MD5_CACHE[str] = out;
    return out;
  }

  function md5hex(s) {
    /* 紧凑的 md5 实现（RFC 1321），避免依赖 crypto.subtle 的异步 */
    function rl(n, c) { return (n << c) | (n >>> (32 - c)); }
    function au(x, y) { var l = (x & 0xFFFF) + (y & 0xFFFF); return (((x >> 16) + (y >> 16) + (l >> 16)) << 16) | (l & 0xFFFF); }
    function cmn(q, a, b, x, s, t) { return au(rl(au(au(a, q), au(x, t)), s), b); }
    function ff(a, b, c, d, x, s, t) { return cmn((b & c) | (~b & d), a, b, x, s, t); }
    function gg(a, b, c, d, x, s, t) { return cmn((b & d) | (c & ~d), a, b, x, s, t); }
    function hh(a, b, c, d, x, s, t) { return cmn(b ^ c ^ d, a, b, x, s, t); }
    function ii(a, b, c, d, x, s, t) { return cmn(c ^ (b | ~d), a, b, x, s, t); }
    function toBlocks(str) {
      var n = str.length, blks = [], i;
      for (i = 0; i < n * 8; i += 8) blks[i >> 5] |= (str.charCodeAt(i / 8) & 0xFF) << (i % 32);
      blks[n * 8 >> 5] |= 0x80 << ((n * 8) % 32);
      blks[(((n * 8 + 64) >>> 9) << 4) + 14] = n * 8;
      return blks;
    }
    var blks = toBlocks(s), a = 1732584193, b = -271733879, c = -1732584194, d = 271733878, i;
    for (i = 0; i < blks.length; i += 16) {
      var oa = a, ob = b, oc = c, od = d;
      a = ff(a, b, c, d, blks[i], 7, -680876936);      d = ff(d, a, b, c, blks[i + 1], 12, -389564586);
      c = ff(c, d, a, b, blks[i + 2], 17, 606105819);  b = ff(b, c, d, a, blks[i + 3], 22, -1044525330);
      a = ff(a, b, c, d, blks[i + 4], 7, -176418897);  d = ff(d, a, b, c, blks[i + 5], 12, 1200080426);
      c = ff(c, d, a, b, blks[i + 6], 17, -1473231341); b = ff(b, c, d, a, blks[i + 7], 22, -45705983);
      a = ff(a, b, c, d, blks[i + 8], 7, 1770035416);  d = ff(d, a, b, c, blks[i + 9], 12, -1958414417);
      c = ff(c, d, a, b, blks[i + 10], 17, -42063);    b = ff(b, c, d, a, blks[i + 11], 22, -1990404162);
      a = ff(a, b, c, d, blks[i + 12], 7, 1804603682); d = ff(d, a, b, c, blks[i + 13], 12, -40341101);
      c = ff(c, d, a, b, blks[i + 14], 17, -1502002290); b = ff(b, c, d, a, blks[i + 15], 22, 1236535329);
      a = gg(a, b, c, d, blks[i + 1], 5, -165796510);  d = gg(d, a, b, c, blks[i + 6], 9, -1069501632);
      c = gg(c, d, a, b, blks[i + 11], 14, 643717713);  b = gg(b, c, d, a, blks[i], 20, -373897302);
      a = gg(a, b, c, d, blks[i + 5], 5, -701558691);  d = gg(d, a, b, c, blks[i + 10], 9, 38016083);
      c = gg(c, d, a, b, blks[i + 15], 14, -660478335); b = gg(b, c, d, a, blks[i + 4], 20, -405537848);
      a = gg(a, b, c, d, blks[i + 9], 5, 568446438);   d = gg(d, a, b, c, blks[i + 14], 9, -1019803690);
      c = gg(c, d, a, b, blks[i + 3], 14, -187363961);  b = gg(b, c, d, a, blks[i + 8], 20, 1163531501);
      a = gg(a, b, c, d, blks[i + 13], 5, -1444681467); d = gg(d, a, b, c, blks[i + 2], 9, -51403784);
      c = gg(c, d, a, b, blks[i + 7], 14, 1735328473);  b = gg(b, c, d, a, blks[i + 12], 20, -1926607734);
      a = hh(a, b, c, d, blks[i + 5], 4, -378558);     d = hh(d, a, b, c, blks[i + 8], 11, -2022574463);
      c = hh(c, d, a, b, blks[i + 11], 16, 1839030562); b = hh(b, c, d, a, blks[i + 14], 23, -35309556);
      a = hh(a, b, c, d, blks[i + 1], 4, -1530992060); d = hh(d, a, b, c, blks[i + 4], 11, 1272893353);
      c = hh(c, d, a, b, blks[i + 7], 16, -155497632);  b = hh(b, c, d, a, blks[i + 10], 23, -1094730640);
      a = hh(a, b, c, d, blks[i + 13], 4, 681279174);  d = hh(d, a, b, c, blks[i], 11, -358537222);
      c = hh(c, d, a, b, blks[i + 3], 16, -722521979);  b = hh(b, c, d, a, blks[i + 6], 23, 76029189);
      a = hh(a, b, c, d, blks[i + 9], 4, -640364487);  d = hh(d, a, b, c, blks[i + 12], 11, -421815835);
      c = hh(c, d, a, b, blks[i + 15], 16, 530742520);  b = hh(b, c, d, a, blks[i + 2], 23, -995338651);
      a = ii(a, b, c, d, blks[i], 6, -198630844);      d = ii(d, a, b, c, blks[i + 7], 10, 1126891415);
      c = ii(c, d, a, b, blks[i + 14], 15, -1416354905); b = ii(b, c, d, a, blks[i + 5], 21, -57434055);
      a = ii(a, b, c, d, blks[i + 12], 6, 1700485571); d = ii(d, a, b, c, blks[i + 3], 10, -1894986606);
      c = ii(c, d, a, b, blks[i + 10], 15, -1051523);   b = ii(b, c, d, a, blks[i + 1], 21, -2054922799);
      a = ii(a, b, c, d, blks[i + 8], 6, 1873313359);  d = ii(d, a, b, c, blks[i + 15], 10, -30611744);
      c = ii(c, d, a, b, blks[i + 6], 15, -1560198380); b = ii(b, c, d, a, blks[i + 13], 21, 1309151649);
      a = ii(a, b, c, d, blks[i + 4], 6, -145523070);  d = ii(d, a, b, c, blks[i + 11], 10, -1120210379);
      c = ii(c, d, a, b, blks[i + 2], 15, 718787259);   b = ii(b, c, d, a, blks[i + 9], 21, -343485551);
      a = au(a, oa); b = au(b, ob); c = au(c, oc); d = au(d, od);
    }
    var hex = '0123456789abcdef';
    function w(n) {
      var s = '';
      for (var j = 0; j < 4; j++) s += hex.charAt((n >> (j * 8 + 4)) & 0x0F) + hex.charAt((n >> (j * 8)) & 0x0F);
      return s;
    }
    return w(a) + w(b) + w(c) + w(d);
  }

  /* 岗位名归一化：去「急聘」「（J12345）」「【2027校招】」等噪声，用于跨站判重 */
  function normalizeTitle(title) {
    return collapse(title || '')
      .replace(/^[【\[（(][^】\]）)]{0,20}[】\]）)]/, '')
      .replace(/[（(]\s*[A-Za-z]{0,4}[-_]?\d{4,}\s*[）)]/g, '')
      .replace(/[（(]\s*(急聘|热招|高薪|急招|new|hot|测试|北京|上海|深圳|广州)\s*[）)]/gi, '')
      .replace(/[（(][^）)]{0,12}[）)]/g, '')   // 残留的括注一律丢掉，避免「（测试）」「（苏州）」把同一岗位判成两个
      .replace(/^(急聘|诚聘|热招|高薪招聘|招聘)\s*[·:：\-]?\s*/, '')
      .replace(/\s*(急聘|热招|长期有效)\s*$/, '')
      .replace(/\s+/g, '')
      .toLowerCase();
  }

  function normalizeCompany(company) {
    var c = collapse(company || '').replace(COMPANY_NOISE, '');
    /* 只在末尾切除，且要求括号里是「地名/分支」这类短后缀（≤6 字），
       否则会把「测试汽车（集团）」这种带「集团」的括号当噪声切掉 */
    c = c.replace(/[（(][^）)]{0,6}[）)]\s*$/, '');
    c = c.replace(/[\s·•・\-—_]/g, '').toLowerCase();
    var prev = c;
    var guard = 0;
    while (guard++ < 6) {
      var next = c.replace(COMPANY_SUFFIX, '');
      if (next === c) break;
      /* 剥完不足 3 字就停：再剥下去会把「上海汽车集团…技术中心」变成「上海」，
         与其他上海企业撞在一起，反而制造误判 */
      if (next.length < 3) break;
      prev = c;
      c = next;
    }
    return c;
  }

  function shortCompany(company) {
    var c = collapse(company || '').replace(COMPANY_NOISE, '')
      .replace(/[（(][^）)]{0,6}[）)]\s*$/, '');
    var prev;
    do { prev = c; c = c.replace(COMPANY_SUFFIX, ''); } while (c !== prev && c.length > 3);
    return c || collapse(company || '');
  }

  /** 判重键：公司与岗位双归一，跨站可复用 */
  function dedupeKey(company, title) {
    return normalizeCompany(company) + '::' + normalizeTitle(title);
  }

  /** 记录 id：与工作台 `job_<candidate_id>` 约定同源，保证「纳入候选池」判重不打架 */
  function recordId(company, title) {
    return 'job_jobcap_' + md5(dedupeKey(company, title)).slice(0, 12);
  }

  function metaContent(names) {
    for (var i = 0; i < names.length; i++) {
      var e = document.querySelector('meta[property="' + names[i] + '"],meta[name="' + names[i] + '"]');
      if (e && e.content) return clean(e.content);
    }
    return '';
  }

  /* 5.4 JD 正文容器定位：打分选最优，取不到就退回正文文字 */
  function pickJdContainer() {
    var best = null, bestScore = 0;
    var nodes = document.querySelectorAll('div,section,article,main,td');
    for (var i = 0; i < nodes.length; i++) {
      var node = nodes[i];
      var text = collapse(node.textContent);
      if (text.length < 80 || text.length > 8000) continue;
      /* 只统计自身的直接文本量，避免父容器因为包含子容器而虚高 */
      var ownLen = 0;
      for (var c = 0; c < node.childNodes.length; c++) {
        if (node.childNodes[c].nodeType === 3) ownLen += (node.childNodes[c].nodeValue || '').length;
      }
      var score = 0;
      var sig = (node.className && typeof node.className === 'string' ? node.className : '') + ' ' +
                (node.id || '') + ' ' +
                (node.getAttribute && (node.getAttribute('data-testid') || node.getAttribute('role')) || '');
      var sigL = sig.toLowerCase();
      JD_CONTAINER_HINTS.forEach(function (h) { if (sigL.indexOf(h.toLowerCase()) >= 0) score += 6; });
      JD_NEGATIVE_HINTS.forEach(function (h) { if (sigL.indexOf(h) >= 0) score -= 5; });
      /* 出现「岗位职责/任职要求」这类小标题 → 强信号 */
      var hits = 0;
      JD_SECTION_WORDS.forEach(function (w) { if (text.indexOf(w) >= 0) hits++; });
      score += Math.min(hits, 4) * 5;
      /* 换行密度：JD 通常有多行分条 */
      var lines = text.split('\n').length;
      score += Math.min(Math.floor(lines / 3), 5);
      /* 含关键词的段落比例 */
      score += Math.min(Math.floor(ownLen / 200), 3);
      /* 篇幅惩罚：过短或过长都不像纯 JD 容器 */
      if (text.length < 150) score -= 2;
      if (text.length > 5000) score -= 3;
      if (node.tagName === 'BODY' || node.tagName === 'MAIN') score -= 4;
      if (score > bestScore) { bestScore = score; best = node; }
    }
    if (best && bestScore >= 6) {
      /* 最后一道闸：容器自己（含直接子节点）的文本不能占掉整页的绝大部分，
       * 否则它就是个页面级包裹层，不是 JD 容器 —— 交出去必然夹带导航和推荐岗 */
      var bestText = collapse(best.textContent);
      var pageText = collapse(document.body ? (document.body.innerText || document.body.textContent) : '');
      if (pageText.length > 400 && bestText.length > pageText.length * 0.82) {
        return { node: null, score: bestScore, rejected: '容器几乎等于整页' };
      }
      return { node: best, score: bestScore };
    }
    return { node: null, score: 0 };
  }

  /**
   * checkDuplicates(capture, existingJobs)
   *   existingJobs: 工作台 DB.jobs（{id,company,title,link,importance,...}）
   * 返回 { level: 'exact'|'same-company-title'|'same-link'|'similar'|'none',
   *        matches: [{job, reason, similarity}] }
   */
  function checkDuplicates(capture, existingJobs) {
    capture = capture || {};
    var f = capture.fields || {};
    var matches = [];
    var key = capture.key || dedupeKey(f.company, f.title);
    var link = (f.link || '').split('#')[0].replace(/\/$/, '');
    (existingJobs || []).forEach(function (j) {
      var jKey = dedupeKey(j.company, j.title);
      var reasons = [];
      if (j.id && j.id === capture.id) reasons.push('id 相同（同一岗位重复抓取）');
      if (jKey && jKey === key) reasons.push('企业与岗位名归一后一致');
      var jl = (j.link || '').split('#')[0].replace(/\/$/, '');
      if (link && jl && link === jl) reasons.push('链接完全相同');
      var sim = 0;
      if (!reasons.length) {
        sim = titleSimilarity(j.title, f.title);
        var sameCo = normalizeCompany(j.company) === normalizeCompany(f.company);
        if (sim >= 0.72 && sameCo) reasons.push('岗位名高度相似（' + Math.round(sim * 100) + '%）');
        else if (sim >= 0.86) reasons.push('岗位名高度相似（' + Math.round(sim * 100) + '%），公司名不同');
      }
      if (reasons.length) matches.push({ job: j, reason: reasons.join('；'), similarity: sim });
    });
    matches.sort(function (a, b) { return b.similarity - a.similarity; });

    var level = 'none';
    if (matches.some(function (m) { return /id 相同|归一后一致|链接完全/.test(m.reason); })) level = 'exact';
    else if (matches.length) level = 'similar';
    return { level: level, matches: matches.slice(0, 5) };
  }

  /* 岗位名相似度：去噪后取字符 bigram 的 Jaccard，并对「工程师/工程师助理」这类包含关系加权 */
  function titleSimilarity(a, b) {
    var x = normalizeTitle(a), y = normalizeTitle(b);
    if (!x || !y) return 0;
    if (x === y) return 1;
    if (x.indexOf(y) >= 0 || y.indexOf(x) >= 0) {
      var ratio = Math.min(x.length, y.length) / Math.max(x.length, y.length);
      return 0.7 + 0.3 * ratio;
    }
    var A = bigrams(x), B = bigrams(y);
    if (!A.size || !B.size) return 0;
    var inter = 0;
    A.forEach(function (g) { if (B.has(g)) inter++; });
    return (2 * inter) / (A.size + B.size);
  }

  function bigrams(s) {
    var set = new Set();
    for (var i = 0; i < s.length - 1; i++) set.add(s.slice(i, i + 2));
    if (!set.size && s.length) set.add(s);
    return set;
  }

  /**
   * buildPayload(capture, opts)
   *   opts.source     默认「浏览器插件」；来自候选池的写「招聘信息·浏览器插件」
   *   opts.batch      批次名，默认「插件抓取-YYYY-MM-DD」
   *   opts.importance 默认按岗位性质推断：校招 → can，社招 → hold
   * 产出的是 POST /api/jobs/capture 的 body（可直接投喂服务端）
   */
  function buildPayload(capture, opts) {
    opts = opts || {};
    var f = capture.fields || {};
    var meta = capture.meta || {};

    var source = opts.source || DEFAULT_SOURCE;
    var batch = opts.batch || ('插件抓取-' + todayStr());
    var importance = opts.importance || inferImportance(meta, f);

    /* 标签：工作台岗位卡按 job.tags 渲染为灰色小标签，这里给几个能立刻用的 */
    var tags = (opts.tags || []).slice();
    tags.push('浏览器插件');
    if (meta.talentType === '校招') tags.push('校招');
    else if (meta.talentType === '社招') tags.push('社招');
    if (f.city) {
      f.city.split(/[\/、]/).forEach(function (c) { if (c) tags.push(c); });
    }
    if (meta.adapter && meta.adapter !== 'generic') tags.push(meta.adapterName);
    tags = uniq(tags).slice(0, 12);

    var job = {
      id: capture.id || recordId(f.company, f.title),
      company: f.company || '',
      title: f.title || '',
      jd: f.jd || '',
      link: f.link || '',
      importance: importance,
      tags: tags,
      createdAt: todayStr(),
      /* ↓ 非 JOB_COLS 字段会被 write_all 平铺进 extra 列，前端按顶层键读 */
      city: f.city || '',
      salary: f.salary || '',
      education: f.education || '',
      experience: f.experience || '',
      department: f.department || '',
      headcount: f.headcount || '',
      jobType: f.jobType || '',
      deadline: f.deadline || '',
      publishDate: f.publishDate || '',
      source: source,
      sourceHost: meta.host || '',
      sourcePage: meta.title || '',
      sourceAdapter: meta.adapter || '',
      capturedAt: meta.extractedAt || nowStamp(),
      captureConfidence: capture.confidence ? capture.confidence.overall : null,
      batch: batch,
    };
    return {
      job: job,
      candidate: buildCandidate(job, capture, { source: opts.candidateSource || '招聘信息·浏览器插件', batch: batch }),
      meta: {
        key: capture.key,
        adapter: meta.adapter,
        confidence: capture.confidence,
        duplicates: opts.duplicates || null,
      },
    };
  }

  function inferImportance(meta, f) {
    var t = (meta.talentType || '').toString();
    if (t === '社招') return 'hold';
    if (t === '校招') {
      return needsThreeYears(f.experience) ? 'hold' : 'can';
    }
    /* 岗位性质未知：经验要求里提到 3 年以上的，按需慎重处理 */
    return needsThreeYears(f.experience) ? 'hold' : 'can';
  }

  /** 经验要求里是否明确要 3 年及以上（只看这一个信号） */
  function needsThreeYears(text) {
    var raw = collapse(text || '');
    if (!raw) return false;
    var m = /(\d{1,2})\s*[-~—至]\s*(\d{1,2})\s*年/.exec(raw)
         || /(\d{1,2})\s*年(?:以上|及以上|\+|起)/.exec(raw)
         || /(\d{1,2})\s*年/.exec(raw);
    if (!m) return false;
    var lo = parseInt(m[1], 10);
    if (isNaN(lo)) return false;
    if (/应届|毕业生|无经验|经验不限|不限经验|实习生/i.test(raw)) return false;
    return lo >= 3;
  }

  function stripHtml(html) {
    return clean(String(html)
      .replace(/<\s*br\s*\/?\s*>/gi, '\n')
      .replace(/<\s*\/\s*(p|div|li|tr|h[1-6])\s*>/gi, '\n')
      .replace(/<\s*li[^>]*>/gi, '· ')
      .replace(/<[^>]+>/g, '')
      .replace(/&nbsp;/g, ' ')
      .replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>')
      .replace(/&quot;/g, '"').replace(/&#39;/g, "'"));
  }

  function todayStr() {
    var d = new Date();
    function p(n) { return (n < 10 ? '0' : '') + n; }
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate());
  }

  function nowStamp() {
    var d = new Date();
    function p(n) { return (n < 10 ? '0' : '') + n; }
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' +
           p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
  }

  /** 入库时同步建一条候选池记录，保证「纳入」链路后续可复用 */
  function buildCandidate(job, capture, opts) {
    opts = opts || {};
    var f = capture.fields || {};
    var meta = capture.meta || {};
    return {
      id: job.id,
      title: job.title,
      company: job.company,
      city: job.city,
      salary: job.salary,
      education: job.education,
      experience: job.experience,
      source: opts.source || '招聘信息·浏览器插件',
      publish_date: f.publishDate || todayStr(),
      url: job.link,
      category: opts.category || CATEGORY,
      fair_date: '',
      fair_location: '',
      extra: {
        '批次': opts.batch || job.batch,
        '岗位来源': '浏览器插件',
        '来源域名': meta.host || '',
        '薪资口径': meta.salaryNormalized || null,
        '学历要求': meta.eduNormalized || null,
        '经验要求': meta.expNormalized || null,
        '岗位性质': meta.talentType || '',
        '截止时间': job.deadline || '',
        '部门': job.department || '',
        '页面标题': meta.title || '',
        '页面适配器': meta.adapterName || '通用启发式',
        '置信度': capture.confidence ? capture.confidence.overall : null,
        '抓取时间': meta.extractedAt || nowStamp(),
      },
    };
  }

  function aiCondenseText(opts) {
    opts = opts || {};
    var maxLen = opts.maxLen || 14000;
    var parts = [];
    var seen = {};

    function push(label, text) {
      var t = collapse(String(text || ''));
      if (!t || t.length < 2) return;
      var key = t.slice(0, 60);
      if (seen[key]) return;
      seen[key] = 1;
      parts.push(label ? '【' + label + '】\n' + t : t);
    }

    /* 1. 元信息 */
    try {
      var metaBits = [];
      if (typeof document !== 'undefined') {
        if (document.title) metaBits.push('页面标题：' + document.title);
        var md = document.querySelector('meta[name="description"]');
        if (md && md.content) metaBits.push('页面描述：' + collapse(md.content));
        var og = document.querySelector('meta[property="og:title"]');
        if (og && og.content && og.content !== document.title) metaBits.push('og标题：' + collapse(og.content));
      }
      if (metaBits.length) push('页面信息', metaBits.join('\n'));
    } catch (e) { /* 忽略 */ }

    /* 2. 标签-值区：把 "学历要求：硕士及以上" 这类行原样捞出来，
          对模型来说这是最直接的结构化线索 */
    try {
      if (typeof document !== 'undefined' && document.body) {
        var labelLines = collectLabelLines();
        if (labelLines.length) push('字段区', labelLines.join('\n'));
      }
    } catch (e) { /* 忽略 */ }

    /* 3. JD 正文：给足配额（占 maxLen 的大头）
     *
     * 这里**只在找到真正的 JD 容器时才填**。容器找不到就整段留空，
     * 宁可空着让用户手填，也绝不退回整页正文 —— 整页正文会把导航、
     * 推荐岗位、页脚、"相关职位"一起塞进来，是 JD 变脏的头号原因。
     * （旧版有个 `|| document.body.innerText` 的降级，已删除。）
     */
    var jdBudget = Math.floor(maxLen * 0.6);
    try {
      if (typeof document !== 'undefined') {
        var c = pickJdContainer();
        if (c && c.node) {
          var jdText = extractJdSections(clean(c.node.textContent));
          if (jdText && jdText.length >= 40) push('岗位正文', clip(jdText, jdBudget));
        }
      }
    } catch (e) { /* 忽略 */ }

    /* 4. 短文本块兜底：只在 JD 没找到时才用，
     *    而且必须通过噪声过滤 —— 这些块本来就比整页正文干净得多 */
    var remain = maxLen - parts.join('\n\n').length;
    if (remain > 800) {
      try {
        var chunks = collectShortBlocks(remain);
        if (chunks.length) push('页面其他信息', chunks.join('\n'));
      } catch (e) { /* 忽略 */ }
    }

    var out = parts.join('\n\n');
    if (out.length > maxLen) out = out.slice(0, maxLen);
    return out;
  }

  /**
   * extractJdSections(text)
   *   从一段（可能仍然很杂的）容器文本里，只保留"岗位职责 ~ 任职要求"这类
   *   正文段，砍掉前后夹带的导航/推荐/页脚。
   *
   * 做法：以 JD_SECTION_WORDS 的小标题为锚点。命中锚点就从锚点开始收，
   * 遇到明显的噪声行（相关职位/热门推荐/版权所有…）就截断。
   * 一个锚点都没有时，退一步用一个"整段看起来像 JD"的启发式判断。
   */
  function extractJdSections(text) {
    var t = clean(text);
    if (!t) return '';

    var lines = t.split('\n');
    /* 逐行扫，找第一个 JD 小标题的位置 */
    var anchor = -1;
    for (var i = 0; i < lines.length; i++) {
      if (JD_SECTION_RE.test(lines[i])) { anchor = i; break; }
      /* 也认 "1. 负责xxx" 这种直接以条列开头的行（有些页面没有小标题） */
      if (/^\s*(\d+[.、)]|[·•\-*])\s*\S/.test(lines[i]) && lines[i].length > 12) { anchor = i; break; }
    }

    if (anchor >= 0) {
      var kept = [];
      for (var j = anchor; j < lines.length; j++) {
        var ln = lines[j];
        if (!ln.trim()) { kept.push(''); continue; }
        /* 噪声行 = 截断点，不是跳过 —— 后面的都不属于 JD */
        if (AI_NOISE.test(ln)) break;
        if (/(版权所有|Copyright|联系我们|关于我们|友情链接|隐私政策|用户协议|扫码|关注我们|分享到|上一篇|下一篇|更多职位|全部职位|投递简历$)/i.test(ln)) break;
        kept.push(ln);
      }
      var res = clean(kept.join('\n'));
      /* 40 字门槛：太短的多半是"岗位职责：详见附件"这种残句，收它没意义 */
      if (res.length >= 40) return res;
      return '';
    }

    /* 没有锚点：只有当整段看着确实像 JD 才返回，否则宁可空着 */
    var hits = 0;
    JD_SECTION_WORDS.forEach(function (w) { if (t.indexOf(w) >= 0) hits++; });
    if (hits > 0 && t.length >= 40) return t;
    return '';
  }

  /* 捞「标签：值」形态的行 */
  function collectLabelLines() {
    var out = [];
    var nodes = document.querySelectorAll('dt, th, td, li, span, div, p, label');
    var n = Math.min(nodes.length, 900);
    for (var i = 0; i < n; i++) {
      var el0 = nodes[i];
      /* 只取叶子节点，避免把整块容器文本重复捞进来 */
      if (el0.children && el0.children.length > 2) continue;
      var t = collapse(el0.textContent);
      if (!t || t.length > 60) continue;
      if (t.indexOf('：') < 0 && t.indexOf(':') < 0) continue;
      if (!AI_KEEP_LABEL.test(t)) continue;
      if (AI_NOISE.test(t)) continue;
      out.push(t);
      if (out.length >= 40) break;
    }
    return out;
  }

  /* 收集短文本块（过滤导航/推荐/页脚） */
  function collectShortBlocks(budget) {
    var out = [];
    var used = 0;
    var nodes = document.querySelectorAll('div,section,article,p,li,td');
    var n = Math.min(nodes.length, 1500);
    for (var i = 0; i < n && used < budget; i++) {
      var el0 = nodes[i];
      if (el0.children && el0.children.length > 4) continue;
      var t = collapse(el0.textContent);
      if (!t) continue;
      if (t.length < 6 || t.length > 300) continue;
      if (AI_NOISE.test(t)) continue;
      if (!AI_KEEP_LABEL.test(t)) continue;
      var key = t.slice(0, 40);
      var dup = false;
      for (var j = 0; j < out.length; j++) {
        if (out[j].indexOf(key) === 0) { dup = true; break; }
      }
      if (dup) continue;
      out.push(t);
      used += t.length;
      if (out.length >= 25) break;
    }
    return out;
  }
  /* ============================================================
   * 列表页：把成行的岗位圈出来
   * ============================================================
   * 详情页那套已经全交给模型了；列表页不行 —— 一屏几十上百行，
   * 整页文本塞进一次 prompt 又贵又容易被导航/推荐位污染。
   * 所以这里先在浏览器侧做两件事：**认定哪些是岗位行**、粗略切字段；
   * 判断（推不推荐、为什么）留给服务端 —— 用户画像只在服务端。
   *
   * 刻意不给站点写适配器：靠"链接像岗位详情 + 结构上重复成行"
   * 这两个跨站共性，改版改类名都不受影响。
   */

  /* 岗位详情链接的样子：路径里有这些词，或 URL 上带着岗位 id */
  var LIST_HREF_KEY = /(?:^|[\/\-_.?&#=])(job|jobs|position|positions|post|posting|opening|openings|recruit|recruitment|career|careers|campus|zhaopin|offer|detail)/i;
  var LIST_HREF_ID = /[?&#](?:job|position|post|career|recruit|opening)?[_-]?id=|jobadid|jobid|positionid|postid|\/\d{6,}/i;
  var LIST_NEG_HREF = /(login|register|logout|download|help|faq|about|contact|privacy|terms|javascript:|mailto:|tel:)/i;
  /* `^li$` 从来没匹配上过（sig 是「类名 + id + 标签名」拼起来的串），改成词边界 */
  var ROW_CLASS_KEY = /(item|card|row|list|cell|job|position|post|panel|box|(?:^|\s)li(?:\s|$))/i;
  var COMPANY_BLACK = /(岗位|职位|招聘|求职|热门|推荐|相关|更多|在线|首页|简历|投递|城市|薪资)/;
  var COMPANY_SUFFIX = /(股份有限公司|有限责任公司|有限公司|股份|集团|控股|研究院|研究所|大学|学院|公司|工厂|厂|科技|技术|工业|实业|制造|汽车|重工)$/;
  /* 薪资必须带单位（K/W/千/万/元）才认 —— 否则「3-5年」会被当成薪资 */
  var SALARY_RE = /(?:\d+(?:\.\d+)?\s*[-~—至]\s*\d+(?:\.\d+)?\s*(?:K|k|W|w|千|万|元)(?:\s*[·xX*×]\s*\d+\s*薪)?|\d+\s*[-~至]\s*\d+\s*元|面议|薪资面议|待遇面议)/;
  var EDU_RE = /(博士|硕士|研究生|本科|大专|专科|中专|学历不限)/;
  /* 这些容器里的东西永远不是岗位列表 —— 导航栏/页脚被当成一行是最常见的噪音 */
  var ROW_NEG_TAGS = /^(NAV|HEADER|FOOTER|ASIDE|SCRIPT|STYLE|FORM|NOSCRIPT)$/;

  /** 往上爬几层看看是不是落在导航/页脚这类壳里 */
  function insideNeg(node) {
    var cur = node, guard = 0;
    while (cur && cur.tagName && guard++ < 5) {
      if (ROW_NEG_TAGS.test(cur.tagName)) return true;
      cur = cur.parentNode;
    }
    return false;
  }

  /* 岗位行的样子：用来把列表块和页脚友情链接/相关推荐区分开 */
  var JOBISH_RE = /(工程师|专员|经理|主管|设计师|分析师|顾问|助理|技师|研究员|实习生|管培生|培训生|应届|校招|社招|技术员|研发|仿真|工艺|测试|电气|机械|结构|热管理|气动|噪声|振动|NVH|CAE|CFD|标定|试验|博士|硕士)/;
  var EXP_RE = /(应届|实习生|在校生|在校\/应届|经验不限|\d+\s*[-~至]\s*\d+\s*年|\d+\s*年以上)/;

  /** 相对链接补全成绝对地址（缺 location/URL 的环境原样退回） */
  function absUrl(href) {
    href = String(href || '');
    if (!href) return '';
    if (/^https?:\/\//i.test(href)) return href;
    if (typeof location === 'undefined' || typeof URL === 'undefined') return href;
    try { return new URL(href, location.href).href; } catch (e) { return href; }
  }

  /**
   * 纯函数：一行原始文本 + 链接 → 结构化条目。
   * 只做"看得见的提取"，不猜 —— 猜的活一律留给模型。
   */
  function polishListRow(raw) {
    raw = raw || {};
    var text = collapse(raw.text || '');
    var title = collapse(raw.title || '')
      .replace(/^[【\[（(][^】\]）)]{0,20}[】\]）)]/, '')
      .trim();
    var company = guessCompany(text, title);
    var sal = SALARY_RE.exec(text) || null;
    var edu = EDU_RE.exec(text) || null;
    var exp = EXP_RE.exec(text) || null;
    return {
      title: title ? String(title).slice(0, 60) : '',
      company: company,
      salary: sal ? sal[0] : '',
      education: edu ? edu[1] : '',
      experience: exp ? exp[1] : '',
      link: String(raw.href || '').slice(0, 500),
      text: clip(text, 160),
    };
  }

  /** 候选岗位链接（浏览器专用）：跨站只用「链接形态 + 文字长度」这两把尺子 */
  function listLinkCandidates(root, limit) {
    var out = [];
    if (!root || !root.querySelectorAll) return out;
    var as;
    try { as = root.querySelectorAll('a[href]'); } catch (e) { return out; }
    var n = Math.min(as.length, limit || 600);
    for (var i = 0; i < n; i++) {
      var a = as[i];
      var href = a.getAttribute ? (a.getAttribute('href') || '') : '';
      if (!href) continue;
      /* 纯锚点（# / #top）跳过；#/xxx 这种 SPA 路由要留着 ——
       * 北森等门户的岗位详情路由就长在 # 后面，一刀切会把整页候选全砍掉 */
      if (href.charAt(0) === '#' && (href.length < 2 || href.charAt(1) !== '/')) continue;
      if (LIST_NEG_HREF.test(href)) continue;
      if (!LIST_HREF_KEY.test(href) && !LIST_HREF_ID.test(href)) continue;
      var text = collapse(a.textContent || '');
      if (!text || text.length < 3 || text.length > 40) continue;
      out.push(a);
    }
    return out;
  }

  /** 从链接往上找到"这一行"的容器；找不到就用链接自己 */
  /**
   * 一堆链接里才可能是"列表容器"，只含 0~1 个链接的才可能是"一行"。
   * 早退：数到 2 就返回。
   */
  function anchorCount(node, cap) {
    if (!node || !node.children) return 0;
    var n = 0, limit = cap || 400;
    var stack = [node], guard = 0;
    while (stack.length && guard++ < limit) {
      var e = stack.pop();
      if (e.tagName === 'A') { n++; if (n > 1) return n; }
      var kids = e.children;
      if (kids) for (var i = 0; i < kids.length; i++) stack.push(kids[i]);
    }
    return n;
  }

  function rowNodeFor(a) {
    var cur = a.parentNode;
    var guard = 0;
    while (cur && cur.tagName && cur.tagName !== 'BODY' && guard++ < 6) {
      var t = collapse(cur.textContent || '');
      if (t && t.length <= 400) {
        var sig = ((cur.className && typeof cur.className === 'string' ? cur.className : '') +
                   ' ' + (cur.id || '') + ' ' + cur.tagName).toLowerCase();
        /* 类名像列表项，且里面只挂了一个链接 —— 这才是"一行" */
        if (ROW_CLASS_KEY.test(sig) && anchorCount(cur) <= 1) return cur;
      }
      cur = cur.parentNode;
    }
    /* 一路没找到：栅格布局里父节点常常是**整个列表块**（某集团门户就是这种），
     * 直接拿它会把整块当成一行。里面塞着一堆链接的，就用锚点自己当行。 */
    var parent = a.parentNode;
    if (parent && !ROW_NEG_TAGS.test(parent.tagName || '') && anchorCount(parent) <= 1) return parent;
    return a;
  }

  /**
   * extractJobList(root, opts) —— 列表页抽取主入口（浏览器专用）。
   * 返回 [{title, company, salary, education, experience, link, text, node, anchor}]
   * node / anchor 只在浏览器里有，用来之后给这一行打标记，别往服务端传。
   */
  /**
   * 行本身就是容器时（卡片式列表，行里套着标题/地点/日期好几个元素）：
   * 优先取行内**最短的那一层标题元素**（h1~h4 / 带 title|name 类名的），
   * 再退到行内第一个链接的文字，最后才按分隔符切第一段。
   *
   * 为什么要这么挑：有的门户卡片整张就是 <a>，标题在里面的 <h3> 里；
   * 直接切整行文字的话，"【27校招 - 数字化】物理AI算法工程师" 会退化成「【27校招」。
   */
  function rowTitleOf(node) {
    var whole = collapse(node.textContent || '');
    /* 1) 行内第一个链接的文字 */
    try {
      var as = node.querySelectorAll ? node.querySelectorAll('a') : null;
      if (as) {
        for (var i = 0; i < as.length && i < 6; i++) {
          var ta = collapse(as[i].textContent || '');
          if (ta && ta.length >= 3 && ta.length <= 60) return ta;
        }
      }
    } catch (e) {}
    /* 2) 行内第一个"短而像标题"的元素 */
    var pick = shortestTitleEl(node, whole);
    if (pick) return pick;
    /* 3) 退化：整行第一段 */
    var segs = String(whole).split(/[\s|｜/／·•、,，\n]+/);
    for (var j = 0; j < segs.length; j++) {
      var s = collapse(segs[j]);
      if (s && s.length >= 3) return s.slice(0, 60);
    }
    return whole.slice(0, 60);
  }

  /** 广度优先地找行里第一个文字长度在 3~60 的元素（优先 h1~h4 / title|name 类名） */
  function shortestTitleEl(node, whole) {
    var fallback = '';
    var queue = [];
    var push = function (n) {
      var kids = n && n.children;
      if (!kids) return;
      for (var k = 0; k < kids.length; k++) queue.push(kids[k]);
    };
    push(node);
    var guard = 0;
    while (queue.length && guard++ < 300) {
      var el = queue.shift();
      if (!el || !el.tagName || ROW_NEG_TAGS.test(el.tagName)) continue;
      var t = collapse(el.textContent || '');
      var sig = ((el.className && typeof el.className === 'string' ? el.className : '') + ' ' + el.tagName).toLowerCase();
      var isHead = /^(h1|h2|h3|h4|h5|h6)$/.test(el.tagName.toLowerCase()) || /(title|name|post|job)/.test(sig);
      if (t && t.length >= 3 && t.length <= 60 && t.length <= whole.length * 0.8) {
        if (isHead) return t;
        if (!fallback) fallback = t;
      }
      if (el.children && el.children.length) push(el);
    }
    return fallback;
  }

  /** 一个块级行 → 一条岗位行（没有链接也能用，靠整行文字） */
  function rowFromNode(node, seen) {
    if (!node || insideNeg(node) || _dupNode(seen, node)) return null;
    var text = collapse(node.textContent || '');
    if (!text || text.length > 600) return null;
    var key = text.slice(0, 50).toLowerCase();
    if (seen[key]) return null;
    seen[key] = 1;
    var item = polishListRow({ title: rowTitleOf(node), text: text, href: '' });
    if (!item.title) return null;
    item.node = node;
    item.anchor = null;
    return item;
  }

  /** 同一个 DOM 节点只能产出一行 —— 导航栏里 6 个链接会各自撞出同一行容器，
   *  不去重就会变成 6 条「校园招聘/社会招聘/…」把真岗位挤掉 */
  function _dupNode(seen, node) {
    if (!node) return true;
    if (!seen._nodes) seen._nodes = [];
    if (seen._nodes.indexOf(node) >= 0) return true;
    seen._nodes.push(node);
    return false;
  }

  /** 一个锚点 → 一条岗位行；认不出来（空标题/整块容器/重复）返回 null */
  function rowFromAnchor(a, seen) {
    var row = rowNodeFor(a);
    if (!row || insideNeg(row) || insideNeg(a) || _dupNode(seen, row)) return null;
    var text = collapse(row.textContent || '');
    if (!text || text.length > 600) return null;
    var href = a.getAttribute ? (a.getAttribute('href') || '') : '';
    var key = (text.slice(0, 50) + '|' + href).toLowerCase();
    if (seen[key]) return null;
    seen[key] = 1;
    /* 标题统一走 rowTitleOf：卡片式列表里行就是那个 <a>，直接拿 a 的整段文字
     * 会把"校招/地点/日期"一起当成标题 */
    var item = polishListRow({
      title: rowTitleOf(row),
      text: text,
      href: absUrl(href),
    });
    if (!item.title) return null;
    item.node = row;
    item.anchor = a;
    return item;
  }

  /**
   * extractJobList(root, opts) —— 列表页抽取主入口（浏览器专用）。
   * 两条路：先按**链接形态**挑（准），不够 2 条再按**结构重复**补（宽）。
   * 第二条是 2026-10-12 补的：不少门户的链接是 /#/xxx、纯数字或 JS 伪链接，
   * 形态认定能靠结构兜底判成列表页，抽取却一条都出不来 —— 症状就是
   * 「明明是列表页，点了筛岗位说找不到岗位」。
   */
  function extractJobList(root, opts) {
    opts = opts || {};
    var limit = opts.limit || 150;
    var seen = {}, out = [];

    var links = listLinkCandidates(root, opts.scan);
    for (var i = 0; i < links.length && out.length < limit; i++) {
      var it = rowFromAnchor(links[i], seen);
      if (it) out.push(it);
    }

    if (out.length < 2) {
      var blk = repeatedRowBlocks(root, 6);
      for (var j = 0; j < blk.nodes.length && out.length < limit; j++) {
        var it2 = blk.mode === 'block' ? rowFromNode(blk.nodes[j], seen) : rowFromAnchor(blk.nodes[j], seen);
        if (it2) out.push(it2);
      }
    }
    return out;
  }

  /**
   * harvestRows(root, opts) —— **只收割，不判断**（浏览器专用）。
   *
   * 2026-10-12 起列表页判定整个交给模型：本地再用正则猜"这是不是列表页"
   * 必然反复踩空（链接形态千奇百怪，结构兜底又认不出 ul>li）。
   * 这里只做"把页面上所有看起来能当一行的东西连 DOM 节点一起捞出来"，
   * 编号后送给服务端，由模型决定哪几行是岗位、值不值得投。
   *
   * 两条来源：锚点行（有链接，便于标记与回跳）+ 重复块行（纯展示的列表）。
   * 不做任何"像不像岗位"的过滤 —— 那是模型的事。
   */
  function harvestRows(root, opts) {
    opts = opts || {};
    var limit = opts.limit || 200;
    var seen = {}, out = [];

    /* 第一条路：链接看着像岗位详情的那些锚点 */
    var links = listLinkCandidates(root, opts.scan || 1200);
    for (var i = 0; i < links.length && out.length < limit; i++) {
      var it = rowFromAnchor(links[i], seen);
      if (it) out.push(it);
    }

    /* 第二条路：**无条件**再扫一遍重复块，跟第一条路合并。
     *
     * 以前写成 `if (out.length < 3)` 才补 —— 结果导航栏里随手捞到 4 个链接，
     * 就把真正的 30 张岗位卡整块跳过（实测：4 行全是导航）。卡片式列表的
     * 整行文字普遍超过 40 字，根本进不了第一条路，只能靠这里兜住。 */
    var blocks = scanRowBlocks(root, 3, false);
    for (var b = 0; b < blocks.length && out.length < limit; b++) {
      var rows = blocks[b].rows;
      for (var j = 0; j < rows.length && out.length < limit; j++) {
        var it2 = rowFromNode(rows[j], seen);
        if (it2) out.push(it2);
      }
    }

    /* 第三条路：扁平式（一堆链接挂同一个父节点） */
    var flats = anchorRowBlocks(root, 3);
    for (var f = 0; f < flats.length && out.length < limit; f++) {
      var as = flats[f].anchors;
      for (var k = 0; k < as.length && out.length < limit; k++) {
        var it3 = rowFromAnchor(as[k], seen);
        if (it3) out.push(it3);
      }
    }
    return out;
  }

  /**
   * 从一行文本里挑企业名。
   * 不用模糊正则去匹配中间片段（试过，会抽出"股份有限公司"这种半个名字），
   * 而是按分隔符切成段，挑**第一段长得像企业名**的：命中企业后缀、不是标题本身、
   * 不含"岗位/推荐"这类噪声词。
   */
  function guessCompany(text, title) {
    var segs = String(text || '').split(/[\s|｜/／·•、,，]+/);
    var titleClean = collapse(title || '');
    for (var i = 0; i < segs.length; i++) {
      var seg = collapse(segs[i]);
      if (!seg || seg === titleClean) continue;
      if (COMPANY_BLACK.test(seg)) continue;
      if (COMPANY_SUFFIX.test(seg)) {
        return seg.slice(0, 40);
      }
    }
    return '';
  }

  /**
   * 结构兜底：链接形态认不出来时（不少门户的链接是 /#/xxx、纯数字，或干脆是
   * 点击后用 JS 跳的伪链接），就看有没有"同一父节点下重复出现一批短文字行"——
   * 这是列表页共有的结构特征。返回命中的那个父节点下的条数（0 表示不像列表）。
   *
   * 门槛取 6 且要求每条文字 ≥6 字，是为了跟导航菜单区分开：
   * 导航里的链接通常两三个字（首页/登录/校招）。
   */
  /**
   * 结构兜底：找出页面上"长得像列表"的那一块。
   *
   * 两条判据，取条数最多的那块：
   *   A. 扁平式：同一父节点下直接挂着 ≥need 个短文字链接（少见，但确实有）。
   *   B. 嵌套式：某个父节点下 ≥need 个子元素，文字长度都在 6~200 ——
   *      这才是 `<ul><li>…</li></ul>`、`<div class=list><div class=item>…</div></div>`
   *      的真实长相。**老版本只有 A，而真实列表页每个链接各有各的父节点，
   *      A 永远凑不满，所以"明明是列表页却认不出来"。**
   *
   * B 额外要求"看起来像岗位行"（`looksJobish`），否则页脚友情链接、
   * 详情页底部的相关岗位推荐这类块也会被当成列表 —— 它们的文字长度同样达标。
   */
  /**
   * 扫出页面上所有"重复成块"的容器，按条数从多到少返回。
   *
   * 判据：某个父节点下有 ≥need 个子元素，文字长度都在 6~200，且至少一半
   * `looksJobish` —— 否则页脚友情链接、"相关推荐"这类块也会混进来。
   *
   * **返回全部合格块而不是只返回最多的那个**：一个列表页常常不止一块
   * （热门职位 + 全部职位分两块渲染、栅格布局拆成两列列容器），
   * 只取一块就会漏掉另一块的岗位 —— "明明 300 多条只抓到几十条"多半是这个。
   */
  function scanRowBlocks(root, min, needJobish) {
    var need = min || 3;
    var out = [];
    if (!root || !root.querySelectorAll) return out;
    var els;
    try { els = root.querySelectorAll('*'); } catch (e) { return out; }
    var m = Math.min((els && els.length) || 0, 4000);
    for (var i = 0; i < m; i++) {
      var el = els[i];
      var kids = el && el.children;
      if (!kids || kids.length < need) continue;
      var rows = [], jobish = 0;
      for (var j = 0; j < kids.length; j++) {
        var kid = kids[j];
        if (!kid || !kid.tagName) continue;
        if (ROW_NEG_TAGS.test(kid.tagName)) continue;
        var t = collapse(kid.textContent || '');
        if (!t || t.length < 6 || t.length > 200) continue;
        if (looksJobish(t)) jobish++;
        rows.push(kid);
      }
      /* needJobish=false 时跳过这一条（收割用）：形态判定早已交给模型，
       * 再拿一张中文岗位词表卡一半，只会把「客户成功FDE」「校园招聘大使」「供应链管培」
       * 这类行所在的整个列表块判不合格 —— 实测因此丢了 24/30。 */
      if (needJobish !== false && jobish * 2 < rows.length) continue;
      if (insideNeg(el)) continue;
      out.push({ parent: el, rows: rows, count: rows.length });
    }
    out.sort(function (a, b) { return b.count - a.count; });
    return out;
  }

  /**
   * 扁平式：同一个父节点下直接挂着一批短文字链接。
   * 真实列表页不常见（大多是 ul>li），但部分老门户就是 `<div><a/><a/>…</div>`，
   * 抽掉这条会让这类页面整页失明。
   */
  function anchorRowBlocks(root, min) {
    var need = min || 3;
    var out = [];
    if (!root || !root.querySelectorAll) return out;
    var as;
    try { as = root.querySelectorAll('a'); } catch (e) { return out; }
    var buckets = [], groups = [];
    var n = Math.min((as && as.length) || 0, 600);
    for (var i = 0; i < n; i++) {
      var a = as[i];
      var t = collapse(a.textContent || '');
      if (!t || t.length < 6 || t.length > 40) continue;
      var parent = a.parentNode;
      if (!parent) continue;
      var k = buckets.indexOf(parent);
      if (k < 0) { buckets.push(parent); groups.push([]); k = buckets.length - 1; }
      groups[k].push(a);
    }
    for (var g = 0; g < groups.length; g++) {
      if (groups[g].length < need || insideNeg(buckets[g])) continue;
      out.push({ parent: buckets[g], anchors: groups[g], count: groups[g].length });
    }
    out.sort(function (x, y) { return y.count - x.count; });
    return out;
  }

  function repeatedRowBlocks(root, min) {
    var need = min || 6;
    var blocks = scanRowBlocks(root, need);
    if (blocks.length) {
      var b = blocks[0];
      return { count: b.count, parent: b.parent, nodes: b.rows, mode: 'block' };
    }
    var flat = anchorRowBlocks(root, need);
    if (flat.length) {
      return { count: flat[0].count, parent: flat[0].parent, nodes: flat[0].anchors, mode: 'anchor' };
    }
    return { count: 0, parent: null, nodes: [], mode: '' };
  }

  function repeatedRows(root, min) {
    return repeatedRowBlocks(root, min).count;
  }

  /** 这行文字像不像一条岗位：有岗位名尾缀，或有薪资/学历/经验标记 */
  function looksJobish(text) {
    var t = String(text || '');
    return JOBISH_RE.test(t) || SALARY_RE.test(t) || EDU_RE.test(t) || EXP_RE.test(t);
  }

  function repeatedRows(root, min) {
    return repeatedRowBlocks(root, min).count;
  }

  /**
   * 轻量探测：这页够不够格当列表页。
   * 两条路，命中任一就算：**链接长得像岗位详情**（准），或**结构上重复成行**（宽）。
   * 列表页常常是 XHR 异步渲染的，所以判定要能被反复调用，不能只判一次。
   */
  function looksLikeListPage(root, min) {
    var need = min || 4;
    if (listLinkCandidates(root, 200).length >= need) return true;
    return repeatedRows(root, Math.max(need + 2, 6)) > 0;
  }

  /** 轻量：只报两条路各自找到多少候选，用来给用户看"到底卡在哪一步" */
  function listScanCounts(root) {
    var c = { links: 0, structural: 0 };
    if (!root || !root.querySelectorAll) return c;
    try { c.links = listLinkCandidates(root, 200).length; } catch (e) {}
    try { c.structural = repeatedRowBlocks(root, 6).count; } catch (e) {}
    return c;
  }

  /** 完整：再多跑一次抽取，报出最终条数和前几条标题（失败时才调，别放进轮询） */
  function listDiagnose(root) {
    var d = listScanCounts(root);
    d.rows = 0;
    d.sample = [];
    try {
      var items = extractJobList(root, { limit: 200 });
      d.rows = items.length;
      d.sample = items.slice(0, 3).map(function (it) {
        return (it.title || '(无标题)') + (it.company ? ' · ' + it.company : '');
      });
    } catch (e) {}
    return d;
  }

  return {
    aiCondenseText: aiCondenseText,
    extractJdSections: extractJdSections,
    pickJdContainer: pickJdContainer,
    buildPayload: buildPayload,
    checkDuplicates: checkDuplicates,
    inferImportance: inferImportance,
    dedupeKey: dedupeKey,
    recordId: recordId,
    normalizeTitle: normalizeTitle,
    normalizeCompany: normalizeCompany,
    shortCompany: shortCompany,
    titleSimilarity: titleSimilarity,
    clean: clean, collapse: collapse, clip: clip, uniq: uniq,
    stripHtml: stripHtml, md5: md5, metaContent: metaContent,
    /* 列表页 */
    absUrl: absUrl,
    polishListRow: polishListRow,
    listLinkCandidates: listLinkCandidates,
    rowNodeFor: rowNodeFor,
    extractJobList: extractJobList,
    harvestRows: harvestRows,
    rowFromNode: rowFromNode,
    rowTitleOf: rowTitleOf,
    looksJobish: looksJobish,
    scanRowBlocks: scanRowBlocks,
    repeatedRows: repeatedRows,
    rowTitleOf: rowTitleOf,
    repeatedRowBlocks: repeatedRowBlocks,
    looksLikeListPage: looksLikeListPage,
    listScanCounts: listScanCounts,
    listDiagnose: listDiagnose,
    VERSION: 2,
    DEFAULT_SOURCE: DEFAULT_SOURCE,
    IMPORTANCE_VALUES: IMPORTANCE_VALUES,
  };
});
