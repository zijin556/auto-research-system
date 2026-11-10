"""Research records, with readable Markdown as the source of truth.

Small metadata comments contain only identifiers, relations and record states.
All authored text remains in readable Markdown sections. Section delimiters let
raw conversations contain arbitrary Markdown headings without corrupting the
record. Pure derivation and manuscript files have no special syntax.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote


class ConflictError(ValueError):
    """The source file changed since the user opened the editor."""


LABELS = {
    "project": {"brief": "给 AI 的简介", "background": "研究背景", "progress": "目前进度", "configuration": "可复用系统配置", "symbols": "符号约定", "data_sources": "已有数据与存放位置", "rules": "对 AI 的统一要求"},
    "framework": {"mainline": "论文主线", "chapters": "章节安排", "risks": "优点与风险"},
    "content": {"objective": "研究目标"},
    "question": {"question": "要回答的问题", "why": "为什么要问", "criterion": "判据", "outline": "分步推导提纲"},
    "discussion": {"ai": "AI", "raw": "原始粘贴内容", "summary": "要点", "understanding": "我的理解", "unresolved": "遗留问题"},
    "plan": {"target": "本轮验证目标", "criterion": "本轮冻结判据", "inputs": "输入与条件", "expected": "运行前的预期"},
    "run": {"target_step": "针对推导哪一步", "command": "运行命令", "parameters": "参数与环境", "mapping": "公式与代码行号对照表", "expected": "运行前的预期", "actual": "实际结果", "analysis": "观察与分析"},
    "judgment": {"text": "人工判断", "scope": "适用条件与局限", "meaning": "对论文的意义"},
    "prompt": {"text": "交接内容（生成不等于已发送）"},
    "code": {"purpose": "程序用途"},
}
STATUS_LABELS = {"discussing": "讨论中", "pending": "待验证", "running": "验证中", "needs_judgment": "待判断", "review": "需复查", "supported": "验证支持", "refuted": "验证不支持", "inconclusive": "证据不足", "not_required": "不需要验证"}
CHAPTER_ROLES = {"intro": "绪论", "theory": "理论", "research": "研究问题", "framework": "随框架而定", "conclusion": "总结与展望", "other": "其他"}
PROJECT_NAME = "00_项目卡.md"
CONTENT_ROOT = "02_研究内容"
FRAMEWORK_ROOT = "01_论文框架"
META_RE = re.compile(r"<!-- research-record\s+(\{[^\n]*\})\s+-->")
FIELD_RE = re.compile(r"<!-- field:([a-z_]+) -->\n## [^\n]+\n\n(.*?)\n<!-- /field:\1 -->", re.S)
MAX_TEXT = 8 * 1024 * 1024
MAX_ATTACHMENT = 32 * 1024 * 1024
TEXT_EXT = {".txt", ".md", ".py", ".m", ".r", ".jl", ".ipynb", ".json", ".csv", ".log", ".yaml", ".yml", ".toml", ".c", ".cpp", ".h", ".tex"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff"}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def natural_id(value):
    return tuple(int(part) if part.isdigit() else part for part in re.split(r"(\d+)", value))


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def clean_text(value, name="内容", allow_empty=True):
    if not isinstance(value, str):
        raise ValueError(f"{name}必须是文本")
    if len(value.encode("utf-8")) > MAX_TEXT:
        raise ValueError(f"{name}超过 8 MB，请拆分保存")
    if not allow_empty and not value.strip():
        raise ValueError(f"请填写{name}")
    if "\x00" in value:
        raise ValueError(f"{name}含有无效字符")
    return value.replace("\r\n", "\n").replace("\r", "\n")


def title_text(value):
    value = clean_text(value, "标题", False).strip()
    if "\n" in value or len(value) > 180:
        raise ValueError("标题应为 180 字以内的一行文字")
    return value


def slug(value):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")[:60]
    return value or "未命名"


class Store:
    def __init__(self, root: Path):
        self.root = Path(root).absolute().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        for name in (FRAMEWORK_ROOT, CONTENT_ROOT, "03_提示词", "04_交接包"):
            self._safe(name).mkdir(exist_ok=True)
        project = self._safe(PROJECT_NAME)
        if not project.exists():
            self._write_record(project, "project", {"id": "PROJECT", "title": "我的论文研究", "created_at": now(), "updated_at": now()}, {key: "" for key in LABELS["project"]}, new=True)
        self._install_prompts()
        self._refresh_overviews()

    def _safe(self, path):
        path = Path(path)
        candidate = path if path.is_absolute() else self.root / path
        if any(part in {"..", "."} for part in path.parts):
            raise PermissionError("不允许相对越界路径")
        resolved = candidate.resolve()
        if not resolved.is_relative_to(self.root):
            raise PermissionError("文件不在本研究工作区内")
        return resolved

    def _rel(self, path):
        return self._safe(path).relative_to(self.root).as_posix()

    def _read(self, path):
        path = self._safe(path)
        if not path.is_file():
            raise FileNotFoundError(f"文件不存在：{self._rel(path)}")
        if path.stat().st_size > MAX_TEXT:
            raise ValueError(f"记录过大：{self._rel(path)}")
        try:
            return path.read_text(encoding="utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
        except UnicodeDecodeError as exc:
            raise ValueError(f"记录必须使用 UTF-8：{self._rel(path)}") from exc

    def _revision(self, path):
        path = self._safe(path)
        return sha(path.read_bytes()) if path.exists() else ""

    def _check_revision(self, path, revision):
        if not isinstance(revision, str) or revision != self._revision(path):
            raise ConflictError("文件已被修改，或缺少版本信息。请刷新后再保存；你的未保存文字仍可复制保留。")

    def _atomic(self, path, data: bytes, new=False, history=True):
        path = self._safe(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if new and path.exists():
            raise ConflictError("文件已存在，不会覆盖原始记录")
        if path.exists():
            old = path.read_bytes()
            if old == data:
                return
            if history:
                history_path = self._safe(Path(".history") / path.relative_to(self.root) / (datetime.now().strftime("%Y%m%d-%H%M%S-%f") + "-" + sha(old)[:12] + ".bak"))
                history_path.parent.mkdir(parents=True, exist_ok=True)
                with history_path.open("xb") as out:
                    out.write(old)
        temp = self._safe(path.parent / (".tmp-" + uuid.uuid4().hex))
        try:
            with temp.open("xb") as out:
                out.write(data)
                out.flush()
                os.fsync(out.fileno())
            # All writes are serialized by the Store lock. The entrypoint owns
            # the process lock for this root.
            os.replace(temp, path)
        finally:
            if temp.exists():
                temp.unlink()

    def _write_text(self, path, text, **kwargs):
        self._atomic(path, clean_text(text).encode("utf-8"), **kwargs)

    def _record_text(self, kind, metadata, fields, extra=""):
        title = title_text(metadata["title"])
        metadata = {**metadata, "kind": kind, "schema": 1}
        sections = [f"# {title}", "", "<!-- research-record " + json.dumps(metadata, ensure_ascii=False, separators=(",", ":")) + " -->", ""]
        for key, label in LABELS[kind].items():
            value = clean_text(fields.get(key, ""), label)
            if re.search(r"<!--\s*/?(?:field:|research-record|generated:)", value):
                raise ValueError("粘贴内容包含系统记录边界，请改为附加文件后保存，避免混淆记录结构")
            sections += [f"<!-- field:{key} -->", "## " + label, "", value, f"<!-- /field:{key} -->", ""]
        if extra.strip():
            sections += [extra.strip(), ""]
        return "\n".join(sections)

    def _write_record(self, path, kind, metadata, fields, extra="", new=False):
        self._write_text(path, self._record_text(kind, metadata, fields, extra), new=new)

    def _load(self, path, kind=None):
        path = self._safe(path)
        text = self._read(path)
        matches = META_RE.findall(text)
        if len(matches) != 1:
            raise ValueError(f"记录信息缺失或重复，请检查：{self._rel(path)}")
        try:
            meta = json.loads(matches[0])
        except json.JSONDecodeError as exc:
            raise ValueError(f"记录信息损坏：{self._rel(path)}") from exc
        record_kind = meta.get("kind")
        if record_kind not in LABELS or (kind and record_kind != kind):
            raise ValueError(f"记录类型不正确：{self._rel(path)}")
        heading = re.search(r"^# (.+)$", text, re.M)
        if not heading:
            raise ValueError(f"记录缺少标题：{self._rel(path)}")
        fields = {}
        for match in FIELD_RE.finditer(text):
            key, value = match.groups()
            if key in fields:
                raise ValueError(f"重复的正文段落：{self._rel(path)}")
            fields[key] = value
        if set(fields) != set(LABELS[record_kind]):
            raise ValueError(f"正文段落边界缺失，请恢复该文件的 field 注释：{self._rel(path)}")
        extra = FIELD_RE.sub("", text)
        extra = META_RE.sub("", extra)
        extra = re.sub(r"^# .+\n?", "", extra, count=1, flags=re.M)
        extra = re.sub(r"<!-- generated:start -->.*?<!-- generated:end -->", "", extra, flags=re.S).strip()
        value = {**meta, **fields, "title": heading.group(1), "revision": self._revision(path), "path": self._rel(path)}
        value["_extra"] = extra
        return value

    @staticmethod
    def _public(value):
        return {key: item for key, item in value.items() if not key.startswith("_")}

    def _metadata(self, value):
        return {key: item for key, item in value.items() if key not in LABELS[value["kind"]] and key not in {"revision", "path", "_extra", "status_reason", "status_label", "counts", "latest_judgment", "artifacts"}}

    def _scan(self, pattern, kind, id_pattern, base=None):
        records, seen = [], set()
        # Glob from a literal base directory: titles may contain [ ] or other
        # glob characters, which must never be interpreted as a pattern.
        for path in sorted((base or self.root).glob(pattern)):
            if not path.is_file():
                continue
            value = self._load(path, kind)
            rid = value.get("id")
            if not isinstance(rid, str) or not re.fullmatch(id_pattern, rid):
                raise ValueError(f"编号不正确：{self._rel(path)}")
            if rid in seen:
                raise ValueError(f"发现重复编号 {rid}，请检查文件，不会自动丢弃任何一份")
            seen.add(rid)
            records.append(value)
        return sorted(records, key=lambda value: natural_id(value["id"]))

    def _project(self):
        return self._load(PROJECT_NAME, "project")

    def _contents(self):
        return self._scan(CONTENT_ROOT + "/C*/研究内容.md", "content", r"C\d{2,}")

    def _frameworks(self):
        values = self._scan(FRAMEWORK_ROOT + "/F*.md", "framework", r"F\d{2,}")
        active = [v for v in values if v.get("status") == "active"]
        if len(active) > 1:
            raise ValueError("有多个框架被标记为当前采用，请只保留一个 active")
        for value in values:
            if value.get("status") not in {"active", "candidate", "parked"}:
                raise ValueError(f"框架状态不正确：{value['id']}")
            if not isinstance(value.get("content_ids"), list):
                raise ValueError(f"框架引用列表不正确：{value['id']}")
        return values

    def _questions(self):
        values = self._scan(CONTENT_ROOT + "/C*/C*-Q*/问题.md", "question", r"C\d{2,}-Q\d{2,}")
        for value in values:
            if value.get("content_id") != value["id"].split("-")[0]:
                raise ValueError(f"问题与研究内容编号不一致：{value['id']}")
        return values

    def _find(self, values, rid, kind):
        if not isinstance(rid, str):
            raise ValueError(f"缺少{kind}编号")
        found = next((v for v in values if v["id"] == rid), None)
        if not found:
            raise FileNotFoundError(f"找不到{kind} {rid}")
        return found

    def _question(self, qid):
        return self._find(self._questions(), qid, "问题")

    def _content(self, cid):
        return self._find(self._contents(), cid, "研究内容")

    def _qdir(self, q):
        return self._safe(q["path"]).parent

    @staticmethod
    def _next(values, prefix):
        numbers = [int(v["id"][len(prefix):]) for v in values if v["id"].startswith(prefix) and v["id"][len(prefix):].isdigit()]
        return prefix + f"{max(numbers, default=0) + 1:02d}"

    def _records(self, q, folder, kind, prefix):
        pattern = "R*/运行记录.md" if kind == "run" else f"{prefix}*.md"
        values = self._scan(pattern, kind, re.escape(prefix) + r"\d{2,}", base=self._qdir(q) / folder)
        for value in values:
            if value.get("question_id") != q["id"]:
                raise ValueError(f"记录归属错误：{value['path']}")
        return values

    def _all_records(self, q):
        return {
            "discussions": self._records(q, "对话", "discussion", "D"),
            "plans": self._records(q, "验证计划", "plan", "P"),
            "runs": self._records(q, "结果", "run", "R"),
            "judgments": self._records(q, "结论", "judgment", "J"),
            "prompts": self._records(q, "交接包", "prompt", "T"),
        }

    def _status(self, q, records, artifacts=None):
        latest_j = max(records["judgments"], key=lambda r: r.get("created_at", ""), default=None)
        last_run = max(records["runs"], key=lambda r: r.get("updated_at", ""), default=None)
        last_plan = max(records["plans"], key=lambda r: r.get("created_at", ""), default=None)
        if latest_j:
            actual_results = (artifacts or self._artifact_summary(q, records))["results"]
            observed = latest_j.get("observed_result_files")
            if observed is None:
                # Legacy judgments know only results registered by older runs.
                # New files must not inherit an old scientific judgment.
                observed = [item for run in records["runs"] if run.get("updated_at", "") <= latest_j.get("created_at", "") for item in run.get("result_files", [])]
            observed_map = {item["path"]: item.get("sha256", "") for item in observed}
            actual_paths = {item["path"] for item in actual_results}
            changed_materials = actual_paths != set(observed_map) or any(file_sha(self._safe(item["path"])) != observed_map.get(item["path"]) for item in actual_results)
            changed_evidence = any(
                not self._safe(ref["source_path"]).exists()
                or self._revision(ref["source_path"]) != ref["sha256"]
                or not self._safe(ref["path"]).exists()
                or self._revision(ref["path"]) != ref["sha256"]
                for ref in latest_j.get("evidence_snapshots", [])
            )
            referenced_runs = [run for run in records["runs"] if run["id"] in latest_j.get("evidence_ids", [])]
            changed_results = any(
                not self._safe(ref["path"]).is_file()
                or file_sha(self._safe(ref["path"])) != ref["sha256"]
                for run in referenced_runs for ref in run.get("result_files", [])
            )
            changed_code_snapshots = any(
                not self._safe(ref["path"]).is_file()
                or file_sha(self._safe(ref["path"])) != ref["sha256"]
                for run in referenced_runs for ref in run.get("code_snapshots", [])
            )
            definition_changed = self._question_signature(q) != latest_j.get("question_signature", self._question_signature(q))
            body_changed = self._revision(self._qdir(q) / "推导笔记.md") != latest_j.get("body_revision", self._revision(self._qdir(q) / "推导笔记.md"))
            newer_run = last_run and last_run.get("updated_at", "") > latest_j.get("created_at", "")
            newer_plan = last_plan and last_plan.get("created_at", "") > latest_j.get("created_at", "")
            if not newer_run and not newer_plan:
                if changed_materials:
                    return "review", "本题有新增或变化的结果材料，旧判断尚未覆盖，请核对后重新判断", latest_j
                if changed_evidence or changed_results or changed_code_snapshots or definition_changed or body_changed:
                    return "review", "结论形成后正文、问题定义或证据已改变，请重新核对", latest_j
                return latest_j["decision"], "人工判断 " + latest_j["id"], latest_j
        if last_run and (not last_plan or last_run.get("created_at", "") >= last_plan.get("created_at", "")):
            return ("running", "已建运行，等待记录结果", latest_j) if last_run.get("outcome", "pending") == "pending" else ("needs_judgment", "结果已保存，等待人工判断", latest_j)
        if last_plan:
            return "pending", "已有冻结验证计划，等待验证", latest_j
        return "discussing", "从问题与分步推导开始", latest_j

    @staticmethod
    def _question_signature(q):
        return sha(json.dumps({key: q.get(key, "") for key in ("title", "question", "why", "criterion", "outline")}, ensure_ascii=False, sort_keys=True).encode("utf-8"))

    def _enrich_question(self, q, records=None):
        records = records or self._all_records(q)
        artifacts = self._artifact_summary(q, records)
        status, reason, judgment = self._status(q, records, artifacts)
        counts = {key: len(value) for key, value in records.items()}
        counts.update(codes=len(artifacts["codes"]), results=len(artifacts["results"]))
        return {**self._public(q), "status": status, "status_label": STATUS_LABELS[status], "status_reason": reason, "latest_judgment": self._public(judgment) if judgment else None, "counts": counts, "artifacts": artifacts}

    def _artifact_summary(self, q, records):
        files = self._file_list(q)
        ignored_parts = {"__pycache__", "node_modules", "代码快照", "证据快照"}
        codes = [item for item in files if item["kind"] == "code"
                 and not any(part in ignored_parts for part in Path(item["path"]).parts)
                 and Path(item["path"]).suffix.lower() not in {".pyc", ".pyo"}]
        result_dir = self._qdir(q) / "结果"
        runs_by_dir = {self._safe(run["path"]).parent: run["id"] for run in records["runs"]}
        results = []
        for item in files:
            path = self._safe(item["path"])
            if not path.is_relative_to(result_dir):
                continue
            parts = path.relative_to(result_dir).parts
            if path.name in {"运行记录.md", "计划快照.md"} or path.name.endswith(".用途.md") or any(part in ignored_parts for part in parts):
                continue
            rid = next((rid for directory, rid in runs_by_dir.items() if path.is_relative_to(directory)), "")
            results.append({"name": item["name"], "path": item["path"], "size": item["size"], "kind": "result", "run_id": rid})
        run_fields = ("id", "title", "path", "outcome", "actual", "code_paths", "code_snapshots", "result_files")
        judgment_fields = ("id", "title", "path", "decision", "text")
        return {"codes": codes, "results": results,
                "runs": [{k: r[k] for k in run_fields if k in r} for r in records["runs"]],
                "judgments": [{k: j[k] for k in judgment_fields if k in j} for j in records["judgments"]]}

    def _file_list(self, q):
        directory = self._qdir(q)
        files = []
        for path in sorted(directory.rglob("*")):
            if not path.is_file() or any(part.startswith(".") for part in path.relative_to(directory).parts):
                continue
            path = self._safe(path)
            rel = path.relative_to(directory).parts
            if rel[0] == "代码":
                kind = "document" if path.name.endswith(".用途.md") else "code"
            elif path.suffix.lower() in IMAGE_EXT:
                kind = "image"
            elif rel[0] == "结果":
                kind = "result"
            else:
                kind = {"对话": "discussion", "验证计划": "plan", "结论": "judgment", "交接包": "prompt", "资料": "attachment"}.get(rel[0], "document")
            item = {"name": path.name, "path": self._rel(path), "size": path.stat().st_size, "kind": kind}
            if kind == "code":
                info = self._safe(str(path) + ".用途.md")
                if info.exists():
                    item["purpose"] = self._load(info, "code")["purpose"]
                else:
                    try:
                        item["purpose"] = path.read_text(encoding="utf-8-sig").splitlines()[0][:300] if path.stat().st_size < MAX_TEXT else ""
                    except (UnicodeDecodeError, IndexError):
                        item["purpose"] = ""
            files.append(item)
        return files

    def state(self):
        with self.lock:
            contents = self._contents()
            frameworks = self._frameworks()
            questions = [self._enrich_question(q) for q in self._questions()]
            warnings = []
            cids = {c["id"] for c in contents}
            for f in frameworks:
                for cid in f["content_ids"]:
                    if cid not in cids:
                        warnings.append(f"{f['id']} 引用了不存在的研究内容 {cid}")
            for q in questions:
                if q["content_id"] not in cids:
                    warnings.append(f"{q['id']} 的研究内容不存在")
            return {"root": str(self.root), "project": {**self._public(self._project()), "root": str(self.root)}, "frameworks": [self._public(v) for v in frameworks], "contents": [self._public(v) for v in contents], "questions": questions, "warnings": warnings}

    def detail(self, qid):
        with self.lock:
            q = self._question(qid)
            records = self._all_records(q)
            body = self._qdir(q) / "推导笔记.md"
            public_records = {key: [self._public(v) for v in values] for key, values in records.items()}
            for run in public_records["runs"]:
                if run.get("plan_snapshot"):
                    run["frozen_plan"] = self._public(self._load(run["plan_snapshot"], "plan"))
            return {**self._enrich_question(q, records), **public_records, "body": self._read(body), "body_revision": self._revision(body), "body_path": self._rel(body), "files": self._file_list(q), "root": str(self.root), "absolute_path": str(self._qdir(q))}

    def content_detail(self, cid):
        with self.lock:
            content = self._content(cid)
            draft = self._safe(content["path"]).parent / "草稿.md"
            state = self.state()
            return {**self._public(content), "draft": self._read(draft), "draft_revision": self._revision(draft), "draft_path": self._rel(draft), "questions": [q for q in state["questions"] if q["content_id"] == cid], "placements": [{"id": f["id"], "title": f["title"], "status": f["status"], "position": f["content_ids"].index(cid) + 1} for f in state["frameworks"] if cid in f["content_ids"]]}

    def read_file(self, relative):
        if not isinstance(relative, str) or not relative or "\\" in relative or ":" in relative or relative.startswith("/"):
            raise PermissionError("无效的研究文件路径")
        raw = Path(relative)
        if any(part.startswith(".") for part in raw.parts):
            raise PermissionError("不公开隐藏文件或程序文件")
        path = self._safe(raw)
        if any(part.startswith(".") for part in path.relative_to(self.root).parts):
            raise PermissionError("不公开隐藏文件，即使通过链接访问")
        if not path.is_file():
            raise FileNotFoundError("文件不存在")
        allowed = {PROJECT_NAME, "总览.md", FRAMEWORK_ROOT + "/框架总览.md", "03_提示词/提示词.md", "工作流说明.md"}
        if relative in allowed:
            return path
        first = raw.parts[0]
        if first == FRAMEWORK_ROOT and len(raw.parts) == 2 and path.suffix == ".md" and path.name.startswith("F"):
            self._load(path, "framework")
            return path
        # Thesis-level workflow files of the framework layer.
        if first == FRAMEWORK_ROOT and len(raw.parts) == 2 and raw.parts[1] in {"锚点.md", "台账.md", "台账日志.md"}:
            return path
        if first == FRAMEWORK_ROOT and len(raw.parts) >= 3 and raw.parts[1] == "讨论":
            return path
        if first == CONTENT_ROOT:
            # Only documented content roots are exposed. Arbitrary files placed
            # elsewhere in the app root are never served.
            for content in self._contents():
                if path.is_relative_to(self._safe(content["path"]).parent):
                    return path
        if first == "04_交接包" and len(raw.parts) == 2 and path.suffix == ".md" and path.name.startswith("T"):
            self._load(path, "prompt")
            return path
        if first == "04_交接包" and len(raw.parts) == 3 and raw.parts[1] == "目录导入" and re.fullmatch(r"I_[A-Za-z0-9][A-Za-z0-9_-]{7,79}\.md", path.name):
            return path
        if first == "04_交接包" and len(raw.parts) >= 4 and raw.parts[1] == "AI整理" and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{7,79}", raw.parts[2]):
            manifest = self._safe(Path(*raw.parts[:3]) / "归档索引.json")
            if manifest.is_file():
                registered = json.loads(manifest.read_text(encoding="utf-8"))
                if relative in registered.get("file_paths", []):
                    return path
        raise PermissionError("该路径不属于已登记的研究资料")

    def _fields(self, kind, data, old=None):
        old = old or {}
        return {key: clean_text(data.get(key, old.get(key, "")), label) for key, label in LABELS[kind].items()}

    def _ids(self, data, key, allowed=None):
        value = data.get(key, [])
        if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
            raise ValueError(f"{key} 应为编号列表")
        if len(value) != len(set(value)):
            raise ValueError(f"{key} 中有重复编号")
        if allowed is not None and any(v not in allowed for v in value):
            raise ValueError(f"{key} 引用了不存在或不属于本题的记录")
        return value

    def _save_project(self, data):
        old = self._project()
        path = self._safe(old["path"])
        self._check_revision(path, data.get("revision"))
        meta = {**self._metadata(old), "title": title_text(data.get("title", old["title"])), "updated_at": now()}
        self._write_record(path, "project", meta, self._fields("project", data, old), old["_extra"])
        return self._public(self._project())

    def _save_framework(self, data):
        frameworks = self._frameworks()
        old = self._find(frameworks, data["id"], "框架") if data.get("id") else None
        if old:
            self._check_revision(old["path"], data.get("revision"))
        title = title_text(data.get("title", old["title"] if old else ""))
        status = data.get("status", old.get("status", "candidate") if old else "candidate")
        if status not in {"active", "candidate", "parked"}:
            raise ValueError("框架状态不正确")
        content_ids = self._ids({"content_ids": data.get("content_ids", old.get("content_ids", []) if old else [])}, "content_ids", {c["id"] for c in self._contents()})
        fid = old["id"] if old else self._next(frameworks, "F")
        path = self._safe(old["path"]) if old else self._safe(Path(FRAMEWORK_ROOT) / f"{fid}_{slug(title)}.md")
        meta = {"id": fid, "title": title, "created_at": old["created_at"] if old else now(), "updated_at": now(), "status": status, "content_ids": content_ids}
        fields = self._fields("framework", data, old)
        extra = old["_extra"] if old else ""
        # Validate all authored text before modifying any other active frame.
        self._record_text("framework", meta, fields, extra)
        # Changing the chosen framework is explicit in the save request. Every
        # previous active record receives a preserved historical version.
        if status == "active":
            for other in frameworks:
                if other["id"] != fid and other["status"] == "active":
                    other_meta = {**self._metadata(other), "status": "candidate", "updated_at": now()}
                    self._write_record(other["path"], "framework", other_meta, self._fields("framework", {}, other), other["_extra"])
        self._write_record(path, "framework", meta, fields, extra, new=not old)
        return self._public(self._load(path, "framework"))

    def _save_content(self, data):
        contents = self._contents()
        old = self._find(contents, data["id"], "研究内容") if data.get("id") else None
        if old:
            self._check_revision(old["path"], data.get("revision"))
        title = title_text(data.get("title", old["title"] if old else ""))
        cid = old["id"] if old else self._next(contents, "C")
        path = self._safe(old["path"]) if old else self._safe(Path(CONTENT_ROOT) / f"{cid}_{slug(title)}" / "研究内容.md")
        # Keep system metadata such as the chapter role across edits.
        meta = {**(self._metadata(old) if old else {}), "id": cid, "title": title, "created_at": old["created_at"] if old else now(), "updated_at": now()}
        if "role" in data:
            if data["role"] not in CHAPTER_ROLES:
                raise ValueError("章节角色不正确")
            meta["role"] = data["role"]
        self._write_record(path, "content", meta, self._fields("content", data, old), old["_extra"] if old else "", new=not old)
        draft = path.parent / "草稿.md"
        if not draft.exists():
            self._write_text(draft, f"# {title} · 论文草稿\n\n", new=True)
        return self.content_detail(cid)

    def _create_question(self, data):
        content = self._content(data.get("content_id"))
        title = title_text(data.get("title", ""))
        qid = self._next([q for q in self._questions() if q["content_id"] == content["id"]], content["id"] + "-Q")
        directory = self._safe(content["path"]).parent / (qid + "_" + slug(title))
        for folder in ("对话", "代码", "验证计划", "结果", "结论", "交接包", "资料"):
            self._safe(directory / folder).mkdir(parents=True, exist_ok=True)
        fields = self._fields("question", data)
        fields["question"] = fields["question"] or title
        self._write_record(directory / "问题.md", "question", {"id": qid, "content_id": content["id"], "title": title, "created_at": now(), "updated_at": now()}, fields, new=True)
        self._write_text(directory / "推导笔记.md", "", new=True)
        return self.detail(qid)

    def _update_question(self, data):
        old = self._question(data.get("id"))
        self._check_revision(old["path"], data.get("revision"))
        meta = {**self._metadata(old), "title": title_text(data.get("title", old["title"])), "updated_at": now()}
        self._write_record(old["path"], "question", meta, self._fields("question", data, old), old["_extra"])
        return self.detail(old["id"])

    def _save_body(self, data):
        q = self._question(data.get("question_id"))
        path = self._qdir(q) / "推导笔记.md"
        self._check_revision(path, data.get("revision"))
        self._write_text(path, clean_text(data.get("text", "")))
        return self.detail(q["id"])

    def _save_discussion(self, data):
        from .prompts import parse_capture
        q = self._question(data.get("question_id"))
        raw = clean_text(data.get("raw", data.get("capture", "")), "原始粘贴内容", False)
        parsed = parse_capture(raw)
        fields = self._fields("discussion", {**parsed, **{key: value for key, value in data.items() if value != ""}, "raw": raw})
        clean_text(fields["ai"], "AI 名称", False)
        records = self._all_records(q)
        did = self._next(records["discussions"], "D")
        parent_id, prompt_id = data.get("parent_id", ""), data.get("prompt_id", "")
        if parent_id and parent_id not in {r["id"] for r in records["discussions"]}:
            raise ValueError("前次讨论必须属于当前问题")
        if prompt_id and prompt_id not in {r["id"] for r in records["prompts"]}:
            raise ValueError("交接包必须属于当前问题")
        title = title_text(data.get("title") or parsed.get("title") or "本轮讨论")
        path = self._qdir(q) / "对话" / f"{did}_{datetime.now():%Y-%m-%d}_{slug(fields['ai'])}_{slug(title)}.md"
        capture_type = "摘要保存块" if re.search(r"【(?:主题|要点|遗留问题)】", raw) else "对话粘贴"
        meta = {"id": did, "question_id": q["id"], "title": title, "created_at": now(), "parent_id": parent_id, "prompt_id": prompt_id, "capture_type": capture_type}
        self._write_record(path, "discussion", meta, fields, new=True)
        return self._public(self._load(path, "discussion"))

    def _source_map(self, q, records=None):
        records = records or self._all_records(q)
        found = {v["id"]: v["path"] for values in records.values() for v in values}
        found.update({"body": self._rel(self._qdir(q) / "推导笔记.md"), "正文": self._rel(self._qdir(q) / "推导笔记.md")})
        return found

    def _save_plan(self, data):
        q = self._question(data.get("question_id"))
        records = self._all_records(q)
        method = data.get("method", "simulation")
        if method not in {"theory", "simulation", "measurement"}:
            raise ValueError("请选择理论核查、数值仿真或实测验证")
        fields = self._fields("plan", data)
        clean_text(fields["target"], "验证目标", False)
        clean_text(fields["criterion"], "本轮判据", False)
        pid = self._next(records["plans"], "P")
        title = title_text(data.get("title") or "本轮验证计划")
        source_ids = self._ids(data, "source_ids", self._source_map(q, records))
        meta = {"id": pid, "title": title, "question_id": q["id"], "created_at": now(), "method": method, "source_ids": source_ids}
        # Optional binding to ledger entries (v2 workflow): the plan names the
        # entries it tests and whether passing the criterion supports or refutes them.
        targets = data.get("target_entries", [])
        if not isinstance(targets, list) or any(not isinstance(t, dict) or not re.fullmatch(r"L\d{2,}", str(t.get("id", ""))) or not isinstance(t.get("sha", ""), str) for t in targets):
            raise ValueError("验证计划指向的台账条目格式不正确")
        direction = data.get("direction", "")
        if direction not in {"", "支持", "否定"}:
            raise ValueError("验证方向只能是支持或否定")
        if targets or direction:
            meta.update(target_entries=[{"id": t["id"], "sha": t.get("sha", "")} for t in targets], direction=direction or "支持")
        frozen = data.get("frozen_criteria", [])
        if not isinstance(frozen, list) or any(not isinstance(f, dict) or f.get("comparator") not in {"<", "<=", ">", ">=", "abs<", "abs<="}
                                               or not isinstance(f.get("threshold"), (int, float)) for f in frozen):
            raise ValueError("验证计划的阈值格式不正确")
        if frozen:
            meta["frozen_criteria"] = [{"comparator": f["comparator"], "threshold": float(f["threshold"])} for f in frozen]
        path = self._qdir(q) / "验证计划" / (pid + "_" + slug(title) + ".md")
        self._write_record(path, "plan", meta, fields, new=True)
        return self._public(self._load(path, "plan"))

    def _snapshot_codes(self, q, directory, paths):
        if not isinstance(paths, list) or any(not isinstance(p, str) for p in paths):
            raise ValueError("代码应为本题文件路径列表")
        if len(paths) != len(set(paths)):
            raise ValueError("代码路径不能重复")
        snapshots = []
        code_dir = self._qdir(q) / "代码"
        for relative in paths:
            source = self.read_file(relative)
            if not source.is_relative_to(code_dir) or source.name.endswith(".用途.md"):
                raise ValueError("运行只能引用当前问题的代码文件")
            data = source.read_bytes()
            digest = sha(data)
            target = directory / "代码快照" / (digest[:16] + "_" + source.name)
            if not target.exists():
                self._atomic(target, data, new=True)
            elif target.read_bytes() != data:
                raise ConflictError("代码快照名称冲突")
            snapshots.append({"source_path": self._rel(source), "path": self._rel(target), "sha256": digest})
        return snapshots

    def _create_run(self, data):
        q = self._question(data.get("question_id"))
        records = self._all_records(q)
        plan_id = data.get("plan_id", "")
        plan = self._find(records["plans"], plan_id, "本题验证计划")
        rid = self._next(records["runs"], "R")
        title = title_text(data.get("title") or "本轮验证")
        directory = self._qdir(q) / "结果" / (rid + "_" + slug(title))
        fields = self._fields("run", data)
        fields["expected"] = fields["expected"] or plan["expected"]
        fields["parameters"] = fields["parameters"] or plan["inputs"]
        code_paths = data.get("code_paths", [])
        snapshots = self._snapshot_codes(q, directory, code_paths)
        plan_copy = directory / "计划快照.md"
        self._atomic(plan_copy, self._safe(plan["path"]).read_bytes(), new=True)
        meta = {"id": rid, "title": title, "question_id": q["id"], "created_at": now(), "updated_at": now(), "plan_id": plan_id, "method": plan["method"], "outcome": "pending", "code_paths": code_paths, "code_snapshots": snapshots, "plan_snapshot": self._rel(plan_copy), "criterion": plan["criterion"]}
        self._write_record(directory / "运行记录.md", "run", meta, fields, new=True)
        return self._public(self._load(directory / "运行记录.md", "run"))

    def _update_run(self, data):
        q = self._question(data.get("question_id"))
        old = self._find(self._records(q, "结果", "run", "R"), data.get("id"), "运行")
        self._check_revision(old["path"], data.get("revision"))
        outcome = data.get("outcome", old["outcome"])
        if outcome not in {"pending", "pass", "fail", "partial", "error"}:
            raise ValueError("运行结果状态不正确")
        fields = self._fields("run", data, old)
        if outcome != "pending":
            clean_text(fields["actual"], "实际结果", False)
        meta = {**self._metadata(old), "updated_at": now(), "outcome": outcome}
        if "code_paths" in data:
            meta["code_paths"] = data["code_paths"]
            meta["code_snapshots"] = self._snapshot_codes(q, self._safe(old["path"]).parent, data["code_paths"])
        if outcome != "pending":
            directory = self._safe(old["path"]).parent
            result_files = []
            for path in sorted(directory.rglob("*")):
                if not path.is_file():
                    continue
                parts = path.relative_to(directory).parts
                if path.name in {"运行记录.md", "计划快照.md"} or parts[0] == "代码快照" or any(part.startswith(".") for part in parts):
                    continue
                path = self._safe(path)
                result_files.append({"name": path.name, "path": self._rel(path), "sha256": file_sha(path), "size": path.stat().st_size})
            meta["result_files"] = result_files
        self._write_record(old["path"], "run", meta, fields, old["_extra"])
        return self._public(self._load(old["path"], "run"))

    def _filename(self, value):
        value = clean_text(value, "文件名", False)
        if value in {".", ".."} or value != Path(value).name or re.search(r'[<>:"/\\|?*\x00-\x1f]', value) or value.startswith(".") or value.endswith((" ", ".")) or len(value) > 180:
            raise ValueError("文件名不能包含路径、隐藏前缀或特殊字符")
        if value.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
            raise ValueError("不能使用 Windows 保留文件名")
        return value

    def _decode_file(self, data, allow_text=False):
        if allow_text and "text" in data:
            return clean_text(data["text"], "代码").encode("utf-8")
        encoded = data.get("base64")
        if not isinstance(encoded, str) or len(encoded) > MAX_ATTACHMENT * 4 // 3 + 8:
            raise ValueError("文件为空或超过 32 MB；大体积原始数据请在项目卡记录原路径")
        try:
            result = base64.b64decode(encoded, validate=True)
        except (ValueError, base64.binascii.Error) as exc:
            raise ValueError("文件编码不正确") from exc
        if len(result) > MAX_ATTACHMENT:
            raise ValueError("文件超过 32 MB")
        return result

    def _unique_file(self, directory, filename):
        directory = self._safe(directory)
        path = self._safe(directory / filename)
        index = 2
        while path.exists():
            path = self._safe(directory / (Path(filename).stem + f"_v{index}" + Path(filename).suffix))
            index += 1
        return path

    def _add_code(self, data):
        q = self._question(data.get("question_id"))
        filename = self._filename(data.get("filename"))
        purpose = clean_text(data.get("purpose", ""), "代码用途", False)
        payload = self._decode_file(data, allow_text=True)
        path = self._unique_file(self._qdir(q) / "代码", filename)
        self._atomic(path, payload, new=True)
        descriptor = self._safe(str(path) + ".用途.md")
        self._write_record(descriptor, "code", {"id": path.name, "title": path.name + " · 用途", "question_id": q["id"], "created_at": now(), "filename": path.name, "sha256": sha(payload)}, {"purpose": purpose}, new=True)
        return {"name": path.name, "path": self._rel(path), "kind": "code", "purpose": purpose, "size": len(payload), "sha256": sha(payload)}

    def _attach_file(self, data):
        q = self._question(data.get("question_id"))
        filename = self._filename(data.get("filename"))
        if filename in {"运行记录.md", "计划快照.md"} or filename.endswith(".用途.md"):
            raise ValueError("该名称用于系统记录，请给附件换一个文件名")
        payload = self._decode_file(data)
        directory = self._qdir(q) / "资料"
        if data.get("run_id"):
            run = self._find(self._records(q, "结果", "run", "R"), data["run_id"], "运行")
            directory = self._safe(run["path"]).parent
        path = self._unique_file(directory, filename)
        self._atomic(path, payload, new=True)
        return {"name": path.name, "path": self._rel(path), "kind": "image" if path.suffix.lower() in IMAGE_EXT else "result" if data.get("run_id") else "attachment", "size": len(payload), "sha256": sha(payload)}

    def _save_judgment(self, data):
        q = self._question(data.get("question_id"))
        records = self._all_records(q)
        decision = data.get("decision")
        if decision not in {"supported", "refuted", "review", "inconclusive", "not_required"}:
            raise ValueError("请选择人工判断结果")
        fields = self._fields("judgment", data)
        clean_text(fields["text"], "判断与理由", False)
        source_map = self._source_map(q, records)
        allowed_evidence = {key: value for key, value in source_map.items() if key in {"body", "正文"} or re.fullmatch(r"[DPR]\d+", key)}
        evidence = self._ids(data, "evidence_ids", allowed_evidence)
        if decision in {"supported", "refuted"}:
            if not evidence:
                raise ValueError("支持或反驳判断必须引用本题证据")
            clean_text(fields["scope"], "适用条件与局限", False)
        jid = self._next(records["judgments"], "J")
        snapshots = []
        for eid in evidence:
            source = self._safe(source_map[eid])
            payload = source.read_bytes()
            if not payload.strip():
                raise ValueError("不能引用空白正文作为证据")
            target = self._qdir(q) / "结论" / "证据快照" / jid / (slug(eid) + ".md")
            self._atomic(target, payload, new=True)
            snapshots.append({"id": eid, "source_path": self._rel(source), "path": self._rel(target), "sha256": sha(payload)})
        path = self._qdir(q) / "结论" / (jid + ".md")
        observed_results = [{"path": item["path"], "sha256": file_sha(self._safe(item["path"]))} for item in self._artifact_summary(q, records)["results"]]
        meta = {"id": jid, "title": jid + " · " + STATUS_LABELS[decision], "question_id": q["id"], "created_at": now(), "decision": decision, "evidence_ids": evidence, "evidence_snapshots": snapshots, "question_signature": self._question_signature(q), "body_revision": self._revision(self._qdir(q) / "推导笔记.md"), "observed_result_files": observed_results}
        self._write_record(path, "judgment", meta, fields, new=True)
        return self._public(self._load(path, "judgment"))

    def _save_draft(self, data, append=False):
        content = self._content(data.get("content_id"))
        path = self._safe(content["path"]).parent / "草稿.md"
        self._check_revision(path, data.get("revision"))
        text = clean_text(data.get("text", ""), "草稿", not append)
        if append:
            q = self.detail(data.get("question_id"))
            if q["content_id"] != content["id"]:
                raise ValueError("草稿来源问题必须属于当前研究内容")
            j = q["latest_judgment"]
            if q["status"] not in {"supported", "refuted", "not_required"} or not j:
                raise ValueError("请先确认本题阶段结论，再追加进论文草稿；需复查或证据不足不算已确认结论")
            sources = "、".join(j["evidence_ids"]) or "人工说明"
            footer = f"\n\n> 来源：{q['id']} / {j['id']}；依据：{sources}。\n> 适用条件与局限：{j['scope'] or '见人工判断说明'}\n"
            text = self._read(path).rstrip() + f"\n\n## {q['title']}\n\n" + text + footer
        self._write_text(path, text)
        return self.content_detail(content["id"])

    def _generate_prompt(self, data):
        from .prompts import build_prompt
        state = self.state()
        template = data.get("template", "P0")
        if template not in {"P" + str(i) for i in range(11)}:
            raise ValueError("提示词编号应为 P0–P10")
        question = self.detail(data["question_id"]) if data.get("question_id") else None
        content = self.content_detail(question["content_id"]) if question else None
        framework = next((f for f in state["frameworks"] if f["status"] == "active"), None)
        if question:
            requested = self._ids(data, "source_ids", self._source_map(question))
            all_records = {v["id"]: v for key in ("discussions", "plans", "runs", "judgments", "prompts") for v in question[key]}
            sources = [all_records[eid] if eid in all_records else {"id": "body", "text": question["body"], "title": "当前推导正文"} for eid in requested] if requested else None
        else:
            sources = None
        if template == "P10":
            sources = {"frameworks": state["frameworks"], "contents": state["contents"], "questions": state["questions"]}
        text = build_prompt(template, state["project"], framework, content, question, focus=clean_text(data.get("focus", "")), sources=sources)
        if question:
            records = question["prompts"]
            directory = self._qdir(question) / "交接包"
        else:
            records = self._scan("04_交接包/T*.md", "prompt", r"T\d{2,}")
            directory = self._safe("04_交接包")
        tid = self._next(records, "T")
        meta = {"id": tid, "title": tid + " · " + template + " 交接包", "question_id": question["id"] if question else "", "created_at": now(), "template": template, "source_ids": data.get("source_ids", []), "project_revision": state["project"]["revision"], "framework_revision": framework["revision"] if framework else "", "question_revision": question["revision"] if question else ""}
        path = directory / (tid + "_" + template + ".md")
        self._write_record(path, "prompt", meta, {"text": text}, new=True)
        return {"id": tid, "text": text, "path": self._rel(path), "template": template}

    def _install_prompts(self):
        from .prompts import library_markdown
        path = self._safe("03_提示词/提示词.md")
        if not path.exists():
            self._write_text(path, library_markdown(), new=True)

    def _refresh_overviews(self):
        state = self.state()
        lines = ["# 研究总览", "", "此文件由研究记录重新生成；正文、讨论、运行与判断保存在各问题目录。", "", "## 论文框架", "", "| 框架 | 状态 | 引用的研究内容 |", "|---|---|---|"]
        esc = lambda s: str(s).replace("|", "\\|").replace("\n", " ")
        content_titles = {c["id"]: c["title"] for c in state["contents"]}
        frame_labels = {"active": "当前采用", "candidate": "候选", "parked": "暂存"}
        for framework in state["frameworks"]:
            lines.append(f"| {esc(framework['title'])} | {frame_labels.get(framework['status'], framework['status'])} | {esc('、'.join(content_titles.get(cid, '未找到的研究内容') for cid in framework['content_ids']))} |")
        framework_text = "\n".join(lines[3:]) + "\n"
        self._write_text(self._safe(FRAMEWORK_ROOT + "/框架总览.md"), "# 框架总览\n\n" + framework_text, history=False)
        for content in state["contents"]:
            lines += ["", f"## {content['title']}", "", "| 问题 | 状态 | 最新人工判断 |", "|---|---|---|"]
            for question in state["questions"]:
                if question["content_id"] == content["id"]:
                    latest = question["latest_judgment"]
                    lines.append(f"| {esc(question['title'])} | {question['status_label']} | {esc(latest['text'] if latest else '')} |")
        if not state["contents"]:
            lines += ["", "尚未新建研究内容。"]
        self._write_text(self._safe("总览.md"), "\n".join(lines) + "\n", history=False)
        self._refresh_record_indexes(state)

    def _generated(self, path, lines):
        """Refresh only the derived appendix, retaining all authored sections."""
        path = self._safe(path)
        old = self._read(path)
        main = re.sub(r"\n?<!-- generated:start -->.*?<!-- generated:end -->\n?", "", old, flags=re.S).rstrip()
        text = main + "\n\n<!-- generated:start -->\n" + "\n".join(lines) + "\n<!-- generated:end -->\n"
        self._write_text(path, text)

    def _refresh_record_indexes(self, state):
        esc = lambda value: str(value).replace("|", "\\|").replace("\n", " ").replace("[", "\\[").replace("]", "\\]")
        # A framework's readable file must carry its directory too. Keep this
        # generated appendix separate from all user-authored fields.
        content_map = {c["id"]: c for c in state["contents"]}
        outcome_names = {"pending": "尚未完成", "pass": "满足本轮判据", "fail": "未满足本轮判据", "partial": "部分满足", "error": "运行出错"}
        for framework in state["frameworks"]:
            path = self._safe(framework["path"])
            def file_link(label, target):
                relative = os.path.relpath(self._safe(target), path.parent).replace("\\", "/")
                return f"[{esc(label)}]({quote(relative, safe='/')})"
            lines = ["## 研究目录", ""]
            for index, cid in enumerate(framework["content_ids"], 1):
                content = content_map.get(cid)
                if content is None:
                    lines += [f"### {index}. {cid}（文件未找到）", ""]
                    continue
                lines += [f"### {index}. {file_link(content['title'], content['path'])}", ""]
                questions = [q for q in state["questions"] if q["content_id"] == cid]
                if not questions:
                    lines += ["尚未建立研究问题。", ""]
                for number, q in enumerate(questions, 1):
                    records = self._all_records(q)
                    artifacts = q["artifacts"]
                    lines += [f"#### {index}.{number} {file_link(q['title'], q['path'])}", "", f"- 状态：{q['status_label']}"]
                    lines.append("- " + file_link("论证正文", self._rel(self._qdir(q) / "推导笔记.md")))
                    for label, items in (("讨论", records["discussions"]), ("代码", artifacts["codes"])):
                        links = [file_link(v.get("name") or (v["id"] + " " + v["title"]), v["path"]) for v in items]
                        lines.append(f"- {label}：" + ("；".join(links) or "尚无文件"))
                    lines.append("- 结果：")
                    if not records["runs"] and not artifacts["results"]:
                        lines.append("  - 尚无结果文件或验证记录。")
                    for run in records["runs"]:
                        lines.append("  - " + file_link(run["id"] + " " + run["title"], run["path"]) + " · " + outcome_names.get(run["outcome"], run["outcome"]))
                        outputs = [v for v in artifacts["results"] if v["run_id"] == run["id"]]
                        for item in outputs:
                            lines.append("    - " + file_link(item["name"], item["path"]))
                        if not outputs:
                            lines.append("    - 尚无实际输出文件。")
                    pending = [v for v in artifacts["results"] if not v["run_id"]]
                    if pending:
                        lines.append("  - 待关联运行")
                        lines += ["    - " + file_link(v["name"], v["path"]) for v in pending]
                    links = [file_link(j["id"] + " " + STATUS_LABELS[j["decision"]], j["path"]) for j in records["judgments"]]
                    lines += ["- 结论：" + ("；".join(links) or "尚未作出判断"), ""]
            if not framework["content_ids"]:
                lines.append("尚未加入研究内容。")
            self._generated(path, lines)
        for content in state["contents"]:
            path = self._safe(content["path"])
            lines = ["## 资料索引（自动更新）", "", "### 出现在哪些论文框架", ""]
            placements = [f for f in state["frameworks"] if content["id"] in f["content_ids"]]
            for f in placements:
                relative = os.path.relpath(self._safe(f["path"]), path.parent).replace("\\", "/")
                lines.append(f"- [{esc(f['title'])}]({quote(relative, safe='/')}) · 第 {f['content_ids'].index(content['id']) + 1} 项 · { {'active': '当前采用', 'candidate': '候选', 'parked': '暂存'}.get(f['status'], f['status'])}")
            if not placements:
                lines.append("尚未被论文框架引用。")
            lines += ["", "### 包含的问题", "", "| 问题 | 状态 | 最新人工判断 |", "|---|---|---|"]
            for q in state["questions"]:
                if q["content_id"] != content["id"]:
                    continue
                relative = os.path.relpath(self._safe(q["path"]), path.parent).replace("\\", "/")
                j = q["latest_judgment"]
                lines.append(f"| [{esc(q['title'])}]({quote(relative, safe='/')}) | {q['status_label']} | {esc(j['text'] if j else '')} |")
            self._generated(path, lines)
        for q in state["questions"]:
            records = self._all_records(q)
            path = self._safe(q["path"])
            link = lambda record: f"[{record['id']}]({quote(os.path.relpath(self._safe(record['path']), path.parent).replace(chr(92), '/'), safe='/')})"
            lines = ["## 资料索引（自动更新）", "", f"- 当前状态：{q['status_label']}", f"- 当前进展：{q['status_reason']}", "", "### 对话记录", "", "| 编号 | AI | 主题 | 遗留问题 |", "|---|---|---|---|"]
            for d in records["discussions"]:
                lines.append(f"| {link(d)} | {esc(d['ai'])} | {esc(d['title'])} | {esc(d['unresolved'])} |")
            lines += ["", "### 验证计划与运行", "", "| 编号 | 标题 | 类型或运行结果 |", "|---|---|---|"]
            for p in records["plans"]:
                lines.append(f"| {link(p)} | {esc(p['title'])} | {p['method']} |")
            for r in records["runs"]:
                lines.append(f"| {link(r)} | {esc(r['title'])} | {r['outcome']}；对应 {r['plan_id']} |")
            lines += ["", "### 代码用途", "", "| 代码 | 用途 |", "|---|---|"]
            for item in self._file_list(q):
                if item["kind"] == "code":
                    relative = os.path.relpath(self._safe(item["path"]), path.parent).replace("\\", "/")
                    lines.append(f"| [{esc(item['name'])}]({quote(relative, safe='/')}) | {esc(item.get('purpose', ''))} |")
            lines += ["", "### 人工判断与阶段结论", "", "| 编号 | 判断 | 依据 | 适用条件与局限 |", "|---|---|---|---|"]
            for j in records["judgments"]:
                lines.append(f"| {link(j)} | {STATUS_LABELS[j['decision']]}：{esc(j['text'])} | {', '.join(j['evidence_ids'])} | {esc(j['scope'])} |")
            self._generated(path, lines)

    def perform(self, action, data):
        if not isinstance(data, dict):
            raise ValueError("请求内容必须是对象")
        actions = {
            "save_project": self._save_project, "save_framework": self._save_framework,
            "save_content": self._save_content, "create_question": self._create_question,
            "update_question": self._update_question, "save_body": self._save_body,
            "save_discussion": self._save_discussion, "save_plan": self._save_plan,
            "create_run": self._create_run, "update_run": self._update_run,
            "add_code": self._add_code, "attach_file": self._attach_file,
            "save_judgment": self._save_judgment, "save_draft": self._save_draft,
            "append_draft": lambda value: self._save_draft(value, append=True),
            "generate_prompt": self._generate_prompt,
        }
        with self.lock:
            if action in {"preview_semantic", "import_semantic"}:
                from . import semantic
                return getattr(semantic, action)(self, data)
            if action in {"preview_capture", "import_capture"}:
                from . import capture
                return getattr(capture, action)(self, data)
            if action in {"preview_outline", "import_outline", "import_materials"}:
                from . import imports
                return getattr(imports, action)(self, data)
            if action == "sync":
                self._refresh_overviews()
                return self.state()
            if action not in actions:
                raise ValueError("不支持的操作")
            result = actions[action](data)
            self._refresh_overviews()
            if isinstance(result, dict) and result.get("kind") == "question":
                result = self.detail(result["id"])
            elif isinstance(result, dict) and result.get("kind") == "content":
                result = self.content_detail(result["id"])
            elif isinstance(result, dict) and result.get("kind") == "framework":
                result = self._public(self._find(self._frameworks(), result["id"], "框架"))
            return result
