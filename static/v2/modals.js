/* 新版页面：对话框——通用输入框、文件查看器、设置、历史版本、采纳。 */
(function () {
  'use strict';
  const NB = window.NB;
  const { S, html, raw } = NB;
  const M = NB.M = {};
  const MD = window.NBMarkdown;
  const IMAGE = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp']);
  const TEXT = new Set(['md', 'markdown', 'txt', 'log', 'py', 'm', 'jl', 'r', 'c', 'cpp', 'h', 'hpp', 'tex', 'bib', 'json', 'yaml', 'yml',
    'toml', 'ipynb', 'csv', 'tsv', 'sh', 'ps1', 'cmd', 'bat', 'ini', 'cfg']);
  const extOf = path => ((String(path).match(/\.([^./]+)$/) || [])[1] || '').toLowerCase();

  M.close = id => { const d = document.getElementById(id); if (d && d.open) d.close(); };

  // ---- generic form dialog: resolves with the values (or the submit() result), or null when cancelled ----
  M.field = function (f) {
    const req = f.required ? raw(' required') : '';
    const star = f.required ? '（必填）' : '';
    if (f.type === 'textarea') {
      return html`<label class="field"><span>${f.label}${star}</span><textarea name="${f.name}" rows="${f.rows || 4}" placeholder="${f.placeholder || ''}"${req}>${f.value || ''}</textarea></label>`;
    }
    if (f.type === 'select') {
      return html`<label class="field"><span>${f.label}</span><select name="${f.name}">${f.options.map(([v, l]) => html`<option value="${v}" ${String(v) === String(f.value) ? raw('selected') : ''}>${l}</option>`)}</select></label>`;
    }
    return html`<label class="field"><span>${f.label}${star}</span><input name="${f.name}" value="${f.value || ''}" placeholder="${f.placeholder || ''}"${req}></label>`;
  };

  let askSeq = 0;
  M.ask = function (opts) {
    const dlg = document.getElementById('ask');
    if (dlg.open) dlg.close();
    const mine = ++askSeq;
    return new Promise(resolve => {
      let done = false;
      const finish = value => { if (!done) { done = true; resolve(value); } };
      // 'close' events are queued: one that belongs to an earlier dialog must not end this one.
      const onClose = () => {
        if (askSeq === mine && dlg.open) return;
        dlg.removeEventListener('close', onClose);
        finish(null);
      };
      dlg.innerHTML = html`<form class="dlg-body" novalidate>
        <header class="dlg-head"><h2 id="ask-title">${opts.title}</h2><button type="button" class="x" data-close aria-label="关闭">×</button></header>
        <div class="dlg-scroll">${opts.intro ? html`<div class="intro">${typeof opts.intro === 'string' ? html`<p>${opts.intro}</p>` : opts.intro}</div>` : ''}
          ${(opts.fields || []).map(M.field)}
          <div class="notice notice-bad" role="alert" hidden></div></div>
        <footer class="dlg-foot"><button type="submit" class="${opts.danger ? 'danger' : 'primary'}">${opts.ok || '确定'}</button><button type="button" data-close>取消</button></footer>
      </form>`.s;
      const form = dlg.querySelector('form');
      const error = form.querySelector('[role="alert"]');
      const fail = msg => { error.textContent = msg; error.hidden = false; };
      form.querySelectorAll('[data-close]').forEach(b => b.addEventListener('click', () => dlg.close()));
      dlg.addEventListener('close', onClose);
      form.addEventListener('submit', async ev => {
        ev.preventDefault();
        const values = {};
        for (const f of opts.fields || []) {
          const el = form.elements[f.name];
          values[f.name] = el ? el.value : '';
          if (f.required && !String(values[f.name]).trim()) { fail('请填写“' + f.label + '”。'); el.focus(); return; }
        }
        const submitBtn = form.querySelector('[type="submit"]');
        try {
          submitBtn.disabled = true;
          const result = opts.submit ? await opts.submit(values) : values;
          finish(result === undefined ? values : result);
          dlg.close();
        } catch (e) {
          fail(e.message || String(e));
        } finally {
          submitBtn.disabled = false;
        }
      });
      dlg.showModal();
      const first = form.querySelector('input, textarea, select');
      (first || form.querySelector('[type="submit"]')).focus();
    });
  };

  M.confirm = (title, text, ok, danger) => M.ask({ title, intro: text, ok: ok || '继续', danger }).then(v => v !== null);

  // ---- file viewer ----
  M.view = { stack: [], raw: false };

  function parseRange(hash) {
    const m = String(hash || '').match(/^L(\d+)(?:-L?(\d+))?$/i);
    if (!m) return null;
    const a = Number(m[1]);
    const b = m[2] ? Number(m[2]) : a;
    return { from: Math.min(a, b), to: Math.max(a, b) };
  }

  function codeView(text, hash) {
    const range = parseRange(hash);
    const lines = String(text).replace(/\r\n?/g, '\n').split('\n');
    if (lines.length > 1 && lines[lines.length - 1] === '') lines.pop();
    const limit = 20000;
    const rows = lines.slice(0, limit).map((l, i) => {
      const n = i + 1;
      const hit = range && n >= range.from && n <= range.to;
      return '<tr class="' + (hit ? 'hl' : '') + '" data-line="' + n + '"><td class="ln">' + n + '</td><td class="lc">' + (NB.esc(l) || ' ') + '</td></tr>';
    }).join('');
    return '<div class="code-view no-math"><table><tbody>' + rows + '</tbody></table></div>'
      + (lines.length > limit ? '<p class="hint">文件较长，只显示前 ' + limit + ' 行。</p>' : '');
  }

  function parseCsv(text, maxRows) {
    const first = text.slice(0, text.indexOf('\n') >= 0 ? text.indexOf('\n') : text.length);
    const sep = (first.match(/\t/g) || []).length > (first.match(/,/g) || []).length ? '\t' : ',';
    const rows = [];
    let row = [];
    let cell = '';
    let quoted = false;
    for (let i = 0; i < text.length && rows.length < maxRows; i++) {
      const ch = text[i];
      if (quoted) {
        if (ch === '"' && text[i + 1] === '"') { cell += '"'; i++; } else if (ch === '"') quoted = false; else cell += ch;
      } else if (ch === '"' && !cell) quoted = true;
      else if (ch === sep) { row.push(cell); cell = ''; }
      else if (ch === '\n' || ch === '\r') {
        if (ch === '\r' && text[i + 1] === '\n') i++;
        row.push(cell); rows.push(row); row = []; cell = '';
      } else cell += ch;
    }
    if ((cell || row.length) && rows.length < maxRows) { row.push(cell); rows.push(row); }
    return rows;
  }

  function csvView(text) {
    const rows = parseCsv(text, 201);
    if (!rows.length) return '<p class="muted">（空文件）</p>';
    const total = (text.match(/\n/g) || []).length + (text.endsWith('\n') ? 0 : 1);
    const head = rows[0];
    const body = rows.slice(1);
    return html`<div class="table-wrap"><table class="grid csv"><thead><tr>${head.map(c => html`<th>${c}</th>`)}</tr></thead>
      <tbody>${body.map(r => html`<tr>${head.map((_, k) => html`<td>${r[k] || ''}</td>`)}</tr>`)}</tbody></table></div>
      ${total - 1 > body.length ? html`<p class="hint">只显示前 ${body.length} 行数据（共约 ${total - 1} 行）。完整内容请下载。</p>` : ''}`.s;
  }

  M.openFile = async function (path, hash, push) {
    const dlg = document.getElementById('viewer');
    if (!dlg.open) { M.view.stack = []; M.view.raw = false; dlg.showModal(); }
    if (push !== false) M.view.stack.push({ path, hash: hash || '' });
    const ext = extOf(path);
    const name = NB.basename(path);
    const back = M.view.stack.length > 1;
    const isMd = ext === 'md' || ext === 'markdown';
    dlg.innerHTML = html`<div class="dlg-body viewer">
      <header class="dlg-head">${back ? html`<button type="button" class="quiet" data-action="viewer-back">← 返回</button>` : ''}
        <div class="vt"><h2 id="viewer-title">${name}</h2><span class="muted small path">${path}${hash ? '#' + hash : ''}</span></div>
        <div class="actions">${isMd ? html`<button type="button" class="quiet" data-action="viewer-raw" aria-pressed="${M.view.raw}">${M.view.raw ? '排版' : '原文'}</button>` : ''}
          <button type="button" class="quiet" data-action="open-folder" data-path="${path}">打开所在文件夹</button>
          <a class="btn quiet" href="${MD.fileUrl(path, true)}">下载</a>
          <button type="button" class="x" data-action="close-dialog" data-key="viewer" aria-label="关闭">×</button></div>
      </header>
      <div class="viewer-body" id="viewer-body"><p class="muted">正在读取…</p></div></div>`.s;
    const body = dlg.querySelector('#viewer-body');
    if (IMAGE.has(ext)) {
      body.innerHTML = html`<figure class="img-view"><img src="${MD.fileUrl(path)}" alt="${name}"></figure>`.s;
      return;
    }
    if (!TEXT.has(ext)) {
      body.innerHTML = html`<p>这种文件不能在页面里显示。</p><p><a class="btn primary" href="${MD.fileUrl(path, true)}">下载 ${name}</a></p>`.s;
      return;
    }
    let file;
    try {
      file = await NB.fetchFile(path);
    } catch (e) {
      body.innerHTML = html`<div class="notice notice-bad" role="alert">${e.message}</div>`.s;
      return;
    }
    if (!dlg.open || M.view.stack[M.view.stack.length - 1].path !== path) return;
    if (file.tooLarge) {
      body.innerHTML = html`<p>文件较大（${Math.round(file.size / 1024 / 1024)} MB），请下载后查看。</p><p><a class="btn primary" href="${MD.fileUrl(path, true)}">下载 ${name}</a></p>`.s;
      return;
    }
    if (isMd && !M.view.raw) {
      body.innerHTML = '<article class="paper">' + NB.md(file.text, NB.dirname(path)).s + '</article>';
      NB.typeset(body);
    } else if (ext === 'csv' || ext === 'tsv') {
      body.innerHTML = csvView(file.text);
    } else {
      body.innerHTML = codeView(file.text, hash);
      const target = body.querySelector('tr.hl');
      if (target) target.scrollIntoView({ block: 'center' });
    }
  };

  M.viewerBack = function () {
    if (M.view.stack.length < 2) return;
    M.view.stack.pop();
    const top = M.view.stack[M.view.stack.length - 1];
    M.openFile(top.path, top.hash, false);
  };

  M.viewerRaw = function () {
    M.view.raw = !M.view.raw;
    const top = M.view.stack[M.view.stack.length - 1];
    if (top) M.openFile(top.path, top.hash, false);
  };

  // ---- settings ----
  const KNOWN = [
    'codex:gpt-6.1-sol', 'codex:gpt-6.1-terra', 'codex:gpt-6-astra', 'codex:gpt-6-sol', 'codex:gpt-6-luna',
    'codex:gpt-5.6-sol', 'codex:gpt-5.6-terra', 'codex:gpt-5.6-luna', 'codex:gpt-reserve',
    'agy:gemini-4-argon-high', 'agy:gemini-4-argon-medium', 'agy:gemini-4-argon-low',
    'agy:gemini-3.8-flash-high', 'agy:gemini-3.8-flash-medium', 'agy:gemini-3.8-flash-low',
    'claude:claude-opus-5-5', 'claude:claude-sonnet-5-5', 'claude:claude-haiku-5-5', 'claude:claude-fable-5-1',
  ];
  const EFFORT_OPTS = [
    ['', '默认强度'], ['low', 'low（低）'], ['medium', 'medium（中）'], ['high', 'high（高）'],
    ['xhigh', 'xhigh（超高）'], ['max', 'max（最大）'], ['ultra', 'ultra（极高·Codex）'],
  ];
  const VIA = { codex: 'Codex', agy: 'Antigravity', claude: '中转站' };
  const PROVIDERS = [['codex', 'Codex 命令行（ChatGPT 订阅）'], ['agy', 'Antigravity 命令行（Google 订阅，仅用 Gemini）'], ['claude', 'Claude Code 中转站（按 token 计费）']];
  const TEST_DEFAULT = { codex: 'gpt-6.1-sol', agy: 'gemini-3.8-flash-medium', claude: 'claude-haiku-5-5' };
  const BUCKETS = { codex: 'Codex', 'agy:gemini': 'Antigravity · Gemini', claude: '中转站 · Claude' };
  const LIMITS = { codex_stop_percent: ['Codex 周额度用到多少就停用（%）', '1'], claude_per_call_usd: ['中转站单次调用上限（美元）', 'any'],
    claude_month_usd: ['中转站每月上限（美元）', 'any'], agy_cooldown_hours: ['Antigravity 额度用尽后暂停（小时）', 'any'],
    rounds_default: ['默认连跑轮数', '1'], max_runs_per_round: ['每轮最多验证运行次数', '1'], call_timeout_minutes: ['单次调用超时（分钟）', '1'] };
  const ERROR_CLASS = { not_installed: '未安装', not_logged_in: '未登录', quota: '额度不足', budget: '超出预算', network: '网络问题',
    timeout: '超时', cancelled: '已取消', error: '出错' };
  const ROLE_ORDER = ['author', 'reviewer', 'engineer', 'writer', 'drift', 'referee'];
  const keyOf = c => (c ? c.provider + ':' + c.model : '');
  const pairLabel = key => {
    const i = key.indexOf(':');
    return key.slice(i + 1) + '（' + (VIA[key.slice(0, i)] || key.slice(0, i)) + '）';
  };

  M.openSettings = async function () {
    const dlg = document.getElementById('settings');
    dlg.innerHTML = html`<div class="dlg-body"><header class="dlg-head"><h2 id="settings-title">设置</h2><button type="button" class="x" data-action="close-dialog" data-key="settings" aria-label="关闭">×</button></header>
      <div class="dlg-scroll"><p class="muted">正在读取设置…</p></div></div>`.s;
    if (!dlg.open) dlg.showModal();
    try {
      S.settingsView = await NB.get('/api/v2/settings');
      M.renderSettings('');
      NB.V.quotaChips();
    } catch (e) {
      dlg.querySelector('.dlg-scroll').innerHTML = html`<div class="notice notice-bad" role="alert">${e.message}</div>`.s;
    }
  };

  M.priceTable = function (prices) {
    const entries = Object.entries(prices || {});
    if (!entries.length) return html`<p class="muted small">没有价格信息。</p>`;
    const pick = (v, keys) => { for (const k of keys) if (v && v[k] != null) return v[k]; return ''; };
    return html`<div class="table-wrap"><table class="grid"><thead><tr><th>模型</th><th>输入（美元 / 百万 token）</th><th>输出（美元 / 百万 token）</th></tr></thead><tbody>
      ${entries.map(([model, v]) => {
        const obj = v && typeof v === 'object';
        const inp = Array.isArray(v) ? v[0] : obj ? pick(v, ['input', 'in', 'prompt']) : v;
        const out = Array.isArray(v) ? v[1] : obj ? pick(v, ['output', 'out', 'completion']) : '';
        return html`<tr><td class="mono">${model}</td><td>${inp}</td><td>${out}</td></tr>`;
      })}</tbody></table></div><p class="hint">官方标价，仅供参考；中转站实际收费可能不同。</p>`;
  };

  M.providerRows = function (view) {
    const prov = view.providers || {};
    const quota = view.quota || {};
    const yes = v => (v === true ? '是' : v === false ? '否' : '未检查');
    return html`<div class="prov-list">${PROVIDERS.map(([p, label]) => {
      const s = prov[p] || {};
      const models = KNOWN.filter(k => k.startsWith(p + ':')).map(k => k.slice(p.length + 1));
      return html`<div class="prov card-soft">
        <div class="prov-head"><b>${label}</b><span class="tag ${s.installed ? 'tone-good' : 'tone-bad'}">${s.installed ? '已安装' : '未找到'}</span>
          ${s.version ? html`<span class="muted small">版本 ${s.version}</span>` : ''}<span class="muted small">登录：${s.login_text || yes(s.logged_in)}</span></div>
        <div class="inline-form"><label><span class="sr">测试用的模型</span><select data-test-model="${p}">${models.map(m => html`<option value="${m}" ${m === TEST_DEFAULT[p] ? raw('selected') : ''}>${m}</option>`)}</select></label>
          <label><span class="sr">测试用的推理强度</span><select data-test-effort="${p}">${EFFORT_OPTS.map(([val, text]) => html`<option value="${val}">${text}</option>`)}</select></label>
          <button type="button" class="btn-small" data-action="test-provider" data-key="${p}">测试连接</button></div>
        <div class="test-result small" data-test-result="${p}" aria-live="polite"></div>
      </div>`;
    })}</div>
    ${Object.keys(quota).length ? html`<h4>额度</h4><ul class="plain quota-list">${Object.entries(quota).filter(([k]) => BUCKETS[k]).map(([k, q]) => html`<li><span class="chip chip-${(q && q.level) || 'muted'}"><span class="dot" aria-hidden="true"></span><b>${BUCKETS[k] || k}</b></span> ${(q && q.text) || ''}</li>`)}</ul>` : ''}`;
  };

  // Calls and tokens per quota bucket (GET /api/v2/usage → summary).
  M.usageTable = function () {
    const sum = (S.usage && S.usage.summary) || {};
    const keys = Object.keys(sum).filter(k => BUCKETS[k] && sum[k] && typeof sum[k] === 'object');
    if (!keys.length) return '';
    const n = v => (v == null || v === '' ? '—' : Number(v).toLocaleString('zh-CN'));
    return html`<h4>用量</h4><div class="table-wrap"><table class="grid"><thead><tr><th>额度</th><th>今天</th><th>本周</th><th>本周 token</th><th>本月费用</th><th>最近错误</th></tr></thead><tbody>
      ${keys.map(k => {
        const u = sum[k];
        return html`<tr><td>${BUCKETS[k] || k}</td><td>${n(u.calls_today)} 次</td><td>${n(u.calls_week)} 次</td><td>${n(u.tokens_week)}</td>
          <td>${u.cost_month_usd != null ? '$' + Number(u.cost_month_usd).toFixed(2) : '—'}</td><td>${u.last_error_class ? ERROR_CLASS[u.last_error_class] || u.last_error_class : '无'}</td></tr>`;
      })}</tbody></table></div>`;
  };

  M.renderSettings = function (note, tone) {
    const view = S.settingsView || {};
    const st = view.settings || {};
    const roles = st.roles || {};
    const labels = view.role_labels || NB.L.agentRole;
    const order = ROLE_ORDER.filter(r => r in roles).concat(Object.keys(roles).filter(r => !ROLE_ORDER.includes(r)));
    const limits = st.limits || {};
    const git = st.git || {};
    const remoteUrl = S.tree && S.tree.git && S.tree.git.remote_url;
    document.getElementById('settings').innerHTML = html`<form class="dlg-body settings" data-form="settings" novalidate>
      <header class="dlg-head"><h2 id="settings-title">设置</h2><button type="button" class="x" data-action="close-dialog" data-key="settings" aria-label="关闭">×</button></header>
      <div class="dlg-scroll">
        <div class="notice ${tone === 'bad' ? 'notice-bad' : 'notice-good'}" role="status" data-settings-note ${note ? '' : raw('hidden')}>${note}</div>
        <section><h3>角色与模型</h3>
          <div class="table-wrap"><table class="grid role-table"><thead><tr><th>角色</th><th>首选模型</th><th>推理强度</th><th>备选（额度不够时自动换用）</th></tr></thead><tbody>
          ${order.map(role => {
            const list = roles[role] || [];
            const cand0 = list[0] || null;
            const cur0 = keyOf(cand0);
            const isAgy0 = cur0.startsWith('agy:');
            const curEffort0 = (cand0 && cand0.effort) || (cur0.startsWith('codex:') ? 'high' : '');
            const baseOpts = KNOWN.filter(k => role !== 'engineer' || k.startsWith('codex:'));
            const opts0 = baseOpts.slice();
            if (cur0 && !opts0.includes(cur0)) opts0.unshift(cur0);
            const fallbacks = list.slice(1).map(c => pairLabel(keyOf(c)) + (c.effort ? ' [' + c.effort + ']' : '')).join(' → ') || '无';
            return html`<tr><th scope="row" class="nowrap">${labels[role] || role}</th>
              <td><select name="role:${role}:0" data-role-select="${role}:0" aria-label="${labels[role] || role}的首选模型">
                ${opts0.map(k => html`<option value="${k}" ${k === cur0 ? raw('selected') : ''}>${pairLabel(k)}</option>`)}
              </select></td>
              <td><select name="effort:${role}:0" aria-label="${labels[role] || role}的首选推理强度">
                ${EFFORT_OPTS.map(([val, text]) => html`<option value="${val}" ${val === curEffort0 ? raw('selected') : ''}>${val === '' && isAgy0 ? '随模型名（推荐）' : text}</option>`)}
              </select></td>
              <td class="small muted">${role === 'engineer' ? '仅用 Codex' : fallbacks}</td></tr>`;
          })}</tbody></table></div>
          <details class="fold fallback-fold">
            <summary class="fold-btn small">高级：调整各角色的备选模型（当首选额度用完时依次替补）</summary>
            <div class="table-wrap"><table class="grid role-table"><thead><tr><th>角色</th><th>备选 1（模型 + 强度）</th><th>备选 2（模型 + 强度）</th></tr></thead><tbody>
            ${order.filter(r => r !== 'engineer').map(role => {
              const list = roles[role] || [];
              const renderFallback = idx => {
                const cand = list[idx] || null;
                const cur = keyOf(cand);
                const curEffort = (cand && cand.effort) || '';
                const opts = KNOWN.slice();
                if (cur && !opts.includes(cur)) opts.unshift(cur);
                return html`<div class="cand-pick">
                  <select name="role:${role}:${idx}" aria-label="${labels[role] || role}的备选 ${idx} 模型">
                    <option value="" ${!cur ? raw('selected') : ''}>（不设）</option>
                    ${opts.map(k => html`<option value="${k}" ${k === cur ? raw('selected') : ''}>${pairLabel(k)}</option>`)}
                  </select>
                  <select name="effort:${role}:${idx}" aria-label="${labels[role] || role}的备选 ${idx} 推理强度">
                    ${EFFORT_OPTS.map(([val, text]) => html`<option value="${val}" ${val === curEffort ? raw('selected') : ''}>${text}</option>`)}
                  </select>
                </div>`;
              };
              return html`<tr><th scope="row" class="nowrap">${labels[role] || role}</th><td>${renderFallback(1)}</td><td>${renderFallback(2)}</td></tr>`;
            })}</tbody></table></div>
          </details>
          <p class="hint">注：Gemini 模型名称自带高低档位（如 -high / -medium / -low），强度选“默认/随模型名”即可；Codex 与 Claude 可自由指定 low / medium / high / xhigh / max。</p>
        </section>
        <section><h3>额度与限制</h3><div class="limit-grid">
          ${Object.keys(limits).map(k => html`<label class="field"><span>${(LIMITS[k] || [k])[0]}</span><input type="number" name="limit:${k}" value="${limits[k]}" step="${(LIMITS[k] || ['', 'any'])[1]}" min="0"></label>`)}
        </div></section>
        <section><h3>版本管理</h3>
          <label class="check"><input type="checkbox" name="git:auto_commit" ${git.auto_commit !== false ? raw('checked') : ''}> 每轮结束和每次保存后自动提交</label>
          <label class="check"><input type="checkbox" name="git:auto_push" ${git.auto_push !== false ? raw('checked') : ''}> 提交后自动推送</label>
          <p class="hint">远程 ${git.remote || 'origin'} · 分支 ${git.branch || 'main'}${remoteUrl ? ' · ' + remoteUrl : ''}</p>
          <div class="git-grid">
            <label class="field"><span>远程仓库地址</span><input name="git:url" value="${remoteUrl || ''}" placeholder="https://github.com/用户名/仓库.git"></label>
            <label class="field"><span>分支</span><input name="git:branch" value="${git.branch || 'main'}"></label>
          </div>
          <div class="form-actions"><button type="button" class="btn-small" data-action="git-remote">更新远程与分支</button>
            <button type="button" class="btn-small" data-action="git-push">立即推送</button></div>
          <div data-git-line>${NB.V.gitStatusLine()}</div>
          ${NB.V.gitDetails()}
        </section>
        <section><h3>价格</h3>${M.priceTable(view.prices)}</section>
        <section><h3>各家状态与连接测试</h3>${M.providerRows(view)}${M.usageTable()}</section>
      </div>
      <footer class="dlg-foot"><button type="submit" class="primary">保存设置</button><button type="button" data-action="close-dialog" data-key="settings">关闭</button></footer>
    </form>`.s;
  };

  M.saveSettings = async function (form) {
    const view = S.settingsView || {};
    const st = JSON.parse(JSON.stringify(view.settings || {}));
    st.roles = st.roles || {};
    for (const role of Object.keys(st.roles)) {
      const list = st.roles[role] || [];
      const nextList = [];
      const seen = new Set();
      for (let idx = 0; idx < 3; idx++) {
        const el = form.elements['role:' + role + ':' + idx] || (idx === 0 ? form.elements['role:' + role] : null);
        const effEl = form.elements['effort:' + role + ':' + idx];
        if (!el || !el.value) continue;
        const val = el.value;
        if (seen.has(val)) continue;
        seen.add(val);
        const cut = val.indexOf(':');
        const cand = { provider: val.slice(0, cut), model: val.slice(cut + 1) };
        const eff = effEl ? effEl.value : (cand.provider === 'codex' ? ((list[idx] && list[idx].effort) || 'high') : '');
        if (eff) cand.effort = eff;
        nextList.push(cand);
      }
      if (nextList.length) st.roles[role] = nextList;
    }
    st.limits = st.limits || {};
    for (const k of Object.keys(st.limits)) {
      const el = form.elements['limit:' + k];
      if (el && el.value.trim() !== '') {
        const n = Number(el.value);
        st.limits[k] = Number.isFinite(n) ? n : el.value;
      }
    }
    st.git = Object.assign({}, st.git, { auto_commit: form.elements['git:auto_commit'].checked, auto_push: form.elements['git:auto_push'].checked });
    const note = form.querySelector('[data-settings-note]');
    try {
      const r = await NB.post('v2_settings_save', { settings: st, revision: view.revision || '' });
      if (!r) return;
      S.settingsView = r;
      M.renderSettings('设置已保存。', 'good');
      NB.V.quotaChips();
      NB.P.render();
    } catch (e) {
      note.className = 'notice notice-bad';
      note.textContent = e.message + (e.status === 409 ? ' 关闭设置后重新打开，就能读到最新内容。' : '');
      note.hidden = false;
      note.scrollIntoView({ block: 'nearest' });
    }
  };

  M.testProvider = async function (provider) {
    const dlg = document.getElementById('settings');
    const sel = dlg.querySelector('[data-test-model="' + provider + '"]');
    const effSel = dlg.querySelector('[data-test-effort="' + provider + '"]');
    const out = dlg.querySelector('[data-test-result="' + provider + '"]');
    const btn = dlg.querySelector('[data-action="test-provider"][data-key="' + provider + '"]');
    const model = sel ? sel.value : TEST_DEFAULT[provider];
    const effort = effSel ? effSel.value : '';
    if (!(await M.confirm('测试连接', '会消耗少量额度，继续？', '继续测试'))) return;
    btn.disabled = true;
    out.innerHTML = html`<span class="muted">正在测试 ${model}${effort ? ' [' + effort + ']' : ''}，最长约 5 分钟…</span>`.s;
    try {
      const r = await NB.post('v2_test_provider', { provider, model, effort });
      if (!r) { out.textContent = ''; return; }
      const u = r.usage || {};
      const extra = ['用时 ' + r.duration_s + ' 秒'];
      if (r.model_reported) extra.push('实际模型 ' + r.model_reported);
      if (u.input_tokens != null) extra.push('输入 ' + u.input_tokens + ' / 输出 ' + (u.output_tokens || 0) + ' token');
      if (r.cost_usd != null) extra.push('约 $' + Number(r.cost_usd).toFixed(4));
      const week = r.rate_limits && r.rate_limits.weekly;
      out.innerHTML = html`${r.ok ? html`<span class="tag tone-good">连接正常</span> 回复：${r.text || '（空）'}`
        : html`<span class="tag tone-bad">没有连通</span> ${r.error || ''}${r.error_class ? '（' + (ERROR_CLASS[r.error_class] || r.error_class) + '）' : ''}`}
        <div class="muted">${extra.join(' · ')}</div>
        ${week ? html`<div class="muted">Codex 周额度已用 ${week.used_percent}%${week.resets_at ? '，' + NB.fmtTime(new Date(week.resets_at * 1000).toISOString()) + ' 重置' : ''}</div>` : ''}`.s;
      NB.A.refreshUsage();
    } catch (e) {
      out.innerHTML = html`<span class="tag tone-bad">出错</span> ${e.message}`.s;
    } finally {
      btn.disabled = false;
    }
  };

  // ---- history of a document ----
  M.hist = null;

  M.openHistory = async function (path, revision) {
    const dlg = document.getElementById('history');
    const h = M.hist = { path, revision: revision || '', commits: [], sel: '', text: '', loading: true, error: '', message: '', viewLoading: false, viewError: '' };
    M.renderHistory();
    if (!dlg.open) dlg.showModal();
    try {
      const r = await NB.get('/api/v2/history?path=' + encodeURIComponent(path));
      h.commits = (r && r.commits) || [];
      h.message = (r && r.message) || '';
    } catch (e) {
      h.error = e.message;
    }
    h.loading = false;
    if (M.hist === h) M.renderHistory();
  };

  M.renderHistory = function () {
    const h = M.hist;
    if (!h) return;
    const isMd = /\.(md|markdown)$/i.test(h.path);
    const cur = h.commits.find(c => c.sha === h.sel);
    let view;
    if (!h.sel) view = html`<p class="muted">在左侧选一个版本查看。</p>`;
    else if (h.viewLoading) view = html`<p class="muted">正在读取这个版本…</p>`;
    else {
      const showDiff = h.diffBase && !h.diffLoading;
      view = html`<div class="hist-tools"><span class="small muted">${cur ? cur.short + ' · ' + NB.fmtTime(cur.date) + ' · ' + cur.message : ''}</span>
          <span class="seg"><button type="button" class="btn-small ${!h.diffBase ? 'on' : ''}" data-action="history-diff" data-key="">内容</button>
            <button type="button" class="btn-small ${h.diffBase === 'parent' ? 'on' : ''}" data-action="history-diff" data-key="parent">与上一版对比</button>
            <button type="button" class="btn-small ${h.diffBase === 'current' ? 'on' : ''}" data-action="history-diff" data-key="current">与当前版本对比</button></span>
          <button type="button" class="primary" data-action="history-restore">恢复此版本</button></div>
        ${h.viewError ? html`<div class="notice notice-bad" role="alert">${h.viewError}</div>` : ''}
        ${h.diffLoading ? html`<p class="muted">正在比较…</p>` : showDiff ? diffView(h.diff, h.diffTruncated)
          : isMd ? html`<article class="paper">${NB.md(h.text, NB.dirname(h.path))}</article>` : raw(codeView(h.text, ''))}`;
    }
    const list = h.loading ? html`<p class="muted">正在读取…</p>`
      : h.error ? html`<div class="notice notice-bad" role="alert">${h.error}</div>`
        : h.commits.length ? html`<ul class="plain commit-list">${h.commits.map(c => html`<li><button type="button" class="commit ${c.sha === h.sel ? 'on' : ''}" aria-pressed="${c.sha === h.sel}" data-action="history-pick" data-key="${c.sha}">
            <span class="cm">${c.message}</span><span class="small muted"><span class="mono">${c.short}</span> · ${NB.fmtTime(c.date)}</span></button></li>`)}</ul>`
          : html`<p class="muted">${h.message || '这份文件还没有历史版本。'}</p>`;
    const dlg = document.getElementById('history');
    dlg.innerHTML = html`<div class="dlg-body history">
      <header class="dlg-head"><div class="vt"><h2 id="history-title">历史版本</h2><span class="muted small path">${h.path}</span></div>
        <div class="actions"><button type="button" class="x" data-action="close-dialog" data-key="history" aria-label="关闭">×</button></div></header>
      <div class="hist-cols"><div class="commit-col">${list}</div><div class="hist-view">${view}</div></div></div>`.s;
    NB.typeset(dlg.querySelector('.hist-view'));
  };

  M.historyPick = async function (sha) {
    const h = M.hist;
    if (!h) return;
    Object.assign(h, { sel: sha, viewLoading: true, viewError: '', text: '', diffBase: '', diff: '' });
    M.renderHistory();
    try {
      const r = await NB.get('/api/v2/version?path=' + encodeURIComponent(h.path) + '&rev=' + encodeURIComponent(sha));
      if (M.hist !== h || h.sel !== sha) return;
      h.text = (r && r.text) || '';
    } catch (e) {
      h.viewError = e.message;
    }
    h.viewLoading = false;
    M.renderHistory();
  };

  M.historyRestore = async function () {
    const h = M.hist;
    const cur = h && h.commits.find(c => c.sha === h.sel);
    if (!cur) return;
    const ok = await M.confirm('恢复此版本', '用 ' + cur.short + '（' + NB.fmtTime(cur.date) + '）的内容覆盖当前文件？现在的内容仍留在历史里，之后可以再恢复。', '恢复');
    if (!ok) return;
    try {
      const r = await NB.post('v2_restore', { path: h.path, rev: h.sel, revision: h.revision });
      if (!r) return;
      M.close('history');
      NB.toast('已恢复到 ' + cur.short);
      await NB.A.reloadAll();
    } catch (e) {
      if (e.status === 409) {
        // Read the newest revision so the next click can succeed.
        await NB.A.loadItem(false);
        const it = S.item;
        const fresh = it && ((it.anchor && it.anchor.path === h.path && it.anchor.revision) || (it.doc && it.doc.path === h.path && it.doc.revision));
        if (fresh) h.revision = fresh;
        h.viewError = '文件刚被修改过，已读到最新版本，请再点一次恢复。';
      } else h.viewError = e.message;
      M.renderHistory();
    }
  };

  M.historyDiff = async function (base) {
    const h = M.hist;
    if (!h || !h.sel) return;
    Object.assign(h, { diffBase: base, diffLoading: true, diff: '', viewError: '' });
    M.renderHistory();
    try {
      const r = await NB.get('/api/v2/diff?path=' + encodeURIComponent(h.path) + '&rev=' + encodeURIComponent(h.sel) + '&base=' + base);
      if (M.hist !== h) return;
      h.diff = (r && r.diff) || '（没有差异）';
      h.diffTruncated = !!(r && r.truncated);
    } catch (e) {
      h.viewError = e.message;
    }
    h.diffLoading = false;
    M.renderHistory();
  };

  function diffView(text, truncated) {
    const rows = String(text || '').split('\n').map(line => {
      const cls = line.startsWith('+') && !line.startsWith('+++') ? 'add' : line.startsWith('-') && !line.startsWith('---') ? 'del' : line.startsWith('@@') ? 'hunk' : '';
      return html`<div class="dl ${cls}">${line || ' '}</div>`;
    });
    return html`<div class="diff mono small">${rows}</div>${truncated ? html`<p class="muted small">差异太长，只显示了前面一部分。</p>` : ''}`;
  }

  // ---- adopt (questions only) ----
  function splitScope(text) {
    const all = String(text || '').trim();
    const cut = all.indexOf('适用范围与局限：');
    return cut >= 0 ? { text: all.slice(0, cut).trim(), scope: all.slice(cut + 8).trim() } : { text: all, scope: '' };
  }

  function section5(text) {
    const lines = String(text || '').split('\n');
    let start = -1;
    let level = 0;
    for (let i = 0; i < lines.length && start < 0; i++) {
      const m = lines[i].match(/^(#{2,4})\s*5(?:[\s.、．]|$)/);
      if (m) { start = i + 1; level = m[1].length; }
    }
    if (start < 0) return { text: '', scope: '' };
    const body = [];
    for (let i = start; i < lines.length; i++) {
      const m = lines[i].match(/^(#{1,6})\s/);
      if (m && m[1].length <= level) break;
      body.push(lines[i]);
    }
    return splitScope(body.join('\n'));
  }

  M.openAdopt = function (it) {
    const staged = it.staged_conclusions || [];
    const last = staged.length ? splitScope(staged[staged.length - 1].body) : null;
    const sec = section5(it.doc && it.doc.text);
    const a = it.anchor || {};
    const scopeSlot = (((a.slots || []).find(s => s.key === 'scope') || {}).text || '').trim();
    const confirmed = (a.assumptions || []).filter(x => x.status === '已确认').map(x => x.id);
    // The user's own section 5 comes first; an AI suggestion is only shown beside it.
    const text = sec.text || '';
    let scope = sec.scope;
    if (!scope) scope = scopeSlot + (confirmed.length ? (scopeSlot ? '\n' : '') + '依赖假设：' + confirmed.join('、') : '');
    const intro = html`${(it.upstream || []).length ? html`<div class="notice notice-warn"><b>上游结论有变化：</b><ul>${it.upstream.map(u => html`<li>${u.message}</li>`)}</ul></div>` : ''}
      <p class="hint">采纳会替换正文第 5 节的现有内容，并记下你的判断。</p>
      ${last ? html`<div class="notice"><b>AI 建议（未写入）：</b><div class="small">${NB.md(last.text, '')}</div>
        <button type="button" class="btn-small" data-action="adopt-use-ai">把建议放进结论框</button></div>` : ''}`;
    M.adoptSuggestion = last;
    return M.ask({
      title: '采纳结论',
      intro,
      fields: [
        { name: 'decision', label: '你的判断', type: 'select', value: 'supported', options: [['supported', '支持'], ['refuted', '不支持'], ['inconclusive', '证据不足']] },
        { name: 'text', label: '结论', type: 'textarea', rows: 5, required: true, value: text },
        { name: 'scope', label: '适用范围与局限', type: 'textarea', rows: 3, value: scope },
      ],
      ok: '采纳',
      submit: async v => {
        const r = await NB.post('v2_adopt', { ref: it.ref, fw: it.fw || S.fw, decision: v.decision, text: v.text.trim(), scope: v.scope.trim() });
        if (r) {
          NB.A.applyItem(r);
          NB.toast('已采纳');
          NB.A.refreshTree();
        }
        return r;
      },
    });
  };
}());
