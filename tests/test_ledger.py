"""Anchor, ledger, reply parsing and evidence rules (notebook/ledger.py)."""
import unittest

from notebook import ledger as L

SRC, FAM = "GPT·第1轮", "openai"


def ops(text):
    return L.parse_reply("```台账变更\n" + text + "\n```").ops


def apply(led, anchor, text, family=FAM, source=SRC, evidence=None):
    return L.apply_ops(led, anchor, ops(text), source=source, family=family, round_label="第1轮", step="draft", evidence=evidence or {})


def question_anchor():
    anchor = L.empty_anchor("question")
    anchor.slots.update(question="能否恢复？", criterion="误差 < 1e-10")
    anchor.assumptions.append(L.Assumption("H1", "相移严格为 2πk/3", "标定", "相移误差 > 0.01 rad", "已确认", "你"))
    anchor.status = "confirmed"
    return anchor


class SplitAndTables(unittest.TestCase):
    def test_split_respects_math_code_and_escapes(self):
        self.assertEqual(L.split_fields("推导步 | 由 $|Z|=b$ 得 | H1"), ["推导步", "由 $|Z|=b$ 得", "H1"])
        self.assertEqual(L.split_fields("论断 | `a|b` 成立 | 无"), ["论断", "`a|b` 成立", "无"])
        self.assertEqual(L.split_fields("a \\| b | c"), ["a | b", "c"])
        self.assertEqual(L.split_fields("$$x|y$$ | z"), ["$$x|y$$", "z"])
        self.assertEqual(L.split_fields("价格 $5 | 便宜 | x"), ["价格 $5", "便宜", "x"])

    def test_anchor_round_trip(self):
        anchor = question_anchor()
        anchor.assumptions.append(L.Assumption("H2", "多行\n内容 $|x|<1$", "", "", "待批", "Gemini·第2轮"))
        anchor.requests.append(L.ChangeRequest("Q1", "判据", "误差 < 1e-8", "太严", "GPT·第3轮"))
        anchor.extra = "## 备注\n\n自由文本"
        text = L.render_anchor(anchor)
        again = L.parse_anchor(text, "question")
        self.assertEqual(again.slots, anchor.slots)
        self.assertEqual(again.assumptions, anchor.assumptions)
        self.assertEqual(again.requests, anchor.requests)
        self.assertEqual(again.status, "confirmed")
        self.assertEqual(L.render_anchor(again), text)

    def test_anchor_tolerant_headings_and_bullets(self):
        text = "## 本章回答的问题\n\nQ\n\n## 判据与评测协议\n\nC\n\n## 依赖与假设\n\n- H1：A（依据：第2章）（失效条件：噪声大）\n- 第二条\n\n## 约束\n\nS\n\n## 不做什么\n\nN\n"
        anchor = L.parse_anchor(text, "chapter")
        self.assertEqual(anchor.slots, {"question": "Q", "criterion": "C", "scope": "S", "nongoals": "N"})
        self.assertEqual([(a.id, a.text, a.basis, a.failure) for a in anchor.assumptions], [("H1", "A", "第2章", "噪声大"), ("H2", "第二条", "", "")])

    def test_ledger_round_trip_and_meta(self):
        led = L.empty_ledger()
        apply(led, question_anchor(), "+ [新1] 推导步 | 由 $|Z|$ 得\n结论 | H1")
        text = L.render_ledger(led)
        again = L.parse_ledger(text)
        self.assertEqual(L.render_ledger(again), text)
        self.assertEqual(again.entries[0].family, "openai")
        self.assertEqual(again.next, 2)

    def test_hand_edited_settled_entry_returns_to_pending(self):
        led = L.empty_ledger()
        apply(led, question_anchor(), "+ 论断 | 原文 | H1")
        L.user_set_state(led, "L01", "确定")
        led.entries[0].evidence = "R01"
        text = L.render_ledger(led).replace("| 原文 |", "| 改过的文字 |")
        again = L.parse_ledger(text)
        self.assertEqual(again.entries[0].state, "待定")
        self.assertIn("不再适用", again.entries[0].note)


