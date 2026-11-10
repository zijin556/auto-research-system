"""Ground and archive a model-produced multi-question plan without running code."""
from __future__ import annotations

import base64
import copy
import json
import os
import re
from pathlib import Path
from urllib.parse import quote

from .capture import _extract
from .imports import FileTransaction, _request
from .store import ConflictError, clean_text, now, sha, title_text

KEY_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,39}")
BATCH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{7,79}")


def _blocks(raw):
    """Split readable paragraphs/fences while preserving every original byte."""
    blocks, pending, marker = [], [], None
    def flush(kind="text"):
        if pending:
            blocks.append({"id": f"B{len(blocks)+1:03}", "text": "".join(pending), "kind": kind, "material_ids": []})
            pending.clear()
    for line in raw.splitlines(keepends=True):
        opener = re.match(r"^\s{0,3}(`{3,}|~{3,})([^\r\n]*)", line)
        if marker:
            pending.append(line)
            if re.fullmatch(r"\s{0,3}" + re.escape(marker[0]) + "{" + str(len(marker)) + r",}\s*", line.rstrip("\r\n")):
                flush("fence")
                marker = None
        elif opener:
            flush()
            marker = opener[1]
            pending.append(line)
        else:
            pending.append(line)
            if not line.strip():
                flush()
    flush("unclosed_fence" if marker else "text")
    if len(blocks) > 3000:
        raise ValueError("讨论段落超过 3000 段，请分轮整理。")
    return blocks


def _catalog(store):
    fs, cs, qs = store._frameworks(), store._contents(), store._questions()
    project = store._project()
    fields = {"frameworks": (fs, ["id", "title", "mainline", "content_ids", "status"]),
              "contents": (cs, ["id", "title", "objective"]),
              "questions": (qs, ["id", "content_id", "title", "question", "why", "criterion"])}
    catalog = {key: [{name: r.get(name, "") for name in names} for r in records] for key, (records, names) in fields.items()}
    catalog["project_context"] = {name: project.get(name, "") for name in ["title", "brief", "background", "progress", "configuration", "symbols", "data_sources", "rules"]}
    revision = sha(json.dumps([[r["id"], r["revision"]] for r in [project] + fs + cs + qs], ensure_ascii=False, sort_keys=True).encode())
    return catalog, revision


def _prepare(store, data):
    raw = data.get("text", "")
    clean_text(raw, "讨论原文", False)
    blocks = _blocks(raw)
    materials, warnings = [], []
    previous = ""
    for block in blocks:
        if block["kind"] == "fence":
            # A filename in the immediately preceding prose is a useful hint;
            # only extracted fence bytes, never the prefix, become the file.
            prefix = previous.rstrip().splitlines()[-1] + "\n" if previous.strip() else ""
            _, extracted, notices = _extract(store, prefix + block["text"], [])
            warnings += notices
            for item in extracted:
                mid = f"M{len(materials)+1:03}"
                materials.append({**item, "id": mid, "source": "fence", "source_block_id": block["id"], "attachment_index": None})
                block["material_ids"].append(mid)
        elif block["kind"] == "unclosed_fence":
            warnings.append("未结束的代码块保留在原文中，不自动生成程序文件。")
        previous = block["text"]
    attachments = data.get("files") or []
    if not isinstance(attachments, list):
        raise ValueError("附件必须是列表。")
    _, extra, notices = _extract(store, "", attachments)
    warnings += notices
    for index, item in enumerate(extra):
        materials.append({**item, "id": f"M{len(materials)+1:03}", "source": "attachment", "source_block_id": "", "attachment_index": index})
    if len(materials) > 100 or sum(len(f["payload"]) for f in materials) > 32 * 1024 * 1024:
        raise ValueError("单轮最多 100 个文件且合计不超过 32 MB。")
    catalog, revision = _catalog(store)
    public = []
    for item in materials:
        truncated = False
        try:
            preview = item["payload"].decode("utf-8-sig")
            truncated = len(preview) > 18000 and "\x00" not in preview
            preview = preview[:18000] if "\x00" not in preview else ""
        except UnicodeError:
            preview = ""
        public.append({k: v for k, v in item.items() if k != "payload"} | {"size": len(item["payload"]), "sha256": sha(item["payload"]), "text_preview": preview, "text_preview_truncated": truncated})
    digest = sha(json.dumps({"text": raw, "materials": [{"id": f["id"], "filename": f["filename"], "kind": f["kind"], "sha256": sha(f["payload"])} for f in materials]}, ensure_ascii=False, sort_keys=True).encode())
    return {"blocks": blocks, "materials": public, "catalog": catalog, "catalog_revision": revision, "input_digest": digest, "warnings": warnings}, materials, raw


