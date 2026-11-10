from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path

from notebook.store import ConflictError, Store


OUTLINE = """# 主线一
先建立模型，再分析边界。
## 模型建立
研究目标说明。
### 理想条件下是否成立？
保留完整问题描述。
## 边界分析
### 误差是否可控？
"""


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="imports-test-", dir=Path(__file__).resolve().parent)
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name))

    def files(self):
        return {p.relative_to(self.store.root).as_posix(): p.read_bytes() for p in self.store.root.rglob("*") if p.is_file() and not any(part.startswith(".") for part in p.relative_to(self.store.root).parts)}

    def question(self):
        c = self.store.perform("save_content", {"title": "研究"})
        return self.store.perform("create_question", {"content_id": c["id"], "title": "核查模型"})

    def material(self, name="sim.py", kind="code", payload=b"print(1)\n"):
        return {"filename": name, "kind": kind, "base64": base64.b64encode(payload).decode()}

    def test_preview_read_only_and_batch_import_receipt(self):
        before = self.files()
        preview = self.store.perform("preview_outline", {"text": OUTLINE})
        self.assertEqual(before, self.files())
        self.assertEqual(preview["counts"], {"frameworks": 1, "contents": 2, "questions": 2})
        data = {"text": OUTLINE, "token": preview["token"], "request_id": "outline-1234"}
        first = self.store.perform("import_outline", data)
        second = self.store.perform("import_outline", data)
        self.assertEqual(first, second)
        self.assertEqual(len(self.store.state()["contents"]), 2)
        self.assertEqual(self.store.read_file(first["source_path"]).read_text(encoding="utf-8"), OUTLINE)
        self.assertEqual(self.store.state()["questions"][0]["question"], "保留完整问题描述。")
        with self.assertRaises(ConflictError):
            self.store.perform("import_outline", {**data, "text": OUTLINE + "changed"})

    def test_append_existing_and_stale_preview_has_no_new_records(self):
        f = self.store.perform("save_framework", {"title": "保留框架", "mainline": "原主线"})
        text = "## 内容\n### 一个问题\n"
        preview = self.store.perform("preview_outline", {"text": text, "framework_id": f["id"]})
        self.store.perform("save_framework", {"id": f["id"], "revision": f["revision"], "mainline": "修正主线"})
        data = {"text": text, "framework_id": f["id"], "token": preview["token"], "request_id": "append-1234"}
        before = self.files()
        with self.assertRaises(ConflictError):
            self.store.perform("import_outline", data)
        self.assertEqual(before, self.files())
        data["token"] = self.store.perform("preview_outline", data)["token"]
        result = self.store.perform("import_outline", data)
        self.assertEqual(self.store.perform("import_outline", data), result)
        self.assertEqual(len(self.store.state()["frameworks"]), 1)
        self.assertEqual(self.store.state()["frameworks"][0]["mainline"], "修正主线")

    def test_bad_outline_is_rejected_before_writes_and_preamble_kept(self):
        before = self.files()
        for text in ("### 没有上级", "# 框架\n## 内容\n### 好问题\n<!-- field:question -->"):
            with self.assertRaises(ValueError):
                self.store.perform("preview_outline", {"text": text})
        self.assertEqual(before, self.files())
        preview = self.store.perform("preview_outline", {"text": "前言必须保留\n" + OUTLINE})
        self.assertIn("前言必须保留", preview["outline"][0]["mainline"])
        self.assertTrue(preview["warnings"])

    def test_materials_validate_entire_batch_and_version_duplicate_names(self):
        q = self.question()
        data = {"question_id": q["id"], "request_id": "materials-1234", "files": [self.material(), self.material("../bad.csv", "result")]}
        before = self.files()
        with self.assertRaises(ValueError):
            self.store.perform("import_materials", data)
        self.assertEqual(before, self.files())
        data["files"] = [self.material(), self.material(), self.material("metrics.csv", "result", b"x,error\n1,0\n")]
        result = self.store.perform("import_materials", data)
        self.assertEqual(result, self.store.perform("import_materials", data))
        self.assertEqual([f["name"] for f in result["files"]], ["sim.py", "sim_v2.py", "metrics.csv"])
        detail = self.store.detail(q["id"])
        self.assertEqual(detail["counts"]["codes"], 2)
        self.assertEqual(detail["counts"]["results"], 1)
        self.assertEqual(detail["artifacts"]["results"][0]["run_id"], "")
        self.assertEqual(detail["runs"], [])
        self.assertEqual(detail["status"], "discussing")
        cache = self.store.root / Path(q["path"]).parent / "代码/__pycache__/sim.pyc"
        cache.parent.mkdir()
        cache.write_bytes(b"not a source program")
        self.assertEqual(self.store.detail(q["id"])["counts"]["codes"], 2)

    def test_actual_results_exclude_system_snapshots_and_link_run(self):
        q = self.question()
        code = self.store.perform("add_code", {"question_id": q["id"], "filename": "sim.py", "purpose": "核查", "text": "print(1)"})
        plan = self.store.perform("save_plan", {"question_id": q["id"], "target": "核查", "criterion": "误差为0"})
        run = self.store.perform("create_run", {"question_id": q["id"], "plan_id": plan["id"], "code_paths": [code["path"]]})
        result = self.store.perform("import_materials", {"question_id": q["id"], "run_id": run["id"], "request_id": "runfiles-1234", "files": [self.material("metric.csv", "result", b"1,0")]})
        detail = self.store.detail(q["id"])
        self.assertEqual(detail["counts"]["codes"], 1)
        self.assertEqual(detail["counts"]["results"], 1)
        self.assertEqual(detail["artifacts"]["results"][0]["run_id"], run["id"])
        self.assertEqual(detail["runs"][0]["outcome"], "pending")
        self.assertEqual(detail["status"], "running")
        self.assertEqual(result["unassigned_results"], 0)

    def test_external_results_require_review_and_new_judgment_can_resolve(self):
        q = self.question()
        d = self.store.perform("save_discussion", {"question_id": q["id"], "ai": "示例", "raw": "解析核查", "title": "推导"})
        judgment = {"question_id": q["id"], "decision": "supported", "text": "仅模型内成立", "scope": "示例假设", "evidence_ids": [d["id"]]}
        self.store.perform("save_judgment", judgment)
        self.assertEqual(self.store.detail(q["id"])["status"], "supported")
        output = self.store.root / Path(q["path"]).parent / "结果/待关联/new.csv"
        output.parent.mkdir(parents=True)
        output.write_text("1,0", encoding="utf-8")
        self.assertEqual(self.store.detail(q["id"])["status"], "review")
        self.store.perform("save_judgment", judgment)
        self.assertEqual(self.store.detail(q["id"])["status"], "supported")
        output.write_text("1,1", encoding="utf-8")
        self.assertEqual(self.store.detail(q["id"])["status"], "review")

    def test_material_batch_failure_rolls_back_prior_files(self):
        q = self.question()
        before = self.files()
        original, calls = self.store._atomic, 0
        def fail_later(path, data, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise OSError("injected write failure")
            return original(path, data, **kwargs)
        self.store._atomic = fail_later
        with self.assertRaises(OSError):
            self.store.perform("import_materials", {"question_id": q["id"], "request_id": "rollback-1234", "files": [self.material(), self.material("out.csv", "result")]})
        self.store._atomic = original
        self.assertEqual(before, self.files())
        self.assertFalse((self.store.root / ".runtime/imports/rollback-1234.json").exists())

    def test_outline_batch_failure_rolls_back_all_created_records(self):
        preview = self.store.perform("preview_outline", {"text": OUTLINE})
        before = self.files()
        original, calls = self.store._atomic, 0
        def fail_later(path, data, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 5:
                raise OSError("injected write failure")
            return original(path, data, **kwargs)
        self.store._atomic = fail_later
        with self.assertRaises(OSError):
            self.store.perform("import_outline", {"text": OUTLINE, "token": preview["token"], "request_id": "outline-rollback"})
        self.store._atomic = original
        self.assertEqual(before, self.files())
        self.assertEqual(self.store.state()["questions"], [])


if __name__ == "__main__":
    unittest.main()
