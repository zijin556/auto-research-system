/* 新版页面：页首、左侧目录、中间正文区。只生成 HTML，事件在 app.js 里统一处理。 */
(function () {
  'use strict';
  const NB = window.NB;
  const { S, html, raw, L } = NB;
  const V = NB.V = {};
  S.picks = S.picks || {};

  // Replace a container's content but keep open editors (text, caret, focus) and the focused control.
  NB.patch = function (el, markup) {
    const kept = {};
    el.querySelectorAll('[data-keep]').forEach(n => { kept[n.dataset.keep] = n; });
    const active = document.activeElement;
    const inside = !!active && active !== document.body && el.contains(active);
    const sel = inside && typeof active.selectionStart === 'number' ? [active.selectionStart, active.selectionEnd] : null;
    const sig = inside ? focusSelector(active) : '';
    el.innerHTML = String(markup);
    el.querySelectorAll('[data-keep]').forEach(n => { const old = kept[n.dataset.keep]; if (old) n.replaceWith(old); });
    if (inside && el.contains(active) && document.activeElement !== active) {
      active.focus({ preventScroll: true });
      if (sel) { try { active.setSelectionRange(sel[0], sel[1]); } catch (e) { /* not a text field */ } }
    } else if (sig) {
      const again = el.querySelector(sig);
      if (again) again.focus({ preventScroll: true });
    }
    NB.typeset(el);
  };
  // A selector that finds "the same" control after a re-render: buttons by data-action, fields by form and name.
  function focusSelector(node) {
    if (node.dataset && node.dataset.action) {
      let sel = '[data-action="' + CSS.escape(node.dataset.action) + '"]';
      for (const key of ['id', 'ref', 'key', 'index', 'decision', 'kind', 'path']) {
        if (node.dataset[key] != null) sel += '[data-' + key + '="' + CSS.escape(node.dataset[key]) + '"]';
      }
      return sel;
    }
    const form = node.closest && node.closest('form[data-form]');
    if (node.name && form) {
      let sel = 'form[data-form="' + CSS.escape(form.dataset.form) + '"] [name="' + CSS.escape(node.name) + '"]';
      if (node.type === 'radio' || node.type === 'checkbox') sel += '[value="' + CSS.escape(node.value) + '"]';
      return sel;
    }
    if (node.dataset && node.dataset.pick) return '[data-pick="' + CSS.escape(node.dataset.pick) + '"][value="' + CSS.escape(node.value) + '"]';
    return '';
  }

  NB.picks = (group, init) => S.picks[group] || (S.picks[group] = new Set(init || []));
  NB.sessionFor = ref => {
    if (S.session && S.session.ref === ref) return S.session;
    if (S.item && S.item.ref === ref && S.item.session) return S.item.session;
    return null;
  };
  V.fileLink = (path, label, cls) => (path
    ? html`<a class="file-link ${cls || ''}" href="${NB.fileUrl(path)}" data-file="${path}">${label}</a>`
    : html`<span class="${cls || ''}">${label}</span>`);
  NB.fileUrl = path => window.NBMarkdown.fileUrl(path);
  const toneOf = state => (state === '确定' ? 'tone-good' : state === '否定' ? 'tone-muted' : 'tone-warn');
  // Role label of a chapter, left out when the title already says it (第1章 绪论 needs no 绪论 tag).
  V.roleTag = c => {
    const label = c.role_label || L.chapterRole[c.role] || '';
    return label && !String(c.title || '').includes(label) ? html`<span class="role">${label}</span>` : '';
  };
  // A document whose first line repeats the page title ("# 第2章 理论基础") does not show it twice.
  V.dropTitle = (text, title) => {
    const m = String(text || '').match(/^\s*#\s+([^\n]*)\n?/);
    return m && title && m[1].includes(title) ? text.slice(m[0].length) : text;
  };
  const count = n => (n > 0 ? html`<span class="count" title="待你决定 ${n} 项">${n}<span class="sr"> 项待你决定</span></span>` : '');

  // ---- header ----
  V.header = function () {
    const t = S.tree;
    const project = (t && t.project) || {};
    const titleEl = document.getElementById('project-title');
    titleEl.textContent = project.title || '科研文件管理系统';
    titleEl.title = titleEl.textContent;
    document.title = (S.item ? S.item.title + ' · ' : '') + (project.title || '科研文件管理系统');
    document.getElementById('example-badge').hidden = !project.is_example;
    document.getElementById('mock-badge').hidden = !S.mock;
    const mockBtn = document.getElementById('mock-toggle');
    if (mockBtn) {
      mockBtn.textContent = S.mock ? '返回正式库' : '演示数据';
      mockBtn.href = (S.mock ? '/' : '/?mock=1') + (location.hash || '#/FW');
      mockBtn.title = S.mock ? '退出演示数据，返回你自己的研究目录' : '查看内置的完整博士论文演示数据';
      mockBtn.classList.toggle('primary', !!S.mock);
    }
    const picker = document.getElementById('fw-picker');
    const fws = (t && t.frameworks) || [];
    picker.innerHTML = fws.length
      ? fws.map(f => html`<option value="${f.id}" ${f.id === S.fw ? raw('selected') : ''}>${f.title}${f.status === 'active' ? '（主推）' : ''}</option>`).join('')
      : '<option value="">还没有框架</option>';
    picker.disabled = !fws.length;
    V.quotaChips();
    V.gitChip();
  };

  const QUOTA = [{ name: 'Codex', keys: ['codex'] }, { name: 'Gemini', keys: ['agy:gemini'] }, { name: 'Claude', keys: ['claude'] }];
  const RANK = { ok: 0, warn: 1, blocked: 2 };
  V.quotaChips = function () {
    const box = document.getElementById('quota-chips');
    const q = (S.usage && S.usage.quota) || (S.settingsView && S.settingsView.quota) || null;
    if (!q) { box.innerHTML = ''; return; }
    box.innerHTML = QUOTA.map(spec => {
      const parts = spec.keys.filter(k => q[k]).map(k => Object.assign({ key: k }, q[k]));
      if (!parts.length) return html`<span class="chip chip-muted" title="${spec.name}：暂无额度信息"><span class="dot" aria-hidden="true"></span><b>${spec.name}</b></span>`;
      // A family is usable when any of its buckets is usable (Claude: Antigravity first, then the relay).
      const rank = p => (RANK[p.level || 'ok'] == null ? 1 : RANK[p.level || 'ok']);
      const best = parts.slice().sort((a, b) => rank(a) - rank(b))[0];
      const via = p => (parts.length > 1 ? (p.key === 'claude' ? '中转站 ' : 'Antigravity ') : '');
      const full = parts.map(p => via(p) + (p.text || '')).join('；');
      // Short form for the chip: no parentheses; when all is well only the first clause ("本周 12 次").
      let brief = String(best.text || '').replace(/（[^）]*）|\([^)]*\)/g, '').trim();
      if ((best.level || 'ok') === 'ok') brief = brief.split(/[，,]/)[0];
      brief = (via(best) + brief).trim();
      return html`<span class="chip chip-${best.level || 'ok'}" title="${spec.name}：${full}"><span class="dot" aria-hidden="true"></span><b>${spec.name}</b><span class="chip-text">${brief}</span></span>`;
    }).join('');
  };

  V.gitChip = function () {
    const box = document.getElementById('git-chip');
    const g = S.tree && S.tree.git;
    if (!g) { box.innerHTML = ''; return; }
    let tone = 'ok';
    let text = '已同步';
    const notes = [];
    if (!g.is_repo) { tone = 'muted'; text = '未启用版本管理'; notes.push(g.message || ''); }
    else if (g.last_error) { tone = 'blocked'; text = '提交失败'; notes.push(g.last_error, g.last_error_hint || ''); }
    else if (g.last_push && g.last_push.ok === false) { tone = 'blocked'; text = '推送失败'; notes.push(g.last_push.message || ''); }
    else if (g.ahead > 0) { tone = 'warn'; text = '未推送 ' + g.ahead; }
    else if (g.dirty) { tone = 'warn'; text = '有未提交'; }
    const outside = (g.skipped_large || []).length + (g.untracked_large || []).length + ((g.ignored_data && g.ignored_data.count) || 0);
    if (outside) notes.push('未进 git 的文件 ' + outside + ' 个（大文件和原始数据，请在项目卡登记位置）');
    if ((g.embedded_repos || []).length) { notes.push('子仓库未备份：' + g.embedded_repos.join('、')); if (tone === 'ok') { tone = 'warn'; text = '子仓库未备份'; } }
    if (g.last_commit && g.last_commit.message) notes.push('最近提交：' + g.last_commit.message);
    if (g.last_push && g.last_push.at) notes.push('最近推送：' + NB.fmtTime(g.last_push.at));
    box.innerHTML = html`<span class="chip chip-${tone}" title="${notes.filter(Boolean).join('\n')}"><span class="dot" aria-hidden="true"></span>${text}</span>`.s;
  };

  // Short git status for the settings dialog, with a manual push when there is something to push.
  V.gitStatusLine = function () {
    const g = S.tree && S.tree.git;
    if (!g || !g.is_repo) return html`<p class="hint">${(g && g.message) || '这个目录没有启用版本管理。'}</p>`;
    const failed = g.last_push && g.last_push.ok === false;
    const parts = [g.dirty ? '有未提交的修改' : '没有未提交的修改', g.ahead > 0 ? g.ahead + ' 个提交还没推送' : '已全部推送'];
    if (g.last_push && g.last_push.at) parts.push('最近推送 ' + NB.fmtTime(g.last_push.at) + (failed ? '（失败：' + (g.last_push.message || '原因未知') + '）' : ''));
    return html`<p class="hint">${parts.join(' · ')}</p>
      ${g.last_error ? html`<div class="notice notice-bad">提交失败：${g.last_error}<br><span class="small">${g.last_error_hint || ''}</span></div>` : ''}`;
  };

  // What is not in git, for the settings dialog.
  V.gitDetails = function () {
    const g = S.tree && S.tree.git;
    if (!g || !g.is_repo) return '';
    const big = (g.skipped_large || []).map(x => (x.path || x) + (x.size ? '（' + (x.size / 1048576).toFixed(1) + ' MB）' : ''));
    const rows = [];
    if (big.length || (g.untracked_large || []).length) rows.push(html`<li>超过 50 MB、没有进 git：${big.concat(g.untracked_large || []).join('、')}</li>`);
    if (g.ignored_data && g.ignored_data.count) rows.push(html`<li>原始数据文件（.mat/.tif/.zip 等）不进 git：${g.ignored_data.count} 个${g.ignored_data.examples.length ? '，例如 ' + g.ignored_data.examples.slice(0, 5).join('、') : ''}</li>`);
    if ((g.over_budget || []).length) rows.push(html`<li>本次新数据太多，下次再提交：${g.over_budget.slice(0, 10).join('、')}</li>`);
    if ((g.embedded_repos || []).length) rows.push(html`<li>子仓库未备份（文件夹里有自己的 .git）：${g.embedded_repos.join('、')}。请移出，或删掉其中的 .git。</li>`);
    return rows.length ? html`<details class="small"><summary>未进 git 的内容</summary><ul>${rows}</ul><p class="hint">这些文件只在本机；请在项目卡里登记位置，另行备份。</p></details>` : '';
  };

  // ---- left tree ----
  V.tree = function () {
    const nav = document.getElementById('tree');
    const t = S.tree;
    if (!t) { nav.innerHTML = '<p class="muted pad">正在读取目录…</p>'; return; }
    const fw = (t.frameworks || []).find(f => f.id === S.fw) || null;
    const cur = S.route.ref;
    const active = NB.P ? NB.P.activeSession() : (t.session && NB.ACTIVE.has(t.session.status) ? t.session : null);
    const running = active ? active.ref : '';
    const runDot = ref => (ref === running ? html`<span class="run-dot" title="正在讨论"><span class="sr">正在讨论</span></span>` : '');
    const node = (ref, fwId, content, cls, tip) => html`<a class="node ${cls} ${ref === cur ? 'current' : ''}" href="${NB.refHash(ref, fwId)}" title="${tip || ''}" ${ref === cur ? raw('aria-current="page"') : ''}>${content}</a>`;
    const anchorTip = s => '锚点' + (L.anchor[s] || s || '未起草');
    const ledgerTip = c => (c ? '台账：待定 ' + (c['待定'] || 0) + '、确定 ' + (c['确定'] || 0) + '、否定 ' + (c['否定'] || 0) : '');
    const question = (q, fwId) => html`<li>${node(q.id, fwId, html`<span class="qdot tone-${L.qTone[q.status] || 'muted'}" aria-hidden="true"></span><span class="label">${q.title}</span><span class="sr">（${q.status_label || ''}）</span>${runDot(q.id)}${count(q.pending)}`,
      'q', [q.status_label, anchorTip(q.anchor_status), ledgerTip(q.ledger)].filter(Boolean).join(' · '))}</li>`;
    const chapter = (c, fwId) => html`<li class="chapter">
      ${node(c.id, fwId, html`<span class="label">${c.no ? '第' + c.no + '章 ' : ''}${c.title}</span>${V.roleTag(c)}${runDot(c.id)}${count(c.pending)}`, 'ch', anchorTip(c.anchor_status))}
      <ul class="qs">${(c.questions || []).map(q => question(q, fwId))}<li><button type="button" class="add" data-action="new-question" data-ref="${c.id}" data-fw="${fwId}">+ 新建小问题</button></li></ul>
    </li>`;
    nav.innerHTML = html`
      <button type="button" class="tree-close" data-action="close-tree">关闭目录</button>
      ${t.pending_total > 0 ? html`<p class="tree-note">共 ${t.pending_total} 项等你决定，数字标在各项旁边。</p>` : ''}
      <div class="tree-top">${node('FW', '', html`<span class="label">论文框架</span>${runDot('FW')}${count(t.framework_layer ? t.framework_layer.pending : 0)}`, 'top',
        anchorTip(t.framework_layer && t.framework_layer.anchor_status))}</div>
      ${fw ? html`<div class="tree-fw">${node(fw.id, fw.id, html`<span class="label">${fw.title}</span>${fw.status === 'active' ? html`<span class="tag tone-good">主推</span>` : html`<span class="tag">${L.fwStatus[fw.status] || fw.status}</span>`}${runDot(fw.id)}`, 'fw')}</div>
        <ul class="chapters">${(fw.chapters || []).map(c => chapter(c, fw.id))}</ul>` : html`<p class="muted pad">还没有论文框架。</p>`}
      <div class="tree-add"><button type="button" class="add" data-action="new-framework">+ 新建框架</button></div>
      ${(t.unplaced || []).length ? html`<div class="tree-sec">未放入框架</div><ul class="chapters">${t.unplaced.map(c => chapter(c, ''))}</ul>` : ''}`.s;
  };

  // ---- main column ----
  V.main = function () {
    const main = document.getElementById('main');
    const it = S.item;
    if (!it) {
      main.innerHTML = S.itemError ? html`<div class="notice notice-bad" role="alert">${S.itemError}</div>`.s : '<p class="muted">正在读取…</p>';
      return;
    }
    const y = window.scrollY;
    NB.patch(main, V.mainHtml(it));
    window.scrollTo(0, y);
    if (S.mmZoom == null && NB.A && NB.A.fitMindmap) {
      const vp = main.querySelector('[data-mm-viewport]');
      if (vp) NB.A.fitMindmap(vp);
    }
  };

  const PROJECT_FIELDS = [
    ['brief', '研究方向、背景与核心需求（想研究什么、解决什么痛点）', 4],
    ['configuration', '硬边界条件与系统配置（实验设备参数、算力、时间限制、不做什么）', 4],
    ['background', '现有方法缺口与研究背景补充', 3],
    ['data_sources', '已有数据、代码基础与存放位置', 2],
    ['symbols', '核心符号与术语约定（可随讨论逐步补充）', 2],
    ['rules', '对 AI 的统一要求（可选）', 2],
  ];

  V.projectCard = function (it) {
    if (it.level !== 'framework_layer') return '';
    const p = it.project_card || { title: (S.tree && S.tree.project && S.tree.project.title) || '我的论文研究', path: '00_项目卡.md', revision: '', fields: {} };
    const f = p.fields || {};
    const editing = S.edit && S.edit.kind === 'project';
    const filled = PROJECT_FIELDS.filter(([k]) => (f[k] || '').trim());
    if (editing) {
      const d = S.edit.fields || f;
      return html`<section class="card project editing" id="sec-project" aria-labelledby="proj-h">
        <div class="card-head">
          <h2 id="proj-h">编辑项目边界、配置与研究需求</h2>
          <span class="hint">保存后，所有 AI 在拟定框架和讨论各章时都会严格遵守这里的边界条件</span>
        </div>
        ${editError(p.path)}
        <form data-form="project" data-keep="project:FW">
          <label class="field"><span>论文 / 课题暂定名称</span><input name="title" value="${S.edit.title != null ? S.edit.title : (p.title || '')}" placeholder="例如：高速动态结构光三维形貌测量与非理想误差补偿"></label>
          ${PROJECT_FIELDS.map(([k, label, rows]) => html`<label class="field"><span>${label}</span><textarea name="proj:${k}" rows="${rows}">${d[k] || ''}</textarea></label>`)}
          <div class="form-actions">
            <button type="submit" class="primary">保存边界与配置</button>
            <button type="button" data-action="fill-project-template">填入示例模板</button>
            <button type="button" data-action="cancel-edit">取消</button>
          </div>
        </form>
      </section>`;
    }
    return html`<section class="card project" id="sec-project" aria-labelledby="proj-h">
      <div class="card-head">
        <h2 id="proj-h">1. 项目边界条件、配置与研究需求</h2>
        ${filled.length ? html`<span class="tag tone-good">已填 ${filled.length} 项</span>` : html`<span class="tag tone-warn">建议先填写</span>`}
        <div class="actions">
          <button type="button" class="${filled.length ? '' : 'primary'}" data-action="edit-project">编辑边界与配置</button>
          ${!filled.length ? html`<button type="button" data-action="fill-project-template">填入示例模板</button>` : ''}
          ${p.path ? html`<button type="button" class="quiet" data-action="history" data-path="${p.path}" data-revision="${p.revision || ''}">历史版本</button>` : ''}
        </div>
      </div>
      ${filled.length ? html`<dl class="slots">${filled.map(([k, label]) => html`<div class="slot"><dt>${label.split('（')[0]}</dt><dd>${NB.md(f[k], '')}</dd></div>`)}</dl>`
        : html`<p class="muted">还没有填写边界条件与研究方向。建议先点 <b>「填入示例模板」</b> 写入你的实验设备/数据配置、想研究的方向、背景需求与限制条件，再交给 AI 拟定或导入论文框架。</p>`}
    </section>`;
  };

  const MM_COLORS = ['#4c8bf5', '#9b6bff', '#10b981', '#f59e0b', '#ec4899', '#06b6d4', '#6366f1', '#84cc16'];

  V.buildMindmapTree = function (t, fw, cur, hasBoundary, fwAnchor) {
    const fws = t.frameworks || [];
    const chapters = (fw && fw.chapters) || [];
    const collapseQ = !!S.mmCollapseQ;
    const X0 = 36, W0 = 220, H0 = 76;
    const X1 = 326, W1 = 226, H1 = 72;
    const X2 = 624, W2 = 264, H2 = 52;
    const X3 = 958, W3 = 276, H3 = 44;
    const chGap = 12, qGap = 10, fwGap = 16;

    const chNodes = [];
    const qNodes = [];
    const links = [];
    let curY = 28;
    let hasAnyQ = false;

    chapters.forEach((c, idx) => {
      const color = MM_COLORS[idx % MM_COLORS.length];
      const qs = collapseQ ? [] : (c.questions || []);
      if (!qs.length) {
        const cy = curY + H2 / 2;
        chNodes.push({ id: c.id, x: X2, y: curY, w: W2, h: H2, cy, color, data: c, totalQ: (c.questions || []).length });
        curY += H2 + chGap;
      } else {
        hasAnyQ = true;
        const topY = curY;
        const qSpan = qs.length * H3 + (qs.length - 1) * qGap;
        const spanH = Math.max(H2, qSpan);
        const cy = topY + spanH / 2;
        const qStart = topY + (spanH - qSpan) / 2;
        const cNode = { id: c.id, x: X2, y: cy - H2 / 2, w: W2, h: H2, cy, color, data: c, totalQ: (c.questions || []).length };
        chNodes.push(cNode);
        qs.forEach((q, j) => {
          const qy = qStart + j * (H3 + qGap);
          const qcy = qy + H3 / 2;
          qNodes.push({ id: q.id, x: X3, y: qy, w: W3, h: H3, cy: qcy, color, data: q, chId: c.id });
          links.push({ x1: X2 + W2, y1: cy, x2: X3, y2: qcy, color, w: 1.8 });
        });
        curY = topY + spanH + chGap;
      }
    });

    const chMidY = chNodes.length ? (chNodes[0].cy + chNodes[chNodes.length - 1].cy) / 2 : 130;
    const activeIdx = Math.max(0, fws.findIndex(f => fw && f.id === fw.id));
    const fwNodes = fws.map((f, idx) => {
      const isSelected = !!(fw && f.id === fw.id);
      const y = (chMidY - H1 / 2) + (idx - activeIdx) * (H1 + fwGap);
      return { id: f.id, x: X1, y, w: W1, h: H1, cy: y + H1 / 2, isSelected, data: f };
    });

    const minY = Math.min(24, ...fwNodes.map(n => n.y), ...chNodes.map(n => n.y));
    if (minY < 24) {
      const shift = 24 - minY;
      fwNodes.forEach(n => { n.y += shift; n.cy += shift; });
      chNodes.forEach(n => { n.y += shift; n.cy += shift; });
      qNodes.forEach(n => { n.y += shift; n.cy += shift; });
      links.forEach(l => { l.y1 += shift; l.y2 += shift; });
      curY += shift;
    }

    const selFwNode = fwNodes.find(n => n.isSelected) || null;
    if (selFwNode) {
      chNodes.forEach(cn => {
        links.push({ x1: X1 + W1, y1: selFwNode.cy, x2: X2, y2: cn.cy, color: cn.color, w: 2.2 });
      });
    }

    const rootCy = fwNodes.length ? (fwNodes[0].cy + fwNodes[fwNodes.length - 1].cy) / 2 : chMidY;
    const rootNode = {
      id: 'FW', x: X0, y: Math.max(24, rootCy - H0 / 2), w: W0, h: H0, cy: Math.max(24 + H0 / 2, rootCy),
      title: (t.project && t.project.title) || '我的论文研究', hasBoundary, fwAnchor,
    };
    fwNodes.forEach(fn => {
      links.push({ x1: X0 + W0, y1: rootNode.cy, x2: X1, y2: fn.cy, color: fn.isSelected ? '#4c8bf5' : '#6e7681', w: fn.isSelected ? 2.5 : 1.6, dashed: !fn.isSelected });
    });

    const maxBot = Math.max(curY + 16, rootNode.y + H0 + 28, ...fwNodes.map(n => n.y + H1 + 28), 300);
    const totalW = hasAnyQ ? (X3 + W3 + 48) : chNodes.length ? (X2 + W2 + 64) : fwNodes.length ? (X1 + W1 + 64) : (X0 + W0 + 480);
    return { rootNode, fwNodes, chNodes, qNodes, links, totalW, totalH: maxBot };
  };

  // Interactive Mindmap Canvas + 4-stage sequential flow bar.
  V.mindmap = function (it, inModal) {
    const t = S.tree;
    if (!t) return '';
    const fws = t.frameworks || [];
    const fw = fws.find(f => f.id === (it && it.fw ? it.fw : S.fw)) || fws.find(f => f.status === 'active') || fws[0] || null;
    const chapters = (fw && fw.chapters) || [];
    const allQ = chapters.reduce((acc, c) => acc.concat(c.questions || []), []);
    const confirmedQ = allQ.filter(q => q.anchor_status === 'confirmed').length;
    const supportedQ = allQ.filter(q => q.status === 'supported').length;
    const confirmedCh = chapters.filter(c => c.anchor_status === 'confirmed').length;
    const fwAnchor = (t.framework_layer && t.framework_layer.anchor_status) || 'none';
    const hasBoundary = !!(t.project && t.project.has_boundary) || !!(it && it.project_card && Object.values(it.project_card.fields || {}).some(v => String(v || '').trim()));
    const cur = (S.route && S.route.ref) || 'FW';

    const done1 = hasBoundary;
    const done2 = !!fw;
    const done3 = allQ.length > 0;
    const done4 = chapters.length > 0 && confirmedCh === chapters.length && fwAnchor === 'confirmed';
    const autoStage = !done1 ? 1 : !done2 ? 2 : !done3 ? 3 : !done4 ? 4 : 0;
    const activeStage = S.mmFocusStage || autoStage;
    const stClass = (no, done) => `${done ? 'is-done' : ''} ${activeStage === no ? 'is-current' : ''}`.trim();
    const stTag = (no, done) => activeStage === no ? '进行中' : done ? '✓ 已完成' : '待进行';
    const stageNames = {
      1: '① 导边界与需求（待填写项目边界与研究方向）',
      2: '② 拟定/导入框架（待生成或导入论文章节结构）',
      3: '③ 深入讨论验证（各章发散探索、跑最小实验并拆解小问题）',
      4: '④ 逐级锚定成文（小问题 → 章节 → 论文总框架三级聚焦锚定）',
      0: '✓ 全部四个阶段均已完成锚定',
    };

    const anchorBadge = st => st === 'confirmed'
      ? html`<span class="tag tone-good">已锚定</span>`
      : st === 'draft' ? html`<span class="tag tone-warn">草稿</span>` : html`<span class="tag tone-info">探索中</span>`;

    const layout = V.buildMindmapTree(t, fw, cur, hasBoundary, fwAnchor);
    const zoom = S.mmZoom || 1;
    const panX = S.mmPanX || 0;
    const panY = S.mmPanY || 0;

    const bezier = l => {
      const dx = Math.max(28, (l.x2 - l.x1) * 0.5);
      return `M ${l.x1} ${l.y1} C ${l.x1 + dx} ${l.y1}, ${l.x2 - dx} ${l.y2}, ${l.x2} ${l.y2}`;
    };

    return html`<section class="card mindmap ${inModal ? 'in-modal' : ''}" id="sec-mindmap" aria-labelledby="mm-h">
      ${!inModal ? html`<div class="card-head mm-head">
        <div class="mm-head-left">
          <h2 id="mm-h">论文框架思维导图</h2>
          <span class="hint">滚轮缩放 · 拖拽平移 · 点击节点进入详情</span>
        </div>
        <div class="actions mm-head-actions">
          <button type="button" class="btn-small" data-action="mm-export-mm" title="导出标准 .mm 思维导图文件，可用 XMind / MindMaster / FreeMind 直接打开">导出 XMind (.mm)</button>
          <button type="button" class="btn-small quiet" data-action="mm-export-md" title="导出 Markdown 思维导图大纲，可用 Markmap / Obsidian 打开">导出 Markdown</button>
          <button type="button" class="btn-small quiet" data-action="open-mindmap">全屏画布</button>
        </div>
      </div>` : ''}

      <!-- 当前状态总览栏 -->
      <div class="mm-status-bar" role="status" aria-label="当前科研状态总览">
        <div class="mm-stat-item mm-stat-main">
          <span class="mm-stat-label">当前状态</span>
          <strong class="mm-stat-val tone-accent">${stageNames[autoStage]}</strong>
        </div>
        <div class="mm-stat-sep" aria-hidden="true"></div>
        <div class="mm-stat-item">
          <span class="mm-stat-label">项目边界</span>
          <span class="mm-stat-val ${hasBoundary ? 'text-good' : 'text-warn'}">${hasBoundary ? '已配置' : '待填写'}</span>
        </div>
        <div class="mm-stat-sep" aria-hidden="true"></div>
        <div class="mm-stat-item">
          <span class="mm-stat-label">当前框架</span>
          <span class="mm-stat-val">${fw ? `${fw.title}（${chapters.length}章 · 共${fws.length}套）` : '尚未建立'}</span>
        </div>
        <div class="mm-stat-sep" aria-hidden="true"></div>
        <div class="mm-stat-item">
          <span class="mm-stat-label">深入讨论与验证</span>
          <span class="mm-stat-val">已拆 <b>${allQ.length}</b> 题 · 验证支持 <b>${supportedQ}</b> 题</span>
        </div>
        <div class="mm-stat-sep" aria-hidden="true"></div>
        <div class="mm-stat-item">
          <span class="mm-stat-label">三级锚定进度</span>
          <span class="mm-stat-val">小问题 <b>${confirmedQ}/${allQ.length}</b> · 章节 <b>${confirmedCh}/${chapters.length}</b> · 总框架 <b>${fwAnchor === 'confirmed' ? '已锚定' : '待锚定'}</b></span>
        </div>
      </div>

      <!-- 四阶段顺序流程条 -->
      <div class="mm-stepper" role="navigation" aria-label="从发散到聚焦四阶段顺序流程">
        <button type="button" class="mm-step ${stClass(1, done1)}" data-action="mm-stage" data-stage="1" data-target="sec-project">
          <div class="mm-st-top">
            <span class="mm-st-no">① 导边界与需求</span>
            <span class="mm-st-badge">${stTag(1, done1)}</span>
          </div>
          <span class="mm-st-desc">填写研究方向、背景与配置</span>
        </button>
        <span class="mm-step-arrow" aria-hidden="true">→</span>
        <button type="button" class="mm-step ${stClass(2, done2)}" data-action="mm-stage" data-stage="2" data-target="sec-frameworks">
          <div class="mm-st-top">
            <span class="mm-st-no">② 拟定/导入框架</span>
            <span class="mm-st-badge">${stTag(2, done2)}</span>
          </div>
          <span class="mm-st-desc">AI 拟定或导入大纲自动分章</span>
        </button>
        <span class="mm-step-arrow" aria-hidden="true">→</span>
        <button type="button" class="mm-step ${stClass(3, done3)}" data-action="mm-stage" data-stage="3" data-target="sec-chapters-overview">
          <div class="mm-st-top">
            <span class="mm-st-no">③ 深入讨论验证</span>
            <span class="mm-st-badge">${stTag(3, done3)}</span>
          </div>
          <span class="mm-st-desc">各章探索验证并拆解小问题</span>
        </button>
        <span class="mm-step-arrow" aria-hidden="true">→</span>
        <button type="button" class="mm-step ${stClass(4, done4)}" data-action="mm-stage" data-stage="4" data-target="sec-anchor">
          <div class="mm-st-top">
            <span class="mm-st-no">④ 逐级锚定成文</span>
            <span class="mm-st-badge">${stTag(4, done4)}</span>
          </div>
          <span class="mm-st-desc">小问题 → 章节 → 总框架锚定</span>
        </button>
      </div>

      <div class="mm-viewport" data-mm-viewport="1" style="height: ${inModal ? 'calc(85vh - 185px)' : Math.min(540, Math.max(320, layout.totalH + 24)) + 'px'}">
        <div class="mm-toolbar" role="toolbar" aria-label="思维导图画布控制">
          <button type="button" data-action="mm-zoom" data-dir="-1" title="缩小画布">－</button>
          <button type="button" class="mm-zoom-label" data-action="mm-zoom-reset" title="点击重置为 100% 清晰比例">${Math.round(zoom * 100)}%</button>
          <button type="button" data-action="mm-zoom" data-dir="1" title="放大画布">＋</button>
          <button type="button" data-action="mm-fit" title="自动居中适应画布">⊡ 适应画布</button>
          ${allQ.length ? html`<button type="button" data-action="mm-toggle-q" title="展开或折叠第4级小问题节点">${S.mmCollapseQ ? '展开小问题 (' + allQ.length + ')' : '收起小问题'}</button>` : ''}
        </div>

        <div class="mm-stage-inner" style="width:${layout.totalW}px; height:${layout.totalH}px; transform: translate(${Math.round(panX)}px, ${Math.round(panY)}px)${zoom !== 1 ? ` scale(${zoom})` : ''};">
          <svg class="mm-svg" width="${layout.totalW}" height="${layout.totalH}" viewBox="0 0 ${layout.totalW} ${layout.totalH}" aria-hidden="true">
            ${layout.links.map(l => html`<path d="${bezier(l)}" stroke="${l.color}" stroke-width="${l.w}" fill="none" stroke-linecap="round" ${l.dashed ? raw('stroke-dasharray="5,4" opacity="0.65"') : raw('opacity="0.88"')} />
              <circle cx="${l.x1}" cy="${l.y1}" r="3" fill="${l.color}" />
              <circle cx="${l.x2}" cy="${l.y2}" r="3" fill="${l.color}" />`)}
          </svg>

          <!-- Level 0: Root (Project Overview & Boundary) -->
          <a class="mm-item mm-lv0 ${cur === 'FW' ? 'is-current' : ''}"
             href="#/FW" data-mm-nav="1"
             style="left:${layout.rootNode.x}px; top:${layout.rootNode.y}px; width:${layout.rootNode.w}px; height:${layout.rootNode.h}px;">
            <div class="mm-lv-badge">L0 · 论文总览与边界</div>
            <div class="mm-item-title">${layout.rootNode.title}</div>
            <div class="mm-item-meta">
              <span class="tag ${hasBoundary ? 'tone-good' : 'tone-warn'}">${hasBoundary ? '边界已填' : '边界待填'}</span>
              ${anchorBadge(fwAnchor)}
            </div>
          </a>

          <!-- Level 1: Frameworks (Children of Root) -->
          ${layout.fwNodes.map(fn => html`<div class="mm-item mm-lv1 ${cur === fn.id ? 'is-current' : ''} ${fn.isSelected ? 'is-selected-fw' : 'is-collapsed-fw'}"
             style="left:${fn.x}px; top:${fn.y}px; width:${fn.w}px; height:${fn.h}px;">
            <a class="mm-item-main" href="#/${fn.id}" data-mm-nav="1">
              <div class="mm-lv-badge">L1 · 论文框架 (${fn.id})</div>
              <div class="mm-item-title">${fn.data.title}</div>
              <div class="mm-item-meta">
                <span class="tag ${fn.data.status === 'active' ? 'tone-good' : ''}">${L.fwStatus[fn.data.status] || fn.data.status}</span>
                <span class="muted small">共 ${(fn.data.chapters || []).length} 章</span>
              </div>
            </a>
            ${!fn.isSelected ? html`<button type="button" class="mm-fw-switch" data-action="mm-switch-fw" data-fw="${fn.id}" title="在思维导图中展开此框架">▶ 展开</button>` : ''}
          </div>`)}

          <!-- Level 2: Chapters (Children of Selected Framework) -->
          ${layout.chNodes.map(cn => html`<div class="mm-item mm-lv2 ${cur === cn.id ? 'is-current' : ''}"
             style="left:${cn.x}px; top:${cn.y}px; width:${cn.w}px; height:${cn.h}px; border-left: 4px solid ${cn.color};">
            <a class="mm-item-main mm-ch-link" href="${NB.refHash(cn.id, fw.id)}" data-mm-nav="1">
              <div class="mm-ch-row">
                <span class="mm-ch-num" style="color:${cn.color}">第${cn.data.no}章</span>
                <strong class="mm-ch-name" title="${cn.data.title}">${cn.data.title}</strong>
              </div>
              <div class="mm-item-meta">
                ${anchorBadge(cn.data.anchor_status)}
                ${cn.data.stage ? html`<span class="tag">创新 ${cn.data.stage}</span>` : ''}
                ${cn.totalQ ? html`<span class="muted small">${cn.totalQ}题</span>` : ''}
                ${count(cn.data.pending)}
              </div>
            </a>
            <button type="button" class="mm-port-add" data-action="new-question" data-ref="${cn.id}" data-fw="${fw.id}" title="在第${cn.data.no}章下拆解/新建小问题">＋</button>
          </div>`)}

          <!-- Level 3: Small Questions (Children of Chapter) -->
          ${layout.qNodes.map(qn => html`<a class="mm-item mm-lv3 ${cur === qn.id ? 'is-current' : ''}"
             href="${NB.refHash(qn.id, fw.id)}" data-mm-nav="1"
             style="left:${qn.x}px; top:${qn.y}px; width:${qn.w}px; height:${qn.h}px; border-left: 3px solid ${qn.color};">
            <span class="qdot tone-${L.qTone[qn.data.status] || 'muted'}" aria-hidden="true"></span>
            <span class="mm-q-name" title="${qn.data.title}">${qn.data.title}</span>
            <span class="tag ${qn.data.anchor_status === 'confirmed' ? 'tone-good' : 'tone-muted'}">${qn.data.anchor_status === 'confirmed' ? '已锚定' : '探索中'}</span>
            ${count(qn.data.pending)}
          </a>`)}

          ${!fws.length ? html`<div class="mm-empty-floating" style="left: 310px; top: 36px; width: 420px;">
            <p><b>尚未生成 L1 论文框架与 L2 章节树</b></p>
            <p class="muted small">请先填写下方「1. 项目边界条件」，然后让 AI 拟定框架或导入已有框架大纲，系统会自动生成多级思维导图。</p>
            <div class="form-actions">
              <button type="button" class="primary" data-action="quick-propose">AI 按边界拟定框架</button>
              <button type="button" data-action="quick-import-fw">导入已有框架/讨论稿</button>
              <button type="button" class="quiet" data-action="new-framework">手动新建框架</button>
            </div>
          </div>` : ''}
        </div>
      </div>
    </section>`;
  };

  V.chaptersOverviewCard = function (it) {
    if (it.level !== 'framework_layer') return '';
    const t = S.tree;
    const fws = (t && t.frameworks) || [];
    const fw = fws.find(f => f.id === S.fw) || fws.find(f => f.status === 'active') || fws[0] || null;
    const chapters = (fw && fw.chapters) || [];
    return html`<section class="card chapters-overview" id="sec-chapters-overview" aria-labelledby="ch-ov-h">
      <div class="card-head">
        <h2 id="ch-ov-h">3. 各章深入讨论与验证总览${fw ? `（当前框架：${fw.title} · 共 ${chapters.length} 章）` : ''}</h2>
        <span class="hint">在锁定锚点前，可进入任意章节发散讨论、跑 S1–S6 最小验证实验并拆解小问题</span>
      </div>
      ${chapters.length ? html`<div class="table-wrap"><table class="grid ch-ov-table">
        <thead><tr><th>章节</th><th>类型</th><th>锚定状态</th><th>探索 / 验证进度</th><th>已拆小问题（点击进入）</th><th>操作</th></tr></thead>
        <tbody>
          ${chapters.map(c => {
            const qs = c.questions || [];
            const sup = qs.filter(q => q.status === 'supported').length;
            const conf = qs.filter(q => q.anchor_status === 'confirmed').length;
            return html`<tr>
              <td><a class="bold" href="${NB.refHash(c.id, fw.id)}">第${c.no}章 ${c.title}</a></td>
              <td>${V.roleTag(c)}</td>
              <td>${c.anchor_status === 'confirmed' ? html`<span class="tag tone-good">已锚定</span>` : c.anchor_status === 'draft' ? html`<span class="tag tone-warn">锚点草稿</span>` : html`<span class="tag tone-info">探索中</span>`}</td>
              <td>
                ${c.stage ? html`<span class="tag">创新 ${c.stage}</span> ` : ''}
                <span class="muted small">台账 确${(c.ledger && c.ledger['确定']) || 0} / 待${(c.ledger && c.ledger['待定']) || 0}${sup ? ` · 验证支持 ${sup}` : ''}</span>
              </td>
              <td>
                ${qs.length ? html`<div class="ch-ov-qs">${qs.map(q => html`<a class="ch-ov-q" href="${NB.refHash(q.id, fw.id)}"><span class="qdot tone-${L.qTone[q.status] || 'muted'}"></span>${q.title}${q.anchor_status === 'confirmed' ? html` <span class="tag tone-good">已锚定</span>` : ''}</a>`)}</div>`
                  : html`<span class="muted small">尚未拆解小问题（${conf}/${qs.length}）</span>`}
              </td>
              <td class="nowrap">
                <a class="btn btn-small primary" href="${NB.refHash(c.id, fw.id)}">进入本章讨论</a>
                <button type="button" class="btn-small" data-action="new-question" data-ref="${c.id}" data-fw="${fw.id}">+ 拆小问题</button>
              </td>
            </tr>`;
          })}
        </tbody>
      </table></div>` : html`<p class="muted">还没有建立论文框架。请先在上方「2. 论文框架与章节划分」中让 AI 拟定或导入框架。</p>`}
    </section>`;
  };

  V.onboardingCard = function (it) {
    if (it.level !== 'framework_layer' || (it.frameworks || []).length) return '';
    return html`<section class="card onboarding" id="sec-onboarding" aria-labelledby="onboard-h">
      <div class="card-head">
        <h2 id="onboard-h">从发散到聚焦：四步开启你的论文研究</h2>
        <span class="tag tone-info">全新工作流</span>
      </div>
      <ol class="onboard-steps">
        <li><b>第 1 步（立边界）</b>：在下方「项目边界条件、配置与研究需求」里填入你的实验配置、方向、背景与需求（可点「填入示例模板」）。</li>
        <li><b>第 2 步（定框架与自动分章）</b>：在右侧点 <b>「AI 拟定候选框架」</b>，或点 <b>「导入已有框架/讨论稿」</b> 由 AI 自动填充；选定后自动按讨论结果分好章节（不限 7 章）。</li>
        <li><b>第 3 步（深入讨论验证）</b>：点击思维导图进入各章，对发散的研究内容进行深入讨论、文献查新、跑最小验证实验，拆解出具体的「小问题」。</li>
        <li><b>第 4 步（逐级锚定成文）</b>：验证跑通后，依次完成 <b>小问题锚定 → 研究内容（章节）锚定 → 论文框架锚定</b>，汇总生成整章论文草稿。</li>
      </ol>
      <div class="form-actions">
        <button type="button" class="primary" data-action="fill-project-template">1. 填入边界条件模板</button>
        <button type="button" data-action="quick-propose">2A. 让 AI 拟定框架</button>
        <button type="button" data-action="quick-import-fw">2B. 导入已有框架/讨论稿</button>
        <button type="button" class="quiet" data-action="open-guide">查看完整实操教程</button>
      </div>
    </section>`;
  };

  V.mainHtml = function (it) {
    const parts = [V.crumbs(it), V.titleBlock(it)];
    if ((it.upstream || []).length) parts.push(V.upstream(it));
    parts.push(V.learnBlock(it));
    if (it.level === 'framework_layer') parts.push(V.onboardingCard(it), V.mindmap(it, false), V.projectCard(it), V.candidates(it), V.frameworkCards(it), V.chaptersOverviewCard(it));
    if (it.level === 'framework') parts.push(V.mindmap(it, false), V.frameworkFields(it));
    if (it.level === 'chapter') parts.push(V.stageBar(it), V.childList(it), V.proposedQuestions(it));
    parts.push(V.anchorCard(it));
    if (it.level === 'chapter' || it.level === 'question') parts.push(V.docBlock(it));
    if (it.level === 'question') parts.push(V.adoptBlock(it));
    parts.push(V.ledger(it));
    if (it.materials) parts.push(V.materials(it));
    return html`${parts}`;
  };

  V.crumbs = it => ((it.breadcrumb || []).length < 2 ? '' : html`<nav class="crumbs" aria-label="当前位置">${it.breadcrumb.map((c, i, all) => (i === all.length - 1
    ? html`<span aria-current="page">${c.title}</span>`
    : html`<a href="${NB.refHash(c.ref, it.fw || S.fw)}">${c.title}</a><span class="sep" aria-hidden="true">/</span>`))}</nav>`);

  V.adoptReason = function (it) {
    const a = it.anchor || {};
    if (a.status !== 'confirmed') return '锚点确认后才能采纳';
    if ((a.assumptions || []).some(x => x.status === '待批') || (a.requests || []).some(r => r.status === '待处理')) return '先处理待批假设和锚点变更申请';
    if (!(it.doc && (it.doc.text || '').trim())) return '正文还是空的';
    if ((it.upstream || []).length) return '上游结论有变化，先复查';
    if ((it.pending || []).some(p => p.kind === 'decision')) return '先回答“待你决定”里的问题';
    return '暂时不能采纳';
  };

  V.titleBlock = function (it) {
    const meta = [];
    if (it.level === 'question' && it.status_label) meta.push(html`<span class="tag tone-${it.stale ? 'warn' : (L.qTone[it.status] || 'muted')}">${it.status_label}</span>`);
    if (it.level === 'chapter' && it.chapter) meta.push(html`<span class="tag">${it.chapter.role_label || L.chapterRole[it.chapter.role] || ''}</span>`);
    if (it.level === 'framework') meta.push(html`<span class="tag ${it.status === 'active' ? 'tone-good' : ''}">${L.fwStatus[it.status] || it.status}</span>`);
    const actions = [];
    if (it.level === 'question') {
      actions.push(html`<button type="button" class="primary" data-action="adopt" ${it.can_adopt ? '' : raw('disabled')}>采纳</button>`);
      if (!it.can_adopt) actions.push(html`<span class="hint">${V.adoptReason(it)}</span>`);
    }
    if (it.level === 'framework' && it.status !== 'active') actions.push(html`<button type="button" data-action="set-active" data-ref="${it.ref}">设为主推</button>`);
    if (it.level === 'chapter') actions.push(html`<button type="button" class="quiet" data-action="edit-chapter">章名与角色</button>`);
    const prefix = it.level === 'chapter' && it.chapter && it.chapter.no ? '第' + it.chapter.no + '章 ' : '';
    return html`<header class="item-head"><div class="item-title"><h1>${prefix}${it.title}</h1>${meta.length ? html`<div class="meta">${meta}</div>` : ''}</div>
      ${actions.length ? html`<div class="actions">${actions}</div>` : ''}</header>`;
  };

  V.upstream = it => html`<div class="notice notice-warn" role="note"><b>上游结论有变化，需要复查：</b><ul>${it.upstream.map(u => html`<li>${u.message}${u.ref ? html` · <a href="${NB.refHash(String(u.ref).split('#')[0], it.fw || S.fw)}">${u.ref}</a>` : ''}</li>`)}</ul></div>`;

  // Learning mode pause: staged revisions side by side, the attack, and the three choices.
  V.learnBlock = function (it) {
    const s = NB.sessionFor(it.ref);
    if (!s || s.status !== 'paused' || !s.learn) return '';
    const l = s.learn;
    const base = NB.dirname(it.doc && it.doc.path);
    const staged = l.staged || [];
    return html`<section class="card learn" id="sec-learn" aria-labelledby="learn-h">
      <div class="card-head"><h2 id="learn-h">学习模式：这一步等你决定</h2></div>
      ${staged.length ? staged.map(st => html`<div class="staged">
          <h3 class="sub">${st.section || '（未指明小节）'}${st.mode ? ' · ' + st.mode : ''}${st.status && st.status !== '已应用' ? html` <span class="tag tone-warn">${st.status}</span>` : ''}</h3>
          <div class="cols"><div class="col"><div class="col-h">原文</div>${(st.old || '').trim() ? NB.md(st.old, base) : html`<p class="muted">（原来没有这一节）</p>`}</div>
          <div class="col"><div class="col-h">修改后</div>${NB.md(st.new, base)}</div></div></div>`)
        : html`<p class="muted">这一步没有修改正文。</p>`}
      ${l.attack ? html`<h3 class="sub">审查意见</h3><div class="quoteish">${NB.md(l.attack, base)}</div>` : ''}
      ${l.explain ? html`<h3 class="sub">作者的解释</h3><div class="quoteish">${NB.md(l.explain, base)}</div>` : ''}
      <div class="form-actions"><button type="button" class="primary" data-action="learn" data-decision="continue">继续</button>
        <button type="button" data-action="learn" data-decision="ask">追问</button>
        <button type="button" class="danger" data-action="learn" data-decision="reject">打回</button></div>
    </section>`;
  };

  // ---- anchor ----
  V.anchorCard = function (it) {
    const a = it.anchor;
    if (!a) return '';
    if (S.edit && S.edit.kind === 'anchor' && S.edit.ref === it.ref) return V.anchorEditor(it);
    const status = !a.exists ? 'none' : a.status;
    const anchorTitle = it.level === 'question' ? '小问题锚定' : it.level === 'chapter' ? '研究内容（本章）锚定' : it.level === 'framework_layer' ? '4. 逐级锚定成文（论文总框架锚定）' : '论文总框架锚定';
    const badge = status === 'confirmed' ? html`<span class="tag tone-good">已锚定 第${a.version}版</span>`
      : status === 'none' ? html`<span class="tag tone-info">探索发散中（未锚定）</span>`
        : html`<span class="tag tone-warn">锚点草稿${a.version ? ' 第' + a.version + '版' : ''}</span>`;
    const base = NB.dirname(a.path);
    const slots = a.slots || [];
    const guide = it.level === 'question'
      ? '尚未锚定本小问题。可点“AI 收敛起草锚点”或“手动编辑锚点”界定问题与判据，再点“确认锚定”进入多轮推导与代码验证。'
      : it.level === 'chapter'
        ? '当前处于【研究内容探索阶段】：你可以先在右侧自由讨论本章内容、跑创新验证或拆解小问题；等本章思路聚焦且小问题确定后，点“AI 收敛起草锚点”并确认锚定。'
        : '当前处于【框架探索阶段】：你可以先拟定或导入框架、深入讨论各章研究内容；等各章研究内容与小问题聚焦后，再收敛并确认全篇锚定。';
    return html`<section class="card anchor ${status === 'confirmed' ? '' : 'is-draft'}" id="sec-anchor" aria-labelledby="anchor-h">
      <div class="card-head"><h2 id="anchor-h">${anchorTitle}</h2>${badge}
        ${status === 'confirmed' && a.confirmed_at ? html`<span class="hint">${NB.fmtTime(a.confirmed_at)} 锚定</span>` : ''}
        ${it.level === 'framework' ? html`<span class="hint">与“论文框架”共用</span>` : ''}
        <div class="actions">
          ${it.level !== 'framework' ? html`<button type="button" data-action="quick-open-anchor">AI 收敛起草锚点</button>` : ''}
          <button type="button" data-action="edit-anchor">手动编辑锚点</button>
          ${a.exists ? html`<button type="button" class="quiet" data-action="history" data-path="${a.path}" data-revision="${a.revision}">历史版本</button>` : ''}
          ${a.exists && status !== 'confirmed' ? html`<button type="button" class="primary" data-action="confirm-anchor">确认锚定</button>` : ''}</div></div>
      ${a.exists && status !== 'confirmed' && a.ever_confirmed ? html`<p class="hint warn">锚点有改动，检查后点“确认锚定”。确认前不能继续讨论。</p>` : ''}
      ${!a.exists ? html`<p class="muted">${guide}</p>` : ''}
      ${a.exists && slots.length ? html`<dl class="slots">${slots.map(s => html`<div class="slot"><dt>${s.label}</dt><dd>${(s.text || '').trim() ? NB.md(s.text, base) : html`<span class="muted">未填写</span>`}</dd></div>`)}</dl>` : ''}
      ${V.assumptionTable(a)}
      ${V.requestList(a)}
    </section>`;
  };

  V.assumptionTable = function (a) {
    const rows = a.assumptions || [];
    if (!rows.length) return '';
    const live = rows.filter(x => x.status !== '已拒绝');
    const rejected = rows.filter(x => x.status === '已拒绝');
    return html`<h3 class="sub">${a.assumptions_label || '假设'}</h3>
      ${live.length ? html`<div class="table-wrap"><table class="grid"><thead><tr><th>编号</th><th>内容</th><th>依据</th><th>失效条件</th><th>状态</th><th>来源</th></tr></thead><tbody>
        ${live.map(x => html`<tr class="${x.status === '待批' ? 'row-pending' : ''}"><td class="mono">${x.id}</td><td>${NB.mdInline(x.text)}</td><td>${NB.mdInline(x.basis)}</td><td>${NB.mdInline(x.failure)}</td>
          <td><span class="tag ${x.status === '待批' ? 'tone-warn' : 'tone-good'}">${x.status}</span></td><td class="muted small">${x.source}</td></tr>`)}
      </tbody></table></div>` : ''}
      ${rejected.length ? html`<p class="hint">已拒绝：${rejected.map(x => x.id + ' ' + x.text).join('；')}</p>` : ''}`;
  };

  V.requestList = function (a) {
    const reqs = a.requests || [];
    if (!reqs.length) return '';
    const tone = s => (s === '待处理' ? 'tone-warn' : s === '已批准' ? 'tone-good' : 'tone-muted');
    return html`<h3 class="sub">变更申请</h3><ul class="reqs">${reqs.map(r => html`<li class="${r.status === '待处理' ? 'row-pending' : ''}">
      <span class="mono">${r.id}</span> 申请把“${r.slot}”改为：${NB.mdInline(r.proposal)}
      <span class="muted small">（理由：${r.reason}；${r.source}）</span> <span class="tag ${tone(r.status)}">${r.status}</span>${r.note ? html` <span class="muted small">${r.note}</span>` : ''}</li>`)}</ul>`;
  };

  V.assumptionRow = x => html`<div class="assume-row" data-id="${x.id || ''}">
    <span class="mono aid">${x.id || '新'}</span>${x.status === '待批' ? html`<span class="tag tone-warn">待批</span>` : ''}
    <label><span class="sr">内容</span><input name="a-text" value="${x.text || ''}" placeholder="内容"></label>
    <label><span class="sr">依据</span><input name="a-basis" value="${x.basis || ''}" placeholder="依据"></label>
    <label><span class="sr">失效条件</span><input name="a-failure" value="${x.failure || ''}" placeholder="失效条件"></label>
    <button type="button" class="quiet" data-action="remove-assumption">删除</button></div>`;

  V.anchorEditor = function (it) {
    const a = it.anchor;
    return html`<section class="card anchor editing" id="sec-anchor" aria-labelledby="anchor-h">
      <div class="card-head"><h2 id="anchor-h">编辑锚点</h2>${a.status === 'confirmed' && a.exists ? html`<span class="hint">保存后锚点回到草稿，需要重新确认。</span>` : ''}</div>
      ${editError(a.path)}
      <form data-form="anchor" data-keep="anchor:${it.ref}">
        ${(a.slots || []).map(s => html`<label class="field"><span>${s.label}</span><textarea name="slot:${s.key}" rows="3">${s.text || ''}</textarea></label>`)}
        <fieldset class="assume-edit"><legend>${a.assumptions_label || '假设'}（内容、依据、失效条件）</legend>
          <div class="assume-rows">${(a.assumptions || []).filter(x => x.status !== '已拒绝').map(x => V.assumptionRow(x))}</div>
          <button type="button" class="quiet" data-action="add-assumption">+ 添加一条</button>
          <p class="hint">删除的假设会标为“已拒绝”，新加的假设由你确认。</p>
        </fieldset>
        <div class="form-actions"><button type="submit" class="primary">保存锚点</button><button type="button" data-action="cancel-edit">取消</button></div>
      </form>
    </section>`;
  };

  // ---- document ----
  function editError(path) {
    if (!S.edit || !S.edit.error) return '';
    return html`<div class="notice notice-bad" role="alert">${S.edit.error}
      ${S.edit.conflict ? html`<div class="form-actions">${path ? html`<a class="btn btn-small" href="${NB.fileUrl(path)}" data-file="${path}">查看文件现在的内容</a>` : ''}
        <button type="button" class="btn-small danger" data-action="force-save">用我的文字覆盖</button></div>` : ''}</div>`;
  }

  V.docBlock = function (it) {
    const d = it.doc;
    if (!d) return '';
    const editing = S.edit && S.edit.kind === 'doc' && S.edit.ref === it.ref;
    const title = d.kind === 'mini_paper' ? '小论文正文' : d.kind === 'chapter_draft' ? '本章草稿' : '正文';
    const tools = editing
      ? html`<button type="button" class="primary" data-action="save-doc">保存</button><button type="button" data-action="cancel-edit">取消</button><span class="hint">Ctrl+S 保存</span>`
      : html`<button type="button" data-action="edit-doc">编辑</button>${d.path ? html`<button type="button" class="quiet" data-action="history" data-path="${d.path}">历史版本</button>` : ''}`;
    return html`<section class="sec doc" id="sec-doc" aria-labelledby="doc-h">
      <div class="sec-head"><h2 id="doc-h">${title}</h2><div class="actions">${tools}</div></div>
      ${editing ? editError(d.path) : ''}
      ${editing ? html`<div class="editor" data-keep="doc:${it.ref}"><textarea class="doc-text" aria-label="${title}" spellcheck="false">${S.edit.text}</textarea></div>`
        : (d.text || '').trim() ? html`<article class="paper">${NB.md(V.dropTitle(d.text, it.title), NB.dirname(d.path))}</article>` : html`<p class="muted">正文还是空的。</p>`}
    </section>`;
  };

  const FW_FIELDS = [['mainline', '论文主线'], ['chapters', '章节安排'], ['risks', '优点与风险']];
  V.frameworkFields = function (it) {
    const d = it.doc || {};
    const f = d.fields || {};
    const editing = S.edit && S.edit.kind === 'fields' && S.edit.ref === it.ref;
    const fwv = (it.frameworks || [])[0] || {};
    const base = NB.dirname(d.path);
    const tools = editing
      ? html`<button type="button" class="primary" data-action="save-fields">保存</button><button type="button" data-action="cancel-edit">取消</button><span class="hint">Ctrl+S 保存</span>`
      : html`<button type="button" data-action="edit-fields">编辑</button>${d.path ? html`<button type="button" class="quiet" data-action="history" data-path="${d.path}">历史版本</button>` : ''}`;
    return html`<section class="sec doc" id="sec-doc" aria-labelledby="doc-h">
      <div class="sec-head"><h2 id="doc-h">框架内容</h2><div class="actions">${tools}</div></div>
      ${editing ? editError(d.path) : ''}
      ${editing ? html`<form class="editor" data-form="fields" data-keep="fields:${it.ref}">
          <label class="field"><span>框架名称</span><input name="title" value="${it.title}"></label>
          ${FW_FIELDS.map(([k, label]) => html`<label class="field"><span>${label}</span><textarea name="${k}" rows="${k === 'chapters' ? 12 : 5}">${f[k] || ''}</textarea></label>`)}
        </form>`
        : FW_FIELDS.map(([k, label]) => html`<h3 class="sub">${label}</h3>${(f[k] || '').trim() ? NB.md(f[k], base) : html`<p class="muted">未填写</p>`}`)}
      ${(fwv.chapters || []).length ? html`<h3 class="sub">已建立的章</h3><ul class="plain chapter-list">${fwv.chapters.map(c => html`<li><a href="${NB.refHash(c.id, it.ref)}">第${c.no}章 ${c.title}</a>${V.roleTag(c)}</li>`)}</ul>` : ''}
    </section>`;
  };

  // ---- framework layer ----
  V.frameworkCards = function (it) {
    const list = it.frameworks || [];
    return html`<section class="sec" id="sec-frameworks" aria-labelledby="fws-h">
      <div class="sec-head"><h2 id="fws-h">2. 论文框架与章节划分（${list.length}）</h2>
        <div class="actions">
          <button type="button" data-action="quick-propose">AI 拟定候选框架</button>
          <button type="button" data-action="quick-import-fw">导入已有框架（AI解析）</button>
          <button type="button" data-action="new-framework">+ 手动新建 / 粘贴大纲分章</button>
        </div>
      </div>
      ${list.length ? html`<div class="fw-cards">${list.map(f => html`<article class="card fw-card ${f.status === 'active' ? 'is-active' : ''}">
          <div class="card-head"><h3><a href="#/${f.id}">${f.title}</a></h3>${f.status === 'active' ? html`<span class="tag tone-good">主推（共 ${(f.chapters || []).length} 章）</span>` : html`<span class="tag">${L.fwStatus[f.status] || f.status}（共 ${(f.chapters || []).length} 章）</span>`}</div>
          ${(f.mainline || '').trim() ? NB.md(f.mainline, NB.dirname(f.path)) : html`<p class="muted">还没有写论文主线。</p>`}
          ${(f.chapters || []).length ? html`<ul class="plain chapter-list compact">${f.chapters.map(c => html`<li><a href="${NB.refHash(c.id, f.id)}">第${c.no}章 ${c.title}</a>${V.roleTag(c)}</li>`)}</ul>` : ''}
          <div class="form-actions"><a class="btn" href="#/${f.id}">进入框架讨论</a>${f.status !== 'active' ? html`<button type="button" data-action="set-active" data-ref="${f.id}">设为主推</button>` : ''}</div>
        </article>`)}</div>` : html`<p class="muted">还没有建立框架。你可以点上方 <b>「AI 拟定候选框架」</b>（由 AI 根据边界条件讨论生成），或点 <b>「导入已有框架（AI解析）」</b> 把已有大纲粘贴进来自动分章。</p>`}
    </section>`;
  };

  // Readable form of a 候选框架 outline: no repeated title, "| research" shown as （研究问题）.
  V.candidateText = c => V.dropTitle(c.markdown || '', c.title)
    .replace(/^(#{2,6}[ \t]+.*?)[ \t]*\|[ \t]*(intro|theory|research|framework|conclusion|other)[ \t]*$/gm, (m, head, role) => head + '（' + L.chapterRole[role] + '）');

  V.candidates = function (it) {
    const list = it.candidates || [];
    if (!list.length) return '';
    return html`<section class="sec" id="sec-candidates" aria-labelledby="cand-h">
      <div class="sec-head"><h2 id="cand-h">候选框架（${list.length}）</h2><div class="actions"><button type="button" class="quiet" data-action="dismiss" data-kind="candidates">全部忽略</button></div></div>
      ${list.map(c => {
        const key = 'cand:' + c.index;
        const open = S.open.has(key);
        return html`<article class="card cand">
          <div class="card-head"><h3>${c.title || '候选框架 ' + (Number(c.index) + 1)}</h3>${c.created ? html`<span class="tag tone-good">已建立为 ${c.created}</span>` : ''}
            <div class="actions">${c.created ? html`<a class="btn" href="#/${c.created}">打开</a>` : html`<button type="button" class="primary" data-action="create-from-candidate" data-index="${c.index}">建立为框架</button>`}</div></div>
          <div class="${open ? '' : 'clip'}">${NB.md(V.candidateText(c))}</div>
          <button type="button" class="quiet" data-action="toggle" data-key="${key}" aria-expanded="${open}">${open ? '收起' : '展开全文'}</button>
        </article>`;
      })}
    </section>`;
  };

  // ---- chapter ----
  V.stageBar = function (it) {
    const st = it.stage;
    if (!st) return '';
    const last = st.current === 'S6';
    return html`<section class="stagebar" id="sec-stage" aria-labelledby="stage-h">
      <h2 id="stage-h" class="sr">创新流程阶段</h2>
      <ol>${(st.stages || []).map(s => html`<li class="${s.key === st.current ? 'current' : ''} ${s.done ? 'done' : ''}"><span class="sk">${s.key}</span><span class="sl">${s.label}</span>${s.done ? html`<span class="sr">（已完成）</span>` : ''}${s.key === st.current ? html`<span class="sr">（当前阶段）</span>` : ''}</li>`)}</ol>
      <div class="stage-foot">${st.done ? html`<span>${st.done} 已完成。看过结果、做完勾选后，进入下一阶段。</span>` : html`<span class="muted">当前阶段 ${st.current}：在右侧“下一轮”里运行“创新流程（当前阶段）”。</span>`}
        <button type="button" class="${st.done ? 'primary' : ''}" data-action="next-stage" ${last ? raw('disabled') : ''}>进入下一阶段</button></div>
    </section>`;
  };

  V.childList = function (it) {
    const kids = it.children || [];
    const fw = it.fw || S.fw;
    return html`<section class="sec" id="sec-children" aria-labelledby="kids-h">
      <div class="sec-head"><h2 id="kids-h">小问题</h2><div class="actions"><button type="button" data-action="new-question" data-ref="${it.ref}" data-fw="${fw}">+ 新建小问题</button></div></div>
      ${kids.length ? html`<ul class="plain kid-list">${kids.map(k => html`<li><span class="qdot tone-${L.qTone[k.status] || 'muted'}" aria-hidden="true"></span><a href="${NB.refHash(k.ref, fw)}">${k.title}</a> <span class="tag tone-${L.qTone[k.status] || 'muted'}">${k.status_label || ''}</span></li>`)}</ul>`
        : html`<p class="muted">还没有小问题。</p>`}
    </section>`;
  };

  V.proposedQuestions = function (it) {
    const list = it.proposed_questions || [];
    if (!list.length) return '';
    const group = 'pq:' + it.ref;
    const picked = NB.picks(group, list.map((_, i) => String(i)));
    return html`<section class="sec" id="sec-questions" aria-labelledby="pq-h">
      <div class="sec-head"><h2 id="pq-h">AI 拆出的小问题（${list.length}）</h2></div>
      <p class="hint">勾选要建立的小问题。建立后每个小问题都带锚点草稿，需要你确认。</p>
      <ul class="check-list">${list.map((q, i) => html`<li><label class="check"><input type="checkbox" data-pick="${group}" value="${i}" ${picked.has(String(i)) ? raw('checked') : ''}>
        <span><b>${q.title}</b>${q.load_bearing ? html` <span class="tag tone-warn">承重</span>` : ''}
          ${q.question ? html`<span class="line">问题：${NB.mdInline(q.question)}</span>` : ''}
          ${q.criterion ? html`<span class="line">判据：${NB.mdInline(q.criterion)}</span>` : ''}
          ${q.depends ? html`<span class="line muted">依赖：${q.depends}</span>` : ''}
          ${q.method ? html`<span class="line muted">验证方式：${q.method}</span>` : ''}</span></label></li>`)}</ul>
      <div class="form-actions"><button type="button" class="primary" data-action="create-questions">建立选中的小问题</button>
        <button type="button" class="quiet" data-action="dismiss" data-kind="questions">忽略</button></div>
    </section>`;
  };

  // ---- ledger ----
  // DOI and arXiv numbers in 文献 entries become links, so you can check them yourself.
  function refText(text) {
    let s = NB.esc(text || '');
    s = s.replace(/\b(10\.\d{4,9}\/[^\s·，；、。：）)|<]+)/g, (m, doi) => '<a href="https://doi.org/' + doi + '" target="_blank" rel="noopener noreferrer">' + doi + '</a>');
    s = s.replace(/\barXiv[:：]?\s*(\d{4}\.\d{4,5}(?:v\d+)?)/gi, (m, id) => '<a href="https://arxiv.org/abs/' + id + '" target="_blank" rel="noopener noreferrer">' + m + '</a>');
    return raw(s);
  }

  // R01 / V01 in the evidence column open the run record or the verification file.
  function evidence(it, text) {
    const m = it.materials || {};
    return String(text || '').split(/([,，、;；\s]+)/).map(tok => {
      const run = /^R\d+$/.test(tok) && (m.runs || []).find(r => r.id === tok);
      if (run) return V.fileLink(run.path, tok, 'mono');
      const ver = /^V\d+$/.test(tok) && (m.verifications || []).find(v => v.id === tok);
      if (ver) return V.fileLink(ver.path, tok, 'mono');
      return tok;
    });
  }

  V.entryTable = function (it, list, base) {
    if (!list.length) return html`<p class="muted">没有符合筛选的条目。</p>`;
    return html`<div class="table-wrap"><table class="grid entries"><thead><tr><th>编号</th><th>类型</th><th>内容</th><th>状态</th><th>依据</th><th>证据</th><th>备注</th><th><span class="sr">操作</span></th></tr></thead><tbody>
      ${list.map(e => html`<tr class="${e.state === '否定' ? 'is-rejected' : ''}">
        <td class="mono">${e.id}</td><td class="nowrap">${e.type}</td>
        <td class="text">${e.type === '文献' ? refText(e.text) : NB.mdInline(e.text, base)}</td>
        <td><span class="tag ${toneOf(e.state)}">${e.state}</span></td><td class="mono small">${e.basis}</td><td class="small">${evidence(it, e.evidence)}</td>
        <td class="small">${e.note}${e.superseded_by ? html` <span class="muted">被 ${e.superseded_by} 替代</span>` : ''}<div class="muted">${e.source}${e.updated ? ' · ' + e.updated : ''}</div></td>
        <td><button type="button" class="btn-small" data-action="entry-menu" data-id="${e.id}" aria-expanded="${S.ledgerMenu === e.id}">改状态</button></td></tr>
        ${S.ledgerMenu === e.id ? html`<tr class="entry-menu"><td colspan="8">
          <form data-form="set-entry" data-id="${e.id}" class="inline-form">
            <label class="grow"><span>说明（可不填）</span><input name="note" placeholder="为什么这样改"></label>
            ${['确定', '否定', '待定'].filter(s => s !== e.state).map(s => html`<button type="submit" name="state" value="${s}" class="${s === '确定' ? 'primary' : ''}">设为${s}</button>`)}
            <button type="button" class="quiet" data-action="entry-menu" data-id="">取消</button>
          </form></td></tr>` : ''}`)}
    </tbody></table></div>`;
  };

  V.pickBlock = function (it, kind, list) {
    const group = kind + ':' + it.ref;
    const picked = NB.picks(group, []);
    const ideas = kind === 'ideas';
    return html`<div class="card-soft pick" id="sec-${kind}">
      <h3 class="sub">${ideas ? '筛选想法' : '核对文献'}</h3>
      <p class="hint">${ideas ? '勾选要保留的想法。没有勾选的待定想法会标为否定，进入淘汰清单。' : '逐条打开 DOI 或 arXiv 链接核对，确认无误再勾选。'}</p>
      <ul class="check-list">${list.map(e => html`<li><label class="check"><input type="checkbox" data-pick="${group}" value="${e.id}" ${picked.has(e.id) ? raw('checked') : ''}>
        <span><span class="mono">${e.id}</span> ${ideas ? NB.mdInline(e.text) : refText(e.text)}</span></label></li>`)}</ul>
      <div class="form-actions"><button type="button" class="primary" data-action="${ideas ? 'pick-ideas' : 'verify-refs'}">${ideas ? '保留选中的想法' : '标记为已核实'}</button></div>
    </div>`;
  };

  V.ledger = function (it) {
    const lg = it.ledger || { entries: [], counts: {} };
    const entries = lg.entries || [];
    const c = lg.counts || {};
    const f = S.ledgerFilter;
    const live = entries.filter(e => e.state !== '否定');
    const rejected = entries.filter(e => e.state === '否定');
    const shown = live.filter(e => f === 'all' || e.state === f);
    const ideas = entries.filter(e => e.type === '想法' && e.state === '待定');
    const refs = entries.filter(e => e.type === '文献' && e.state === '待定');
    const base = NB.dirname(lg.path);
    const openKey = 'rejected:' + it.ref;
    const seg = (key, label) => html`<button type="button" class="seg ${f === key ? 'on' : ''}" aria-pressed="${f === key}" data-action="ledger-filter" data-key="${key}">${label}</button>`;
    return html`<section class="sec ledger" id="sec-ledger" aria-labelledby="ledger-h">
      <div class="sec-head"><h2 id="ledger-h">台账</h2>
        <div class="counts"><span class="tag tone-warn">待定 ${c['待定'] || 0}</span><span class="tag tone-good">确定 ${c['确定'] || 0}</span><span class="tag tone-muted">否定 ${c['否定'] || 0}</span></div>
        ${lg.path ? html`<div class="actions">${V.fileLink(lg.path, '台账文件', 'btn quiet')}</div>` : ''}</div>
      ${entries.length ? html`
        <div class="segs" role="group" aria-label="按状态筛选">${seg('all', '全部')}${seg('待定', '只看待定')}${seg('确定', '只看确定')}</div>
        ${V.entryTable(it, shown, base)}
        ${ideas.length ? V.pickBlock(it, 'ideas', ideas) : ''}
        ${refs.length ? V.pickBlock(it, 'refs', refs) : ''}
        ${rejected.length ? html`<div class="fold"><button type="button" class="fold-btn" data-action="toggle" data-key="${openKey}" aria-expanded="${S.open.has(openKey)}">淘汰清单（否定 ${rejected.length} 条）</button>
          ${S.open.has(openKey) ? V.entryTable(it, rejected, base) : ''}</div>` : ''}`
        : html`<p class="muted">台账还是空的。每轮讨论后，AI 提出的论断和推导步骤会记在这里，状态由证据决定。</p>`}
    </section>`;
  };

  // ---- materials (questions) ----
  V.materials = function (it) {
    const m = it.materials || {};
    const codes = m.codes || [];
    const runs = m.runs || [];
    const vers = m.verifications || [];
    const old = m.legacy_discussions || [];
    if (!codes.length && !runs.length && !vers.length && !old.length) return '';
    const verdictTone = v => (v === '一致' ? 'tone-good' : v === '不一致' ? 'tone-bad' : 'tone-muted');
    return html`<section class="sec" id="sec-materials" aria-labelledby="mat-h"><div class="sec-head"><h2 id="mat-h">材料</h2></div>
      ${codes.length ? html`<h3 class="sub">代码</h3><ul class="plain mat">${codes.map(c => html`<li>${V.fileLink(c.path, c.name, 'mono')}${c.purpose ? html` <span class="muted small">${c.purpose}</span>` : ''}</li>`)}</ul>` : ''}
      ${runs.length ? html`<h3 class="sub">验证运行</h3><ul class="plain mat">${runs.map(r => html`<li>
          <div><span class="mono">${r.id}</span> ${V.fileLink(r.path, r.title || r.id)} <span class="tag tone-${L.outcomeTone[r.outcome] || 'muted'}">${r.outcome_label || r.outcome}</span>${r.plan_id ? html` <span class="muted small">计划 ${r.plan_id}</span>` : ''}</div>
          ${(r.files || []).length ? html`<div class="files">${r.files.map(f => V.fileLink(f.path, f.name, 'file-chip'))}</div>` : ''}</li>`)}</ul>` : ''}
      ${vers.length ? html`<h3 class="sub">独立复推</h3><ul class="plain mat">${vers.map(v => html`<li>${V.fileLink(v.path, v.id, 'mono')} <span class="muted small">针对 ${v.entry || '—'}</span> <span class="tag ${verdictTone(v.verdict)}">${v.verdict || '未给出'}</span></li>`)}</ul>` : ''}
      ${old.length ? html`<h3 class="sub">早期讨论</h3><ul class="plain mat">${old.map(d => html`<li><span class="mono">${d.id}</span> ${V.fileLink(d.path, d.title || d.id)}</li>`)}</ul>` : ''}
    </section>`;
  };

  // ---- adopted judgment and the AI's suggested conclusion (section 5) ----
  V.adoptBlock = function (it) {
    const staged = it.staged_conclusions || [];
    const j = it.judgment;
    if (!staged.length && !j) return '';
    const last = staged[staged.length - 1];
    const label = d => L.decision[d] || ({ review: '需复查', not_required: '不需要验证' })[d] || d;
    return html`<section class="sec" id="sec-adopt" aria-labelledby="adopt-h"><div class="sec-head"><h2 id="adopt-h">结论</h2></div>
      ${j ? html`<div class="notice notice-good"><b>已采纳：${label(j.decision)}</b>${j.text ? html` · ${NB.mdInline(j.text)}` : ''}${j.scope ? html`<div class="muted small">适用范围：${j.scope}</div>` : ''}</div>` : ''}
      ${last ? html`<div class="card-soft"><h3 class="sub">AI 建议的结论（第 5 节，等你采纳）</h3>${NB.md(last.body, NB.dirname(it.doc && it.doc.path))}
        <div class="form-actions"><button type="button" class="primary" data-action="adopt" ${it.can_adopt ? '' : raw('disabled')}>采纳…</button>
          <button type="button" class="quiet" data-action="dismiss" data-kind="conclusion">忽略这条建议</button></div></div>` : ''}
    </section>`;
  };
}());
