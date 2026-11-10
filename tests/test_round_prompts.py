"""Prompt templates for v2 rounds (v2 spec, section C; reply grammar B.3)."""
import re
import unittest

from notebook import round_prompts as P

KEYS = {
    "open_framework_layer", "fw_propose", "fw_attack", "fw_synthesize", "fw_refine",
    "open_chapter", "ch_outline", "ch_attack",
    "open_question", "q_draft", "q_attack", "q_reply", "q_verify", "explain", "drift", "referee",
    "innov_s1", "innov_s2", "innov_s3", "innov_s4", "innov_s5", "innov_s6",
    "intro_story", "intro_final", "conclusion", "assemble",
}
ROLES = {"author", "reviewer", "engineer", "writer", "drift", "referee"}
NOT_AUTHOR = {"q_attack": "reviewer", "ch_attack": "reviewer", "fw_attack": "reviewer", "q_verify": "engineer",
              "drift": "drift", "referee": "referee", "fw_synthesize": "referee", "innov_s2": "writer",
              "intro_story": "writer", "intro_final": "writer", "conclusion": "writer", "assemble": "writer"}
NO_REPLY_FORMAT = {"q_verify", "drift"}
SPEC_MARKERS = {"project", "anchor", "ledger", "rejected", "doc", "level", "chapter", "upstream", "focus", "round",
                "rejected_reason", "draft_blocks", "attack", "plan", "code_files", "results", "frameworks",
                "proposals", "attacks", "stage", "ideas", "staged_step"}
BLOCK_NAMES = {"正文修订", "台账变更", "验证计划", "小问题清单", "候选框架", "复推", "锚点草稿"}
LEFTOVER = re.compile(r"<<[^<>\n]*>>")
FENCE_OPEN = re.compile(r"^(`{3,}|~{3,})([^\s`~]\S*)\s*$")
FENCE_CLOSE = re.compile(r"^(`{3,}|~{3,})\s*$")


def value_for(name):
    return f"〔{name}〕第一行\n第二行 $a|b$"


def full_ctx():
    return {name: value_for(name) for name in P.MARKERS}


def fenced_blocks(text):
    """[(name, fence, body_lines)] with the CommonMark closing rule of spec B.3."""
    blocks, lines, i = [], text.split("\n"), 0
    while i < len(lines):
        opened = FENCE_OPEN.match(lines[i])
        if not opened:
            i += 1
            continue
        fence, j = opened.group(1), i + 1
        while j < len(lines):
            closed = FENCE_CLOSE.match(lines[j])
            if closed and closed.group(1)[0] == fence[0] and len(closed.group(1)) >= len(fence):
                break
            j += 1
        if j == len(lines):
            raise AssertionError(f"块没有闭合：{opened.group(2)}")
        blocks.append((opened.group(2), fence, lines[i + 1:j]))
        i = j + 1
    return blocks


def only_block(text, name):
    found = [b for b in fenced_blocks(text) if b[0] == name]
    assert len(found) == 1, (name, found)
    return found[0]


def slot(text, label):
    """Material inserted under 【label】 up to the next 【…】 line or blank line."""
    match = re.search(r"^【" + re.escape(label) + r"】\n(.*?)(?=\n【|\n\n)", text, re.S | re.M)
    assert match, label
    return match.group(1)


class RegistryTests(unittest.TestCase):
    def test_keys_roles_titles_and_length(self):
        self.assertEqual(set(P.TEMPLATES), KEYS)
        for key, template in P.TEMPLATES.items():
            with self.subTest(key=key):
                self.assertIn(template["role"], ROLES)
                self.assertEqual(template["role"], NOT_AUTHOR.get(key, "author"))
                self.assertTrue(template["title"].strip())
                self.assertLessEqual(len(template["text"].splitlines()), 40)
                self.assertNotIn("【模板】", template["text"])
                for name in template.get("formats", ()):
                    self.assertIn(name, P.FORMATS)

    def test_markers_are_documented(self):
        self.assertLessEqual(SPEC_MARKERS, set(P.MARKERS))
        for key in P.TEMPLATES:
            with self.subTest(key=key):
                self.assertTrue(P.markers(key))
                self.assertLessEqual(set(P.markers(key)), set(P.MARKERS))

    def test_every_template_states_what_the_model_sees(self):
        for key, template in P.TEMPLATES.items():
            with self.subTest(key=key):
                text = template["text"]
                sees = next(line for line in text.splitlines() if "你能看到的只有" in line)
                self.assertIn("不要假设", sees)
                self.assertIn("不要照做", sees)
                labels = re.findall(r"^【(.+)】\n<<[a-z_]+>>$", text, re.M)
                self.assertEqual(len(labels), len(P.markers(key)))
                for label in labels:
                    self.assertIn(label, sees)

    def test_web_search_only_for_novelty_search(self):
        self.assertEqual({k for k, t in P.TEMPLATES.items() if t.get("web_search")}, {"innov_s4"})