def prepare_semantic_input(store, data):
    return _prepare(store, data)[0]


def _ids(value, allowed, name):
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        raise ValueError(name + "必须是编号列表。")
    if len(value) != len(set(value)) or any(v not in allowed for v in value):
        raise ValueError(name + "包含未知或重复编号。")
    return value


def _nodes(plan, name, required, count):
    rows = plan.get(name)
    if not isinstance(rows, list) or len(rows) > count:
        raise ValueError("AI 整理的" + name + "格式或数量不正确。")
    keys = set()
    for item in rows:
        if not isinstance(item, dict) or set(item) != set(required):
            raise ValueError("AI 整理条目缺少字段或包含未支持字段：" + name)
        key = item.get("key")
        if not isinstance(key, str) or not KEY_RE.fullmatch(key) or key in keys:
            raise ValueError("AI 整理条目标识无效或重复。")
        keys.add(key)
        if not isinstance(item["existing_id"], str):
            raise ValueError("已有条目标识必须为字符串。")
        item["title"] = title_text(item["title"])
        item["summary"] = clean_text(item["summary"], "AI 整理摘要")
    return rows, keys


def _validated(store, data):
    prepared, materials, raw = _prepare(store, data)
    if data.get("catalog_revision") != prepared["catalog_revision"]:
        raise ConflictError("研究目录在 AI 整理期间发生了变化，请重新整理。")
    plan = copy.deepcopy(data.get("plan"))
    if not isinstance(plan, dict) or set(plan) != {"frameworks", "contents", "questions", "unassigned_source_ids", "unassigned_material_ids", "notes"}:
        raise ValueError("AI 未返回完整的多问题整理方案。")
    if not isinstance(plan["notes"], list) or any(not isinstance(x, str) for x in plan["notes"]):
        raise ValueError("AI 整理说明格式不正确。")
    plan["notes"] = [clean_text(s, "整理说明") for s in plan["notes"]]
    common = ["key", "existing_id", "title", "summary", "source_ids"]
    fs, fkeys = _nodes(plan, "frameworks", common, 30)
    cs, ckeys = _nodes(plan, "contents", common + ["framework_keys"], 100)
    qs, qkeys = _nodes(plan, "questions", common + ["content_key", "criterion", "candidate_conclusion", "scope", "material_ids"], 200)
    if len(fkeys | ckeys | qkeys) != len(fkeys) + len(ckeys) + len(qkeys):
        raise ValueError("不同层级不能使用相同的临时标识。")
    block_map = {b["id"]: b for b in prepared["blocks"]}
    material_map = {m["id"]: m for m in prepared["materials"]}
    catalogs = {kind: {r["id"]: r for r in prepared["catalog"][kind]} for kind in ["frameworks", "contents", "questions"]}
    for kind, rows in [("frameworks", fs), ("contents", cs), ("questions", qs)]:
        used = set()
        for item in rows:
            _ids(item["source_ids"], block_map, "原文依据")
            eid = item["existing_id"]
            if eid:
                if eid not in catalogs[kind] or eid in used:
                    raise ValueError("AI 指向了不存在或重复的已有条目。")
                used.add(eid)
                item["title"] = catalogs[kind][eid]["title"]
            elif not item["source_ids"]:
                raise ValueError("新建研究条目必须引用讨论原文。")
    c_map = {c["key"]: c for c in cs}
    f_map = {f["key"]: f for f in fs}
    for c in cs:
        _ids(c["framework_keys"], fkeys, "所属框架")
        if not c["framework_keys"]:
            raise ValueError("研究内容缺少所属框架。")
    covered_blocks, covered_materials = set(), set()
    enriched = []
    for q in qs:
        if q["content_key"] not in ckeys or not q["source_ids"]:
            raise ValueError("研究问题缺少内容归属或原文依据。")
        content = c_map[q["content_key"]]
        if q["existing_id"] and catalogs["questions"][q["existing_id"]]["content_id"] != content["existing_id"]:
            raise ValueError("已有问题不能被转移到另一项研究内容。")
        for field in ["criterion", "candidate_conclusion", "scope"]:
            q[field] = clean_text(q[field], field)
        _ids(q["material_ids"], material_map, "问题材料")
        for mid in q["material_ids"]:
            bid = material_map[mid]["source_block_id"]
            if bid and bid not in q["source_ids"]:
                raise ValueError("代码或结果块的归属需要包含对应原文依据。")
        covered_blocks.update(q["source_ids"])
        covered_materials.update(q["material_ids"])
        store._record_text("discussion", {"title": q["title"]}, {"summary": q["summary"] + "\n" + q["candidate_conclusion"] + "\n" + q["scope"], "raw": "来源原文另存"})
        store._record_text("question", {"title": q["title"]}, {"question": q["title"], "criterion": q["criterion"]})
        enriched.append({**q, "content": {"key": content["key"], "title": content["title"]}, "frameworks": [{"key": key, "title": f_map[key]["title"]} for key in content["framework_keys"]], "sources": [block_map[i] for i in q["source_ids"]], "materials": [material_map[i] for i in q["material_ids"]]})
    unassigned_blocks = _ids(plan["unassigned_source_ids"], block_map, "未归属原文")
    unassigned_materials = _ids(plan["unassigned_material_ids"], material_map, "未归属材料")
    if set(unassigned_blocks) & covered_blocks or set(unassigned_materials) & covered_materials:
        raise ValueError("已归属材料不能同时标成未归属。")
    if covered_blocks | set(unassigned_blocks) != set(block_map) or covered_materials | set(unassigned_materials) != set(material_map):
        raise ValueError("AI 整理遗漏了部分原文或文件，尚未保存，请重新整理。")
    for f in fs:
        store._record_text("framework", {"title": f["title"]}, {"mainline": f["summary"]})
    for c in cs:
        store._record_text("content", {"title": c["title"]}, {"objective": c["summary"]})
    counts = {"frameworks": sum(not f["existing_id"] for f in fs), "contents": sum(not c["existing_id"] for c in cs), "questions": len(qs), "new_questions": sum(not q["existing_id"] for q in qs), "existing_questions": sum(bool(q["existing_id"]) for q in qs), "discussions": len(qs), "codes": sum(m["kind"] == "code" for m in materials), "results": sum(m["kind"] == "result" for m in materials), "unassigned_materials": len(unassigned_materials)}
    token = sha(json.dumps({"input_digest": prepared["input_digest"], "catalog_revision": prepared["catalog_revision"], "plan": plan, "model_info": data.get("model_info", {})}, ensure_ascii=False, sort_keys=True).encode())
    warnings = prepared["warnings"] + (["部分原文或文件没有足够依据归入具体问题，将随本轮原文完整保存。"] if unassigned_blocks or unassigned_materials else [])
    return {"plan": plan, "counts": counts, "questions": enriched, "unassigned_sources": [block_map[i] for i in unassigned_blocks], "unassigned_materials": [material_map[i] for i in unassigned_materials], "warnings": warnings, "token": token, "catalog_revision": prepared["catalog_revision"], "can_import": True}, materials, raw, prepared


