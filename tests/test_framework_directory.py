"""Readable framework files preserve the same hierarchy as the UI."""
import base64
import tempfile
import unittest
from pathlib import Path
from urllib.parse import unquote

from notebook.store import Store


class FrameworkDirectoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="framework-directory-")
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name))

    def test_directory_preserves_authored_fields_and_returned_revision(self):
        content = self.store.perform("save_content", {"title": "模型建立"})
        frame = self.store.perform("save_framework", {"title": "候选主线", "mainline": "我的原始主线", "content_ids": [content["id"]]})
        self.assertEqual(frame["revision"], self.store._revision(frame["path"]))
        updated = self.store.perform("save_framework", {"id": frame["id"], "revision": frame["revision"], "risks": "待核查边界"})
        self.assertEqual(updated["mainline"], "我的原始主线")
        self.assertEqual(updated["risks"], "待核查边界")
        source = self.store.read_file(frame["path"]).read_text(encoding="utf-8")
        self.assertEqual(source.count("## 研究目录"), 1)
        self.assertIn("### 1. [模型建立]", source)
        self.assertIn("尚未建立研究问题", source)
        self.store.perform("sync", {})
        self.assertEqual(source, self.store.read_file(frame["path"]).read_text(encoding="utf-8"))

    def test_question_materials_are_nested_under_correct_content(self):
        c1 = self.store.perform("save_content", {"title": "模型"})
        c2 = self.store.perform("save_content", {"title": "验证"})
        q = self.store.perform("create_question", {"content_id": c1["id"], "title": "边界能否成立？"})
        self.store.perform("import_materials", {
            "request_id": "directory-test-001", "question_id": q["id"], "files": [
                {"filename": "verify.py", "kind": "code", "base64": base64.b64encode(b"print(1)").decode()},
                {"filename": "结果.csv", "kind": "result", "base64": base64.b64encode(b"x,y\n1,1\n").decode()},
            ],
        })
        f = self.store.perform("save_framework", {"title": "先验证后建模", "content_ids": [c2["id"], c1["id"]]})
        source = self.store.read_file(f["path"]).read_text(encoding="utf-8")
        self.assertLess(source.index("### 1. [验证]"), source.index("### 2. [模型]"))
        self.assertIn("#### 2.1 [边界能否成立？]", source)
        self.assertIn("- 代码：[verify.py]", source)
        self.assertIn("- 结果：\n  - 待关联运行\n    - [结果.csv]", source)
        self.assertIn("- 状态：讨论中", source)
        import re
        for relative in re.findall(r"\]\(([^)]+)\)", source):
            resolved = (self.store.read_file(f["path"]).parent / unquote(relative)).resolve()
            self.assertTrue(resolved.is_file(), str(resolved))
            self.assertTrue(resolved.is_relative_to(self.store.root))


if __name__ == "__main__":
    unittest.main()
