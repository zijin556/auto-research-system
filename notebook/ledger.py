"""Anchors, the claim ledger, model reply blocks and the evidence rules (v2).

Pure text processing: callers read and write the files; this module only
parses and renders strings and applies the rules. Parsers never raise on
model output, they collect problems in ``errors``.

Rules in short (the product description is in 工作流说明.md):
- Every entry a model adds starts as 待定.
- 确定 needs bound evidence: a passing run whose plan named this entry
  (and its content hash) beforehand, or an independent re-derivation (V)
  by a different model family, or the user's own decision on the page.
- 否定 needs the same kind of evidence. A bare counterexample only adds a
  note and a new 待验证 entry; it never flips a state by itself.
- Model text that claims to be a human decision is refused.
- Refuted entries form the 淘汰清单; re-proposing one is refused.
- Anchors change only through requests that the user approves.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import re
from datetime import datetime, timezone
from dataclasses import dataclass, field

STATES = ("待定", "确定", "否定")
TYPES = ("推导步", "论断", "待验证", "想法", "文献")
TYPE_ALIASES = {"推导": "推导步", "推导步骤": "推导步", "步骤": "推导步", "命题": "论断", "结论": "论断", "断言": "论断",
                "验证": "待验证", "待核查": "待验证", "核查": "待验证", "淘汰方向": "想法", "方向": "想法", "候选": "想法",
                "参考文献": "文献", "文献条目": "文献"}
FAMILY_LABELS = {"openai": "GPT", "google": "Gemini", "anthropic": "Claude", "human": "你", "system": "系统"}
LABEL_FAMILIES = {"GPT": "openai", "Gemini": "google", "Claude": "anthropic", "你": "human", "系统": "system"}
MODEL_FAMILIES = {"openai", "google", "anthropic"}
HUMAN_WORDS = ("人工", "用户", "本人", "你的决定", "我的决定", "老师", "导师")

ANCHOR_SLOTS = {
    "framework_layer": [("question", "要回答的大问题"), ("criterion", "论文主张与成功标准"), ("assumptions", "前提与假设"),
                        ("scope", "约束（设备、数据、时间、学校要求）"), ("nongoals", "不做什么")],
    "chapter": [("question", "本章回答的问题"), ("criterion", "判据与评测协议（数据、指标、基线）"), ("assumptions", "依赖与假设"),
                ("scope", "范围"), ("nongoals", "不做什么")],
    "question": [("question", "要回答的问题"), ("criterion", "判据"), ("assumptions", "假设"), ("scope", "范围"), ("nongoals", "不做什么")],
}
ASSUMPTION_STATES = ("已确认", "待批", "已拒绝")
REQUEST_STATES = ("待处理", "已批准", "已拒绝")
ASSUMPTION_HEADER = ["编号", "内容", "依据", "失效条件", "状态", "来源"]
REQUEST_HEADER = ["编号", "项目", "申请改为", "理由", "来源", "状态", "处理说明"]
LEDGER_HEADER = ["编号", "类型", "内容", "状态", "依据", "证据", "备注", "来源", "更新"]
LEDGER_NOTE = "> 状态：待定 / 确定 / 否定。确定或否定需要证据：按事先写定判据通过的运行（R）、另一家模型的独立复推（V），或你的决定。"

BLOCK_NAMES = ("正文修订", "台账变更", "验证计划", "小问题清单", "候选框架", "复推", "锚点草稿")
PLAN_KEYS = ("标题", "针对", "方向", "方法", "输入与参数", "退化自检", "判据", "预期")
QUESTION_KEYS = {"问题": "question", "判据": "criterion", "依赖": "depends", "承重": "load_bearing", "验证方式": "method"}
SUMMARY_KEYS = ("结论摘要", "主要分歧", "建议的下一步")
DRIFT_KEYS = (("仍在回答锚点里的问题", "question_ok"), ("用了锚点以外的假设", "extra_assumptions"),
              ("判据或范围被改变了", "criterion_changed"), ("判定", "verdict"))

_COLON = r"[：:]"
_FENCE_OPEN = re.compile(r"^(?P<indent> {0,3})(?P<fence>`{3,}|~{3,})[ \t]*(?P<info>[^\n]*?)[ \t]*$")
_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_NUMBER = re.compile(r"^(\d+(?:\.\d+)*)")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def norm_space(text: str) -> str:
    return " ".join((text or "").split())


def norm_key(text: str) -> str:
    """Comparison key for duplicate detection: no spaces, no trailing punctuation."""
    value = re.sub(r"\s+", "", text or "")
    return value.rstrip("。．.；;，,！!")


def family_label(family: str) -> str:
    return FAMILY_LABELS.get(family or "", "模型")


def family_from_source(source: str) -> str:
    head = (source or "").split("·", 1)[0].strip()
    return LABEL_FAMILIES.get(head, "")


def split_fields(line: str) -> list[str]:
    """Split on | except inside $…$, $$…$$, backticks, or when escaped as \\|."""
    def split(protect: bool) -> tuple[list[str], bool]:
        cells, buf, i, math, code = [], [], 0, "", 0
        while i < len(line):
            ch = line[i]
            if ch == "\\" and i + 1 < len(line) and line[i + 1] == "|":
                buf.append("|")
                i += 2
                continue
            if protect and ch == "`" and not math:
                run = len(line[i:]) - len(line[i:].lstrip("`"))
                code = 0 if code == run else (run if not code else code)
                buf.append("`" * run)
                i += run
                continue
            if protect and ch == "$" and not code and (i == 0 or line[i - 1] != "\\"):
                token = "$$" if line.startswith("$$", i) else "$"
                math = "" if math == token else (token if not math else math)
                buf.append(token)
                i += len(token)
                continue
            if ch == "|" and not math and not code:
                cells.append("".join(buf).strip())
                buf = []
                i += 1
                continue
            buf.append(ch)
            i += 1
        cells.append("".join(buf).strip())
        return cells, bool(math or code)

    cells, unbalanced = split(True)
    if unbalanced:
        # A lone $ (for example a price) must not swallow the remaining separators.
        cells, _ = split(False)
    return cells


def cell(text: str) -> str:
    """Escape a value for one Markdown table cell."""
    value = (text or "").replace("\r\n", "\n").strip()
    value = value.replace("|", "\\|")
    return value.replace("\n", "<br>")


def uncell(text: str) -> str:
    return re.sub(r"<br\s*/?>", "\n", text or "", flags=re.I).strip()


def table_rows(lines: list[str]) -> tuple[list[str], list[list[str]]]:
    """Parse a Markdown table: (header, rows). Rows are lists of raw cell strings."""
    header, rows = [], []
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = split_fields(stripped)
        if cells and cells[0] == "":
            cells = cells[1:]
        if cells and cells[-1] == "" and stripped.endswith("|") and not stripped.endswith("\\|"):
            cells = cells[:-1]
        if all(re.fullmatch(r":?-{2,}:?", c.replace(" ", "")) for c in cells if c) and any(cells):
            continue
        if not header:
            header = [c.strip() for c in cells]
            continue
        rows.append(cells)
    return header, rows


def render_table(header: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(cell(v) for v in row) + " |")
    return "\n".join(lines)


def _column(header: list[str], names: tuple[str, ...], default: int) -> int:
    for index, title in enumerate(header):
        if any(name in title for name in names):
            return index
    return default


def _get(row: list[str], index: int) -> str:
    return uncell(row[index]) if 0 <= index < len(row) else ""


def iter_lines_outside_fences(text: str):
    """Yield (index, line, in_fence) for every line, tracking fenced code."""
    fence = None
    for index, line in enumerate((text or "").split("\n")):
        match = _FENCE_OPEN.match(line)
        if fence is None and match:
            fence = (match.group("fence")[0], len(match.group("fence")))
            yield index, line, True
            continue
        if fence is not None:
            stripped = line.strip()
            if stripped and set(stripped) == {fence[0]} and len(stripped) >= fence[1]:
                fence = None
            yield index, line, True
            continue
        yield index, line, False


# ---------------------------------------------------------------------------
# Anchors
# ---------------------------------------------------------------------------

SLOT_ALIASES = {
    "question": ("要回答的问题", "要回答的大问题", "本章回答的问题", "本章要回答的问题", "研究问题", "大问题", "问题"),
    "criterion": ("判据与评测协议", "论文主张与成功标准", "成功标准", "评测协议", "判据", "主张", "标准"),
    "assumptions": ("前提与假设", "依赖与假设", "假设", "前提", "新假设", "新增假设"),
    "scope": ("约束", "范围", "适用范围", "条件与约束"),
    "nongoals": ("不做什么", "非目标", "不做", "不讨论"),
}


@dataclass
class Assumption:
    id: str
    text: str
    basis: str = ""
    failure: str = ""
    status: str = "待批"
    source: str = ""


@dataclass
class ChangeRequest:
    id: str
    slot: str
    proposal: str
    reason: str = ""
    source: str = ""
    status: str = "待处理"
    note: str = ""


@dataclass
class Anchor:
    level: str
    version: int = 1
    status: str = "draft"
    confirmed_at: str = ""
    updated_at: str = ""
    slots: dict = field(default_factory=dict)
    assumptions: list = field(default_factory=list)
    requests: list = field(default_factory=list)
    extra: str = ""


def slot_labels(level: str) -> dict:
    return dict(ANCHOR_SLOTS.get(level, ANCHOR_SLOTS["question"]))


def slot_key(text: str) -> str:
    """Map a heading or a `?` item such as '判据与评测协议（数据…）' to a slot key ('' if unknown)."""
    value = re.sub(r"[（(][^）)]*[）)]", "", text or "")
    value = re.sub(r"^[\d.\s、．]+", "", value)
    value = re.sub(r"[\s*：:#【】]", "", value)
    if not value:
        return ""
    for key, aliases in SLOT_ALIASES.items():
        if key == "question":
            continue
        if any(value == alias or value.startswith(alias) for alias in aliases):
            return key
    if any(value == alias or value.startswith(alias) or value.endswith(alias) for alias in SLOT_ALIASES["question"]):
        return "question"
    return ""


def empty_anchor(level: str) -> Anchor:
    return Anchor(level=level, slots={key: "" for key, _ in ANCHOR_SLOTS.get(level, ANCHOR_SLOTS["question"]) if key != "assumptions"})


def _sections(text: str) -> list[tuple[str, list[str]]]:
    """Split Markdown into ('', preamble) + (heading, lines) for each level-2 heading outside fences."""
    out, heading, lines = [], "", []
    for _, line, in_fence in iter_lines_outside_fences(text):
        match = None if in_fence else re.match(r"^##[ \t]+(.+?)[ \t]*#*[ \t]*$", line)
        if match:
            out.append((heading, lines))
            heading, lines = match.group(1).strip(), []
        else:
            lines.append(line)
    out.append((heading, lines))
    return out


def _extract_failure(text: str) -> tuple[str, str]:
    match = re.search(r"[（(]?\s*失效条件\s*" + _COLON + r"\s*([^）)]*)[）)]?\s*$", text or "")
    if not match:
        return (text or "").strip(), ""
    return text[:match.start()].strip().rstrip("，,；;"), match.group(1).strip()


def _parse_assumptions(lines: list[str]) -> list[Assumption]:
    header, rows = table_rows(lines)
    found = []
    if header:
        c_id = _column(header, ("编号",), 0)
        c_text = _column(header, ("内容", "假设"), 1)
        c_basis = _column(header, ("依据",), 2)
        c_fail = _column(header, ("失效",), 3)
        c_state = _column(header, ("状态",), 4)
        c_source = _column(header, ("来源",), 5)
        for row in rows:
            text = _get(row, c_text)
            if not text:
                continue
            status = _get(row, c_state)
            if status not in ASSUMPTION_STATES:
                status = "已确认" if status in {"确认", "已批准", "确定"} else "已拒绝" if status in {"拒绝", "否定"} else "待批"
            found.append(Assumption(id=_get(row, c_id), text=text, basis=_get(row, c_basis), failure=_get(row, c_fail),
                                    status=status, source=_get(row, c_source)))
    else:
        for line in lines:
            match = re.match(r"^\s*(?:[-*+]|\d+[.)、])\s+(?:\*\*)?(H\d+)?(?:\*\*)?\s*[：:.]?\s*(.+)$", line)
            if match and match.group(2).strip():
                text, failure = _extract_failure(match.group(2))
                basis = ""
                basis_match = re.search(r"[（(]\s*依据\s*" + _COLON + r"\s*([^）)]*)[）)]", text)
                if basis_match:
                    basis, text = basis_match.group(1).strip(), (text[:basis_match.start()] + text[basis_match.end():]).strip()
                found.append(Assumption(id=match.group(1) or "", text=text, basis=basis, failure=failure, status="待批"))
    # Every assumption needs a unique H id; repair missing or repeated ids.
    number = max((int(a.id[1:]) for a in found if re.fullmatch(r"H\d+", a.id or "")), default=0)
    seen = set()
    for a in found:
        if not re.fullmatch(r"H\d+", a.id or "") or a.id in seen:
            number += 1
            a.id = f"H{number}"
        seen.add(a.id)
    return found


def _parse_requests(lines: list[str]) -> list[ChangeRequest]:
    header, rows = table_rows(lines)
    if not header:
        return []
    cols = {name: _column(header, (name,), i) for i, name in enumerate(REQUEST_HEADER)}
    out = []
    for row in rows:
        rid = _get(row, cols["编号"])
        if not re.fullmatch(r"Q\d+", rid):
            continue
        status = _get(row, cols["状态"])
        out.append(ChangeRequest(id=rid, slot=_get(row, cols["项目"]), proposal=_get(row, cols["申请改为"]), reason=_get(row, cols["理由"]),
                                 source=_get(row, cols["来源"]), status=status if status in REQUEST_STATES else "待处理", note=_get(row, cols["处理说明"])))
    return out


def parse_anchor(text: str, level: str | None = None) -> Anchor:
    meta = {}
    match = re.search(r"<!--\s*anchor\s+(\{[^\n]*\})\s*-->", text or "")
    if match:
        try:
            meta = json.loads(match.group(1))
        except ValueError:
            meta = {}
    level = level or meta.get("level") or "question"
    anchor = empty_anchor(level)
    try:
        anchor.version = max(1, int(meta.get("version", 1)))
    except (TypeError, ValueError):
        anchor.version = 1
    anchor.status = "confirmed" if meta.get("status") == "confirmed" else "draft"
    anchor.confirmed_at = str(meta.get("confirmed_at", "") or "")
    anchor.updated_at = str(meta.get("updated_at", "") or "")
    body = re.sub(r"<!--\s*anchor\s+\{[^\n]*\}\s*-->", "", text or "")
    extra = []
    for heading, lines in _sections(body):
        if not heading:
            continue
        if re.sub(r"\s", "", heading) in {"变更申请", "锚点变更申请"}:
            anchor.requests = _parse_requests(lines)
            continue
        key = slot_key(heading)
        if key == "assumptions" and not anchor.assumptions:
            anchor.assumptions = _parse_assumptions(lines)
        elif key and key in anchor.slots and not anchor.slots[key]:
            anchor.slots[key] = "\n".join(lines).strip()
        else:
            extra.append("## " + heading + "\n\n" + "\n".join(lines).strip())
    anchor.extra = "\n\n".join(x.strip() for x in extra if x.strip())
    return anchor


def render_anchor(anchor: Anchor) -> str:
    labels = ANCHOR_SLOTS.get(anchor.level, ANCHOR_SLOTS["question"])
    meta = {"schema": 1, "level": anchor.level, "version": anchor.version, "status": anchor.status,
            "confirmed_at": anchor.confirmed_at or "", "updated_at": anchor.updated_at or ""}
    parts = ["# 锚点", "", "<!-- anchor " + json.dumps(meta, ensure_ascii=False, separators=(",", ":")) + " -->", ""]
    for key, label in labels:
        parts += ["## " + label, ""]
        if key == "assumptions":
            rows = [[a.id, a.text, a.basis, a.failure, a.status, a.source] for a in anchor.assumptions]
            parts += [render_table(ASSUMPTION_HEADER, rows), ""]
        else:
            value = (anchor.slots.get(key) or "").strip()
            parts += [value, ""] if value else []
    if anchor.extra.strip():
        parts += [anchor.extra.strip(), ""]
    if anchor.requests:
        rows = [[r.id, r.slot, r.proposal, r.reason, r.source, r.status, r.note] for r in anchor.requests]
        parts += ["## 变更申请", "", render_table(REQUEST_HEADER, rows), ""]
    return "\n".join(parts).rstrip() + "\n"


def next_assumption_id(anchor: Anchor) -> str:
    numbers = [int(a.id[1:]) for a in anchor.assumptions if re.fullmatch(r"H\d+", a.id or "")]
    return f"H{max(numbers, default=0) + 1}"


def next_request_id(anchor: Anchor) -> str:
    numbers = [int(r.id[1:]) for r in anchor.requests if re.fullmatch(r"Q\d+", r.id or "")]
    return f"Q{max(numbers, default=0) + 1}"


def anchor_blocking(anchor: Anchor) -> list[ChangeRequest]:
    return [r for r in anchor.requests if r.status == "待处理"]


def pending_assumptions(anchor: Anchor) -> list[Assumption]:
    return [a for a in anchor.assumptions if a.status == "待批"]


def find_assumption(anchor: Anchor, aid: str) -> Assumption | None:
    return next((a for a in anchor.assumptions if a.id == aid), None)


def render_anchor_section(anchor: Anchor) -> str:
    """Body of mini-paper section '1 问题与假设', generated from the anchor."""
    labels = slot_labels(anchor.level)
    lines = [f"> 本节由锚点（第 {anchor.version} 版）生成；要修改请编辑锚点。", ""]
    for key in ("question", "criterion"):
        value = (anchor.slots.get(key) or "").strip() or "（待填写）"
        lines += [f"**{labels.get(key, key)}。** {value}", ""]
    active = [a for a in anchor.assumptions if a.status != "已拒绝"]
    lines.append(f"**{labels.get('assumptions', '假设')}。**" + ("" if active else "（暂无）"))
    if active:
        lines.append("")
        for a in active:
            tags = (["依据：" + a.basis] if a.basis else []) + (["失效条件：" + a.failure] if a.failure else [])
            pending = "（待批）" if a.status == "待批" else ""
            lines.append(f"- {a.id}{pending}：{a.text}" + ("。" + "；".join(tags) + "。" if tags else ""))
    lines.append("")
    for key in ("scope", "nongoals"):
        value = (anchor.slots.get(key) or "").strip() or "（待填写）"
        lines += [f"**{labels.get(key, key)}。** {value}", ""]
    return "\n".join(lines).strip()


def user_decide_assumption(anchor: Anchor, aid: str, approve: bool, note: str = "") -> Assumption:
    found = find_assumption(anchor, aid)
    if found is None:
        raise ValueError(f"找不到假设 {aid}")
    if found.status != "待批":
        raise ValueError(f"{aid} 已经处理过（{found.status}）")
    found.status = "已确认" if approve else "已拒绝"
    if note.strip():
        found.source = (found.source + "；" if found.source else "") + "你：" + note.strip()
    anchor.version += 1
    return found


def user_decide_request(anchor: Anchor, rid: str, approve: bool, note: str = "") -> ChangeRequest:
    request = next((r for r in anchor.requests if r.id == rid), None)
    if request is None:
        raise ValueError(f"找不到变更申请 {rid}")
    if request.status != "待处理":
        raise ValueError(f"{rid} 已经处理过（{request.status}）")
    request.note = note.strip()
    if not approve:
        request.status = "已拒绝"
        return request
    request.status = "已批准"
    target = find_assumption(anchor, request.slot) if re.fullmatch(r"H\d+", request.slot or "") else None
    if target is not None:
        text, failure = _extract_failure(request.proposal)
        target.text, target.failure = text, failure or target.failure
        target.status = "已确认"
    else:
        key = slot_key(request.slot)
        if key == "assumptions":
            text, failure = _extract_failure(request.proposal)
            anchor.assumptions.append(Assumption(id=next_assumption_id(anchor), text=text, failure=failure, basis=request.reason,
                                                 status="已确认", source=request.source))
        elif key in anchor.slots:
            anchor.slots[key] = request.proposal.strip()
        else:
            request.note = (request.note + "；" if request.note else "") + "未知锚点项，请手动修改锚点"
    anchor.version += 1
    # The user reads the whole new anchor once more and confirms it.
    anchor.status = "draft"
    return request


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------

@dataclass
class Entry:
    id: str
    type: str
    text: str
    state: str = "待定"
    basis: str = ""
    evidence: str = ""
    note: str = ""
    source: str = ""
    updated: str = ""
    family: str = ""
    support: list = field(default_factory=list)
    superseded_by: str = ""
    sha: str = ""
    counter_at: str = ""      # time of the latest open counterexample
    pending_token: str = ""   # evidence waiting for the basis to be settled


@dataclass
class Ledger:
    entries: list = field(default_factory=list)
    next: int = 1
    last_temp_map: dict = field(default_factory=dict)


def normalize_type(value: str) -> str:
    value = re.sub(r"[\s【】\[\]]", "", value or "")
    if value in TYPES:
        return value
    return TYPE_ALIASES.get(value, "")


def normalize_state(value: str) -> str:
    value = re.sub(r"\s", "", value or "")
    if value in STATES:
        return value
    if value in {"已确定", "成立", "确认", "已确认", "支持"}:
        return "确定"
    if value in {"已否定", "不成立", "否决", "驳回", "错误"}:
        return "否定"
    if value in {"待核", "待核查", "存疑", "未定", "待验证"}:
        return "待定"
    return ""


def entry_sha(entry: Entry) -> str:
    """Content hash used to bind a verification plan to an entry's exact wording."""
    return sha256((entry.type or "") + "\n" + norm_space(uncell(entry.text)))