class ReplyParsing(unittest.TestCase):
    def test_blocks_and_nested_fences(self):
        reply = L.parse_reply("说明\n\n````正文修订\n节：2.3 反例\n方式：替换\n代码：\n```python\nprint(1)\n```\n结束\n````\n\n"
                              "```验证计划\n标题：核对\n针对：[新1]\n方向：否定\n方法：仿真\n输入与参数：a=1\nb=2\n判据：误差 > 0.1\n```\n\n"
                              "```小问题清单\n### 第一个问题\n问题：能否？\n判据：误差<1\n承重：是\n### 第二个\n验证方式：推导\n```\n\n"
                              "```候选框架\n# 甲\n## 第1章 绪论 | intro\n```\n\n```复推\nL03 | 独立复推一致 | 好\nL04 | 不一致 | 第2步错\nX | 一致\n```\n"
                              "结论摘要：完成\n主要分歧：无\n")
        self.assertEqual(len(reply.revisions), 1)
        self.assertIn("```python", reply.revisions[0].body)
        self.assertEqual(reply.revisions[0].section, "2.3 反例")
        self.assertEqual(reply.plans[0]["输入与参数"], "a=1\nb=2")
        self.assertEqual(reply.plans[0]["方向"], "否定")
        self.assertEqual([q["title"] for q in reply.questions], ["第一个问题", "第二个"])
        self.assertTrue(reply.questions[0]["load_bearing"])
        self.assertEqual(reply.questions[1]["method"], "推导")
        self.assertEqual(reply.candidates, ["# 甲\n## 第1章 绪论 | intro"])
        self.assertEqual([(v["entry"], v["verdict"]) for v in reply.verifications], [("L03", "一致"), ("L04", "不一致")])
        self.assertTrue(reply.errors)
        self.assertEqual(reply.summary["结论摘要"], "完成")

    def test_revision_without_mode_is_an_error(self):
        reply = L.parse_reply("````正文修订\n节：2\n内容\n````")
        self.assertEqual(reply.revisions, [])
        self.assertTrue(reply.errors)

    def test_unfenced_ledger_fallback_and_drift(self):
        reply = L.parse_reply("**台账变更：**\n+ 论断 | 甲 | 无\n\n后文\n\n仍在回答锚点里的问题吗：是\n判定：轻微漂移")
        self.assertEqual(len(reply.ops), 1)
        self.assertEqual(reply.drift["verdict"], "轻微")
        self.assertEqual(L.parse_reply("判定：漂移（需要人决定）").drift["verdict"], "漂移")
        self.assertEqual(L.parse_reply("判定：未漂移").drift["verdict"], "未漂移")
        self.assertIsNone(L.parse_reply("没有漂移检查").drift)


