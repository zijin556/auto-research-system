"""Transport tests use an isolated store; they do not touch real research files."""
import http.client
import base64
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

from notebook import server


class ConflictError(ValueError):
    pass


class StubStore:
    def __init__(self, root):
        self.root = root
        self.calls = []

    def state(self):
        return {"root": str(self.root), "project": {"title": "新项目"}, "frameworks": [], "contents": [], "questions": []}

    def detail(self, qid):
        return {"id": qid, "title": "问题", "discussions": [], "runs": []}

    def content_detail(self, cid):
        return {"id": cid, "title": "研究内容", "questions": []}

    def perform(self, action, data):
        self.calls.append((action, data))
        if action == "conflict":
            raise ConflictError("正文已被其他编辑器修改")
        if action == "invalid":
            raise ValueError("缺少字段")
        if action == "unexpected":
            raise RuntimeError("private internal detail")
        return {"ok": True, "action": action, "data": data}

    def read_file(self, relative):
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root) or any(part.startswith(".") for part in Path(relative).parts):
            raise PermissionError("超出范围")
        if not path.is_file():
            raise FileNotFoundError("文件不存在")
        return path


class TransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="research-http-")
        cls.root = Path(cls.temp.name).resolve()
        cls.static = cls.root / "static"
        cls.static.mkdir()
        (cls.static / "index.html").write_text("<!doctype html><title>test notebook</title>", encoding="utf-8")
        (cls.static / "app.js").write_text("'use strict';", encoding="utf-8")
        (cls.root / "记录.md").write_text("# 验证\n\n<script>alert('x')</script>", encoding="utf-8")
        (cls.root / "report.html").write_text("<script>alert('x')</script>", encoding="utf-8")
        (cls.root / "figure.svg").write_text('<svg onload="alert(1)"></svg>', encoding="utf-8")
        (cls.root / "figure.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        (cls.root / "02_研究内容").mkdir()
        cls.static_patch = patch.object(server, "STATIC", cls.static)
        cls.static_patch.start()
        cls.store = StubStore(cls.root)
        cls.http = server.NotebookServer(("127.0.0.1", 0), cls.store)
        cls.thread = threading.Thread(target=cls.http.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.http.server_port

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.http.server_close()
        cls.thread.join(timeout=3)
        cls.static_patch.stop()
        cls.temp.cleanup()

    def request(self, method="GET", path="/api/health", data=None, headers=None, raw=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        payload = raw
        defaults = {}
        if data is not None:
            payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        if method == "POST":
            defaults = {"X-Research-Notebook": "1", "Content-Type": "application/json"}
        defaults.update(headers or {})
        try:
            connection.request(method, path, body=payload, headers=defaults)
            response = connection.getresponse()
            return response.status, {key.lower(): value for key, value in response.getheaders()}, response.read()
        finally:
            connection.close()

    def test_health_state_and_ids(self):
        status, headers, body = self.request()
        health = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(health["app"], "research-file-system")
        self.assertEqual(Path(health["root"]), self.root)
        self.assertEqual(headers["x-content-type-options"], "nosniff")
        self.assertNotIn("access-control-allow-origin", headers)
        for path, expected in (("/api/question?id=C01-Q01", "C01-Q01"), ("/api/content?id=C01", "C01")):
            self.assertEqual(json.loads(self.request(path=path)[2])["id"], expected)
        self.assertFalse(json.loads(self.request(path="/api/state")[2])["is_example"])

    def test_cross_site_and_foreign_host_are_rejected_before_store(self):
        cases = [
            ("GET", {"Host": "evil.example"}),
            ("GET", {"Origin": "https://evil.example"}),
            ("GET", {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "cors"}),
            ("POST", {"Origin": "https://evil.example"}),
            ("POST", {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate"}),
            ("POST", {"X-Research-Notebook": ""}),
        ]
        for method, headers in cases:
            with self.subTest(method=method, headers=headers):
                before = len(self.store.calls)
                result = self.request(method, "/api/action", data={"action": "save_project"} if method == "POST" else None, headers=headers)
                self.assertEqual(result[0], 403)
                self.assertEqual(len(self.store.calls), before)

    def test_same_origin_write_passes_exact_payload(self):
        payload = {"action": "save_discussion", "question_id": "C01-Q01", "raw": "【主题】核查\n原始全文"}
        status, _, body = self.request("POST", "/api/action", data=payload, headers={"Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(status, 200)
        result = json.loads(body)
        self.assertEqual(result["action"], "save_discussion")
        self.assertEqual(result["data"]["raw"], payload["raw"])
        self.assertNotIn("action", result["data"])

    def test_conflict_and_error_statuses(self):
        for action, expected in (("conflict", 409), ("invalid", 400), ("unexpected", 500)):
            with self.subTest(action=action):
                with patch.object(server.LOG, "exception"):
                    status, _, body = self.request("POST", "/api/action", data={"action": action})
                self.assertEqual(status, expected)
                if action == "unexpected":
                    self.assertNotIn(b"private internal detail", body)

    def test_json_shape_encoding_and_content_type(self):
        for raw, headers in ((b"[]", {}), (b"not-json", {}), (b"\xff", {}), (b"{}", {"Content-Type": "text/plain"})):
            with self.subTest(raw=raw, headers=headers):
                self.assertEqual(self.request("POST", "/api/action", raw=raw, headers=headers)[0], 400)
        self.assertEqual(self.request("POST", "/api/action", data={})[0], 400)

    def test_size_limit_rejects_body_without_dispatch(self):
        before = len(self.store.calls)
        with patch.object(server, "MAX_BODY", 32):
            status, _, _ = self.request("POST", "/api/action", raw=b" " * 33)
        self.assertEqual(status, 400)
        self.assertEqual(len(self.store.calls), before)
        status, _, _ = self.request("POST", "/api/action", raw=b"", headers={"Content-Length": "-1"})
        self.assertEqual(status, 400)

    def test_source_text_is_not_interpreted_as_html(self):
        status, headers, body = self.request(path="/api/file?path=" + quote("记录.md"))
        self.assertEqual(status, 200)
        self.assertTrue(headers["content-type"].startswith("text/plain"))
        self.assertIn(b"<script>", body)
        self.assertEqual(headers["x-content-type-options"], "nosniff")

    def test_html_svg_and_explicit_download_are_attachments(self):
        for filename in ("report.html", "figure.svg", "记录.md"):
            with self.subTest(filename=filename):
                suffix = "&download=1" if filename.endswith(".md") else ""
                status, headers, _ = self.request(path="/api/file?path=" + quote(filename) + suffix)
                self.assertEqual(status, 200)
                self.assertEqual(headers["content-type"], "application/octet-stream")
                self.assertTrue(headers["content-disposition"].startswith("attachment;"))

    def test_images_and_static_resources_have_specific_types(self):
        self.assertEqual(self.request(path="/api/file?path=figure.png")[1]["content-type"], "image/png")
        self.assertEqual(self.request(path="/static/app.js")[1]["content-type"], "application/javascript")
        self.assertEqual(self.request(path="/")[1]["content-type"], "text/html")

    def test_encoded_static_traversal_hidden_and_bad_routes(self):
        for route in ("/static/../report.html", "/static/%2e%2e/report.html", "/static/%5c..%5creport.html", "/static/.secret.js", "/static/C%3A/test.js"):
            with self.subTest(route=route):
                self.assertEqual(self.request(path=route)[0], 403)
        self.assertEqual(self.request(path="/missing")[0], 404)
        self.assertEqual(self.request(path="/api/file?path=../outside.md")[0], 403)

    def test_open_folder_stays_inside_registered_research_roots(self):
        for path in ("../", "static", "C:/Windows", "02_研究内容/../../", ".runtime"):
            with self.subTest(path=path):
                self.assertEqual(self.request("POST", "/api/action", data={"action": "open_folder", "path": path})[0], 403)
        with patch.object(server.os, "startfile", create=True) as startfile:
            status, _, body = self.request("POST", "/api/action", data={"action": "open_folder", "path": "02_研究内容"})
            self.assertEqual(status, 200)
            self.assertEqual(Path(json.loads(body)["path"]), self.root / "02_研究内容")
            if server.os.name == "nt":
                startfile.assert_called_once()


class RealStoreFlowTests(unittest.TestCase):
    def setUp(self):
        from notebook.store import Store
        self.temp = tempfile.TemporaryDirectory(prefix="research-flow-")
        self.root = Path(self.temp.name).resolve()
        self.store = Store(self.root)
        self.http = server.NotebookServer(("127.0.0.1", 0), self.store)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.http.server_port

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(timeout=3)
        self.temp.cleanup()

    def request(self, route, payload=None, expected=200):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=8)
        try:
            method = "GET" if payload is None else "POST"
            body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers = {"Content-Type": "application/json", "X-Research-Notebook": "1"}
            connection.request(method, route, body, headers)
            response = connection.getresponse()
            raw = response.read()
            self.assertEqual(response.status, expected, raw.decode("utf-8", errors="replace"))
            return json.loads(raw) if response.getheader("Content-Type", "").startswith("application/json") else raw
        finally:
            connection.close()

    def action(self, action, expected_status=200, **data):
        return self.request("/api/action", {"action": action, **data}, expected_status)

    def test_question_evidence_and_draft_flow_over_http(self):
        initial = self.request("/api/state")
        self.assertEqual(initial["questions"], [])
        self.action("save_project", revision=initial["project"]["revision"], title="接口验收样例", brief="仅软件验收合成资料")
        content = self.action("save_content", title="固定偏差", objective="核对加性模型")
        first = self.action("save_framework", title="先理论后核查", mainline="推导→核查", status="active", content_ids=[content["id"]])
        self.action("save_framework", title="先现象后模型", status="candidate", content_ids=[content["id"]])
        question = self.action("create_question", content_id=content["id"], title="已知偏差可否校正", question="x_hat=y-b 是否恢复x", criterion="误差小于1e-8")
        qid = question["id"]
        detail = self.request("/api/question?id=" + qid)
        self.action("save_body", question_id=qid, revision=detail["body_revision"], text="# 测试推导\n\nx_hat=y-b=x\n\n假设b准确已知。")
        discussion = self.action("save_discussion", question_id=qid, ai="验收夹具", raw="【主题】模型核查\n【要点】固定偏差须准确已知\n【遗留问题】核对实现")
        self.assertEqual(discussion["summary"], "固定偏差须准确已知")
        plan = self.action("save_plan", question_id=qid, title="最小核查", method="simulation", target="检查公式实现", criterion="误差小于1e-8", inputs="x=10,b=2", expected="x_hat=10", source_ids=[discussion["id"], "body"])
        code = self.action("add_code", question_id=qid, filename="check.py", purpose="核对已知偏差校正", text="# 验收夹具\nx=10\nb=2\ny=x+b\nx_hat=y-b\n")
        run = self.action("create_run", question_id=qid, title="首次实现检查", plan_id=plan["id"], code_paths=[code["path"]], command="python check.py", mapping="式1 → check.py L4–L5")
        prepared = self.action("generate_prompt", question_id=qid, template="P7", source_ids=[run["id"], "body"])
        self.assertIn("公式 ↔ 代码行号对照表", prepared["text"])
        self.assertIn("本次已登记运行：" + run["id"], prepared["text"])
        self.assertTrue((self.root / prepared["path"]).is_file())
        saved = self.action("update_run", question_id=qid, id=run["id"], revision=run["revision"], actual="x_hat=10, error=0", outcome="pass", mapping="式1 → check.py L4–L5")
        attachment_bytes = b"x_hat,error\n10,0\n"
        attachment = self.action("attach_file", question_id=qid, run_id=run["id"], filename="metrics.csv", base64=base64.b64encode(attachment_bytes).decode("ascii"))
        self.assertEqual(self.request("/api/file?path=" + quote(attachment["path"])), attachment_bytes)
        current = self.request("/api/question?id=" + qid)
        self.assertEqual(current["status"], "needs_judgment")
        self.assertTrue(saved["code_snapshots"])
        judgment = self.action("save_judgment", question_id=qid, decision="supported", text="在已知固定偏差模型与本输入上支持校正", evidence_ids=[run["id"], "body"], scope="仅此模型与合成输入", meaning="用于实现核对")
        self.assertEqual(self.request("/api/question?id=" + qid)["status"], "supported")
        cdetail = self.request("/api/content?id=" + content["id"])
        drafted = self.action("append_draft", content_id=content["id"], question_id=qid, revision=cdetail["draft_revision"], text="在所列假设与合成输入下，校正数值与预期一致。")
        self.assertIn(qid, drafted["draft"])
        self.assertIn(judgment["id"], drafted["draft"])
        self.assertIn("仅此模型与合成输入", drafted["draft"])
        self.action("save_body", expected_status=409, question_id=qid, revision=detail["body_revision"], text="不可用旧页面覆盖")
        self.action("create_run", question_id=qid, title="补充输入核查", plan_id=plan["id"], code_paths=[code["path"]])
        reopened = self.request("/api/question?id=" + qid)
        self.assertEqual(reopened["status"], "running")
        self.assertEqual(len(reopened["runs"]), 2)
        self.assertEqual(len(reopened["judgments"]), 1)
        state = self.request("/api/state")
        self.assertEqual(len(state["contents"]), 1)
        self.assertEqual(len(state["frameworks"]), 2)
        self.assertEqual(next(f["id"] for f in state["frameworks"] if f["status"] == "active"), first["id"])
        comparison = self.action("generate_prompt", template="P10")
        self.assertIn("先理论后核查", comparison["text"])
        self.assertIn("先现象后模型", comparison["text"])


if __name__ == "__main__":
    unittest.main()
