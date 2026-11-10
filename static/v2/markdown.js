/* 新版页面的 Markdown 渲染器。所有 HTML 一律转义；数学公式原样保留给 KaTeX。
   浏览器中为 window.NBMarkdown，Node 中为 module.exports。 */
(function (root, factory) {
  'use strict';
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.NBMarkdown = api;
}(typeof window !== 'undefined' ? window : null, function () {
  'use strict';

  const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  const RESULT_TITLE = /^R\d+\.[\p{L}\p{N}_.]+$/u;
  const FENCE = /^( {0,3})(`{3,}|~{3,})(.*)$/;
  const LIST = /^( *)([-+*]|\d{1,9}[.)])(?:[ \t]+(.*))?$/;
  const HR = /^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$/;
  const HEADING = /^ {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$/;
  const TABLE_SEP = /^ *\|? *:?-+:? *(?:\| *:?-+:? *)*\|? *$/;
  const MATH_START = /^ {0,3}(\$\$|\\\[)/;
  const MATH_INLINE = /\$\$[\s\S]+?\$\$|\\\[[\s\S]+?\\\]|\\\([\s\S]+?\\\)|(?<![\\$])\$(?=[^\s$])(?:\\[\s\S]|[^$\\])*?(?:\\[^\s]|[^\s\\$])\$(?![\d$])/g;
  const URL_RE = /https?:\/\/[^\s<>"'`\u0000\u2e80-\u9fff\uff00-\uffef]+/g;

  function escapeHtml(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, ch => ESC[ch]);
  }

  // Resolve a relative link against the folder of the file being shown.
  // Returns a root-relative POSIX path, or '' when the target is not allowed.
  function resolvePath(target, base) {
    let clean = String(target || '').trim();
    if (!clean) return '';
    try { clean = decodeURIComponent(clean); } catch (e) { /* keep the raw text */ }
    clean = clean.replace(/\\/g, '/');
    if (clean.includes('\0') || /^[a-z][a-z0-9+.-]*:/i.test(clean) || clean.startsWith('/')) return '';
    const parts = [];
    for (const piece of ((base ? base + '/' : '') + clean).split('/')) {
      if (!piece || piece === '.') continue;
      if (piece === '..') {
        if (!parts.length) return '';
        parts.pop();
        continue;
      }
      parts.push(piece);
    }
    if (!parts.length || parts.some(p => p.startsWith('.'))) return '';
    return parts.join('/');
  }

  function fileUrl(path, download) {
    return '/api/file?path=' + encodeURIComponent(path) + (download ? '&download=1' : '');
  }

  function emphasis(s) {
    return s
      .replace(/\*\*\*(?=\S)([\s\S]*?\S)\*\*\*/g, '<strong><em>$1</em></strong>')
      .replace(/\*\*(?=\S)([\s\S]*?\S)\*\*/g, '<strong>$1</strong>')
      .replace(/(?<![\w])__(?=\S)([\s\S]*?\S)__(?![\w])/g, '<strong>$1</strong>')
      .replace(/(?<![*\w])\*(?=[^\s*])([\s\S]*?[^\s*])\*(?![*\w])/g, '<em>$1</em>')
      .replace(/(?<![\w])_(?=[^\s_])([\s\S]*?[^\s_])_(?![\w])/g, '<em>$1</em>')
      .replace(/~~(?=\S)([\s\S]*?\S)~~/g, '<del>$1</del>');
  }

  function formatText(s) {
    return emphasis(escapeHtml(s));
  }

  function linkHtml(label, target, title, opts) {
    const t = String(target || '').trim();
    const titleAttr = title ? ' title="' + escapeHtml(title) + '"' : '';
    if (/^https?:\/\//i.test(t)) {
      return '<a href="' + escapeHtml(t) + '" target="_blank" rel="noopener noreferrer"' + titleAttr + '>' + label + '</a>';
    }
    const cut = t.indexOf('#');
    const hash = cut >= 0 ? t.slice(cut + 1) : '';
    const path = resolvePath(cut >= 0 ? t.slice(0, cut) : t, opts.base);
    if (!path) return '<span class="md-plainlink">' + label + '</span>';
    const attrs = ' href="' + escapeHtml(fileUrl(path) + (hash ? '#' + hash : '')) + '" data-file="' + escapeHtml(path) + '"'
      + (hash ? ' data-hash="' + escapeHtml(hash) + '"' : '');
    if (title && RESULT_TITLE.test(title)) {
      return '<a class="result-link"' + attrs + titleAttr + '><span class="result-value">' + label
        + '</span><span class="result-tag">' + escapeHtml(title.split('.')[0]) + '</span></a>';
    }
    return '<a class="file-link"' + attrs + titleAttr + '>' + label + '</a>';
  }

  function imageHtml(alt, target, title, opts) {
    const t = String(target || '').trim();
    const path = /^[a-z][a-z0-9+.-]*:/i.test(t) ? '' : resolvePath(t.split('#')[0], opts.base);
    if (!path) return '<span class="md-noimg">[图片：' + escapeHtml(alt) + ']</span>';
    return '<img class="md-img" src="' + escapeHtml(fileUrl(path)) + '" alt="' + escapeHtml(alt) + '" loading="lazy"'
      + (title ? ' title="' + escapeHtml(title) + '"' : '') + '>';
  }

  // [label](dest "title") starting at s[i] === '['.
  function matchLink(s, i) {
    let depth = 0;
    let j = i;
    for (; j < s.length; j++) {
      const c = s[j];
      if (c === '[') depth++;
      else if (c === ']') {
        depth--;
        if (depth === 0) break;
      } else if (c === '\n' && s[j + 1] === '\n') return null;
    }
    if (j >= s.length || s[j + 1] !== '(') return null;
    const label = s.slice(i + 1, j);
    let k = j + 2;
    while (s[k] === ' ') k++;
    let dest = '';
    if (s[k] === '<') {
      const end = s.indexOf('>', k);
      if (end < 0) return null;
      dest = s.slice(k + 1, end);
      k = end + 1;
    } else {
      const begin = k;
      let par = 0;
      for (; k < s.length; k++) {
        const c = s[k];
        if (c === '(') par++;
        else if (c === ')') {
          if (par === 0) break;
          par--;
        } else if (/\s/.test(c)) break;
      }
      dest = s.slice(begin, k);
    }
    while (s[k] === ' ') k++;
    let title = '';
    if (s[k] === '"' || s[k] === "'") {
      const end = s.indexOf(s[k], k + 1);
      if (end < 0) return null;
      title = s.slice(k + 1, end);
      k = end + 1;
      while (s[k] === ' ') k++;
    }
    if (s[k] !== ')') return null;
    return { label, dest, title, end: k + 1 };
  }

  function parseLinks(s, opts, keep) {
    let out = '';
    let i = 0;
    while (i < s.length) {
      const image = s[i] === '!' && s[i + 1] === '[';
      if (s[i] === '[' || image) {
        const m = matchLink(s, image ? i + 1 : i);
        if (m && !m.dest.includes('\u0000') && !m.title.includes('\u0000')) {
          out += keep(image
            ? imageHtml(m.label.replace(/\u0000\d+\u0000/g, ''), m.dest, m.title, opts)
            : linkHtml(formatText(m.label), m.dest, m.title, opts));
          i = m.end;
          continue;
        }
      }
      out += s[i];
      i++;
    }
    return out;
  }

  function renderInline(text, opts) {
    opts = opts || {};
    const store = [];
    const keep = html => '\u0000' + (store.push(html) - 1) + '\u0000';
    let s = String(text == null ? '' : text).replace(/\u0000/g, '');
    s = s.replace(/(`+)([^`]|[^`][\s\S]*?[^`])\1(?!`)/g, (m, ticks, body) => {
      const b = body.length > 2 && body[0] === ' ' && body[body.length - 1] === ' ' ? body.slice(1, -1) : body;
      return keep('<code>' + escapeHtml(b) + '</code>');
    });
    s = s.replace(MATH_INLINE, m => keep(escapeHtml(m)));
    s = s.replace(/<br\s*\/?>/gi, () => keep('<br>'));
    s = s.replace(/\\([\\`*_{}[\]()#+\-.!|~<>$])/g, (m, ch) => keep(ch === '$' ? '<span class="md-dollar">$</span>' : escapeHtml(ch)));
    s = parseLinks(s, opts, keep);
    s = s.replace(URL_RE, url => {
      const trail = url.match(/[.,;:!?)\]]+$/);
      const clean = trail ? url.slice(0, -trail[0].length) : url;
      return keep('<a href="' + escapeHtml(clean) + '" target="_blank" rel="noopener noreferrer">' + escapeHtml(clean) + '</a>')
        + (trail ? trail[0] : '');
    });
    s = formatText(s).replace(/\n/g, '<br>');
    for (let n = 0; n < 8 && s.indexOf('\u0000') >= 0; n++) {
      s = s.replace(/\u0000(\d+)\u0000/g, (m, k) => store[Number(k)]);
    }
    return s;
  }

  const SYSTEM_COMMENT = /^\s*(?:\/?field:|research-record\s|generated:|anchor\s\{|ledger\s\{|verification\s\{|角色：)/;

  // Make <!-- comments --> outside code visible: nothing in a document may be hidden from the user.
  // The comment text is kept and shown (escaped) after a "隐藏注释：" label.
  function stripComments(source) {
    let open = false;
    let fence = null;
    const out = [];
    for (const line of String(source || '').split('\n')) {
      const marker = !open && line.match(/^ {0,3}(`{3,}|~{3,})/);
      if (marker) {
        if (!fence) fence = { ch: marker[1][0], len: marker[1].length };
        else if (fence.ch === marker[1][0] && marker[1].length >= fence.len && /^ {0,3}(`{3,}|~{3,})\s*$/.test(line)) fence = null;
        out.push(line);
        continue;
      }
      if (fence) {
        out.push(line);
        continue;
      }
      let value = '';
      let code = '';
      let i = 0;
      let system = false;
      while (i < line.length) {
        if (open) {
          const end = line.indexOf('-->', i);
          const part = end < 0 ? line.slice(i) : line.slice(i, end);
          if (!system) value += part;
          if (end < 0) { i = line.length; break; }
          open = false;
          if (!system) value += '〕';
          i = end + 3;
          continue;
        }
        if (line[i] === '`') {
          const ticks = line.slice(i).match(/^`+/)[0];
          if (!code) code = ticks;
          else if (code === ticks) code = '';
          value += ticks;
          i += ticks.length;
          continue;
        }
        if (!code && line.startsWith('<!--', i)) {
          open = true;
          i += 4;
          // Metadata the app writes into its own records stays hidden; anything else is shown.
          system = SYSTEM_COMMENT.test(line.slice(i));
          if (!system) value += '〔隐藏注释：';
          continue;
        }
        value += line[i++];
      }
      out.push(value);
    }
    return out.join('\n');
  }

  function indentOf(line) {
    return line.length - line.replace(/^ +/, '').length;
  }

  function isTableStart(lines, i) {
    return i + 1 < lines.length && lines[i].includes('|') && lines[i + 1].includes('|') && lines[i + 1].includes('-')
      && TABLE_SEP.test(lines[i + 1]);
  }

  function startsBlock(lines, i) {
    const l = lines[i];
    return FENCE.test(l) || HEADING.test(l) || HR.test(l) || /^ {0,3}>/.test(l) || LIST.test(l) || MATH_START.test(l)
      || isTableStart(lines, i);
  }

  // Split a table row on '|' except inside code spans, $…$ math, or when written as \|.
  function splitRow(line) {
    let s = line.trim();
    if (s.startsWith('|')) s = s.slice(1);
    if (s.endsWith('|') && !s.endsWith('\\|')) s = s.slice(0, -1);
    const cells = [];
    let cur = '';
    let code = 0;
    let math = '';
    let i = 0;
    while (i < s.length) {
      const ch = s[i];
      if (ch === '\\' && s[i + 1] === '|') { cur += '|'; i += 2; continue; }
      if (ch === '\\' && i + 1 < s.length) { cur += ch + s[i + 1]; i += 2; continue; }
      if (!math && ch === '`') {
        const run = s.slice(i).match(/^`+/)[0].length;
        if (!code) code = run; else if (run === code) code = 0;
        cur += s.slice(i, i + run);
        i += run;
        continue;
      }
      if (!code && ch === '$') {
        const mark = s[i + 1] === '$' ? '$$' : '$';
        if (!math) {
          const close = s.indexOf(mark, i + mark.length);
          if (close > 0 && /\S/.test(s[i + mark.length] || '') && /\S/.test(s[close - 1])) math = mark;
        } else if (math === mark) math = '';
        cur += mark;
        i += mark.length;
        continue;
      }
      if (ch === '|' && !code && !math) { cells.push(cur.trim()); cur = ''; i++; continue; }
      cur += ch;
      i++;
    }
    cells.push(cur.trim());
    return cells;
  }

  function fenced(lines, i, f, out) {
    const ch = f[2][0];
    const indent = f[1].length;
    const close = new RegExp('^ {0,3}' + (ch === '`' ? '`' : '~') + '{' + f[2].length + ',}[ \\t]*$');
    const strip = new RegExp('^ {0,' + indent + '}');
    const code = [];
    i++;
    while (i < lines.length && !close.test(lines[i])) {
      code.push(indent ? lines[i].replace(strip, '') : lines[i]);
      i++;
    }
    const lang = f[3].trim().split(/\s+/)[0];
    out.push('<pre class="md-pre"' + (lang ? ' data-lang="' + escapeHtml(lang) + '"' : '') + '><code>'
      + escapeHtml(code.join('\n')) + '</code></pre>');
    return i < lines.length ? i + 1 : i;
  }

  // Index of the line closing a display-math block that starts at line i, or -1.
  function mathBlockEnd(lines, i) {
    const first = lines[i].trim();
    const closer = first.startsWith('\\[') ? '\\]' : '$$';
    const rest = first.slice(2);
    if (rest.includes(closer)) return rest.trimEnd().endsWith(closer) ? i : -1;
    for (let j = i + 1; j < lines.length && j < i + 400; j++) {
      if (lines[j].trimEnd().endsWith(closer)) return j;
    }
    return -1;
  }

  function table(lines, i, opts, out) {
    const head = splitRow(lines[i]);
    const aligns = splitRow(lines[i + 1]).map(c => (/^:-+:$/.test(c) ? 'c' : /^-+:$/.test(c) ? 'r' : ''));
    i += 2;
    const rows = [];
    while (i < lines.length && lines[i].trim() && lines[i].includes('|') && !FENCE.test(lines[i]) && !HEADING.test(lines[i])) {
      rows.push(splitRow(lines[i]));
      i++;
    }
    const cell = (tag, text, k) => '<' + tag + (aligns[k] ? ' class="al-' + aligns[k] + '"' : '') + '>'
      + renderInline(text, opts) + '</' + tag + '>';
    let html = '<div class="table-wrap"><table><thead><tr>' + head.map((c, k) => cell('th', c, k)).join('') + '</tr></thead>';
    if (rows.length) {
      html += '<tbody>' + rows.map(r => '<tr>' + head.map((_, k) => cell('td', r[k] || '', k)).join('') + '</tr>').join('') + '</tbody>';
    }
    out.push(html + '</table></div>');
    return i;
  }

  function quote(lines, i, opts, out) {
    const q = [];
    while (i < lines.length) {
      const l = lines[i];
      if (/^ {0,3}>/.test(l)) { q.push(l.replace(/^ {0,3}> ?/, '')); i++; continue; }
      if (l.trim() && q.length && q[q.length - 1].trim() && !startsBlock(lines, i)) { q.push(l); i++; continue; }
      break;
    }
    out.push('<blockquote>' + blocks(q, opts, false) + '</blockquote>');
    return i;
  }

  function list(lines, i, opts, out) {
    const first = lines[i].match(LIST);
    const base = first[1].length;
    const ordered = /\d/.test(first[2]);
    const start = ordered ? parseInt(first[2], 10) : 1;
    const items = [];
    let loose = false;
    while (i < lines.length) {
      const m = lines[i].match(LIST);
      if (!m || m[1].length !== base || /\d/.test(m[2]) !== ordered || HR.test(lines[i])) break;
      const after = lines[i].slice(base + m[2].length);
      const gap = after.length - after.replace(/^ +/, '').length;
      const col = base + m[2].length + (gap >= 1 && gap <= 4 ? gap : 1);
      const body = [m[3] || ''];
      i++;
      while (i < lines.length) {
        const l = lines[i];
        if (!l.trim()) {
          let j = i + 1;
          while (j < lines.length && !lines[j].trim()) j++;
          if (j < lines.length && indentOf(lines[j]) >= col) { body.push(''); i++; continue; }
          break;
        }
        const ind = indentOf(l);
        if (ind >= col) { body.push(l.slice(col)); i++; continue; }
        if (ind > base && LIST.test(l)) { body.push(l.slice(ind)); i++; continue; }
        if (body[body.length - 1].trim() && !startsBlock(lines, i)) { body.push(l.trim()); i++; continue; }
        break;
      }
      items.push(body);
      let j = i;
      while (j < lines.length && !lines[j].trim()) j++;
      const next = j < lines.length && lines[j].match(LIST);
      if (next && next[1].length === base && /\d/.test(next[2]) === ordered && !HR.test(lines[j])) {
        if (j > i) loose = true;
        i = j;
        continue;
      }
      break;
    }
    const tag = ordered ? 'ol' : 'ul';
    const lis = items.map(body => {
      let task = '';
      const t = body[0].match(/^\[([ xX])\][ \t]+/);
      if (t) {
        body = [body[0].slice(t[0].length)].concat(body.slice(1));
        task = '<span class="md-task" aria-hidden="true">' + (t[1] === ' ' ? '☐' : '☑') + '</span> ';
      }
      return '<li>' + task + blocks(body, opts, !(loose || body.includes(''))) + '</li>';
    });
    out.push('<' + tag + (ordered && start !== 1 ? ' start="' + start + '"' : '') + '>' + lis.join('') + '</' + tag + '>');
    return i;
  }

  function blocks(lines, opts, tight) {
    const out = [];
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      if (!line.trim()) { i++; continue; }
      const f = line.match(FENCE);
      if (f && !(f[2][0] === '`' && f[3].includes('`'))) { i = fenced(lines, i, f, out); continue; }
      if (MATH_START.test(line)) {
        const end = mathBlockEnd(lines, i);
        if (end >= 0) {
          out.push('<div class="math-block">' + escapeHtml(lines.slice(i, end + 1).map(l => l.trim()).join('\n')) + '</div>');
          i = end + 1;
          continue;
        }
      }
      const h = line.match(HEADING);
      if (h) {
        const n = h[1].length;
        out.push('<h' + n + '>' + renderInline(h[2] || '', opts) + '</h' + n + '>');
        i++;
        continue;
      }
      if (HR.test(line)) { out.push('<hr>'); i++; continue; }
      if (isTableStart(lines, i)) { i = table(lines, i, opts, out); continue; }
      if (/^ {0,3}>/.test(line)) { i = quote(lines, i, opts, out); continue; }
      if (LIST.test(line)) { i = list(lines, i, opts, out); continue; }
      const para = [line.trim()];
      i++;
      while (i < lines.length && lines[i].trim() && !startsBlock(lines, i)) { para.push(lines[i].trim()); i++; }
      const inner = renderInline(para.join('\n'), opts);
      out.push(tight ? inner : '<p>' + inner + '</p>');
    }
    return out.join(tight ? '' : '\n');
  }

  // opts.base: folder of the file (root-relative) used to resolve relative links and images.
  function render(source, opts) {
    opts = Object.assign({ base: '' }, opts || {});
    const text = stripComments(String(source == null ? '' : source).replace(/\r\n?/g, '\n').replace(/\u0000/g, ''));
    const lines = text.split('\n').map(l => l.replace(/^\t+/, t => '    '.repeat(t.length)));
    return blocks(lines, opts, false);
  }

  return { render, renderInline, escapeHtml, resolvePath, fileUrl, stripComments };
}));
