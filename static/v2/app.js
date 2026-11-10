/* 新版页面：路由、读取数据、轮询讨论进度、执行操作、统一处理事件。 */
(function () {
  'use strict';
  const NB = window.NB;
  const { S, V, P, M, html } = NB;
  const A = NB.A = {};
  let sessionTimer = 0;
  let gitTimer = 0;
  let itemSeq = 0;
  let started = false;

  // ---- routing: #/FW, #/F01, #/C04?fw=F01, #/C04-Q01?fw=F01 ----
  function parseHash() {
    const h = location.hash || '';
    if (!h.startsWith('#/')) return null;
    const cut = h.indexOf('?');
    let ref = cut >= 0 ? h.slice(2, cut) : h.slice(2);
    try { ref = decodeURIComponent(ref); } catch (e) { return null; }
    if (!NB.REF_RE.test(ref)) return null;
    const fw = new URLSearchParams(cut >= 0 ? h.slice(cut + 1) : '').get('fw') || '';
    return { ref, fw: /^F\d{2,}$/.test(fw) ? fw : '' };
  }

  A.go = (ref, fw) => {
    const h = NB.refHash(ref, fw);
    if (location.hash === h) A.onRoute(); else location.hash = h;
  };

  function resolveFw() {
    const fws = (S.tree && S.tree.frameworks) || [];
    if (S.fw && fws.some(f => f.id === S.fw)) return;
    const saved = NB.store.get('fw');
    const pick = fws.find(f => f.id === saved) || fws.find(f => f.status === 'active') || fws[0];
    S.fw = pick ? pick.id : '';
  }

  // The framework a chapter or question is shown under (affects chapter numbers and the breadcrumb).
  function fwFor(ref) {
    if (S.route.fw) return S.route.fw;
    if (!/^C\d/.test(ref)) return '';
    const cid = ref.split('-')[0];
    const fws = (S.tree && S.tree.frameworks) || [];
    const has = f => (f.chapters || []).some(c => c.id === cid);
    const own = fws.find(f => f.id === S.fw && has(f)) || fws.find(has);
    return own ? own.id : '';
  }

  A.onRoute = async function () {
    const r = parseHash();
    if (!r) {
      history.replaceState(null, '', started ? NB.refHash(S.route.ref, S.route.fw) : '#/FW');
      if (started) return;
      return A.onRoute();
    }
    const changed = r.ref !== S.route.ref || r.fw !== S.route.fw || !S.item;
    if (changed && S.edit && S.edit.dirty && started) {
      if (!window.confirm('有未保存的修改，离开会丢失。仍然离开吗？')) {
        history.replaceState(null, '', NB.refHash(S.route.ref, S.route.fw));
        return;
      }
    }
    started = true;
    S.route = r;
    if (/^F\d/.test(r.ref)) S.fw = r.ref;
    else if (r.fw) S.fw = r.fw;
    NB.store.set('last', location.hash);
    if (S.fw) NB.store.set('fw', S.fw);
    if (changed) {
      S.edit = null;
      S.ledgerMenu = '';
      S.ledgerFilter = 'all';
    }
    toggleTree(false);
    V.header();
    V.tree();
    await A.loadItem(changed);
    if (changed) window.scrollTo(0, 0);
  };

  // ---- loading ----
  A.loadItem = async function (fresh) {
    const seq = ++itemSeq;
    const ref = S.route.ref;
    if (fresh) {
      S.item = null;
      S.itemError = '';
      V.main();
      P.render();
    }
    try {
      const it = await NB.get('/api/v2/item?ref=' + encodeURIComponent(ref) + '&fw=' + encodeURIComponent(fwFor(ref)));
      if (seq === itemSeq) A.applyItem(it);
    } catch (e) {
      if (seq !== itemSeq) return;
      if (fresh || !S.item) {
        S.item = null;
        S.itemError = e.message;
        V.main();
        P.render();
      } else NB.toast(e.message, 'error');
    }
  };

  A.applyItem = function (it) {
    if (!it || it.ref !== S.route.ref) return;
    S.item = it;
    S.itemError = '';
    const project = S.tree && S.tree.project && S.tree.project.title;
    document.title = it.title + (project ? ' · ' + project : '');
    if (it.session && NB.ACTIVE.has(it.session.status)) A.follow(it.session);
    V.main();
    P.render();
  };

  A.refreshTree = async function () {
    try {
      const t = await NB.get('/api/v2/tree');
      S.tree = t;
      resolveFw();
      if (t.session && NB.ACTIVE.has(t.session.status)) A.follow(t.session);
      V.header();
      V.tree();
      if (S.item) P.render();
    } catch (e) {
      const text = e.status === 404 ? '没有找到系统接口（/api/v2），请更新并重启科研文件管理系统。' : e.message;
      if (!S.tree) {
        document.getElementById('tree').innerHTML = html`<div class="notice notice-bad">${text}</div>`.s;
      } else NB.toast(text, 'error');
    }
  };

  A.reloadAll = () => Promise.all([A.refreshTree(), A.loadItem(false)]);

  A.refreshUsage = async function () {
    try {
      S.usage = await NB.get('/api/v2/usage');
      V.quotaChips();
    } catch (e) { /* keep the chips as they were */ }
  };

  // Git status changes a few seconds after a save (commits are coalesced, then pushed).
  A.gitSoon = function () {
    clearTimeout(gitTimer);
    gitTimer = setTimeout(async () => {
      try {
        const g = await NB.get('/api/v2/git');
        if (S.tree && g) { S.tree.git = g; V.gitChip(); }
      } catch (e) { /* ignore */ }
    }, 6000);
  };

  // ---- following a session: poll every 2 s while queued / running / paused ----
  A.follow = function (snap) {
    if (!snap || !snap.id) return;
    if (S.ended && S.ended.id === snap.id) return;
    S.session = snap;
    if (NB.ACTIVE.has(snap.status) && !sessionTimer) sessionTimer = setTimeout(poll, 2000);
  };

  const learnSig = s => (s ? s.status + '|' + (s.learn ? (s.learn.staged || []).length + ':' + (s.learn.explain || '').length : '') : '');

  async function poll() {
    sessionTimer = 0;
    const s = S.session;
    if (!s || !NB.ACTIVE.has(s.status)) return;
    let snap;
    try {
      snap = await NB.get('/api/v2/session?id=' + encodeURIComponent(s.id));
    } catch (e) {
      if (e.status === 404) {
        S.session = null;
        await A.reloadAll();
        return;
      }
      sessionTimer = setTimeout(poll, 4000);
      return;
    }
    if (!snap || S.session !== s) return;
    S.session = snap;
    if (NB.ACTIVE.has(snap.status)) {
      P.render();
      if (snap.ref === S.route.ref && learnSig(snap) !== learnSig(s)) V.main();
      // Entering or leaving a learning pause changes 待你决定 and the tree badge.
      const pauseChanged = (snap.status === 'paused') !== (s.status === 'paused');
      if ((snap.round_index !== s.round_index && s.round_index) || pauseChanged) A.reloadAll();
      sessionTimer = setTimeout(poll, 2000);
      return;
    }
    S.ended = snap;
    S.session = null;
    if (S.tree) S.tree.session = null;
    const label = NB.L.session[snap.status] || snap.status;
    NB.toast('讨论' + label + (snap.stop_reason ? '：' + snap.stop_reason : ''), snap.status === 'failed' ? 'error' : '');
    await A.reloadAll();
    A.gitSoon();
  }

  // ---- running an action: POST, apply the returned object, refresh the tree ----
  A.act = async function (action, payload, opts) {
    opts = opts || {};
    if (S.busy) return null;
    S.busy = true;
    document.body.classList.add('busy');
    try {
      const r = await NB.post(action, payload);
      if (r && opts.apply !== false) {
        if (r.level && r.ref) A.applyItem(r);
        else if (r.item && r.item.level) A.applyItem(r.item);
      }
      if (r && r.tree) {
        S.tree = r.tree;
        resolveFw();
        V.header();
        V.tree();
      } else if (r) A.refreshTree();
      if (r) A.gitSoon();
      return r;
    } catch (e) {
      if (opts.onError) opts.onError(e);
      else NB.toast(e.message, 'error');
      return null;
    } finally {
      S.busy = false;
      document.body.classList.remove('busy');
    }
  };

  const fwOf = () => (S.item && S.item.fw) || '';

  // Saving an editor: on 409 the text stays in the editor and the page reloads the newest revision.
  async function saveEdit(action, payload, okText) {
    const e = S.edit;
    e.saving = true;
    try {
      const r = await NB.post(action, Object.assign({ revision: e.revision }, payload));
      if (!r) return;
      S.edit = null;
      A.applyItem(r);
      NB.toast(okText);
      A.refreshTree();
      A.gitSoon();
    } catch (err) {
      e.error = err.message;
      e.conflict = err.status === 409;
      V.main();
      if (e.conflict) {
        await A.loadItem(false);
        if (e.kind === 'anchor') {
          const added = A.mergeAnchorRows();
          if (added) NB.toast('讨论期间新增了 ' + added + ' 条假设，已标为“新”加入编辑框，请检查后再保存');
        }
      }
    } finally {
      e.saving = false;
    }
  }

  A.saveDoc = function () {
    const e = S.edit;
    if (!e || e.kind !== 'doc' || e.saving) return;
    const ta = document.querySelector('[data-keep="doc:' + CSS.escape(e.ref) + '"] textarea');
    if (ta) saveEdit('v2_save_doc', { ref: e.ref, fw: fwOf(), text: ta.value }, '已保存');
  };

  A.saveFields = function () {
    const e = S.edit;
    const form = document.querySelector('form[data-form="fields"]');
    if (!e || e.kind !== 'fields' || e.saving || !form) return;
    const fields = { mainline: form.elements.mainline.value, chapters: form.elements.chapters.value, risks: form.elements.risks.value };
    const payload = { ref: e.ref, fw: '', fields };
    const title = form.elements.title.value.trim();
    if (title && S.item && title !== S.item.title) payload.title = title;
    saveEdit('v2_save_doc', payload, '框架已保存');
  };

  A.saveAnchor = function (form) {
    const e = S.edit;
    if (!e || e.kind !== 'anchor' || e.saving || !form) return;
    const slots = {};
    form.querySelectorAll('textarea[name^="slot:"]').forEach(t => { slots[t.name.slice(5)] = t.value; });
    const assumptions = Array.from(form.querySelectorAll('.assume-row')).map(row => {
      const one = { text: row.querySelector('[name="a-text"]').value.trim(), basis: row.querySelector('[name="a-basis"]').value.trim(),
        failure: row.querySelector('[name="a-failure"]').value.trim() };
      if (row.dataset.id) one.id = row.dataset.id;
      return one;
    }).filter(a => a.text);
    // Only rows the user removed are rejected; assumptions added meanwhile by a round stay untouched.
    const deleted = Array.from(e.deleted || []);
    saveEdit('v2_save_anchor', { ref: e.ref, fw: fwOf(), slots, assumptions, deleted }, '锚点已保存');
  };

  // After a 409 on the anchor editor, merge assumptions that appeared meanwhile into the open form (marked 新).
  A.mergeAnchorRows = function () {
    const e = S.edit;
    const form = document.querySelector('form[data-form="anchor"]');
    if (!e || e.kind !== 'anchor' || !form || !S.item || !S.item.anchor) return 0;
    const shown = new Set(Array.from(form.querySelectorAll('.assume-row')).map(r => r.dataset.id).filter(Boolean));
    const gone = e.deleted || new Set();
    const fresh = (S.item.anchor.assumptions || []).filter(a => a.status !== '已拒绝' && !shown.has(a.id) && !gone.has(a.id));
    const list = form.querySelector('.assume-rows') || form;
    fresh.forEach(a => {
      const holder = document.createElement('div');
      holder.innerHTML = String(V.assumptionRow(a));
      const row = holder.firstElementChild;
      row.classList.add('is-new');
      row.insertAdjacentHTML('afterbegin', String(NB.html`<span class="tag tone-info">新 ${a.id}</span>`));
      list.appendChild(row);
    });
    return fresh.length;
  };

  A.forceSave = function () {
    const e = S.edit;
    const it = S.item;
    if (!e || !it) return;
    e.revision = e.kind === 'anchor' ? it.anchor.revision : it.doc.revision;
    if (e.kind === 'doc') A.saveDoc();
    else if (e.kind === 'fields') A.saveFields();
    else A.saveAnchor(document.querySelector('form[data-form="anchor"]'));
  };

  A.confirmAnchor = async function () {
    const it = S.item;
    const a = it.anchor;
    const waiting = (a.assumptions || []).filter(x => x.status === '待批').length;
    if (waiting && !(await M.confirm('确认锚点', '确认后，' + waiting + ' 条待批假设也会一并确认。继续吗？', '确认锚点'))) return;
    const r = await A.act('v2_confirm_anchor', { ref: it.ref, fw: fwOf(), revision: a.revision });
    if (r) NB.toast('锚点已确认');
  };

  A.start = async function (form) {
    const it = S.item;
    const d = P.draft(it);
    const recipe = form.elements.recipe.value;
    const payload = { ref: it.ref, fw: fwOf(), recipe, mode: recipe === 'advance' && d.mode === 'learn' ? 'learn' : 'auto',
      focus: (form.elements.focus.value || '').trim(), request_id: NB.uid() };
    if (P.MULTI.has(recipe)) {
      const n = d.rounds || P.roundsDefault();
      if (n) payload.rounds = n;
    }
    const r = await A.act('v2_start', payload, { apply: false });
    if (r && r.id) {
      S.ended = null;
      d.focus = '';
      A.follow(r);
      P.render();
      V.tree();
      NB.toast('已开始：' + (r.recipe_label || recipe));
    }
  };

  A.cancelSession = async function (id) {
    if (!(await M.confirm('取消讨论', '已完成的步骤和文件都会保留。确定取消这次讨论吗？', '取消讨论', true))) return;
    const r = await A.act('v2_cancel', { session_id: id }, { apply: false });
    if (r && r.id) { A.follow(r); P.render(); }
  };

  A.learn = async function (decision) {
    const s = P.activeSession();
    if (!s) return;
    let note = '';
    if (decision === 'ask') {
      const v = await M.ask({ title: '追问', fields: [{ name: 'note', label: '想问什么', type: 'textarea', rows: 4, placeholder: '例如：式(5)的第二项是怎么来的？' }], ok: '追问' });
      if (!v) return;
      note = v.note.trim();
    }
    if (decision === 'reject') {
      const v = await M.ask({ title: '打回这一步', intro: '理由会带进下一轮，作者会先处理它。', ok: '打回', danger: true,
        fields: [{ name: 'note', label: '打回的理由', type: 'textarea', rows: 4, required: true }] });
      if (!v) return;
      note = v.note.trim();
    }
    const r = await A.act('v2_learn', { session_id: s.id, decision, note }, { apply: false });
    if (r && r.id) {
      A.follow(r);
      P.render();
      V.main();
    }
  };

  A.decide = async function (form, submitter) {
    const approve = !submitter || submitter.value === '1';
    const note = form.elements.note ? form.elements.note.value.trim() : '';
    const r = await A.act('v2_decide', { ref: S.item.ref, fw: fwOf(), kind: form.dataset.kind, id: form.dataset.id, approve, note });
    if (r) NB.toast((approve ? '已批准 ' : '已拒绝 ') + form.dataset.id);
  };

  // Answering a question from the AI or the system: the answer is passed to the next round's prompts.
  A.answer = async function (form, submitter) {
    const option = submitter && submitter.name === 'option' ? submitter.value : '';
    const text = form.elements.note ? form.elements.note.value.trim() : '';
    const note = option ? option + (text ? '：' + text : '') : text;
    if (!note) { NB.toast('请写下你的回答', 'error'); return; }
    const r = await A.act('v2_decide', { ref: S.item.ref, fw: fwOf(), kind: 'decision', id: form.dataset.id, approve: true, note });
    if (r) NB.toast('已回答 ' + form.dataset.id + '，下一轮会照此执行');
  };

  A.applyAnchorDraft = async function (path) {
    const it = S.item;
    if (!(await M.confirm('用草稿替换锚点', '草稿里的问题、判据、范围会替换现有内容；你的假设保留，草稿里新的假设记为待批。替换后需要你再确认一次锚点。继续吗？', '替换'))) return;
    const r = await A.act('v2_apply_anchor_draft', { ref: it.ref, fw: fwOf(), path, revision: it.anchor.revision });
    if (r) NB.toast('已用草稿替换，请检查后确认锚点');
  };

  A.setEntry = async function (form, submitter) {
    const state = submitter && submitter.value;
    if (!state) return;
    const id = form.dataset.id;
    const r = await A.act('v2_set_entry', { ref: S.item.ref, fw: fwOf(), id, state, note: form.elements.note.value.trim() }, { apply: false });
    if (r) {
      S.ledgerMenu = '';
      A.applyItem(r);
      NB.toast(id + ' 已设为' + state);
    }
  };

  A.newFramework = async function () {
    const r = await M.ask({
      title: '新建框架', ok: '建立',
      intro: '会建立 7 章骨架：绪论、理论基础、三个研究问题、随框架而定的一章、总结与展望。',
      fields: [{ name: 'title', label: '框架名称', required: true, placeholder: '例如：模型驱动的三维重建' }],
      submit: v => NB.post('v2_create_framework', { title: v.title.trim() }),
    });
    if (r && r.id) A.afterCreate(r, '已建立框架');
  };

  A.afterCreate = function (r, text) {
    S.tree = r.tree;
    resolveFw();
    V.header();
    V.tree();
    NB.toast(text + ' ' + r.id);
    A.gitSoon();
    A.go(r.id);
  };

  A.newQuestion = async function (ref, fw) {
    const r = await M.ask({
      title: '新建小问题', ok: '建立',
      intro: '在“' + P.titleOf(ref) + '”下建立一个小问题。建立后可以在右侧让 AI 起草锚点。',
      fields: [{ name: 'title', label: '小问题', required: true, placeholder: '用一句话写出要回答的问题' }],
      submit: v => NB.post('v2_new_question', { ref, fw, title: v.title.trim() }),
    });
    if (!r) return;
    A.refreshTree();
    A.gitSoon();
    if (r.created && r.created.length) A.go(r.created[0], fw);
    else if (r.item) A.applyItem(r.item);
  };

  A.editChapter = async function () {
    const it = S.item;
    await M.ask({
      title: '章名与角色', ok: '保存',
      fields: [{ name: 'title', label: '章名', value: it.title, required: true },
        { name: 'role', label: '这一章的角色', type: 'select', value: it.chapter ? it.chapter.role : 'other', options: Object.entries(NB.L.chapterRole) }],
      submit: async v => {
        const r = await NB.post('v2_set_chapter', { ref: it.ref, fw: fwOf(), title: v.title.trim(), role: v.role });
        if (r) { A.applyItem(r); A.refreshTree(); A.gitSoon(); }
        return r;
      },
    });
  };

  A.createFromCandidate = async function (index) {
    const c = (S.item.candidates || []).find(x => Number(x.index) === index);
    const r = await M.ask({
      title: '建立为框架', ok: '建立',
      intro: '按这个候选的章节建立框架、各章和其中的小问题。小问题的锚点是草稿，需要你逐个确认。',
      fields: [{ name: 'title', label: '框架名称', value: c ? c.title : '', required: true }],
      submit: v => NB.post('v2_create_from_candidate', { index, title: v.title.trim() }),
    });
    if (r && r.id) A.afterCreate(r, '已建立框架');
  };

  A.createQuestions = async function () {
    const it = S.item;
    const list = it.proposed_questions || [];
    const group = 'pq:' + it.ref;
    const set = NB.picks(group, list.map((_, i) => String(i)));
    const items = list.filter((_, i) => set.has(String(i))).map(q => ({ title: q.title, question: q.question || '', criterion: q.criterion || '' }));
    if (!items.length) { NB.toast('请至少勾选一个小问题', 'error'); return; }
    const r = await A.act('v2_create_questions', { ref: it.ref, fw: fwOf(), items }, { apply: false });
    if (r) {
      delete S.picks[group];
      A.applyItem(r.item);
      NB.toast('已建立 ' + (r.created || []).length + ' 个小问题');
    }
  };

  A.nextStage = async function () {
    const st = S.item.stage;
    if (!st) return;
    const list = st.stages || [];
    const next = list[list.findIndex(s => s.key === st.current) + 1];
    if (!next) return;
    if (!st.done && !(await M.confirm('进入下一阶段', st.current + ' 还没有运行过。确定直接进入 ' + next.key + ' ' + next.label + ' 吗？', '进入下一阶段'))) return;
    const r = await A.act('v2_next_stage', { ref: S.item.ref, fw: fwOf() });
    if (r) NB.toast('已进入 ' + next.key + ' ' + next.label);
  };

  A.pickIdeas = async function () {
    const it = S.item;
    const ideas = ((it.ledger && it.ledger.entries) || []).filter(e => e.type === '想法' && e.state === '待定');
    const group = 'ideas:' + it.ref;
    const set = NB.picks(group);
    const keep = ideas.filter(e => set.has(e.id)).map(e => e.id);
    const drop = ideas.length - keep.length;
    const text = keep.length ? '保留 ' + keep.length + ' 个想法；其余 ' + drop + ' 个待定想法会标为否定，进入淘汰清单。继续吗？'
      : '没有勾选任何想法：全部 ' + drop + ' 个待定想法都会标为否定。继续吗？';
    if (!(await M.confirm('保留选中的想法', text, '确定', !keep.length))) return;
    const r = await A.act('v2_pick_ideas', { ref: it.ref, fw: fwOf(), keep });
    if (r) { delete S.picks[group]; NB.toast('已保留 ' + keep.length + ' 个想法'); }
  };

  A.verifyRefs = async function () {
    const it = S.item;
    const group = 'refs:' + it.ref;
    const entries = (it.ledger && it.ledger.entries) || [];
    const ids = Array.from(NB.picks(group)).filter(id => entries.some(e => e.id === id && e.type === '文献' && e.state === '待定'));
    if (!ids.length) { NB.toast('请先勾选已经核对过的文献', 'error'); return; }
    const r = await A.act('v2_verify_refs', { ref: it.ref, fw: fwOf(), ids });
    if (r) { delete S.picks[group]; NB.toast('已核实 ' + ids.length + ' 条文献'); }
  };

  A.dismiss = async function (kind) {
    const text = { candidates: '忽略全部候选框架吗？需要时可以再让 AI 提出。', questions: '忽略 AI 拆出的这些小问题吗？', conclusion: '忽略 AI 建议的结论吗？' }[kind];
    if (!(await M.confirm('忽略', text || '忽略这一项吗？', '忽略'))) return;
    const r = await A.act('v2_dismiss', { ref: kind === 'candidates' ? 'FW' : S.item.ref, fw: fwOf(), kind });
    if (r) NB.toast('已忽略');
  };

  A.setActive = async function (ref) {
    const r = await A.act('v2_set_active', { ref });
    if (r) {
      NB.toast('已设为主推框架');
      A.loadItem(false);
    }
  };

  A.gitPush = async function () {
    const r = await A.act('v2_git_push', {}, { apply: false });
    if (r && S.tree) {
      S.tree.git = r;
      V.gitChip();
      const line = document.querySelector('#settings [data-git-line]');
      if (line) line.innerHTML = V.gitStatusLine().s;
      NB.toast('已开始推送，结果稍后显示在页首');
    }
  };

  A.gitRemote = async function () {
    const dlg = document.getElementById('settings');
    const url = dlg.querySelector('[name="git:url"]').value.trim();
    const branch = dlg.querySelector('[name="git:branch"]').value.trim();
    const r = await A.act('v2_git_remote', { url, branch }, { apply: false });
    if (r && S.tree) {
      S.tree.git = r.git;
      V.gitChip();
      NB.toast((r.result && r.result.message) || '远程与分支已更新');
    }
  };

  A.pickFramework = function (fw) {
    if (!fw) return;
    S.fw = fw;
    NB.store.set('fw', fw);
    const ref = S.route.ref;
    if (/^F\d/.test(ref)) return A.go(fw);
    if (/^C\d/.test(ref)) {
      const cid = ref.split('-')[0];
      const f = ((S.tree && S.tree.frameworks) || []).find(x => x.id === fw);
      return f && (f.chapters || []).some(c => c.id === cid) ? A.go(ref, fw) : A.go(fw);
    }
    V.header();
    V.tree();
  };

  // ---- small UI helpers ----
  function toggleTree(force) {
    const open = typeof force === 'boolean' ? force : !document.body.classList.contains('tree-open');
    document.body.classList.toggle('tree-open', open);
    const btn = document.querySelector('[data-action="toggle-tree"]');
    if (btn) btn.setAttribute('aria-expanded', String(open));
    const shade = document.querySelector('.backdrop');
    if (shade) shade.hidden = !open;
    if (open) {
      const cur = document.querySelector('#tree .current') || document.querySelector('#tree a');
      if (cur) cur.focus();
    }
  }

  function scrollToSection(id) {
    const el = document.getElementById(id);
    if (!el) { NB.toast('这一部分不在当前页面上'); return; }
    el.scrollIntoView({ behavior: 'smooth', block: 'start' });
    el.classList.add('flash');
    setTimeout(() => el.classList.remove('flash'), 1600);
  }

  async function startEdit(kind) {
    const it = S.item;
    if (!it) return;
    if (S.edit && S.edit.dirty && !(await M.confirm('放弃修改', '另一个编辑框里的修改还没保存，确定放弃吗？', '放弃修改', true))) return;
    const revision = kind === 'anchor' ? it.anchor.revision : it.doc.revision;
    S.edit = { kind, ref: it.ref, revision, text: kind === 'doc' ? it.doc.text || '' : '', error: '', conflict: false, dirty: false };
    V.main();
    const box = document.querySelector('#main [data-keep]');
    const field = box && box.querySelector('textarea, input');
    if (field) field.focus();
  }

  async function cancelEdit() {
    if (S.edit && S.edit.dirty && !(await M.confirm('放弃修改', '编辑框里的修改还没保存，确定放弃吗？', '放弃修改', true))) return;
    S.edit = null;
    V.main();
  }

  function toggleOpen(el) {
    const key = el.dataset.key;
    if (S.open.has(key)) S.open.delete(key); else S.open.add(key);
    if (el.closest('#side')) P.render(); else V.main();
  }

  const CLICK = {
    'toggle-tree': () => toggleTree(),
    'close-tree': () => toggleTree(false),
    'open-settings': () => M.openSettings(),
    'close-dialog': el => M.close(el.dataset.key),
    'git-push': () => A.gitPush(),
    'git-remote': () => A.gitRemote(),
    'new-framework': () => A.newFramework(),
    'new-question': el => A.newQuestion(el.dataset.ref, el.dataset.fw || ''),
    'edit-chapter': () => A.editChapter(),
    'edit-doc': () => startEdit('doc'),
    'edit-fields': () => startEdit('fields'),
    'edit-anchor': () => startEdit('anchor'),
    'cancel-edit': () => cancelEdit(),
    'save-doc': () => A.saveDoc(),
    'save-fields': () => A.saveFields(),
    'force-save': () => A.forceSave(),
    'add-assumption': el => {
      const rows = el.closest('fieldset').querySelector('.assume-rows');
      rows.insertAdjacentHTML('beforeend', V.assumptionRow({}).s);
      rows.lastElementChild.querySelector('input').focus();
      if (S.edit) S.edit.dirty = true;
    },
    'apply-anchor-draft': el => A.applyAnchorDraft(el.dataset.path),
    'dismiss-anchor-draft': () => A.act('v2_dismiss_anchor_draft', { ref: S.item.ref, fw: fwOf() }),
    'remove-assumption': el => {
      const row = el.closest('.assume-row');
      if (S.edit && row.dataset.id) (S.edit.deleted = S.edit.deleted || new Set()).add(row.dataset.id);
      row.remove();
      if (S.edit) S.edit.dirty = true;
    },
    'confirm-anchor': () => A.confirmAnchor(),
    'history': el => M.openHistory(el.dataset.path, el.dataset.revision || (S.item && S.item.doc ? S.item.doc.revision : '')),
    'set-active': el => A.setActive(el.dataset.ref),
    'dismiss': el => A.dismiss(el.dataset.kind),
    'create-from-candidate': el => A.createFromCandidate(Number(el.dataset.index)),
    'create-questions': () => A.createQuestions(),
    'next-stage': () => A.nextStage(),
    'pick-ideas': () => A.pickIdeas(),
    'verify-refs': () => A.verifyRefs(),
    'ledger-filter': el => { S.ledgerFilter = el.dataset.key; V.main(); },
    'entry-menu': el => {
      S.ledgerMenu = el.dataset.id || '';
      V.main();
      const input = S.ledgerMenu && document.querySelector('form[data-form="set-entry"] input');
      if (input) input.focus();
    },
    'toggle': el => toggleOpen(el),
    'scroll': el => scrollToSection(el.dataset.key),
    'adopt': () => M.openAdopt(S.item),
    'learn': el => A.learn(el.dataset.decision),
    'cancel-session': el => A.cancelSession(el.dataset.id),
    'dismiss-ended': () => { S.ended = null; P.render(); },
    'viewer-back': () => M.viewerBack(),
    'viewer-raw': () => M.viewerRaw(),
    'history-pick': el => M.historyPick(el.dataset.key),
    'history-restore': () => M.historyRestore(),
    'history-diff': el => (el.dataset.key ? M.historyDiff(el.dataset.key) : (M.hist.diffBase = '', M.renderHistory())),
    'adopt-use-ai': () => { const f = document.querySelector('dialog[open] textarea[name="text"]'); if (f && M.adoptSuggestion) f.value = M.adoptSuggestion.text; },
    'test-provider': el => M.testProvider(el.dataset.key),
    'open-folder': el => NB.post('open_folder', { path: el.dataset.path || '' }).then(r => r && NB.toast('已在资源管理器中打开文件夹')).catch(e => NB.toast(e.message, 'error')),
    'open-guide': () => M.openFile('快速上手示例.md'),
    'fill-starter-focus': () => {
      if (!S.item) return;
      const d = P.draft(S.item);
      const a = S.item.anchor || {};
      if (a.status === 'confirmed') {
        d.recipe = 'propose_frameworks';
        d.focus = '请围绕已确认的全篇锚点，提出 2 到 3 套逻辑递进方式不同的 7 章博士论文候选框架（第1章绪论、第2章理论基础、第3-5章三个核心研究问题、第6章系统集成或综合实验验证、第7章总结与展望），明确标出每章的承重问题与潜在风险。';
      } else {
        d.recipe = 'draft_anchor';
        d.focus = '我的博士论文研究方向是：【填写你的方向，例如：高速动态结构光三维形貌测量与非理想误差补偿】。\n核心痛点是：【填写现有方法在什么场景下失效，例如：在高速投影与高反光干扰下，传统相移法需要帧数多、易产生运动伪影与非线性相位失真】。\n核心思路是：【填写技术路线，例如：结合时空先验建模、三步相移谐波抑制与物理引导的轻量级解调算法，在不增加投影帧数的前提下实现高精度相位恢复】。\n量化目标与验证方式：【填写指标，例如：3帧条纹输入下动态相位 RMSE 降低 50% 以上，先用 Python 仿真验证数学误差上界，再在实测数据上验证】。\n暂不纳入本论文范围：【填写边界，例如：不做硬件光机镜头设计，不做多相机大视场拼接】。';
      }
      P.render();
      const box = document.querySelector('form[data-form="start"] textarea[name="focus"]');
      if (box) {
        box.rows = 7;
        box.focus();
      }
      NB.toast('已填入示例模板，请把【】里的内容改成你的课题后点右侧“开始”');
    },
  };

  document.addEventListener('click', ev => {
    const link = ev.target.closest('a[data-file]');
    if (link) {
      if (ev.button !== 0 || ev.ctrlKey || ev.metaKey || ev.shiftKey || ev.altKey) return;
      ev.preventDefault();
      M.openFile(link.dataset.file, link.dataset.hash || '');
      return;
    }
    const el = ev.target.closest('[data-action]');
    if (!el || el.disabled) return;
    const fn = CLICK[el.dataset.action];
    if (fn) {
      ev.preventDefault();
      fn(el, ev);
    }
  });

  document.addEventListener('submit', ev => {
    const form = ev.target.closest('form[data-form]');
    if (!form) return;
    ev.preventDefault();
    const kind = form.dataset.form;
    if (kind === 'anchor') A.saveAnchor(form);
    else if (kind === 'fields') A.saveFields();
    else if (kind === 'start') A.start(form);
    else if (kind === 'decide') A.decide(form, ev.submitter);
    else if (kind === 'answer') A.answer(form, ev.submitter);
    else if (kind === 'set-entry') A.setEntry(form, ev.submitter);
    else if (kind === 'settings') M.saveSettings(form);
  });

  document.addEventListener('change', ev => {
    const t = ev.target;
    if (t.id === 'fw-picker') { A.pickFramework(t.value); return; }
    if (t.dataset.pick) {
      const set = NB.picks(t.dataset.pick);
      if (t.checked) set.add(t.value); else set.delete(t.value);
      return;
    }
    const kind = t.dataset.change;
    if (!kind || !S.item) return;
    const d = P.draft(S.item);
    if (kind === 'recipe') d.recipe = t.value;
    if (kind === 'mode') d.mode = t.value;
    if (kind === 'rounds') d.rounds = Number(t.value) || 0;
    P.render();
  });

  document.addEventListener('input', ev => {
    const t = ev.target;
    if (t.name === 'focus' && S.item && t.closest('form[data-form="start"]')) P.draft(S.item).focus = t.value;
    if (S.edit && t.closest('#main [data-keep]')) S.edit.dirty = true;
  });

  document.addEventListener('keydown', ev => {
    if ((ev.ctrlKey || ev.metaKey) && (ev.key === 's' || ev.key === 'S') && S.edit && !document.querySelector('dialog[open]')) {
      ev.preventDefault();
      if (S.edit.kind === 'doc') A.saveDoc();
      else if (S.edit.kind === 'fields') A.saveFields();
      else A.saveAnchor(document.querySelector('form[data-form="anchor"]'));
      return;
    }
    if (ev.key === 'Escape' && document.body.classList.contains('tree-open') && !document.querySelector('dialog[open]')) toggleTree(false);
  });

  window.addEventListener('beforeunload', ev => {
    if (S.edit && S.edit.dirty) { ev.preventDefault(); ev.returnValue = ''; }
  });

  // A broken image in a document shows its alt text instead of an empty frame.
  document.addEventListener('error', ev => {
    const img = ev.target;
    if (!(img instanceof HTMLImageElement) || !img.classList.contains('md-img')) return;
    const note = document.createElement('span');
    note.className = 'md-noimg';
    note.textContent = '[图片无法显示：' + (img.alt || '没有说明') + ']';
    img.replaceWith(note);
  }, true);

  // The sticky tree and right panel start below the header, whatever its height.
  function trackHeader() {
    const bar = document.querySelector('.topbar');
    if (!bar || typeof ResizeObserver !== 'function') return;
    new ResizeObserver(() => document.documentElement.style.setProperty('--top-h', bar.offsetHeight + 'px')).observe(bar);
  }

  // ---- start ----
  async function init() {
    trackHeader();
    if (!parseHash()) {
      const last = NB.store.get('last');
      history.replaceState(null, '', last && last.startsWith('#/') ? last : '#/FW');
    }
    window.addEventListener('hashchange', () => { A.onRoute(); });
    await A.refreshTree();
    await A.onRoute();
    A.refreshUsage();
    setInterval(() => { if (!document.hidden) A.refreshUsage(); }, 60000);
    // Settings give the real default number of rounds; read them quietly once.
    setTimeout(async () => {
      try {
        S.settingsView = await NB.get('/api/v2/settings');
        V.quotaChips();
        if (S.item && !P.activeSession()) P.render();
      } catch (e) { /* the settings dialog shows the error when opened */ }
    }, 1500);
  }

  init();
}());