class BuildTests(unittest.TestCase):
    def test_every_key_builds_with_empty_ctx(self):
        for key in P.TEMPLATES:
            with self.subTest(key=key):
                text = P.build(key, {})
                self.assertEqual(text.splitlines()[0], f"【模板】{key}")
                self.assertEqual(text.count("【模板】"), 1)
                self.assertIsNone(LEFTOVER.search(text))
                self.assertIn("】\n" + P.EMPTY + "\n", text)
                self.assertTrue(text.endswith("\n"))
                self.assertNotIn("None", text)
        self.assertEqual(P.build("q_draft"), P.build("q_draft", {}))
        self.assertEqual(P.build("q_draft", None), P.build("q_draft", {}))

    def test_every_key_builds_with_full_ctx(self):
        ctx = full_ctx()
        for key in P.TEMPLATES:
            with self.subTest(key=key):
                text = P.build(key, ctx)
                self.assertEqual(text.splitlines()[0], f"【模板】{key}")
                self.assertIsNone(LEFTOVER.search(text))
                used = set(P.markers(key))
                for name in P.MARKERS:
                    if name in used:
                        self.assertIn(value_for(name), text)
                    else:
                        self.assertNotIn(f"〔{name}〕", text)

    def test_reply_format_present_or_absent(self):
        self.assertEqual({k for k, t in P.TEMPLATES.items() if t.get("reply_format", True) is False}, NO_REPLY_FORMAT)
        for key in P.TEMPLATES:
            with self.subTest(key=key):
                text = P.build(key, full_ctx())
                if key in NO_REPLY_FORMAT:
                    self.assertNotIn(P.REPLY_FORMAT, text)
                    self.assertNotIn("台账变更", text)
                else:
                    self.assertEqual(text.count(P.REPLY_FORMAT), 1)
                    self.assertTrue(text.endswith(P.REPLY_FORMAT + "\n"))

    def test_drift_prompt_ends_with_drift_format_and_has_no_blocks(self):
        text = P.build("drift", full_ctx())
        self.assertTrue(text.endswith(P.DRIFT_FORMAT + "\n"))
        self.assertNotIn("```", text)
        for key in P.TEMPLATES.keys() - {"drift"}:
            self.assertNotIn(P.DRIFT_FORMAT, P.build(key, {}))

    def test_block_formats_follow_the_text_in_order(self):
        for key, template in P.TEMPLATES.items():
            with self.subTest(key=key):
                text = P.build(key, {})
                names = template.get("formats", ())
                for name, fmt in P.FORMATS.items():
                    self.assertEqual(text.count(fmt), 1 if name in names else 0, name)
                positions = [text.index(P.FORMATS[name]) for name in names]
                self.assertEqual(positions, sorted(positions))
                if names:
                    self.assertGreater(positions[0], text.index(template["text"].splitlines()[-1]))

    def test_inserted_material_is_not_expanded_again(self):
        text = P.build("q_attack", {"draft_blocks": "见 <<anchor>> 与 <<ledger>>", "anchor": "锚点内容"})
        self.assertIn("见 <<anchor>> 与 <<ledger>>", text)
        self.assertEqual(text.count("锚点内容"), 1)

    def test_value_rendering(self):
        def rendered(value):
            return slot(P.build("q_verify", {"plan": value}), "验证计划")

        for empty in (None, "", "  \n\t ", [], (), {}, ["", None]):
            self.assertEqual(rendered(empty), P.EMPTY, repr(empty))
        self.assertEqual(rendered("\r\n\r\n第一行\r\n  第二行\r\n\r\n"), "第一行\n  第二行")
        self.assertEqual(rendered(["甲", "", None, "乙"]), "甲\n乙")
        self.assertEqual(rendered(7), "7")
        self.assertIn('"键": "值"', P.build("q_verify", {"plan": {"键": "值"}}))

    def test_build_does_not_change_the_registry(self):
        before = {k: dict(v) for k, v in P.TEMPLATES.items()}
        P.build("q_draft", full_ctx())
        self.assertEqual(before, {k: dict(v) for k, v in P.TEMPLATES.items()})

    def test_unknown_key(self):
        with self.assertRaisesRegex(ValueError, "未知的提示词模板"):
            P.build("q_unknown", {})
        with self.assertRaises(ValueError):
            P.markers("q_unknown")
        with self.assertRaises(ValueError):
            P.build(["q_draft"], {})


