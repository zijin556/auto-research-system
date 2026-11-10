"""Preview and archive pasted discussions locally, without a model call.

The parser recognises explicit routing labels, exact existing titles and fenced
files. It never treats pasted conclusions or output as independently verified.
"""
from __future__ import annotations

import base64
import json
import re
from pathlib import Path

from .imports import FileTransaction, _request
from .store import ConflictError, MAX_ATTACHMENT, clean_text, now, sha, title_text


CODE_LANGUAGES = {"python": ".py", "py": ".py", "matlab": ".m", "octave": ".m", "javascript": ".js", "js": ".js", "typescript": ".ts", "ts": ".ts", "r": ".r", "julia": ".jl", "cpp": ".cpp", "c": ".c", "bash": ".sh", "sh": ".sh", "powershell": ".ps1"}
RESULT_LANGUAGES = {"csv": ".csv", "tsv": ".tsv", "json": ".json", "text": ".txt", "txt": ".txt", "log": ".log", "output": ".txt", "stdout": ".log"}
ROUTE_LABELS = {"framework": r"(?:论文框架|研究框架|框架)", "content": r"(?:研究内容|研究主题)", "question": r"(?:研究问题|具体问题|问题)"}
RESERVED = {"运行记录.md", "计划快照.md", "问题.md", "研究内容.md"}


def _plain_title(value):
    return value.strip().strip("*` ").strip("“”\"'《》 ")


def _labels(lines, pattern):
    found = []
    for line in lines:
        match = re.match(r"^\s*(?:#{1,6}\s*)?(?:\*\*)?" + pattern + r"(?:\*\*)?\s*[：:]\s*(.+?)\s*$", line)
        if match:
            value = _plain_title(match[1])
            if value and value not in found:
                found.append(title_text(value))
    return found


def _section(lines, labels):
    """Return literal source text, never a generated interpretation."""
    parts, collecting = [], False
    for line in lines:
        match = re.match(r"^\s*(?:#{1,6}\s*)?(?:\*\*)?(?:" + labels + r")(?:\*\*)?\s*(?:[：:]\s*(.*)|$)", line)
        if match:
            if parts:
                break
            collecting = True
            if match[1]:
                parts.append(match[1])
            continue
        if collecting:
            if re.match(r"^\s*(?:#{1,6}\s+|(?:用户|User|AI|GPT|Gemini|Claude)\s*[：:]|[\u4e00-\u9fff]{2,12}[：:])", line):
                break
            parts.append(line)
    return "\n".join(parts).strip()


def _outside_fences(lines):
    result, active = [], None
    for line in lines:
        marker = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
        if marker:
            if active is None:
                active = marker[1]
            elif marker[1][0] == active[0] and len(marker[1]) >= len(active):
                active = None
            continue
        if active is None:
            result.append(line)
    return result


