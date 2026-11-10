/* 新版页面：右侧栏——下一轮 / 进行中的讨论 / 待你决定 / 最近简报 / 历史轮次。 */
(function () {
  'use strict';
  const NB = window.NB;
  const { S, html, raw, L } = NB;
  const P = NB.P = {};
  P.MULTI = new Set(['advance', 'outline', 'refine']);   // recipes that may run 1–5 rounds
  const NEEDS_ANCHOR = new Set(['advance']);
  const JUMP = { questions: 'sec-questions', candidates: 'sec-candidates', ideas: 'sec-ideas', refs: 'sec-refs', stage: 'sec-stage', learn: 'sec-learn' };
  const KIND = { request: '锚点变更', assumption: '待批假设', decision: '需要你决定', learn: '学习模式', questions: '小问题',
    candidates: '候选框架', ideas: '想法', refs: '文献', stage: '阶段', revisions: '未写入的修改', anchor_draft: '锚点草稿' };

  P.render = function () {
    const side = document.getElementById('side');
    const it = S.item;
    if (!it) { side.innerHTML = ''; return; }
    NB.patch(side, html`${P.sessionArea(it)}${P.pending(it)}${P.brief(it)}${P.rounds(it)}`);
  };

  // The one session that is queued / running / paused anywhere, or null.
  P.activeSession = function () {
    if (S.session && NB.ACTIVE.has(S.session.status)) return S.session;
    const t = S.tree && S.tree.session;
    if (t && NB.ACTIVE.has(t.status) && !(S.ended && S.ended.id === t.id)) return t;
    return null;
  };

  P.draft = it => S.next[it.ref] || (S.next[it.ref] = { recipe: '', mode: 'auto', rounds: 0, focus: '' });
  P.roundsDefault = () => {
    const lim = S.settingsView && S.settingsView.settings && S.settingsView.settings.limits;
    return (lim && Number(lim.rounds_default)) || 0;
  };
  P.recipeOf = it => {
    const d = P.draft(it);
    const list = it.recipes || [];
    if (list.some(r => r.key === d.recipe)) return d.recipe;
    if (it.level === 'framework_layer' && !(it.frameworks || []).length && list.some(r => r.key === 'propose')) return 'propose';
    if (it.level === 'chapter' && !(it.anchor && it.anchor.status === 'confirmed') && list.some(r => r.key === 'innov')) return 'innov';
    if (it.level === 'chapter' && !(it.anchor && it.anchor.status === 'confirmed') && list.some(r => r.key === 'outline')) return 'outline';
    return list.some(r => r.key === it.default_recipe) ? it.default_recipe : (list[0] && list[0].key) || '';
  };

  P.titleOf = function (ref) {
    const t = S.tree;
    if (!t) return ref;
    if (ref === 'FW') return '论文框架';
    for (const f of t.frameworks || []) {
      if (f.id === ref) return f.title;
      for (const c of f.chapters || []) {
        if (c.id === ref) return (c.no ? '第' + c.no + '章 ' : '') + c.title;
        const q = (c.questions || []).find(x => x.id === ref);
        if (q) return q.title;
      }
    }
    for (const c of t.unplaced || []) {
      if (c.id === ref) return c.title;
      const q = (c.questions || []).find(x => x.id === ref);
      if (q) return q.title;
    }
    return ref;
  };

  P.sessionArea = function (it) {
    const active = P.activeSession();
    if (active && active.ref === it.ref) return P.sessionBox(active);
    const parts = [];
    if (S.ended && S.ended.ref === it.ref) parts.push(P.endedBox(S.ended));
    parts.push(active ? P.otherNotice(active) : P.nextBox(it));
    return html`${parts}`;
  };

  const RECIPE_ROLES = {
    open: ['author'],
    import_fw: ['author'],
    advance: ['author', 'reviewer', 'engineer', 'drift'],
    outline: ['author', 'reviewer', 'drift'],
    refine: ['author', 'reviewer', 'drift'],
    referee: ['author', 'referee'],
    propose: ['author', 'reviewer', 'writer', 'referee'],
    write: ['author', 'writer'],
    assemble: ['author', 'writer'],
    innov: ['author', 'writer'],
  };

  P.roleSummary = function (recipe) {
    const st = S.settingsView && S.settingsView.settings;
    const roles = (st && st.roles) || {};
    const wanted = RECIPE_ROLES[recipe] || ['author'];
    const items = wanted.map(r => {
      const c = (roles[r] || [])[0];
      if (!c) return null;
      const eff = c.effort ? ' [' + c.effort + ']' : '';
      return (L.agentRole[r] || r) + '：' + c.model + eff;
    }).filter(Boolean);
    return html`<div class="next-roles">
      <div class="next-roles-head"><b>本轮模型与强度</b><button type="button" class="linkish small" data-action="open-settings">更换模型与强度…</button></div>
      <div class="muted small">${items.length ? items.join(' · ') : '可在“设置”中为各角色选择模型与推理强度'}</div>
    </div>`;
  };

  P.linearStepHint = function (it, recipe) {
    const map = {
      question: [
        { step: '① 提出问题', key: 'open', label: '起草/收敛锚点' },
        { step: '②/③ 分析与解决', key: 'advance', label: '推进推导与验证' },
        { step: '③ 独立复推核验', key: 'referee', label: '审稿人独立复推' },
      ],
      chapter: [
        { step: '① 提出本章问题', key: 'open', label: '收敛本章锚点' },
        { step: '② 分析缺口创新', key: 'innov', label: '跑创新阶段' },
        { step: '③ 拆解小问题', key: 'outline', label: '提纲与拆题' },
        { step: '④ 汇总本章草稿', key: 'assemble', label: '组装本章草稿' },
      ],
      framework_layer: [
        { step: '②A AI 拟定框架', key: 'propose', label: 'AI 拟定框架' },
        { step: '②B 导入已有框架', key: 'import_fw', label: '导入大纲分章' },
        { step: '④ 收敛总锚点', key: 'open', label: '起草总框架锚点' },
      ],
    }[it.level] || [];
    const avail = map.filter(m => (it.recipes || []).some(r => r.key === m.key));
    if (!avail.length) return '';
    return html`<div class="next-linear-pills" role="group" aria-label="按线性步骤选择讨论任务">
      ${avail.map(m => html`<button type="button" class="nlp-btn ${recipe === m.key ? 'is-on' : ''}" data-action="quick-recipe" data-recipe="${m.key}">
        <span class="nlp-step">${m.step}</span>
        <span class="nlp-label">${m.label}</span>
      </button>`)}
    </div>`;
  };

  P.nextBox = function (it) {
    const recipes = it.recipes || [];
    if (!recipes.length) return '';
    const d = P.draft(it);
    const recipe = P.recipeOf(it);
    const def = P.roundsDefault();
    const rounds = d.rounds || def || 2;
    const reconfirm = it.anchor && it.anchor.ever_confirmed && it.anchor.status !== 'confirmed' && recipe !== 'open' && recipe !== 'import_fw';
    const needsAnchor = reconfirm || (NEEDS_ANCHOR.has(recipe) && !(it.anchor && it.anchor.exists && it.anchor.status === 'confirmed'));
    const isImport = recipe === 'import_fw';
    const focusLabel = isImport ? '粘贴已讨论好的框架大纲或聊天记录（AI 将自动解析分章）' : '本轮重点 / 补充说明（可不填）';
    const focusPlaceholder = isImport
      ? '在此粘贴你已有的论文大纲、章节安排或与导师/AI 的讨论记录…'
      : it.level === 'framework_layer'
        ? '例如：倾向于分成 5 章或 6 章，重点聚焦动态非理想解调…'
        : '例如：先把 2.2 节的推导补完整，或者重点讨论方案二的可行性…';
    return html`<section class="panel next" aria-labelledby="next-h">
      <div class="panel-head"><h2 id="next-h">下一轮 AI 协作操作</h2><span class="muted small">按线性步骤执行</span></div>
      ${P.linearStepHint(it, recipe)}
      <form data-form="start">
        <label class="field"><span>当前执行动作</span><select name="recipe" data-change="recipe">${recipes.map(r => html`<option value="${r.key}" ${r.key === recipe ? raw('selected') : ''}>${r.label}</option>`)}</select></label>
        ${recipe === 'advance' ? html`<fieldset class="field radios"><legend>模式</legend>
          <label><input type="radio" name="mode" value="auto" data-change="mode" ${d.mode !== 'learn' ? raw('checked') : ''}> 自动</label>
          <label><input type="radio" name="mode" value="learn" data-change="mode" ${d.mode === 'learn' ? raw('checked') : ''}> 学习（每步停下等你）</label></fieldset>` : ''}
        ${P.MULTI.has(recipe) ? html`<label class="field inline"><span>连跑</span><select name="rounds" data-change="rounds">${[1, 2, 3, 4, 5].map(n => html`<option value="${n}" ${n === rounds ? raw('selected') : ''}>${n} 轮${n === (def || 2) && !d.rounds ? '（默认）' : ''}</option>`)}</select></label>` : ''}
        <label class="field"><span>${focusLabel}</span><textarea name="focus" rows="${isImport ? 6 : 3}" data-keep="focus:${it.ref}" placeholder="${focusPlaceholder}">${d.focus || ''}</textarea></label>
        ${P.roleSummary(recipe)}
        ${needsAnchor ? html`<p class="hint warn">${reconfirm
          ? '锚点有改动，检查后点“确认锚点”。'
          : '推进正式推导前，请先选上方“① 提出问题（起草/收敛锚点）”，确认本小问题的判据后点“确认锚定”。'}</p>` : ''}
        <button type="submit" class="primary wide">${isImport ? '解析并导入框架' : '开始执行本步讨论'}</button>
      </form>
      ${it.level === 'question' ? html`<div class="next-adopt-bar">
        <span class="muted small">第 4 步：结论整理就绪后</span>
        <button type="button" class="btn-small ${it.can_adopt ? 'primary' : 'quiet'}" data-action="scroll" data-key="step-4">④ 查看/采纳结论 ↓</button>
      </div>` : ''}
    </section>`;
  };

  P.stepTime = function (st) {
    if (st.status === 'running' && st.started_at) {
      const t = Date.parse(st.started_at);
      return Number.isNaN(t) ? '' : NB.fmtDur(Math.max(0, (Date.now() - t) / 1000));
    }
    return st.duration_s ? NB.fmtDur(st.duration_s) : '';
  };

  P.sessionBox = function (s) {
    const steps = s.steps || [];
    const paused = s.status === 'paused';
    return html`<section class="panel session" aria-labelledby="sess-h">
      <div class="panel-head"><h2 id="sess-h">${s.recipe_label || s.recipe}</h2><span class="tag ${paused ? 'tone-warn' : 'tone-info'}">${L.session[s.status] || s.status}</span></div>
      <p class="muted small">${s.mode === 'learn' ? '学习模式' : '自动模式'} · 本次第 ${s.round_index || 0} / ${s.rounds_planned || 1} 轮${s.round && s.round.no ? '（总第 ' + s.round.no + ' 轮）' : ''}${s.created_at ? ' · ' + NB.fmtTime(s.created_at) + ' 开始' : ''}</p>
      ${s.message ? html`<p class="msg" role="status">${s.message}</p>` : ''}
      ${steps.length ? html`<ol class="steps">${steps.map(st => html`<li class="step st-${st.status}">
          <span class="ico" aria-hidden="true">${L.stepIcon[st.status] || '·'}</span>
          <span class="sl"><b>${st.label}</b>${st.model_label ? html` <span class="muted small">${st.model_label}</span>` : ''}<span class="sr">（${L.step[st.status] || st.status}）</span>
            ${st.error ? html`<span class="${st.status === 'skipped' ? 'muted' : 'err'} small">${st.error}</span>` : ''}</span>
          <span class="dur small">${P.stepTime(st)}</span>
          ${st.output_path && st.status !== 'running' ? NB.V.fileLink(st.output_path, '回复', 'small') : ''}</li>`)}</ol>` : html`<p class="muted small">正在准备…</p>`}
      ${paused ? html`<p><button type="button" class="linkish" data-action="scroll" data-key="sec-learn">这一步等你决定，去看 ↓</button></p>` : ''}
      <div class="form-actions"><button type="button" class="danger" data-action="cancel-session" data-id="${s.id}">取消</button></div>
    </section>`;
  };

  P.endedBox = function (s) {
    const tone = s.status === 'done' ? 'tone-good' : s.status === 'failed' ? 'tone-bad' : 'tone-warn';
    return html`<section class="panel ended" aria-labelledby="ended-h">
      <div class="panel-head"><h2 id="ended-h">本次讨论${L.session[s.status] || s.status}</h2><span class="tag ${tone}">${s.recipe_label || s.recipe || ''}</span></div>
      ${s.stop_reason ? html`<p>${s.stop_reason}</p>` : s.message ? html`<p>${s.message}</p>` : ''}
      <p class="muted small">简报和每一步的回复都在下面的“历史轮次”里。</p>
      <div class="form-actions"><button type="button" class="quiet" data-action="dismiss-ended">知道了</button></div>
    </section>`;
  };

  P.otherNotice = s => html`<section class="panel" aria-labelledby="other-h"><h2 id="other-h">下一轮</h2>
    <p>正在讨论另一项：<a href="${NB.refHash(s.ref, s.fw)}">${P.titleOf(s.ref)}</a>（${s.recipe_label || s.recipe}，${L.session[s.status] || s.status}）。</p>
    <p class="muted small">同一时间只进行一次讨论。它结束后这里会恢复“开始”按钮。</p></section>`;

  P.pending = function (it) {
    const list = (it.pending || []).filter(p => !(p.kind === 'candidates' && it.level !== 'framework_layer'));
    const answered = it.answered_decisions || [];
    const key = 'answered:' + it.ref;
    return html`<section class="panel pending" id="sec-pending" aria-labelledby="pend-h">
      <div class="panel-head"><h2 id="pend-h">待你决定${list.length ? html` <span class="count">${list.length}</span>` : ''}</h2></div>
      ${list.length ? html`<ul class="plain pend-list">${list.map(p => P.pendingRow(it, p))}</ul>` : html`<p class="muted small">现在没有需要你处理的事。</p>`}
      ${answered.length ? html`<button type="button" class="linkish small" data-action="toggle" data-key="${key}" aria-expanded="${S.open.has(key)}">已回答 ${answered.length} 项</button>
        ${S.open.has(key) ? html`<ul class="plain small">${answered.map(d => html`<li><b>${d.id}</b> ${d.text}<br><span class="muted">你的回答：${d.answer || '（无）'}</span></li>`)}</ul>` : ''}` : ''}
    </section>`;
  };

  P.pendingRow = function (it, p) {
    if (p.kind === 'decision') {
      // A question from an AI or from the system (for example drift): it needs an answer, not approve/reject.
      const options = p.options || [];
      return html`<li class="pend pend-decision">
        <div class="pend-text"><span class="tag tone-warn">${KIND.decision}${p.id ? ' ' + p.id : ''}</span> ${NB.mdInline(p.text)}</div>
        <form class="stack-form" data-form="answer" data-id="${p.id}" data-keep="answer:${it.ref}:${p.id}">
          <label><span class="sr">你的回答</span><textarea name="note" rows="2" ${p.needs_answer && !options.length ? raw('required') : ''} placeholder="${options.length ? '补充说明（可不填）' : '你的回答'}"></textarea></label>
          <div class="form-actions">${options.length
            ? options.map(o => html`<button type="submit" name="option" value="${o}" class="btn-small">${o}</button>`)
            : html`<button type="submit" class="primary btn-small">提交回答</button>`}</div></form></li>`;
    }
    if (p.kind === 'anchor_draft') {
      return html`<li class="pend pend-anchor-draft">
        <div class="pend-text"><span class="tag tone-warn">锚点草稿</span> ${p.text}</div>
        ${p.draft ? html`<div class="clip clip-short small">${NB.md(p.draft, '')}</div>` : ''}
        <div class="form-actions">${p.path ? NB.V.fileLink(p.path, '打开草稿', 'btn btn-small quiet') : ''}
          <button type="button" class="btn-small" data-action="apply-anchor-draft" data-path="${p.path || ''}">用草稿替换锚点</button>
          <button type="button" class="btn-small quiet" data-action="dismiss-anchor-draft">忽略</button></div></li>`;
    }
    const decidable = p.kind === 'request' || p.kind === 'assumption';
    return html`<li class="pend pend-${p.kind}">
      <div class="pend-text"><span class="tag ${decidable ? 'tone-warn' : ''}">${KIND[p.kind] || p.kind}${decidable && p.id ? ' ' + p.id : ''}</span> ${NB.mdInline(p.text)}</div>
      ${decidable ? html`<form class="inline-form" data-form="decide" data-kind="${p.kind}" data-id="${p.id}" data-keep="decide:${it.ref}:${p.kind}:${p.id}">
          <label class="grow"><span class="sr">说明</span><input name="note" placeholder="说明（可不填）"></label>
          <button type="submit" name="approve" value="1" class="primary btn-small">批准</button>
          <button type="submit" name="approve" value="0" class="btn-small">拒绝</button></form>`
        : JUMP[p.kind] ? html`<button type="button" class="linkish small" data-action="scroll" data-key="${JUMP[p.kind]}">去处理 ↓</button>` : ''}
    </li>`;
  };

  P.brief = function (it) {
    if (!(it.latest_brief || '').trim()) return '';
    const r = (it.rounds || []).find(x => x.brief_path);
    const key = 'brief:' + it.ref;
    const open = S.open.has(key);
    return html`<section class="panel brief" aria-labelledby="brief-h">
      <div class="panel-head"><h2 id="brief-h">最近简报</h2>${r ? html`<span class="muted small">${r.title}</span>` : ''}</div>
      <div class="${open ? '' : 'clip clip-short'}">${NB.md(it.latest_brief, r ? NB.dirname(r.brief_path) : '')}</div>
      <div class="form-actions"><button type="button" class="quiet" data-action="toggle" data-key="${key}" aria-expanded="${open}">${open ? '收起' : '展开全部'}</button>
        ${r ? NB.V.fileLink(r.brief_path, '在查看器中打开', 'btn quiet') : ''}</div>
    </section>`;
  };

  const roundTone = s => (s === 'done' ? 'tone-good' : s === 'failed' ? 'tone-bad' : s === 'running' || s === 'paused' ? 'tone-info' : 'tone-muted');
  const fmtRoundFile = name => {
    const m = String(name || '').match(/^(\d+)_([^_]+)_([^.]+)\.md$/);
    return m ? `${m[1]} ${m[2]} (${m[3]})` : name;
  };
  P.rounds = function (it) {
    const list = it.rounds || [];
    if (!list.length) return '';
    return html`<section class="panel rounds" aria-labelledby="rounds-h">
      <div class="panel-head"><h2 id="rounds-h">历史轮次</h2></div>
      <ul class="plain round-list">${list.map(r => {
        const key = 'round:' + r.id;
        const open = S.open.has(key);
        const stepFiles = (r.files || []).filter(f => f.path !== r.brief_path && f.name !== 'round.json');
        return html`<li class="round">
          <button type="button" class="round-btn" data-action="toggle" data-key="${key}" aria-expanded="${open}">
            <span class="rt">${r.title}</span>${r.ref && r.ref !== it.ref ? html`<span class="tag">${r.ref}</span>` : ''}
            <span class="tag ${roundTone(r.status)}">${L.round[r.status] || r.status || '—'}</span><span class="muted small">${NB.fmtTime(r.started_at)}</span></button>
          ${open ? html`${r.stop_reason ? html`<p class="small muted">${r.stop_reason}</p>` : ''}
            <div class="round-files">${r.brief_path ? NB.V.fileLink(r.brief_path, '📄 简报', 'btn btn-small primary') : html`<span class="muted small">这一轮没有简报。</span>`}
            ${r.dir ? html`<button type="button" class="btn-small quiet" data-action="open-folder" data-path="${r.dir}">打开文件夹</button>` : ''}
            ${stepFiles.map(f => NB.V.fileLink(f.path, fmtRoundFile(f.name), 'file-chip'))}</div>` : ''}
        </li>`;
      })}</ul>
    </section>`;
  };
}());
