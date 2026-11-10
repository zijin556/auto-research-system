"""Local batch imports with previews, idempotent receipts and file rollback."""
from __future__ import annotations

import json
import re
from pathlib import Path

from .store import ConflictError, MAX_ATTACHMENT, clean_text, file_sha, now, sha, title_text


def _request(store, operation, data):
    rid = data.get("request_id")
    if not isinstance(rid, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{7,79}", rid):
        raise ValueError("缺少有效的批次编号，请重新打开导入窗口。")
    digest = sha(json.dumps({"operation": operation, "payload": data}, sort_keys=True, ensure_ascii=False).encode())
    receipt = store._safe(Path(".runtime/imports") / (rid + ".json"))
    if receipt.exists():
        old = json.loads(receipt.read_text(encoding="utf-8"))
        if old["digest"] != digest:
            raise ConflictError("此批次编号已用于不同内容，请重新预览后再导入。")
        return receipt, digest, old["result"]
    return receipt, digest, None


class FileTransaction:
    """Roll back only paths written by this operation; never delete a tree."""
    def __init__(self, store):
        self.store, self.before, self.parents = store, {}, set()

    def __enter__(self):
        self.original = self.store._atomic
        def tracked(path, payload, **kwargs):
            path = self.store._safe(path)
            if path not in self.before:
                self.before[path] = path.read_bytes() if path.exists() else None
                parent = path.parent
                while parent != self.store.root and not parent.exists():
                    self.parents.add(parent)
                    parent = parent.parent
            return self.original(path, payload, **kwargs)
        self.store._atomic = tracked
        return self

    def __exit__(self, kind, error, traceback):
        self.store._atomic = self.original
        if kind is None:
            return False
        failures = []
        for path, prior in reversed(list(self.before.items())):
            try:
                self.store._safe(path)
                if prior is None:
                    if path.exists():
                        path.unlink()
                elif not path.exists() or path.read_bytes() != prior:
                    # Bypass an injected failing writer; retain normal history.
                    type(self.store)._atomic(self.store, path, prior, history=True)
            except OSError as exc:
                failures.append(str(exc))
        for directory in sorted(self.parents, key=lambda p: len(p.parts), reverse=True):
            try:
                if directory.exists():
                    directory.rmdir()
            except OSError:
                pass  # non-empty directories are deliberately retained
        if failures:
            raise RuntimeError("导入未完成且部分文件恢复失败，请检查资料目录；不要重复导入：" + "; ".join(failures)) from error
        return False


def parse_outline(text, target=None):
    raw = clean_text(text, "目录原文", False)
    lines = raw.strip().splitlines()
    if lines and re.fullmatch(r"```(?:markdown|md)?\s*", lines[0].strip(), re.I) and lines[-1].strip() == "```":
        lines = lines[1:-1]
    roots, preamble, warnings = [], [], []
    frame = content = question = None
    fenced = False
    for line_no, line in enumerate(lines, 1):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            heading = None
        else:
            heading = None if fenced else re.match(r"^(#{1,3})\s+(.+?)\s*#*\s*$", line)
        if heading:
            level, title = len(heading[1]), title_text(heading[2])
            if level == 1:
                if target and roots:
                    raise ValueError("加入已有框架时只能粘贴一个框架；请从 ## 研究内容 开始。")
                frame = {"title": title, "mainline": "", "contents": []}
                roots.append(frame)
                content = question = None
            elif level == 2:
                if frame is None:
                    if target is None:
                        raise ValueError(f"第 {line_no} 行缺少上层 # 论文框架。")
                    frame = {"title": target["title"], "mainline": "", "contents": []}
                    roots.append(frame)
                content = {"title": title, "objective": "", "questions": []}
                frame["contents"].append(content)
                question = None
            else:
                if content is None:
                    raise ValueError(f"第 {line_no} 行的问题缺少上层 ## 研究内容。")
                question = {"title": title, "question": ""}
                content["questions"].append(question)
        else:
            if question is not None:
                question["question"] += line + "\n"
            elif content is not None:
                content["objective"] += line + "\n"
            elif frame is not None:
                frame["mainline"] += line + "\n"
            else:
                preamble.append(line)
    if not roots:
        raise ValueError("没有识别到目录，请使用 # 框架、## 研究内容、### 问题。")
    if "\n".join(preamble).strip():
        roots[0]["mainline"] = "\n".join(preamble).strip() + "\n\n" + roots[0]["mainline"]
        warnings.append("目录前的文字保留为首个框架的说明，完整原文也会留档。")
    if target:
        if roots[0]["title"] != target["title"]:
            warnings.append("将追加到所选框架，不修改其名称；粘贴原文另存。")
        roots[0]["title"] = target["title"]
        if roots[0]["mainline"].strip():
            warnings.append("所选框架的原主线保持不变；粘贴的框架说明保存在导入原文中。")
    counts = {"frameworks": 0 if target else len(roots), "contents": 0, "questions": 0}
    for f in roots:
        f["mainline"] = f["mainline"].strip()
        counts["contents"] += len(f["contents"])
        for c in f["contents"]:
            c["objective"] = c["objective"].strip()
            counts["questions"] += len(c["questions"])
            for q in c["questions"]:
                q["question"] = q["question"].strip() or q["title"]
    if len(roots) > 20 or counts["contents"] > 100 or counts["questions"] > 300:
        raise ValueError("单次最多 20 个框架、100 项研究内容、300 个问题，请分批导入。")
    if target and counts["contents"] == 0:
        raise ValueError("请至少粘贴一项 ## 研究内容。")
    return raw, roots, counts, warnings


def preview_outline(store, data):
    target = store._find(store._frameworks(), data["framework_id"], "框架") if data.get("framework_id") else None
    raw, outline, counts, warnings = parse_outline(data.get("text", ""), target)
    # Use the record serializer for full validation before any file is created.
    for f in outline:
        store._record_text("framework", {"title": f["title"]}, {"mainline": f["mainline"]})
        for c in f["contents"]:
            store._record_text("content", {"title": c["title"]}, {"objective": c["objective"]})
            for q in c["questions"]:
                store._record_text("question", {"title": q["title"]}, {"question": q["question"]})
    revision = target["revision"] if target else ""
    token = sha(json.dumps({"text": raw, "target": target["id"] if target else "", "revision": revision}, ensure_ascii=False, sort_keys=True).encode())
    return {"outline": outline, "counts": counts, "warnings": warnings, "token": token, "framework_revision": revision}


def import_outline(store, data):
    receipt, digest, prior = _request(store, "outline", data)
    if prior is not None:
        return prior
    preview = preview_outline(store, data)
    if preview["token"] != data.get("token"):
        raise ConflictError("目录内容或目标框架已改变，请重新预览后保存。")
    frameworks, contents, questions = [], [], []
    with FileTransaction(store):
        for item in preview["outline"]:
            target = store._find(store._frameworks(), data["framework_id"], "框架") if data.get("framework_id") else None
            cids = []
            for c in item["contents"]:
                saved = store._save_content({"title": c["title"], "objective": c["objective"]})
                cids.append(saved["id"])
                contents.append(saved["id"])
                for q in c["questions"]:
                    question = store._create_question({"content_id": saved["id"], **q})
                    questions.append(question["id"])
            if target:
                frame = store._save_framework({"id": target["id"], "revision": target["revision"], "content_ids": target["content_ids"] + cids})
            else:
                frame = store._save_framework({"title": item["title"], "mainline": item["mainline"], "content_ids": cids, "status": "candidate"})
            frameworks.append(frame["id"])
        source = store._safe(Path("04_交接包/目录导入") / ("I_" + data["request_id"] + ".md"))
        store._write_text(source, data["text"], new=True)
        result = {"framework_id": frameworks[0], "framework_ids": frameworks, "content_ids": contents, "question_ids": questions, "source_path": store._rel(source), "counts": preview["counts"]}
        store._refresh_overviews()
        store._write_text(receipt, json.dumps({"digest": digest, "result": result, "created_at": now()}, ensure_ascii=False), new=True)
    return result


def import_materials(store, data):
    receipt, digest, prior = _request(store, "materials", data)
    if prior is not None:
        return prior
    q = store._question(data.get("question_id"))
    rid = data.get("run_id") or ""
    run = store._find(store._records(q, "结果", "run", "R"), rid, "本题运行") if rid else None
    purpose = clean_text(data.get("purpose") or "用于核查：" + q["title"], "本批代码用途", False)
    store._record_text("code", {"title": "用途"}, {"purpose": purpose})
    files = data.get("files")
    if not isinstance(files, list) or not files or len(files) > 100:
        raise ValueError("请选择 1 至 100 个文件。")
    prepared, total, reserved_codes = [], 0, set()
    for f in files:
        if not isinstance(f, dict):
            raise ValueError("文件信息不正确")
        name = store._filename(f.get("filename"))
        if name in {"运行记录.md", "计划快照.md", "问题.md", "研究内容.md"} or name.endswith(".用途.md"):
            raise ValueError("文件名用于系统记录，请给附件换名：" + name)
        kind = f.get("kind")
        if kind not in {"code", "result", "attachment"}:
            raise ValueError("文件类型应为代码、结果或资料。")
        if kind == "code":
            # Reserve duplicate names before any write, including derived titles.
            directory = store._qdir(q) / "代码"
            candidate, index = name, 2
            while (directory / candidate).exists() or candidate.casefold() in reserved_codes:
                candidate = Path(name).stem + f"_v{index}" + Path(name).suffix
                index += 1
            reserved_codes.add(candidate.casefold())
            name = candidate
            store._record_text("code", {"title": name + " · 用途"}, {"purpose": purpose})
        payload = store._decode_file(f)
        total += len(payload)
        if total > MAX_ATTACHMENT:
            raise ValueError("单批文件总大小不能超过 32 MB，请分批导入。")
        prepared.append((name, kind, payload, f["base64"]))
    imported = []
    with FileTransaction(store):
        for name, kind, payload, encoded in prepared:
            if kind == "code":
                item = store._add_code({"question_id": q["id"], "filename": name, "purpose": purpose, "base64": encoded})
            else:
                directory = store._qdir(q) / "资料"
                if kind == "result":
                    directory = store._safe(run["path"]).parent if run else store._qdir(q) / "结果/待关联"
                path = store._unique_file(directory, name)
                store._atomic(path, payload, new=True)
                item = {"name": path.name, "path": store._rel(path), "size": len(payload), "kind": kind, "sha256": sha(payload)}
                if kind == "result":
                    item["run_id"] = rid
            imported.append(item)
        result = {"files": imported, "question_id": q["id"], "run_id": rid, "unassigned_results": sum(f["kind"] == "result" and not rid for f in imported)}
        store._refresh_overviews()
        store._write_text(receipt, json.dumps({"digest": digest, "result": result, "created_at": now()}, ensure_ascii=False), new=True)
    return result