def empty_ledger() -> Ledger:
    return Ledger()


def parse_ledger(text: str) -> Ledger:
    meta = {}
    match = re.search(r"<!--\s*ledger\s+(\{[^\n]*\})\s*-->", text or "")
    if match:
        try:
            meta = json.loads(match.group(1))
        except ValueError:
            meta = {}
    header, rows = table_rows([line for _, line, fenced in iter_lines_outside_fences(text or "") if not fenced])
    cols = {name: _column(header, (name,), i) for i, name in enumerate(LEDGER_HEADER)} if header else {}
    entries, seen = [], set()
    entry_meta = meta.get("meta", {}) if isinstance(meta.get("meta"), dict) else {}
    for row in rows:
        eid = _get(row, cols.get("编号", 0))
        if not re.fullmatch(r"L\d+", eid) or eid in seen:
            continue
        seen.add(eid)
        state = normalize_state(_get(row, cols["状态"])) or "待定"
        entry = Entry(id=eid, type=normalize_type(_get(row, cols["类型"])) or _get(row, cols["类型"]), text=_get(row, cols["内容"]), state=state,
                      basis=_get(row, cols["依据"]), evidence=_get(row, cols["证据"]), note=_get(row, cols["备注"]),
                      source=_get(row, cols["来源"]), updated=_get(row, cols["更新"]))
        info = entry_meta.get(eid, {}) if isinstance(entry_meta.get(eid), dict) else {}
        entry.family = info.get("family") or family_from_source(entry.source)
        entry.support = [f for f in info.get("support", []) if isinstance(f, str)]
        entry.superseded_by = str(info.get("superseded_by", "") or "")
        entry.sha = str(info.get("sha", "") or "")
        entry.counter_at = str(info.get("counter_at", "") or "")
        entry.pending_token = str(info.get("pending", "") or "")
        # A settled entry whose wording was edited by hand no longer matches its evidence.
        if entry.state in {"确定", "否定"} and entry.sha and entry.sha != entry_sha(entry) and "你的决定" not in entry.evidence:
            old = entry.evidence
            entry.state, entry.evidence = "待定", ""
            entry.note = (entry.note + "；" if entry.note else "") + f"内容在{'确定' if state == '确定' else '否定'}后被修改，原证据 {old or '无'} 不再适用"
            entry.sha = entry_sha(entry)
        entries.append(entry)
    numbers = [int(e.id[1:]) for e in entries]
    try:
        stored_next = int(meta.get("next", 1))
    except (TypeError, ValueError):
        stored_next = 1
    return Ledger(entries=entries, next=max([stored_next, *(n + 1 for n in numbers)] or [1]))


