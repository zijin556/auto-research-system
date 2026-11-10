"""HTTP views and actions for the v2 page (``static/v2``).

GET  /api/v2/<name>   -> V2.get(name, params)
POST /api/action      -> V2.action(name, data) for action names starting with ``v2_``
The JSON shapes follow ``.runtime/v2_spec.md`` section D.4.
"""
from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from pathlib import Path

from . import agents
from . import ledger as L
from . import rounds as R
from .store import CHAPTER_ROLES, ConflictError, STATUS_LABELS, slug

OUTCOME_LABELS = {"pending": "未完成", "pass": "满足判据", "fail": "未满足判据", "partial": "部分满足", "error": "运行出错"}
REF_RE = re.compile(r"^(FW|F\d{2,}|C\d{2,}(?:-Q\d{2,})?)$")


def _git_toplevel(root):
    try:
        proc = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"], capture_output=True, encoding="utf-8",
                              errors="replace", timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired):
        return None
    return Path(proc.stdout.strip()).resolve() if proc.returncode == 0 and proc.stdout.strip() else None


class V2:
    def __init__(self, store):
        self.store = store
        self.settings = agents.Settings(store.root)
        self.repo = self._make_repo()
        self.rounds = R.RoundManager(store, self.repo)
        self._cache = {}
        self._cache_lock = threading.Lock()

    # infrastructure -----------------------------------------------------------
    def _make_repo(self):
        try:
            from . import vcs
        except ImportError:
            return None
        top = _git_toplevel(self.store.root)
        # Version only a research root that is itself the top of a repository.
        # The example root lives inside the main repository and must not commit there.
        if top is None or top != Path(self.store.root).resolve():
            return None
        try:
            # write_lock: git never stages a file while the store is replacing it.
            repo = vcs.Repo(self.store.root, settings_getter=lambda: self.settings.load()[0].get("git", {}), write_lock=self.store.lock,
                            recent_seconds=vcs.RECENT_SECONDS)
            # Idempotent: guarantees `* -text` and the ignore rules before any auto-commit.
            repo.ensure()
            return repo
        except Exception:  # noqa: BLE001 - versioning is optional
            return None

    def cached(self, key, seconds, fn):
        with self._cache_lock:
            hit = self._cache.get(key)
            if hit and time.monotonic() - hit[0] < seconds:
                return hit[1]
        value = fn()
        with self._cache_lock:
            self._cache[key] = (time.monotonic(), value)
        return value

    def commit(self, message):
        if self.repo is None:
            return
        settings, _ = self.settings.load()
        if not settings.get("git", {}).get("auto_commit", True):
            return
        try:
            self.repo.commit_async(message)
        except Exception:  # noqa: BLE001 - never block the user's save on git
            pass

    CLASSIC_LABELS = {"save_body": "保存正文", "update_question": "修改问题", "save_project": "修改项目卡", "save_framework": "修改框架",
                      "save_content": "修改研究内容", "create_question": "新建问题", "save_discussion": "收录讨论", "save_plan": "登记验证计划",
                      "create_run": "新建运行", "update_run": "更新运行", "add_code": "添加代码", "attach_file": "附加文件",
                      "save_judgment": "记录判断", "save_draft": "保存草稿", "append_draft": "追加草稿", "generate_prompt": "生成提示词",
                      "import_outline": "导入目录", "import_materials": "导入材料", "import_capture": "归档讨论",
                      "import_semantic": "归档讨论", "import_semantic_job": "归档讨论", "sync": "刷新"}

    def after_classic(self, action, data=None):
        if action.startswith("preview_") or action in {"open_folder", "sync", "generate_prompt"}:
            return
        data = data or {}
        ref = data.get("question_id") or data.get("content_id") or data.get("id") or ""
        ref = ref if isinstance(ref, str) and REF_RE.match(ref) else ""
        self.commit(f"{ref + ' ' if ref else ''}手动（经典版）：{self.CLASSIC_LABELS.get(action, action)}")

    def close(self):
        self.rounds.close()
        if self.repo is not None and hasattr(self.repo, "close"):
            try:
                self.repo.close()
            except Exception:  # noqa: BLE001
                pass

    # GET -------------------------------------------------------------------------
    def get(self, name, params):
        one = lambda key, default="": (params.get(key) or [default])[0]
        if name == "tree":
            return self.tree()
        if name == "item":
            return self.item(one("ref", "FW"), one("fw"))
        if name == "session":
            return self.rounds.snapshot(one("id"))
        if name == "settings":
            return self.settings_view()
        if name == "usage":
            return {"quota": self.quota(), "summary": agents.UsageLedger(self.store.root).summary()}
        if name == "git":
            return self.git_status()
        if name == "history":
            path = one("path")
            self.store.read_file(path)
            if self.repo is None:
                return {"commits": [], "message": "这个目录没有启用版本管理"}
            return {"commits": self.repo.history(path)}
        if name == "version":
            path, rev = one("path"), one("rev")
            self.store.read_file(path)
            if self.repo is None:
                raise ValueError("这个目录没有启用版本管理")
            return {"path": path, "rev": rev, "text": self.repo.show(rev, path).decode("utf-8", errors="replace")}
        if name == "diff":
            return self.diff(one("path"), one("rev"), one("base", "parent"))
        raise FileNotFoundError("没有这个接口")

    def diff(self, path, rev, base):
        import difflib
        target = self.store.read_file(path)
        if self.repo is None:
            raise ValueError("这个目录没有启用版本管理")
        new_text = self.repo.show(rev, path).decode("utf-8", errors="replace")
        if base == "current":
            old_text, old_label, new_label = new_text, f"{rev[:8]}", "当前版本"
            new_text = target.read_text(encoding="utf-8", errors="replace")
        else:
            commits = self.repo.history(path, 200)
            index = next((i for i, c in enumerate(commits) if c["sha"].startswith(rev) or rev.startswith(c["short"])), None)
            parent = commits[index + 1]["sha"] if index is not None and index + 1 < len(commits) else ""
            old_text = self.repo.show(parent, path).decode("utf-8", errors="replace") if parent else ""
            old_label, new_label = (parent[:8] if parent else "（无）"), rev[:8]
        lines = list(difflib.unified_diff(old_text.splitlines(), new_text.splitlines(), old_label, new_label, lineterm="", n=3))
        truncated = len(lines) > 4000
        return {"diff": "\n".join(lines[:4000]), "base": base, "truncated": truncated}

    def quota(self):
        return self.cached("quota", 20, lambda: agents.quota_status(self.store.root, self.settings.load()[0]))

    def git_status(self):
        if self.repo is None:
            return {"available": False, "is_repo": False, "message": "这个目录没有启用版本管理"}
        return self.cached("git", 5, self.repo.status)

    def settings_view(self):
        data, revision = self.settings.load()
        providers = self.cached("providers", 600, lambda: agents.provider_status(False))
        return {"settings": data, "revision": revision, "providers": providers, "quota": self.quota(),
                "prices": getattr(agents, "PRICES", {}), "role_labels": R.ROLE_LABELS}

    # tree ------------------------------------------------------------------------
    def tree(self):
        state = self.store.state()
        contents = {c["id"]: c for c in state["contents"]}
        by_content = {}
        for q in state["questions"]:
            by_content.setdefault(q["content_id"], []).append(q)
        placed = set()
        frameworks = []
        total = 0
        for f in state["frameworks"]:
            chapters = []
            for no, cid in enumerate(f["content_ids"], 1):
                content = contents.get(cid)
                if not content:
                    continue
                placed.add(cid)
                chapter = self._chapter_node(content, by_content.get(cid, []), no)
                chapters.append(chapter)
            frameworks.append({"id": f["id"], "title": f["title"], "status": f["status"], "chapters": chapters})
        unplaced = [self._chapter_node(c, by_content.get(c["id"], []), None) for c in state["contents"] if c["id"] not in placed]
        for node in [ch for f in frameworks for ch in f["chapters"]] + unplaced:
            total += node["pending"] + sum(q["pending"] for q in node["questions"])
        fw_item = R.Item(self.store, "FW")
        fw_pending = len(self.pending(fw_item))
        total += fw_pending
        return {"project": {"title": state["project"]["title"], "is_example": Path(self.store.root).name == "示例研究"},
                "framework_layer": {"anchor_status": self._anchor_status(fw_item), "pending": fw_pending},
                "frameworks": frameworks, "unplaced": unplaced, "session": self.rounds.active_session(),
                "pending_total": total, "git": self.git_status()}

    def _anchor_status(self, item):
        if not item.anchor_path.is_file():
            return "none"
        match = re.search(r'<!-- anchor \{[^\n]*"status"\s*:\s*"(\w+)"', item.read(item.anchor_path))
        return match.group(1) if match else "draft"

    def _chapter_node(self, content, questions, no):
        item = R.Item(self.store, content["id"], record=content)
        role = R.role_of(content)
        nodes = []
        for q in questions:
            qitem = R.Item(self.store, q["id"], record=q)
            try:
                ledger, _ = qitem.ledger()
                node = {"anchor_status": self._anchor_status(qitem), "ledger": L.counts(ledger), "pending": len(self.pending(qitem)), "error": ""}
            except (ValueError, OSError) as exc:
                # One unreadable file (for example saved as GBK) marks only its own node.
                node = {"anchor_status": "none", "ledger": {"待定": 0, "确定": 0, "否定": 0}, "pending": 0, "error": f"{exc}（请另存为 UTF-8）"}
            nodes.append({"id": q["id"], "title": q["title"], "status": q["status"], "status_label": q["status_label"], **node})
        try:
            anchor_status, pending, error = self._anchor_status(item), len(self.pending(item)), ""
        except (ValueError, OSError) as exc:
            anchor_status, pending, error = "none", 0, f"{exc}（请另存为 UTF-8）"
        return {"id": content["id"], "no": no, "title": content["title"], "role": role, "role_label": CHAPTER_ROLES.get(role, ""),
                "anchor_status": anchor_status, "pending": pending, "questions": nodes, "error": error}

    # pending ---------------------------------------------------------------------
    def pending(self, item):
        anchor, _ = item.anchor()
        state = item.state()
        out = [{"kind": "request", "id": r.id, "text": f"申请修改“{r.slot}”：{r.proposal}（理由：{r.reason}；来源：{r.source}）"} for r in L.anchor_blocking(anchor)]
        out += [{"kind": "assumption", "id": a.id, "text": f"{a.text}（来源：{a.source}）"} for a in L.pending_assumptions(anchor)]
        out += [{"kind": "decision", "id": d["id"], "text": d["text"], "needs_answer": bool(d.get("needs_answer")), "options": d.get("options", []),
                 "source": d.get("source", "")} for d in state.get("decisions", []) if d.get("status") == "待处理"]
        if state.get("anchor_draft") and item.level != "framework":
            draft = state["anchor_draft"]
            out.append({"kind": "anchor_draft", "id": draft.get("round_id", ""), "path": draft.get("path", ""), "round_id": draft.get("round_id", ""),
                        "text": "AI 起草了一份新锚点，现有锚点没有改动。可以打开对比，需要时用它替换。", "draft": draft.get("text", "")})
        active = self.rounds.active_session()
        if active and active.get("ref") == item.ref and active.get("status") == "paused":
            out.append({"kind": "learn", "id": active["id"], "text": "学习模式：这一步等你决定"})
        if state.get("proposed_questions"):
            out.append({"kind": "questions", "id": "", "text": f"AI 提出了 {len(state['proposed_questions'])} 个小问题，勾选后建立"})
        open_candidates = [c for c in state.get("candidates", {}).get("items", []) if not c.get("created")]
        if open_candidates and item.level == "framework_layer":
            out.append({"kind": "candidates", "id": "", "text": f"有 {len(open_candidates)} 个候选框架，选择要建立的"})
        if state.get("staged_revisions"):
            out.append({"kind": "revisions", "id": "", "text": f"有 {len(state['staged_revisions'])} 处 AI 修改因为你在讨论期间改过同一节而没有写入，请查看"})
        if item.level == "chapter" and state.get("stage_done"):
            ledger, _ = item.ledger()
            stage = state["stage_done"]
            if stage in {"S2", "S3"} and any(e.type == "想法" and e.state == "待定" for e in ledger.entries):
                out.append({"kind": "ideas", "id": "", "text": "请勾选要保留的想法"})
            if stage == "S4" and any(e.type == "文献" and e.state == "待定" for e in ledger.entries):
                out.append({"kind": "refs", "id": "", "text": "请逐条核对文献编号并勾选“已核实”"})
            out.append({"kind": "stage", "id": stage, "text": f"{stage} 已完成，确认后进入下一阶段"})
        return out

    # item ------------------------------------------------------------------------
    def item(self, ref, fw=""):
        if not REF_RE.match(ref or ""):
            raise ValueError("无法识别的条目编号")
        item = R.Item(self.store, ref, fw)
        anchor, anchor_rev = item.anchor()
        ledger, ledger_rev = item.ledger()
        session = self.rounds.active_session()
        out = {"ref": ref, "level": item.level, "title": item.title, "fw": fw,
               "breadcrumb": self.breadcrumb(item, fw),
               "anchor": self.anchor_view(item, anchor, anchor_rev),
               "ledger": {"path": item.rel(item.ledger_path), "revision": ledger_rev, "counts": L.counts(ledger),
                          "entries": [self.entry_view(e) for e in ledger.entries]},
               "pending": self.pending(item), "rounds": self.rounds_view(item), "latest_brief": self.latest_brief(item),
               "session": session if session and session.get("ref") == ref else None,
               "recipes": [{"key": k, "label": R.RECIPE_LABELS.get(k, k)} for k in self.rounds.recipes(item)],
               "default_recipe": self.rounds.default_recipe(item), "doc": None, "chapter": None, "materials": None,
               "upstream": [], "children": [], "frameworks": [], "stage": None, "proposed_questions": [], "candidates": [],
               "status": "", "status_label": "", "judgment": None, "can_adopt": False, "staged_conclusions": [], "staged_revisions": [], "stale": False,
               "answered_decisions": [{"id": d["id"], "text": d["text"], "answer": d.get("answer", ""), "decided_at": d.get("decided_at", "")} for d in item.state().get("decisions", []) if d.get("status") == "已处理"][-20:]}
        state = item.state()
        if item.level == "question":
            detail = self.store.detail(ref)
            content = self.store._content(detail["content_id"])
            no, _ = R.chapter_number(self.store, content["id"], fw)
            out["chapter"] = {"id": content["id"], "no": no, "title": content["title"], "role": R.role_of(content), "role_label": CHAPTER_ROLES.get(R.role_of(content), "")}
            out["doc"] = {"path": detail["body_path"], "text": detail["body"], "revision": detail["body_revision"], "kind": "mini_paper"}
            out["materials"] = self.materials(item, detail)
            out["upstream"] = self.upstream(item, anchor)
            out["status"], out["status_label"] = detail["status"], detail["status_label"]
            out["judgment"] = detail.get("latest_judgment")
            # An adopted conclusion whose upstream basis changed is no longer valid: say 需复查.
            out["stale"] = bool(out["upstream"]) and detail["status"] in {"supported", "refuted"}
            if out["stale"]:
                out["status_label"] = "需复查（上游有变化）"
            open_decisions = [d for d in state.get("decisions", []) if d.get("status") == "待处理"]
            out["can_adopt"] = (not L.anchor_blocking(anchor) and not L.pending_assumptions(anchor) and anchor.status == "confirmed"
                                and bool(detail["body"].strip()) and not out["upstream"] and not open_decisions)
            out["staged_conclusions"] = state.get("staged_conclusions", [])[-3:]
            out["staged_revisions"] = state.get("staged_revisions", [])[-10:]
        elif item.level == "chapter":
            detail = self.store.content_detail(ref)
            no, _ = R.chapter_number(self.store, ref, fw)
            role = R.role_of(item.record)
            out["chapter"] = {"id": ref, "no": no, "title": item.title, "role": role, "role_label": CHAPTER_ROLES.get(role, "")}
            out["doc"] = {"path": detail["draft_path"], "text": detail["draft"], "revision": detail["draft_revision"], "kind": "chapter_draft"}
            out["children"] = [{"ref": q["id"], "title": q["title"], "status": q["status"], "status_label": q["status_label"]} for q in detail["questions"]]
            out["proposed_questions"] = state.get("proposed_questions", [])
            if role == "research":
                current = state.get("stage", "S1")
                order = [k for k, _ in R.STAGES]
                out["stage"] = {"current": current, "done": state.get("stage_done", ""),
                                "stages": [{"key": k, "label": label, "done": order.index(k) < order.index(current) or state.get("stage_done") == k} for k, label in R.STAGES]}
        elif item.level == "framework":
            record = self.store._find(self.store._frameworks(), ref, "框架")
            out["doc"] = {"path": record["path"], "kind": "framework_fields", "revision": record["revision"], "text": item.doc_text(),
                          "fields": {key: record.get(key, "") for key in ("mainline", "chapters", "risks")}}
            out["frameworks"] = [self.framework_view(record)]
            out["status"] = record["status"]
        else:
            out["frameworks"] = [self.framework_view(f) for f in self.store._frameworks()]
            out["candidates"] = state.get("candidates", {}).get("items", [])
        return out

    def anchor_view(self, item, anchor, revision):
        labels = dict(L.ANCHOR_SLOTS[item.anchor_level])
        return {"path": item.rel(item.anchor_path), "revision": revision, "exists": item.anchor_path.is_file(), "level": item.anchor_level,
                "version": anchor.version, "status": anchor.status, "confirmed_at": anchor.confirmed_at or "", "ever_confirmed": R.ever_confirmed(anchor),
                "slots": [{"key": key, "label": label, "text": anchor.slots.get(key, "")} for key, label in L.ANCHOR_SLOTS[item.anchor_level] if key != "assumptions"],
                "assumptions_label": labels.get("assumptions", "假设"),
                "assumptions": [{"id": a.id, "text": a.text, "basis": a.basis, "failure": a.failure, "status": a.status, "source": a.source} for a in anchor.assumptions],
                "requests": [{"id": r.id, "slot": r.slot, "proposal": r.proposal, "reason": r.reason, "source": r.source, "status": r.status, "note": r.note} for r in anchor.requests]}

    @staticmethod
    def entry_view(e):
        return {"id": e.id, "type": e.type, "text": e.text, "state": e.state, "basis": e.basis, "evidence": e.evidence,
                "note": e.note, "source": e.source, "updated": e.updated, "superseded_by": getattr(e, "superseded_by", "")}

    def framework_view(self, f):
        chapters = []
        for no, cid in enumerate(f["content_ids"], 1):
            try:
                content = self.store._content(cid)
            except FileNotFoundError:
                continue
            role = R.role_of(content)
            chapters.append({"id": cid, "no": no, "title": content["title"], "role": role, "role_label": CHAPTER_ROLES.get(role, "")})
        return {"id": f["id"], "title": f["title"], "status": f["status"], "mainline": f.get("mainline", ""), "chapters_text": f.get("chapters", ""),
                "risks": f.get("risks", ""), "path": f["path"], "revision": f["revision"], "chapters": chapters}

    def breadcrumb(self, item, fw):
        crumbs = [{"ref": "FW", "title": "论文框架"}]
        frameworks = self.store._frameworks()
        framework = next((f for f in frameworks if f["id"] == fw), None) or next((f for f in frameworks if f["status"] == "active"), None)
        if item.level == "framework":
            crumbs.append({"ref": item.ref, "title": item.title})
            return crumbs
        if framework and item.level in {"chapter", "question"}:
            crumbs.append({"ref": framework["id"], "title": framework["title"]})
        if item.level == "chapter":
            no, _ = R.chapter_number(self.store, item.ref, fw)
            crumbs.append({"ref": item.ref, "title": (f"第{no}章 " if no else "") + item.title})
        if item.level == "question":
            content = self.store._content(item.record["content_id"])
            no, _ = R.chapter_number(self.store, content["id"], fw)
            crumbs.append({"ref": content["id"], "title": (f"第{no}章 " if no else "") + content["title"]})
            crumbs.append({"ref": item.ref, "title": item.title})
        return crumbs

    def rounds_view(self, item):
        if not item.rounds_dir.is_dir():
            return []
        out = []
        for folder in sorted((p for p in item.rounds_dir.iterdir() if p.is_dir() and re.match(r"第\d+轮_", p.name)), key=lambda p: p.name, reverse=True)[:40]:
            try:
                data = json.loads((folder / "round.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            if item.level == "framework" and data.get("ref") not in {item.ref, None}:
                continue
            out.append({"id": folder.name, "no": data.get("no"), "ref": data.get("ref", item.ref),
                        "title": f"第{data.get('no', '?')}轮 · {R.RECIPE_LABELS.get(data.get('recipe', ''), data.get('recipe', ''))}",
                        "status": data.get("status", ""), "started_at": data.get("started_at", ""), "stop_reason": data.get("stop_reason", ""),
                        "brief_path": item.rel(folder / "简报.md") if (folder / "简报.md").is_file() else "", "dir": item.rel(folder),
                        "files": [{"name": p.name, "path": item.rel(p)} for p in sorted(folder.iterdir()) if p.is_file() and p.suffix in {".md", ".json"}]})
        return out

    def latest_brief(self, item):
        rounds = self.rounds_view(item)
        for r in rounds:
            if r["brief_path"]:
                return self.store._read(self.store._safe(r["brief_path"]))
        return ""

    def materials(self, item, detail):
        result_dir = item.dir / "结果"
        runs = []
        for run in detail["runs"]:
            directory = self.store._safe(run["path"]).parent
            files = []
            for p in sorted(directory.rglob("*")):
                if p.is_file() and p.name not in {"运行记录.md", "计划快照.md"} and "代码快照" not in p.relative_to(directory).parts:
                    files.append({"name": p.name, "path": item.rel(p)})
            runs.append({"id": run["id"], "title": run["title"], "path": run["path"], "outcome": run.get("outcome", "pending"),
                         "outcome_label": OUTCOME_LABELS.get(run.get("outcome", "pending"), run.get("outcome", "")), "files": files,
                         "plan_id": run.get("plan_id", "")})
        verifications = []
        if item.verify_dir.is_dir():
            for p in sorted(item.verify_dir.glob("V*.md")):
                meta = re.search(r"<!-- verification (\{.*?\}) -->", p.read_text(encoding="utf-8", errors="replace"))
                data = json.loads(meta.group(1)) if meta else {}
                verifications.append({"id": data.get("id", p.stem), "path": item.rel(p), "entry": data.get("entry", ""), "verdict": data.get("verdict", "")})
        codes = [{"name": f["name"], "path": f["path"], "purpose": f.get("purpose", "")} for f in detail["files"] if f["kind"] == "code"]
        return {"codes": codes, "runs": runs, "verifications": verifications,
                "legacy_discussions": [{"id": d["id"], "title": d["title"], "path": d["path"]} for d in detail["discussions"]]}

    def upstream(self, item, anchor):
        """Assumptions resting on other questions' results: flag every one whose basis is not settled now.

        Accepts 'C02-Q01#L01', 'C02-Q01 L01', 'C02-Q01 的 L01' and a bare 'C02-Q01'.
        """
        flags = []
        refs = {}
        for a in anchor.assumptions:
            if a.status == "已拒绝":
                continue
            for match in re.finditer(r"(C\d{2,}-Q\d{2,})(?:\s*(?:#|的|中的|里的)?\s*(L\d{2,}(?:\s*[,，、]\s*L\d{2,})*))?", a.basis or ""):
                ids = re.findall(r"L\d{2,}", match.group(2) or "") or [None]
                for eid in ids:
                    refs.setdefault(match.group(1), []).append((a.id, eid))
        for qid, uses in refs.items():
            if qid == item.ref:
                continue
            try:
                upstream = self.store.detail(qid)
            except FileNotFoundError:
                flags += [{"assumption": aid, "ref": qid, "message": f"{aid} 的依据 {qid} 不存在"} for aid, _ in uses]
                continue
            qitem = R.Item(self.store, qid)
            ledger, _ = qitem.ledger()
            for aid, eid in uses:
                if eid:
                    entry = L.find(ledger, eid)
                    if entry is None or entry.state != "确定":
                        flags.append({"assumption": aid, "ref": f"{qid}#{eid}", "message": f"{aid} 依据的 {qid}#{eid} 目前是“{entry.state if entry else '不存在'}”，需复查"})
                elif upstream["status"] not in {"supported", "not_required"}:
                    flags.append({"assumption": aid, "ref": qid, "message": f"{aid} 依据的 {qid} 当前为“{upstream['status_label']}”，需复查"})
        return flags

    # actions ------------------------------------------------------------------
    def action(self, name, data):
        handler = getattr(self, "act_" + name[3:], None) if name.startswith("v2_") else None
        if handler is None:
            raise ValueError("不支持的操作")
        return handler(data)

    def act_start(self, data):
        return self.rounds.start(data)

    def act_cancel(self, data):
        return self.rounds.cancel(data.get("session_id", ""))

    def act_learn(self, data):
        return self.rounds.learn(data.get("session_id", ""), data.get("decision", ""), data.get("note", ""))

    def act_save_doc(self, data):
        ref = data.get("ref", "")
        item = R.Item(self.store, ref)
        if item.level == "question":
            self.store.perform("save_body", {"question_id": ref, "text": data.get("text", ""), "revision": data.get("revision")})
        elif item.level == "chapter":
            self.store.perform("save_draft", {"content_id": ref, "text": data.get("text", ""), "revision": data.get("revision")})
        elif item.level == "framework":
            fields = data.get("fields") or {}
            self.store.perform("save_framework", {"id": ref, "revision": data.get("revision"), **{k: fields.get(k, "") for k in ("mainline", "chapters", "risks") if k in fields},
                                                  **({"title": data["title"]} if data.get("title") else {})})
        else:
            raise ValueError("这一层没有可编辑的正文")
        self.commit(f"{ref} 手动：保存正文")
        return self.item(ref, data.get("fw", ""))

    def _check(self, path, revision):
        current = self.store._revision(path)
        if revision is None or revision != current:
            raise ConflictError("文件已被修改（可能是 AI 正在写入）。请刷新后再保存，你的文字仍保留在编辑框里。")

    def act_save_anchor(self, data):
        ref = data.get("ref", "")
        item = R.Item(self.store, ref)
        with self.store.lock:
            self._check(item.anchor_path, data.get("revision", ""))
            anchor, _ = item.anchor()
            slots = data.get("slots") or {}
            if not isinstance(slots, dict):
                raise ValueError("锚点内容格式不正确")
            for key, _label in L.ANCHOR_SLOTS[item.anchor_level]:
                if key != "assumptions" and key in slots:
                    if not isinstance(slots[key], str) or len(slots[key]) > 20_000:
                        raise ValueError("锚点每一项应为 2 万字以内的文字")
                    anchor.slots[key] = slots[key].strip()
            if "assumptions" in data:
                incoming = data.get("assumptions") or []
                if not isinstance(incoming, list):
                    raise ValueError("假设列表格式不正确")
                existing = {a.id: a for a in anchor.assumptions}
                for row in incoming:
                    if not isinstance(row, dict) or not str(row.get("text", "")).strip():
                        continue
                    aid = row.get("id") if row.get("id") in existing else None
                    if aid:
                        a = existing[aid]
                        a.text, a.basis, a.failure = str(row.get("text", "")).strip(), str(row.get("basis", "")).strip(), str(row.get("failure", "")).strip()
                    else:
                        new = L.Assumption(id=L.next_assumption_id(anchor), text=str(row.get("text", "")).strip(), basis=str(row.get("basis", "")).strip(),
                                           failure=str(row.get("failure", "")).strip(), status="已确认", source="你")
                        anchor.assumptions.append(new)
                # Only assumptions the user explicitly deleted are rejected; ones the editor did not
                # know about (added by a round while the editor was open) stay untouched.
                deleted = data.get("deleted") or []
                if not isinstance(deleted, list):
                    raise ValueError("删除的假设列表格式不正确")
                for aid in deleted:
                    if aid in existing and existing[aid].status != "已拒绝":
                        existing[aid].status = "已拒绝"
                        existing[aid].source = (existing[aid].source + "；" if existing[aid].source else "") + "你删除了它"
            if anchor.status == "confirmed":
                anchor.status = "draft"
                anchor.version += 1
            item.save_anchor(anchor)
        self.commit(f"{ref} 手动：修改锚点")
        return self.item(ref, data.get("fw", ""))

    def act_confirm_anchor(self, data):
        ref = data.get("ref", "")
        item = R.Item(self.store, ref)
        with self.store.lock:
            self._check(item.anchor_path, data.get("revision", ""))
            anchor, _ = item.anchor()
            if not anchor.slots.get("question", "").strip() or not anchor.slots.get("criterion", "").strip():
                raise ValueError("请至少写清“要回答的问题”和“判据”再确认")
            for a in anchor.assumptions:
                if a.status == "待批":
                    a.status = "已确认"
            anchor.status = "confirmed"
            anchor.confirmed_at = R.utc()
            item.save_anchor(anchor)
            if item.level == "question":
                doc = item.read(item.doc_path) or L.skeleton("question", "")
                new = L.set_section(doc, "1", "问题与假设", L.render_anchor_section(anchor))
                if new != doc:
                    self.store._write_text(item.doc_path, new)
                q = self.store._question(ref)
                question, criterion = anchor.slots.get("question", "").strip(), anchor.slots.get("criterion", "").strip()
                if (q.get("question", "").strip(), q.get("criterion", "").strip()) != (question, criterion):
                    self.store.perform("update_question", {"id": ref, "revision": q["revision"], "question": question, "criterion": criterion})
        self.commit(f"{ref} 手动：确认锚点（第 {anchor.version} 版）")
        return self.item(ref, data.get("fw", ""))

    def act_decide(self, data):
        ref, kind, rid = data.get("ref", ""), data.get("kind", ""), data.get("id", "")
        approve, note = bool(data.get("approve")), str(data.get("note", ""))
        item = R.Item(self.store, ref)
        with self.store.lock:
            if kind == "request":
                anchor, _ = item.anchor()
                L.user_decide_request(anchor, rid, approve, note)
                item.save_anchor(anchor)
            elif kind == "assumption":
                anchor, _ = item.anchor()
                L.user_decide_assumption(anchor, rid, approve, note)
                item.save_anchor(anchor)
            elif kind == "decision":
                state = item.state()
                found = next((d for d in state.get("decisions", []) if d.get("id") == rid), None)
                if not found:
                    raise FileNotFoundError("找不到这项待决事项")
                found.update(status="已处理", answer=note, approved=approve, decided_at=R.utc())
                item.save_state(state)
            else:
                raise ValueError("不支持的决定类型")
            if kind in {"request", "assumption"} and item.level == "question":
                # Section 1 of the mini paper always mirrors the current anchor.
                anchor, _ = item.anchor()
                doc = item.read(item.doc_path)
                new = L.set_section(doc, "1", "问题与假设", L.render_anchor_section(anchor))
                if new != doc:
                    self.store._write_text(item.doc_path, new)
        self.commit(f"{ref} 手动：处理 {rid}（{'批准' if approve else '拒绝'}）")
        return self.item(ref, data.get("fw", ""))

    def act_set_entry(self, data):
        ref = data.get("ref", "")
        item = R.Item(self.store, ref)
        with self.store.lock:
            ledger, _ = item.ledger()
            results = L.user_set_state(ledger, data.get("id", ""), data.get("state", ""), str(data.get("note", "")))
            item.save_ledger(ledger)
            item.log(f"你在页面上操作 · {R.utc()}", results if isinstance(results, list) else [results] if results else [])
        self.commit(f"{ref} 手动：{data.get('id', '')} 设为{data.get('state', '')}")
        return self.item(ref, data.get("fw", ""))

    def act_adopt(self, data):
        ref = data.get("ref", "")
        item = R.Item(self.store, ref)
        if item.level != "question":
            raise ValueError("只有小问题需要采纳结论")
        decision = data.get("decision")
        if decision not in {"supported", "refuted", "inconclusive"}:
            raise ValueError("请选择：支持、不支持或证据不足")
        text = str(data.get("text", "")).strip()
        scope = str(data.get("scope", "")).strip()
        if not text:
            raise ValueError("请写一句结论")
        anchor, _ = item.anchor()
        if anchor.status != "confirmed":
            raise ValueError("锚点还没确认，不能采纳")
        if L.anchor_blocking(anchor) or L.pending_assumptions(anchor):
            raise ValueError("还有待批的假设或锚点变更申请，请先处理")
        flags = self.upstream(item, anchor)
        if flags:
            raise ValueError("上游结论有变化，需要先复查：" + "；".join(f["message"] for f in flags))
        open_decisions = [d for d in item.state().get("decisions", []) if d.get("status") == "待处理"]
        if open_decisions:
            raise ValueError("还有需要你决定的事项，请先在“待你决定”里处理")
        with self.store.lock:
            doc = item.read(item.doc_path)
            body = text + ("\n\n适用范围与局限：" + scope if scope else "")
            new = L.set_section(doc, "5", "结论与适用范围", body)
            if new != doc:
                self.store._write_text(item.doc_path, new)
            detail = self.store.detail(ref)
            evidence = ["body"] + [r["id"] for r in detail["runs"] if r.get("outcome") == "pass"]
            self.store.perform("save_judgment", {"question_id": ref, "decision": decision, "text": text,
                                                 "scope": scope or "见正文第 1 节的假设与范围", "meaning": "", "evidence_ids": evidence})
            state = item.state()
            if state.get("staged_conclusions"):
                state["staged_conclusions"] = []
                item.save_state(state)
        self.commit(f"{ref} 采纳：{STATUS_LABELS.get(decision, decision)}")
        return self.item(ref, data.get("fw", ""))

    def act_create_framework(self, data):
        title = str(data.get("title", "")).strip() or "新的论文框架"
        with self.store.lock:
            content_ids = []
            for chapter_title, role in R.DEFAULT_CHAPTERS:
                detail = self.store.perform("save_content", {"title": chapter_title, "objective": "", "role": role})
                content_ids.append(detail["id"])
                self._seed_chapter(detail["id"], role)
            active = any(f["status"] == "active" for f in self.store._frameworks())
            framework = self.store.perform("save_framework", {"title": title, "status": "candidate" if active else "active", "content_ids": content_ids,
                                                              "mainline": str(data.get("mainline", "")), "chapters": "", "risks": ""})
        self.commit(f"新建框架 {framework['id']}：{title}")
        return {"tree": self.tree(), "id": framework["id"]}

    def _seed_chapter(self, cid, role):
        item = R.Item(self.store, cid)
        draft = item.read(item.doc_path)
        if len(draft.strip().splitlines()) <= 1:
            self.store._write_text(item.doc_path, draft.rstrip() + "\n\n" + L.skeleton("chapter", role))

    def act_create_from_candidate(self, data):
        fw_item = R.Item(self.store, "FW")
        state = fw_item.state()
        items = state.get("candidates", {}).get("items", [])
        index = data.get("index")
        candidate = next((c for c in items if c.get("index") == index), None)
        if candidate is None:
            raise FileNotFoundError("找不到这个候选框架")
        parsed = parse_candidate(candidate["markdown"])
        title = str(data.get("title") or parsed["title"] or candidate.get("title") or "候选框架").strip()
        with self.store.lock:
            content_ids = []
            for chapter in parsed["chapters"] or [{"title": t, "role": r, "questions": []} for t, r in R.DEFAULT_CHAPTERS]:
                detail = self.store.perform("save_content", {"title": chapter["title"][:120], "objective": chapter.get("objective", ""), "role": chapter["role"]})
                content_ids.append(detail["id"])
                self._seed_chapter(detail["id"], chapter["role"])
                for q in chapter.get("questions", []):
                    created = self.store.perform("create_question", {"content_id": detail["id"], "title": q["title"][:150], "question": q.get("question", ""), "criterion": "", "why": "", "outline": ""})
                    self._seed_question(created["id"], q.get("question", "") or q["title"], "")
            active = any(f["status"] == "active" for f in self.store._frameworks())
            framework = self.store.perform("save_framework", {"title": title[:150], "status": "candidate" if active else "active", "content_ids": content_ids,
                                                              "mainline": parsed["mainline"], "chapters": candidate["markdown"][:200_000], "risks": parsed["risks"]})
            candidate["created"] = framework["id"]
            fw_item.save_state(state)
        self.commit(f"由候选建立框架 {framework['id']}：{title}")
        return {"tree": self.tree(), "id": framework["id"]}

    def _seed_question(self, qid, question, criterion):
        item = R.Item(self.store, qid)
        if not item.anchor_path.is_file():
            anchor = L.empty_anchor("question")
            anchor.slots["question"], anchor.slots["criterion"] = question, criterion
            item.save_anchor(anchor)
        if not item.read(item.doc_path).strip():
            self.store._write_text(item.doc_path, L.skeleton("question", ""))

    def act_create_questions(self, data):
        ref = data.get("ref", "")
        item = R.Item(self.store, ref)
        if item.level != "chapter":
            raise ValueError("请在章节页面建立小问题")
        rows = data.get("items") or []
        if not isinstance(rows, list) or not rows:
            raise ValueError("请至少选择一个小问题")
        created = []
        with self.store.lock:
            for row in rows:
                title = str(row.get("title", "")).strip()
                if not title:
                    continue
                q = self.store.perform("create_question", {"content_id": ref, "title": title[:150], "question": str(row.get("question", "")), "criterion": str(row.get("criterion", "")), "why": "", "outline": ""})
                self._seed_question(q["id"], str(row.get("question", "")) or title, str(row.get("criterion", "")))
                created.append(q["id"])
            state = item.state()
            titles = {str(r.get("title", "")).strip() for r in rows}
            state["proposed_questions"] = [p for p in state.get("proposed_questions", []) if str(p.get("title", "")).strip() not in titles]
            item.save_state(state)
        self.commit(f"{ref} 新建小问题：{'、'.join(created)}")
        return {"item": self.item(ref, data.get("fw", "")), "created": created}

    def act_new_question(self, data):
        return self.act_create_questions({"ref": data.get("ref", ""), "fw": data.get("fw", ""), "items": [{"title": data.get("title", ""), "question": data.get("question", ""), "criterion": ""}]})

    def act_set_chapter(self, data):
        ref = data.get("ref", "")
        content = self.store._content(ref)
        payload = {"id": ref, "revision": content["revision"]}
        if data.get("title"):
            payload["title"] = data["title"]
        if data.get("role"):
            payload["role"] = data["role"]
        self.store.perform("save_content", payload)
        self.commit(f"{ref} 手动：修改章节信息")
        return self.item(ref, data.get("fw", ""))

    def act_set_active(self, data):
        ref = data.get("ref", "")
        record = self.store._find(self.store._frameworks(), ref, "框架")
        self.store.perform("save_framework", {"id": ref, "revision": record["revision"], "status": "active"})
        self.commit(f"{ref} 设为主推框架")
        return {"tree": self.tree()}

    def act_pick_ideas(self, data):
        ref = data.get("ref", "")
        keep = set(data.get("keep") or [])
        item = R.Item(self.store, ref)
        with self.store.lock:
            ledger, _ = item.ledger()
            results = []
            for e in ledger.entries:
                if e.type == "想法" and e.state == "待定" and e.id not in keep:
                    out = L.user_set_state(ledger, e.id, "否定", "你在筛选时没有保留")
                    results += out if isinstance(out, list) else [out]
                elif e.type == "想法" and e.id in keep and "你选择保留" not in (e.note or ""):
                    e.note = ((e.note + "；") if e.note else "") + "你选择保留"
            item.save_ledger(ledger)
            item.log(f"你筛选想法 · {R.utc()}", [r for r in results if r])
        self.commit(f"{ref} 手动：筛选想法")
        return self.item(ref, data.get("fw", ""))

    def act_verify_refs(self, data):
        ref = data.get("ref", "")
        item = R.Item(self.store, ref)
        with self.store.lock:
            ledger, _ = item.ledger()
            results = []
            for eid in data.get("ids") or []:
                entry = L.find(ledger, eid)
                if entry is not None and entry.type == "文献":
                    out = L.user_set_state(ledger, eid, "确定", "你已打开核对")
                    results += out if isinstance(out, list) else [out]
            item.save_ledger(ledger)
            item.log(f"你核实文献 · {R.utc()}", [r for r in results if r])
        self.commit(f"{ref} 手动：核实文献")
        return self.item(ref, data.get("fw", ""))

    def act_next_stage(self, data):
        ref = data.get("ref", "")
        item = R.Item(self.store, ref)
        order = [k for k, _ in R.STAGES]
        with self.store.lock:
            state = item.state()
            current = state.get("stage", "S1")
            index = order.index(current) if current in order else 0
            state["stage"] = order[min(index + 1, len(order) - 1)]
            state["stage_done"] = ""
            item.save_state(state)
        self.commit(f"{ref} 进入 {state['stage']}")
        return self.item(ref, data.get("fw", ""))

    def act_dismiss(self, data):
        ref, kind = data.get("ref", ""), data.get("kind", "")
        item = R.Item(self.store, ref)
        with self.store.lock:
            state = item.state()
            if kind == "questions":
                state["proposed_questions"] = []
            elif kind == "candidates":
                state.setdefault("candidates", {})["items"] = []
            elif kind == "conclusion":
                state["staged_conclusions"] = []
            else:
                raise ValueError("不能忽略这一项")
            item.save_state(state)
        return self.item(ref, data.get("fw", ""))

    def act_settings_save(self, data):
        self.settings.save(data.get("settings") or {}, data.get("revision", ""))
        with self._cache_lock:
            self._cache.pop("quota", None)
        self.commit("手动：修改模型与额度设置")
        return self.settings_view()

    def act_test_provider(self, data):
        provider, model = data.get("provider", ""), data.get("model", "")
        effort = data.get("effort", "")
        if not isinstance(effort, str):
            effort = ""
        if provider not in {"codex", "agy", "claude"} or not isinstance(model, str) or not model:
            raise ValueError("请选择命令行和模型")
        spec = agents.CallSpec(provider=provider, model=model, prompt="【模板】connection_test\n这是连接测试。请只回复两个字：收到", mode="ask",
                               effort=effort or ("low" if provider == "codex" else ""), timeout_s=300, budget_usd=0.05 if provider == "claude" else None, label="连接测试")
        result = agents.run_call(spec)
        try:
            agents.UsageLedger(self.store.root).record(result, role="test", ref="", round_id="")
        except TypeError:
            agents.UsageLedger(self.store.root).record(result, "test", "", "")
        with self._cache_lock:
            self._cache.pop("quota", None)
        return {"ok": result.ok, "text": (result.text or "")[:200], "usage": result.usage, "cost_usd": result.cost_usd,
                "error": result.error, "error_class": result.error_class, "duration_s": round(result.duration_s, 1),
                "model_reported": result.model_reported, "rate_limits": result.rate_limits}

    def act_git_push(self, data):
        if self.repo is None:
            raise ValueError("这个目录没有启用版本管理")
        self.repo.push_async()
        with self._cache_lock:
            self._cache.pop("git", None)
        return self.git_status()

    def act_restore(self, data):
        path, rev = data.get("path", ""), data.get("rev", "")
        target = self.store.read_file(path)
        if self.repo is None:
            raise ValueError("这个目录没有启用版本管理")
        old = self.repo.show(rev, path)
        parts = Path(path).parts
        with self.store.lock:
            self._check(target, data.get("revision"))
            if len(parts) == 2 and parts[0] == "01_论文框架" and parts[1].startswith("F"):
                # A framework record: restore only the user's text fields; keep status and chapter list.
                fields = {}
                text = old.decode("utf-8", errors="replace").replace("\r\n", "\n")
                for match in re.finditer(r"<!-- field:([a-z_]+) -->\n## [^\n]+\n\n(.*?)\n<!-- /field:\1 -->", text, re.S):
                    if match.group(1) in {"mainline", "chapters", "risks"}:
                        fields[match.group(1)] = match.group(2)
                record = next(f for f in self.store._frameworks() if f["path"] == path)
                self.store.perform("save_framework", {"id": record["id"], "revision": record["revision"], **fields})
            elif parts[-1] == "锚点.md":
                anchor = L.parse_anchor(old.decode("utf-8", errors="replace"))
                current, _ = L.parse_anchor(self.store._read(target)), None
                anchor.status, anchor.version = "draft", max(anchor.version, current.version) + 1
                anchor.updated_at = R.utc()
                self.store._write_text(target, L.render_anchor(anchor))
            else:
                self.store._atomic(target, old)
                if parts[-1] == "推导笔记.md":
                    # Section 1 always mirrors the current anchor, also after restoring an old text.
                    qid = next((q["id"] for q in self.store._questions() if self.store._rel(self.store._qdir(q) / "推导笔记.md") == path), "")
                    if qid:
                        item = R.Item(self.store, qid)
                        anchor, _ = item.anchor()
                        if item.anchor_path.is_file():
                            doc = item.read(item.doc_path)
                            fixed = L.set_section(doc, "1", "问题与假设", L.render_anchor_section(anchor))
                            if fixed != doc:
                                self.store._write_text(item.doc_path, fixed)
            try:
                self.store.state()
            except ValueError as exc:
                # Never leave the project unreadable: undo the restore.
                self.store._atomic(target, self.repo.show(self._head_rev(path), path) if self._head_rev(path) else old)
                raise ValueError(f"恢复后项目无法读取（{exc}），已撤销这次恢复") from exc
        self.commit(f"恢复 {path} 到 {rev[:8]}")
        return {"ok": True, "path": path}

    def _head_rev(self, path):
        try:
            commits = self.repo.history(path, 1)
            return commits[0]["sha"] if commits else ""
        except Exception:  # noqa: BLE001
            return ""

    def act_apply_anchor_draft(self, data):
        ref = data.get("ref", "")
        item = R.Item(self.store, ref)
        with self.store.lock:
            state = item.state()
            draft = state.get("anchor_draft") or {}
            if not draft.get("path") or draft.get("path") != data.get("path", draft.get("path")):
                raise FileNotFoundError("找不到这份锚点草稿")
            self._check(item.anchor_path, data.get("revision", ""))
            current, _ = item.anchor()
            proposed = L.parse_anchor(self.store._read(self.store._safe(draft["path"])), item.anchor_level)
            # Keep the user's assumptions and the request history; add the draft's new assumptions as 待批.
            known = {L.norm_key(a.text) for a in current.assumptions}
            for a in proposed.assumptions:
                if L.norm_key(a.text) not in known:
                    current.assumptions.append(L.Assumption(id=L.next_assumption_id(current), text=a.text, basis=a.basis, failure=a.failure,
                                                            status="待批", source=a.source or "AI 草稿"))
            for key, value in proposed.slots.items():
                if value.strip():
                    current.slots[key] = value
            current.status, current.version = "draft", current.version + 1
            item.save_anchor(current)
            state.pop("anchor_draft", None)
            item.save_state(state)
        self.commit(f"{ref} 手动：用 AI 草稿替换锚点内容")
        return self.item(ref, data.get("fw", ""))

    def act_dismiss_anchor_draft(self, data):
        ref = data.get("ref", "")
        item = R.Item(self.store, ref)
        with self.store.lock:
            state = item.state()
            state.pop("anchor_draft", None)
            item.save_state(state)
        self.commit(f"{ref} 手动：忽略锚点草稿")
        return self.item(ref, data.get("fw", ""))

    def act_git_remote(self, data):
        if self.repo is None:
            raise ValueError("这个目录没有启用版本管理")
        url = str(data.get("url", "")).strip()
        branch = str(data.get("branch", "")).strip()
        result = self.repo.set_remote(url) if url else {}
        if branch:
            settings, revision = self.settings.load()
            settings.setdefault("git", {})["branch"] = branch
            self.settings.save(settings, revision)
        with self._cache_lock:
            self._cache.pop("git", None)
        return {"git": self.git_status(), "result": result}


def parse_candidate(markdown):
    """Parse a 候选框架 outline: # title, ## chapters (with optional '| role'), ### questions."""
    roles = {"绪论": "intro", "理论": "theory", "研究问题": "research", "研究": "research", "随框架而定": "framework", "框架": "framework",
             "总结": "conclusion", "总结与展望": "conclusion", "intro": "intro", "theory": "theory", "research": "research", "framework": "framework", "conclusion": "conclusion"}
    title, mainline, risks, chapters = "", "", "", []
    for raw in (markdown or "").splitlines():
        line = raw.strip()
        if line.startswith("# ") and not title:
            title = line[2:].strip()
        elif line.startswith("## "):
            text = line[3:].strip()
            role_text = ""
            if "|" in text:
                text, role_text = [part.strip() for part in text.split("|", 1)]
            name = re.sub(r"^第\s*[0-9一二三四五六七八九十]+\s*章\s*", "", text).strip() or text
            role = roles.get(role_text) or next((v for k, v in roles.items() if k in text), "research" if len(chapters) in {2, 3, 4} else "other")
            chapters.append({"title": name, "role": role, "questions": [], "objective": ""})
        elif line.startswith("### ") and chapters:
            chapters[-1]["questions"].append({"title": line[4:].strip(), "question": ""})
        elif line.startswith(("一句话主张：", "主张：")):
            mainline = line.split("：", 1)[1].strip()
        elif line.startswith(("风险：", "最大风险：")):
            risks = (risks + "\n" if risks else "") + line.split("：", 1)[1].strip()
        elif chapters and line and not line.startswith("#"):
            if chapters[-1]["questions"]:
                q = chapters[-1]["questions"][-1]
                q["question"] = (q["question"] + " " + line).strip()[:2000]
            else:
                chapters[-1]["objective"] = (chapters[-1]["objective"] + "\n" + line).strip()[:4000]
    return {"title": title, "mainline": mainline, "risks": risks, "chapters": chapters}