class GrammarTests(unittest.TestCase):
    """The formats the models are shown must match spec B.3 exactly."""

    def test_block_names_and_fences(self):
        for key in P.TEMPLATES:
            with self.subTest(key=key):
                for name, fence, _ in fenced_blocks(P.build(key, {})):
                    self.assertIn(name, BLOCK_NAMES)
                    self.assertEqual(fence, "````" if name == "正文修订" else "```")

    def test_reply_format_revision_and_ledger_blocks(self):
        _, fence, body = only_block(P.REPLY_FORMAT, "正文修订")
        self.assertEqual(fence, "````")
        self.assertTrue(body[0].startswith("节："))
        self.assertEqual(body[1], "方式：替换")
        _, _, ops = only_block(P.REPLY_FORMAT, "台账变更")
        self.assertTrue(ops)
        for line in ops:
            self.assertRegex(line, r"^(\+ \[新\d+\] \S+ \| .+ \| .+|~ L\d+ \| (确定|否定|待定) \| .+ \| .*|\? .+ \| .+ \| .+|! .+)$")
        self.assertEqual({line[0] for line in ops}, {"+", "~", "?", "!"})
        self.assertTrue(any("| 反例：" in line for line in ops))
        self.assertTrue(re.search(r"^~ L\d+ \| 确定 \| .+ \| R\d+$", "\n".join(ops), re.M))

    def test_reply_format_states_the_rules(self):
        text = P.REPLY_FORMAT
        for phrase in ("C1", "{{R01.max_err}}", "{{R01.max_err|.3e}}", "四个反引号", "替换", "追加", "第 1 节", "第 5 节",
                       "不写状态", "推导步、论断、待验证、想法、文献", "替代 L03", "确定、否定、待定", "Rnn", "Vnn",
                       "「反例：」", "有反例待核", "人工", "? 假设", "不能用 + 行", "暂停", "\\|", "只写一个字：无"):
            self.assertIn(phrase, text)
        self.assertEqual([line.split("：")[0] for line in text.splitlines()[-3:]], ["结论摘要", "主要分歧", "建议的下一步"])
        self.assertIsNone(LEFTOVER.search(text))

    def test_plan_format(self):
        _, _, body = only_block(P.PLAN_FORMAT, "验证计划")
        keys = [line.split("：", 1)[0] for line in body]
        self.assertEqual(keys, ["标题", "针对", "方向", "方法", "输入与参数", "退化自检", "判据", "预期"])
        values = dict(line.split("：", 1) for line in body)
        self.assertRegex(values["针对"], r"^L\d+$")
        self.assertIn(values["方向"], {"支持", "否定"})
        self.assertIn(values["方法"], {"仿真", "理论", "实测"})
        for phrase in ("支持 或 否定", "仿真、理论 或 实测", "[新1]"):
            self.assertIn(phrase, P.PLAN_FORMAT)

    def test_question_list_format(self):
        _, _, body = only_block(P.QUESTION_LIST_FORMAT, "小问题清单")
        self.assertTrue(body[0].startswith("### "))
        self.assertEqual([line.split("：", 1)[0] for line in body[1:]], ["问题", "判据", "依赖", "承重", "验证方式"])
        self.assertEqual(body[4], "承重：是")

    def test_candidate_format(self):
        _, _, body = only_block(P.CANDIDATE_FORMAT, "候选框架")
        self.assertTrue(body[0].startswith("# "))
        self.assertTrue(any(line.startswith("一句话主张：") for line in body))
        self.assertTrue(any(line.startswith("风险：") for line in body))
        chapters = [re.match(r"^## 第(\d)章 .+ \| (\w+)$", line) for line in body if line.startswith("## ")]
        self.assertTrue(all(chapters))
        self.assertEqual([m.group(1) for m in chapters][0], "1")
        self.assertEqual([m.group(1) for m in chapters][-1], "7")
        self.assertEqual({m.group(2) for m in chapters}, {"intro", "theory", "research", "framework", "conclusion"})
        self.assertTrue(any(line.startswith("### ") for line in body))

    def test_reverify_format(self):
        _, _, body = only_block(P.REVERIFY_FORMAT, "复推")
        self.assertEqual([re.match(r"^L\d+ \| (一致|不一致|无法核对) \| .+$", line).group(1) for line in body],
                         ["一致", "不一致", "无法核对"])

    def test_drift_format(self):
        lines = P.DRIFT_FORMAT.splitlines()[1:]
        self.assertEqual([line.split("：", 1)[0] for line in lines],
                         ["仍在回答锚点里的问题吗", "用了锚点以外的假设吗", "判据或范围被改变了吗", "判定"])
        self.assertEqual(lines[-1], "判定：未漂移 / 轻微 / 漂移")
        self.assertIn("未漂移、轻微、漂移 三个词之一", P.DRIFT_FORMAT)

    def test_anchor_draft_uses_the_slot_labels_of_each_level(self):
        expected = {
            "framework_layer": ["要回答的大问题", "论文主张与成功标准", "前提与假设", "约束（设备、数据、时间、学校要求）", "不做什么"],
            "chapter": ["本章回答的问题", "判据与评测协议（数据、指标、基线）", "依赖与假设", "范围", "不做什么"],
            "question": ["要回答的问题", "判据", "假设", "范围", "不做什么"],
        }
        for level, labels in expected.items():
            with self.subTest(level=level):
                _, _, body = only_block(P.FORMATS["anchor_" + level], "锚点草稿")
                self.assertEqual([line[3:] for line in body if line.startswith("## ")], labels)
                self.assertIn("| 编号 | 内容 | 依据 | 失效条件 | 状态 | 来源 |", body)
                self.assertTrue(any(line.startswith("| H1 |") and "| 待批 |" in line for line in body))
        for key, level in (("open_question", "question"), ("open_chapter", "chapter"), ("open_framework_layer", "framework_layer")):
            self.assertEqual(P.TEMPLATES[key]["formats"], ("anchor_" + level,))


