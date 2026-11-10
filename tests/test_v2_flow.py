"""End-to-end v2 workflow with the fake CLIs (no quota is used)."""
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from notebook import agents, rounds
from notebook import ledger as L
from notebook.store import Store
from notebook.v2api import V2, parse_candidate

APP = Path(__file__).resolve().parent.parent
FENCE = "````"

REPLIES = {
    "open_question": {"text": "\n".join([
        "下面是锚点草稿。", "", "```锚点草稿", "## 要回答的问题", "", "三步相移能否从纯正弦强度中精确恢复基频幅值？", "",
        "## 判据", "", "理想模型下幅值相对误差小于 1e-10。", "", "## 假设", "",
        "| 编号 | 内容 | 依据 | 失效条件 | 状态 | 来源 |", "|---|---|---|---|---|---|",
        "| H1 | 相移严格为 2πk/3 | 系统标定 | 相移误差超过 0.01 rad | 待批 | GPT·第1轮 |", "",
        "## 范围", "", "b>0，a≥b。", "", "## 不做什么", "", "不讨论噪声。", "```", "",
        FENCE + "正文修订", "节：摘要", "方式：替换", "本文研究三步相移的幅值恢复。", FENCE, "",
        "```台账变更", "无", "```", "", "结论摘要：起草了锚点"])},
    "q_draft": {"text": "\n".join([
        "C1 推导结果：三次单位根求和使直流项与负频项抵消。", "",
        FENCE + "正文修订", "节：2 理论推导", "方式：替换", "由式(1)得 $Z=b e^{i\\phi}$，对应台账 [新1]。", FENCE, "",
        "```台账变更", "+ [新1] 推导步 | 由三次单位根求和得到 Z=b e^{iφ} | H1",
        "+ [新2] 待验证 | 数值核对理想模型幅值误差小于 1e-10 | H1", "```", "",
        "```验证计划", "标题：理想模型数值核对", "针对：[新1]", "方向：支持", "方法：仿真", "输入与参数：a=100, b=2",
        "退化自检：c=0 时与解析值一致", "判据：最大相对误差 < 1e-10", "预期：误差约 1e-14", "```", "",
        "结论摘要：完成第一步推导", "主要分歧：无", "建议的下一步：运行数值核对"])},
    "q_attack": {"text": "\n".join([
        "1. L01：站得住。", "", "```台账变更", "+ [新1] 待验证 | 检查 φ 接近 0 时的数值稳定性 | H1", "```"])},
    "q_reply": {"text": "\n".join([
        "回应：接受审查意见。", "", FENCE + "正文修订", "节：4 结果与分析", "方式：替换",
        "理想模型最大相对误差为 {{R01.max_err|.1e}}。", FENCE, "", "```台账变更", "无", "```", "",
        "结论摘要：理想模型推导完成，等待数值核对"])},
    "q_verify": {"text": "已按计划实现并运行。", "files": {
        "check.py": "# C04-Q01 P01 式(1)-(3)\nprint('ok')\n",
        "输出/summary.json": json.dumps({"selfcheck_passed": True, "criteria": [{"name": "最大相对误差", "predicted": 0, "observed": 3e-14, "threshold": 1e-10, "comparator": "<", "passed": True}],
                                         "independent_check": {"max_diff": 1e-15}, "parameters": {"a": 100, "b": 2}, "max_err": 3.0e-14}),
        "输出/对照表.md": "| 式号 | 文件:行号 | 说明 |\n|---|---|---|\n| (3) | check.py:2 | 直接计算 |\n",
        "运行命令.txt": "py -3.12 check.py\n"}},
    "drift": {"text": "仍在回答锚点里的问题吗：是\n用了锚点以外的假设吗：无\n判据或范围被改变了吗：无\n判定：未漂移"},
    "referee": {"text": "\n".join([
        "评分 7/10。", "", "```复推", "L02 | 一致 | 独立推导得到同一结果", "```", "", "```台账变更", "无", "```", "", "结论摘要：推导成立"])},
    "fw_propose": {"text": "\n".join(["```候选框架", "# 框架甲", "一句话主张：用模型驱动的方法提升重建精度", "## 第1章 绪论 | intro",
                                      "## 第2章 理论基础 | theory", "### 成像模型的可辨识条件", "## 第3章 方法一 | research", "## 第7章 总结与展望 | conclusion", "```"])},
    "fw_attack": {"text": "第3章缺口还没有量化（严重）。"},
    "fw_synthesize": {"text": "\n".join(["对照见下。", "", "```候选框架", "# 框架甲（修订）", "一句话主张：模型驱动的重建", "风险：实测数据不足",
                                         "## 第1章 绪论 | intro", "## 第2章 理论基础 | theory", "### 成像模型的可辨识条件", "问题能否唯一确定参数？",
                                         "## 第3章 研究问题一 | research", "## 第4章 研究问题二 | research", "## 第5章 研究问题三 | research",
                                         "## 第6章 系统验证 | framework", "## 第7章 总结与展望 | conclusion", "```", "",
                                         "```候选框架", "# 框架乙", "## 第1章 绪论 | intro", "## 第2章 理论 | theory", "```", "", "结论摘要：两个候选"])},
}