def render_ledger(ledger: Ledger) -> str:
    meta_entries = {e.id: {"family": e.family, "support": sorted(set(e.support)), "sha": e.sha or entry_sha(e), "superseded_by": e.superseded_by,
                           **({"counter_at": e.counter_at} if e.counter_at else {}), **({"pending": e.pending_token} if e.pending_token else {})}
                    for e in ledger.entries}
    meta = {"schema": 1, "next": ledger.next, "meta": meta_entries}
    rows = [[e.id, e.type, e.text, e.state, e.basis, e.evidence, e.note, e.source, e.updated] for e in ledger.entries]
    return "\n".join(["# 台账", "", "<!-- ledger " + json.dumps(meta, ensure_ascii=False, separators=(",", ":")) + " -->", "",
                      LEDGER_NOTE, "", render_table(LEDGER_HEADER, rows), ""])


def counts(ledger: Ledger) -> dict:
    out = {state: 0 for state in STATES}
    for e in ledger.entries:
        out[e.state] = out.get(e.state, 0) + 1
    return out


def rejected_list(ledger: Ledger) -> list[Entry]:
    return [e for e in ledger.entries if e.state == "否定" and not e.superseded_by]


def find(ledger: Ledger, eid: str) -> Entry | None:
    return next((e for e in ledger.entries if e.id == eid), None)


def append_log(old_text: str, heading: str, results: list) -> str:
    text = (old_text or "").rstrip() + "\n\n## " + heading.strip() + "\n\n| 原文 | 处理 | 说明 |\n|---|---|---|\n"
    for r in results:
        status = r.status + (f" → {r.entry_id}" if getattr(r, "entry_id", "") else "")
        text += f"| {cell(r.raw)} | {cell(status)} | {cell(r.reason)} |\n"
    return text


# ---------------------------------------------------------------------------
# Model replies
# ---------------------------------------------------------------------------

@dataclass
class Revision:
    section: str
    mode: str
    body: str
    raw: str = ""


@dataclass
class LedgerOp:
    kind: str            # '+', '~', '?', '!', 'invalid'
    temp: str
    fields: list
    raw: str


@dataclass
class Reply:
    text: str
    revisions: list = field(default_factory=list)
    ops: list = field(default_factory=list)
    plans: list = field(default_factory=list)
    questions: list = field(default_factory=list)
    candidates: list = field(default_factory=list)
    verifications: list = field(default_factory=list)
    drift: dict | None = None
    summary: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)


def _block_name(info: str) -> str:
    name = re.sub(r"[【】\[\]：:]", " ", info or "").strip().split(" ")[0] if info else ""
    return name if name in BLOCK_NAMES else ""


def blocks(text: str) -> list[tuple[str, str, int]]:
    """Fenced blocks with a known name: (name, body, start_line). Unclosed blocks run to the end.

    An unnamed fence whose body contains a named block opener is a wrapper (a model put its
    whole reply in a code block): skip the wrapper's opener and keep scanning inside it.
    """
    lines = (text or "").split("\n")
    out, i = [], 0
    while i < len(lines):
        match = _FENCE_OPEN.match(lines[i])
        if not match:
            i += 1
            continue
        char, length = match.group("fence")[0], len(match.group("fence"))
        name = _block_name(match.group("info"))
        body, j = [], i + 1
        while j < len(lines):
            stripped = lines[j].strip()
            if stripped and set(stripped) == {char} and len(stripped) >= length:
                break
            body.append(lines[j])
            j += 1
        if not name and any((m := _FENCE_OPEN.match(line)) and _block_name(m.group("info")) for line in body):
            i += 1
            continue
        if name:
            out.append((name, "\n".join(body), i))
        i = j + 1
    return out


