"""Layered AI discussion rounds (v2).

One loop for every level: anchor (user confirms) -> round (models run) ->
brief (user skims) -> adoption (user decides). A *session* is one start
request covering 1-5 rounds; each round is a folder under the item's
``讨论/`` directory holding every prompt, raw reply, the brief and a
machine-readable ``round.json``. Models never write research files directly:
this module parses their replies and applies the ledger and revision rules
from ``ledger.py``. Model calls never hold the store lock.
"""
from __future__ import annotations

import base64
import json
import re
import shutil
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from . import agents
from . import ledger as L
from . import round_prompts as P
from .store import CHAPTER_ROLES, CONTENT_ROOT, FRAMEWORK_ROOT, ConflictError, now, sha

FAMILY_NAME = {"openai": "GPT", "google": "Gemini", "anthropic": "Claude", "human": "你", "system": "系统", "unknown": "模型"}
ROLE_LABELS = {"author": "作者", "reviewer": "审查", "engineer": "工程", "writer": "写作", "drift": "漂移检查", "referee": "审稿"}
RECIPE_LABELS = {
    "open": "收敛并起草/更新锚点", "advance": "深入推导与代码验证", "referee": "请审稿人独立盲审", "explain": "解释这一步",
    "outline": "深入讨论本章并拆解小问题", "innov": "创新探索与验证（当前阶段）", "write": "撰写本章正文", "assemble": "汇总小问题组装本章草稿",
    "propose": "AI 拟定候选框架（按边界与方向）", "import_fw": "导入已有框架/讨论稿（AI 自动解析）", "refine": "深入讨论与调整本框架",
}
STAGES = [("S1", "量化缺口"), ("S2", "发散想法"), ("S3", "可行性筛选"), ("S4", "查新"), ("S5", "设计杀测"), ("S6", "方法化")]
STAGE_TEMPLATES = {"S1": ("innov_s1", "author"), "S2": ("innov_s2", "writer"), "S3": ("innov_s3", "author"),
                   "S4": ("innov_s4", "author"), "S5": ("innov_s5", "author"), "S6": ("innov_s6", "author")}
DEFAULT_CHAPTERS = [("绪论", "intro"), ("理论基础", "theory"), ("研究问题一", "research"), ("研究问题二", "research"),
                    ("研究问题三", "research"), ("随框架而定的一章", "framework"), ("总结与展望", "conclusion")]
MAX_DOC_CHARS = 80_000
CODE_SUFFIXES = {".py", ".m", ".jl", ".r", ".R", ".ipynb"}
FW_FIELDS = {"论文主线": "mainline", "章节安排": "chapters", "优点与风险": "risks", "mainline": "mainline", "chapters": "chapters", "risks": "risks"}


class Cancelled(Exception):
    pass


class StepFailed(Exception):
    def __init__(self, message, error_class="error"):
        super().__init__(message)
        self.error_class = error_class


def stamp():
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clip(text, limit):
    text = text or ""
    return text if len(text) <= limit else text[:limit] + f"\n\n（以下省略 {len(text) - limit} 字）"


def family_label(family):
    return FAMILY_NAME.get(family or "unknown", "模型")


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------

