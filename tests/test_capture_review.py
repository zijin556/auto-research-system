"""Independent adversarial checks for discussion-to-file capture."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from notebook.store import ConflictError, Store


ROUTE = "论文框架：方法检验\n研究内容：误差分析\n研究问题：误差会随步长缩小吗？\n"


class CaptureReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="capture-review-", dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name))

    def visible_files(self):
        return {p.relative_to(self.store.root).as_posix(): p.read_bytes()
                for p in self.store.root.rglob("*") if p.is_file()
                and not any(x.startswith(".") for x in p.relative_to(self.store.root).parts)}

    def archive(self, text, request_id="capture-review-0001", **kwargs):
        data = {"text": text, **kwargs}
        preview = self.store.perform("preview_capture", data)
        self.assertTrue(preview["can_import"], preview["needs_choice"])
        return self.store.perform("import_capture", {**data, "token": preview["token"], "request_id": request_id})

    def test_raw_crlf_and_extracted_long_fence_bytes_preserved(self):
        code = 'sample = """\r\n```\r\n研究问题：这行只是字符串\r\n"""\r\nprint(sample)\r\n'
        raw = ROUTE.replace("\n", "\r\n") + "````python nested.py\r\n" + code + "````\r\n\r\n结论：仍待核查。\r\n"
        result = self.archive(raw)
        self.assertEqual(self.store.read_file(result["source_path"]).read_bytes(), raw.encode())
        saved = self.store.read_file(result["files"][0]["path"]).read_bytes()
        self.assertEqual(saved, code.encode())
        question = self.store.detail(result["question_id"])
        self.assertEqual(question["title"], "误差会随步长缩小吗？")
        self.assertEqual(question["judgments"], [])
        self.assertNotEqual(question["status"], "supported")

    def test_incomplete_fence_cannot_supply_route_labels(self):
        raw = ROUTE + '```python\nprint("unfinished")\n研究问题：代码里的另一个标题\n'
        preview = self.store.perform("preview_capture", {"text": raw})
        self.assertTrue(preview["can_import"], preview["needs_choice"])
        self.assertEqual(preview["plan"]["question"]["title"], "误差会随步长缩小吗？")
        self.assertEqual(preview["files"], [])
        self.assertTrue(any("没有结束" in warning for warning in preview["warnings"]))

    def test_conflicting_current_question_requires_choice_without_writes(self):
        original = self.archive(ROUTE + "原问题的讨论。")
        raw = "论文框架：另一主线\n研究内容：另一内容\n研究问题：另一个明确的问题？\n```python another.py\nprint(2)\n```\n"
        data = {"text": raw, "question_id": original["question_id"], "framework_id": original["framework_id"]}
        before = self.visible_files()
        preview = self.store.perform("preview_capture", data)
        self.assertFalse(preview["can_import"])
        self.assertTrue(preview["needs_choice"])
        with self.assertRaises(ValueError):
            self.store.perform("import_capture", {**data, "token": preview["token"], "request_id": "capture-review-conflict"})
        self.assertEqual(before, self.visible_files())

    def test_invalid_fence_filename_rejected_before_writes(self):
        before = self.visible_files()
        for name in ["../escaped.py", "C:\\escaped.py", "CON.py", ".hidden.py", "bad.py:stream"]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.store.perform("preview_capture", {"text": ROUTE + "```python " + name + "\nprint(1)\n```\n"})
            self.assertEqual(before, self.visible_files())

    def test_exact_existing_child_route_wins_over_unrelated_active_framework(self):
        first = self.store.perform("save_content", {"title": "模型推导"})
        second = self.store.perform("save_content", {"title": "误差分析"})
        self.store.perform("save_framework", {"title": "当前框架", "status": "active", "content_ids": [first["id"]]})
        other = self.store.perform("save_framework", {"title": "候选框架", "content_ids": [second["id"]]})
        question = self.store.perform("create_question", {"content_id": second["id"], "title": "边界误差有多大？"})
        preview = self.store.perform("preview_capture", {"text": "研究内容：误差分析\n研究问题：边界误差有多大？\n继续讨论已有问题。"})
        self.assertTrue(preview["can_import"], preview["needs_choice"])
        self.assertEqual(preview["plan"]["framework"]["id"], other["id"])
        self.assertEqual(preview["plan"]["content"]["id"], second["id"])
        self.assertEqual(preview["plan"]["question"]["id"], question["id"])

    def test_stale_preview_rejected_and_retry_is_idempotent(self):
        raw = ROUTE + "初始讨论。\n```csv data.csv\nx,y\n1,2\n```\n"
        data = {"text": raw}
        preview = self.store.perform("preview_capture", data)
        self.store.perform("save_framework", {"title": "另外新增的框架"})
        before = self.visible_files()
        with self.assertRaises(ConflictError):
            self.store.perform("import_capture", {**data, "token": preview["token"], "request_id": "capture-review-stale"})
        self.assertEqual(before, self.visible_files())
        preview = self.store.perform("preview_capture", data)
        payload = {**data, "token": preview["token"], "request_id": "capture-review-current"}
        first = self.store.perform("import_capture", payload)
        after = self.visible_files()
        self.assertEqual(first, self.store.perform("import_capture", payload))
        self.assertEqual(after, self.visible_files())
        with self.assertRaises(ConflictError):
            self.store.perform("import_capture", {**payload, "text": raw + "changed"})

    def test_multiple_questions_require_choice_and_keep_full_discussion(self):
        raw = ROUTE + "第一问题完整讨论。\n研究问题：第二个问题如何解释？\n第二问题完整讨论。\n```python two.py\nprint('both preserved')\n```\n"
        preview = self.store.perform("preview_capture", {"text": raw})
        self.assertFalse(preview["can_import"])
        self.assertTrue(any(c["level"] == "question" for c in preview["needs_choice"]))
        result = self.archive(raw, question_title="第二个问题如何解释？")
        self.assertEqual(self.store.read_file(result["source_path"]).read_bytes(), raw.encode())
        self.assertIn("第一问题完整讨论", self.store.detail(result["question_id"])["discussions"][0]["raw"])


if __name__ == "__main__":
    unittest.main()