class Rules(unittest.TestCase):
    def setUp(self):
        self.led, self.anchor = L.empty_ledger(), question_anchor()

    def test_add_is_always_pending_and_maps_temp(self):
        res = apply(self.led, self.anchor, "+ [新1] 推导步 | 甲 | H1 | 确定\n+ [新2] 待验证 | 核对甲 | [新1]")
        self.assertEqual([r.status for r in res], ["已执行", "已执行"])
        self.assertEqual([e.state for e in self.led.entries], ["待定", "待定"])
        self.assertEqual(self.led.last_temp_map, {"[新1]": "L01", "[新2]": "L02"})
        self.assertIn("L01", self.led.entries[1].basis)

    def test_bad_type_empty_and_duplicates(self):
        apply(self.led, self.anchor, "+ 论断 | 甲 | H1")
        res = apply(self.led, self.anchor, "+ 猜想 | 乙\n+ 论断 |  \n+ [新1] 论断 | 甲。 | H1")
        self.assertEqual([r.status for r in res], ["已拒绝"] * 3)
        self.assertIn("与 L01 重复", res[2].reason)
        self.assertEqual(self.led.last_temp_map, {"[新1]": "L01"})

    def test_rejected_list_blocks_rephrasing_exact_text(self):
        apply(self.led, self.anchor, "+ 想法 | 用深度网络 | 无")
        L.user_set_state(self.led, "L01", "否定", "不适合")
        res = apply(self.led, self.anchor, "+ 想法 | 用深度网络")
        self.assertIn("淘汰清单", res[0].reason)
        self.assertEqual([e.id for e in L.rejected_list(self.led)], ["L01"])

    def test_assumption_add_goes_to_anchor(self):
        res = apply(self.led, self.anchor, "+ [新1] 假设 | 噪声可忽略（失效条件：SNR<20） | 实测")
        self.assertEqual(res[0].status, "已执行")
        self.assertEqual(self.anchor.assumptions[-1].status, "待批")
        self.assertEqual(self.anchor.assumptions[-1].failure, "SNR<20")
        self.assertEqual(self.led.entries, [])
        self.assertEqual(self.led.last_temp_map["[新1]"], "H2")

    def test_basis_checks(self):
        self.anchor.assumptions.append(L.Assumption("H2", "待批的", status="待批"))
        apply(self.led, self.anchor, "+ 论断 | 甲 | H2,H9,L07")
        note = self.led.entries[0].note
        self.assertIn("依赖待批假设 H2", note)
        self.assertIn("依据 H9 不存在", note)
        self.assertIn("依据 L07 不存在", note)

    def test_confirm_needs_bound_passing_run(self):
        apply(self.led, self.anchor, "+ 论断 | 甲 | H1")
        sha = L.entry_sha(self.led.entries[0])
        cases = {
            "R01": {"outcome": "pass", "direction": "支持", "targets": {}},
            "R02": {"outcome": "fail", "direction": "支持", "targets": {"L01": sha}},
            "R03": {"outcome": "pass", "direction": "支持", "targets": {"L01": "旧哈希"}},
            "R04": {"outcome": "pass", "direction": "否定", "targets": {"L01": sha}},
        }
        evidence = {"runs": cases}
        for rid, why in (("R01", "没有针对"), ("R02", "没有通过"), ("R03", "改动过"), ("R04", "方向")):
            res = apply(self.led, self.anchor, f"~ L01 | 确定 | 理由 | {rid}", evidence=evidence)
            self.assertEqual(res[0].status, "记为意见", rid)
            self.assertIn(why, res[0].reason)
        self.assertEqual(self.led.entries[0].state, "待定")
        evidence["runs"]["R05"] = {"outcome": "pass", "direction": "支持", "targets": {"L01": sha}}
        res = apply(self.led, self.anchor, "~ L01 | 确定 | 理由 | R05", evidence=evidence)
        self.assertEqual((res[0].status, self.led.entries[0].state, self.led.entries[0].evidence), ("已执行", "确定", "R05"))

    def test_verification_must_come_from_another_family(self):
        apply(self.led, self.anchor, "+ 论断 | 甲 | H1")
        sha = L.entry_sha(self.led.entries[0])
        evidence = {"verifications": {"V01": {"entry": "L01", "verdict": "一致", "family": "openai", "entry_sha": sha},
                                      "V02": {"entry": "L01", "verdict": "一致", "family": "anthropic", "entry_sha": sha}}}
        self.assertEqual(apply(self.led, self.anchor, "~ L01 | 确定 | x | V01", evidence=evidence)[0].status, "记为意见")
        self.assertEqual(apply(self.led, self.anchor, "~ L01 | 确定 | x | V02", evidence=evidence)[0].status, "已执行")

    def test_two_families_agreeing_is_still_pending(self):
        apply(self.led, self.anchor, "+ 论断 | 甲 | H1")
        apply(self.led, self.anchor, "~ L01 | 确定 | 我也同意 | ", family="google", source="Gemini·第1轮")
        entry = self.led.entries[0]
        self.assertEqual(entry.state, "待定")
        self.assertIn("已获 GPT、Gemini 同意，缺证据", entry.note)

    def test_human_evidence_refused(self):
        apply(self.led, self.anchor, "+ 论断 | 甲 | H1")
        res = apply(self.led, self.anchor, "~ L01 | 确定 | 用户已确认 | 人工")
        self.assertEqual(res[0].status, "已拒绝")
        self.assertIn("只有你本人", res[0].reason)

    def test_counterexample_marks_pending_and_adds_check(self):
        apply(self.led, self.anchor, "+ 论断 | 甲 | H1")
        L.user_set_state(self.led, "L01", "确定")
        res = apply(self.led, self.anchor, "~ L01 | 否定 | 不成立 | 反例：b=2, c=0.2 时误差 10%", family="google", source="Gemini·第1轮")
        self.assertEqual(res[0].status, "已执行")
        self.assertTrue(res[0].blocking)
        self.assertEqual(self.led.entries[0].state, "确定")
        self.assertIn("有反例待核", self.led.entries[0].note)
        self.assertEqual((self.led.entries[1].type, self.led.entries[1].basis), ("待验证", "L01"))
        again = apply(self.led, self.anchor, "~ L01 | 否定 | 不成立 | 反例：b=2, c=0.2 时误差 10%", family="google", source="Gemini·第1轮")
        self.assertEqual(len(self.led.entries), 2, "the check entry is reused, not duplicated")
        self.assertTrue(again[0].blocking)

    def test_leaving_confirmed_needs_new_evidence(self):
        apply(self.led, self.anchor, "+ 论断 | 甲 | H1")
        sha = L.entry_sha(self.led.entries[0])
        evidence = {"runs": {"R01": {"outcome": "pass", "direction": "支持", "targets": {"L01": sha}},
                             "R02": {"outcome": "pass", "direction": "否定", "targets": {"L01": sha}}}}
        apply(self.led, self.anchor, "~ L01 | 确定 | x | R01", evidence=evidence)
        self.assertEqual(apply(self.led, self.anchor, "~ L01 | 否定 | 我觉得不对", evidence=evidence)[0].status, "记为意见")
        self.assertEqual(apply(self.led, self.anchor, "~ L01 | 否定 | x | R01", evidence=evidence)[0].status, "记为意见")
        res = apply(self.led, self.anchor, "~ L01 | 否定 | x | R02", evidence=evidence)
        self.assertEqual((res[0].status, res[0].blocking), ("记为意见", True))
        self.assertEqual(self.led.entries[0].state, "确定", "contradicting evidence goes to the user")
        self.assertIn("矛盾", self.led.entries[0].note)

    def test_replace_pending_and_confirmed(self):
        apply(self.led, self.anchor, "+ 论断 | 旧 | H1\n+ 论断 | 已定 | H1")
        L.user_set_state(self.led, "L02", "确定")
        apply(self.led, self.anchor, "+ 论断 | 新 | H1 | 替代 L01\n+ 论断 | 更新的已定 | H1 | 替代 L02")
        self.assertEqual((self.led.entries[0].state, self.led.entries[0].superseded_by), ("否定", "L03"))
        self.assertEqual(self.led.entries[1].state, "确定")
        self.assertIn("拟替代 L02", self.led.entries[3].note)
        self.assertEqual(L.rejected_list(self.led), [])
        self.assertEqual(apply(self.led, self.anchor, "~ L01 | 确定 | x | R01")[0].status, "已拒绝")

    def test_requests_and_decisions_block(self):
        res = apply(self.led, self.anchor, "? 判据 | 误差 < 1e-8 | 太严\n? H1 | 相移误差小于 0.01 | 实测\n? 假设 | 新的前提 | 依据\n? H7 | x | y\n! 两条假设冲突")
        self.assertEqual([r.status for r in res], ["已执行", "已执行", "已执行", "已拒绝", "已执行"])
        self.assertEqual([r.blocking for r in res], [True, True, False, False, True])
        self.assertEqual([r.id for r in L.anchor_blocking(self.anchor)], ["Q1", "Q2"])
        self.assertEqual(self.anchor.requests[0].slot, "判据")
        self.assertEqual([a.id for a in L.pending_assumptions(self.anchor)], ["H2"])
        self.assertEqual(apply(self.led, self.anchor, "? 判据 | 误差 < 1e-8 | 再提")[0].status, "已拒绝")

    def test_user_decisions(self):
        apply(self.led, self.anchor, "? 判据 | 误差 < 1e-8 | 太严\n? H1 | 相移误差 < 0.02（失效条件：温漂） | 实测\n? 假设 | 新前提 | 依据")
        version = self.anchor.version
        L.user_decide_request(self.anchor, "Q1", True, "同意")
        self.assertEqual((self.anchor.slots["criterion"], self.anchor.status, self.anchor.version), ("误差 < 1e-8", "draft", version + 1))
        L.user_decide_request(self.anchor, "Q2", True)
        self.assertEqual((self.anchor.assumptions[0].text, self.anchor.assumptions[0].failure), ("相移误差 < 0.02", "温漂"))
        L.user_decide_assumption(self.anchor, "H2", False, "不需要")
        self.assertEqual(self.anchor.assumptions[1].status, "已拒绝")
        with self.assertRaises(ValueError):
            L.user_decide_request(self.anchor, "Q1", True)

    def test_apply_run(self):
        apply(self.led, self.anchor, "+ 论断 | 甲 | H1\n+ 想法 | 乙 | 无")
        sha1, sha2 = (L.entry_sha(e) for e in self.led.entries)
        L.apply_run(self.led, run_id="R01", outcome="pass", direction="支持", targets={"L01": sha1}, round_label="第2轮")
        L.apply_run(self.led, run_id="R02", outcome="fail", direction="否定", targets={"L02": sha2}, round_label="第2轮")
        self.assertEqual((self.led.entries[0].state, self.led.entries[0].evidence), ("确定", "R01"))
        self.assertEqual(self.led.entries[1].state, "待定")
        self.assertIn("杀测未触发", self.led.entries[1].note)
        L.apply_run(self.led, run_id="R03", outcome="pass", direction="否定", targets={"L02": sha2}, round_label="第3轮")
        self.assertEqual(self.led.entries[1].state, "否定")
        res = L.apply_run(self.led, run_id="R04", outcome="pass", direction="支持", targets={"L01": "旧"}, round_label="第3轮")
        self.assertEqual(res[0].status, "记为意见")

    def test_unknown_lines_are_logged_not_dropped(self):
        res = apply(self.led, self.anchor, "这行不是变更")
        self.assertEqual(res[0].status, "已拒绝")
        log = L.append_log("# 台账日志\n", "第1轮", res)
        self.assertIn("这行不是变更", log)