def _key_values(body: str, keys: tuple[str, ...], aliases: dict | None = None) -> dict:
    """Parse '键：值' lines; values continue on following lines until the next key."""
    aliases = aliases or {}
    known = {**{k: k for k in keys}, **aliases}
    pattern = re.compile(r"^\s*(?:[-*]\s*)?(?:\*\*)?(" + "|".join(sorted(map(re.escape, known), key=len, reverse=True)) + r")(?:\*\*)?\s*" + _COLON + r"\s*(.*)$")
    out, current = {}, None
    for line in body.split("\n"):
        match = pattern.match(line)
        if match:
            current = known[match.group(1)]
            out[current] = (out.get(current, "") + "\n" + match.group(2)).strip() if current in out else match.group(2).strip()
        elif current and line.strip():
            out[current] = (out[current] + "\n" + line.rstrip()).strip()
    return out


def _parse_revision(body: str, errors: list) -> Revision | None:
    lines = body.split("\n")
    section = mode = None
    start = 0
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped and section is None and mode is None:
            continue
        m_section = re.match(r"^(?:\*\*)?节(?:\*\*)?\s*" + _COLON + r"\s*(.+)$", stripped)
        m_mode = re.match(r"^(?:\*\*)?方式(?:\*\*)?\s*" + _COLON + r"\s*(.+)$", stripped)
        if m_section and section is None:
            section, start = m_section.group(1).strip(), index + 1
        elif m_mode and mode is None:
            mode, start = m_mode.group(1).strip(), index + 1
        else:
            break
    if not section:
        errors.append("正文修订缺少“节：”")
        return None
    mode_text = (mode or "").replace(" ", "")
    if mode_text.startswith("替换") or mode_text.lower().startswith("replace"):
        mode = "替换"
    elif mode_text.startswith("追加") or mode_text.lower().startswith("append") or mode_text.startswith("新增"):
        mode = "追加"
    else:
        errors.append(f"正文修订“{section}”的方式不明（只能是 替换 或 追加），未应用")
        return None
    content = "\n".join(lines[start:]).strip("\n")
    return Revision(section=section, mode=mode, body=content.strip(), raw=body)


def _parse_op(line: str) -> LedgerOp | None:
    raw = line.strip()
    stripped = re.sub(r"^(?:[-*]\s+)", "", raw)
    if not stripped or stripped in {"无", "（无）", "无变更", "没有变更"}:
        return None
    kind = stripped[0]
    if kind in "+＋":
        rest = stripped[1:].strip()
        temp = ""
        match = re.match(r"^(\[\s*新\s*\d+\s*\]|【\s*新\s*\d+\s*】)\s*(.*)$", rest)
        if match:
            temp = "[新" + re.sub(r"\D", "", match.group(1)) + "]"
            rest = match.group(2)
        return LedgerOp("+", temp, split_fields(rest), raw)
    if kind in "~～":
        return LedgerOp("~", "", split_fields(stripped[1:].strip()), raw)
    if kind in "?？":
        return LedgerOp("?", "", split_fields(stripped[1:].strip()), raw)
    if kind in "!！":
        return LedgerOp("!", "", [stripped[1:].strip()], raw)
    return LedgerOp("invalid", "", [raw], raw)


def _parse_questions(body: str) -> list[dict]:
    items, current = [], None
    for line in body.split("\n"):
        heading = re.match(r"^\s*#{2,4}\s+(.+?)\s*$", line)
        numbered = re.match(r"^\s*\d+[.)、]\s+(?:标题\s*" + _COLON + r"\s*)?(.+?)\s*$", line)
        if heading or (numbered and not re.match(r"^\s*\d+[.)、]\s+(?:问题|判据|依赖|承重|验证方式)\s*" + _COLON, line)):
            title = (heading or numbered).group(1).strip().strip("*")
            title = re.sub(r"^标题\s*" + _COLON + r"\s*", "", title)
            current = {"title": title, "question": "", "criterion": "", "depends": "", "load_bearing": False, "method": "", "_lines": []}
            items.append(current)
        elif current is not None:
            current["_lines"].append(line)
    out = []
    for item in items:
        values = _key_values("\n".join(item.pop("_lines")), tuple(QUESTION_KEYS))
        for zh, key in QUESTION_KEYS.items():
            if zh in values:
                item[key] = values[zh]
        item["load_bearing"] = str(item.get("load_bearing", "")).strip().startswith("是")
        if item["title"]:
            out.append(item)
    return out


def _parse_verifications(body: str, errors: list) -> list[dict]:
    out = []
    for line in body.split("\n"):
        if not line.strip():
            continue
        fields = split_fields(line.strip().lstrip("-* ").strip())
        if len(fields) < 2 or not re.fullmatch(r"L\d+", fields[0]):
            errors.append("无法识别的复推行：" + line.strip())
            continue
        verdict_text = fields[1]
        verdict = "不一致" if "不一致" in verdict_text else "一致" if "一致" in verdict_text else "无法核对" if "无法" in verdict_text else ""
        if not verdict:
            errors.append("复推结论只能是 一致、不一致 或 无法核对：" + line.strip())
            continue
        out.append({"entry": fields[0], "verdict": verdict, "note": " | ".join(fields[2:]).strip()})
    return out


def _normalize_verdict(value: str) -> str:
    """One of 未漂移 / 轻微 / 漂移, or '' when the answer is missing or names several options."""
    value = (value or "").strip()
    if not value or "/" in value or "／" in value:
        return ""
    no_drift = any(word in value for word in ("未漂移", "没有漂移", "无漂移"))
    slight = "轻微" in value
    rest = value.replace("轻微漂移", "轻微")
    for word in ("未漂移", "没有漂移", "无漂移"):
        rest = rest.replace(word, "")
    options = [name for name, hit in (("未漂移", no_drift), ("轻微", slight), ("漂移", "漂移" in rest)) if hit]
    return options[0] if len(options) == 1 else ""


def _unwrap(text: str) -> str:
    """Some models wrap the whole reply in one code fence; remove that outer fence."""
    lines = (text or "").strip().split("\n")
    if len(lines) >= 2:
        first = _FENCE_OPEN.match(lines[0])
        last = lines[-1].strip()
        if first and not _block_name(first.group("info")) and last and set(last) == {first.group("fence")[0]} and len(last) >= len(first.group("fence")):
            return "\n".join(lines[1:-1])
    return text


def _split_plans(body: str) -> list[str]:
    """One 验证计划 block may hold several plans: split at ---/### lines or when 标题/针对 repeats."""
    chunks, current, seen = [], [], set()
    starter = re.compile(r"^\s*(?:[-*]\s*)?(?:\*\*)?(标题|针对)(?:\*\*)?\s*" + _COLON)
    for line in body.split("\n"):
        if re.match(r"^\s*(?:-{3,}|\*{3,}|#{2,4}\s)", line):
            if any(x.strip() for x in current):
                chunks.append("\n".join(current))
            current, seen = [], set()
            heading = re.match(r"^\s*#{2,4}\s+(.+)$", line)
            if heading:
                current.append("标题：" + heading.group(1).strip())
                seen.add("标题")
            continue
        match = starter.match(line)
        if match and match.group(1) in seen:
            chunks.append("\n".join(current))
            current, seen = [], set()
        if match:
            seen.add(match.group(1))
        current.append(line)
    if any(x.strip() for x in current):
        chunks.append("\n".join(current))
    return chunks


def parse_reply(text: str) -> Reply:
    inner = _unwrap(text)
    if inner != text and len(blocks(inner)) > len(blocks(text)):
        unwrapped = parse_reply(inner)
        unwrapped.text = text or ""
        return unwrapped
    reply = Reply(text=text or "")
    found = blocks(text)
    for name, body, _ in found:
        if name == "正文修订":
            revision = _parse_revision(body, reply.errors)
            if revision:
                reply.revisions.append(revision)
        elif name == "台账变更":
            for line in body.split("\n"):
                op = _parse_op(line)
                if op:
                    reply.ops.append(op)
        elif name == "验证计划":
            aliases = {"输入": "输入与参数", "参数": "输入与参数", "自检": "退化自检", "通过标准": "判据", "目标": "针对"}
            for chunk in _split_plans(body):
                plan = _key_values(chunk, PLAN_KEYS, aliases)
                if not plan:
                    continue
                direction = plan.get("方向", "")
                if "支持" in direction and "否定" in direction:
                    reply.errors.append(f"验证计划“{plan.get('标题', '未命名')}”的方向同时写了支持和否定，未登记")
                    continue
                reply.plans.append(plan)
        elif name == "小问题清单":
            reply.questions += _parse_questions(body)
        elif name == "候选框架":
            if body.strip():
                reply.candidates.append(body.strip())
        elif name == "复推":
            reply.verifications += _parse_verifications(body, reply.errors)
    if not any(name == "台账变更" for name, _, _ in found):
        # Lenient fallback: a '台账变更' heading followed by op lines, without a fence.
        collecting = False
        for _, line, fenced in iter_lines_outside_fences(text or ""):
            if fenced:
                continue
            if re.match(r"^\s*(?:#+\s*)?(?:\*\*)?【?台账变更】?\s*[：:]?\s*(?:\*\*)?\s*[：:]?\s*$", line):
                collecting = True
                continue
            if collecting:
                if not line.strip():
                    continue
                if line.strip()[0] in "+＋~～?？!！" or line.strip() in {"无", "（无）"}:
                    op = _parse_op(line)
                    if op:
                        reply.ops.append(op)
                else:
                    break
    drift = {}
    pending_key = None
    for _, line, fenced in iter_lines_outside_fences(text or ""):
        # Summary lines only count outside fences; drift answers are read everywhere,
        # because models often put the four answer lines in a code block or a table.
        bare = re.sub(r"^\s*(?:[>|]\s*)*", "", line).replace("**", "")
        bare = re.sub(r"^\s*(?:[-*]\s|\d+[.)、]\s*)?\s*", "", bare).strip().strip("|").strip()
        if not fenced:
            for label in SUMMARY_KEYS:
                match = re.match(r"^" + label + r"\s*" + _COLON + r"\s*(.*)$", bare)
                if match:
                    reply.summary[label] = match.group(1).strip()
        bare = re.sub(r"^#+\s*", "", bare)
        if pending_key and bare:
            drift[pending_key] = bare
            pending_key = None
            continue
        for label, key in DRIFT_KEYS:
            prefix = r"^(?:最终|综合)?" if key == "verdict" else r"^"
            match = re.match(prefix + label + r"[^：:|]*" + _COLON + r"\s*(.*)$", bare)
            if not match:
                match = re.match(prefix + label + r"[^：:|]*\s*\|\s*(.*)$", bare)
            if match:
                # The last answer wins: a model may first restate the options.
                value = match.group(1).strip().strip("|").strip()
                if value:
                    drift[key] = value
                else:
                    pending_key = key
    if drift:
        drift["verdict_raw"] = drift.get("verdict", "")
        drift["verdict"] = _normalize_verdict(drift.get("verdict", ""))
        for key in ("question_ok", "extra_assumptions", "criterion_changed"):
            drift.setdefault(key, "")
        reply.drift = drift
    return reply


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------