def wait(manager, sid, timeout=60):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        snap = manager.snapshot(sid)
        if snap["status"] in {"done", "stopped", "failed", "cancelled", "paused"}:
            return snap
        time.sleep(0.1)
    raise AssertionError(f"session did not finish: {manager.snapshot(sid)}")


class V2FlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="v2-flow-")
        self.root = Path(self.temp.name) / "研究 根目录"
        self.root.mkdir()
        self.replies = Path(self.temp.name) / "replies.json"
        self.replies.write_text(json.dumps(REPLIES, ensure_ascii=False), encoding="utf-8")
        self.calls = Path(self.temp.name) / "calls"
        env = {"RESEARCH_NOTEBOOK_FAKE_AGENTS": "1", "RESEARCH_NOTEBOOK_FAKE_REPLIES": str(self.replies)}
        self.env = patch.dict(os.environ, env)
        self.env.start()
        self.patches = [patch.object(agents, "call_root", lambda: self.calls / "calls"), patch.object(agents, "work_root", lambda: self.calls / "work"),
                        patch.object(agents, "codex_rate_limits", lambda *a, **k: None)]
        for p in self.patches:
            p.start()
        self.store = Store(self.root)
        self.v2 = V2(self.store)

    def tearDown(self):
        self.v2.close()
        for p in self.patches:
            p.stop()
        self.env.stop()
        self.temp.cleanup()

    def make_question(self):
        result = self.v2.act_create_framework({"title": "测试框架"})
        framework = next(f for f in result["tree"]["frameworks"] if f["id"] == result["id"])
        self.assertEqual([c["role"] for c in framework["chapters"]], ["intro", "theory", "research", "research", "research", "framework", "conclusion"])
        theory = framework["chapters"][1]["id"]
        made = self.v2.act_create_questions({"ref": theory, "items": [{"title": "三步相移 [1] 能否恢复幅值？", "question": "能否恢复？", "criterion": ""}]})
        return result["id"], theory, made["created"][0]

    def test_full_question_cycle(self):
        fw, theory, qid = self.make_question()
        item = self.v2.item(qid, fw)
        self.assertEqual(item["level"], "question")
        self.assertEqual(item["default_recipe"], "open")
        self.assertIn("## 2 理论推导", item["doc"]["text"])
        with self.assertRaises(ValueError):
            self.v2.act_start({"ref": qid, "fw": fw, "recipe": "advance"})
        snap = wait(self.v2.rounds, self.v2.act_start({"ref": qid, "fw": fw, "recipe": "open", "request_id": "open-1"})["id"])
        self.assertEqual(snap["status"], "stopped", snap)
        item = self.v2.item(qid, fw)
        self.assertEqual(item["anchor"]["status"], "draft")
        self.assertIn("精确恢复", item["anchor"]["slots"][0]["text"])
        self.assertEqual(item["anchor"]["assumptions"][0]["status"], "待批")
        self.assertIn("三步相移的幅值恢复", item["doc"]["text"])
        item = self.v2.act_confirm_anchor({"ref": qid, "fw": fw, "revision": item["anchor"]["revision"]})
        self.assertEqual(item["anchor"]["status"], "confirmed")
        self.assertEqual(item["anchor"]["assumptions"][0]["status"], "已确认")
        self.assertIn("H1", item["doc"]["text"].split("## 2 理论推导")[0])
        self.assertEqual(self.store._question(qid)["criterion"].strip(), "理想模型下幅值相对误差小于 1e-10。")

        snap = wait(self.v2.rounds, self.v2.act_start({"ref": qid, "fw": fw, "recipe": "advance", "rounds": 2})["id"])
        self.assertEqual(snap["status"], "stopped", snap)
        self.assertIn("没有变化", snap["stop_reason"])
        item = self.v2.item(qid, fw)
        entries = {e["id"]: e for e in item["ledger"]["entries"]}
        self.assertEqual(entries["L01"]["state"], "确定", entries)
        self.assertEqual(entries["L01"]["evidence"], "R01")
        self.assertEqual(entries["L02"]["state"], "待定")
        self.assertTrue(any(e["text"].startswith("检查 φ") for e in entries.values()))
        run = item["materials"]["runs"][0]
        self.assertEqual(run["outcome"], "pass")
        self.assertTrue(any(f["name"] == "summary.json" for f in run["files"]))
        self.assertTrue(any(c["name"] == "check.py" for c in item["materials"]["codes"]))
        doc = item["doc"]["text"]
        self.assertIn("对应台账 L01", doc)
        self.assertIn('[3.0e-14](结果/', doc)
        self.assertIn('"R01.max_err"', doc)
        self.assertEqual(len(item["rounds"]), 3)
        self.assertIn("## 验证运行", item["latest_brief"] + self.store._read(self.store._safe(item["rounds"][1]["brief_path"])))
        log = self.store._read(self.store._safe(Path(item["ledger"]["path"]).parent / "台账日志.md"))
        self.assertIn("与 L01 重复", log)

        snap = wait(self.v2.rounds, self.v2.act_start({"ref": qid, "fw": fw, "recipe": "referee"})["id"])
        self.assertEqual(snap["status"], "stopped", snap)
        item = self.v2.item(qid, fw)
        self.assertEqual(item["materials"]["verifications"][0]["entry"], "L02")
        entries = {e["id"]: e for e in item["ledger"]["entries"]}
        self.assertEqual(entries["L02"]["state"], "确定")
        self.assertTrue(entries["L02"]["evidence"].startswith("V"))

        self.assertTrue(item["can_adopt"])
        item = self.v2.act_adopt({"ref": qid, "fw": fw, "decision": "supported", "text": "理想模型下三步相移精确恢复幅值。", "scope": "仅限纯正弦与严格相移"})
        self.assertEqual(item["status"], "supported")
        self.assertIn("理想模型下三步相移精确恢复幅值", item["doc"]["text"].split("## 5")[1])

    def test_learning_mode_pause_and_reject(self):
        fw, theory, qid = self.make_question()
        wait(self.v2.rounds, self.v2.act_start({"ref": qid, "fw": fw, "recipe": "open"})["id"])
        item = self.v2.item(qid, fw)
        self.v2.act_confirm_anchor({"ref": qid, "fw": fw, "revision": item["anchor"]["revision"]})
        sid = self.v2.act_start({"ref": qid, "fw": fw, "recipe": "advance", "mode": "learn"})["id"]
        snap = wait(self.v2.rounds, sid)
        self.assertEqual(snap["status"], "paused")
        self.assertEqual(snap["learn"]["staged"][0]["section"], "2 理论推导")
        self.assertNotIn("对应台账", self.v2.item(qid, fw)["doc"]["text"])
        self.assertTrue(any(p["kind"] == "learn" for p in self.v2.item(qid, fw)["pending"]))
        with self.assertRaises(ValueError):
            self.v2.act_learn({"session_id": sid, "decision": "reject", "note": ""})
        self.v2.act_learn({"session_id": sid, "decision": "reject", "note": "式(2)的指数符号写反了"})
        snap = wait(self.v2.rounds, sid)
        self.assertEqual(snap["status"], "stopped")
        item = self.v2.item(qid, fw)
        self.assertEqual(item["ledger"]["entries"], [])
        self.assertNotIn("对应台账", item["doc"]["text"])
        self.assertIn("指数符号", json.dumps(rounds.Item(self.store, qid).state(), ensure_ascii=False))
        prompts = sorted(self.store._safe(item["rounds"][0]["dir"]).glob("*.提示词.md"))
        self.assertTrue(prompts)

    def test_learning_mode_continue_applies_step(self):
        fw, theory, qid = self.make_question()
        wait(self.v2.rounds, self.v2.act_start({"ref": qid, "fw": fw, "recipe": "open"})["id"])
        item = self.v2.item(qid, fw)
        self.v2.act_confirm_anchor({"ref": qid, "fw": fw, "revision": item["anchor"]["revision"]})
        sid = self.v2.act_start({"ref": qid, "fw": fw, "recipe": "advance", "mode": "learn", "rounds": 1})["id"]
        self.assertEqual(wait(self.v2.rounds, sid)["status"], "paused")
        self.v2.act_learn({"session_id": sid, "decision": "continue"})
        snap = wait(self.v2.rounds, sid)
        self.assertIn(snap["status"], {"done", "stopped"}, snap)
        self.assertIn("对应台账 L01", self.v2.item(qid, fw)["doc"]["text"])

    def test_only_one_session_and_cancel(self):
        fw, theory, qid = self.make_question()
        slow = dict(REPLIES)
        slow["open_question"] = {**REPLIES["open_question"], "sleep": 5}
        self.replies.write_text(json.dumps(slow, ensure_ascii=False), encoding="utf-8")
        sid = self.v2.act_start({"ref": qid, "fw": fw, "recipe": "open"})["id"]
        with self.assertRaises(Exception):
            self.v2.act_start({"ref": theory, "fw": fw, "recipe": "open"})
        self.v2.act_cancel({"session_id": sid})
        snap = wait(self.v2.rounds, sid, timeout=30)
        self.assertEqual(snap["status"], "cancelled", snap)

    def test_quota_failure_stops_without_retry(self):
        fw, theory, qid = self.make_question()
        failing = dict(REPLIES)
        failing["open_question"] = {"error": "quota"}
        self.replies.write_text(json.dumps(failing, ensure_ascii=False), encoding="utf-8")
        snap = wait(self.v2.rounds, self.v2.act_start({"ref": qid, "fw": fw, "recipe": "open"})["id"])
        self.assertEqual(snap["status"], "failed", snap)
        self.assertIn("不会自动重发", snap["stop_reason"])
        usage = (self.root / ".runtime" / "usage.jsonl").read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(usage), 1)

    def test_framework_layer_propose_and_create_candidate(self):
        item = self.v2.item("FW")
        self.assertEqual(item["default_recipe"], "open")
        anchor = L.empty_anchor("framework_layer")
        anchor.slots["question"], anchor.slots["criterion"] = "大问题", "主张成立的标准"
        rounds.Item(self.store, "FW").save_anchor(anchor)
        item = self.v2.item("FW")
        self.v2.act_confirm_anchor({"ref": "FW", "revision": item["anchor"]["revision"]})
        snap = wait(self.v2.rounds, self.v2.act_start({"ref": "FW", "recipe": "propose"})["id"])
        self.assertEqual(snap["status"], "stopped", snap)
        self.assertEqual(len([s for s in snap["steps"] if s["key"].startswith("propose")]), 2)
        families = {s["family"] for s in snap["steps"] if s["key"].startswith("propose")}
        self.assertEqual(len(families), 2, snap["steps"])
        item = self.v2.item("FW")
        self.assertEqual(len(item["candidates"]), 2)
        made = self.v2.act_create_from_candidate({"index": 0})
        framework = next(f for f in made["tree"]["frameworks"] if f["id"] == made["id"])
        self.assertEqual(len(framework["chapters"]), 7)
        self.assertEqual(framework["chapters"][1]["role"], "theory")
        self.assertEqual(framework["chapters"][1]["questions"][0]["title"], "成像模型的可辨识条件")

    def test_tree_and_upstream_flags(self):
        fw, theory, qid = self.make_question()
        tree = self.v2.tree()
        self.assertEqual(tree["frameworks"][0]["chapters"][1]["questions"][0]["id"], qid)
        research = tree["frameworks"][0]["chapters"][2]["id"]
        made = self.v2.act_create_questions({"ref": research, "items": [{"title": "下游问题"}]})
        down = made["created"][0]
        anchor, _ = rounds.Item(self.store, down).anchor()
        anchor.assumptions.append(L.Assumption(id="H1", text="理论成立", basis=f"{qid}#L01", failure="", status="已确认", source="你"))
        rounds.Item(self.store, down).save_anchor(anchor)
        flags = self.v2.item(down, fw)["upstream"]
        self.assertTrue(flags and "需复查" in flags[0]["message"])

    def confirmed_question(self):
        fw, theory, qid = self.make_question()
        wait(self.v2.rounds, self.v2.act_start({"ref": qid, "fw": fw, "recipe": "open"})["id"])
        item = self.v2.item(qid, fw)
        self.v2.act_confirm_anchor({"ref": qid, "fw": fw, "revision": item["anchor"]["revision"]})
        return fw, qid

    def set_replies(self, **changes):
        replies = dict(REPLIES)
        replies.update(changes)
        self.replies.write_text(json.dumps(replies, ensure_ascii=False), encoding="utf-8")

    def verify_with(self, summary):
        files = dict(REPLIES["q_verify"]["files"])
        files["输出/summary.json"] = json.dumps(summary)
        return {"text": "运行完成", "files": files}

    def test_run_judged_against_frozen_criterion(self):
        fw, qid = self.confirmed_question()
        loose = {"selfcheck_passed": True, "criteria": [{"name": "误差", "predicted": 0, "observed": 3e-3, "threshold": 1e-2, "comparator": "<", "passed": True}]}
        self.set_replies(q_verify=self.verify_with(loose))
        wait(self.v2.rounds, self.v2.act_start({"ref": qid, "fw": fw, "recipe": "advance", "rounds": 1})["id"])
        item = self.v2.item(qid, fw)
        self.assertEqual(item["materials"]["runs"][0]["outcome"], "error")
        self.assertNotEqual({e["id"]: e for e in item["ledger"]["entries"]}["L01"]["state"], "确定")

    def test_malformed_summaries_never_pass(self):
        frozen = [{"comparator": "<", "threshold": 1e-10}]
        base = Path(self.temp.name)
        cases = [
            {"selfcheck_passed": True, "criteria": ["误差 3e-14 < 1e-10 通过"]},
            {"selfcheck_passed": True, "criteria": [{"name": "误差", "observed": 3e-14, "threshold": 1e-10, "comparator": "<", "passed": "True"}]},
            {"criteria": [{"name": "误差", "observed": 3e-14, "threshold": 1e-10, "comparator": "<", "passed": True}]},
            {"selfcheck_passed": True, "criteria": [{"name": "误差", "observed": 3e-14, "threshold": 1e-10, "comparator": "≈", "passed": True}]},
            {"selfcheck_passed": True, "criterion_met": True},
        ]
        for index, summary in enumerate(cases):
            path = base / f"s{index}.json"
            path.write_text(json.dumps(summary, ensure_ascii=False), encoding="utf-8")
            self.assertEqual(rounds.judge_summary(path, frozen)[0], "error", summary)
        good = base / "good.json"
        good.write_text(json.dumps({"selfcheck_passed": True, "criteria": [{"name": "误差", "observed": 3e-14, "threshold": 1e-10, "comparator": "≤", "passed": True}]}), encoding="utf-8")
        self.assertEqual(rounds.judge_summary(good, [{"comparator": "<=", "threshold": 1e-10}])[0], "pass")
        self.assertEqual(rounds.parse_criteria("最大相对误差 < 1e-10，且 |Δ| ≤ 2×10^-3"),
                         [{"comparator": "<", "threshold": 1e-10}, {"comparator": "abs<=", "threshold": 2e-3}])

    def test_reopened_anchor_is_never_overwritten(self):
        fw, qid = self.confirmed_question()
        item = self.v2.item(qid, fw)
        anchor_before = item["anchor"]
        self.v2.act_save_anchor({"ref": qid, "fw": fw, "revision": anchor_before["revision"], "slots": {"scope": "b>0，a≥b，φ∈[0,2π)"}})
        item = self.v2.item(qid, fw)
        self.assertEqual(item["anchor"]["status"], "draft")
        self.assertNotEqual(item["default_recipe"], "open")
        wait(self.v2.rounds, self.v2.act_start({"ref": qid, "fw": fw, "recipe": "open"})["id"])
        item = self.v2.item(qid, fw)
        self.assertEqual(next(s["text"] for s in item["anchor"]["slots"] if s["key"] == "scope"), "b>0，a≥b，φ∈[0,2π)")
        self.assertTrue(any(p["kind"] == "anchor_draft" for p in item["pending"]))

    def test_fenced_drift_verdict_stops_and_asks(self):
        fw, qid = self.confirmed_question()
        self.set_replies(drift={"text": "```\n仍在回答锚点里的问题吗：否，换成了相位恢复\n用了锚点以外的假设吗：无。\n判据或范围被改变了吗：判据从 1e-10 放宽到 1e-6\n判定：漂移\n```"})
        snap = wait(self.v2.rounds, self.v2.act_start({"ref": qid, "fw": fw, "recipe": "advance", "rounds": 2})["id"])
        self.assertEqual(snap["round_index"], 1, "the second round must not start")
        item = self.v2.item(qid, fw)
        decisions = [p for p in item["pending"] if p["kind"] == "decision"]
        self.assertTrue(decisions and "偏离" in decisions[0]["text"])
        self.assertEqual([a for a in item["anchor"]["assumptions"] if a["status"] == "待批"], [], "'无。' is not an assumption")
        with self.assertRaises(ValueError):
            self.v2.act_start({"ref": qid, "fw": fw, "recipe": "advance"})

    def test_failed_engineer_call_stops_and_plan_is_retried(self):
        fw, qid = self.confirmed_question()
        self.set_replies(q_verify={"error": "quota"})
        snap = wait(self.v2.rounds, self.v2.act_start({"ref": qid, "fw": fw, "recipe": "advance", "rounds": 2})["id"])
        self.assertIn("工程调用没有完成", snap["stop_reason"])
        self.assertEqual(snap["round_index"], 1)
        self.set_replies()
        # Codex cools down after a quota error; once the quota is back the plan runs again.
        (self.root / ".runtime" / "usage.jsonl").unlink()
        wait(self.v2.rounds, self.v2.act_start({"ref": qid, "fw": fw, "recipe": "advance", "rounds": 1})["id"])
        outcomes = [r["outcome"] for r in self.v2.item(qid, fw)["materials"]["runs"]]
        self.assertEqual(outcomes, ["error", "pass"])

    def test_user_edit_during_round_is_kept(self):
        fw, qid = self.confirmed_question()
        original = agents.run_call

        def slow(spec, cancel=None):
            if "【模板】q_draft" in spec.prompt:
                item = self.v2.item(qid, fw)
                text = item["doc"]["text"].replace("## 2 理论推导\n", "## 2 理论推导\n\n我手写的推导。\n", 1)
                self.v2.act_save_doc({"ref": qid, "fw": fw, "text": text, "revision": item["doc"]["revision"]})
            return original(spec, cancel)

        with patch.object(agents, "run_call", slow):
            wait(self.v2.rounds, self.v2.act_start({"ref": qid, "fw": fw, "recipe": "advance", "rounds": 1})["id"])
        item = self.v2.item(qid, fw)
        self.assertIn("我手写的推导", item["doc"]["text"])
        self.assertIn("你在本轮期间改过这一节", item["latest_brief"])

    def test_engineer_touching_research_files_invalidates_run(self):
        fw, theory, qid = self.make_question()
        wait(self.v2.rounds, self.v2.act_start({"ref": qid, "fw": fw, "recipe": "open"})["id"])
        item = self.v2.item(qid, fw)
        self.v2.act_confirm_anchor({"ref": qid, "fw": fw, "revision": item["anchor"]["revision"]})
        # The fake CLI refuses to write outside its folder, so simulate a run that changed 台账.md.
        digests = iter([{"台账.md": "before"}, {"台账.md": "after"}])
        with patch.object(rounds.RoundManager, "_protected_digest", lambda self, item: next(digests)):
            wait(self.v2.rounds, self.v2.act_start({"ref": qid, "fw": fw, "recipe": "advance", "rounds": 1})["id"])
        item = self.v2.item(qid, fw)
        self.assertEqual(item["materials"]["runs"][0]["outcome"], "error")
        self.assertIn("受保护文件有变化", item["latest_brief"])

    def test_parse_candidate(self):
        parsed = parse_candidate("# 甲\n一句话主张：好\n## 第1章 绪论 | intro\n## 第2章 理论基础\n### Q1\n问题文本\n## 第3章 新方法 | research\n")
        self.assertEqual(parsed["title"], "甲")
        self.assertEqual([c["role"] for c in parsed["chapters"]], ["intro", "theory", "research"])
        self.assertEqual(parsed["chapters"][1]["questions"][0], {"title": "Q1", "question": "问题文本"})

    def test_judge_summary_rejects_inconsistent_passed(self):
        path = Path(self.temp.name) / "summary.json"
        path.write_text(json.dumps({"selfcheck_passed": True, "criteria": [{"name": "误差", "observed": 0.5, "threshold": 0.1, "comparator": "<", "passed": True}]}), encoding="utf-8")
        outcome, actual, _ = rounds.judge_summary(path)
        self.assertEqual(outcome, "error")
        self.assertIn("不符", actual)
        path.write_text(json.dumps({"selfcheck_passed": False, "criteria": [{"name": "误差", "observed": 0.01, "threshold": 0.1, "comparator": "<", "passed": True}]}), encoding="utf-8")
        self.assertEqual(rounds.judge_summary(path)[0], "fail")
        self.assertEqual(rounds.judge_summary(None)[0], "error")


