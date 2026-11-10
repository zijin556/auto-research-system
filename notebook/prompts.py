"""Local, deterministic research handoff prompts. No model calls or file writes.

The caller persists the returned text as an immutable prompt snapshot. A prompt
snapshot means prepared material, never a message that has been sent to an AI.
"""
from __future__ import annotations

import json
import ntpath
import re
from collections.abc import Mapping, Sequence
from pathlib import PurePosixPath
from typing import Any


TEMPLATES = {
    "P0": {"title": "上下文交接", "text": """请先阅读本次交接材料，简要复述当前问题、已经得到的依据与尚未解决的部分。
不要自行补全缺失的系统参数，不要把讨论意见当成已验证事实。
先确认本轮要推进的一个具体问题，再开始推导。"""},
    "P1": {"title": "逐步推导", "text": """我想理解这个问题的每一步推导。
1. 若还没有推导提纲，先列出 3–4 个步骤，说明各步要解决什么、依赖哪些假设，然后停下来等我确认。
2. 若已有提纲，本轮只推进其中一个具体步骤，写明前提、推导过程、物理含义和适用范围。
3. 完成这一步就停下，等我理解并回复后再继续；不要一次给出整篇长推导。
4. 沿用项目的符号与参数。明确区分已知事实、基本假设、推导结果和猜想。
可计算的中间式可以建议最小核查，但不必把每一步都强制变成仿真。"""},
    "P2": {"title": "解释没理解的一步", "text": """请只解释本轮关注的句子或公式：先给直观物理图像，再用一个最小数值例子，最后回到公式逐项对应。
说明这一步为什么成立、用了什么前提。不要借解释之机继续推后面的步骤；解释完等我确认。"""},
    "P3": {"title": "检查我的理解", "text": """下面本轮关注部分是我自己的理解。请逐句指出正确之处、遗漏的假设和不严谨之处，并说明理由。
先指出需要我自己修正的地方，不要直接替我写一篇全新的推导。最后用一个小问题检查我是否真的理解。"""},
    "P4": {"title": "交叉检查", "text": """请独立检查本轮选中的提纲、某一步或推导正文：
1. 假设与已登记的系统配置是否一致？
2. 变形、量纲、符号、边界条件有没有错误、跳步或隐含前提？
3. 是否存在简单反例，或者仅在特定范围内成立？
逐项给出“同意 / 有条件同意 / 不同意 / 资料不足”及具体理由，指出对应原文位置。
不要仅因为另一个 AI 也这样说就认可结论。优先给出能区分分歧的最小核查。"""},
    "P5": {"title": "设计最小验证", "text": """请针对当前的一步推导或一个判断设计最小验证，先不要写代码。
先选择适合的方式：理论核查、数值仿真或实测；纯理论问题不强求建立运行记录。
写清：验证对象（步骤或公式）、输入和已知条件、预期结果、可判定的通过/不通过标准、适用范围，以及最少需要的图或指标。
区分程序实现检查、模型内部一致性和独立验证。能使用解析特例、独立实现或外部基准时说明比较方法。
判据必须在查看结果前确定；若后续修改判据，应另存计划并说明原因。"""},
    "P6": {"title": "整理本轮讨论供一次保存", "text": """本轮先到这里。请整理一份能整段粘贴保存的 Markdown 文稿，不要让我逐项填表。
直接粘贴原始对话也能由系统理解归档，这份收尾稿是可选的阅读摘要。
按实际讨论的问题组织内容；一轮涉及多个问题时分别整理，不强行合并。保留问题之间的依赖、分歧和共用材料关系。
不要求补写论文框架、研究内容标签。沿用上下文的自然名称，不编造编号或新的研究主题；归属不明确就说明尚未确定。
保留下面三个摘要标签，标签各占一行；【要点】中可以按多个问题分别展开：
【主题】
用一句短话概括本轮推进了什么。
【要点】
- 讨论的具体问题：
- 得到的内容：分别标注事实、假设、推导结果、猜想或待验证判断。
- 关键公式与成立条件：
- 原文依据与使用的资料：保留可核对的引文、文件名或已有来源，不虚构引用。
【遗留问题】
- 下一次从哪一个具体问题继续；没有则写“无”。
若本轮产生代码，保留完整代码块，在围栏后同时写语言和文件名，例如三个反引号后写 python verify.py。
若有我提供的真实输出，同样保留命名的 csv、json 或 text 结果块；仅知道文件位置时列出文件名，不编造文件内容。
说明每份程序和输出回答哪个问题；共用程序或比较结果明确写出共用关系，不为不同问题擅自改写原文件。
若有候选结论，在对应问题下写“结论：……”和“适用范围：……”，明确尚待本人确认；未知内容不补造。
这是整理摘要，不是原始对话。不要虚构我已理解、已运行或已确认的内容。"""},
    "P7": {"title": "交给 Codex 编码验证", "text": """请只为本问题、本轮验证编写并执行必要的代码。
以附带的验证计划为准，先核对输入、冻结判据和输出目录；缺少关键条件就明确列出缺口，不自行编造。
代码文件开头注明问题编号、用途和针对的步骤。保留环境版本、随机种子（如适用）、实际命令、参数、退出状态、输出和日志。
必须输出“公式 ↔ 代码行号对照表”：推导步骤或公式 → 代码文件与行号 → 实现说明，明确离散化、近似、归一化和边界处理。
输出运行记录、必要图片或数据表，并报告文件名；不要覆写前一轮代码、结果或判断，也不要修改其他问题的资料。
程序正常结束、误差减小或本轮数值通过都不自动等于科学结论成立。请报告证据与限制，最终判断由我确认。"""},
    "P8": {"title": "分析验证结果", "text": """请对照本次运行对应的原计划和冻结判据分析结果：
1. 实际是否满足判据？引用具体运行编号、图或数值，并区分程序报错、未达标、部分满足和证据不足。
2. 结果对应推导的哪一步？用公式与代码行号对照表核查实现、参数和近似。
3. 若与预期不符，分别检查实现问题、输入差异、近似误差和理论假设，给出区分它们的最小补充验证。
4. 指出当前证据能支持到什么范围，哪些仍未验证。
不要为了得到通过结果而回改原判据，不要把一次失败运行直接宣布为理论被否定。先给分析和下一步建议，等我作判断。"""},
    "P9": {"title": "整理论文草稿", "text": """请根据问题定义、推导正文、证据和人工判断，整理成可追加到所属研究内容的论文草稿。
按“问题提出 → 理论推导 → 核查或验证 → 本节小结”组织；不适用的环节可省略，不得为了完整结构虚构实验。
每个关键判断保留来源编号、成立条件和局限。已验证、待验证、需复查、证据不足和被反驳的内容必须分清。
如果没有已确认结论，只能写研究动机、当前推导和待补充部分，不能写成已证明或已验证的成果。
沿用已有符号。无法接续已有公式编号时先使用局部编号并标注待统编。
请只输出草稿供我审阅；不要声称已写入文件或已获我确认。"""},
    "P10": {"title": "比较论文框架", "text": """请比较附带的候选论文框架：
1. 各自的主线、章节递进与拟回答的问题是否清楚？
2. 各框架引用了哪些研究内容；分别有哪些已确认结论、被反驳路径和证据缺口？
3. 各自需要补什么工作，风险和可完成性有什么差别？
给出对照表和需要我决定的取舍。不要将候选框架当成已经采用，也不要替我自动切换框架。
研究内容由框架引用和排列，不能因换框架而复制或改变其原始证据。"""},
}