@dataclass
class OpResult:
    raw: str
    status: str          # '已执行' | '已拒绝' | '记为意见'
    reason: str = ""
    entry_id: str = ""
    blocking: bool = False


SIMILAR_REJECT = 0.85    # this close to a refuted entry: not registered
SIMILAR_NOTE = 0.70      # this close: registered with a warning note
NONE_ANSWERS = ("无", "否", "没有", "未发现", "不存在", "未使用", "没用", "未见")


def similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, norm_key(a), norm_key(b)).ratio()


def is_none_answer(text: str) -> bool:
    """'无' / '否。' / '未发现锚点以外的假设' / '无 / 列出' all mean nothing to report."""
    value = re.sub(r"[\s。．.，,；;：:！!（）()]", "", text or "")
    return not value or value.startswith(NONE_ANSWERS)


def _model_families(entry: Entry, extra: str = "") -> list[str]:
    families = {f for f in [entry.family, *entry.support, extra] if f in MODEL_FAMILIES}
    order = ["openai", "google", "anthropic"]
    return [f for f in order if f in families]


def _strip_agreement(entry: Entry) -> None:
    entry.note = re.sub(r"(?:；)?已获[^；]*同意，缺证据", "", entry.note or "").strip("；")


def _agreement_note(entry: Entry) -> None:
    _strip_agreement(entry)
    families = _model_families(entry)
    if len(families) >= 2:
        _add_note(entry, "已获 " + "、".join(family_label(f) for f in families) + " 同意，缺证据")


def _add_note(entry: Entry, text: str) -> None:
    if text and text not in (entry.note or ""):
        entry.note = (entry.note + "；" if entry.note else "") + text


def _map_temp(value: str, temp_map: dict) -> str:
    for tag, real in temp_map.items():
        value = value.replace(tag, real)
    return value


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _user_decided(entry: Entry) -> bool:
    return "你的决定" in (entry.evidence or "")


def _new_entry(ledger: Ledger, etype: str, text: str, basis: str, source: str, family: str, round_label: str, note: str = "") -> Entry:
    entry = Entry(id=f"L{ledger.next:02d}", type=etype, text=text.strip(), state="待定", basis=basis, note=note,
                  source=source, updated=round_label, family=family or family_from_source(source))
    entry.sha = entry_sha(entry)
    ledger.next += 1
    ledger.entries.append(entry)
    return entry


def _check_entry(ledger: Ledger, text: str, basis: str, source: str, family: str, round_label: str) -> Entry:
    """A 待验证 entry, reused when an identical one already exists (no duplicate rows)."""
    key = norm_key(text)
    existing = next((e for e in ledger.entries if e.type == "待验证" and norm_key(e.text) == key), None)
    return existing or _new_entry(ledger, "待验证", text, basis, source, family, round_label)


def _check_basis(basis: str, ledger: Ledger, anchor: Anchor | None) -> str:
    notes = []
    for ref in re.findall(r"\b([HL]\d+)\b", basis or ""):
        if ref.startswith("H"):
            found = find_assumption(anchor, ref) if anchor else None
            if found is None or found.status == "已拒绝":
                notes.append(f"依据 {ref} 不存在")
            elif found.status == "待批":
                notes.append(f"依赖待批假设 {ref}")
        elif find(ledger, ref) is None:
            notes.append(f"依据 {ref} 不存在")
    return "；".join(dict.fromkeys(notes))


def basis_problem(entry: Entry, ledger: Ledger, anchor: Anchor | None) -> str:
    """Why this entry may not be 确定 yet because of what it rests on ('' when nothing blocks it)."""
    problems = []
    for ref in dict.fromkeys(re.findall(r"\b([HL]\d+)\b", entry.basis or "")):
        if ref.startswith("H"):
            if anchor is None:
                continue
            found = find_assumption(anchor, ref)
            if found is None:
                problems.append(f"依据的假设 {ref} 不存在")
            elif found.status == "待批":
                problems.append(f"依据的假设 {ref} 还在待批")
            elif found.status == "已拒绝":
                problems.append(f"依据的假设 {ref} 已被你拒绝")
        else:
            if ref == entry.id:
                continue
            dep = find(ledger, ref)
            if dep is None:
                problems.append(f"依据的条目 {ref} 不存在")
            elif dep.state != "确定":
                problems.append(f"依据的条目 {ref} 还是{dep.state}")
    return "；".join(problems)


def _settle(entry: Entry, ledger: Ledger, anchor: Anchor | None, token: str, round_label: str) -> tuple[str, str]:
    """Move a 待定 entry to 确定 unless its basis is not settled yet; returns (status, reason)."""
    problem = basis_problem(entry, ledger, anchor)
    if problem:
        entry.pending_token = token
        _add_note(entry, f"证据已有（{token}），{problem}，暂留待定")
        entry.updated = round_label
        return "记为意见", f"{entry.id} 的证据 {token} 已记下；{problem}，暂留待定"
    entry.state, entry.evidence, entry.updated, entry.pending_token = "确定", token, round_label, ""
    _strip_agreement(entry)
    entry.note = re.sub(r"(?:；)?证据已有（[^）]*）[^；]*暂留待定", "", entry.note or "").strip("；")
    entry.sha = entry_sha(entry)
    return "已执行", f"{entry.id} → 确定（证据 {token}）"


def recheck_dependents(ledger: Ledger, anchor: Anchor | None, round_label: str = "系统") -> list[OpResult]:
    """Promote entries whose basis became settled; demote 确定 entries whose basis no longer is.

    User decisions (evidence 你的决定) are never changed automatically.
    """
    results, changed = [], True
    while changed:
        changed = False
        for entry in ledger.entries:
            if entry.superseded_by:
                continue
            if entry.state == "待定" and entry.pending_token and not basis_problem(entry, ledger, anchor):
                token = entry.pending_token
                status, reason = _settle(entry, ledger, anchor, token, round_label)
                if status == "已执行":
                    results.append(OpResult(f"系统：{entry.id} 的依据已满足", "已执行", reason, entry.id))
                    changed = True
            elif entry.state == "确定" and not _user_decided(entry):
                problem = basis_problem(entry, ledger, anchor)
                if problem:
                    token = entry.evidence
                    entry.state, entry.evidence, entry.pending_token, entry.updated = "待定", "", token, round_label
                    _add_note(entry, f"{problem}，从确定改回待定（原证据 {token}）")
                    entry.sha = entry_sha(entry)
                    results.append(OpResult(f"系统：{entry.id} 的依据有变化", "已执行", f"{problem}，{entry.id} 改回待定", entry.id))
                    changed = True
    return results


def _accept_evidence(entry: Entry, target: str, evidence_text: str, evidence: dict) -> tuple[str, str]:
    """Return (accepted token, '') or ('', reason why the evidence does not count)."""
    runs, verifications = (evidence or {}).get("runs", {}), (evidence or {}).get("verifications", {})
    tokens = re.findall(r"\b([RV]\d+)\b", evidence_text or "")
    if not tokens:
        return "", "没有可用的证据（需要按事先写定判据通过的运行 R，或另一家模型的独立复推 V）"
    reasons = []
    for token in tokens:
        record = runs.get(token) if token.startswith("R") else verifications.get(token)
        if record and entry.counter_at and (record.get("created_at") or "") <= entry.counter_at:
            reasons.append(f"{token} 早于对 {entry.id} 提出的反例，不能用来排除反例")
            continue
        if token.startswith("R"):
            run = record
            if not run:
                reasons.append(f"找不到运行 {token}")
                continue
            want = "支持" if target == "确定" else "否定"
            if entry.id not in (run.get("targets") or {}):
                reasons.append(f"{token} 的验证计划事先没有针对 {entry.id}")
            elif run["targets"][entry.id] and run["targets"][entry.id] != entry_sha(entry):
                reasons.append(f"{entry.id} 的内容在验证计划写定后改动过，{token} 不能作为证据")
            elif run.get("outcome") != "pass":
                reasons.append(f"{token} 没有通过事先写定的判据（结果：{run.get('outcome', '未知')}）")
            elif (run.get("direction") or "支持") != want:
                reasons.append(f"{token} 的验证方向是“{run.get('direction') or '支持'}”，不能用来改为{target}")
            else:
                return token, ""
        else:
            ver = record
            if not ver:
                reasons.append(f"找不到复推记录 {token}")
                continue
            want = "一致" if target == "确定" else "不一致"
            if ver.get("entry") != entry.id:
                reasons.append(f"{token} 复推的是 {ver.get('entry') or '别的条目'}")
            elif not ver.get("entry_sha") or ver.get("entry_sha") != entry_sha(entry):
                reasons.append(f"{token} 复推的是 {entry.id} 改动前的内容，不能作为证据")
            elif ver.get("family") not in MODEL_FAMILIES or ver.get("family") == entry.family:
                reasons.append(f"{token} 与提出者同属 {family_label(entry.family)}，不算独立复推")
            elif ver.get("family") in entry.support:
                reasons.append(f"{family_label(ver.get('family'))} 此前已表示同意 {entry.id}，它的复推不算独立")
            elif ver.get("verdict") != want:
                reasons.append(f"{token} 的复推结论是“{ver.get('verdict')}”")
            else:
                return token, ""
    return "", "；".join(reasons)


