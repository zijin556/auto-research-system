/* 新版页面：共享状态、安全的 HTML 模板、请求、演示数据（?mock=1）。 */
(function () {
  'use strict';
  const MD = window.NBMarkdown;
  const NB = window.NB = {};
  const params = new URLSearchParams(location.search);

  NB.S = {
    mock: params.get('mock') === '1',
    mockNoSession: params.get('session') === '0',
    mockData: null,
    tree: null,
    item: null,
    itemError: '',
    usage: null,
    settingsView: null,
    route: { ref: 'FW', fw: '' },
    fw: '',
    session: null,        // newest snapshot of the session being followed
    ended: null,          // a finished session, shown until dismissed
    edit: null,           // {kind: 'doc' | 'fields' | 'anchor', ref, revision, error}
    open: new Set(),      // expanded sections (keys)
    ledgerFilter: 'all',
    ledgerMenu: '',       // ledger entry id whose state menu is open
    next: {},             // per-item drafts of the 下一轮 box: {recipe, mode, rounds, focus}
    busy: false,
  };

  // ---- safe HTML templating: values are escaped unless wrapped with raw() ----
  class Raw {
    constructor(s) { this.s = s; }
    toString() { return this.s; }
  }
  function fmt(v) {
    if (v == null || v === false) return '';
    if (v instanceof Raw) return v.s;
    if (Array.isArray(v)) return v.map(fmt).join('');
    return MD.escapeHtml(String(v));
  }
  NB.raw = s => new Raw(String(s == null ? '' : s));
  NB.html = function (strings, ...vals) {
    let out = strings[0];
    for (let i = 0; i < vals.length; i++) out += fmt(vals[i]) + strings[i + 1];
    return new Raw(out);
  };
  NB.esc = MD.escapeHtml;
  NB.md = (text, base) => NB.raw('<div class="md">' + MD.render(text || '', { base: base || '' }) + '</div>');
  NB.mdInline = (text, base) => NB.raw(MD.renderInline(text || '', { base: base || '' }));
  NB.dirname = p => { const s = String(p || ''); const i = s.lastIndexOf('/'); return i > 0 ? s.slice(0, i) : ''; };
  NB.basename = p => { const s = String(p || ''); return s.slice(s.lastIndexOf('/') + 1); };
  NB.typeset = el => {
    if (!el || typeof window.renderMathInElement !== 'function') return;
    try {
      window.renderMathInElement(el, {
        delimiters: [{ left: '$$', right: '$$', display: true }, { left: '\\[', right: '\\]', display: true },
          { left: '\\(', right: '\\)', display: false }, { left: '$', right: '$', display: false }],
        throwOnError: false, trust: false, maxExpand: 1000, strict: 'ignore', ignoredClasses: ['no-math'],
      });
    } catch (e) { /* leave the TeX source visible */ }
  };

  // ---- labels ----
  NB.L = {
    session: { queued: '排队中', running: '进行中', paused: '等你决定', done: '已完成', stopped: '已停止', failed: '失败', cancelled: '已取消' },
    round: { running: '进行中', paused: '暂停', done: '完成', failed: '失败', cancelled: '已取消', stopped: '已停止' },
    stepIcon: { pending: '○', running: '◔', done: '✓', skipped: '–', failed: '✕' },
    step: { pending: '等待', running: '进行中', done: '完成', skipped: '跳过', failed: '失败' },
    qTone: { discussing: 'info', pending: 'warn', running: 'info', needs_judgment: 'warn', review: 'bad', supported: 'good',
      refuted: 'muted', inconclusive: 'muted', not_required: 'muted' },
    chapterRole: { intro: '绪论', theory: '理论', research: '研究问题', framework: '随框架而定', conclusion: '总结与展望', other: '其他' },
    fwStatus: { active: '主推', candidate: '候选', parked: '搁置' },
    anchor: { none: '未起草', draft: '草稿', confirmed: '已确认' },
    decision: { supported: '支持', refuted: '不支持', inconclusive: '证据不足' },
    outcomeTone: { pass: 'good', fail: 'bad', partial: 'warn', error: 'bad', pending: 'muted' },
    agentRole: { author: '作者', reviewer: '审查', engineer: '工程', writer: '写作', drift: '漂移检查', referee: '审稿' },
    level: { framework_layer: '论文框架', framework: '框架', chapter: '章', question: '小问题' },
  };
  NB.ACTIVE = new Set(['queued', 'running', 'paused']);

  NB.fmtTime = iso => {
    if (!iso) return '';
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return String(iso);
    const p = x => String(x).padStart(2, '0');
    const head = d.getFullYear() === new Date().getFullYear() ? '' : d.getFullYear() + '-';
    return head + p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
  };
  NB.fmtDur = s => {
    const v = Number(s) || 0;
    if (v < 60) return Math.round(v) + ' 秒';
    return Math.floor(v / 60) + ' 分 ' + String(Math.round(v % 60)).padStart(2, '0') + ' 秒';
  };
  NB.REF_RE = /^(FW|F\d{2,}|C\d{2,}(?:-Q\d{2,})?)$/;
  NB.refHash = (ref, fw) => '#/' + ref + (fw && /^C\d/.test(ref) ? '?fw=' + encodeURIComponent(fw) : '');
  NB.uid = () => (window.crypto && crypto.randomUUID ? crypto.randomUUID()
    : 'r-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 10));
  NB.store = {
    get(key) { try { return localStorage.getItem('nb.v2.' + key) || ''; } catch (e) { return ''; } },
    set(key, value) { try { localStorage.setItem('nb.v2.' + key, value); } catch (e) { /* private mode */ } },
  };

  NB.toast = (message, kind) => {
    const box = document.getElementById('toasts');
    if (!box) return;
    const el = document.createElement('div');
    el.className = 'toast' + (kind ? ' toast-' + kind : '');
    el.innerHTML = NB.html`<span>${message}</span><button type="button" class="toast-x" aria-label="关闭提示">×</button>`.s;
    el.querySelector('button').addEventListener('click', () => el.remove());
    box.appendChild(el);
    setTimeout(() => el.remove(), kind === 'error' ? 9000 : 4000);
  };

  // ---- requests ----
  class ApiError extends Error {
    constructor(message, status, type) {
      super(message);
      this.status = status || 0;
      this.type = type || '';
    }
  }
  NB.ApiError = ApiError;

  async function parse(res) {
    const text = await res.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch (e) { data = null; }
    if (!res.ok) throw new ApiError((data && data.error) || '请求没有完成（HTTP ' + res.status + '）', res.status, data && data.type);
    return data;
  }

  NB.get = async path => {
    if (NB.S.mock) return mockGet(path);
    let res;
    try {
      res = await fetch(path, { headers: { Accept: 'application/json' }, cache: 'no-store' });
    } catch (e) {
      throw new ApiError('无法连接本地服务。请确认科研文件管理系统正在运行。', 0, 'network');
    }
    return parse(res);
  };

  NB.post = async (action, payload) => {
    if (NB.S.mock) {
      NB.toast('演示模式：没有提交（' + action + '）');
      return null;
    }
    let res;
    try {
      res = await fetch('/api/action', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Research-Notebook': '1' },
        body: JSON.stringify(Object.assign({}, payload || {}, { action })),
      });
    } catch (e) {
      throw new ApiError('无法连接本地服务，操作没有提交。', 0, 'network');
    }
    return parse(res);
  };

  // Text of a research file (for the viewer). Large files are not loaded.
  NB.fetchFile = async path => {
    if (NB.S.mock) {
      const m = await loadMock();
      if (m.files && Object.prototype.hasOwnProperty.call(m.files, path)) return { text: m.files[path], size: m.files[path].length };
    }
    let res;
    try {
      res = await fetch(MD.fileUrl(path), { cache: 'no-store' });
    } catch (e) {
      throw new ApiError('无法连接本地服务。', 0, 'network');
    }
    if (!res.ok) return parse(res);
    const size = Number(res.headers.get('Content-Length') || 0);
    if (size > 3 * 1024 * 1024) {
      try { await res.body.cancel(); } catch (e) { /* ignore */ }
      return { tooLarge: true, size };
    }
    return { text: await res.text(), size };
  };

  // ---- demo data (?mock=1): GETs come from mock.json, POSTs only show a toast ----
  async function loadMock() {
    if (!NB.S.mockData) {
      const res = await fetch('/static/v2/mock.json', { cache: 'no-store' });
      if (!res.ok) throw new ApiError('找不到演示数据 mock.json', res.status);
      const m = await res.json();
      if (NB.S.mockNoSession) {
        m.tree.session = null;
        m.session = null;
        Object.values(m.items || {}).forEach(it => {
          it.session = null;
          it.pending = (it.pending || []).filter(p => p.kind !== 'learn');
        });
      }
      NB.S.mockData = m;
    }
    return NB.S.mockData;
  }

  function treeTitle(m, ref) {
    for (const f of m.tree.frameworks || []) {
      if (f.id === ref) return f.title;
      for (const c of f.chapters || []) {
        if (c.id === ref) return c.title;
        const q = (c.questions || []).find(x => x.id === ref);
        if (q) return q.title;
      }
    }
    for (const c of m.tree.unplaced || []) if (c.id === ref) return c.title;
    return ref;
  }

  function placeholderItem(m, ref, fw) {
    const level = ref === 'FW' ? 'framework_layer' : /^F\d/.test(ref) ? 'framework' : /-Q\d/.test(ref) ? 'question' : 'chapter';
    const title = treeTitle(m, ref);
    return {
      ref, fw, level, title,
      breadcrumb: [{ ref: 'FW', title: '论文框架' }, { ref, title }],
      chapter: null,
      doc: level === 'framework_layer' ? null : { path: '', text: '（演示数据里没有这一项的正文。）', revision: '', kind: 'chapter_draft' },
      anchor: { path: '', revision: '', exists: false, level, version: 0, status: 'draft', confirmed_at: '', slots: [],
        assumptions_label: '假设', assumptions: [], requests: [] },
      ledger: { path: '', revision: '', entries: [], counts: { 待定: 0, 确定: 0, 否定: 0 } },
      pending: [], rounds: [], latest_brief: '', session: null,
      recipes: [{ key: 'open', label: '起草锚点和骨架' }], default_recipe: 'open',
      materials: null, upstream: [], children: [], frameworks: [], stage: null, proposed_questions: [], candidates: [],
      status: '', status_label: '', judgment: null, can_adopt: false, staged_conclusions: [],
    };
  }

  async function mockGet(path) {
    const m = await loadMock();
    const u = new URL(path, location.origin);
    const name = u.pathname.replace('/api/v2/', '');
    const q = key => u.searchParams.get(key) || '';
    const clone = v => (v == null ? v : JSON.parse(JSON.stringify(v)));
    if (name === 'tree') return clone(m.tree);
    if (name === 'item') {
      const it = clone((m.items || {})[q('ref')] || placeholderItem(m, q('ref'), q('fw')));
      it.fw = q('fw');
      return it;
    }
    if (name === 'session') return clone(m.session);
    if (name === 'settings') return clone(m.settings);
    if (name === 'usage') return clone(m.usage);
    if (name === 'git') return clone(m.tree.git);
    if (name === 'history') return clone(m.history || { commits: [] });
    if (name === 'version') {
      return { path: q('path'), rev: q('rev'), text: (m.versions || {})[q('rev')] || '（演示数据里没有这个版本。）' };
    }
    throw new ApiError('演示数据里没有这个接口', 404);
  }
}());