STATUS_LABELS = {
    "discussing": "讨论中", "pending": "待验证", "running": "验证中",
    "needs_judgment": "已有结果，待人工判断", "review": "需复查",
    "supported": "验证支持", "refuted": "验证不支持",
    "inconclusive": "证据不足", "not_required": "不需要验证",
}
METHOD_LABELS = {"theory": "理论核查", "simulation": "数值仿真", "measurement": "实测验证"}


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)):
        return "\n".join(_text(item) for item in value if _text(item))
    if isinstance(value, Mapping):
        return json.dumps(dict(value), ensure_ascii=False, indent=2)
    return str(value)


def parse_capture(raw: str) -> dict[str, str]:
    """Extract P6 labels for prefill without modifying or replacing the input.

    Repeated labeled sections are joined rather than silently dropped. This
    function never treats an unlabelled conversation as an AI-authored summary.
    """
    if not isinstance(raw, str):
        raise ValueError("粘贴内容必须是文本")
    tags = list(re.finditer(r"(?:\*\*)?【(主题|要点|遗留问题)】(?:\*\*)?[ \t]*[:：]?[ \t]*", raw))
    parts = {"主题": [], "要点": [], "遗留问题": []}
    for index, match in enumerate(tags):
        end = tags[index + 1].start() if index + 1 < len(tags) else len(raw)
        value = raw[match.end():end].strip()
        # An enclosing code fence or a Markdown heading prefix is presentation,
        # not part of the next field. The caller still saves raw verbatim.
        value = re.sub(r"\n[ \t]*#{1,6}[ \t]*$", "", value).strip()
        value = re.sub(r"\n?```[ \t]*$", "", value).strip()
        if value:
            parts[match.group(1)].append(value)
    return {
        "title": "\n\n".join(parts["主题"]),
        "summary": "\n\n".join(parts["要点"]),
        "unresolved": "\n\n".join(parts["遗留问题"]),
    }