def apply_ops(ledger: Ledger, anchor: Anchor | None, ops: list, *, source: str, family: str, round_label: str,
              step: str = "", evidence: dict | None = None) -> list[OpResult]:
    """Apply a model's ledger changes under the rules. Mutates ledger and anchor."""
    results = []
    ledger.last_temp_map = {}
    for op in ops:
        handler = {"+": _op_add, "~": _op_state, "?": _op_request, "!": _op_decision}.get(op.kind)
        if handler is None:
            results.append(OpResult(op.raw, "已拒绝", "无法识别这一行（台账变更只接受 + ~ ? ! 开头的行）"))
            continue
        if "……" in op.raw:
            # Lines copied from the format example in the prompt are not real changes.
            results.append(OpResult(op.raw, "已拒绝", "这是格式示例里的占位内容（……），不是真实的变更"))
            continue
        results += handler(ledger, anchor, op, source=source, family=family, round_label=round_label, step=step, evidence=evidence or {})
    results += recheck_dependents(ledger, anchor, round_label)
    return results


def _op_add(ledger, anchor, op, *, source, family, round_label, step, evidence):
    fields = [f for f in op.fields]
    if not fields or not fields[0]:
        return [OpResult(op.raw, "已拒绝", "缺少类型和内容")]
    raw_type = fields[0]
    if re.sub(r"\s", "", raw_type) in {"假设", "新假设", "前提"}:
        if anchor is None:
            return [OpResult(op.raw, "已拒绝", "这一层没有锚点，不能登记假设")]
        text = fields[1] if len(fields) > 1 else ""
        if not text.strip():
            return [OpResult(op.raw, "已拒绝", "假设内容为空")]
        return [_new_assumption(anchor, op.raw, text, fields[2] if len(fields) > 2 else "", source, op.temp, ledger)]
    etype = normalize_type(raw_type)
    if not etype:
        return [OpResult(op.raw, "已拒绝", f"类型“{raw_type}”不对（只能是 推导步、论断、待验证、想法、文献）")]
    text = _map_temp(fields[1] if len(fields) > 1 else "", ledger.last_temp_map).strip()
    if not text:
        return [OpResult(op.raw, "已拒绝", "内容为空")]
    replace = ""
    basis_parts = []
    for value in fields[2:]:
        match = re.fullmatch(r"替代\s*(L\d+|\[新\d+\])", value.strip())
        if match:
            replace = _map_temp(match.group(1), ledger.last_temp_map)
        elif value.strip():
            basis_parts.append(value.strip())
    basis = _map_temp("，".join(basis_parts), ledger.last_temp_map)
    if basis in {"无", "（无）", "-"}:
        basis = ""
    key = norm_key(text)
    duplicate = next((e for e in ledger.entries if norm_key(e.text) == key), None)
    if duplicate is not None:
        if op.temp:
            ledger.last_temp_map[op.temp] = duplicate.id
        if duplicate.state == "否定":
            return [OpResult(op.raw, "已拒绝", f"已在淘汰清单（{duplicate.id}），不要换个说法再提", duplicate.id)]
        return [OpResult(op.raw, "已拒绝", f"与 {duplicate.id} 重复", duplicate.id)]
    close = max(((similarity(text, e.text), e) for e in rejected_list(ledger)), default=(0.0, None), key=lambda pair: pair[0])
    if close[1] is not None and close[0] >= SIMILAR_REJECT:
        return [OpResult(op.raw, "已拒绝", f"与淘汰清单里的 {close[1].id} 几乎相同（相似度 {close[0]:.2f}），不登记；"
                         "确有实质区别的话，请在正文里写明区别，由你决定", close[1].id)]
    old = find(ledger, replace) if replace else None
    if old is not None and old.state == "否定":
        return [OpResult(op.raw, "已拒绝", f"{old.id} 已被否定，不能用“替代”把它换个说法再提", old.id)]
    notes = [n for n in [_check_basis(basis, ledger, anchor)] if n]
    if close[1] is not None and close[0] >= SIMILAR_NOTE:
        notes.append(f"与淘汰清单 {close[1].id} 相近（相似度 {close[0]:.2f}），请核对区别")
    if replace and old is None:
        notes.append(f"要替代的 {replace} 不存在")
    entry = _new_entry(ledger, etype, text, basis, source, family, round_label, "；".join(notes))
    if op.temp:
        ledger.last_temp_map[op.temp] = entry.id
    reason = ""
    if old is not None:
        if old.state == "待定":
            old.state, old.superseded_by, old.updated = "否定", entry.id, round_label
            _add_note(old, f"被 {entry.id} 替代")
            old.sha = entry_sha(old)
            _add_note(entry, f"替代 {old.id}")
            if old.counter_at:
                # An open counterexample follows the claim to its new wording.
                entry.counter_at = old.counter_at
                _add_note(entry, "原条目有反例待核")
                for check in ledger.entries:
                    if check.type == "待验证" and check.state == "待定" and re.search(rf"\b{old.id}\b", check.basis or ""):
                        check.basis = re.sub(rf"\b{old.id}\b", entry.id, check.basis)
            reason = f"{old.id} 已被替代"
        else:
            _add_note(entry, f"拟替代 {old.id}（{old.id} 已确定，需要证据或你的决定）")
            reason = f"{old.id} 已确定，保持不变"
    return [OpResult(op.raw, "已执行", reason or "新条目，记为待定", entry.id)]


def _new_assumption(anchor, raw, text, basis, source, temp, ledger):
    text, failure = _extract_failure(text)
    key = norm_key(text)
    existing = next((a for a in anchor.assumptions if norm_key(a.text) == key), None)
    if existing is not None:
        if temp:
            ledger.last_temp_map[temp] = existing.id
        if existing.status == "已拒绝":
            return OpResult(raw, "已拒绝", f"这条假设已被你拒绝（{existing.id}）", existing.id)
        return OpResult(raw, "已拒绝", f"与假设 {existing.id} 重复", existing.id)
    rejected = [a for a in anchor.assumptions if a.status == "已拒绝"]
    close = max(((similarity(text, a.text), a) for a in rejected), default=(0.0, None), key=lambda pair: pair[0])
    if close[1] is not None and close[0] >= SIMILAR_REJECT:
        return OpResult(raw, "已拒绝", f"与你拒绝过的假设 {close[1].id} 几乎相同", close[1].id)
    basis = re.sub(r"^依据\s*" + _COLON + r"\s*", "", basis or "").strip()
    assumption = Assumption(id=next_assumption_id(anchor), text=text, basis=basis, failure=failure, status="待批", source=source)
    anchor.assumptions.append(assumption)
    if temp:
        ledger.last_temp_map[temp] = assumption.id
    return OpResult(raw, "已执行", f"已登记为待批假设 {assumption.id}，依赖它的结论只能是待定", assumption.id)


