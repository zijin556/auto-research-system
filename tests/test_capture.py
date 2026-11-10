from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path

from notebook.store import ConflictError, Store


TEXT = """论文框架：数值方法的误差与验证
研究内容：导数近似与收敛阶
研究问题：中心差分是否二阶收敛？

用户：请解释理论，再写程序。
AI（示例）：中心差分的截断误差通常为二阶。
判据：误差阶在 1.98 到 2.02。

```python central_difference.py
print(2)
```

```csv convergence.csv
h,error
0.1,0.001
```
结论：尚待人工确认。
适用范围：只适用于本次算例。
"""


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="capture-test-", dir=Path(__file__).resolve().parent)
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name))

    def files(self):
        return {p.relative_to(self.store.root).as_posix(): p.read_bytes() for p in self.store.root.rglob("*") if p.is_file() and not any(part.startswith(".") for part in p.relative_to(self.store.root).parts)}

    def save(self, text=TEXT, rid="capture-test-001", **extra):
        data = {"text": text, **extra}
        preview = self.store.perform("preview_capture", data)
        self.assertTrue(preview["can_import"], preview["needs_choice"])
        return self.store.perform("import_capture", {**data, "token": preview["token"], "request_id": rid})

    def test_preview_zero_writes_and_single_save_original_code_result(self):
        before = self.files()
        raw = TEXT.replace("\n", "\r\n")
        p = self.store.perform("preview_capture", {"text": raw})
        self.assertEqual(before, self.files())
        self.assertEqual(p["counts"], {"frameworks": 1, "contents": 1, "questions": 1, "discussions": 1, "codes": 1, "results": 1, "attachments": 0})
        self.assertEqual(p["plan"]["ai"], "AI（示例）")
        self.assertNotIn("print(2)", p["plan"]["body"])
        self.assertIn("central_difference.py", p["plan"]["body"])
        payload = {"text": raw, "token": p["token"], "request_id": "capture-test-001"}
        r = self.store.perform("import_capture", payload)
        self.assertEqual(r, self.store.perform("import_capture", payload))
        self.assertEqual(self.store.read_file(r["source_path"]).read_bytes(), raw.encode())
        self.assertEqual(self.store.read_file(r["files"][0]["path"]).read_bytes(), b"print(2)\r\n")
        q = self.store.detail(r["question_id"])
        self.assertEqual(q["status"], "discussing")
        self.assertEqual(q["runs"], [])
        self.assertEqual(q["judgments"], [])
        self.assertEqual(len(q["discussions"]), 1)
        self.assertEqual(q["counts"]["codes"], 1)
        self.assertEqual(q["counts"]["results"], 1)
        self.assertEqual(q["criterion"], "误差阶在 1.98 到 2.02。")

    def test_followup_reuses_route_preserves_body_and_versions_files(self):
        first = self.save()
        body = self.store.detail(first["question_id"])["body"]
        second = self.save(rid="capture-test-002")
        self.assertEqual(first["question_id"], second["question_id"])
        self.assertEqual(second["counts"]["frameworks"], 0)
        self.assertEqual(second["counts"]["contents"], 0)
        self.assertEqual(second["counts"]["questions"], 0)
        q = self.store.detail(first["question_id"])
        self.assertEqual(q["body"], body)
        self.assertEqual(q["counts"]["codes"], 2)
        self.assertEqual(q["counts"]["results"], 2)
        self.assertEqual(len(q["discussions"]), 2)
        self.assertEqual(second["files"][0]["name"], "central_difference_v2.py")

    def test_conflicting_context_requires_choice_and_can_resolve_new_or_current(self):
        first = self.save()
        payload = {"text": TEXT.replace("中心差分是否二阶收敛？", "前向差分是否一阶收敛？"), "question_id": first["question_id"], "framework_id": first["framework_id"]}
        p = self.store.perform("preview_capture", payload)
        self.assertFalse(p["can_import"])
        self.assertTrue(any(c["level"] == "question" for c in p["needs_choice"]))
        before = self.files()
        with self.assertRaises(ValueError):
            self.store.perform("import_capture", {**payload, "token": p["token"], "request_id": "capture-conflict"})
        self.assertEqual(before, self.files())
        chosen = self.store.perform("preview_capture", {**payload, "question_title": "中心差分是否二阶收敛？"})
        self.assertTrue(chosen["can_import"])
        self.assertEqual(chosen["plan"]["question"]["id"], first["question_id"])
        payload.pop("question_id")
        new = self.save(rid="capture-new-001", **{k: v for k, v in payload.items()})
        self.assertNotEqual(new["question_id"], first["question_id"])

    def test_duplicate_existing_name_and_multiple_questions_are_ambiguous(self):
        first = self.save()
        self.store.perform("create_question", {"content_id": first["content_id"], "title": "中心差分是否二阶收敛？"})
        p = self.store.perform("preview_capture", {"text": TEXT})
        self.assertFalse(p["can_import"])
        self.assertEqual(len(p["needs_choice"][0]["options"]), 2)
        p = self.store.perform("preview_capture", {"text": TEXT + "\n研究问题：另一个问题？\n"})
        self.assertFalse(p["can_import"])

    def test_attachments_and_bad_paths_validate_before_mutation(self):
        file = {"filename": "actual.csv", "base64": base64.b64encode(b"x,e\n1,0\n").decode()}
        r = self.save(files=[file])
        self.assertEqual(r["counts"]["results"], 2)
        before = self.files()
        for name in ("../bad.py", "C:\\bad.py", "问题.md"):
            with self.assertRaises(ValueError):
                self.store.perform("preview_capture", {"text": TEXT, "files": [{**file, "filename": name}]})
        with self.assertRaises(ValueError):
            self.store.perform("preview_capture", {"text": TEXT.replace("central_difference.py", "../bad.py")})
        with self.assertRaises(ValueError):
            self.store.perform("preview_capture", {"text": TEXT + "<!-- field:raw -->"})
        self.assertEqual(before, self.files())

    def test_stale_preview_and_failed_write_leave_no_partial_research(self):
        p = self.store.perform("preview_capture", {"text": TEXT})
        self.store.perform("save_framework", {"title": "其他框架"})
        before = self.files()
        with self.assertRaises(ConflictError):
            self.store.perform("import_capture", {"text": TEXT, "token": p["token"], "request_id": "capture-stale"})
        self.assertEqual(before, self.files())
        p = self.store.perform("preview_capture", {"text": TEXT})
        original, count = self.store._atomic, 0
        def fail(path, data, **kwargs):
            nonlocal count
            count += 1
            if count == 7:
                raise OSError("injected")
            return original(path, data, **kwargs)
        self.store._atomic = fail
        try:
            with self.assertRaises(OSError):
                self.store.perform("import_capture", {"text": TEXT, "token": p["token"], "request_id": "capture-rollback"})
        finally:
            self.store._atomic = original
        self.assertEqual(before, self.files())

    def test_unfinished_or_unknown_fence_does_not_create_false_route(self):
        raw = "用户：什么是差分？\n```mystery\n研究问题：这只是围栏内容\n"
        p = self.store.perform("preview_capture", {"text": raw})
        self.assertEqual(p["plan"]["question"]["title"], "什么是差分？")
        self.assertEqual(p["files"], [])
        self.assertTrue(p["warnings"])

    def test_exact_existing_child_resolves_parent_before_active_framework(self):
        first = self.save()
        other = self.store.perform("save_framework", {"title": "另一个当前框架", "status": "active"})
        raw = "研究内容：导数近似与收敛阶\n研究问题：中心差分是否二阶收敛？\n用户：继续核查上次结果。"
        p = self.store.perform("preview_capture", {"text": raw})
        self.assertTrue(p["can_import"])
        self.assertEqual(p["plan"]["framework"]["id"], first["framework_id"])
        self.assertNotEqual(p["plan"]["framework"]["id"], other["id"])
        self.assertEqual(p["plan"]["content"]["id"], first["content_id"])
        self.assertEqual(p["plan"]["question"]["id"], first["question_id"])
        p = self.store.perform("preview_capture", {"text": "研究问题：中心差分是否二阶收敛？\n继续核查。"})
        self.assertEqual(p["plan"]["framework"]["id"], first["framework_id"])
        self.assertEqual(p["plan"]["question"]["id"], first["question_id"])


if __name__ == "__main__":
    unittest.main()