def _section(title: str, value: Any, *, empty: str = "") -> str:
    body = _text(value) or empty
    return f"## {title}\n\n{body}" if body else ""


def _fields(record: Mapping[str, Any], fields: Sequence[tuple[str, str]]) -> str:
    return "\n\n".join(
        f"{label}：\n{_text(record.get(key))}" for key, label in fields if _text(record.get(key))
    )


def _record_text(kind: str, record: Mapping[str, Any], *, full: bool = False) -> str:
    rid = _text(record.get("id"))
    title = _text(record.get("title"))
    if record.get("_frozen_for"):
        title += "（" + _text(record["_frozen_for"]) + " 建立时的计划快照）"
    rows = [f"### {rid} {title}".rstrip()]
    if record.get("path"):
        rows.append("来源文件：" + _text(record["path"]))
    if kind == "discussion":
        rows.append(_fields(record, (("ai", "对话 AI"), ("parent_id", "接续讨论"), ("summary", "摘要"),
                                     ("understanding", "我的理解"), ("unresolved", "遗留问题"))))
        if full and record.get("raw"):
            rows.append("原始粘贴内容（可能是对话或保存块，以记录类型为准）：\n" + _text(record["raw"]))
    elif kind == "plan":
        rows.append("验证方式：" + METHOD_LABELS.get(_text(record.get("method")), _text(record.get("method"))))
        criterion_label = "运行时冻结判据" if record.get("_frozen_for") else "计划中登记的判据（运行判断以其快照为准）"
        rows.append(_fields(record, (("target", "验证对象"), ("criterion", criterion_label), ("inputs", "输入与条件"),
                                     ("expected", "预期结果"), ("source_ids", "设计依据"))))
    elif kind == "run":
        rows.append(_fields(record, (("plan_id", "对应验证计划"), ("target_step", "对应推导步骤"),
                                     ("criterion", "本轮冻结判据（以运行建立时为准）"), ("plan_snapshot", "本轮验证计划快照"),
                                     ("code_paths", "代码路径"), ("code_snapshots", "代码快照与哈希"),
                                     ("command", "实际命令"), ("parameters", "参数"),
                                     ("mapping", "公式与代码行号对照表"), ("expected", "预期结果"),
                                     ("actual", "实际结果"), ("analysis", "观察分析"), ("outcome", "本轮运行/判据状态"))))
    elif kind == "prompt":
        rows.append("已保存交接包（只是待发送或曾准备的内容，不证明已发送）：\n" + _text(record.get("text")))
    else:
        decision = _text(record.get("decision"))
        rows.append("人工判断类型：" + STATUS_LABELS.get(decision, decision or "未记录"))
        rows.append(_fields(record, (("text", "判断内容"), ("evidence_ids", "证据编号"),
                                     ("scope", "成立条件与局限"), ("meaning", "对论文的意义"))))
    return "\n\n".join(row for row in rows if row)


def _question_directory(project: Mapping[str, Any], question: Mapping[str, Any]) -> str:
    absolute_directory = _text(question.get("absolute_path"))
    path = absolute_directory or _text(question.get("path"))
    if not path:
        return ""
    path = path.replace("\\", "/")
    if not absolute_directory and PurePosixPath(path).suffix:
        path = str(PurePosixPath(path).parent)
    root = _text(question.get("root") or project.get("root"))
    if root and not ntpath.isabs(path):
        path = ntpath.join(root, path).replace("\\", "/")
    return path.rstrip("/")