class Item:
    """A framework layer, framework, chapter or question with its workflow files."""

    def __init__(self, store, ref, fw="", record=None):
        self.store, self.ref, self.fw = store, ref, fw or ""
        self.record = None
        if ref == "FW":
            self.level, self.anchor_level = "framework_layer", "framework_layer"
            self.dir = store._safe(FRAMEWORK_ROOT)
            self.doc_path = None
        elif re.fullmatch(r"F\d{2,}", ref or ""):
            self.record = record or store._find(store._frameworks(), ref, "框架")
            self.level, self.anchor_level = "framework", "framework_layer"
            self.dir = store._safe(FRAMEWORK_ROOT)
            self.doc_path = store._safe(self.record["path"])
        elif re.fullmatch(r"C\d{2,}", ref or ""):
            self.record = record or store._content(ref)
            self.level, self.anchor_level = "chapter", "chapter"
            self.dir = store._safe(self.record["path"]).parent
            self.doc_path = self.dir / "草稿.md"
        elif re.fullmatch(r"C\d{2,}-Q\d{2,}", ref or ""):
            # A caller that already scanned all questions may pass the record
            # to avoid re-reading every question file.
            self.record = record or store._question(ref)
            self.level, self.anchor_level = "question", "question"
            self.dir = store._qdir(self.record)
            self.doc_path = self.dir / "推导笔记.md"
        else:
            raise ValueError("无法识别的条目编号")

    # workflow files -------------------------------------------------------
    @property
    def anchor_path(self):
        return self.dir / "锚点.md"

    @property
    def ledger_path(self):
        return self.dir / "台账.md"

    @property
    def log_path(self):
        return self.dir / "台账日志.md"

    @property
    def rounds_dir(self):
        return self.dir / "讨论"

    @property
    def state_path(self):
        return self.dir / "讨论" / "状态.json"

    @property
    def verify_dir(self):
        return self.dir / "复推"

    @property
    def title(self):
        if self.ref == "FW":
            return "论文框架"
        return self.record["title"]

    def rel(self, path):
        return self.store._rel(path)

    def read(self, path):
        path = self.store._safe(path)
        return self.store._read(path) if path.is_file() else ""

    def write(self, path, text):
        with self.store.lock:
            self.store._write_text(path, text)

    # anchor / ledger -------------------------------------------------------
    def anchor(self):
        text = self.read(self.anchor_path)
        return (L.parse_anchor(text, self.anchor_level) if text.strip() else L.empty_anchor(self.anchor_level)), self.store._revision(self.anchor_path)

    def save_anchor(self, anchor):
        anchor.updated_at = utc()
        self.write(self.anchor_path, L.render_anchor(anchor))

    def ledger(self):
        text = self.read(self.ledger_path)
        return (L.parse_ledger(text) if text.strip() else L.empty_ledger()), self.store._revision(self.ledger_path)

    def save_ledger(self, ledger):
        self.write(self.ledger_path, L.render_ledger(ledger))

    def log(self, heading, results):
        if not results:
            return
        with self.store.lock:
            old = self.read(self.log_path) or "# 台账日志\n\n本文件只追加：记录每条台账变更的原文、处理结果和理由。\n"
            self.store._write_text(self.log_path, L.append_log(old, heading, results))

    def state(self):
        try:
            return json.loads(self.read(self.state_path) or "{}")
        except ValueError:
            return {}

    def save_state(self, data):
        with self.store.lock:
            self.store._atomic(self.state_path, json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8"), history=False)

    # document ----------------------------------------------------------------
    def doc_text(self):
        if self.level == "framework":
            rec = self.store._find(self.store._frameworks(), self.ref, "框架")
            return "\n\n".join(f"## {label}\n\n{rec.get(key, '')}" for label, key in (("论文主线", "mainline"), ("章节安排", "chapters"), ("优点与风险", "risks")))
        if self.doc_path is None:
            return ""
        return self.read(self.doc_path)

    def chapter_role(self):
        if self.level != "chapter":
            return ""
        return role_of(self.record)

    def question_content(self):
        if self.level != "question":
            return None
        return self.store._content(self.record["content_id"])


def role_of(content):
    role = content.get("role")
    if role in CHAPTER_ROLES:
        return role
    title = content.get("title", "")
    if "绪论" in title or "引言" in title:
        return "intro"
    if "理论" in title:
        return "theory"
    if "总结" in title or "展望" in title:
        return "conclusion"
    if "研究问题" in title:
        return "research"
    return "other"


def chapter_number(store, cid, fw=""):
    frameworks = store._frameworks()
    framework = next((f for f in frameworks if f["id"] == fw), None) or next((f for f in frameworks if f["status"] == "active"), None)
    if framework and cid in framework["content_ids"]:
        return framework["content_ids"].index(cid) + 1, framework["id"]
    for f in frameworks:
        if cid in f["content_ids"]:
            return f["content_ids"].index(cid) + 1, f["id"]
    return None, ""


# ---------------------------------------------------------------------------
# Prompt context
# ---------------------------------------------------------------------------

def ever_confirmed(anchor):
    """True once the user has confirmed this anchor at least once (later edits put it back to draft)."""
    return bool(anchor is not None and (anchor.confirmed_at or anchor.version > 1))


def anchor_prompt(anchor):
    if anchor is None:
        return "（尚未建立锚点）"
    labels = dict(L.ANCHOR_SLOTS[anchor.level]) if hasattr(L, "ANCHOR_SLOTS") else {}
    lines = [f"锚点状态：{'已确认' if anchor.status == 'confirmed' else '草稿（尚未确认）'}，第 {anchor.version} 版"]
    for key in ("question", "criterion"):
        lines.append(f"{labels.get(key, key)}：{anchor.slots.get(key, '').strip() or '（空）'}")
    lines.append(labels.get("assumptions", "假设") + "：")
    if not anchor.assumptions:
        lines.append("- （无）")
    for a in anchor.assumptions:
        if a.status == "已拒绝":
            continue
        tag = "已确认" if a.status == "已确认" else "待批，依赖它的结论只能是待定"
        lines.append(f"- {a.id} {a.text}（依据：{a.basis or '未写'}；失效条件：{a.failure or '未写'}）[{tag}]")
    for key in ("scope", "nongoals"):
        lines.append(f"{labels.get(key, key)}：{anchor.slots.get(key, '').strip() or '（空）'}")
    pending = [r for r in anchor.requests if r.status == "待处理"]
    if pending:
        lines.append("尚未处理的锚点变更申请：" + "；".join(f"{r.id} {r.slot} → {r.proposal}" for r in pending))
    return "\n".join(lines)


def ledger_prompt(ledger, changed=(), notes=True):
    """Ledger as prompt text. notes=False hides notes such as '已获 GPT、Gemini 同意' (reviewer, drift, referee)."""
    rows = [e for e in ledger.entries if e.state != "否定"]
    if not rows:
        return "（台账为空）"
    out = []
    for e in rows:
        mark = "＊" if e.id in changed else ""
        note = e.note if notes else ""
        extra = "；".join(x for x in (f"依据 {e.basis}" if e.basis else "", f"证据 {e.evidence}" if e.evidence else "", note) if x)
        out.append(f"{mark}{e.id} [{e.type}|{e.state}] {e.text}" + (f"（{extra}）" if extra else ""))
    return "\n".join(out)


def neutralize_comments(text):
    """Model text must not hide anything from the user: '<!--' becomes visible, inert text."""
    return (text or "").replace("<!--", "&lt;!--")


def render_blocks(reply):
    """What the author will actually change, rebuilt from the parsed reply (never the raw prose)."""
    parts = []
    for rev in reply.revisions:
        parts.append(f"````正文修订\n节：{rev.section}\n方式：{rev.mode}\n{rev.body}\n````")
    if reply.ops:
        parts.append("```台账变更\n" + "\n".join(op.raw for op in reply.ops) + "\n```")
    for plan in reply.plans:
        parts.append("```验证计划\n" + "\n".join(f"{k}：{v}" for k, v in plan.items()) + "\n```")
    if reply.questions:
        lines = []
        for q in reply.questions:
            lines += [f"### {q.get('title', '')}", f"问题：{q.get('question', '')}", f"判据：{q.get('criterion', '')}"]
        parts.append("```小问题清单\n" + "\n".join(lines) + "\n```")
    for md in reply.candidates:
        parts.append("```候选框架\n" + md + "\n```")
    return "\n\n".join(parts) or "（作者本轮没有可识别的改动）"


def rejected_prompt(ledger):
    rows = L.rejected_list(ledger)
    if not rows:
        return "（淘汰清单为空）"
    return "\n".join(f"{e.id} [{e.type}] {e.text}（否定依据：{e.evidence or e.note or '你的决定'}）" for e in rows)


def project_prompt(store):
    p = store._project()
    parts = []
    for key, label in (("brief", "简介"), ("background", "研究背景"), ("progress", "目前进度"), ("configuration", "系统配置"), ("symbols", "符号约定"), ("data_sources", "已有数据"), ("rules", "统一要求")):
        value = (p.get(key) or "").strip()
        if value:
            parts.append(f"【{label}】\n{clip(value, 4000)}")
    return f"项目：{p['title']}\n" + ("\n\n".join(parts) if parts else "（项目卡尚未填写）")


def strip_prose(text):
    """Keep only what will be applied (for reviewers: no persuasion, no summaries)."""
    return clip(render_blocks(L.parse_reply(text)), 30_000)


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

class Session:
    def __init__(self, data):
        self.id = "s-" + uuid.uuid4().hex[:12]
        self.request_id = data.get("request_id", "")
        self.ref = data["ref"]
        self.fw = data.get("fw", "") or ""
        self.recipe = data["recipe"]
        self.mode = data.get("mode", "auto")
        self.rounds_planned = data["rounds"]
        self.focus = data.get("focus", "")
        self.target = data.get("target", "")
        self.status = "queued"
        self.round_index = 0
        self.round = None
        self.steps = []
        self.learn = None
        self.stop_reason = ""
        self.message = "排队中"
        self.created_at = utc()
        self.ended_at = ""
        self.roles = {}
        self.cancel = threading.Event()
        self.decision = threading.Event()
        self.decision_value = None
        self.thread = None

    def snapshot(self):
        return {"id": self.id, "ref": self.ref, "fw": self.fw, "recipe": self.recipe, "recipe_label": RECIPE_LABELS.get(self.recipe, self.recipe),
                "mode": self.mode, "status": self.status, "rounds_planned": self.rounds_planned, "round_index": self.round_index,
                "round": self.round, "steps": self.steps, "learn": self.learn, "stop_reason": self.stop_reason, "message": self.message,
                "created_at": self.created_at, "ended_at": self.ended_at,
                "roles": {k: {"label": v.get("label", ""), "family": v.get("family", "")} for k, v in self.roles.items()}}


class RoundManager:
    """Runs at most one session at a time, on a background thread."""

    def __init__(self, store, repo=None):
        self.store = store
        self.repo = repo
        self.settings = agents.Settings(store.root)
        self.usage = agents.UsageLedger(store.root)
        self.lock = threading.RLock()
        self.sessions = {}
        self.by_request = {}
        self.base = store._safe(".runtime/v2/sessions")
        self.base.mkdir(parents=True, exist_ok=True)
        files = sorted(self.base.glob("s-*.json"), key=lambda p: p.stat().st_mtime)
        for path in files:
            try:
                snap = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if snap.get("status") in {"queued", "running", "paused"}:
                reason = "服务已重启，本次讨论停止。已完成的步骤和文件都已保存。"
                snap.update(status="stopped", stop_reason=reason, message="已停止")
                path.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
                self._close_interrupted(snap, reason)
            if path in files[-50:]:
                self.sessions[snap["id"]] = snap

    def _close_interrupted(self, snap, reason):
        """After a restart: mark the interrupted round stopped, write a short brief, end pending runs."""
        try:
            round_dir = self.store._safe((snap.get("round") or {}).get("dir", ""))
        except (PermissionError, ValueError):
            return
        meta = round_dir / "round.json"
        if not meta.is_file():
            return
        try:
            rnd = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if rnd.get("status") == "running":
            rnd.update(status="stopped", stop_reason=reason, ended_at=utc())
            with self.store.lock:
                self.store._atomic(meta, json.dumps(rnd, ensure_ascii=False, indent=1).encode("utf-8"), history=False)
                if not (round_dir / "简报.md").exists():
                    self.store._write_text(round_dir / "简报.md", f"# 第{rnd.get('no', '?')}轮简报\n\n- 停下的原因：{reason}\n- 已完成的步骤和原始回复都在本轮目录里。\n")
        ref = rnd.get("ref", "")
        if re.fullmatch(r"C\d{2,}-Q\d{2,}", ref or ""):
            try:
                q = self.store._question(ref)
                for run in self.store._records(q, "结果", "run", "R"):
                    if run.get("outcome") == "pending":
                        self.store.perform("update_run", {"question_id": ref, "id": run["id"], "revision": run["revision"], "outcome": "error",
                                                          "actual": "服务重启时这次运行还没有完成；计划保留，下一轮会重新排队。"})
            except (FileNotFoundError, ValueError, ConflictError):
                pass

    # public API ------------------------------------------------------------
    def start(self, data):
        ref = data.get("ref", "")
        item = Item(self.store, ref, data.get("fw", ""))
        recipe = data.get("recipe") or self.default_recipe(item)
        if recipe not in self.recipes(item):
            raise ValueError("这一层没有这种讨论方式")
        settings, _ = self.settings.load()
        rounds = data.get("rounds") or settings["limits"].get("rounds_default", 2)
        if not isinstance(rounds, int) or not 1 <= rounds <= 5:
            raise ValueError("轮数应在 1 到 5 之间")
        if recipe in {"open", "referee", "propose", "import_fw", "write", "assemble", "innov"}:
            rounds = 1
        mode = data.get("mode", "auto")
        if mode not in {"auto", "learn"}:
            raise ValueError("模式只能是自动或学习")
        if mode == "learn" and recipe != "advance":
            mode = "auto"
        focus = data.get("focus", "")
        max_focus = 24000 if recipe == "import_fw" else 4000
        if not isinstance(focus, str) or len(focus) > max_focus:
            raise ValueError(f"本轮重点应为 {max_focus} 字以内的文字")
        if recipe == "import_fw" and not focus.strip():
            raise ValueError("请先在“本轮重点”里粘贴你已有的框架大纲或讨论记录，再开始导入")
        request_id = data.get("request_id", "")
        with self.lock:
            if request_id and request_id in self.by_request:
                return self.snapshot(self.by_request[request_id])
            active = self.active_session()
            if active:
                raise ConflictError(f"已有一次讨论正在进行（{active['ref']}），请等它结束或取消。")
            anchor, _ = item.anchor()
            if recipe == "advance" and anchor.status != "confirmed":
                if ever_confirmed(anchor):
                    raise ValueError("锚点有改动，请检查后点“确认锚点”，再继续讨论。")
                raise ValueError("锚点还没确认。请先“收敛并起草/更新锚点”，改好后点“确认锚点”。")
            if recipe in {"outline", "innov", "propose", "refine"} and anchor.status != "confirmed" and ever_confirmed(anchor):
                raise ValueError("锚点有改动，请检查后点“确认锚点”，再继续讨论。")
            open_decisions = [d for d in item.state().get("decisions", []) if d.get("status") == "待处理"]
            if recipe in {"advance", "outline", "innov", "propose", "import_fw", "refine"} and (L.anchor_blocking(anchor) or open_decisions):
                raise ValueError("还有锚点变更申请或需要你决定的事项，请先在“待你决定”里处理，再继续讨论。")
            if recipe == "refine" and item.level != "framework":
                raise ValueError("请在具体的框架页面上修改框架")
            session = Session({**data, "ref": ref, "recipe": recipe, "rounds": rounds, "mode": mode, "focus": focus})
            self.sessions[session.id] = session
            if request_id:
                self.by_request[request_id] = session.id
            self._persist(session)
            session.thread = threading.Thread(target=self._run_session, args=(session,), daemon=True, name="v2-" + session.id)
            session.thread.start()
            return session.snapshot()

    def cancel(self, sid):
        with self.lock:
            session = self.sessions.get(sid)
            if not isinstance(session, Session):
                raise FileNotFoundError("找不到这次讨论")
            session.cancel.set()
            session.decision.set()
            session.message = "正在取消…"
            return session.snapshot()

    def learn(self, sid, decision, note=""):
        with self.lock:
            session = self.sessions.get(sid)
            if not isinstance(session, Session) or session.status != "paused":
                raise ValueError("这次讨论没有在等你的决定")
            if decision not in {"continue", "ask", "reject"}:
                raise ValueError("请选择继续、追问或打回")
            if decision == "reject" and not (note or "").strip():
                raise ValueError("打回时请写一句理由，会带进下一轮")
            session.decision_value = (decision, note or "")
            # Leave the paused state at once, so a poll right after the click sees it.
            session.status = "running"
            session.message = {"continue": "继续…", "ask": "追问中…", "reject": "已打回"}[decision]
            session.decision.set()
            return session.snapshot()

    def snapshot(self, sid):
        with self.lock:
            session = self.sessions.get(sid)
            if session is None:
                raise FileNotFoundError("找不到这次讨论")
            return session.snapshot() if isinstance(session, Session) else session

    def active_session(self):
        with self.lock:
            for session in self.sessions.values():
                if isinstance(session, Session) and session.status in {"queued", "running", "paused"}:
                    return session.snapshot()
        return None

    def close(self):
        with self.lock:
            sessions = [s for s in self.sessions.values() if isinstance(s, Session)]
        for session in sessions:
            session.cancel.set()
            session.decision.set()
        for session in sessions:
            if session.thread:
                session.thread.join(timeout=15)

    # recipes ------------------------------------------------------------------
    def recipes(self, item):
        if item.level == "question":
            return ["open", "advance", "referee"]
        if item.level == "chapter":
            role = item.chapter_role()
            keys = ["outline"]
            if role == "research":
                keys = ["innov", "outline"]
            if role in {"intro", "conclusion"}:
                keys.append("write")
            keys += ["open", "assemble", "referee"]
            return keys
        if item.level == "framework":
            return ["refine"]
        return ["propose", "import_fw", "open"]

    def default_recipe(self, item):
        anchor, _ = item.anchor()
        if item.level == "framework":
            return "refine"
        if anchor.status != "confirmed" and not ever_confirmed(anchor):
            return "open"
        if item.level == "question":
            return "advance"
        if item.level == "chapter":
            role = item.chapter_role()
            return {"research": "innov", "intro": "write", "conclusion": "write"}.get(role, "outline")
        return "propose"

    # session runner ------------------------------------------------------------
    def _persist(self, session):
        with self.lock:
            data = json.dumps(session.snapshot(), ensure_ascii=False, default=str)
        try:
            (self.base / (session.id + ".json")).write_text(data, encoding="utf-8")
        except OSError:
            pass

    def _set(self, session, **fields):
        with self.lock:
            for key, value in fields.items():
                setattr(session, key, value)
        self._persist(session)

    def _run_session(self, session):
        self._set(session, status="running", message="准备中")
        try:
            failed, stop = False, ""
            for index in range(session.rounds_planned):
                if session.cancel.is_set():
                    raise Cancelled()
                self._set(session, round_index=index + 1, steps=[], learn=None)
                outcome = self._run_round(session)
                if outcome.get("stop"):
                    failed, stop = bool(outcome.get("failed")), outcome["stop"]
                    break
            final = "failed" if failed else "stopped" if stop else "done"
            self._set(session, status=final, stop_reason=stop, message=stop or "本次讨论已完成", ended_at=utc())
        except Cancelled:
            self._set(session, status="cancelled", message="已取消。已完成的步骤和文件都已保存。", ended_at=utc(), stop_reason=session.stop_reason or "你取消了这次讨论")
        except Exception as exc:  # noqa: BLE001 - keep the server alive, record the failure
            self._set(session, status="failed", message=f"出错：{exc}", stop_reason=str(exc), ended_at=utc())
            try:
                (self.base / (session.id + ".error.txt")).write_text(traceback.format_exc(), encoding="utf-8")
            except OSError:
                pass

    # one round -------------------------------------------------------------
    def _run_round(self, session):
        item = Item(self.store, session.ref, session.fw)
        no = self._next_round_no(item)
        folder = item.rounds_dir / f"第{no:02d}轮_{stamp()}"
        with self.store.lock:
            folder.mkdir(parents=True, exist_ok=False)
        rnd = {"id": folder.name, "no": no, "dir": item.rel(folder), "ref": item.ref, "fw": session.fw, "recipe": session.recipe,
               "mode": session.mode, "status": "running", "started_at": utc(), "ended_at": "", "steps": [],
               "applied": {"revisions": [], "ops": [], "runs": [], "verifications": []},
               "progress": {"ledger_changes": 0, "revisions": 0, "runs": 0}, "pending": [], "summary": {}, "drift": None,
               "stop_reason": "", "candidates": [], "questions": [], "notes": []}
        ctx_round = {"session": session, "item": item, "folder": folder, "round": rnd, "no": no, "label": f"第{no}轮",
                     "doc_at_start": item.doc_text() if item.doc_path is not None and item.level != "framework" else None}
        self._set(session, round={"id": rnd["id"], "no": no, "dir": rnd["dir"]}, message=f"第{no}轮开始")
        self._write_round(ctx_round)
        result = {}
        try:
            settings, _ = self.settings.load()
            quota = agents.quota_status(self.store.root, settings)
            needed = self._needed_roles(session.recipe, item)
            # The engineer (Codex, sandboxed) is optional: without it plans wait for quota.
            optional = [r for r in needed if r == "engineer"]
            required = [r for r in needed if r not in optional]
            try:
                session.roles = agents.resolve_roles(settings, quota, required)
            except agents.RoleError as exc:
                raise StepFailed(f"找不到可用的模型：{exc}", "quota") from exc
            for role in optional:
                try:
                    session.roles.update({role: agents.resolve_roles(settings, quota, ["author", role])[role]})
                except agents.RoleError as exc:
                    rnd["notes"].append(f"本轮没有可用的{ROLE_LABELS.get(role, role)}（{exc}），验证计划会先登记，等额度恢复后再运行")
            self._write_task(ctx_round)
            runner = getattr(self, "_recipe_" + session.recipe)
            result = runner(ctx_round) or {}
            rnd["status"] = "done"
        except Cancelled:
            rnd["status"], rnd["stop_reason"] = "cancelled", "你取消了这次讨论"
            raise
        except StepFailed as exc:
            rnd["status"], rnd["stop_reason"] = "failed", str(exc)
            # The session's final status is set once, together with the reason.
            result = {"stop": str(exc), "failed": True}
        except Exception as exc:
            rnd["status"], rnd["stop_reason"] = "failed", f"程序出错：{exc}"
            raise
        finally:
            rnd["ended_at"] = utc()
            if not rnd["stop_reason"] and result.get("stop"):
                rnd["stop_reason"] = result["stop"]
            if rnd["status"] == "done" and not result.get("stop"):
                # Decide the stop reason first, so the brief can say why the session stopped.
                try:
                    stop = self._stop_reason(ctx_round)
                except Exception as exc:  # noqa: BLE001
                    stop = f"停止条件检查失败：{exc}"
                if stop:
                    result["stop"] = stop
                    rnd["stop_reason"] = stop
            try:
                self._finish_round(ctx_round, result)
            except Exception as exc:  # noqa: BLE001 - never mask the original outcome
                rnd["notes"].append(f"简报生成失败：{exc}")
                self._write_round(ctx_round)
        return result

    def _needed_roles(self, recipe, item):
        anchor, _ = item.anchor()
        has_anchor = anchor.status == "confirmed"
        if recipe == "advance":
            return ["author", "reviewer", "engineer", "drift"]
        if recipe in {"outline", "refine"}:
            return ["author", "reviewer", "drift"] if has_anchor else ["author", "reviewer"]
        if recipe == "referee":
            return ["author", "referee"]
        if recipe == "propose":
            return ["author", "reviewer", "writer", "referee"]
        if recipe in {"write", "assemble"}:
            return ["author", "writer"]
        if recipe == "innov":
            stage = item.state().get("stage", "S1")
            return ["author", "writer"] if STAGE_TEMPLATES.get(stage, ("", "author"))[1] == "writer" else ["author"]
        return ["author"]

    def _next_round_no(self, item):
        numbers = [int(m.group(1)) for p in (item.rounds_dir.iterdir() if item.rounds_dir.is_dir() else []) if (m := re.match(r"第(\d+)轮_", p.name))]
        return max(numbers, default=0) + 1

    def _write_round(self, c):
        # Serialize under the manager lock, write under the store lock; never nest them.
        with self.lock:
            data = json.dumps(c["round"], ensure_ascii=False, indent=1, default=str).encode("utf-8")
        with self.store.lock:
            self.store._atomic(c["folder"] / "round.json", data, history=False)

    def _write_task(self, c):
        s, item = c["session"], c["item"]
        roles = "\n".join(f"- {ROLE_LABELS.get(k, k)}：{v.get('label', '')}" for k, v in s.roles.items())
        text = (f"# {c['label']} · {RECIPE_LABELS.get(s.recipe, s.recipe)}\n\n- 条目：{item.ref} {item.title}\n- 模式：{'学习模式' if s.mode == 'learn' else '自动'}"
                f"（本次共 {s.rounds_planned} 轮中的第 {s.round_index} 轮）\n- 开始：{c['round']['started_at']}\n\n## 角色\n\n{roles}\n\n## 本轮重点\n\n{s.focus or '（未指定）'}\n")
        with self.store.lock:
            self.store._write_text(c["folder"] / "任务.md", text)

    # model calls --------------------------------------------------------------
    def _call(self, c, key, label, role, template, ctx, mode="ask", web_search=False, workdir=None):
        s = c["session"]
        if s.cancel.is_set():
            raise Cancelled()
        info = s.roles.get(role)
        if not info:
            raise StepFailed(f"没有为“{ROLE_LABELS.get(role, role)}”找到可用的模型", "quota")
        settings, _ = self.settings.load()
        limits = settings["limits"]
        prompt = P.build(template, ctx)
        step = {"key": key, "label": label, "role": role, "model_label": info.get("label", ""), "provider": info["provider"],
                "model": info["model"], "family": info.get("family", ""), "status": "running", "started_at": utc(),
                "duration_s": 0, "output_path": "", "prompt_path": "", "error": ""}
        with self.lock:
            s.steps.append(step)
            s.message = f"{label}：{info.get('label', '')}"
        self._persist(s)
        c["round"]["steps"].append(step)
        n = len(c["round"]["steps"])
        base = f"{n:02d}_{label}_{family_label(info.get('family'))}"
        with self.store.lock:
            self.store._write_text(c["folder"] / (base + ".提示词.md"), prompt)
        step["prompt_path"] = c["item"].rel(c["folder"] / (base + ".提示词.md"))
        spec = agents.CallSpec(provider=info["provider"], model=info["model"], prompt=prompt, mode=mode, effort=info.get("effort", ""),
                               workdir=workdir, timeout_s=int(limits.get("call_timeout_minutes", 25)) * 60,
                               budget_usd=float(limits.get("claude_per_call_usd", 0.5)) if info["provider"] == "claude" else None,
                               web_search=web_search, label=f"{c['item'].ref}:{key}")
        result = agents.run_call(spec, cancel=s.cancel)
        try:
            self.usage.record(result, role=role, ref=c["item"].ref, round_id=c["round"]["id"])
        except TypeError:
            self.usage.record(result, role, c["item"].ref, c["round"]["id"])
        usage = getattr(result, "usage", {}) or {}
        header = (f"<!-- 角色：{ROLE_LABELS.get(role, role)}；模型：{info.get('label', '')}；时间：{utc()}；"
                  f"用时：{result.duration_s:.0f} 秒；用量：输入 {usage.get('input_tokens', 0)} / 输出 {usage.get('output_tokens', 0)} token"
                  + (f"；估算费用 ${result.cost_usd:.4f}" if result.cost_usd else "") + " -->\n\n")
        with self.store.lock:
            self.store._write_text(c["folder"] / (base + ".md"), header + (result.text or f"（没有得到回复：{result.error}）"))
        with self.lock:
            step.update(status="done" if result.ok else "failed", duration_s=round(result.duration_s, 1), output_path=c["item"].rel(c["folder"] / (base + ".md")),
                        error="" if result.ok else (result.error or result.error_class), usage=usage, cost_usd=result.cost_usd)
        self._persist(s)
        self._write_round(c)
        if result.error_class == "cancelled" or s.cancel.is_set():
            raise Cancelled()
        if not result.ok:
            raise StepFailed(f"{label}（{info.get('label', '')}）没有完成：{result.error or result.error_class}。这次请求不会自动重发。", result.error_class)
        # The reply is archived in the round folder; drop the scratch folder of
        # a successful read-only call (failed calls keep raw stdout/stderr).
        if mode == "ask" and workdir is None and getattr(result, "workdir", None):
            try:
                if Path(result.workdir).resolve().is_relative_to(agents.call_root().resolve()):
                    shutil.rmtree(result.workdir, ignore_errors=True)
            except (OSError, ValueError):
                pass
        return result

    def _skip(self, c, key, label, reason):
        step = {"key": key, "label": label, "role": "", "model_label": "", "status": "skipped", "duration_s": 0, "output_path": "", "error": reason}
        with self.lock:
            c["session"].steps.append(step)
        c["round"]["steps"].append(step)
        self._persist(c["session"])

    # context ------------------------------------------------------------------
    def _context(self, c, **extra):
        item, s = c["item"], c["session"]
        anchor, _ = item.anchor()
        ledger, _ = item.ledger()
        notes = extra.pop("notes", True)
        anchor_text = anchor_prompt(anchor)
        answered = [d for d in item.state().get("decisions", []) if d.get("status") == "已处理" and d.get("answer")]
        if answered:
            anchor_text += "\n用户对讨论中问题的决定：\n" + "\n".join(f"- {d['text'][:200]} → {d['answer']}" for d in answered[-8:])
        ctx = {"project": project_prompt(self.store), "anchor": anchor_text, "ledger": ledger_prompt(ledger, extra.pop("changed", ()), notes=notes),
               "rejected": rejected_prompt(ledger), "doc": clip(item.doc_text(), MAX_DOC_CHARS) or "（正文为空）", "level": item.level,
               "round": c["label"], "focus": s.focus or "（未指定，按锚点推进）", "rejected_reason": self._rejected_reason(item)}
        ctx["item"] = f"{item.ref} {item.title}"
        ctx["chapter"] = self._chapter_ctx(item, s.fw)
        ctx["upstream"] = self._upstream_ctx(item, s.fw)
        if item.level == "question":
            ctx["results"] = self._results_ctx(item)
            ctx["code_files"] = self._code_list(item)
        # Framework layer: every framework. Chapters and questions: only the one they sit in.
        ctx["frameworks"] = self._frameworks_ctx() if item.level in {"framework_layer", "framework"} else self._frameworks_ctx(only=self._framework_id(s.fw))
        if item.level == "chapter":
            ctx["stage"] = item.state().get("stage", "S1")
            ctx["ideas"] = "\n".join(f"{e.id} [{e.state}] {e.text}" for e in ledger.entries if e.type == "想法") or "（尚无想法）"
        ctx.update(extra)
        return ctx

    def _rejected_reason(self, item):
        state = item.state()
        parts = []
        if state.get("rejected_reason"):
            parts.append(state["rejected_reason"])
        answered = [d for d in state.get("decisions", []) if d.get("status") == "已处理" and d.get("answer")]
        if answered:
            parts.append("用户已经回答的问题（照此执行，不要再问同样的问题）：\n" + "\n".join(f"- 问：{d['text']}\n  答：{d['answer']}" for d in answered[-8:]))
        return "\n\n".join(parts) or "（无）"

    def _chapter_ctx(self, item, fw):
        if item.level == "chapter":
            no, _ = chapter_number(self.store, item.ref, fw)
            anchor, _ = item.anchor()
            return f"第{no or '?'}章 {item.title}（{CHAPTER_ROLES.get(item.chapter_role(), '')}）\n{anchor_prompt(anchor)}"
        if item.level == "question":
            content = item.question_content()
            no, _ = chapter_number(self.store, content["id"], fw)
            chapter = Item(self.store, content["id"], fw)
            anchor, _ = chapter.anchor()
            return f"第{no or '?'}章 {content['title']}（{CHAPTER_ROLES.get(role_of(content), '')}）\n本章锚点：\n{anchor_prompt(anchor)}"
        return "（不适用）"

    def _theory_chapter(self, fw):
        frameworks = self.store._frameworks()
        framework = next((f for f in frameworks if f["id"] == fw), None) or next((f for f in frameworks if f["status"] == "active"), None)
        if not framework:
            return None
        for cid in framework["content_ids"]:
            try:
                content = self.store._content(cid)
            except FileNotFoundError:
                continue
            if role_of(content) == "theory":
                return content
        return None

    def _upstream_ctx(self, item, fw):
        theory = self._theory_chapter(fw)
        if not theory:
            return "（没有找到第 2 章理论）"
        if item.level == "question" and item.record["content_id"] == theory["id"]:
            return "（本问题属于理论章，上游为项目卡与本章锚点）"
        if item.level == "chapter" and item.ref == theory["id"]:
            return "（本章就是理论章）"
        lines = []
        state = self.store.state()
        for q in state["questions"]:
            if q["content_id"] != theory["id"]:
                continue
            qitem = Item(self.store, q["id"], fw)
            ledger, _ = qitem.ledger()
            confirmed = [e for e in ledger.entries if e.state == "确定" and e.type in {"推导步", "论断"}]
            lines.append(f"{q['id']}《{q['title']}》：{q['status_label']}")
            lines += [f"  - {e.id} {clip(e.text, 300)}" for e in confirmed[:12]]
        return "\n".join(lines) or "（理论章还没有已确定的命题）"

    def _results_ctx(self, item):
        runs = self.store._records(item.record, "结果", "run", "R")
        if not runs:
            return "（还没有验证运行）"
        out = []
        for run in runs[-6:]:
            directory = self.store._safe(run["path"]).parent
            summary = directory / "summary.json"
            text = summary.read_text(encoding="utf-8", errors="replace") if summary.is_file() else "（无 summary.json）"
            out.append(f"{run['id']} {run['title']}：结果 {run['outcome']}；对应计划 {run.get('plan_id', '')}\n{clip(text, 2500)}")
        return "\n\n".join(out)

    def _code_list(self, item):
        files = [f for f in self.store._file_list(item.record) if f["kind"] == "code"]
        return "\n".join(f"- {f['path']}：{f.get('purpose', '')}" for f in files) or "（还没有代码）"

    def _framework_id(self, fw):
        frameworks = self.store._frameworks()
        found = next((f for f in frameworks if f["id"] == fw), None) or next((f for f in frameworks if f["status"] == "active"), None)
        return found["id"] if found else ""

    def _frameworks_ctx(self, only=None):
        out = []
        for f in self.store._frameworks():
            if only is not None and f["id"] != only:
                continue
            chapters = []
            for index, cid in enumerate(f["content_ids"], 1):
                try:
                    content = self.store._content(cid)
                    chapters.append(f"第{index}章 {content['title']}（{CHAPTER_ROLES.get(role_of(content), '')}）")
                except FileNotFoundError:
                    chapters.append(f"第{index}章 {cid}（未找到）")
            status = {"active": "主推", "candidate": "候选", "parked": "暂存"}.get(f["status"], f["status"])
            out.append(f"{f['id']}《{f['title']}》[{status}]\n论文主线：{clip(f.get('mainline', ''), 1500)}\n" + "\n".join(chapters))
        return "\n\n".join(out) or "（还没有框架）"

    # applying replies -----------------------------------------------------------
    def _evidence(self, item):
        runs, verifications = {}, {}
        if item.level == "question":
            plans = {p["id"]: p for p in self.store._records(item.record, "验证计划", "plan", "P")}
            for run in self.store._records(item.record, "结果", "run", "R"):
                plan = plans.get(run.get("plan_id"), {})
                runs[run["id"]] = {"outcome": run.get("outcome", "pending"), "direction": plan.get("direction", ""),
                                   "targets": {t["id"]: t.get("sha", "") for t in plan.get("target_entries", [])},
                                   "created_at": plan.get("created_at", "")}
        if item.verify_dir.is_dir():
            for path in sorted(item.verify_dir.glob("V*.md")):
                meta = re.search(r"<!-- verification (\{.*?\}) -->", path.read_text(encoding="utf-8", errors="replace"))
                if meta:
                    try:
                        data = json.loads(meta.group(1))
                        verifications[data["id"]] = {"entry": data.get("entry", ""), "verdict": data.get("verdict", ""), "family": data.get("family", ""),
                                                     "entry_sha": data.get("entry_sha", ""), "created_at": data.get("created_at", "")}
                    except (ValueError, KeyError):
                        continue
        return {"runs": runs, "verifications": verifications}

    def _apply_ops(self, c, reply, step, role):
        item, s = c["item"], c["session"]
        info = s.roles.get(role, {})
        family = info.get("family", "unknown")
        source = f"{family_label(family)}·{c['label']}"
        c["temp_map"] = {}
        if not reply.ops:
            return []
        with self.store.lock:
            anchor, _ = item.anchor()
            ledger, _ = item.ledger()
            before = {e.id: (e.state, e.text) for e in ledger.entries}
            anchor_before, ledger_before = L.render_anchor(anchor), L.render_ledger(ledger)
            results = L.apply_ops(ledger, anchor, reply.ops, source=source, family=family, round_label=c["label"], step=step, evidence=self._evidence(item))
            # [新1]-style tags are local to one reply; keep this reply's map.
            c["temp_map"] = dict(getattr(ledger, "last_temp_map", {}) or {})
            # Write only real changes: unchanged files keep their revision, so an
            # editor the user has open does not get a false conflict.
            if L.render_ledger(ledger) != ledger_before:
                item.save_ledger(ledger)
            if L.render_anchor(anchor) != anchor_before:
                item.save_anchor(anchor)
            changed = sum(1 for e in ledger.entries if before.get(e.id) != (e.state, e.text))
            c["round"]["progress"]["ledger_changes"] += changed
            c.setdefault("changed", set()).update(e.id for e in ledger.entries if before.get(e.id) != (e.state, e.text))
            # Requests (?) stay in the anchor; '!' lines and evidence conflicts become decisions for the user.
            self._add_decisions(c, [r for r in results if r.blocking and not re.match(r"^\s*(?:[-*]\s+)?[?？]", r.raw)], source)
            item.log(f"{c['label']} · {ROLE_LABELS.get(role, role)} {info.get('label', '')}", results)
        c["round"]["applied"]["ops"] += [{"raw": r.raw, "status": r.status, "reason": r.reason, "entry": r.entry_id} for r in results]
        return results

    def _add_decisions(self, c, results, source, options=None):
        """Record blocking results as 待处理 decisions (the next start is blocked until they are answered)."""
        if not results:
            return
        item = c["item"]
        with self.store.lock:
            state = item.state()
            items = state.setdefault("decisions", [])
            open_texts = {d["text"] for d in items if d.get("status") == "待处理"}
            for r in results:
                text = r.reason if isinstance(r, L.OpResult) else str(r)
                if text in open_texts:
                    continue
                entry = {"id": f"N{len(items) + 1}", "text": text, "source": source, "status": "待处理", "answer": "",
                         "round": c["round"]["id"], "needs_answer": True}
                if options:
                    entry["options"] = list(options)
                items.append(entry)
                open_texts.add(text)
                c["round"]["pending"].append({"kind": "decision", "id": entry["id"], "text": text})
            item.save_state(state)

    @staticmethod
    def _temp_map(c):
        return dict(c.get("temp_map") or {})

    def _apply_revisions(self, c, reply, temp_map=None):
        item = c["item"]
        if not reply.revisions:
            return []
        revisions = reply.revisions
        hidden = False
        for rev in revisions:
            if "<!--" in rev.body:
                rev.body, hidden = neutralize_comments(rev.body), True
            for tag, real in (temp_map or {}).items():
                rev.body = rev.body.replace(tag, real)
        if hidden:
            c["round"]["notes"].append("AI 的修订里有 HTML 隐藏注释，已改成可见文字")
        if item.level == "framework":
            return self._apply_framework_revisions(c, revisions)
        if item.doc_path is None:
            return [{"section": r.section, "mode": r.mode, "status": "已拒绝", "reason": "这一层没有正文"} for r in revisions]
        protected = {"1": "第 1 节由锚点生成，请用 ? 行申请修改锚点", "5": "__stage__"} if item.level == "question" else {}
        with self.store.lock:
            old = item.read(item.doc_path)
            # The user may have edited the document while the model was thinking: a section the
            # user changed since the round started is never overwritten; that revision waits for review.
            base = c.get("doc_at_start")
            user_changed = set()
            if base is not None and base != old:
                for rev in revisions:
                    if section_text(base, rev.section) != section_text(old, rev.section):
                        user_changed.add(id(rev))
            safe = [r for r in revisions if id(r) not in user_changed]
            new, results = L.apply_revisions(old, safe, protected=protected)
            for rev in revisions:
                if id(rev) in user_changed:
                    results.append({"section": rev.section, "mode": rev.mode, "status": "待你确认",
                                    "reason": "你在本轮期间改过这一节，AI 的修改没有覆盖，放在候选里"})
            if item.level == "question":
                # Unknown placeholders stay as written; the end-of-round check reports them.
                new, _missing = L.resolve_placeholders(new, self._lookup(item))
            if new != old:
                self.store._write_text(item.doc_path, new)
            c["doc_at_start"] = new
        applied = [r for r in results if r.get("status") == "已应用"]
        # Re-sending identical text is not progress (the no-progress stop relies on this).
        if new != old:
            c["round"]["progress"]["revisions"] += len(applied)
        else:
            for r in applied:
                r["reason"] = r.get("reason") or "与原文相同，没有改动"
        staged = [r for r in results if r.get("status") == "待你确认"]
        if staged:
            state = item.state()
            for r in staged:
                body = next((v.body for v in revisions if v.section == r.get("section")), "")
                key = "staged_conclusions" if r.get("reason") == "__stage__" else "staged_revisions"
                state.setdefault(key, []).append({"round": c["round"]["id"], "section": r.get("section"), "mode": r.get("mode"), "body": body})
            item.save_state(state)
        c["round"]["applied"]["revisions"] += results
        return results

    def _resolve_doc_placeholders(self, c, report=False):
        """Placeholders written before this round's runs existed are filled in now."""
        item = c["item"]
        if item.level != "question" or item.doc_path is None:
            return
        with self.store.lock:
            old = item.read(item.doc_path)
            new, missing = L.resolve_placeholders(old, self._lookup(item))
            if new != old:
                self.store._write_text(item.doc_path, new)
                c["round"]["notes"].append("已按运行结果填入正文中的结果数字")
        if report and missing:
            c["round"]["notes"].append("正文里有找不到对应结果的占位符：" + "、".join(missing) + "（需要先有这次运行，或 summary.json 里有这个键）")

    def _apply_framework_revisions(self, c, revisions):
        item = c["item"]
        results = []
        with self.store.lock:
            record = self.store._find(self.store._frameworks(), item.ref, "框架")
            fields = {key: record.get(key, "") for key in ("mainline", "chapters", "risks")}
            for rev in revisions:
                key = FW_FIELDS.get(re.sub(r"^[\d.\s]+", "", rev.section).strip())
                if not key:
                    results.append({"section": rev.section, "mode": rev.mode, "status": "已拒绝", "reason": "框架只有 论文主线 / 章节安排 / 优点与风险 三节"})
                    continue
                body = re.sub(r"^#{1,6} [^\n]*\n", "", rev.body.strip()).strip()
                if rev.mode == "追加" and fields[key].rstrip().endswith(body):
                    results.append({"section": rev.section, "mode": rev.mode, "status": "已应用", "reason": "与原文相同，没有改动"})
                    continue
                fields[key] = body if rev.mode == "替换" else (fields[key].rstrip() + "\n\n" + body).strip()
                results.append({"section": rev.section, "mode": rev.mode, "status": "已应用", "reason": ""})
            changed = any(fields[k] != record.get(k, "") for k in fields)
            if changed:
                self.store.perform("save_framework", {"id": item.ref, "revision": record["revision"], **fields})
        if changed:
            c["round"]["progress"]["revisions"] += sum(1 for r in results if r["status"] == "已应用" and not r["reason"])
        c["round"]["applied"]["revisions"] += results
        return results

    def _lookup(self, item):
        runs = {r["id"]: r for r in self.store._records(item.record, "结果", "run", "R")}
        doc_dir = item.doc_path.parent

        def lookup(rid, key):
            run = runs.get(rid)
            if not run:
                return None
            path = self.store._safe(run["path"]).parent / "summary.json"
            if not path.is_file():
                return None
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                return None
            for part in key.split("."):
                if isinstance(value, dict) and part in value:
                    value = value[part]
                elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
                    value = value[int(part)]
                else:
                    return None
            return value, md_href(path.relative_to(doc_dir).as_posix())
        return lookup

    # plans and engineer runs ----------------------------------------------------
    def _create_plans(self, c, reply, temp_map=None):
        item = c["item"]
        if item.level != "question" or not reply.plans:
            return []
        created = []
        ledger, _ = item.ledger()
        for block in reply.plans:
            title = ((block.get("标题") or "验证计划").strip().splitlines() or ["验证计划"])[0].strip()[:120] or "验证计划"
            target_text = block.get("针对", "")
            for tag, real in (temp_map or {}).items():
                target_text = target_text.replace(tag, real)
            # Only the leading list of ids is bound; ids mentioned later in prose are not tested by this plan.
            lead = re.match(r"^\s*((?:L\d{2,})(?:\s*[,，、和及]\s*L\d{2,})*)", target_text)
            ids = re.findall(r"L\d{2,}", lead.group(1)) if lead else []
            if lead and re.search(r"L\d{2,}", target_text[lead.end():]):
                c["round"]["notes"].append(f"验证计划“{title}”的「针对」在说明里还提到了其他条目，只绑定开头的 {'、'.join(ids)}")
            targets = []
            for eid in ids:
                entry = L.find(ledger, eid)
                if entry is not None and entry.state == "待定" and not entry.superseded_by:
                    targets.append({"id": eid, "sha": L.entry_sha(entry)})
            direction = "否定" if "否定" in block.get("方向", "") else "支持"
            method = {"理论": "theory", "实测": "measurement"}.get(block.get("方法", "").strip()[:2], "simulation")
            criterion = block.get("判据", "").strip()
            frozen = parse_criteria(criterion)
            if not criterion or not targets:
                c["round"]["notes"].append(f"验证计划“{title}”缺少判据，或「针对」开头没有待定的台账条目，未登记")
                continue
            if not frozen:
                c["round"]["notes"].append(f"验证计划“{title}”的判据没有写成“量 比较符号 阈值”，系统无法核对，未登记")
                continue
            existing = self.store._records(item.record, "验证计划", "plan", "P")
            same = next((p for p in existing if p.get("criterion", "").strip() == criterion
                         and sorted(t["id"] for t in p.get("target_entries", [])) == sorted(t["id"] for t in targets)), None)
            if same is not None:
                if self._plan_done(item, same["id"]):
                    c["round"]["notes"].append(f"验证计划“{title}”与 {same['id']} 相同，未重复登记")
                else:
                    created.append(same)
                    c["round"]["notes"].append(f"验证计划“{title}”与 {same['id']} 相同，{same['id']} 还没有有效结果，重新排队")
                continue
            try:
                plan = self.store.perform("save_plan", {
                    "question_id": item.ref, "title": title, "method": method,
                    "target": f"{target_text}\n\n" + "\n".join(f"{t['id']}：{L.find(ledger, t['id']).text}" for t in targets),
                    "criterion": criterion,
                    "inputs": "\n\n".join(x for x in (block.get("输入与参数", ""), "退化自检：" + block.get("退化自检", "") if block.get("退化自检") else "") if x),
                    "expected": block.get("预期", ""), "target_entries": targets, "direction": direction, "frozen_criteria": frozen})
            except (ValueError, ConflictError) as exc:
                c["round"]["notes"].append(f"验证计划“{title}”格式不对，未登记：{exc}")
                continue
            created.append(plan)
        return created

    def _plan_done(self, item, plan_id):
        """A plan counts as done once it has a run that reached a verdict (pass, fail or partial)."""
        return any(r.get("plan_id") == plan_id and r.get("outcome") in {"pass", "fail", "partial"}
                   for r in self.store._records(item.record, "结果", "run", "R"))

    def _unrun_plans(self, item):
        """Bound plans without a verdict yet (quota error, cancel, restart...); at most 3 attempts each."""
        if item.level != "question":
            return []
        runs = self.store._records(item.record, "结果", "run", "R")
        ledger, _ = item.ledger()
        out = []
        for plan in self.store._records(item.record, "验证计划", "plan", "P"):
            if not plan.get("target_entries") or self._plan_done(item, plan["id"]):
                continue
            if sum(1 for r in runs if r.get("plan_id") == plan["id"]) >= 3:
                continue
            entries = [L.find(ledger, t["id"]) for t in plan["target_entries"]]
            live = [t for t, e in zip(plan["target_entries"], entries) if e is not None and e.state == "待定" and L.entry_sha(e) == t.get("sha")]
            if live:
                out.append(plan)
        return out

    def _run_plans(self, c, plans):
        item = c["item"]
        if "engineer" not in c["session"].roles:
            for plan in plans:
                c["round"]["notes"].append(f"{plan['id']} 已登记，等工程（Codex）有额度时再运行")
            return []
        settings, _ = self.settings.load()
        limit = int(settings["limits"].get("max_runs_per_round", 2))
        runs, started = [], 0
        for plan in plans:
            # Re-read: an earlier run this round may already have settled these targets.
            ledger, _ = item.ledger()
            live = [t for t in plan.get("target_entries", [])
                    if (e := L.find(ledger, t["id"])) is not None and e.state == "待定" and L.entry_sha(e) == t.get("sha")]
            if not live:
                c["round"]["notes"].append(f"{plan['id']} 针对的条目已有结论或内容已改动，未运行")
                continue
            if started >= limit:
                c["round"]["notes"].append(f"{plan['id']} 超出本轮运行上限，留到下一轮")
                continue
            started += 1
            runs.append(self._run_plan(c, plan))
        return [r for r in runs if r]

    def _run_plan(self, c, plan):
        item, s = c["item"], c["session"]
        run = self.store.perform("create_run", {"question_id": item.ref, "plan_id": plan["id"], "title": plan["title"][:100], "target_step": plan.get("target", "")[:2000]})
        workspace = agents.work_root() / f"{c['round']['id']}_{plan['id']}"
        if workspace.exists():
            shutil.rmtree(workspace, ignore_errors=True)
        (workspace / "代码").mkdir(parents=True)
        (workspace / "输出").mkdir()
        workspace = workspace.resolve()
        originals = {}
        for f in self.store._file_list(item.record):
            if f["kind"] == "code":
                src = self.store._safe(f["path"])
                data = src.read_bytes()
                (workspace / "代码" / src.name).write_bytes(data)
                originals[src.name] = sha(data)
        plan_text = (f"# {plan['id']} {plan['title']}\n\n- 方向：{plan.get('direction', '支持')}（判据满足则{'支持' if plan.get('direction', '支持') == '支持' else '否定'}所针对的条目）\n"
                     f"- 方法：{plan.get('method', '')}\n\n## 针对\n\n{plan.get('target', '')}\n\n## 判据（运行前冻结）\n\n{plan.get('criterion', '')}\n\n"
                     f"## 输入与条件\n\n{plan.get('inputs', '')}\n\n## 预期\n\n{plan.get('expected', '')}\n")
        (workspace / "计划.md").write_text(plan_text, encoding="utf-8")
        (workspace / "正文摘录.md").write_text(clip(item.doc_text(), 40_000), encoding="utf-8")
        ctx = self._context(c, plan=plan_text, code_files="\n".join(f"- 代码/{name}" for name in originals) or "（没有现有代码）",
                            python=engineer_python())
        before = self._protected_digest(item)
        try:
            result = self._call(c, "verify", f"验证{plan['id']}", "engineer", "q_verify", ctx, mode="engineer", workdir=workspace)
        except Cancelled:
            self._finish_run(c, run, plan, "error", "你取消了这次运行；计划保留，下一轮会重新排队。", {}, [], "", "")
            raise
        except StepFailed as exc:
            self._finish_run(c, run, plan, "error", f"工程调用失败：{exc}", {}, [], "", "")
            c["round"].setdefault("failed_calls", []).append(f"{plan['id']}：{exc}")
            return None
        after = self._protected_digest(item)
        touched = sorted(name for name in set(before) | set(after) if before.get(name) != after.get(name))
        files, skipped = safe_workspace_files(workspace)
        for note in skipped:
            c["round"]["notes"].append(f"{run['id']}：{note}")
        held = []
        # import code
        code_paths, renamed = [], {}
        for path in [p for p in files if p.parent in {workspace, workspace / "代码"} and p.suffix in CODE_SUFFIXES]:
            data = path.read_bytes()
            if originals.get(path.name) == sha(data):
                continue
            if looks_secret(data):
                held.append(path.name)
                continue
            first = data.decode("utf-8", errors="replace").splitlines()[:1]
            purpose = (first[0].lstrip("#%/ ").strip() if first else "") or f"{plan['id']} 的验证程序"
            added = self.store.perform("add_code", {"question_id": item.ref, "filename": path.name, "purpose": purpose[:300], "base64": base64.b64encode(data).decode()})
            code_paths.append(added["path"])
            if added["name"] != path.name:
                renamed[path.name] = added["name"]
        # import outputs; the judged summary.json is always kept with the run
        out_dir = workspace / "输出"
        summary_path = next((p for p in (out_dir / "summary.json", workspace / "summary.json") if p in files), None)
        outputs = [p for p in files if p.is_relative_to(out_dir)]
        if summary_path is not None and summary_path not in outputs:
            outputs.append(summary_path)
        for path in sorted(outputs):
            if path.stat().st_size > 32 * 1024 * 1024:
                c["round"]["notes"].append(f"{path.name} 超过 32 MB，未导入，原件在 {path}")
                continue
            data = path.read_bytes()
            if looks_secret(data):
                held.append(path.name)
                continue
            name = path.name if path.parent in {out_dir, workspace} else "_".join(path.relative_to(out_dir).parts)
            try:
                self.store.perform("attach_file", {"question_id": item.ref, "run_id": run["id"], "filename": name, "base64": base64.b64encode(data).decode()})
            except (ValueError, ConflictError) as exc:
                c["round"]["notes"].append(f"{name} 未导入：{exc}")
        if held:
            c["round"]["notes"].append(f"{run['id']}：这些文件里像有密钥或令牌，没有导入，原件留在 {workspace}：" + "、".join(held))
        mapping = command = ""
        for name in ("对照表.md", "输出/对照表.md"):
            if workspace / name in files:
                mapping = (workspace / name).read_text(encoding="utf-8", errors="replace")
        for name in ("运行命令.txt", "输出/运行命令.txt"):
            if workspace / name in files:
                command = (workspace / name).read_text(encoding="utf-8", errors="replace")
        for old, new in renamed.items():
            # The code was saved under a new name; keep the formula-to-code links pointing at it.
            mapping = re.sub(rf"(?<![\w.]){re.escape(old)}(?![\w])", new, mapping)
            command = re.sub(rf"(?<![\w.]){re.escape(old)}(?![\w])", new, command)
        outcome, actual, summary = judge_summary(summary_path, plan.get("frozen_criteria") or None)
        if not result.ok:
            outcome = "error"
        if touched:
            outcome = "error"
            actual = (actual + "\n\n" if actual else "") + ("运行期间问题目录里这些文件发生了变化：" + "、".join(touched)
                      + "。如果是你在页面上改的，下一轮会重新运行；否则说明程序越界写入。本次运行不作为证据。")
            c["round"]["notes"].append(f"{run['id']} 运行期间受保护文件有变化，已判为出错：" + "、".join(touched))
        self._finish_run(c, run, plan, outcome, actual, summary, code_paths, mapping, command)
        if outcome in {"pass", "fail"} and not held:
            shutil.rmtree(workspace, ignore_errors=True)
        return run["id"]

    def _protected_digest(self, item):
        """Hashes of the files an engineer run must never touch (checked before and after)."""
        files = {}
        for name in ("锚点.md", "台账.md", "推导笔记.md", "问题.md"):
            path = item.dir / name
            if path.is_file():
                files[name] = sha(path.read_bytes())
        for sub in ("结论", "验证计划", "代码"):
            folder = item.dir / sub
            if folder.is_dir():
                for path in sorted(folder.rglob("*")):
                    if path.is_file():
                        files[path.relative_to(item.dir).as_posix()] = sha(path.read_bytes())
        return files

    def _finish_run(self, c, run, plan, outcome, actual, summary, code_paths, mapping, command):
        item = c["item"]
        fresh = self.store._find(self.store._records(item.record, "结果", "run", "R"), run["id"], "运行")
        parameters = json.dumps(summary.get("parameters", {}), ensure_ascii=False, indent=1) if isinstance(summary, dict) and summary.get("parameters") else ""
        self.store.perform("update_run", {"question_id": item.ref, "id": run["id"], "revision": fresh["revision"], "outcome": outcome,
                                          "actual": actual or "（无）", "analysis": "", "mapping": mapping[:200_000], "command": command[:20_000],
                                          "parameters": parameters, "code_paths": code_paths})
        with self.store.lock:
            anchor, _ = item.anchor()
            ledger, _ = item.ledger()
            before = {e.id: (e.state, e.note) for e in ledger.entries}
            results = L.apply_run(ledger, run_id=run["id"], outcome=outcome, direction=plan.get("direction", "支持"),
                                  targets={t["id"]: t.get("sha", "") for t in plan.get("target_entries", [])}, round_label=c["label"],
                                  anchor=anchor, created_at=plan.get("created_at", ""))
            item.save_ledger(ledger)
            item.log(f"{c['label']} · 系统按 {run['id']} 的结果更新", results)
            c["round"]["progress"]["ledger_changes"] += sum(1 for e in ledger.entries if before.get(e.id, (None, None))[0] != e.state)
            self._add_decisions(c, [r for r in results if r.blocking], "系统")
        c["round"]["applied"]["ops"] += [{"raw": r.raw, "status": r.status, "reason": r.reason, "entry": r.entry_id} for r in results]
        c["round"]["progress"]["runs"] += 1
        c["round"]["applied"]["runs"].append({"id": run["id"], "title": run["title"], "outcome": outcome, "actual": actual, "path": run["path"]})

    # recipes ------------------------------------------------------------------
    def _recipe_open(self, c):
        item, s = c["item"], c["session"]
        if item.doc_path is not None and item.level in {"question", "chapter"} and not item.doc_text().strip():
            item.write(item.doc_path, L.skeleton(item.level, item.chapter_role() or ""))
        elif item.level == "chapter" and len(item.doc_text().strip().splitlines()) <= 1:
            item.write(item.doc_path, item.doc_text().rstrip() + "\n\n" + L.skeleton("chapter", item.chapter_role()))
        template = {"question": "open_question", "chapter": "open_chapter"}.get(item.level, "open_framework_layer")
        result = self._call(c, "open", "起草锚点", "author", template, self._context(c))
        reply = L.parse_reply(result.text)
        draft = extract_block(result.text, "锚点草稿")
        if draft:
            anchor, _ = item.anchor()
            meta = json.dumps({"schema": 1, "level": item.anchor_level, "version": 1, "status": "draft"}, ensure_ascii=False)
            body = draft if "<!-- anchor" in draft else f"<!-- anchor {meta} -->\n\n{draft}"
            proposed = L.parse_anchor("# 锚点\n\n" + body, item.anchor_level)
            if ever_confirmed(anchor) or any(a.source == "你" for a in anchor.assumptions) or anchor.requests:
                # The user has shaped this anchor: never overwrite it. Offer the draft instead.
                path = c["folder"] / "锚点草稿.md"
                with self.store.lock:
                    self.store._write_text(path, L.render_anchor(proposed).replace("# 锚点", "# 锚点草稿（未应用）", 1))
                state = item.state()
                state["anchor_draft"] = {"path": item.rel(path), "round_id": c["round"]["id"], "text": clip(draft, 4000)}
                item.save_state(state)
                c["round"]["pending"].append({"kind": "anchor_draft", "text": "AI 起草的新锚点放在本轮目录的“锚点草稿.md”，现有锚点没有改动"})
                c["round"]["summary"] = reply.summary
                self._apply_revisions(c, reply, None)
                return {"stop": "AI 起草的新锚点放在本轮目录的“锚点草稿.md”，现有锚点没有改动；需要时在“待你决定”里用它替换。"}
            proposed.version, proposed.status = anchor.version, "draft"
            for a in proposed.assumptions:
                a.status, a.source = "待批" if a.status != "已拒绝" else a.status, a.source or f"{family_label(s.roles['author'].get('family'))}·{c['label']}"
            item.save_anchor(proposed)
            c["round"]["progress"]["ledger_changes"] += 1
        else:
            c["round"]["notes"].append("回复里没有找到“锚点草稿”块，锚点未改动")
        self._apply_ops(c, reply, "draft", "author")
        self._apply_revisions(c, reply, self._temp_map(c))
        c["round"]["summary"] = reply.summary
        return {"stop": "锚点草稿已写好，请修改后点“确认锚点”。"}

    def _recipe_advance(self, c):
        item, s = c["item"], c["session"]
        ctx = self._context(c)
        draft = self._call(c, "draft", "作者推进", "author", "q_draft", ctx)
        r1 = L.parse_reply(draft.text)
        attack = self._call(c, "attack", "审查找问题", "reviewer", "q_attack", self._context(c, draft_blocks=strip_prose(draft.text), notes=False))
        r2 = L.parse_reply(attack.text)
        if s.mode == "learn":
            decision = self._learning_pause(c, r1, attack.text)
            if decision == "reject":
                return {"stop": "这一步已打回，理由会带进下一轮。"}
        state = item.state()
        if state.get("rejected_reason"):
            state["rejected_reason"] = ""
            item.save_state(state)
        self._apply_ops(c, r1, "draft", "author")
        temp1 = self._temp_map(c)
        self._apply_revisions(c, r1, temp1)
        plans = self._create_plans(c, r1, temp1)
        self._apply_ops(c, r2, "attack", "reviewer")
        reply = self._call(c, "reply", "作者回应", "author", "q_reply", self._context(c, draft_blocks=strip_prose(draft.text), attack=clip(attack.text, 30_000)))
        r3 = L.parse_reply(reply.text)
        self._apply_ops(c, r3, "reply", "author")
        temp3 = self._temp_map(c)
        self._apply_revisions(c, r3, temp3)
        plans += self._create_plans(c, r3, temp3)
        fresh = {p["id"] for p in plans}
        plans += [p for p in self._unrun_plans(item) if p["id"] not in fresh]
        if plans:
            self._run_plans(c, plans)
            self._resolve_doc_placeholders(c)
        else:
            self._skip(c, "verify", "验证运行", "本轮没有新的验证计划")
        summary = r3.summary or r1.summary
        c["round"]["summary"] = summary
        self._drift(c, summary)
        return {}

    def _learning_pause(self, c, r1, attack_text):
        item, s = c["item"], c["session"]
        doc = item.doc_text()
        staged = []
        protected = {"1": "第 1 节由锚点生成", "5": "__stage__"}
        for rev in r1.revisions:
            new, res = L.apply_revisions(doc, [rev], protected=protected)
            staged.append({"section": rev.section, "mode": rev.mode, "old": section_text(doc, rev.section), "new": rev.body, "status": res[0].get("status") if res else ""})
        with self.store.lock:
            self.store._write_text(c["folder"] / "候选修订.json", json.dumps(staged, ensure_ascii=False, indent=1))
        learn = {"staged": staged, "attack": clip(attack_text, 30_000), "explain": "", "ops": [op.raw for op in r1.ops]}
        while True:
            # Clear before announcing the pause, so a quick click is never lost.
            with self.lock:
                s.decision.clear()
                s.decision_value = None
            self._set(s, status="paused", learn=learn, message="学习模式：请看这一步和审查意见，再选继续、追问或打回")
            while not s.decision.wait(1.0):
                if s.cancel.is_set():
                    raise Cancelled()
            if s.cancel.is_set():
                raise Cancelled()
            decision, note = s.decision_value or ("continue", "")
            s.decision_value = None
            if decision == "ask":
                self._set(s, status="running", message="追问中…")
                ctx = self._context(c, staged_step=json.dumps(staged, ensure_ascii=False, indent=1)[:30_000], attack=clip(attack_text, 20_000), focus=note or "请把这一步讲慢一点")
                result = self._call(c, "explain", "解释这一步", "author", "explain", ctx)
                learn["explain"] = result.text
                continue
            if decision == "reject":
                with self.store.lock:
                    self.store._write_text(c["folder"] / "打回理由.md", f"# 打回理由\n\n{note}\n")
                state = item.state()
                state["rejected_reason"] = note
                item.save_state(state)
                self._set(s, status="running", learn=None)
                return "reject"
            self._set(s, status="running", learn=None, message="继续：应用这一步并请作者回应")
            return "continue"

    def _drift(self, c, summary):
        changed = c.get("changed", set())
        ctx = self._context(c, changed=changed, focus="")
        ctx["ledger"] = ledger_prompt(c["item"].ledger()[0], changed, notes=False)
        applied = [r for r in c["round"]["applied"]["revisions"] if r.get("status") == "已应用"]
        bodies = []
        doc = c["item"].doc_text()
        for r in applied:
            text = section_text(doc, r.get("section", ""))
            if text:
                bodies.append(text)
        summary_text = "\n".join(f"{k}：{v}" for k, v in (summary or {}).items()) or "（作者没有给出结论摘要）"
        if bodies:
            summary_text += "\n\n【本轮写进正文的内容（逐节原文）】\n" + clip("\n\n".join(bodies), 20_000)
        ctx["summary"] = summary_text
        ctx["doc"] = "（漂移检查只看本轮写进正文的内容，不看讨论）"
        ctx["rejected"] = "（不适用）"
        result = self._call(c, "drift", "漂移检查", "drift", "drift", ctx)
        reply = L.parse_reply(result.text)
        drift = dict(reply.drift or {})
        if not drift.get("verdict"):
            drift.update(verdict="未给出", raw=clip(result.text, 1500))
        c["round"]["drift"] = drift
        extra = (drift.get("extra_assumptions") or "").strip()
        if extra and not L.is_none_answer(extra):
            items = [x.strip(" -•") for x in re.split(r"[；;\n]", extra) if x.strip(" -•") and not L.is_none_answer(x)]
            op_lines = [f"? 假设 | {x} | 漂移检查发现本轮正文用到、锚点没有登记" for x in items[:6]]
            if op_lines:
                ops = L.parse_reply("```台账变更\n" + "\n".join(op_lines) + "\n```").ops
                self._apply_ops(c, type("R", (), {"ops": ops})(), "drift", "drift")
        question_bad = (drift.get("question_ok") or "").strip().startswith("否")
        criterion_bad = bool((drift.get("criterion_changed") or "").strip()) and not L.is_none_answer(drift.get("criterion_changed", ""))
        if drift["verdict"] in {"漂移", "未给出"} or question_bad or criterion_bad:
            why = []
            if drift["verdict"] == "未给出":
                why.append("漂移检查没有给出可读的判定（原文见简报）")
            if question_bad or drift["verdict"] == "漂移":
                why.append("问题：" + (drift.get("question_ok") or "（未说明）"))
            if criterion_bad:
                why.append("判据或范围：" + drift.get("criterion_changed", ""))
            self._add_decisions(c, ["漂移检查认为讨论可能偏离了锚点。" + "；".join(why) + "。请回答：继续、改锚点，还是打回这一轮？"],
                                "漂移检查", options=["继续", "改锚点", "打回这一轮"])
        return drift

    def _recipe_referee(self, c):
        item, s = c["item"], c["session"]
        ctx = self._context(c, notes=False)
        ctx["results"] = self._results_ctx(item) if item.level == "question" else "（不适用）"
        if item.level == "question":
            code = []
            for f in self.store._file_list(item.record):
                if f["kind"] == "code":
                    code.append(f"### {f['path']}\n\n```\n{clip(self.store._safe(f['path']).read_text(encoding='utf-8', errors='replace'), 12_000)}\n```")
            ctx["code_files"] = clip("\n\n".join(code), 40_000) or "（没有代码）"
        result = self._call(c, "referee", "审稿人独立审查", "referee", "referee", ctx)
        reply = L.parse_reply(result.text)
        info = s.roles["referee"]
        created = []
        if reply.verifications:
            ledger, _ = item.ledger()
            with self.store.lock:
                item.verify_dir.mkdir(parents=True, exist_ok=True)
                existing = [int(m.group(1)) for p in item.verify_dir.glob("V*.md") if (m := re.match(r"V(\d+)", p.name))]
                number = max(existing, default=0)
                for v in reply.verifications:
                    entry = L.find(ledger, v.get("entry", ""))
                    if entry is None:
                        continue
                    number += 1
                    vid = f"V{number:02d}"
                    meta = {"schema": 1, "id": vid, "entry": entry.id, "entry_sha": L.entry_sha(entry), "verdict": v.get("verdict", ""),
                            "provider": info["provider"], "model": info["model"], "family": info.get("family", ""), "round": c["label"], "created_at": utc()}
                    text = (f"# {vid} · 独立复推 · {entry.id}\n\n<!-- verification {json.dumps(meta, ensure_ascii=False)} -->\n\n"
                            f"- 条目：{entry.id} {entry.text}\n- 结论：{v.get('verdict', '')}\n- 审稿人：{info.get('label', '')}（没有看过讨论记录）\n\n## 审稿人说明\n\n{v.get('note', '')}\n")
                    self.store._write_text(item.verify_dir / f"{vid}.md", text, new=True)
                    created.append((vid, entry.id, v.get("verdict", "")))
            ops_text = []
            for vid, eid, verdict in created:
                if verdict == "一致":
                    ops_text.append(f"~ {eid} | 确定 | 审稿人独立复推一致 | {vid}")
                elif verdict == "不一致":
                    ops_text.append(f"~ {eid} | 否定 | 审稿人独立复推发现错误 | {vid}")
            if ops_text:
                ops = L.parse_reply("```台账变更\n" + "\n".join(ops_text) + "\n```").ops
                self._apply_ops(c, type("R", (), {"ops": ops})(), "referee", "referee")
        c["round"]["applied"]["verifications"] = [{"id": vid, "entry": eid, "verdict": verdict} for vid, eid, verdict in created]
        c["round"]["summary"] = reply.summary
        c["round"]["notes"].append("审稿意见全文见本轮目录中的审稿人回复。")
        return {"stop": "审稿完成，请看简报里的复推结果和弱点清单。"}

    def _recipe_outline(self, c):
        item = c["item"]
        if item.doc_path is not None and len(item.doc_text().strip().splitlines()) <= 1:
            item.write(item.doc_path, item.doc_text().rstrip() + "\n\n" + L.skeleton("chapter", item.chapter_role()))
        result = self._call(c, "outline", "章节提纲", "author", "ch_outline", self._context(c))
        r1 = L.parse_reply(result.text)
        attack = self._call(c, "attack", "审查找问题", "reviewer", "ch_attack", self._context(c, draft_blocks=strip_prose(result.text), notes=False))
        r2 = L.parse_reply(attack.text)
        self._apply_ops(c, r1, "draft", "author")
        self._apply_revisions(c, r1, self._temp_map(c))
        self._apply_ops(c, r2, "attack", "reviewer")
        reply = self._call(c, "reply", "作者回应", "author", "q_reply", self._context(c, draft_blocks=strip_prose(result.text), attack=clip(attack.text, 30_000)))
        r3 = L.parse_reply(reply.text)
        self._apply_ops(c, r3, "reply", "author")
        self._apply_revisions(c, r3, self._temp_map(c))
        questions = r3.questions or r1.questions
        self._propose_questions(c, questions)
        c["round"]["summary"] = r3.summary or r1.summary
        if "drift" in c["session"].roles:
            self._drift(c, c["round"]["summary"])
        return {}

    def _propose_questions(self, c, questions):
        if not questions:
            return
        item = c["item"]
        state = item.state()
        state["proposed_questions"] = [{**q, "round": c["round"]["id"]} for q in questions]
        item.save_state(state)
        c["round"]["questions"] = questions
        c["round"]["pending"].append({"kind": "questions", "text": f"AI 提出了 {len(questions)} 个小问题，勾选后建立"})

    def _recipe_innov(self, c):
        item = c["item"]
        state = item.state()
        stage = state.get("stage", "S1")
        template, role = STAGE_TEMPLATES.get(stage, STAGE_TEMPLATES["S1"])
        label = dict(STAGES).get(stage, stage)
        result = self._call(c, stage.lower(), f"{stage} {label}", role, template, self._context(c), web_search=(stage == "S4"))
        reply = L.parse_reply(result.text)
        self._apply_ops(c, reply, "draft", role)
        self._apply_revisions(c, reply, self._temp_map(c))
        if stage in {"S5", "S6"}:
            self._propose_questions(c, reply.questions)
        if reply.plans and stage in {"S1", "S5"}:
            # Chapter plans become small questions: each kill test is run and judged there.
            ledger, _ = item.ledger()
            temp = self._temp_map(c)
            proposed = []
            for plan in reply.plans:
                target = plan.get("针对", "")
                for tag, real in temp.items():
                    target = target.replace(tag, real)
                ids = [i for i in re.findall(r"L\d{2,}", target) if L.find(ledger, i) is not None]
                title = (plan.get("标题") or "杀测").splitlines()[0][:100]
                proposed.append({"title": f"{stage} {title}", "question": f"针对本章 {'、'.join(ids) or '（未指定条目）'}：{plan.get('预期', '')}".strip(),
                                 "criterion": plan.get("判据", ""), "depends": "、".join(ids), "load_bearing": False,
                                 "method": plan.get("方法", "仿真"), "plan": plan})
            state = item.state()
            state["proposed_questions"] = state.get("proposed_questions", []) + [{**q, "round": c["round"]["id"]} for q in proposed]
            item.save_state(state)
            c["round"]["notes"].append(f"{len(proposed)} 个验证计划转成了待建立的小问题（勾选后建立，在小问题里运行和判定）")
        hints = {"S2": "勾选要保留的想法（v2_pick_ideas），再进入下一阶段", "S3": "按筛选意见勾选保留的想法，再进入下一阶段",
                 "S4": "逐条核对文献编号，勾选“已核实”，再进入下一阶段", "S5": "杀测设计成小问题，勾选建立后逐个推进",
                 "S6": "方法已写入本章草稿，勾选建立小问题"}
        c["round"]["pending"].append({"kind": "stage", "text": f"{stage} {label} 已完成。{hints.get(stage, '确认后进入下一阶段')}"})
        c["round"]["summary"] = reply.summary
        state = item.state()
        state["stage_done"] = stage
        item.save_state(state)
        return {"stop": f"{stage} {label} 已完成，等你确认后再进入下一阶段。"}

    def _recipe_write(self, c):
        item = c["item"]
        role = item.chapter_role()
        if role == "intro":
            template = "intro_final" if self._adopted_count(c["session"].fw) >= 3 else "intro_story"
        else:
            template = "conclusion"
        ctx = self._context(c, ledger=self._framework_ledgers(c["session"].fw))
        result = self._call(c, "write", "撰写本章", "writer", template, ctx)
        reply = L.parse_reply(result.text)
        self._apply_ops(c, reply, "draft", "writer")
        self._apply_revisions(c, reply)
        c["round"]["summary"] = reply.summary
        return {"stop": "本章草稿已更新，请阅读修改。"}

    def _recipe_assemble(self, c):
        item = c["item"]
        parts = []
        for q in self.store.state()["questions"]:
            if q["content_id"] != item.ref:
                continue
            qdir = self.store._qdir(q)
            body = self.store._read(qdir / "推导笔记.md")
            body, _missing = L.resolve_placeholders(body, self._lookup(Item(self.store, q["id"], record=q)))
            # Links in a mini paper are relative to its own folder; make them work from the chapter folder.
            prefix = md_href(qdir.relative_to(item.dir).as_posix()) + "/"
            body = re.sub(r"(\]\()(?!https?:|#|/|\.\./)([^)\s]+)", lambda m: m.group(1) + prefix + m.group(2), body)
            parts.append(f"## 小问题 {q['id']}《{q['title']}》（{q['status_label']}）\n\n{clip(body, 20_000)}")
        result = self._call(c, "assemble", "组装本章", "writer", "assemble", self._context(c, results="\n\n".join(parts) or "（本章还没有小问题）"))
        reply = L.parse_reply(result.text)
        self._apply_revisions(c, reply)
        c["round"]["summary"] = reply.summary
        return {"stop": "本章草稿已组装，新写的段落标有“（组装时新增）”。"}

    def _recipe_propose(self, c):
        s = c["session"]
        ctx = self._context(c)
        second = "writer" if s.roles.get("writer", {}).get("family") != s.roles["author"].get("family") else "reviewer"
        outputs = {}

        def run(role):
            outputs[role] = self._call(c, f"propose_{role}", f"独立提出框架（{ROLE_LABELS[role]}）", role, "fw_propose", ctx)

        threads = [threading.Thread(target=self._guard, args=(run, role, outputs)) for role in ("author", second)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        for role in ("author", second):
            if isinstance(outputs.get(role), BaseException):
                raise outputs[role]
        a, b = outputs["author"].text, outputs[second].text
        attacks = {}

        def attack(pair):
            role, target = pair
            attacks[role] = self._call(c, f"attack_{role}", f"交叉攻击（{ROLE_LABELS[role]}）", role, "fw_attack", self._context(c, proposals=clip(target, 40_000)))

        threads = [threading.Thread(target=self._guard, args=(attack, pair, attacks)) for pair in ((second, a), ("author", b))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        for key, value in list(attacks.items()):
            if isinstance(value, BaseException):
                raise value
        synth = self._call(c, "synthesize", "整理候选框架", "referee", "fw_synthesize",
                           self._context(c, proposals=clip(f"### 方案甲\n\n{a}\n\n### 方案乙\n\n{b}", 60_000),
                                         attacks=clip(f"### 对方案甲的攻击\n\n{attacks[second].text}\n\n### 对方案乙的攻击\n\n{attacks['author'].text}", 40_000)))
        reply = L.parse_reply(synth.text)
        candidates = [{"index": i, "title": candidate_title(md), "markdown": md} for i, md in enumerate(reply.candidates)]
        c["round"]["candidates"] = candidates
        item = c["item"]
        self._apply_ops(c, reply, "draft", "referee")
        c["round"]["summary"] = reply.summary
        if not candidates:
            # Keep the earlier candidates: a reply without parsable blocks must not wipe them.
            c["round"]["notes"].append("整理结果里没有解析出“候选框架”块，原有候选保留；原文见本轮目录")
            return {"stop": "没有解析出候选框架，原有候选保留。请查看本轮整理的原文。"}
        state = item.state()
        state["candidates"] = {"round": c["round"]["id"], "items": candidates}
        item.save_state(state)
        c["round"]["pending"].append({"kind": "candidates", "text": f"整理出 {len(candidates)} 个候选框架，选择要建立的"})
        return {"stop": "候选框架已整理好，请比较后选择建立。"}

    def _guard(self, fn, arg, sink):
        key = arg if isinstance(arg, str) else arg[0]
        try:
            fn(arg)
        except BaseException as exc:  # noqa: BLE001 - re-raised on the session thread
            sink[key] = exc

    def _recipe_import_fw(self, c):
        item, s = c["item"], c["session"]
        result = self._call(c, "import_fw", "解析已有框架", "author", "fw_import", self._context(c))
        reply = L.parse_reply(result.text)
        draft = extract_block(result.text, "锚点草稿")
        if draft:
            anchor, _ = item.anchor()
            if not ever_confirmed(anchor):
                meta = json.dumps({"schema": 1, "level": item.anchor_level, "version": 1, "status": "draft"}, ensure_ascii=False)
                body = draft if "<!-- anchor" in draft else f"<!-- anchor {meta} -->\n\n{draft}"
                proposed = L.parse_anchor("# 锚点\n\n" + body, item.anchor_level)
                proposed.version, proposed.status = anchor.version, "draft"
                for a in proposed.assumptions:
                    a.status, a.source = "待批" if a.status != "已拒绝" else a.status, a.source or f"{family_label(s.roles['author'].get('family'))}·{c['label']}"
                item.save_anchor(proposed)
        self._apply_ops(c, reply, "draft", "author")
        candidates = [{"index": i, "title": candidate_title(md), "markdown": md} for i, md in enumerate(reply.candidates)]
        c["round"]["candidates"] = candidates
        c["round"]["summary"] = reply.summary
        if not candidates:
            c["round"]["notes"].append("没有从粘贴内容中解析出“候选框架”块，请查看本轮原文")
            return {"stop": "未能解析出标准章节结构，请查看本轮回复原文。"}
        state = item.state()
        state["candidates"] = {"round": c["round"]["id"], "items": candidates}
        item.save_state(state)
        c["round"]["pending"].append({"kind": "candidates", "text": f"已从导入内容解析出 {len(candidates)} 个框架方案，点击“建立为框架”即可自动分章"})
        return {"stop": "已有框架已解析完成，请在候选框架中点击“建立为框架”自动生成各章目录。"}

    def _recipe_refine(self, c):
        item = c["item"]
        result = self._call(c, "refine", "修改框架", "author", "fw_refine", self._context(c))
        r1 = L.parse_reply(result.text)
        attack = self._call(c, "attack", "审查找问题", "reviewer", "q_attack", self._context(c, draft_blocks=strip_prose(result.text), notes=False))
        r2 = L.parse_reply(attack.text)
        self._apply_ops(c, r1, "draft", "author")
        self._apply_revisions(c, r1, self._temp_map(c))
        self._apply_ops(c, r2, "attack", "reviewer")
        reply = self._call(c, "reply", "作者回应", "author", "q_reply", self._context(c, draft_blocks=strip_prose(result.text), attack=clip(attack.text, 30_000)))
        r3 = L.parse_reply(reply.text)
        self._apply_ops(c, r3, "reply", "author")
        self._apply_revisions(c, r3, self._temp_map(c))
        c["round"]["summary"] = r3.summary or r1.summary
        if "drift" in c["session"].roles:
            self._drift(c, c["round"]["summary"])
        return {}

    def _adopted_count(self, fw):
        return sum(1 for q in self.store.state()["questions"] if q["status"] in {"supported", "refuted"})

    def _framework_ledgers(self, fw):
        frameworks = self.store._frameworks()
        framework = next((f for f in frameworks if f["id"] == fw), None) or next((f for f in frameworks if f["status"] == "active"), None)
        if not framework:
            return "（没有框架）"
        lines = []
        state = self.store.state()
        for index, cid in enumerate(framework["content_ids"], 1):
            for q in state["questions"]:
                if q["content_id"] != cid:
                    continue
                qitem = Item(self.store, q["id"], fw)
                ledger, _ = qitem.ledger()
                for e in ledger.entries:
                    if e.type == "文献" and e.state != "确定":
                        continue
                    lines.append(f"第{index}章 {q['id']} {e.id} [{e.type}|{e.state}] {clip(e.text, 300)}")
        return clip("\n".join(lines), 50_000) or "（各章台账为空）"

    # finishing ----------------------------------------------------------------
    def _finish_round(self, c, result):
        item, rnd, s = c["item"], c["round"], c["session"]
        try:
            self._resolve_doc_placeholders(c, report=True)
        except (OSError, ValueError) as exc:
            rnd["notes"].append(f"结果数字检查失败：{exc}")
        anchor, _ = item.anchor()
        ledger, _ = item.ledger()
        state = item.state()
        pending = list(rnd["pending"])
        pending += [{"kind": "request", "id": r.id, "text": f"{r.slot} → {r.proposal}（{r.reason}）"} for r in L.anchor_blocking(anchor)]
        pending += [{"kind": "assumption", "id": a.id, "text": a.text} for a in L.pending_assumptions(anchor)]
        pending += [{"kind": "decision", "id": d["id"], "text": d["text"]} for d in state.get("decisions", []) if d.get("status") == "待处理"]
        unique, seen = [], set()
        for p in pending:
            key = (p.get("kind"), p.get("id") or p.get("text"))
            if key not in seen:
                seen.add(key)
                unique.append(p)
        pending = unique
        rnd["pending"] = pending
        brief = self._brief(c, anchor, ledger, pending)
        with self.store.lock:
            self.store._write_text(c["folder"] / "简报.md", brief)
        self._write_round(c)
        if self.repo is not None:
            roles = "、".join(f"{ROLE_LABELS.get(k, k)}{family_label(v.get('family'))}" for k, v in s.roles.items() if any(st.get("role") == k for st in rnd["steps"]))
            try:
                self.repo.commit_async(f"{item.ref} {c['label']}：{RECIPE_LABELS.get(s.recipe, s.recipe)}（{roles or '无模型调用'}）")
            except Exception:  # noqa: BLE001 - versioning must never break a round
                pass

    def _brief(self, c, anchor, ledger, pending):
        item, rnd, s = c["item"], c["round"], c["session"]
        status_text = {"done": "完成", "failed": "未完成", "cancelled": "已取消", "running": "进行中"}.get(rnd["status"], rnd["status"])
        lines = [f"# {c['label']}简报 · {item.ref} {item.title}", "",
                 f"- 讨论方式：{RECIPE_LABELS.get(s.recipe, s.recipe)}；模式：{'学习' if s.mode == 'learn' else '自动'}；状态：{status_text}",
                 f"- 时间：{rnd['started_at']} → {rnd['ended_at'] or '…'}"]
        if rnd.get("stop_reason"):
            lines.append(f"- 停下的原因：{rnd['stop_reason']}")
        lines += ["", f"## 需要你决定（{len(pending)}）", ""]
        lines += [f"- {p.get('id', '') + ' ' if p.get('id') else ''}{p['text']}" for p in pending] or ["- 没有"]
        ops = [o for o in rnd["applied"]["ops"]]
        lines += ["", "## 台账变化", ""]
        if ops:
            lines += ["| 原文 | 处理 | 说明 |", "|---|---|---|"]
            for o in ops[:60]:
                raw = o["raw"].replace("|", "\\|").replace("\n", " ")[:160]
                lines.append(f"| `{raw}` | {o['status']}{(' → ' + o['entry']) if o.get('entry') else ''} | {(o.get('reason') or '').replace('|', '/')[:160]} |")
        else:
            lines.append("- 没有变化")
        counts = L.counts(ledger)
        lines.append(f"\n当前台账：待定 {counts.get('待定', 0)} · 确定 {counts.get('确定', 0)} · 否定 {counts.get('否定', 0)}")
        revs = rnd["applied"]["revisions"]
        lines += ["", "## 正文改动", ""]
        lines += [f"- {r.get('section')}（{r.get('mode')}）：{r.get('status')}{('，' + r.get('reason')) if r.get('reason') and r.get('reason') != '__stage__' else ''}" for r in revs] or ["- 没有改动"]
        if rnd["applied"]["runs"]:
            lines += ["", "## 验证运行", ""]
            names = {"pass": "满足判据", "fail": "未满足判据", "error": "出错", "partial": "部分满足", "pending": "未完成"}
            for r in rnd["applied"]["runs"]:
                lines.append(f"- {r['id']} {r['title']}：{names.get(r['outcome'], r['outcome'])}。{clip(r.get('actual', ''), 600)}")
        if rnd["applied"].get("verifications"):
            lines += ["", "## 独立复推", ""]
            lines += [f"- {v['id']} → {v['entry']}：{v['verdict']}" for v in rnd["applied"]["verifications"]]
        if rnd.get("drift"):
            d = rnd["drift"]
            lines += ["", "## 漂移检查", "", f"- 判定：{d.get('verdict', '未给出')}",
                      f"- 仍在回答锚点的问题：{d.get('question_ok', '')}", f"- 锚点外的假设：{d.get('extra_assumptions', '')}",
                      f"- 判据或范围变化：{d.get('criterion_changed', '')}"]
        if rnd.get("summary"):
            lines += ["", "## 作者的结论摘要（仅供参考，不是证据）", ""]
            lines += [f"- {k}：{v}" for k, v in rnd["summary"].items()]
        if rnd.get("notes"):
            lines += ["", "## 其他", ""] + [f"- {n}" for n in rnd["notes"]]
        lines += ["", "## 本轮调用", ""]
        for st in rnd["steps"]:
            if st.get("status") == "skipped":
                lines.append(f"- {st['label']}：跳过（{st.get('error', '')}）")
                continue
            u = st.get("usage") or {}
            cost = f"，约 ${st['cost_usd']:.4f}" if st.get("cost_usd") else ""
            lines.append(f"- {st['label']}：{st.get('model_label', '')}，{st.get('duration_s', 0)} 秒，输入 {u.get('input_tokens', 0)} / 输出 {u.get('output_tokens', 0)} token{cost}"
                         + ("" if st.get("status") == "done" else f"，未完成：{st.get('error', '')}"))
        lines.append("")
        return "\n".join(lines)

    def _stop_reason(self, c):
        rnd, item, s = c["round"], c["item"], c["session"]
        if rnd["status"] != "done":
            return rnd.get("stop_reason") or "本轮没有完成"
        if any(p["kind"] in {"request", "decision"} for p in rnd["pending"]):
            return "有需要你决定的事项，自动推进已暂停"
        drift = rnd.get("drift") or {}
        if "漂移" in (drift.get("verdict") or "") and "未漂移" not in (drift.get("verdict") or ""):
            return "漂移检查认为讨论偏离了锚点，请你决定怎么处理"
        progress = rnd["progress"]
        if rnd.get("failed_calls"):
            return "工程调用没有完成：" + "；".join(rnd["failed_calls"]) + "。验证计划已保留，等额度恢复后会重新运行。"
        if s.recipe in {"advance", "outline", "refine"} and not (progress["ledger_changes"] or progress["revisions"] or progress["runs"]):
            return "这一轮正文和台账都没有变化，先停下，避免空转"
        if item.level == "question" and s.recipe == "advance":
            ledger, _ = item.ledger()
            open_checks = [e for e in ledger.entries if e.type == "待验证" and e.state == "待定"]
            judged = [r for r in self.store._records(item.record, "结果", "run", "R") if r.get("outcome") in {"pass", "fail", "partial"}]
            waiting = self._unrun_plans(item)
            if judged and not open_checks and not waiting:
                return "待验证项都有了结果，可以请审稿人独立审一遍，然后采纳"
        return ""


# ---------------------------------------------------------------------------
# Helpers shared with v2api
# ---------------------------------------------------------------------------

def engineer_python():
    """Full path of a real python.exe for sandboxed runs (the py launcher cannot see installs there)."""
    import os
    import sys
    candidates = [os.environ.get("RESEARCH_NOTEBOOK_PYTHON", ""), sys.executable,
                  str(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Python" / "Python312" / "python.exe")]
    for value in candidates:
        if value and Path(value).is_file() and "WindowsApps" not in value and Path(value).name.lower().startswith("python"):
            return value
    return sys.executable


SECRET_PATTERNS = [re.compile(p) for p in (rb"refresh_token", rb"ANTHROPIC_AUTH_TOKEN", rb"ANTHROPIC_API_KEY", rb"OPENAI_API_KEY",
                                              rb"\bsk-[A-Za-z0-9_-]{16,}", rb"\bgh[pousr]_[A-Za-z0-9]{20,}", rb"github_pat_[A-Za-z0-9_]{20,}",
                                              rb"AIza[0-9A-Za-z_-]{30,}", rb"-----BEGIN [A-Z ]*PRIVATE KEY-----")]
IMPORT_MAX_FILES = 200
IMPORT_MAX_BYTES = 200 * 1024 * 1024


def looks_secret(data: bytes) -> bool:
    return any(p.search(data[:2_000_000]) for p in SECRET_PATTERNS)


def safe_workspace_files(workspace):
    """Regular files inside the workspace only: never follow junctions, symlinks or hard links out of it."""
    import os
    import stat as statmod
    root = Path(workspace).resolve()
    files, skipped, total = [], [], 0
    for folder, dirs, names in os.walk(root, followlinks=False):
        here = Path(folder)
        keep = []
        for d in dirs:
            p = here / d
            if p.is_symlink() or (hasattr(os.path, "isjunction") and os.path.isjunction(p)):
                skipped.append(f"跳过了链接目录 {p.relative_to(root)}")
            else:
                keep.append(d)
        dirs[:] = keep
        for name in names:
            p = here / name
            try:
                st = os.lstat(p)
            except OSError:
                continue
            if not statmod.S_ISREG(st.st_mode) or st.st_nlink != 1 or not p.resolve().is_relative_to(root):
                skipped.append(f"跳过了不是普通文件的 {p.relative_to(root)}")
                continue
            if len(files) >= IMPORT_MAX_FILES or total + st.st_size > IMPORT_MAX_BYTES:
                skipped.append(f"导入上限（{IMPORT_MAX_FILES} 个文件 / 200 MB）已满，{p.relative_to(root)} 未导入")
                continue
            total += st.st_size
            files.append(p)
    return files, skipped


def md_href(relative):
    """Readable Markdown link target: keep Chinese text, escape only what breaks link syntax."""
    return "".join({" ": "%20", "(": "%28", ")": "%29", "<": "%3C", ">": "%3E", "%": "%25"}.get(ch, ch) for ch in relative)


def extract_block(text, name):
    match = re.search(r"(?ms)^(?P<fence>`{3,}|~{3,})[ \t]*" + re.escape(name) + r"[^\n]*\n(?P<body>.*?)^(?P=fence)[ \t]*$", text or "")
    return match.group("body").strip() if match else ""


def candidate_title(markdown):
    match = re.search(r"^#\s+(.+)$", markdown or "", re.M)
    return (match.group(1).strip() if match else "候选框架")[:120]


def section_text(doc, section):
    """Best-effort extraction of one section, used only for side-by-side display."""
    lines = (doc or "").split("\n")
    key = (section or "").strip()
    number = re.match(r"^(\d+(?:\.\d+)*)", key)
    start, level = None, 0
    for i, line in enumerate(lines):
        m = re.match(r"^(#{2,4})\s+(.*)$", line)
        if not m:
            continue
        heading = m.group(2).strip()
        if start is None:
            if number and re.match(re.escape(number.group(1)) + r"(?:[\s.、．]|$)", heading):
                start, level = i, len(m.group(1))
            elif not number and re.sub(r"[\s\d.、．]", "", heading) == re.sub(r"[\s\d.、．]", "", key):
                start, level = i, len(m.group(1))
        elif len(m.group(1)) <= level:
            return "\n".join(lines[start:i]).strip()
    return "\n".join(lines[start:]).strip() if start is not None else ""


COMPARATORS = {"<": "<", "<=": "<=", ">": ">", ">=": ">=", "≤": "<=", "≥": ">=", "＜": "<", "＞": ">", "=<": "<=", "=>": ">=",
               "abs<": "abs<", "abs<=": "abs<=", "|x|<": "abs<"}
LOOSER = {"<": lambda frozen, got: got > frozen, "<=": lambda frozen, got: got > frozen, "abs<": lambda frozen, got: got > frozen,
          "abs<=": lambda frozen, got: got > frozen, ">": lambda frozen, got: got < frozen, ">=": lambda frozen, got: got < frozen}
CRITERION_RE = re.compile(r"(abs\s*<=?|\|[^|]{0,40}\|\s*(?:<=?|≤|＜)|<=|>=|=<|=>|≤|≥|＜|＞|<|>)\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:\s*[eE]\s*[-+]?\d+|\s*[×xX]\s*10\s*\^?\s*\{?\s*[-−]?\d+\s*\}?)?)")


def parse_number(text):
    value = str(text).replace(" ", "").replace("−", "-")
    match = re.fullmatch(r"([-+]?(?:\d+\.?\d*|\.\d+))(?:[×xX]10\^?\{?([-+]?\d+)\}?)?", value)
    if match and match.group(2):
        return float(match.group(1)) * 10 ** int(match.group(2))
    try:
        return float(value)
    except ValueError:
        return None


def parse_criteria(text):
    """Frozen machine-readable thresholds from a plan's 判据, e.g. '最大相对误差 < 1e-10' -> [('<', 1e-10)]."""
    found = []
    for match in CRITERION_RE.finditer(text or ""):
        raw = re.sub(r"\s", "", match.group(1))
        if raw.startswith("|"):
            comparator = "abs<=" if ("<=" in raw or "≤" in raw) else "abs<"
        else:
            comparator = COMPARATORS.get(raw.replace("abs", "abs"), COMPARATORS.get(raw))
        value = parse_number(match.group(2))
        if comparator and value is not None:
            found.append({"comparator": comparator, "threshold": value})
    return found


def judge_summary(path, frozen=None):
    """Decide a run outcome from summary.json. The system judges, not the model.

    Fails closed: anything malformed or not re-checkable is 'error', never 'pass'.
    frozen: thresholds parsed from the plan's 判据 when it was registered; the summary
    must test each of them with the same comparator and a threshold no looser.
    """
    if path is None:
        return "error", "没有找到 summary.json，无法按判据判断。", {}
    try:
        summary = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return "error", f"summary.json 无法读取：{exc}", {}
    if not isinstance(summary, dict):
        return "error", "summary.json 不是对象", {}
    lines, problems = [], []
    selfcheck = summary.get("selfcheck_passed")
    lines.append(f"退化自检：{'通过' if selfcheck is True else '未通过' if selfcheck is False else '未报告'}")
    if not isinstance(selfcheck, bool):
        problems.append("selfcheck_passed 必须是 true 或 false")
    criteria = summary.get("criteria")
    if not isinstance(criteria, list) or not criteria:
        return "error", "\n".join(lines + ["summary.json 没有 criteria 列表，无法复核"]), summary
    checked, all_pass, mismatch = [], True, []
    for index, crit in enumerate(criteria):
        if not isinstance(crit, dict):
            problems.append(f"criteria 第 {index + 1} 项不是对象")
            continue
        name = str(crit.get("name", f"判据{index + 1}"))
        comparator = COMPARATORS.get(str(crit.get("comparator", "")).strip())
        observed, threshold = parse_number(crit.get("observed")) if crit.get("observed") is not None else None, \
            parse_number(crit.get("threshold")) if crit.get("threshold") is not None else None
        reported = crit.get("passed")
        if comparator is None or observed is None or threshold is None or not isinstance(reported, bool):
            problems.append(f"“{name}”缺少数值的 observed / threshold、可识别的 comparator 或布尔值 passed")
            continue
        passed = compare(observed, threshold, comparator)
        if passed != reported:
            mismatch.append(name)
        checked.append({"name": name, "comparator": comparator, "threshold": threshold, "observed": observed, "passed": passed})
        all_pass = all_pass and passed
        lines.append(f"{name}：观测 {observed:g} {comparator} 阈值 {threshold:g} → {'通过' if passed else '未通过'}（已复核）")
    ind = summary.get("independent_check")
    if ind is not None:
        lines.append(f"独立核对：{json.dumps(ind, ensure_ascii=False)[:300]}")
    if frozen:
        for want in frozen:
            match = next((c for c in checked if c["comparator"] == want["comparator"]
                          and not LOOSER.get(want["comparator"], lambda a, b: a != b)(want["threshold"], c["threshold"])), None)
            if match is None:
                problems.append(f"计划写定的判据 {want['comparator']} {want['threshold']:g} 没有在 summary.json 里按同样或更严的阈值检验")
            else:
                lines.append(f"计划判据 {want['comparator']} {want['threshold']:g}：由“{match['name']}”检验")
    elif frozen is not None:
        problems.append("计划没有可核对的阈值（判据要写成“量 比较符号 阈值”，例如 最大相对误差 < 1e-10）")
    if mismatch:
        problems.append("summary 中的 passed 与数值不符：" + "、".join(mismatch))
    if problems:
        return "error", "\n".join(lines + ["无法按判据复核：" + "；".join(problems)]), summary
    if selfcheck is False or not all_pass:
        return "fail", "\n".join(lines), summary
    return "pass", "\n".join(lines), summary


def compare(observed, threshold, comparator):
    try:
        o, t = float(observed), float(threshold)
    except (TypeError, ValueError):
        return None
    ops = {"<": o < t, "<=": o <= t, ">": o > t, ">=": o >= t, "abs<": abs(o) < t, "abs<=": abs(o) <= t}
    return ops.get(COMPARATORS.get((comparator or "").strip()))