def _op_state(ledger, anchor, op, *, source, family, round_label, step, evidence):
    fields = op.fields + [""] * (4 - len(op.fields))
    eid = _map_temp(fields[0].strip(), ledger.last_temp_map)
    entry = find(ledger, eid)
    if entry is None:
        return [OpResult(op.raw, "已拒绝", f"找不到 {eid or '条目编号'}")]
    target = normalize_state(fields[1])
    if not target:
        return [OpResult(op.raw, "已拒绝", f"新状态“{fields[1]}”不对（只能是 确定、否定、待定）", eid)]
    reason_text, evidence_text = fields[2].strip(), " ".join(f for f in fields[3:] if f).strip()
    if not evidence_text and re.fullmatch(r"\s*(?:反例\s*[:：].*|[RV]\d+(?:\s*[,，、]\s*[RV]\d+)*)\s*", reason_text or ""):
        evidence_text, reason_text = reason_text, ""
    if any(word in evidence_text for word in HUMAN_WORDS):
        return [OpResult(op.raw, "已拒绝", "只有你本人能在页面上作出决定", eid)]
    if entry.superseded_by:
        return [OpResult(op.raw, "已拒绝", f"{eid} 已被 {entry.superseded_by} 替代", eid)]
    who = family_label(family)
    counter = re.match(r"^\s*反例\s*" + _COLON + r"?\s*(.*)$", evidence_text, re.S)
    if counter and target in {"否定", "待定"}:
        detail = (counter.group(1).strip() or reason_text or "（未给出参数）")[:300]
        if entry.state == "否定":
            return [OpResult(op.raw, "记为意见", f"{eid} 已被否定；反例不会改变它，只有运行、复推或你本人能改", eid)]
        _add_note(entry, "有反例待核：" + detail)
        entry.counter_at = _now()
        check = _check_entry(ledger, f"核查反例：{detail}（针对 {eid}）", eid, source, family, round_label)
        if entry.state == "确定":
            return [OpResult(op.raw, "已执行", f"{eid} 已确定（{entry.evidence}），{who} 给出了反例：{detail}。"
                             f"已新建待验证 {check.id}；要不要重新核查 {eid}，请你决定", eid, blocking=True)]
        return [OpResult(op.raw, "已执行", f"{eid} 有反例待核；已新建待验证 {check.id}", eid)]
    if target == entry.state:
        if target == "确定" and family in MODEL_FAMILIES:
            entry.support = sorted(set(entry.support) | {family})
        return [OpResult(op.raw, "记为意见", "状态未变", eid)]
    if entry.state in {"确定", "否定"}:
        # Settled entries never move on a model's word. Contradicting evidence goes to the user.
        if _user_decided(entry):
            return [OpResult(op.raw, "记为意见", f"{eid} 是你本人的决定，模型不能改。{who} 认为应为{target}：{reason_text or evidence_text}",
                             eid, blocking=bool(re.search(r"\b[RV]\d+\b", evidence_text)))]
        token, why = _accept_evidence(entry, target, evidence_text, evidence)
        if token:
            _add_note(entry, f"{token} 与原证据 {entry.evidence} 矛盾")
            return [OpResult(op.raw, "记为意见", f"{eid} 现为{entry.state}（证据 {entry.evidence}），但 {token} 支持改为{target}。"
                             "两份证据矛盾，请你决定", eid, blocking=True)]
        return [OpResult(op.raw, "记为意见", f"{eid} 已{entry.state}，改变它需要你决定。{why}", eid)]
    token, why = _accept_evidence(entry, target, evidence_text, evidence)
    if token:
        if target == "确定":
            status, reason = _settle(entry, ledger, anchor, token, round_label)
            return [OpResult(op.raw, status, reason, eid)]
        entry.state, entry.evidence, entry.updated, entry.pending_token = "否定", token, round_label, ""
        _strip_agreement(entry)
        entry.sha = entry_sha(entry)
        return [OpResult(op.raw, "已执行", f"{eid} → 否定（证据 {token}）", eid)]
    if target == "确定" and family in MODEL_FAMILIES:
        entry.support = sorted(set(entry.support) | {family})
        _agreement_note(entry)
    return [OpResult(op.raw, "记为意见", why or "没有可用的证据", eid)]


def _op_request(ledger, anchor, op, *, source, family, round_label, step, evidence):
    if anchor is None:
        return [OpResult(op.raw, "已拒绝", "这一层没有锚点")]
    fields = op.fields + [""] * (3 - len(op.fields))
    item, proposal, reason = fields[0].strip(), fields[1].strip(), " | ".join(f for f in fields[2:] if f).strip()
    if not item or not proposal:
        return [OpResult(op.raw, "已拒绝", "锚点变更申请要写清锚点项和申请改为什么")]
    if re.fullmatch(r"H\d+", item):
        target = find_assumption(anchor, item)
        if target is None:
            return [OpResult(op.raw, "已拒绝", f"假设 {item} 不存在")]
        if target.status == "已拒绝":
            return [OpResult(op.raw, "已拒绝", f"{item} 已被你拒绝，不能申请修改；有新依据的话请提成新的假设", item)]
    elif slot_key(item) == "assumptions":
        return [_new_assumption(anchor, op.raw, proposal, reason, source, "", ledger)]
    duplicate = next((r for r in anchor.requests if r.status == "待处理" and r.slot == item and norm_key(r.proposal) == norm_key(proposal)), None)
    if duplicate is not None:
        return [OpResult(op.raw, "已拒绝", f"已有相同的申请 {duplicate.id}", duplicate.id)]
    key = slot_key(item)
    slot = item if re.fullmatch(r"H\d+", item) else (slot_labels(anchor.level).get(key, item) if key else item)
    request = ChangeRequest(id=next_request_id(anchor), slot=slot, proposal=proposal, reason=reason, source=source,
                            note="" if key or re.fullmatch(r"H\d+", item) else "未知锚点项")
    anchor.requests.append(request)
    return [OpResult(op.raw, "已执行", f"已登记锚点变更申请 {request.id}，等你批准", request.id, blocking=True)]


def _op_decision(ledger, anchor, op, *, source, family, round_label, step, evidence):
    text = (op.fields[0] if op.fields else "").strip()
    if not text:
        return [OpResult(op.raw, "已拒绝", "内容为空")]
    return [OpResult(op.raw, "已执行", text, blocking=True)]


def user_set_state(ledger: Ledger, eid: str, state: str, note: str = "", round_label: str = "你", anchor: Anchor | None = None) -> list[OpResult]:
    entry = find(ledger, eid)
    if entry is None:
        raise ValueError(f"找不到条目 {eid}")
    target = normalize_state(state)
    if not target:
        raise ValueError("状态只能是 确定、否定、待定")
    previous = entry.state
    entry.state, entry.updated, entry.pending_token = target, round_label, ""
    entry.evidence = "你的决定" if target in {"确定", "否定"} else ""
    if target != "待定":
        entry.counter_at = ""
        entry.note = re.sub(r"(?:；)?有反例待核：[^；]*", "", entry.note or "").strip("；")
    _strip_agreement(entry)
    _add_note(entry, ("你：" + note.strip()) if note.strip() else ("你改回待定" if target == "待定" else ""))
    entry.sha = entry_sha(entry)
    return [OpResult(f"你：{eid} {previous} → {target}", "已执行", note.strip(), eid)] + recheck_dependents(ledger, anchor, round_label)


def apply_run(ledger: Ledger, *, run_id: str, outcome: str, direction: str, targets: dict, round_label: str,
              anchor: Anchor | None = None, created_at: str = "") -> list[OpResult]:
    """System action after a verification run: only passing, bound runs change states."""
    results = []
    direction = "否定" if direction == "否定" else "支持"
    for eid, sha in (targets or {}).items():
        entry = find(ledger, eid)
        raw = f"系统：{run_id}（{outcome}，方向 {direction}）→ {eid}"
        if entry is None:
            results.append(OpResult(raw, "已拒绝", f"{eid} 不存在"))
            continue
        if entry.superseded_by:
            results.append(OpResult(raw, "记为意见", f"{eid} 已被 {entry.superseded_by} 替代", eid))
            continue
        if outcome != "pass":
            if direction == "否定" and outcome == "fail":
                _add_note(entry, f"{run_id} 没有否定它（杀测未触发）")
                results.append(OpResult(raw, "记为意见", "杀测没有触发，状态不变", eid))
            else:
                label = {"fail": "未达判据", "partial": "部分满足", "error": "运行出错"}.get(outcome, "未完成")
                _add_note(entry, f"{run_id} {label}，待分析")
                results.append(OpResult(raw, "记为意见", f"{run_id} {label}，状态不变", eid))
            continue
        if sha and sha != entry_sha(entry):
            results.append(OpResult(raw, "记为意见", f"{eid} 的内容在验证计划写定后改动过，{run_id} 不能作为证据", eid))
            continue
        if entry.counter_at and created_at and created_at <= entry.counter_at:
            results.append(OpResult(raw, "记为意见", f"{run_id} 的计划早于对 {eid} 提出的反例，不能用来排除反例", eid))
            continue
        target = "确定" if direction == "支持" else "否定"
        if entry.state == target:
            if target == "确定" and not _user_decided(entry):
                entry.evidence = ", ".join(dict.fromkeys([*(x.strip() for x in entry.evidence.split(",") if x.strip()), run_id]))
            results.append(OpResult(raw, "已执行", f"{eid} 已是{target}，补充证据 {run_id}", eid))
            continue
        if entry.state != "待定":
            _add_note(entry, f"{run_id} 与原证据 {entry.evidence} 矛盾")
            results.append(OpResult(raw, "记为意见", f"{eid} 现为{entry.state}（证据 {entry.evidence}），但 {run_id} 按判据支持改为{target}。"
                                    "两份证据矛盾，请你决定", eid, blocking=True))
            continue
        if target == "确定":
            status, reason = _settle(entry, ledger, anchor, run_id, round_label)
            results.append(OpResult(raw, status, reason, eid))
        else:
            entry.state, entry.evidence, entry.updated, entry.pending_token = "否定", run_id, round_label, ""
            _strip_agreement(entry)
            entry.sha = entry_sha(entry)
            results.append(OpResult(raw, "已执行", f"{eid} → 否定（证据 {run_id}）", eid))
    return results + recheck_dependents(ledger, anchor, round_label)


# ---------------------------------------------------------------------------
# Document sections
# ---------------------------------------------------------------------------

@dataclass
class _Heading:
    index: int
    level: int
    text: str


def _doc_headings(lines: list[str], min_level: int = 2, max_level: int = 4) -> list[_Heading]:
    out = []
    for index, line, fenced in iter_lines_outside_fences("\n".join(lines)):
        if fenced:
            continue
        match = _HEADING.match(line)
        if match and min_level <= len(match.group(1)) <= max_level:
            out.append(_Heading(index, len(match.group(1)), match.group(2).strip()))
    return out


def _heading_number(text: str) -> str:
    match = re.match(r"^(\d+(?:\.\d+)*)\.?(?=[\s、．]|$)", (text or "").strip())
    return match.group(1) if match else ""