def _select_records(question: Mapping[str, Any], sources: Any) -> tuple[list[tuple[str, Mapping[str, Any]]], bool]:
    all_records: list[tuple[str, Mapping[str, Any]]] = []
    for key, kind in (("discussions", "discussion"), ("plans", "plan"), ("runs", "run"), ("judgments", "judgment"), ("prompts", "prompt")):
        all_records.extend((kind, item) for item in question.get(key, []) if isinstance(item, Mapping))
    explicit = sources.get("source_ids") if isinstance(sources, Mapping) else sources
    if explicit is not None:
        if isinstance(explicit, str):
            explicit = [explicit]
        if not isinstance(explicit, Sequence):
            raise ValueError("选择的资料编号必须是列表")
        ids = {_text(item.get("id")) if isinstance(item, Mapping) else _text(item) for item in explicit}
        ids.discard("")
        known = {_text(item.get("id")) for _, item in all_records} | {"正文", "body", "BODY", "推导笔记"}
        unknown = ids - known
        if unknown:
            raise ValueError("选中的资料不存在于本问题：" + "、".join(sorted(unknown)))
        selected = [(kind, record) for kind, record in all_records if record.get("id") in ids]
        # Prefer the actual frozen plan file. Its source may have been edited
        # later outside the application, while the run's snapshot is retained.
        plans = {record.get("id"): record for kind, record in all_records if kind == "plan"}
        selected_ids = {record.get("id") for _, record in selected}
        for kind, record in list(selected):
            plan_id = record.get("plan_id")
            frozen = record.get("frozen_plan")
            if kind == "run" and isinstance(frozen, Mapping):
                selected.append(("plan", {**frozen, "_frozen_for": record.get("id")}))
            elif kind == "run" and plan_id in plans and plan_id not in selected_ids:
                selected.append(("plan", plans[plan_id]))
                selected_ids.add(plan_id)
        return selected, True
    selected = []
    for kind in ("discussion", "plan", "run", "judgment"):
        group = [(k, record) for k, record in all_records if k == kind]
        if group:
            selected.append(group[-1])
    run = next((record for kind, record in selected if kind == "run"), None)
    if run and run.get("plan_id"):
        frozen = run.get("frozen_plan")
        if isinstance(frozen, Mapping):
            selected = [(kind, record) for kind, record in selected if kind != "plan" or record.get("id") != run["plan_id"]]
            selected.append(("plan", {**frozen, "_frozen_for": run.get("id")}))
        else:
            plan = next((record for kind, record in all_records if kind == "plan" and record.get("id") == run["plan_id"]), None)
            if plan is not None and all(record.get("id") != plan.get("id") for _, record in selected):
                selected.append(("plan", plan))
    return selected, False


