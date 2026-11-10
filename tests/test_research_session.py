from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from notebook.store import Store
from tools.prepare_research_session import prepare, ReadOnlyStore


class ResearchSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="research-session-", dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.store = Store(self.root)
        self.chapter = self.store.perform("save_content", {"title": "第二章 理论基础", "objective": "检验最小模型"})
        self.frame = self.store.perform("save_framework", {"title": "从模型到验证", "content_ids": [self.chapter["id"]]})
        self.q = self.store.perform("create_question", {"content_id": self.chapter["id"], "title": "已知偏差能否消去？", "criterion": "绝对误差小于 1e-8"})
        self.store.perform("save_body", {"question_id": self.q["id"], "revision": self.q["body_revision"], "text": "## 假设\n\ny=x+b，b 已知。\n\n## 推导\n\ny-b=x。"})

    def prepare(self, scope="question", rid=None, **kwargs):
        return prepare(self.root, scope, rid or self.q["id"], "检验消去偏差的条件", store=self.store, **kwargs)

    def files(self):
        return {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}

    def test_snapshot_hash_scope_and_no_existing_changes(self):
        other = self.store.perform("create_question", {"content_id": self.chapter["id"], "title": "无关的其他小问题"})
        d = self.store.perform("save_discussion", {"question_id": self.q["id"], "ai": "GPT", "raw": "必须说明 b 已知，未知 b 时不成立。", "title": "边界"})
        self.store.perform("save_judgment", {"question_id": self.q["id"], "decision": "supported", "text": "只在已知固定偏差模型内成立", "scope": "常数 b", "evidence_ids": [d["id"]]})
        before = self.files()
        directory = self.prepare()
        self.assertTrue((directory / "讨论").is_dir())
        self.assertTrue((directory / "候选正文").is_dir())
        self.assertTrue((directory / "审查").is_dir())
        for name, data in before.items():
            self.assertEqual((self.root / name).read_bytes(), data, name)
        manifest = json.loads((directory / "来源清单.json").read_text(encoding="utf-8"))
        self.assertFalse(manifest["model_called"])
        self.assertFalse(manifest["research_executed"])
        for entry in manifest["sources"]:
            self.assertEqual(entry["sha256"], hashlib.sha256((self.root / entry["path"]).read_bytes()).hexdigest())
        background = (directory / "背景快照.md").read_text(encoding="utf-8")
        self.assertIn("b 已知", background)
        self.assertIn("未知 b 时不成立", background)
        self.assertIn("只在已知固定偏差模型内成立", background)
        self.assertNotIn(other["title"], background)

    def test_framework_and_chapter_scope(self):
        framework = self.prepare("framework", self.frame["id"])
        chapter = self.prepare("chapter", self.chapter["id"])
        self.assertIn(self.q["title"], (framework / "背景快照.md").read_text(encoding="utf-8"))
        self.assertIn(self.q["criterion"], (chapter / "背景快照.md").read_text(encoding="utf-8"))
        self.assertNotIn("y-b=x", (framework / "背景快照.md").read_text(encoding="utf-8"))

    def test_shared_chapter_records_all_or_explicit_valid_framework(self):
        second = self.store.perform("save_framework", {"title": "从证据回查理论", "content_ids": [self.chapter["id"]]})
        directory = self.prepare()
        manifest = json.loads((directory / "来源清单.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["framework_ids"], [self.frame["id"], second["id"]])
        self.assertIn("不得擅自选定", (directory / "背景快照.md").read_text(encoding="utf-8"))
        explicit = self.prepare(framework_id=second["id"])
        m = json.loads((explicit / "来源清单.json").read_text(encoding="utf-8"))
        self.assertEqual(m["framework_ids"], [second["id"]])
        self.assertEqual(m["framework_selection"], "explicit")
        wrong = self.store.perform("save_framework", {"title": "另一框架", "content_ids": []})
        with self.assertRaises(ValueError):
            self.prepare(framework_id=wrong["id"])

    def test_invalid_scope_ids_and_missing_record_create_no_session(self):
        for scope, rid in (("other", self.q["id"]), ("question", "../../00_项目卡.md"),
                           ("question", "C01-Q01/extra"), ("chapter", "C99")):
            with self.subTest(scope=scope, rid=rid), self.assertRaises(ValueError):
                self.prepare(scope, rid)
        self.assertFalse((self.root / "04_交接包/研究会话").exists())

    def test_collision_never_overwrites_prior_session(self):
        class Fixed:
            hex = "f" * 32
        with patch("tools.prepare_research_session.uuid.uuid4", return_value=Fixed()):
            directory = self.prepare()
            before = (directory / "任务.md").read_bytes()
            # Force the same timestamp as well, including a boundary second.
            class FrozenTime:
                @classmethod
                def now(cls, tz=None):
                    from datetime import datetime
                    return datetime.strptime(directory.name.split("_")[0], "%Y%m%d-%H%M%S")
            with patch("tools.prepare_research_session.datetime", FrozenTime), self.assertRaises(RuntimeError):
                self.prepare()
            self.assertEqual((directory / "任务.md").read_bytes(), before)

    def test_offline_reader_does_not_initialize_or_rewrite_sources(self):
        before = self.files()
        with patch("tools.prepare_research_session.LocalClient.get", side_effect=OSError("offline")):
            directory = prepare(self.root, "question", self.q["id"], "只审查推导")
        self.assertTrue(directory.is_dir())
        for name, data in before.items():
            self.assertEqual((self.root / name).read_bytes(), data, name)
        reader = ReadOnlyStore(self.root)
        with self.assertRaises(RuntimeError):
            reader._atomic("00_项目卡.md", b"replacement")

    def test_materials_are_hashed_and_indexed_without_loading_code_into_context(self):
        question_dir = (self.root / self.q["path"]).parent
        code = question_dir / "代码/verify.py"
        code.write_text("print('code-only-marker')\n", encoding="utf-8")
        result = question_dir / "结果/result.bin"
        result.write_bytes(b"\x00\xff\x03")
        directory = self.prepare()
        manifest = json.loads((directory / "来源清单.json").read_text(encoding="utf-8"))
        entries = {entry["path"]: entry for entry in manifest["sources"]}
        for path in (code, result):
            entry = entries[path.relative_to(self.root).as_posix()]
            self.assertFalse(entry["embedded"])
            self.assertEqual(entry["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertNotIn("code-only-marker", (directory / "背景快照.md").read_text(encoding="utf-8"))

    def test_matching_local_server_is_preferred_without_offline_initialization(self):
        store = self.store
        root = self.root
        calls = []

        class Client:
            def __init__(self, url):
                self.url = url

            def get(self, path):
                calls.append(self.url)
                return {"app": "research-file-system", "root": str(root if self.url.endswith("8788") else root / "other")}

            def state(self):
                return store.state()

            def detail(self, rid):
                return store.detail(rid)

            def content_detail(self, rid):
                return store.content_detail(rid)

        with patch("tools.prepare_research_session.LocalClient", Client), patch("tools.prepare_research_session.WorkspaceLock", side_effect=AssertionError("offline lock should not be used")):
            directory = prepare(self.root, "question", self.q["id"], "核对推导")
        self.assertTrue(directory.is_dir())
        self.assertEqual(len(calls), 2)

    def test_stale_source_revision_rejects_packet(self):
        stale = self.store.detail(self.q["id"])
        (self.root / stale["body_path"]).write_text("来源被外部编辑器修改", encoding="utf-8")
        with patch.object(self.store, "detail", return_value=stale), self.assertRaisesRegex(ValueError, "来源已修改"):
            self.prepare()
        self.assertFalse((self.root / "04_交接包/研究会话").exists())


if __name__ == "__main__":
    unittest.main()