def _norm_title(text: str) -> str:
    value = re.sub(r"^\s*\d+(?:\.\d+)*\.?", "", text or "")
    return re.sub(r"[\s，,。．.、：:（）()【】*#]", "", value)


def _span_end(lines: list[str], heads: list[_Heading], target: _Heading) -> int:
    for h in heads:
        if h.index > target.index and h.level <= target.level:
            return h.index
    return len(lines)


def _trim(lines: list[str]) -> list[str]:
    start, end = 0, len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return lines[start:end]


def _body_lines(body: str, number: str, title: str) -> list[str]:
    lines = _trim((body or "").split("\n"))
    if lines:
        match = _HEADING.match(lines[0])
        if match:
            heading = match.group(2).strip()
            same = (number and _heading_number(heading) == number) or (title and _norm_title(heading) == title)
            if same:
                lines = _trim(lines[1:])
    return lines


def _replace_span(lines: list[str], start: int, end: int, body: list[str]) -> list[str]:
    before = lines[:start + 1]
    after = lines[end:]
    middle = [""] + body + [""] if body else [""]
    return before + middle + after


def _append_span(lines: list[str], start: int, end: int, body: list[str]) -> list[str]:
    section = _trim(lines[start:end])
    return lines[:start] + section + [""] + body + [""] + lines[end:]


def _finish(lines: list[str]) -> str:
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip("\n") + "\n"


def _apply_one(text: str, rev: Revision, protected: dict) -> tuple[str, dict]:
    result = {"section": rev.section, "mode": rev.mode, "status": "已应用", "reason": ""}
    lines = (text or "").split("\n")
    heads = _doc_headings(lines)
    key = (rev.section or "").strip()
    number = _heading_number(key) or (_NUMBER.match(key).group(1) if _NUMBER.match(key) else "")
    title = _norm_title(key)
    target = next((h for h in heads if number and _heading_number(h.text) == number), None)
    if target is None and title:
        target = next((h for h in heads if _norm_title(h.text) == title), None)
    if target is None and title and not number:
        target = next((h for h in heads if len(title) >= 2 and (title in _norm_title(h.text) or (len(_norm_title(h.text)) >= 2 and _norm_title(h.text) in title))), None)
    top = (_heading_number(target.text) if target else number).split(".")[0]
    if top and top in protected:
        reason = protected[top]
        if reason == "__stage__":
            return text, {**result, "status": "待你确认", "reason": "__stage__"}
        return text, {**result, "status": "已拒绝", "reason": reason}
    body = _body_lines(rev.body, number or (_heading_number(target.text) if target else ""), title or (_norm_title(target.text) if target else ""))
    target_level = target.level if target is not None else (2 if "." not in (number or "") else number.count(".") + 2)
    for _, line, fenced in iter_lines_outside_fences("\n".join(body)):
        heading = None if fenced else _HEADING.match(line)
        if not heading:
            continue
        inner_top = _heading_number(heading.group(2)).split(".")[0]
        if inner_top and inner_top in protected and inner_top != top:
            return text, {**result, "status": "已拒绝", "reason": f"修订内容里含有第 {inner_top} 节的标题，这一节不能由 AI 修改"}
        if len(heading.group(1)) <= target_level:
            return text, {**result, "status": "已拒绝", "reason": "修订内容里含有与本节同级或更高级的标题，会改动其他小节；请一个修订块只改一节"}
    if target is not None:
        end = _span_end(lines, heads, target)
        lines = _replace_span(lines, target.index, end, body) if rev.mode == "替换" else _append_span(lines, target.index, end, body)
        return _finish(lines), result
    # The section does not exist yet: create it under its parent (or at the end).
    parent = None
    if number and "." in number:
        parent_number = number.rsplit(".", 1)[0]
        parent = next((h for h in heads if _heading_number(h.text) == parent_number), None)
    if parent is not None:
        end = _span_end(lines, heads, parent)
        new_heading = "#" * min(parent.level + 1, 6) + " " + key
        lines = _trim(lines[:end]) + ["", new_heading, ""] + body + [""] + lines[end:]
    else:
        lines = _trim(lines) + ["", "## " + key, ""] + body
    return _finish(lines), {**result, "reason": "原文没有这一节，已新建"}


def apply_revisions(doc_text: str, revisions: list, *, protected: dict | None = None) -> tuple[str, list[dict]]:
    text, results = doc_text or "", []
    for rev in revisions:
        text, result = _apply_one(text, rev, protected or {})
        results.append(result)
    return text, results


def set_section(doc_text: str, number: str, heading: str, body: str) -> str:
    """Create or replace a top-level section '## <number> <heading>' (used for section 1 and 5).

    Extra sections with the same number (for example injected by an earlier revision) are removed.
    """
    lines = (doc_text or "").split("\n")
    heads = _doc_headings(lines, 2, 2)
    matches = [h for h in heads if _heading_number(h.text) == number]
    if len(matches) > 1:
        all_heads = _doc_headings(lines)
        for extra in reversed(matches[1:]):
            lines = lines[:extra.index] + lines[_span_end(lines, all_heads, extra):]
            all_heads = _doc_headings(lines)
        heads = _doc_headings(lines, 2, 2)
    target = next((h for h in heads if _heading_number(h.text) == number), None)
    if target is None:
        target = next((h for h in heads if _norm_title(h.text) == _norm_title(heading)), None)
    body_lines = _trim((body or "").split("\n"))
    if target is not None:
        all_heads = _doc_headings(lines)
        return _finish(_replace_span(lines, target.index, _span_end(lines, all_heads, target), body_lines))
    wanted = tuple(int(x) for x in number.split("."))
    later = next((h for h in heads if _heading_number(h.text) and tuple(int(x) for x in _heading_number(h.text).split(".")) > wanted), None)
    block = [f"## {number} {heading}", ""] + body_lines + [""]
    if later is not None:
        return _finish(lines[:later.index] + block + lines[later.index:])
    return _finish(_trim(lines) + [""] + block)


SKELETONS = {
    "question": ["摘要", "1 问题与假设", "2 理论推导", "3 验证方法", "4 结果与分析", "5 结论与适用范围"],
    "intro": ["1 研究背景与意义", "2 国内外研究现状", "3 存在的问题", "4 本文总体方案与技术路线", "5 主要创新点", "6 论文结构"],
    "theory": ["1 引言", "2 符号与假设", "3 主要命题与推导", "4 数值核对", "5 本章小结（交给后续各章：符号表、假设表、误差模型）"],
    "research": ["1 引言（缺口与本章贡献）", "2 方法", "3 理论分析", "4 实验设计（评测协议）", "5 结果与分析", "6 讨论与局限", "7 本章小结"],
    "conclusion": ["1 全文结论", "2 主要创新点", "3 局限", "4 展望"],
    "other": ["1 引言", "2 正文", "3 本章小结"],
}


def skeleton(level: str, role: str = "") -> str:
    if level == "question":
        names = SKELETONS["question"]
    elif level == "chapter":
        names = SKELETONS.get("research" if role == "framework" else role, SKELETONS["other"])
    else:
        return ""
    return "\n\n".join("## " + name for name in names) + "\n"


# ---------------------------------------------------------------------------
# Result placeholders
# ---------------------------------------------------------------------------

PLACEHOLDER = re.compile(r"\{\{\s*(R\d+)\.([^\s{}|]+?)\s*(?:\|\s*([^{}]*?)\s*)?\}\}")
RESULT_LINK = re.compile(r"\[((?:\\\]|[^\]])*)\]\(([^)\s]+)\s+\"(R\d+)\.([^\"]+)\"\)")


def format_value(value, spec: str = "") -> str:
    if isinstance(value, bool):
        return "是" if value else "否"
    if spec:
        try:
            return format(value, spec)
        except (TypeError, ValueError):
            pass
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return f"{value:.6g}"
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return "null" if value is None else str(value)


def resolve_placeholders(text: str, lookup) -> tuple[str, list[str]]:
    """Replace {{R01.key}} / {{R01.a.b|.3e}} with links to the run's summary.json."""
    missing, out = [], []

    def replace(match):
        rid, key, spec = match.group(1), match.group(2), (match.group(3) or "").strip()
        found = lookup(rid, key)
        if found is None:
            missing.append(f"{rid}.{key}")
            return match.group(0)
        value, href = found
        shown = format_value(value, spec).replace("[", "\\[").replace("]", "\\]")
        return f'[{shown}]({href} "{rid}.{key}")'

    for _, line, fenced in iter_lines_outside_fences(text or ""):
        out.append(line if fenced else PLACEHOLDER.sub(replace, line))
    return "\n".join(out), list(dict.fromkeys(missing))


def check_result_links(text: str, lookup) -> list[dict]:
    """Re-check already rendered result links against the current summary.json values."""
    checks = []
    for match in RESULT_LINK.finditer(text or ""):
        shown, rid, key = match.group(1).replace("\\]", "]").replace("\\[", "["), match.group(3), match.group(4)
        found = lookup(rid, key)
        if found is None:
            checks.append({"key": f"{rid}.{key}", "shown": shown, "actual": None, "ok": False})
            continue
        actual = found[0]
        try:
            ok = abs(float(shown) - float(actual)) <= 1e-12 + 1e-3 * abs(float(actual))
        except (TypeError, ValueError):
            ok = shown == format_value(actual)
        checks.append({"key": f"{rid}.{key}", "shown": shown, "actual": actual, "ok": ok})
    return checks