def build_prompt(template: str, project: Mapping[str, Any] | None, framework: Any,
                 content: Mapping[str, Any] | None, question: Mapping[str, Any] | None,
                 focus: str = "", sources: Any = None) -> str:
    """Build a provenance-aware handoff using only caller-supplied records."""
    template = str(template).upper()
    if template not in TEMPLATES:
        raise ValueError("未知提示词编号，请选择 P0–P10")
    project, content, question = project or {}, content or {}, question or {}
    frames = sources.get("frameworks", []) if isinstance(sources, Mapping) else []
    if isinstance(framework, (list, tuple)):
        frames = list(framework)
        framework = next((f for f in frames if f.get("status") == "active"), None)
    framework = framework or {}
    parts = [f"# {template} · {TEMPLATES[template]['title']}", TEMPLATES[template]["text"]]
    if _text(focus):
        parts.append(_section("本轮只处理这一点", focus))
    overview = _text(project.get("brief")) or _text(project.get("background")) or "尚未填写，请勿自行推断。"
    parts.append(_section("项目背景", "项目：" + (_text(project.get("title")) or "未命名项目") + "\n\n" + overview))
    for key, label in (("progress", "当前进展"), ("configuration", "可复用系统配置"), ("symbols", "符号约定"),
                       ("data_sources", "已有数据与位置"), ("rules", "对 AI 的统一要求")):
        parts.append(_section(label, project.get(key)))
    if framework:
        fstatus = {"active": "当前采用", "candidate": "候选（尚未采用）", "parked": "暂存"}.get(framework.get("status"), "未明确采用状态")
        parts.append(_section("论文框架", f"{framework.get('id', '')} {framework.get('title', '')} · {fstatus}\n\n" +
                              _fields(framework, (("mainline", "主线"), ("content_ids", "有序引用研究内容"), ("chapters", "章节安排")))))
    else:
        parts.append(_section("论文框架", "尚未选择当前采用的框架。"))
    if content:
        parts.append(_section("所属研究内容", f"{content.get('id', '')} {content.get('title', '')}\n\n" + _text(content.get("objective"))))
    selected, full = _select_records(question, sources)
    if question:
        status = _text(question.get("status"))
        parts.append(_section("当前问题", f"{question.get('id', '')} {question.get('title', '')}\n\n当前研究状态：{STATUS_LABELS.get(status, status or '未记录')}\n\n" +
                              _fields(question, (("question", "问题定义"), ("why", "为什么要问"), ("criterion", "当前问题判据"), ("outline", "分步提纲")))))
        parts.append(_section("当前推导正文", question.get("body"), empty="尚未整理正文；不要把对话意见冒充已确认推导。"))
        if selected:
            parts.append(_section("本轮依据材料" if full else "近期进展与依据（摘要交接）",
                                  "\n\n".join(_record_text(kind, record, full=full) for kind, record in selected)))
        else:
            parts.append(_section("本轮依据材料", "尚无已保存或被选中的讨论、验证记录与人工判断。"))
        if status in {"review", "inconclusive", "needs_judgment", "running", "pending", "discussing"}:
            parts.append(_section("本题结论边界", "当前状态不代表已验证成立。历史判断、对话建议和运行完成不能替代本轮的人工判断。"))
    if template == "P7":
        directory = _question_directory(project, question)
        targets = ["问题编号：" + (_text(question.get("id")) or "尚未选择问题")]
        if directory:
            targets.extend(["问题目录：" + directory + "/", "代码目录：" + directory + "/代码/"])
        selected_runs = [record for kind, record in selected if kind == "run"]
        pending_runs = [record for record in selected_runs if record.get("outcome", "pending") == "pending"]
        if pending_runs:
            run = pending_runs[-1]
            run_path = _text(run.get("path")).replace("\\", "/")
            run_dir = str(PurePosixPath(run_path).parent) if PurePosixPath(run_path).suffix else run_path
            run_root = _text(question.get("root") or project.get("root"))
            if run_root and run_dir and not ntpath.isabs(run_dir):
                run_dir = ntpath.join(run_root, run_dir).replace("\\", "/")
            targets.append("本次已登记运行：" + _text(run.get("id")))
            if run_dir:
                targets.append("本次结果目录：" + run_dir.rstrip("/") + "/")
            targets.append("请在该运行的运行记录.md 中填写实际结果、命令、参数与公式代码对照，不改前轮记录。")
        else:
            targets.append("当前没有选中的待执行运行记录。先在本问题新建本轮运行，取得结果目录后再执行；不要在已经完成的运行目录覆写输出。")
        targets.extend(["交付文件：代码、运行记录.md、必要的图/数据表/日志。",
                        "公式 ↔ 代码行号对照表：\n| 推导步骤/公式 | 代码文件与行号 | 实现、近似或边界处理 |\n|---|---|---|"])
        parts.append(_section("本问题的执行与保存位置", "\n\n".join(targets)))
    if template == "P9":
        path = _text(content.get("path"))
        if path:
            parent = str(PurePosixPath(path.replace("\\", "/")).parent)
            parts.append(_section("草稿归属", f"研究内容 {content.get('id', '')}，目标：{parent}/草稿.md。\n先输出供我审核，再由我选择追加到草稿；保留本问题编号及证据。"))
    if template == "P10":
        if not frames:
            frames = project.get("frameworks", []) or ([framework] if framework else [])
        fblocks = []
        for f in frames:
            if not isinstance(f, Mapping):
                continue
            fblocks.append(f"### {f.get('id', '')} {f.get('title', '')}\n\n" +
                           _fields(f, (("status", "采用状态"), ("mainline", "主线"), ("content_ids", "引用研究内容"),
                                       ("chapters", "章节安排"), ("risks", "优点、风险与缺口"))))
        parts.append(_section("待比较的全部框架", "\n\n".join(fblocks), empty="尚未登记候选框架，不能比较。"))
        if isinstance(sources, Mapping):
            contents = sources.get("contents", [])
            questions = sources.get("questions", [])
            if contents:
                parts.append(_section("研究内容清单", "\n".join(f"- {c.get('id', '')} {c.get('title', '')}：{c.get('objective', '')}" for c in contents if isinstance(c, Mapping))))
            if questions:
                parts.append(_section("各问题的当前研究状态", "\n".join(f"- {q.get('id', '')} {q.get('title', '')}：{STATUS_LABELS.get(q.get('status'), q.get('status', '未记录'))}" for q in questions if isinstance(q, Mapping))))
    return "\n\n".join(part for part in parts if part).rstrip() + "\n"


def library_markdown() -> str:
    sections = ["# 科研讨论提示词", "提示词可复制给任意 AI；生成与保存快照不等于已发送。先选具体问题，系统会拼入它的背景、正文与证据。"]
    sections.extend(f"## {key} {value['title']}\n\n{value['text']}" for key, value in TEMPLATES.items())
    return "\n\n".join(sections) + "\n"