class ContentTests(unittest.TestCase):
    """Spot checks of the constraints listed in spec C.2."""

    def text(self, key):
        return P.build(key, {})

    def assertAllIn(self, phrases, key):
        text = self.text(key)
        for phrase in phrases:
            self.assertIn(phrase, text, f"{key}: {phrase}")

    def test_theory_rigor(self):
        core = ("依据", "失效条件", "成立条件", "误差的量级", "解析上答案已知", "退化", "两条独立")
        for key in ("q_draft", "open_chapter", "ch_outline", "open_question"):
            self.assertAllIn(core, key)
        for key in ("q_draft", "open_chapter", "ch_outline", "q_reply", "explain"):
            self.assertAllIn(("不跳步",), key)
        self.assertAllIn(("跳过的步骤", "误差的量级", "退化自检", "无论条目对错都会通过"), "q_attack")
        self.assertAllIn(("不跳步", "误差的量级", "退化自检", "两条独立"), "q_reply")
        self.assertAllIn(("退化自检", "两条独立"), "q_verify")
        self.assertAllIn(("退化自检", "两条独立"), "innov_s5")

    def test_q_draft(self):
        text = self.text("q_draft")
        self.assertIn("只推进一步", text)
        self.assertIn("推完这一步就停", text)
        self.assertIn("起点式和目标式", text)
        self.assertIn("```验证计划", text)
        first_rule = text.split("要求：\n", 1)[1].splitlines()[0]
        self.assertIn("上次被打回的理由", first_rule)
        self.assertIn("rejected_reason", P.markers("q_draft"))

    def test_reviewer_sees_only_anchor_ledger_and_this_round(self):
        self.assertEqual(set(P.markers("q_attack")), {"level", "anchor", "ledger", "rejected", "draft_blocks"})
        self.assertAllIn(("站得住", "有条件成立", "不成立", "资料不足", "不提出替代方案", "+ [新1] 待验证",
                          "| 反例：", "不写正文修订块"), "q_attack")

    def test_q_reply(self):
        self.assertAllIn(("接受", "拒绝", "需要实验", "部分接受", "```验证计划", "替代 Lxx"), "q_reply")
        self.assertLessEqual({"draft_blocks", "attack", "doc"}, set(P.markers("q_reply")))

    def test_q_verify(self):
        self.assertAllIn(('"selfcheck_passed"', '"criteria"', '"predicted"', '"observed"', '"threshold"', '"comparator"',
                          '"passed"', '"abs<"', '"independent_check"', '"parameters"', "对照表.md", "运行命令.txt",
                          "Python 解释器", "输出/", "计划.md", "正文摘录.md", "代码/", "不要宣布命题成立"), "q_verify")
        self.assertEqual(set(P.markers("q_verify")), {"item", "plan", "code_files", "python"})

    def test_explain(self):
        self.assertAllIn(("不提出新的论断", "不推进下一步", "最小的数值例子"), "explain")
        self.assertLessEqual({"staged_step", "attack", "focus"}, set(P.markers("explain")))

    def test_drift_sees_nothing_else(self):
        self.assertEqual(P.markers("drift"), ["level", "anchor", "ledger", "summary"])
        self.assertAllIn(("没有参与", "＊", "待批"), "drift")

    def test_referee_never_sees_discussion(self):
        self.assertFalse(set(P.markers("referee")) & {"draft_blocks", "attack", "attacks", "proposals", "staged_step",
                                                      "summary", "rejected_reason", "focus"})
        self.assertAllIn(("```复推", "1–10", "三个弱点", "最小的修补", "对照表", "占位符", "不要采信", "不写正文修订块"), "referee")

    def test_independent_framework_proposals(self):
        self.assertFalse(set(P.markers("fw_propose")) & {"proposals", "attacks"})
        self.assertAllIn(("1–2 个", "7 章", "第 6 章的角色随框架而定", "计算机方向", "```候选框架"), "fw_propose")
        self.assertAllIn(("致命", "严重", "可修补", "未核实", "DOI", "arXiv", "不提出你自己的框架"), "fw_attack")
        self.assertAllIn(("2–3 个", "不要合并", "不要替用户选", "对照表", "取舍", "```候选框架"), "fw_synthesize")
        self.assertAllIn(("论文主线", "章节安排", "优点与风险"), "fw_refine")

    def test_chapter_templates(self):
        self.assertAllIn(("```锚点草稿", "评测协议", "符号表、假设表和误差模型"), "open_chapter")
        self.assertAllIn(("3–8 个小问题", "几天内", "```小问题清单"), "ch_outline")
        self.assertAllIn(("覆盖", "依赖", "可证伪", "重叠"), "ch_attack")

    def test_innovation_stages(self):
        self.assertAllIn(("参数范围", "+ 待验证", "```验证计划"), "innov_s1")
        self.assertAllIn(("8–12", "淘汰清单", "+ [新n] 想法"), "innov_s2")
        self.assertAllIn(("3 个左右", "~ Lxx | 否定", "由用户决定"), "innov_s3")
        self.assertAllIn(("DOI", "arXiv", "点名", "放弃", "待核实", "+ [新n] 文献"), "innov_s4")
        self.assertAllIn(("「方向」写 否定", "```验证计划", "```小问题清单"), "innov_s5")
        self.assertAllIn(("合成数据", "基线", "消融", "鲁棒性", "实测", "```小问题清单"), "innov_s6")

    def test_writing_templates(self):
        self.assertAllIn(("故事稿", "待验证", "待查文献"), "intro_story")
        self.assertAllIn(("「确定」", "已核实", "DOI"), "intro_final")
        self.assertAllIn(("「确定」", "「否定」", "「待定」", "局限", "展望", "没有论证过"), "conclusion")
        self.assertAllIn(("（组装时新增）", "占位符", "链接", "章号.序号"), "assemble")


if __name__ == "__main__":
    unittest.main()
