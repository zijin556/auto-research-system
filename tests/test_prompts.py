import unittest

from notebook.prompts import TEMPLATES, build_prompt, library_markdown, parse_capture


class CaptureTests(unittest.TestCase):
    def test_multiline_tagged_capture_keeps_formulas(self):
        raw = "【主题】核对偏差\n【要点】\n- x̂=y−b\n- 假设 b 已知\n【遗留问题】\nb 未知时怎么办？"
        got = parse_capture(raw)
        self.assertEqual(got["title"], "核对偏差")
        self.assertEqual(got["summary"], "- x̂=y−b\n- 假设 b 已知")
        self.assertEqual(got["unresolved"], "b 未知时怎么办？")
        self.assertTrue(raw.startswith("【主题】"))

    def test_markdown_wrapping_and_repeated_sections(self):
        got = parse_capture("```text\n**【主题】** 校正\n**【要点】** 甲\n【要点】乙\n【遗留问题】 无\n```")
        self.assertEqual(got, {"title": "校正", "summary": "甲\n\n乙", "unresolved": "无"})

    def test_unlabelled_original_is_not_invented_summary(self):
        self.assertEqual(parse_capture("用户：为什么？\nAI：需要检查。"), {"title": "", "summary": "", "unresolved": ""})

    def test_inline_capture(self):
        self.assertEqual(parse_capture("【主题】a【要点】b【遗留问题】c"), {"title": "a", "summary": "b", "unresolved": "c"})


class PromptTests(unittest.TestCase):
    def setUp(self):
        self.project = {"title": "新项目", "brief": "测试简介", "configuration": "b=2", "symbols": "x 为真值",
                        "root": "E:/研究/项目"}
        self.framework = {"id": "F01", "title": "偏差模型", "status": "active", "mainline": "先模型后核查", "content_ids": ["C01"]}
        self.content = {"id": "C01", "title": "偏差", "objective": "确认边界", "path": "02_研究内容/C01_偏差/研究内容.md"}
        self.question = {"id": "C01-Q01", "title": "校正是否成立", "status": "review", "body": "我编辑后的正文 x̂=y−b",
                         "path": "02_研究内容/C01_偏差/C01-Q01_校正/问题.md", "criterion": "误差小于 1e-8",
                         "discussions": [{"id": "D01", "title": "早期核查", "summary": "早期摘要", "raw": "早期原文"},
                                         {"id": "D02", "title": "边界", "summary": "当前摘要", "unresolved": "检查独立性", "raw": "完整原文"}],
                         "plans": [{"id": "P01", "criterion": "原判据", "method": "simulation"},
                                   {"id": "P02", "criterion": "另一判据", "method": "theory"}],
                         "runs": [{"id": "R01", "plan_id": "P01", "outcome": "pending", "path": "02_研究内容/C01_偏差/C01-Q01_校正/结果/R01_测试/运行记录.md", "mapping": "式1 → check.py L4", "actual": "误差0.1"}],
                         "judgments": [{"id": "J01", "decision": "review", "text": "先检查代码", "evidence_ids": ["R01"], "scope": "仅选定输入"}]}

    def build(self, key, **kwargs):
        return build_prompt(key, self.project, self.framework, self.content, self.question, **kwargs)

    def test_context_uses_current_body_and_does_not_claim_verified(self):
        text = self.build("P0")
        for required in ("我编辑后的正文", "b=2", "x 为真值", "当前采用", "需复查", "检查独立性", "仅选定输入", "当前状态不代表已验证成立"):
            self.assertIn(required, text)
        self.assertNotIn("完整原文", text)
        self.assertNotIn("早期摘要", text)

    def test_selected_run_uses_its_own_frozen_plan(self):
        text = self.build("P8", sources=["R01"])
        self.assertIn("原判据", text)
        self.assertNotIn("另一判据", text)
        self.assertNotIn("当前摘要", text)
        self.assertIn("check.py L4", text)

    def test_edited_plan_does_not_replace_actual_run_snapshot(self):
        self.question["plans"][0]["criterion"] = "后改的宽松判据"
        self.question["runs"][0]["frozen_plan"] = {"id": "P01", "title": "最初计划", "criterion": "运行时严格判据", "inputs": "原输入", "path": "结果/R01/计划快照.md"}
        text = self.build("P8", sources=["R01"])
        self.assertIn("运行时严格判据", text)
        self.assertIn("R01 建立时的计划快照", text)
        self.assertNotIn("后改的宽松判据", text)

    def test_selected_discussion_can_include_original(self):
        text = self.build("P4", sources=["D01"], focus="只检查第一式")
        self.assertIn("早期原文", text)
        self.assertIn("只检查第一式", text)
        self.assertNotIn("完整原文", text)

    def test_unknown_source_is_not_silently_ignored(self):
        with self.assertRaisesRegex(ValueError, "不存在"):
            self.build("P4", sources=["D99"])

    def test_backend_body_id_and_explicit_handoff_record(self):
        self.question["prompts"] = [{"id": "T01", "text": "此前准备的任务", "path": "交接包/T01_P4.md"}]
        text = self.build("P4", sources=[{"id": "body"}, {"id": "T01"}])
        self.assertIn("我编辑后的正文", text)
        self.assertIn("此前准备的任务", text)
        self.assertIn("不证明已发送", text)

    def test_absolute_question_directory_with_dot_is_not_truncated(self):
        self.question["absolute_path"] = "E:/研究/C01-Q01_算法v1.0"
        self.assertIn("E:/研究/C01-Q01_算法v1.0/代码/", self.build("P7"))

    def test_execution_prompt_has_exact_paths_and_mapping(self):
        text = self.build("P7")
        self.assertIn("E:/研究/项目/02_研究内容/C01_偏差/C01-Q01_校正/代码/", text)
        self.assertIn("E:/研究/项目/02_研究内容/C01_偏差/C01-Q01_校正/结果/R01_测试/", text)
        self.assertIn("公式 ↔ 代码行号对照表", text)
        self.assertIn("不要覆写", text)

    def test_complete_run_is_not_automatically_reused(self):
        self.question["runs"][0]["outcome"] = "pass"
        text = self.build("P7")
        self.assertIn("当前没有选中的待执行运行记录", text)

    def test_paper_prompt_preserves_limits_and_needs_review_state(self):
        text = self.build("P9")
        for phrase in ("不能写成已证明或已验证", "仅选定输入", "需复查", "草稿.md", "供我审核"):
            self.assertIn(phrase, text)

    def test_comparison_includes_all_candidates_without_adopting(self):
        sources = {"frameworks": [self.framework, {"id": "F02", "title": "另一主线", "status": "candidate", "content_ids": ["C01"]}],
                   "contents": [self.content], "questions": [self.question]}
        text = self.build("P10", sources=sources)
        self.assertIn("F02 另一主线", text)
        self.assertIn("不要替我自动切换框架", text)
        self.assertIn("各问题的当前研究状态", text)

    def test_library_and_empty_project_work(self):
        text = library_markdown()
        self.assertEqual(len(TEMPLATES), 11)
        self.assertIn("## P10", text)
        self.assertIn("尚未填写", build_prompt("P0", {}, None, None, None))


if __name__ == "__main__":
    unittest.main()