def _file_kind(filename):
    ext = Path(filename).suffix.lower()
    if ext in set(CODE_LANGUAGES.values()) | {".ipynb", ".h", ".hpp", ".f", ".f90", ".java", ".bat", ".cmd"}:
        return "code"
    if ext in set(RESULT_LANGUAGES.values()) | {".png", ".jpg", ".jpeg", ".pdf", ".svg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".npy", ".npz", ".mat", ".h5", ".hdf5", ".xlsx", ".xls", ".md"}:
        return "result"
    return "attachment"


def _extract(store, raw, attachments):
    lines = raw.splitlines(keepends=True)
    prose, prepared, warnings = [], [], []
    i = 0
    while i < len(lines):
        opener = re.match(r"^\s{0,3}(`{3,}|~{3,})([^\r\n]*)", lines[i])
        if not opener:
            prose.append(lines[i].rstrip("\r\n"))
            i += 1
            continue
        start, marker, info = i, opener[1], opener[2].strip()
        i += 1
        code = []
        while i < len(lines) and not re.fullmatch(r"\s{0,3}" + re.escape(marker[0]) + "{" + str(len(marker)) + r",}\s*", lines[i].rstrip("\r\n")):
            code.append(lines[i])
            i += 1
        if i == len(lines):
            warnings.append("有一个代码围栏没有结束，已保留在原文中，不单独生成文件。")
            prose.extend(line.rstrip("\r\n") for line in lines[start:])
            break
        i += 1
        bits = info.split(maxsplit=1)
        language = bits[0].lower() if bits else ""
        extension = CODE_LANGUAGES.get(language) or RESULT_LANGUAGES.get(language)
        candidate = bits[1].strip() if len(bits) == 2 else ""
        candidate = re.sub(r"^(?:filename|file|文件名)\s*=\s*", "", candidate).strip('"\'`')
        if not candidate and "." in language:
            candidate, language = bits[0], ""
        if not candidate and prose:
            previous = _plain_title(prose[-1]).lstrip("# ")
            previous = re.sub(r"^(?:验证代码|代码|程序|文件|文件名|结果文件|输出文件|结果)\s*[：:]\s*", "", previous)
            if re.fullmatch(r"[^\s]+\.[A-Za-z0-9]{1,10}", previous):
                candidate = previous
        if not extension and not candidate:
            prose.extend(line.rstrip("\r\n") for line in lines[start:i])
            continue  # Unknown fences remain in the original discussion.
        if not candidate:
            prefix = "讨论代码" if language in CODE_LANGUAGES else "讨论输出"
            candidate = prefix + f"_{len(prepared) + 1}" + extension
        name = store._filename(candidate)
        kind = "code" if language in CODE_LANGUAGES else "result" if language in RESULT_LANGUAGES else _file_kind(name)
        prepared.append({"filename": name, "kind": kind, "payload": "".join(code).encode("utf-8"), "source": "讨论中的代码块" if kind == "code" else "讨论中的结果块"})
        prose.append("\n" + ("代码文件" if kind == "code" else "结果文件") + "：`" + name + "`（已单独归档）\n")
    if attachments is None:
        attachments = []
    if not isinstance(attachments, list) or len(attachments) > 100:
        raise ValueError("单次最多导入 100 个附件。")
    for item in attachments:
        if not isinstance(item, dict):
            raise ValueError("附件信息不正确。")
        name = store._filename(item.get("filename"))
        kind = item.get("kind") or _file_kind(name)
        if kind not in {"code", "result", "attachment"}:
            raise ValueError("附件类型应为代码、结果或资料。")
        prepared.append({"filename": name, "kind": kind, "payload": store._decode_file(item), "source": "一同选入的附件"})
    if len(prepared) > 100 or sum(len(f["payload"]) for f in prepared) > MAX_ATTACHMENT:
        raise ValueError("单次最多 100 个文件、合计 32 MB，请分批保存。")
    for item in prepared:
        name = item["filename"]
        if name in RESERVED or name.endswith(".用途.md"):
            raise ValueError("文件名用于系统记录，请给附件换名：" + name)
        if item["kind"] == "code":
            store._record_text("code", {"title": name + " · 用途"}, {"purpose": "用于当前问题的讨论核查"})
    return prose, prepared, warnings


def _pick(level, records, specified_id, specified_title, labels, raw, fallback, choices, warnings):
    if specified_id:
        matches = [r for r in records if r["id"] == specified_id]
        if not matches:
            raise ValueError("所选归属不存在或不属于上层，请重新选择。")
        selected = matches[0]
        if labels and selected["title"] not in labels and specified_title != selected["title"]:
            choices.append({"level": level, "reason": "粘贴内容的名称与当前页面不同，请确认保存位置。", "options": [{"id": selected["id"], "title": selected["title"]}] + [{"id": "", "title": label} for label in labels]})
        elif labels and selected["title"] not in labels:
            warnings.append("文字中的名称与所选位置不同，将保存在你选定的“" + selected["title"] + "”下；原文保留。")
        return {"id": selected["id"], "title": selected["title"], "mode": "existing", "reason": "你选定的位置"}
    labels = [title_text(specified_title)] if specified_title else labels
    if len(labels) > 1:
        choices.append({"level": level, "reason": "这段讨论出现多个名称，请选择本轮主要归属；完整讨论会保留。", "options": [{"id": "", "title": label} for label in labels]})
        return {"id": "", "title": "待选择", "mode": "pending", "reason": "包含多个归属"}
    title = labels[0] if labels else ""
    matches = [r for r in records if r["title"].strip() == title] if title else [r for r in records if len(r["title"].strip()) >= 4 and r["title"].strip() in raw]
    if len(matches) > 1:
        choices.append({"level": level, "reason": "找到多个可能的位置，请选择一次。", "options": [{"id": r["id"], "title": r["title"]} for r in matches]})
        return {"id": "", "title": title or "待选择", "mode": "pending", "reason": "有多个现有匹配"}
    if matches:
        return {"id": matches[0]["id"], "title": matches[0]["title"], "mode": "existing", "reason": "与已有名称准确匹配"}
    return {"id": "", "title": title_text(title or fallback), "mode": "new", "reason": "来自讨论中的名称" if title else "根据当前内容建议新建，可返回原文补充名称"}


def _prepare(store, data):
    raw = data.get("text", "")
    normalized = clean_text(raw, "讨论原文", False)
    prose, files, warnings = _extract(store, raw, data.get("files"))
    routing_prose = _outside_fences(prose)
    labels = {key: _labels(routing_prose, pattern) for key, pattern in ROUTE_LABELS.items()}
    fs, cs, qs = store._frameworks(), store._contents(), store._questions()
    choices = []
    q_context = store._question(data["question_id"]) if data.get("question_id") else None
    content_id = data.get("content_id") or (q_context["content_id"] if q_context else "")
    if q_context and content_id != q_context["content_id"]:
        raise ValueError("所选问题不属于这项研究内容。")
    # An exact child name is stronger evidence than the currently active frame.
    # Resolve its real ancestors first so a follow-up cannot create duplicates
    # merely because another framework has become active.
    content_names = [data["content_title"]] if data.get("content_title") else labels["content"]
    question_names = [data["question_title"]] if data.get("question_title") else labels["question"]
    if not content_id and len(content_names) == 1:
        exact_contents = [c for c in cs if c["title"] == content_names[0]]
        if len(exact_contents) == 1:
            content_id = exact_contents[0]["id"]
        elif len(exact_contents) > 1 and not data.get("framework_id") and not labels["framework"] and not data.get("framework_title"):
            choices.append({"level": "content", "reason": "已有多项同名研究内容，请确认归属。", "options": [{"id": c["id"], "title": c["title"]} for c in exact_contents]})
    if not content_id and len(question_names) == 1:
        exact_questions = [q for q in qs if q["title"] == question_names[0]]
        if len(exact_questions) == 1:
            parent = next(c for c in cs if c["id"] == exact_questions[0]["content_id"])
            if not content_names or content_names == [parent["title"]]:
                content_id = parent["id"]
    frame_id = data.get("framework_id") or ""
    if content_id and not frame_id and not labels["framework"] and not data.get("framework_title"):
        parents = [f for f in fs if content_id in f.get("content_ids", [])]
        if len(parents) == 1:
            frame_id = parents[0]["id"]
        elif len(parents) > 1:
            choices.append({"level": "framework", "reason": "这项研究内容被多个框架引用，请选择本轮浏览路径。", "options": [{"id": f["id"], "title": f["title"]} for f in parents]})
    if not frame_id and not labels["framework"] and not data.get("framework_title") and not any(c["level"] == "framework" for c in choices):
        active = [f for f in fs if f.get("status") == "active"]
        if len(active) == 1:
            frame_id = active[0]["id"]
        elif len(fs) == 1:
            frame_id = fs[0]["id"]
        elif len(fs) > 1:
            choices.append({"level": "framework", "reason": "原文没有明确论文框架，请选择本轮归属。", "options": [{"id": f["id"], "title": f["title"]} for f in fs]})
    frame = _pick("framework", fs, frame_id, data.get("framework_title"), labels["framework"], normalized, "待完善的论文框架", choices, warnings)
    frame_record = next((f for f in fs if f["id"] == frame["id"]), None)
    scoped_contents = [c for c in cs if c["id"] in frame_record.get("content_ids", [])] if frame_record else cs
    if content_id:
        scoped_contents = cs  # explicit choice can add an existing content reference
    if not content_id and not labels["content"] and not data.get("content_title") and len(scoped_contents) == 1 and frame_record:
        content_id = scoped_contents[0]["id"]
    content = _pick("content", scoped_contents, content_id, data.get("content_title"), labels["content"], normalized, "待归类的研究内容", choices, warnings)
    if content["mode"] == "new" and not labels["content"] and not data.get("content_title") and len(scoped_contents) > 1:
        choices.append({"level": "content", "reason": "原文没有明确研究内容，请选择归属，或输入新的研究内容名称。", "options": [{"id": c["id"], "title": c["title"]} for c in scoped_contents]})
    scoped_questions = [q for q in qs if q["content_id"] == content["id"]] if content["id"] else []
    proposed = next((re.sub(r"^\s*(?:用户|User|我)\s*[：:]\s*", "", line).strip() for line in prose if re.match(r"^\s*(?:用户|User|我)\s*[：:]", line, re.I)), "")
    if not proposed:
        proposed = next((line.strip().lstrip("# ") for line in prose if "？" in line or "?" in line), "本轮讨论的问题")
    proposed = re.split(r"(?<=[？?])", proposed)[0][:140] or "本轮讨论的问题"
    question = _pick("question", scoped_questions, data.get("question_id"), data.get("question_title"), labels["question"], normalized, proposed, choices, warnings)
    if frame["mode"] == "new" and not labels["framework"] and not data.get("framework_title"):
        warnings.append("讨论未明确框架名称，先建议“待完善的论文框架”；可返回原文补充名称，或选择已有位置。")
    if content["mode"] == "new" and not labels["content"] and not data.get("content_title"):
        warnings.append("讨论未明确研究内容，先建议“待归类的研究内容”；可返回原文补充名称，或选择已有位置。")
    if any(f["kind"] == "result" for f in files):
        warnings.append("结果文件与本问题一起保存，暂未绑定运行记录；粘贴的输出不等于系统已运行验证。")
    candidate = _section(routing_prose, r"(?:阶段结论|候选结论|当前结论|结论)")
    scope = _section(routing_prose, r"(?:适用范围|适用条件|局限)")
    criterion = _section(routing_prose, r"(?:验证判据|判据|验收标准)")
    if not criterion:
        criterion = next((line.strip() for line in routing_prose if re.search(r"(?:事先约定|验证判据|验收标准)", line)), "")
    ai_match = re.search(r"^\s*((?:GPT|ChatGPT|Gemini|Claude|Codex|AI)(?:[（(][^）)\n]{1,30}[）)])?)\s*[：:]", normalized, re.M | re.I)
    ai = ai_match[1] if ai_match else "未注明 AI"
    body_text = "\n".join(line for line in prose if not any(re.match(r"^\s*(?:#{1,6}\s*)?(?:\*\*)?" + pat + r"(?:\*\*)?\s*[：:]", line) for pat in ROUTE_LABELS.values())).strip()
    body = "# " + question["title"] + "\n\n> 以下为本轮讨论原文整理，尚未确认研究结论。\n\n" + body_text
    plan = {"framework": frame, "content": content, "question": question, "body": body, "body_mode": "create" if question["mode"] == "new" else "keep", "ai": ai, "discussion_title": question["title"][:165] + " · 讨论归档", "criterion": criterion, "candidate_conclusion": candidate, "candidate_scope": scope}
    # Validate every persisted text field before starting the transaction.
    store._record_text("discussion", {"title": plan["discussion_title"]}, {"ai": ai, "raw": normalized, "summary": candidate})
    store._record_text("question", {"title": question["title"]}, {"question": question["title"], "criterion": criterion})
    counts = {"frameworks": int(frame["mode"] == "new"), "contents": int(content["mode"] == "new"), "questions": int(question["mode"] == "new"), "discussions": 1, "codes": sum(f["kind"] == "code" for f in files), "results": sum(f["kind"] == "result" for f in files), "attachments": sum(f["kind"] == "attachment" for f in files)}
    revision_state = [[r["id"], r["revision"]] for r in fs + cs + qs]
    token = sha(json.dumps({"input": {k: v for k, v in data.items() if k not in {"token", "request_id"}}, "revisions": revision_state, "plan": plan}, ensure_ascii=False, sort_keys=True).encode())
    preview = {"plan": plan, "files": [{"filename": f["filename"], "kind": f["kind"], "size": len(f["payload"]), "source": f["source"]} for f in files], "counts": counts, "warnings": warnings, "needs_choice": choices, "can_import": not choices, "token": token, "parser": "local-rules"}
    return preview, files, raw


def preview_capture(store, data):
    return _prepare(store, data)[0]


def import_capture(store, data):
    receipt, digest, prior = _request(store, "capture", data)
    if prior is not None:
        return prior
    preview, files, raw = _prepare(store, data)
    if data.get("token") != preview["token"]:
        raise ConflictError("讨论、附件或归属资料已改变，请重新预览后保存。")
    if not preview["can_import"]:
        raise ValueError("请先确认预览中尚不明确的归属。")
    plan = preview["plan"]
    with FileTransaction(store):
        content = store._content(plan["content"]["id"]) if plan["content"]["id"] else store._save_content({"title": plan["content"]["title"]})
        if plan["framework"]["id"]:
            framework = store._find(store._frameworks(), plan["framework"]["id"], "框架")
            if content["id"] not in framework["content_ids"]:
                framework = store._save_framework({"id": framework["id"], "revision": framework["revision"], "content_ids": framework["content_ids"] + [content["id"]]})
        else:
            framework = store._save_framework({"title": plan["framework"]["title"], "status": "candidate", "content_ids": [content["id"]]})
        question = store._question(plan["question"]["id"]) if plan["question"]["id"] else store._create_question({"content_id": content["id"], "title": plan["question"]["title"], "question": plan["question"]["title"], "criterion": plan["criterion"]})
        source = store._qdir(question) / "资料" / ("讨论原文_" + data["request_id"] + ".md")
        store._atomic(source, raw.encode("utf-8"), new=True)  # Preserve CRLF and all bytes of the pasted text.
        candidate_summary = ("待确认的原文结论：\n\n" + plan["candidate_conclusion"]) if plan["candidate_conclusion"] else ""
        if plan["candidate_scope"]:
            candidate_summary += "\n\n原文适用范围：\n\n" + plan["candidate_scope"]
        discussion = store._save_discussion({"question_id": question["id"], "ai": plan["ai"], "title": plan["discussion_title"], "raw": raw, "summary": candidate_summary.strip(), "unresolved": "本次归档未自动运行代码或确认研究结论。"})
        if plan["body_mode"] == "create":
            store._save_body({"question_id": question["id"], "revision": store.detail(question["id"])["body_revision"], "text": plan["body"]})
        imported = []
        for file in files:
            if file["kind"] == "code":
                result = store._add_code({"question_id": question["id"], "filename": file["filename"], "purpose": "核查“" + question["title"] + "”；来源：" + discussion["id"], "base64": base64.b64encode(file["payload"]).decode()})
            else:
                folder = "结果/待关联" if file["kind"] == "result" else "资料"
                target = store._unique_file(store._qdir(question) / folder, file["filename"])
                store._atomic(target, file["payload"], new=True)
                result = {"name": target.name, "path": store._rel(target), "kind": file["kind"], "size": len(file["payload"]), "sha256": sha(file["payload"]), "run_id": ""}
            imported.append(result)
        store._refresh_overviews()
        result = {"framework_id": framework["id"], "content_id": content["id"], "question_id": question["id"], "framework_ids": [framework["id"]], "content_ids": [content["id"]], "question_ids": [question["id"]], "discussion_id": discussion["id"], "source_path": store._rel(source), "files": imported, "counts": preview["counts"], "candidate_conclusion": plan["candidate_conclusion"], "candidate_scope": plan["candidate_scope"]}
        store._write_text(receipt, json.dumps({"digest": digest, "result": result, "created_at": now()}, ensure_ascii=False), new=True)
    return result