def preview_semantic(store, data):
    return _validated(store, data)[0]


def import_semantic(store, data):
    receipt, digest, prior = _request(store, "semantic", data)
    if prior is not None:
        return prior
    preview, materials, raw, prepared = _validated(store, data)
    if data.get("token") != preview["token"]:
        raise ConflictError("整理内容已改变，请重新预览后保存。")
    plan = preview["plan"]
    batch = store._safe(Path("04_交接包/AI整理") / data["request_id"])
    source = batch / "讨论原文.md"
    batch_files, origins, question_results, frame_ids, content_ids, qids = [], {}, [], {}, {}, {}
    def write_batch(path, payload):
        store._atomic(path, payload, new=True)
        batch_files.append(store._rel(path))
    def link(from_path, target):
        return quote(os.path.relpath(target, from_path.parent).replace("\\", "/"), safe="/")
    with FileTransaction(store):
        write_batch(source, raw.encode("utf-8"))
        write_batch(batch / "模型整理方案.json", json.dumps({"plan": data["plan"], "model_info": data.get("model_info", {}), "catalog_revision": data["catalog_revision"], "input_digest": prepared["input_digest"]}, ensure_ascii=False, indent=2).encode())
        write_batch(batch / "原文分段.json", json.dumps(prepared["blocks"], ensure_ascii=False, indent=2).encode())
        for item in materials:
            target = batch / "原始材料" / item["id"] / item["filename"]
            write_batch(target, item["payload"])
            origins[item["id"]] = {"id": item["id"], "filename": item["filename"], "kind": item["kind"], "path": store._rel(target), "sha256": sha(item["payload"]), "source_block_id": item["source_block_id"], "source": item["source"], "attachment_index": item["attachment_index"]}
        for content in plan["contents"]:
            saved = store._content(content["existing_id"]) if content["existing_id"] else store._save_content({"title": content["title"], "objective": content["summary"]})
            content_ids[content["key"]] = saved["id"]
        for frame in plan["frameworks"]:
            cids = [content_ids[c["key"]] for c in plan["contents"] if frame["key"] in c["framework_keys"]]
            if frame["existing_id"]:
                saved = store._find(store._frameworks(), frame["existing_id"], "框架")
                additions = [cid for cid in cids if cid not in saved["content_ids"]]
                if additions:
                    saved = store._save_framework({"id": saved["id"], "revision": saved["revision"], "content_ids": saved["content_ids"] + additions})
            else:
                saved = store._save_framework({"title": frame["title"], "mainline": frame["summary"], "status": "candidate", "content_ids": cids})
            frame_ids[frame["key"]] = saved["id"]
        bmap = {b["id"]: b for b in prepared["blocks"]}
        mmap = {m["id"]: m for m in materials}
        cmap = {c["key"]: c for c in plan["contents"]}
        for item in plan["questions"]:
            question = store._question(item["existing_id"]) if item["existing_id"] else store._create_question({"content_id": content_ids[item["content_key"]], "title": item["title"], "question": item["title"], "criterion": item["criterion"]})
            qids[item["key"]] = question["id"]
            qdir = store._qdir(question)
            source_ref = qdir / "资料" / ("AI整理来源_" + data["request_id"] + ".md")
            rows = ["# 本轮 AI 整理来源", "", "- [完整原始讨论](" + link(source_ref, source) + ")", "- [模型整理方案](" + link(source_ref, batch / "模型整理方案.json") + ")", "", "## 本题原文段落", ""]
            for bid in item["source_ids"]:
                rows += ["### " + bid, "", bmap[bid]["text"], ""]
            rows += ["## 本题材料的共同来源", ""]
            for mid in item["material_ids"]:
                origin = origins[mid]
                rows += ["- [" + origin["filename"] + "](" + link(source_ref, store._safe(origin["path"])) + ") · SHA256 `" + origin["sha256"] + "`"]
            store._atomic(source_ref, "\n".join(rows).encode(), new=True)
            summary = "AI 按内容整理的摘要（需核对原文）：\n\n" + item["summary"]
            if item["candidate_conclusion"]:
                summary += "\n\n结论：" + item["candidate_conclusion"] + "\n\n适用范围：" + item["scope"]
            provenance = "本记录由 AI 从多问题讨论中整理。原文段落：" + "、".join(item["source_ids"]) + "。\n\n[阅读本题原文与材料来源](" + link(qdir / "对话" / "placeholder.md", source_ref) + ")"
            discussion = store._save_discussion({"question_id": question["id"], "ai": "Codex · 内容理解整理", "title": item["title"][:145] + " · AI 整理", "raw": provenance, "summary": summary, "unresolved": "自动归档不代表推导正确或数值验证已完成。"})
            files = []
            for mid in item["material_ids"]:
                material = mmap[mid]
                if material["kind"] == "code":
                    stored = store._add_code({"question_id": question["id"], "filename": material["filename"], "purpose": "针对“" + question["title"] + "”；AI 整理摘要来源：" + discussion["id"] + "；共同原件：" + origins[mid]["path"], "base64": base64.b64encode(material["payload"]).decode()})
                else:
                    target = store._unique_file(qdir / ("结果/待关联" if material["kind"] == "result" else "资料"), material["filename"])
                    store._atomic(target, material["payload"], new=True)
                    stored = {"path": store._rel(target), "name": target.name, "kind": material["kind"], "size": len(material["payload"]), "sha256": sha(material["payload"]), "run_id": ""}
                files.append({**stored, "material_id": mid, "origin_path": origins[mid]["path"], "origin_sha256": origins[mid]["sha256"]})
            if not item["existing_id"]:
                body_path = qdir / "推导笔记.md"
                body = "# " + question["title"] + "\n\n> AI 根据整段讨论整理的正文草稿；结论须结合原文与证据核对。\n\n" + item["summary"]
                if item["criterion"]:
                    body += "\n\n## 讨论中的判据\n\n" + item["criterion"]
                if item["candidate_conclusion"]:
                    body += "\n\n## 待确认的讨论结论\n\n" + item["candidate_conclusion"] + "\n\n### 适用范围\n\n" + item["scope"]
                body += "\n\n## 原文与材料依据\n\n[查看本题原文段落和文件出处](" + link(body_path, source_ref) + ")"
                store._save_body({"question_id": question["id"], "revision": store.detail(question["id"])["body_revision"], "text": body})
            question_results.append({"id": question["id"], "title": question["title"], "framework_id": frame_ids[cmap[item["content_key"]]["framework_keys"][0]], "content_id": question["content_id"], "discussion_id": discussion["id"], "source_path": store._rel(source_ref), "files": files})
        intake = ["# 本轮 AI 整理", "", "[完整原始讨论](讨论原文.md)", "", "## 归档的问题", ""]
        for item in question_results:
            path = store._safe(next(q["path"] for q in store._questions() if q["id"] == item["id"]))
            intake.append("- [" + item["title"] + "](" + link(batch / "本轮整理.md", path) + ")")
        intake += ["", "## 尚未确定归属的原文", ""]
        for bid in plan["unassigned_source_ids"]:
            intake += ["### " + bid, "", bmap[bid]["text"], ""]
        intake += ["## 尚未确定归属的材料", ""]
        for mid in plan["unassigned_material_ids"]:
            item = origins[mid]
            intake.append("- [" + item["filename"] + "](" + link(batch / "本轮整理.md", store._safe(item["path"])) + ")")
        intake += ["", "## AI 整理说明", "", "\n".join(plan["notes"])]
        write_batch(batch / "本轮整理.md", "\n".join(intake).encode())
        manifest_path = batch / "归档索引.json"
        batch_files.append(store._rel(manifest_path))
        store._atomic(manifest_path, json.dumps({"created_at": now(), "file_paths": batch_files, "materials": list(origins.values()), "questions": question_results, "unassigned_source_ids": plan["unassigned_source_ids"], "unassigned_material_ids": plan["unassigned_material_ids"]}, ensure_ascii=False, indent=2).encode(), new=True)
        store._refresh_overviews()
        result = {"framework_ids": list(frame_ids.values()), "content_ids": list(content_ids.values()), "question_ids": list(qids.values()), "questions": question_results, "source_path": store._rel(source), "batch_path": store._rel(batch / "本轮整理.md"), "counts": preview["counts"]}
        store._write_text(receipt, json.dumps({"digest": digest, "result": result, "created_at": now()}, ensure_ascii=False), new=True)
    return result