class Loopholes(unittest.TestCase):
    """Each case reproduces a way the review found to get a state without real evidence."""

    def setUp(self):
        self.led, self.anchor = L.empty_ledger(), question_anchor()

    def confirm_by_run(self, eid, rid="R01"):
        sha = L.entry_sha(L.find(self.led, eid))
        return L.apply_run(self.led, run_id=rid, outcome="pass", direction="支持", targets={eid: sha}, round_label="第1轮",
                           anchor=self.anchor, created_at="2026-01-01T00:00:00+00:00")

    def test_referee_cannot_overturn_run_or_user_decisions(self):
        apply(self.led, self.anchor, "+ 论断 | 甲 | H1\n+ 论断 | 乙 | H1")
        self.confirm_by_run("L01")
        L.user_set_state(self.led, "L02", "确定")
        shas = {e.id: L.entry_sha(e) for e in self.led.entries}
        evidence = {"verifications": {f"V0{i}": {"entry": eid, "verdict": "不一致", "family": "anthropic", "entry_sha": shas[eid], "created_at": "2026-02-01"}
                                      for i, eid in ((1, "L01"), (2, "L02"))}}
        res = apply(self.led, self.anchor, "~ L01 | 否定 | 推不出 | V01\n~ L02 | 否定 | 二阶项 | V02", family="anthropic", source="Claude·第2轮", evidence=evidence)
        self.assertEqual([e.state for e in self.led.entries], ["确定", "确定"])
        self.assertTrue(all(r.blocking for r in res[:2]))
        self.assertEqual(L.rejected_list(self.led), [])

    def test_counterexample_cannot_revive_refuted_entries(self):
        apply(self.led, self.anchor, "+ 想法 | 用深度网络回归相位 | 无")
        L.user_set_state(self.led, "L01", "否定", "不适合")
        res = apply(self.led, self.anchor, "~ L01 | 待定 | 换个角度 | 反例：数据足够多时可行")
        self.assertEqual((res[0].status, self.led.entries[0].state), ("记为意见", "否定"))

    def test_evidence_older_than_counterexample_is_refused(self):
        apply(self.led, self.anchor, "+ 论断 | 甲 | H1")
        sha = L.entry_sha(self.led.entries[0])
        apply(self.led, self.anchor, "~ L01 | 待定 | 不成立 | 反例：φ=0 时误差为 1", family="google", source="Gemini·第1轮")
        evidence = {"runs": {"R01": {"outcome": "pass", "direction": "支持", "targets": {"L01": sha}, "created_at": "2000-01-01T00:00:00+00:00"},
                             "R02": {"outcome": "pass", "direction": "支持", "targets": {"L01": sha}, "created_at": "2999-01-01T00:00:00+00:00"}}}
        res = apply(self.led, self.anchor, "~ L01 | 确定 | 反例不成立 | R01", evidence=evidence)
        self.assertEqual(res[0].status, "记为意见")
        self.assertIn("早于", res[0].reason)
        self.assertEqual(apply(self.led, self.anchor, "~ L01 | 确定 | 新运行 | R02", evidence=evidence)[0].status, "已执行")

    def test_pending_assumption_blocks_confirmation_until_approved(self):
        apply(self.led, self.anchor, "? 假设 | 噪声为零均值高斯白噪声 | 依据：实测\n+ 推导步 | 在 H2 下估计量无偏 | H2")
        res = self.confirm_by_run("L01")
        self.assertEqual(self.led.entries[0].state, "待定")
        self.assertIn("H2 还在待批", res[0].reason)
        L.user_decide_assumption(self.anchor, "H2", True)
        promoted = L.recheck_dependents(self.led, self.anchor)
        self.assertEqual((self.led.entries[0].state, self.led.entries[0].evidence), ("确定", "R01"))
        self.assertTrue(promoted)

    def test_rejecting_an_assumption_demotes_dependents(self):
        self.anchor.assumptions.append(L.Assumption("H2", "噪声可忽略", status="已确认"))
        apply(self.led, self.anchor, "+ 推导步 | 忽略噪声得到闭式解 | H2\n+ 论断 | 闭式解唯一 | L01")
        self.confirm_by_run("L01")
        self.confirm_by_run("L02", "R02")
        self.assertEqual([e.state for e in self.led.entries], ["确定", "确定"])
        self.anchor.assumptions[-1].status = "已拒绝"
        L.recheck_dependents(self.led, self.anchor)
        self.assertEqual([e.state for e in self.led.entries], ["待定", "待定"], "the dependency chain follows")

    def test_entry_waits_for_the_entries_it_rests_on(self):
        apply(self.led, self.anchor, "+ 推导步 | 第一步 | H1\n+ 推导步 | 第二步 | L01")
        self.confirm_by_run("L02", "R02")
        self.assertEqual(self.led.entries[1].state, "待定")
        self.confirm_by_run("L01", "R01")
        self.assertEqual([e.state for e in self.led.entries], ["确定", "确定"])

    def test_verification_must_match_wording_and_be_independent(self):
        apply(self.led, self.anchor, "+ 论断 | 误差上界为 O(ε²) | H1")
        old_sha = L.entry_sha(self.led.entries[0])
        self.led.entries[0].text = "误差上界为 O(ε)，且对任意相移误差成立"
        stale = {"verifications": {"V01": {"entry": "L01", "verdict": "一致", "family": "anthropic", "entry_sha": old_sha}}}
        self.assertEqual(apply(self.led, self.anchor, "~ L01 | 确定 | 仍然有效 | V01", evidence=stale)[0].status, "记为意见")
        apply(self.led, self.anchor, "~ L01 | 确定 | 我同意 |", family="google", source="Gemini·第1轮")
        same = {"verifications": {"V02": {"entry": "L01", "verdict": "一致", "family": "google", "entry_sha": L.entry_sha(self.led.entries[0])}}}
        res = apply(self.led, self.anchor, "~ L01 | 确定 | 复推一致 | V02", family="google", evidence=same)
        self.assertEqual(res[0].status, "记为意见")
        self.assertIn("不算独立", res[0].reason)

    def test_near_copies_of_refuted_ideas_and_replacing_refuted_entries(self):
        apply(self.led, self.anchor, "+ 想法 | 用深度网络直接回归相位 | 无")
        L.user_set_state(self.led, "L01", "否定")
        res = apply(self.led, self.anchor, "+ 想法 | 用深度神经网络直接回归相位\n+ 想法 | 新的方向 | 无 | 替代 L01")
        self.assertEqual([r.status for r in res], ["已拒绝", "已拒绝"])
        self.assertEqual(len(self.led.entries), 1)

    def test_rejected_assumption_cannot_be_revived_by_request(self):
        self.anchor.assumptions.append(L.Assumption("H2", "噪声可忽略", status="已拒绝"))
        self.assertEqual(apply(self.led, self.anchor, "? H2 | 噪声很小可忽略 | 实测")[0].status, "已拒绝")

    def test_drift_parsing_fails_closed(self):
        self.assertEqual(L.parse_reply("判定：未漂移 / 轻微 / 漂移\n判定：漂移").drift["verdict"], "漂移")
        self.assertEqual(L.parse_reply("判定：未漂移 / 轻微 / 漂移").drift["verdict"], "")
        self.assertEqual(L.parse_reply("### 最终判定：漂移").drift["verdict"], "漂移")
        self.assertEqual(L.parse_reply("判定：轻微漂移，可以继续").drift["verdict"], "轻微")
        self.assertEqual(L.parse_reply("结论：漂移").drift, None)
        for answer in ("无", "否。", "未发现锚点以外的假设", "无 / 列出", "没有"):
            self.assertTrue(L.is_none_answer(answer), answer)
        self.assertFalse(L.is_none_answer("噪声为高斯白噪声（式 3）"))


