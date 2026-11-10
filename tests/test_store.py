from __future__ import annotations

import base64
import hashlib
import tempfile
import unittest
from pathlib import Path

from notebook.store import ConflictError, Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="store-test-", dir=Path(__file__).resolve().parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root)
        self.content = self.store.perform("save_content", {"title": "误差研究", "objective": "核对已知偏差"})
        self.q = self.store.perform("create_question", {"content_id": self.content["id"], "title": "校正是否恢复真值", "criterion": "绝对误差小于 1e-8"})

    def action(self, name, **kwargs):
        return self.store.perform(name, {"question_id": self.q["id"], **kwargs})

    def discussion(self):
        return self.action("save_discussion", ai="GPT", raw="用户：模型如何写？\nGPT：y=x+b，若 b 已知，则 y-b=x。", title="建立模型")

    def plan(self):
        return self.action("save_plan", title="解析例核对", method="simulation", target="核对 y-b=x", criterion="绝对误差小于 1e-8", inputs="x=10,b=2", expected="10", source_ids=[])

    def create_run_record(self, plan=None):
        plan = plan or self.plan()
        return self.action("create_run", title="首次验证", plan_id=plan["id"], code_paths=[])

    def test_empty_project_and_independent_content_reused(self):
        initial = Store(self.root / "empty").state()
        self.assertEqual(initial["questions"], [])
        self.assertEqual(initial["frameworks"], [])
        f1 = self.store.perform("save_framework", {"title": "框架一", "status": "active", "content_ids": [self.content["id"]]})
        f2 = self.store.perform("save_framework", {"title": "框架二", "content_ids": [self.content["id"]]})
        detail = self.store.content_detail(self.content["id"])
        self.assertEqual(len(detail["placements"]), 2)
        self.assertEqual(len(detail["questions"]), 1)
        self.store.perform("save_framework", {"id": f2["id"], "revision": f2["revision"], "status": "active"})
        self.assertEqual([f["id"] for f in self.store.state()["frameworks"] if f["status"] == "active"], [f2["id"]])
        self.assertEqual(self.store.state()["questions"][0]["path"], self.q["path"])

    def test_source_edit_refresh_and_stale_revision_does_not_overwrite(self):
        old = self.store.detail(self.q["id"])
        body_path = self.root / old["body_path"]
        body_path.write_text("用户在编辑器中写入的推导。", encoding="utf-8")
        self.assertEqual(self.store.detail(self.q["id"])["body"], "用户在编辑器中写入的推导。")
        with self.assertRaises(ConflictError):
            self.action("save_body", revision=old["body_revision"], text="过期页面试图覆盖")
        self.assertEqual(body_path.read_text(encoding="utf-8"), "用户在编辑器中写入的推导。")

    def test_repeated_discussion_saved_separately_and_raw_heading_preserved(self):
        raw = "## 要点\n原始对话自己的标题\n\n【主题】模型\n【要点】\n- y=x+b\n【遗留问题】\n- b 怎样估计？"
        first = self.action("save_discussion", ai="Gemini", raw=raw)
        second = self.action("save_discussion", ai="Gemini", raw=raw)
        self.assertEqual(first["raw"], raw)
        self.assertEqual(first["title"], "模型")
        self.assertEqual(first["capture_type"], "摘要保存块")
        self.assertNotEqual(first["id"], second["id"])
        self.assertTrue((self.root / first["path"]).exists())

    def test_failed_run_does_not_refute_and_review_does_not_complete(self):
        run = self.create_run_record()
        self.assertEqual(self.store.detail(self.q["id"])["status"], "running")
        done = self.action("update_run", id=run["id"], revision=run["revision"], actual="残差 0.1", outcome="fail")
        self.assertEqual(self.store.detail(self.q["id"])["status"], "needs_judgment")
        self.action("save_judgment", decision="review", text="先查代码参数", evidence_ids=[done["id"]], scope="本轮")
        self.assertEqual(self.store.detail(self.q["id"])["status"], "review")
        with self.assertRaises(ValueError):
            self.store.perform("append_draft", {"content_id": self.content["id"], "question_id": self.q["id"], "revision": self.store.content_detail(self.content["id"])["draft_revision"], "text": "不应入稿"})

    def test_theory_judgment_without_run_and_later_body_edit_needs_review(self):
        discussion = self.discussion()
        judgment = self.action("save_judgment", decision="supported", text="模型内代数成立", evidence_ids=[discussion["id"]], scope="已知恒定 b")
        self.assertEqual(self.store.detail(self.q["id"])["status"], "supported")
        body = self.store.detail(self.q["id"])
        self.action("save_body", text="改为非线性模型。", revision=body["body_revision"])
        detail = self.store.detail(self.q["id"])
        self.assertEqual(detail["status"], "review")
        self.assertEqual(detail["latest_judgment"]["id"], judgment["id"])
        frozen = self.root / judgment["evidence_snapshots"][0]["path"]
        self.assertEqual(hashlib.sha256(frozen.read_bytes()).hexdigest(), judgment["evidence_snapshots"][0]["sha256"])

    def test_question_definition_change_does_not_inherit_supported(self):
        d = self.discussion()
        self.action("save_judgment", decision="supported", text="原模型成立", evidence_ids=[d["id"]], scope="原模型")
        q = self.store.detail(self.q["id"])
        self.store.perform("update_question", {"id": q["id"], "revision": q["revision"], "criterion": "新判据"})
        self.assertEqual(self.store.detail(q["id"])["status"], "review")

    def test_code_snapshot_is_immutable_and_duplicate_filename_versions(self):
        code = self.action("add_code", filename="sim.py", purpose="校正", text="print(10)\n")
        code2 = self.action("add_code", filename="sim.py", purpose="新版", text="print(11)\n")
        self.assertNotEqual(code["path"], code2["path"])
        plan = self.plan()
        run = self.action("create_run", plan_id=plan["id"], title="核对", code_paths=[code["path"]])
        frozen_path = self.root / run["code_snapshots"][0]["path"]
        (self.root / code["path"]).write_text("print(99)\n", encoding="utf-8")
        self.assertEqual(frozen_path.read_text(encoding="utf-8"), "print(10)\n")
        self.assertEqual(run["criterion"], plan["criterion"])

    def test_plan_and_judgment_validation(self):
        with self.assertRaises(ValueError):
            self.action("save_plan", target="test", criterion="", method="simulation")
        with self.assertRaises(ValueError):
            self.action("save_judgment", decision="supported", text="没有证据", evidence_ids=[], scope="测试")
        with self.assertRaises(ValueError):
            self.action("save_judgment", decision="supported", text="错误引用", evidence_ids=["R99"], scope="测试")
        with self.assertRaises(ValueError):
            self.action("save_judgment", decision="supported", text="空正文", evidence_ids=["body"], scope="测试")

    def test_new_run_supersedes_current_status_not_old_evidence(self):
        d = self.discussion()
        self.action("save_judgment", decision="supported", text="原模型内成立", evidence_ids=[d["id"]], scope="原条件")
        run = self.create_run_record()
        self.assertEqual(self.store.detail(self.q["id"])["status"], "running")
        self.action("update_run", id=run["id"], revision=run["revision"], actual="0", outcome="pass")
        detail = self.store.detail(self.q["id"])
        self.assertEqual(detail["status"], "needs_judgment")
        self.assertEqual(len(detail["judgments"]), 1)

    def test_attachments_no_overwrite_and_path_escape_blocked(self):
        data = base64.b64encode(b"x,error\n10,0\n").decode()
        first = self.action("attach_file", filename="metrics.csv", base64=data)
        second = self.action("attach_file", filename="metrics.csv", base64=data)
        self.assertNotEqual(first["path"], second["path"])
        self.assertEqual(self.store.read_file(first["path"]).read_bytes(), b"x,error\n10,0\n")
        for name in ("../oops.py", "CON", "x:y.txt", ".secret", "dir/a.txt", "dir\\a.txt"):
            with self.subTest(name=name), self.assertRaises((PermissionError, ValueError)):
                self.action("add_code", filename=name, purpose="bad", text="")
        for relative in ("../outside", "notebook/store.py", ".history/secret", "C:/Users/user/secret"):
            with self.subTest(relative=relative), self.assertRaises((PermissionError, FileNotFoundError)):
                self.store.read_file(relative)

    def test_draft_preserves_scope_and_sources_and_previous_file(self):
        d = self.discussion()
        self.action("save_judgment", decision="supported", text="校正成立", evidence_ids=[d["id"]], scope="只适用于已知恒定 b", meaning="误差分析")
        old = self.store.content_detail(self.content["id"])
        new = self.store.perform("append_draft", {"content_id": self.content["id"], "question_id": self.q["id"], "revision": old["draft_revision"], "text": "本文采用加性校正。"})
        self.assertIn("只适用于已知恒定 b", new["draft"])
        self.assertIn("D01", new["draft"])
        self.assertTrue(list((self.root / ".history").rglob("*.bak")))

    def test_prompt_snapshot_survives_project_change(self):
        prompt = self.action("generate_prompt", template="P1", focus="第一步")
        before = (self.root / prompt["path"]).read_bytes()
        project = self.store.state()["project"]
        self.store.perform("save_project", {"revision": project["revision"], "brief": "新的背景"})
        self.assertEqual((self.root / prompt["path"]).read_bytes(), before)
        detail = self.store.detail(self.q["id"])
        self.assertEqual(detail["prompts"][0]["id"], prompt["id"])

    def test_duplicate_id_and_broken_record_are_visible_errors(self):
        original = self.root / self.q["path"]
        duplicate = original.parent.parent / (self.q["id"] + "_重复") / "问题.md"
        duplicate.parent.mkdir()
        duplicate.write_bytes(original.read_bytes())
        with self.assertRaisesRegex(ValueError, "重复编号"):
            self.store.state()

    def test_corrupted_section_is_not_silently_replaced(self):
        project = self.root / "00_项目卡.md"
        project.write_text(project.read_text(encoding="utf-8").replace("<!-- /field:brief -->", ""), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "段落边界"):
            self.store.state()

    def test_run_output_changes_after_judgment_require_review(self):
        run = self.create_run_record()
        attachment = self.action("attach_file", run_id=run["id"], filename="metrics.csv", base64=base64.b64encode(b"error\n0\n").decode())
        run = self.action("update_run", id=run["id"], revision=run["revision"], outcome="pass", actual="误差为 0")
        self.assertEqual(len(run["result_files"]), 1)
        self.action("save_judgment", decision="supported", text="本轮支持", evidence_ids=[run["id"]], scope="三个输入")
        self.assertEqual(self.store.detail(self.q["id"])["status"], "supported")
        (self.root / attachment["path"]).write_text("error\n99\n", encoding="utf-8")
        self.assertEqual(self.store.detail(self.q["id"])["status"], "review")

    def test_missing_source_does_not_hide_evidence_gap(self):
        d = self.discussion()
        j = self.action("save_judgment", decision="supported", text="模型成立", evidence_ids=[d["id"]], scope="原模型")
        (self.root / d["path"]).unlink()
        self.assertEqual(self.store.detail(self.q["id"])["status"], "review")
        self.assertTrue((self.root / j["evidence_snapshots"][0]["path"]).is_file())

    def test_question_and_content_files_have_readable_relationship_indexes(self):
        self.discussion()
        q = self.store.detail(self.q["id"])
        source = (self.root / q["path"]).read_text(encoding="utf-8")
        self.assertIn("对话记录", source)
        self.assertIn("[D01]", source)
        c = self.store.content_detail(self.content["id"])
        self.assertIn(self.q["id"], (self.root / c["path"]).read_text(encoding="utf-8"))
        self.assertEqual(q["question"], self.q["question"])


if __name__ == "__main__":
    unittest.main()