class StoreBracketTitleTests(unittest.TestCase):
    def test_titles_with_brackets_keep_their_records(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(Path(temp))
            content = store.perform("save_content", {"title": "章 [a]"})
            q = store.perform("create_question", {"content_id": content["id"], "title": "step [1] check"})
            store.perform("save_discussion", {"question_id": q["id"], "ai": "GPT", "raw": "第一次"})
            second = store.perform("save_discussion", {"question_id": q["id"], "ai": "GPT", "raw": "第二次"})
            self.assertEqual(second["id"], "D02")
            self.assertEqual(len(store.detail(q["id"])["discussions"]), 2)

    def test_content_role_survives_edits(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(Path(temp))
            content = store.perform("save_content", {"title": "理论", "role": "theory"})
            store.perform("save_content", {"id": content["id"], "revision": store._content(content["id"])["revision"], "title": "理论基础"})
            self.assertEqual(store._content(content["id"])["role"], "theory")
            with self.assertRaises(ValueError):
                store.perform("save_content", {"title": "坏", "role": "nonsense"})


class V2GitTests(unittest.TestCase):
    def test_round_creates_commit_in_repo_root_only(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "repo"
            root.mkdir()
            run = lambda *args: subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True)
            run("init", "-b", "main", "-q")
            run("config", "user.name", "test")
            run("config", "user.email", "test@example.com")
            store = Store(root)
            api = V2(store)
            try:
                self.assertIsNotNone(api.repo)
                api.act_create_framework({"title": "框架"})
                if hasattr(api.repo, "close"):
                    api.repo.close()
                log = run("log", "--oneline").stdout
                self.assertIn("新建框架", log)
                nested = root / "示例研究"
                nested.mkdir()
                inner = V2(Store(nested))
                self.assertIsNone(inner.repo)
                inner.close()
            finally:
                api.close()


if __name__ == "__main__":
    unittest.main()