class ReviewRegressions(unittest.TestCase):
    """Parsing and document problems reproduced by the v2 review."""

    def test_wrapper_fence_with_prose_around(self):
        text = "下面是本轮回复：\n```markdown\n说明\n````正文修订\n节：2\n方式：替换\n内容\n````\n```台账变更\n+ 论断 | 甲 | 无\n```\n```\n以上是本轮回复。"
        reply = L.parse_reply(text)
        self.assertEqual((len(reply.revisions), len(reply.ops)), (1, 1))

    def test_two_plans_in_one_block_and_mixed_direction(self):
        reply = L.parse_reply("```验证计划\n标题：甲\n针对：L01\n方向：支持\n判据：e<1\n---\n标题：乙\n针对：L02\n方向：否定\n判据：e>1\n```")
        self.assertEqual([(p["标题"], p["方向"]) for p in reply.plans], [("甲", "支持"), ("乙", "否定")])
        reply = L.parse_reply("```验证计划\n标题：甲\n针对：L01\n判据：e<1\n标题：乙\n针对：L02\n判据：e<2\n```")
        self.assertEqual(len(reply.plans), 2)
        reply = L.parse_reply("```验证计划\n标题：丙\n针对：L01\n方向：支持或否定\n判据：e<1\n```")
        self.assertEqual(reply.plans, [])
        self.assertTrue(reply.errors)

    def test_drift_answers_in_fences_tables_and_next_line(self):
        self.assertEqual(L.parse_reply("```\n仍在回答锚点里的问题吗：否\n判定：漂移\n```").drift["verdict"], "漂移")
        self.assertEqual(L.parse_reply("> 判定：漂移").drift["verdict"], "漂移")
        self.assertEqual(L.parse_reply("| 判定 | 漂移 |").drift["verdict"], "漂移")
        self.assertEqual(L.parse_reply("**判定：**\n漂移").drift["verdict"], "漂移")

    def test_revision_cannot_inject_protected_or_sibling_sections(self):
        doc = L.skeleton("question")
        protected = {"1": "第 1 节由锚点生成", "5": "__stage__"}
        new, res = L.apply_revisions(doc, [L.Revision("4 结果与分析", "替换", "结果\n\n## 5 结论与适用范围\n\nAI 的结论")], protected=protected)
        self.assertEqual(res[0]["status"], "已拒绝")
        self.assertEqual(new, doc)
        new, res = L.apply_revisions(doc, [L.Revision("2 理论推导", "替换", "推导\n\n## 3 验证方法\n\n改了别的节")], protected=protected)
        self.assertEqual(res[0]["status"], "已拒绝")
        new, res = L.apply_revisions(doc, [L.Revision("2 理论推导", "替换", "推导\n\n### 2.1 第一步\n\n子节")], protected=protected)
        self.assertEqual(res[0]["status"], "已应用")

    def test_set_section_removes_duplicates(self):
        doc = "## 4 结果\n\nx\n\n## 5 结论与适用范围\n\nAI 写的\n\n## 5 结论与适用范围\n\n空\n"
        new = L.set_section(doc, "5", "结论与适用范围", "用户的结论")
        self.assertEqual(new.count("## 5 "), 1)
        self.assertIn("用户的结论", new)
        self.assertNotIn("AI 写的", new)


