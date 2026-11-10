"""A separate synthetic example. Never seed the real project implicitly."""
from __future__ import annotations
import json
from pathlib import Path


def initialize_example(store):
    root = Path(store.root).resolve()
    if root.name != "示例研究":
        raise ValueError("示例只能初始化在名为“示例研究”的独立目录，不能写入正式研究。")
    marker = root / ".runtime" / "example.initialized.json"
    if marker.exists():
        return {"message": "示例已存在，原有操作与文件保持不变。", "root": str(root)}
    state = store.state()
    if state["contents"] or state["questions"] or state["frameworks"]:
        raise ValueError("示例目录已有资料但没有初始化标记，请保留资料并检查，系统不会覆盖它们。")
    store.perform("save_project", {
        "revision": state["project"]["revision"],
        "title": "使用示例：固定偏差校正",
        "brief": "这是软件操作与验收示例，只使用合成数字，不代表用户真实研究，也不是已发表结论。",
        "background": "用一个简单问题演示讨论、正文、验证计划、代码、结果和判断怎样关联保存。",
        "progress": "已整理加性偏差模型，可从最小数值核对开始。",
        "configuration": "真值 x = [0, 10, 20]；加性偏差 b = 2；不含随机噪声。",
        "symbols": "x：真值；y：含偏差的测量值；b：已知固定偏差；\\hat{x}：校正值。",
        "data_sources": "本示例仅含程序生成的数值，无任何旧研究数据。",
        "rules": "每轮只推进一步；明确假设；保存原始材料；程序通过不自动形成科学结论。",
    })
    content = store.perform("save_content", {"title": "固定偏差与校正", "objective": "明确已知固定偏差在加性模型中如何校正，并核对程序实现。"})
    store.perform("save_framework", {"title": "先建模，再验证", "status": "active", "mainline": "加性模型 → 校正推导 → 程序核对 → 适用条件", "content_ids": [content["id"]], "chapters": "第二章：固定偏差与校正\n第三章：实现核对与适用条件", "risks": "简单算例不能代表真实系统性能。"})
    store.perform("save_framework", {"title": "从结果差异回查模型", "status": "candidate", "mainline": "观察残差 → 核对实现 → 返回模型解释", "content_ids": [content["id"]], "chapters": "第二章：校正前后的对照\n第三章：模型与实现的一致性", "risks": "仍须保留推导前提，不能只展示达标结果。"})
    question = store.perform("create_question", {"content_id": content["id"], "title": "已知固定偏差能否直接校正？", "question": "在 y=x+b 且 b 已知恒定时，减去 b 是否恢复 x？", "why": "为这一部分的校正方法提供明确前提和可核对的实现。", "criterion": "对 x=[0,10,20]，校正后的绝对误差均小于 1e-8。", "outline": "1. 写出加性模型。\n2. 代入校正公式。\n3. 对三个输入核对程序。\n4. 明确已知偏差与估计偏差的区别。"})
    qid = question["id"]
    detail = store.detail(qid)
    store.perform("save_body", {"question_id": qid, "revision": detail["body_revision"], "text": "# 已知固定偏差的校正\n\n这是操作示例，不是用户的真实科研成果。\n\n## 模型与推导\n\n设测量值满足加性模型，偏差准确已知且在各次测量中保持不变。\n\n$$y=x+b,\\qquad \\hat{x}=y-b=x$$\n\n## 待核对部分\n\n数值程序是否使用与推导相同的 b；若出现残差，先检查参数与实现。\n\n## 适用条件\n\n仅讨论已知固定偏差，不包含随机噪声和偏差估计误差。"})
    discussion = store.perform("save_discussion", {"question_id": qid, "ai": "示例文本", "title": "建立模型并明确条件", "raw": "【操作示例，非真实 AI 调用】\n提问：固定偏差已知时怎样校正？\n示例回答：在 y=x+b 下直接减去 b 得到 x。需要保证实际程序使用的 b 与假设一致；数值检查不能替代一般推导。", "summary": "加性模型下校正成立的前提是 b 准确已知。", "unresolved": "核对数值实现中的偏差参数。"})
    store.perform("save_plan", {"question_id": qid, "title": "三个输入的最小实现核对", "method": "simulation", "target": "核对公式 x_hat=y-b 的实现与参数。", "criterion": "最大绝对误差 < 1e-8。", "inputs": "x=[0,10,20]；true_bias=2；无随机噪声。", "expected": "若实际使用 correction_bias=2，三个校正值应分别为 0、10、20。", "source_ids": [discussion["id"], "body"]})
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"question_id": qid, "note": "Synthetic software example only"}, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"root": str(root), "question_id": qid, "message": "独立示例已建立，可从问题正文继续。"}