class Documents(unittest.TestCase):
    def test_revisions(self):
        doc = L.skeleton("question")
        new, res = L.apply_revisions(doc, [
            L.Revision("2 理论推导", "替换", "## 2 理论推导\n\n推导 A"),
            L.Revision("2.1 第一步", "追加", "子节"),
            L.Revision("理论推导", "追加", "补充 B"),
            L.Revision("5 结论与适用范围", "替换", "AI 的结论"),
            L.Revision("1", "替换", "改锚点"),
            L.Revision("9.3 远处", "替换", "末尾"),
        ], protected={"1": "第 1 节由锚点生成", "5": "__stage__"})
        self.assertEqual([r["status"] for r in res], ["已应用", "已应用", "已应用", "待你确认", "已拒绝", "已应用"])
        self.assertEqual(new.count("## 2 理论推导"), 1)
        self.assertLess(new.index("推导 A"), new.index("### 2.1 第一步"))
        self.assertLess(new.index("补充 B"), new.index("## 3 验证方法"))
        self.assertNotIn("AI 的结论", new)
        self.assertTrue(new.rstrip().endswith("末尾"))

    def test_set_section_creates_in_order(self):
        doc = "## 摘要\n\n## 2 理论推导\n\nX\n"
        new = L.set_section(doc, "1", "问题与假设", "Q")
        self.assertLess(new.index("## 1 问题与假设"), new.index("## 2 理论推导"))
        self.assertEqual(L.set_section(new, "1", "问题与假设", "R").count("## 1 问题与假设"), 1)

    def test_skeleton_roles(self):
        self.assertIn("## 5 主要创新点", L.skeleton("chapter", "intro"))
        self.assertIn("误差模型", L.skeleton("chapter", "theory"))
        self.assertEqual(L.skeleton("chapter", "framework"), L.skeleton("chapter", "research"))
        self.assertEqual(L.skeleton("framework_layer"), "")

    def test_placeholders(self):
        lookup = lambda rid, key: {("R01", "max_err"): (3e-14, "结果/R01/summary.json"), ("R01", "a.b"): (True, "s.json"), ("R01", "n"): (7, "s.json")}.get((rid, key))
        text, missing = L.resolve_placeholders("误差 {{R01.max_err|.1e}}，{{ R01.a.b }}，{{R01.n}}，{{R09.x}}\n```\n{{R01.n}}\n```", lookup)
        self.assertIn('[3.0e-14](结果/R01/summary.json "R01.max_err")', text)
        self.assertIn('[是](s.json "R01.a.b")', text)
        self.assertIn('[7](s.json "R01.n")', text)
        self.assertEqual(missing, ["R09.x"])
        self.assertIn("```\n{{R01.n}}\n```", text)
        checks = L.check_result_links(text, lookup)
        self.assertTrue(all(c["ok"] for c in checks))


if __name__ == "__main__":
    unittest.main()
