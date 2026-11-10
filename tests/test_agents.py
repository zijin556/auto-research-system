"""Tests for notebook/agents.py. No real model is called: runs go through tests/fake_cli.py."""
import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from notebook import agents
from notebook.agents import CallSpec

DEFAULT_REPLY = "结论摘要：（模拟）\n\n```台账变更\n无\n```\n"
DENY = ["read_file(*)", "write_file(*)", "command(*)", "mcp(*)", "read_url(*)", "execute_url(*)"]


def process_exited(pid: int, wait_s: float = 10.0) -> bool:
    """True when the process has ended (waits up to wait_s seconds)."""
    if os.name != "nt":
        end = time.monotonic() + wait_s
        while True:
            try:
                os.kill(pid, 0)
            except OSError:
                return True
            if time.monotonic() >= end:
                return False
            time.sleep(0.1)
    import ctypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
    kernel32.WaitForSingleObject.restype = ctypes.c_uint32
    kernel32.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    handle = kernel32.OpenProcess(0x00100000 | 0x1000, 0, pid)  # SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return True
    try:
        return kernel32.WaitForSingleObject(handle, int(wait_s * 1000)) == 0
    finally:
        kernel32.CloseHandle(handle)


class FamilyTests(unittest.TestCase):
    def test_family_of(self):
        cases = {"gpt-6.1-sol": "openai", "gpt-oss-120b-medium": "openai", "o3": "openai", "o4-mini": "openai",
                 "codex-mini-latest": "openai", "gemini-4-argon-high": "google", "gemini-3.8-flash-medium": "google",
                 "claude-opus-5-5-high": "anthropic", "claude-haiku-5-5": "anthropic", "llama-4": "unknown",
                 "omni": "unknown", "": "unknown"}
        for model, family in cases.items():
            self.assertEqual(agents.family_of(model), family, model)

    def test_buckets_and_labels(self):
        self.assertEqual(agents.bucket_of("codex", "gpt-6.1-sol"), "codex")
        self.assertEqual(agents.bucket_of("agy", "gemini-4-argon-high"), "agy:gemini")
        self.assertEqual(agents.bucket_of("agy", "claude-opus-5-5-high"), "agy:other")
        self.assertEqual(agents.bucket_of("agy", "gpt-oss-120b-medium"), "agy:other")
        self.assertEqual(agents.bucket_of("claude", "claude-opus-5-5"), "claude")
        self.assertEqual(agents.model_label("gpt-6.1-sol"), "GPT（gpt-6.1-sol）")
        self.assertEqual(agents.model_label("gemini-4-argon-high"), "Gemini（gemini-4-argon-high）")
        self.assertEqual(agents.model_label("claude-opus-5-5-high"), "Claude（claude-opus-5-5-high）")

    def test_prices(self):
        self.assertEqual(set(agents.PRICES), {"claude-fable-5-1", "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"})
        self.assertEqual(agents.PRICES["claude-fable-5-1"], {"input": 10.0, "output": 50.0})
        self.assertEqual(agents.PRICES["claude-haiku-5-5"], {"input": 0.10, "output": 0.50})


class CommandTests(unittest.TestCase):
    workdir = Path("C:/Users/researcher/AppData/Local/ResearchNotebook/调用 目录/第01轮 x")

    def codex(self, sandbox, middle, tail):
        w = self.workdir
        return (["CODEX", "exec", "--ignore-user-config", "--skip-git-repo-check", "--sandbox", sandbox,
                 "--cd", str(w), "--color", "never", "--json", "--output-last-message", str(w / "_answer.md"),
                 "--model", "gpt-6.1-sol", "-c", 'model_reasoning_effort="high"', "-c", 'model_provider="openai"',
                 "-c", 'forced_login_method="chatgpt"', "-c", 'approval_policy="never"', "-c", 'web_search="disabled"',
                 "-c", "project_doc_max_bytes=0"] + middle +
                ["-c", "features.apps=false", "-c", "features.plugins=false", "-c", "features.remote_plugin=false",
                 "-c", "features.computer_use=false", "-c", "features.browser_use=false", "-c", "features.multi_agent=false",
                 "-c", "features.memories=false", "-c", "features.code_mode=false", "-c", "features.code_mode_host=false",
                 "-c", "features.skill_mcp_dependency_install=false", "-c", "skills.bundled.enabled=false"] + tail + ["-"])

    def test_codex_ask(self):
        args, stdin = agents.command_for(CallSpec("codex", "gpt-6.1-sol", "【模板】k\n问题"), self.workdir, "CODEX")
        self.assertEqual(args, self.codex("read-only", ["-c", "features.shell_tool=false"], []))
        self.assertEqual(stdin, "【模板】k\n问题".encode("utf-8"))
        self.assertNotIn("--ephemeral", args)

    def test_codex_ask_options(self):
        spec = CallSpec("codex", "gpt-6.1-sol", "问题", effort="medium", web_search=True, system_prompt="系统说明")
        args, stdin = agents.command_for(spec, self.workdir, "CODEX")
        self.assertIn('model_reasoning_effort="medium"', args)
        self.assertIn('web_search="live"', args)
        self.assertNotIn('web_search="disabled"', args)
        self.assertEqual(stdin, "系统说明\n\n问题".encode("utf-8"))

    def test_codex_engineer(self):
        spec = CallSpec("codex", "gpt-6.1-sol", "问题", mode="engineer", web_search=True)
        args, _ = agents.command_for(spec, self.workdir, "CODEX")
        self.assertEqual(args, self.codex("workspace-write", [], ["-c", "sandbox_workspace_write.network_access=false",
                                                                  "-c", 'windows.sandbox="mxc"']))
        self.assertNotIn("features.shell_tool=false", args)
        self.assertNotIn("--ephemeral", args)

    def test_agy_ask(self):
        spec = CallSpec("agy", "gemini-4-argon-high", "【模板】k\n问题：α≥β", timeout_s=1500)
        args, stdin = agents.command_for(spec, self.workdir, "AGY")
        self.assertEqual(args, ["AGY", "--input-format", "stream-json", "--output-format", "stream-json",
                                "--model", "gemini-4-argon-high", "--disable-slash-commands", "--print-timeout", "25m",
                                "--settings", str(self.workdir / "_agy_settings.json")])
        self.assertNotIn("-p", args)
        self.assertTrue(stdin.isascii())
        self.assertEqual(stdin.count(b"\n"), 1)
        self.assertTrue(stdin.endswith(b"\n"))
        self.assertEqual(json.loads(stdin), {"event": "user", "message": {"content": "【模板】k\n问题：α≥β"}})
        short, _ = agents.command_for(CallSpec("agy", "gemini-4-argon-high", "x", timeout_s=30, effort="high"), self.workdir, "AGY")
        self.assertEqual(short[short.index("--print-timeout") + 1], "1m")
        self.assertEqual(short[short.index("--effort") + 1], "high")

    def test_claude_ask(self):
        args, stdin = agents.command_for(CallSpec("claude", "claude-opus-5-5", "【模板】k\n问题"), self.workdir, "CLAUDE")
        self.assertEqual(args, ["CLAUDE", "-p", "--output-format", "json", "--model", "claude-opus-5-5",
                                "--setting-sources", "user", "--strict-mcp-config", "--disable-slash-commands",
                                "--no-session-persistence", "--tools", "", "--permission-mode", "dontAsk",
                                "--max-budget-usd", "0.50", "--system-prompt", agents.DEFAULT_SYSTEM])
        self.assertEqual(stdin, "【模板】k\n问题".encode("utf-8"))
        spec = CallSpec("claude", "claude-haiku-5-5", "x", effort="high", budget_usd=0.05, system_prompt="只回答是或否")
        args, _ = agents.command_for(spec, self.workdir, "CLAUDE")
        self.assertEqual(args[args.index("--model") + 1], "claude-haiku-5-5")
        self.assertEqual(args[args.index("--effort") + 1], "high")
        self.assertEqual(args[args.index("--max-budget-usd") + 1], "0.05")
        self.assertEqual(args[args.index("--system-prompt") + 1], "只回答是或否")

    def test_validate_spec(self):
        self.assertEqual(agents.validate_spec(CallSpec("codex", "gpt-6.1-sol", "p")), "")
        bad = [CallSpec("agy", "gemini-4-argon-high", "p", mode="engineer"), CallSpec("claude", "claude-opus-5-5", "p", mode="engineer"),
               CallSpec("codex", "--dangerously-bypass-approvals-and-sandbox", "p"), CallSpec("codex", "gpt-6.1-sol", "p", effort='high"\nx="y'),
               CallSpec("openai", "gpt-6.1-sol", "p"), CallSpec("codex", "gpt-6.1-sol", "   "), CallSpec("codex", "", "p"),
               CallSpec("claude", "claude-opus-5-5", "p", budget_usd=0), CallSpec("codex", "gpt-6.1-sol", "p", timeout_s=0)]
        for spec in bad:
            self.assertTrue(agents.validate_spec(spec), spec)


class ClassifyTests(unittest.TestCase):
    def test_signatures(self):
        cases = {
            "You've hit your usage limit. Try again at 6:43 AM.": "quota", "You’ve hit your usage limit": "quota",
            "Quota exceeded. Check your plan and billing details.": "quota", "RESOURCE_EXHAUSTED: capacity": "quota",
            "exceeded retry limit, last status: 429 Too Many Requests": "quota", "Credit balance is too low": "quota",
            "spend limit reached": "quota", "rate limit exceeded: later": "quota", "You are out of credits": "quota",
            "unexpected status 401 Unauthorized": "not_logged_in", "Not logged in · Please run /login": "not_logged_in",
            "authentication required": "not_logged_in", "You are not logged into Antigravity.": "not_logged_in",
            "Invalid API key": "not_logged_in", "Your access token could not be refreshed": "not_logged_in",
            "stream disconnected before completion: error": "network", "Connection failed: tls": "network",
            "API Error: Unable to connect to API (ECONNREFUSED)": "network", "Request timed out": "network",
            "No response from API": "network", "Repeated 529 Overloaded errors": "network",
            "Selected model is at capacity. Please try a different model.": "network",
            "request 14012 failed with code 4291": "error", "invalid model selection (x)": "error", "": "error",
        }
        for text, expected in cases.items():
            self.assertEqual(agents.classify_error(text), expected, text)
        self.assertEqual(agents.classify_error("anything", subtype="error_max_budget_usd"), "budget")

    def test_error_text_hides_tokens(self):
        text = agents.error_text("error", "claude", "Bearer abcdefghijklmnop rejected sk-1234567890abcdef")
        self.assertNotIn("abcdefghijklmnop", text)
        self.assertNotIn("sk-1234567890abcdef", text)
        self.assertTrue(text.startswith("Claude Code 调用失败"))
        self.assertTrue(agents.error_text("not_logged_in", "codex", "401").endswith("请在终端运行 codex login"))


def jsonl(*items):
    return "\n".join(x if isinstance(x, str) else json.dumps(x, ensure_ascii=False) for x in items)


class ParserTests(unittest.TestCase):
    def test_codex_sums_turns_and_takes_the_last_agent_message(self):
        out = agents.parse_codex(jsonl(
            {"type": "thread.started", "thread_id": "t-1234567890"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "第一段"}},
            {"type": "turn.completed", "usage": {"input_tokens": 10, "cached_input_tokens": 4, "output_tokens": 3,
                                                 "reasoning_output_tokens": 1}},
            "not json", {"type": "error", "message": "Reconnecting... 1/5"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "最终答案"}},
            {"type": "turn.completed", "usage": {"input_tokens": 20, "output_tokens": 7}}))
        self.assertEqual(out["text"], "最终答案")
        self.assertEqual(out["session_id"], "t-1234567890")
        self.assertEqual(out["usage"], {"input_tokens": 30, "output_tokens": 10, "cached_input_tokens": 4,
                                        "reasoning_tokens": 1, "total_tokens": 40})
        self.assertFalse(out["failed"])

    def test_codex_turn_failed(self):
        out = agents.parse_codex(jsonl({"type": "turn.failed", "error": {"message": "boom"}}))
        self.assertTrue(out["failed"])
        self.assertEqual(out["errors"], ["boom"])

    def test_agy_falls_back_to_text_deltas(self):
        out = agents.parse_agy(jsonl(
            {"event": "init", "conversation_id": "c-1", "init": {"model": "gemini-4-argon-high"}},
            {"event": "step_update", "step_update": {"step_type": "agent_response", "state": "ACTIVE", "text_delta": "你"}},
            {"event": "step_update", "step_update": {"step_type": "agent_response", "state": "DONE", "text_delta": "好"}},
            {"event": "result", "result": {"conversation_id": "c-1", "status": "SUCCESS", "response": "",
                                           "usage": {"input_tokens": 5, "output_tokens": 2}}}))
        self.assertEqual((out["text"], out["session_id"], out["model_reported"]), ("你好", "c-1", "gemini-4-argon-high"))
        self.assertEqual(out["usage"]["total_tokens"], 7)

    def test_agy_failures(self):
        out = agents.parse_agy(jsonl({"event": "result", "result": {"status": "ERROR", "error": "RESOURCE_EXHAUSTED", "response": ""}}))
        self.assertTrue(out["failed"])
        self.assertIn("RESOURCE_EXHAUSTED", out["errors"][0])
        self.assertTrue(agents.parse_agy(jsonl({"event": "init", "conversation_id": "c", "init": {}}))["failed"])
        bare = agents.parse_agy(json.dumps({"conversation_id": "c", "status": "SUCCESS", "response": "答"}))
        self.assertEqual((bare["text"], bare["failed"]), ("答", False))

    def test_claude_main_model_and_errors(self):
        obj = {"type": "result", "subtype": "success", "is_error": False, "result": "答", "total_cost_usd": 0.02,
               "session_id": "s-1", "usage": {"input_tokens": 1, "output_tokens": 2},
               "modelUsage": {"claude-haiku-5-5": {"outputTokens": 3}, "claude-opus-5-5": {"outputTokens": 300}}}
        out = agents.parse_claude("warning: slow disk\n" + json.dumps(obj))
        self.assertEqual((out["text"], out["model_reported"], out["cost_usd"], out["session_id"]),
                         ("答", "claude-opus-5-5", 0.02, "s-1"))
        failed = agents.parse_claude(json.dumps({**obj, "is_error": True, "result": "API Error: 401"}))
        self.assertTrue(failed["failed"])
        self.assertEqual(failed["text"], "")
        self.assertIn("API Error: 401", failed["errors"])
        self.assertTrue(agents.parse_claude("not json at all")["failed"])
        self.assertFalse(agents.parse_claude("not json at all")["structured"])


class FakeEnvCase(unittest.TestCase):
    """Fake CLIs on; call and work roots in a temp folder whose path has Chinese characters and spaces."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agents-", ignore_cleanup_errors=True)
        self.base = Path(self.temp.name) / "研究 根目录"
        self.base.mkdir()
        self.replies = Path(self.temp.name) / "replies.json"
        self.set_replies({})
        env = {"RESEARCH_NOTEBOOK_FAKE_AGENTS": "1", "RESEARCH_NOTEBOOK_FAKE_REPLIES": str(self.replies)}
        self.patches = [patch.dict(os.environ, env), patch.object(agents, "call_root", lambda: self.base / "调用 记录"),
                        patch.object(agents, "work_root", lambda: self.base / "工程 目录"),
                        patch.object(agents, "codex_rate_limits", lambda *a, **k: None)]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def set_replies(self, data):
        self.replies.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def workdir(self, name="工作 目录 一"):
        return self.base / name

    @staticmethod
    def fake_record(result):
        return json.loads((result.workdir / "_fake_call.json").read_text(encoding="utf-8"))


class FakeRunTests(FakeEnvCase):
    def test_codex_ask(self):
        usage = {"input_tokens": 1000, "cached_input_tokens": 400, "output_tokens": 50, "reasoning_output_tokens": 20}
        self.set_replies({"k": {"text": "C1 结论：成立。", "usage": usage}})
        seen, limits = [], {"plan_type": "pro", "weekly": {"used_percent": 10.0, "resets_at": 1, "window_minutes": 10080}}
        with patch.object(agents, "codex_rate_limits", lambda thread_id="": seen.append(thread_id) or limits):
            result = agents.run_call(CallSpec("codex", "gpt-6.1-sol", "【模板】k\n问题：中文", workdir=self.workdir()))
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.text, "C1 结论：成立。")
        self.assertEqual(result.usage, {"input_tokens": 1000, "output_tokens": 50, "cached_input_tokens": 400,
                                        "reasoning_tokens": 20, "total_tokens": 1050})
        self.assertEqual((result.family, result.bucket, result.cost_usd, result.model_reported), ("openai", "codex", None, ""))
        self.assertEqual((result.exit_code, result.error_class, result.error), (0, "", ""))
        self.assertEqual(seen, [result.session_id])
        self.assertEqual(result.rate_limits, limits)
        self.assertEqual((result.workdir / "_answer.md").read_text(encoding="utf-8"), "C1 结论：成立。")
        self.assertIn('"thread.started"', result.stdout_path.read_text(encoding="utf-8"))
        self.assertEqual(result.stdout_path, result.workdir / "_stdout.txt")
        self.assertTrue(result.stderr_path.is_file())
        record = self.fake_record(result)
        self.assertEqual(Path(record["cwd"]).resolve(), self.workdir().resolve())
        self.assertEqual(record["prompt"], "【模板】k\n问题：中文")
        self.assertFalse(record["stdin_ascii"])
        self.assertEqual(record["argv"][0], "exec")

    def test_new_call_folder_and_minimal_reply(self):
        result = agents.run_call(CallSpec("claude", "claude-haiku-5-5", "【模板】没有这个键\n问题"))
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.workdir.parent, self.base / "调用 记录")
        self.assertEqual(result.text, DEFAULT_REPLY.strip())
        os.environ["RESEARCH_NOTEBOOK_FAKE_REPLIES"] = str(self.base / "没有这个文件.json")
        result = agents.run_call(CallSpec("agy", "gemini-4-argon-high", "【模板】k\n问题"))
        self.assertEqual(result.text, DEFAULT_REPLY.strip())

    def test_codex_engineer_writes_files(self):
        files = {"check.py": "# C04-Q01 P01 式(1)\nprint('ok')\n", "输出/summary.json": "{\"selfcheck_passed\": true}",
                 "输出/子 目录/图.csv": "a,b\n1,2\n"}
        self.set_replies({"q_verify": {"text": "已运行。", "files": files}})
        workspace = self.base / "工程 目录" / "第01轮_20261110-153000_P01"
        (workspace / "代码").mkdir(parents=True)
        result = agents.run_call(CallSpec("codex", "gpt-6.1-sol", "【模板】q_verify\n实现", mode="engineer", workdir=workspace))
        self.assertTrue(result.ok, result.error)
        for rel, content in files.items():
            self.assertEqual((workspace / rel).read_text(encoding="utf-8"), content)
        self.assertIn("workspace-write", self.fake_record(result)["argv"])

    def test_ask_mode_never_writes_reply_files(self):
        self.set_replies({"k": {"text": "好", "files": {"x.py": "print(1)"}}})
        result = agents.run_call(CallSpec("codex", "gpt-6.1-sol", "【模板】k\nx", workdir=self.workdir()))
        self.assertTrue(result.ok, result.error)
        self.assertFalse((result.workdir / "x.py").exists())

    def test_agy_ask(self):
        usage = {"input_tokens": 900, "output_tokens": 120, "thinking_tokens": 33, "cache_read_tokens": 500, "total_tokens": 1020}
        self.set_replies({"k": {"text": "审查意见：站得住。", "usage": usage}})
        result = agents.run_call(CallSpec("agy", "gemini-4-argon-high", "【模板】k\n问题：∑", workdir=self.workdir(), timeout_s=600))
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.text, "审查意见：站得住。")
        self.assertEqual(result.usage, {"input_tokens": 900, "output_tokens": 120, "cached_input_tokens": 500,
                                        "reasoning_tokens": 33, "total_tokens": 1020})
        self.assertEqual((result.model_reported, result.family, result.bucket), ("gemini-4-argon-high", "google", "agy:gemini"))
        self.assertEqual((result.rate_limits, result.cost_usd), (None, None))
        self.assertTrue(result.session_id)
        settings = json.loads((result.workdir / "_agy_settings.json").read_text(encoding="utf-8"))
        self.assertEqual(settings, {"permissions": {"deny": DENY}, "useG1Credits": False})
        record = self.fake_record(result)
        self.assertTrue(record["stdin_ascii"])
        self.assertEqual(record["prompt"], "【模板】k\n问题：∑")
        self.assertEqual(record["agy_settings"], settings)
        self.assertIn("10m", record["argv"])

    def test_claude_ask(self):
        usage = {"input_tokens": 100, "cache_read_input_tokens": 2000, "cache_creation_input_tokens": 300, "output_tokens": 80}
        self.set_replies({"k": {"text": "复推一致。", "usage": usage}})
        result = agents.run_call(CallSpec("claude", "claude-opus-5-5", "【模板】k\n问题", workdir=self.workdir(), budget_usd=0.5))
        self.assertTrue(result.ok, result.error)
        self.assertEqual((result.text, result.cost_usd, result.model_reported), ("复推一致。", 0.0012, "claude-opus-5-5"))
        self.assertEqual(result.usage, {"input_tokens": 2400, "output_tokens": 80, "cached_input_tokens": 2000,
                                        "reasoning_tokens": 0, "total_tokens": 2480})
        record = self.fake_record(result)
        self.assertFalse(record["stdin_ascii"])
        self.assertEqual(record["prompt"], "【模板】k\n问题")
        self.assertEqual(record["argv"][record["argv"].index("--max-budget-usd") + 1], "0.50")

    def test_failure_signatures(self):
        self.set_replies({kind: {"error": kind} for kind in ("quota", "not_logged_in", "network", "error", "budget")})
        for provider, model in (("codex", "gpt-6.1-sol"), ("agy", "gemini-4-argon-high"), ("claude", "claude-haiku-5-5")):
            for kind in ("quota", "not_logged_in", "network", "error"):
                with self.subTest(provider=provider, kind=kind):
                    result = agents.run_call(CallSpec(provider, model, f"【模板】{kind}\n问题"))
                    self.assertFalse(result.ok)
                    self.assertEqual((result.text, result.error_class, result.exit_code), ("", kind, 1))
                    self.assertRegex(result.error, "[\u4e00-\u9fff]")
                    self.assertTrue(result.stdout_path.is_file() and result.stderr_path.is_file())
        result = agents.run_call(CallSpec("claude", "claude-haiku-5-5", "【模板】budget\n问题", budget_usd=0.05))
        self.assertEqual(result.error_class, "budget")
        self.assertEqual(result.cost_usd, 0.06)

    def test_cancel_kills_the_process_tree(self):
        self.set_replies({"slow": {"sleep": 60, "child": 120}})
        workdir, cancel, box = self.workdir("取消 测试"), threading.Event(), {}
        spec = CallSpec("codex", "gpt-6.1-sol", "【模板】slow\n问题", workdir=workdir)
        thread = threading.Thread(target=lambda: box.update(result=agents.run_call(spec, cancel)))
        thread.start()
        pid_file, deadline = workdir / "child.pid", time.monotonic() + 30
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(pid_file.exists(), "the fake CLI did not start its child process")
        child = int(pid_file.read_text(encoding="ascii"))
        self.assertFalse(process_exited(child, 0))
        started = time.monotonic()
        cancel.set()
        thread.join(30)
        self.assertFalse(thread.is_alive())
        result = box["result"]
        self.assertEqual((result.ok, result.error_class, result.error), (False, "cancelled", "已取消"))
        self.assertLess(time.monotonic() - started, 15)
        self.assertTrue(process_exited(child, 10), "the grandchild survived the cancel")

    def test_timeout(self):
        self.set_replies({"slow": {"sleep": 60}})
        started = time.monotonic()
        result = agents.run_call(CallSpec("agy", "gemini-4-argon-high", "【模板】slow\n问题", timeout_s=1))
        self.assertEqual((result.ok, result.error_class), (False, "timeout"))
        self.assertIn("没有完成", result.error)
        self.assertLess(time.monotonic() - started, 15)

    def test_cli_ending_at_its_own_time_limit_counts_as_timeout(self):
        self.assertEqual(agents._time_limit_s(CallSpec("agy", "m", "p", timeout_s=1530)), 1500.0)
        self.assertEqual(agents._time_limit_s(CallSpec("agy", "m", "p", timeout_s=30)), 30.0)
        self.assertEqual(agents._time_limit_s(CallSpec("codex", "m", "p", timeout_s=1530)), 1530.0)
        self.set_replies({"late": {"sleep": 0.8, "error": "error"}, "early": {"error": "error"}})
        with patch.object(agents, "_time_limit_s", lambda spec: 0.5):
            late = agents.run_call(CallSpec("agy", "gemini-4-argon-high", "【模板】late\nx", timeout_s=60))
        self.assertEqual(late.error_class, "timeout")
        self.assertEqual(agents.run_call(CallSpec("agy", "gemini-4-argon-high", "【模板】early\nx")).error_class, "error")

    def test_cancel_before_start_and_invalid_spec_start_nothing(self):
        cancel = threading.Event()
        cancel.set()
        result = agents.run_call(CallSpec("codex", "gpt-6.1-sol", "【模板】k\nx", workdir=self.workdir()), cancel)
        self.assertEqual(result.error_class, "cancelled")
        self.assertFalse((result.workdir / "_fake_call.json").exists())
        result = agents.run_call(CallSpec("agy", "gemini-4-argon-high", "【模板】k\nx", mode="engineer", workdir=self.workdir("二")))
        self.assertEqual(result.error_class, "error")
        self.assertIn("Codex", result.error)
        self.assertFalse((result.workdir / "_fake_call.json").exists())
        self.assertTrue(result.stderr_path.is_file())

    def test_stale_answer_file_is_never_reused(self):
        workdir = self.workdir()
        workdir.mkdir(parents=True)
        (workdir / "_answer.md").write_text("旧答案", encoding="utf-8")
        self.set_replies({"q": {"error": "network"}})
        result = agents.run_call(CallSpec("codex", "gpt-6.1-sol", "【模板】q\nx", workdir=workdir))
        self.assertEqual((result.ok, result.text, result.error_class), (False, "", "network"))
        self.assertFalse((workdir / "_answer.md").exists())

    def test_secrets_and_parent_session_variables_are_not_passed(self):
        extra = {"OPENAI_API_KEY": "x", "ANTHROPIC_AUTH_TOKEN": "y", "ANTHROPIC_BASE_URL": "http://relay.invalid",
                 "CLAUDECODE": "1", "CLAUDE_CONFIG_DIR": "C:/elsewhere", "GEMINI_API_KEY": "z", "CODEX_THREAD_ID": "t"}
        with patch.dict(os.environ, extra):
            result = agents.run_call(CallSpec("claude", "claude-haiku-5-5", "【模板】k\nx", workdir=self.workdir()))
        self.assertTrue(result.ok, result.error)
        self.assertEqual(self.fake_record(result)["env_seen"], [])

    def test_fake_mode_provider_status(self):
        status = agents.provider_status(check_login=True)
        self.assertEqual(set(status), set(agents.PROVIDERS))
        for info in status.values():
            self.assertTrue(info["installed"])
            self.assertTrue(info["logged_in"])


class BinaryTests(unittest.TestCase):
    """Real-mode lookups with every override pointing at a missing file: nothing real is ever started."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agents-bin-", ignore_cleanup_errors=True)
        missing = str(Path(self.temp.name) / "没有" / "cli.exe")
        env = {"RESEARCH_NOTEBOOK_FAKE_AGENTS": "0", "RESEARCH_NOTEBOOK_CODEX": missing, "RESEARCH_NOTEBOOK_AGY": missing,
               "RESEARCH_NOTEBOOK_CLAUDE": missing, "CODEX_HOME": str(Path(self.temp.name) / "codex-home")}
        self.patches = [patch.dict(os.environ, env), patch.object(agents, "call_root", lambda: Path(self.temp.name) / "calls")]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def test_not_installed(self):
        for provider, model in (("codex", "gpt-6.1-sol"), ("agy", "gemini-4-argon-high"), ("claude", "claude-opus-5-5")):
            result = agents.run_call(CallSpec(provider, model, "【模板】k\nx"))
            self.assertEqual((result.ok, result.error_class, result.exit_code), (False, "not_installed", None))
            self.assertIn("没有找到", result.error)

    def test_override_must_be_an_existing_absolute_file(self):
        with patch.dict(os.environ, {"RESEARCH_NOTEBOOK_AGY": sys.executable}):
            self.assertEqual(agents.find_binary("agy"), Path(sys.executable))
        with patch.dict(os.environ, {"RESEARCH_NOTEBOOK_AGY": "python.exe"}):
            self.assertIsNone(agents.find_binary("agy"))

    def test_provider_status_without_binaries(self):
        status = agents.provider_status(check_login=True)
        for provider in agents.PROVIDERS:
            self.assertEqual((status[provider]["installed"], status[provider]["logged_in"], status[provider]["login_text"]),
                             (False, None, "未安装"))

    def test_quota_blocks_missing_clis(self):
        quota = agents.quota_status(Path(self.temp.name), agents.DEFAULT_SETTINGS)
        self.assertTrue(all(not item["available"] and item["level"] == "blocked" for item in quota.values()), quota)
        self.assertIn("Codex", quota["codex"]["text"])

    def test_version_parsing(self):
        self.assertEqual(agents._version_tuple("codex-cli 0.166.0"), (0, 166, 0))
        self.assertLess(agents._version_tuple("codex-cli 0.130.0-alpha.5")[:2], agents.CODEX_MIN_VERSION)
        self.assertGreaterEqual(agents._version_tuple("codex-cli 0.166.0")[:2], agents.CODEX_MIN_VERSION)
        self.assertEqual(agents._version_tuple("2.1.140 (Claude Code)"), (2, 1, 140))
        self.assertIsNone(agents._version_tuple("unknown"))


class FakeCliTests(unittest.TestCase):
    def run_fake(self, args, stdin, cwd, env=None):
        return subprocess.run([sys.executable, str(agents.FAKE_CLI), *args], input=stdin, capture_output=True, timeout=60,
                              cwd=cwd, env={**os.environ, "RESEARCH_NOTEBOOK_FAKE_REPLIES": "", **(env or {})})

    def test_short_flags_and_unknown_flags(self):
        with tempfile.TemporaryDirectory(prefix="fake-cli-") as temp:
            folder = Path(temp) / "目录 一"
            folder.mkdir()
            answer = folder / "答案.md"
            proc = self.run_fake(["codex", "ask", "exec", "--json", "--brand-new-flag", "-C", str(folder), "-o", str(answer),
                                  "-c", 'x="y"', "-"], "【模板】none\n问".encode("utf-8"), temp)
            self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
            self.assertEqual(answer.read_text(encoding="utf-8"), DEFAULT_REPLY)
            self.assertTrue((folder / "_fake_call.json").is_file())

    def test_argument_rules_of_the_real_clis(self):
        with tempfile.TemporaryDirectory(prefix="fake-cli-") as temp:
            proc = self.run_fake(["agy", "ask", "--input-format", "stream-json", "--output-format", "text"], b"", temp)
            self.assertEqual(proc.returncode, 2)
            self.assertIn(b"requires --output-format stream-json", proc.stderr)
            self.assertEqual(self.run_fake(["claude", "ask", "--output-format", "json"], b"x", temp).returncode, 2)
            self.assertEqual(self.run_fake(["codex", "ask", "exec", "-"], b"x", temp).returncode, 2)
            bad = self.run_fake(["codex", "ask", "exec", "--json", "-"], "中文".encode("gbk"), temp)
            self.assertEqual(bad.returncode, 1)
            self.assertIn(b"not valid UTF-8", bad.stderr)


class RateLimitTests(unittest.TestCase):
    THREAD = "01a1c06f-dffd-7cc0-b7a1-50c5883cd13a"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="codex-home-", ignore_cleanup_errors=True)
        self.home = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"CODEX_HOME": str(self.home)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def rollout(self, day, thread, lines, age_s=0):
        folder = self.home / "sessions" / "2026" / "11" / day
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"rollout-2026-11-{day}T10-00-00-{thread}.jsonl"
        path.write_text(jsonl(*lines) + "\n", encoding="utf-8")
        if age_s:
            stamp = time.time() - age_s
            os.utime(path, (stamp, stamp))
        return path

    @staticmethod
    def token_count(stamp, limits):
        return {"timestamp": stamp, "type": "event_msg",
                "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 1}}, "rate_limits": limits}}

    def test_windows_identified_by_minutes(self):
        limits = {"limit_id": "codex", "primary": {"used_percent": 12.5, "window_minutes": 300, "resets_at": 1794600000},
                  "secondary": {"used_percent": 55.0, "window_minutes": 10080, "resets_at": 1794609820},
                  "credits": {"has_credits": True, "unlimited": False, "balance": "1234.5"}, "plan_type": "pro",
                  "rate_limit_reached_type": None}
        self.rollout("09", self.THREAD, [{"timestamp": "2026-11-09T10:00:00Z", "type": "session_meta", "payload": {"id": self.THREAD}},
                                         self.token_count("2026-11-09T10:01:00.000Z", limits),
                                         self.token_count("2026-11-09T10:02:00.000Z", None), "{broken"])
        snap = agents.codex_rate_limits(self.THREAD)
        self.assertEqual(snap["weekly"], {"used_percent": 55.0, "resets_at": 1794609820, "window_minutes": 10080})
        self.assertEqual(snap["five_hour"], {"used_percent": 12.5, "resets_at": 1794600000, "window_minutes": 300})
        self.assertEqual((snap["observed_at"], snap["plan_type"], snap["reached"]), ("2026-11-09T10:01:00.000Z", "pro", None))
        self.assertEqual(snap["credits"], {"has_credits": True, "unlimited": False})
        self.assertNotIn("1234.5", json.dumps(snap))
        self.assertNotIn("balance", json.dumps(snap))

    def test_newest_files_without_thread_id(self):
        self.rollout("08", "aaaaaaaa-0000-0000-0000-000000000001",
                     [self.token_count("2026-11-08T09:00:00Z", {"primary": {"used_percent": 40.0, "window_minutes": 10080, "resets_at": 1794000000}})],
                     age_s=3600)
        self.rollout("09", "aaaaaaaa-0000-0000-0000-000000000002",
                     [self.token_count("2026-11-09T13:23:36.739Z", {"primary": {"used_percent": 87.0, "window_minutes": 10080,
                                                                               "resets_at": 1794609820}, "secondary": None, "plan_type": "pro"})])
        snap = agents.codex_rate_limits()
        self.assertEqual(snap["weekly"]["used_percent"], 87.0)
        self.assertIsNone(snap["five_hour"])
        self.assertEqual(agents.codex_rate_limits("aaaaaaaa-0000-0000-0000-000000000001")["weekly"]["used_percent"], 40.0)
        self.assertIsNone(agents.codex_rate_limits("bbbbbbbb-0000-0000-0000-000000000009"))
        self.assertIsNone(agents.codex_rate_limits("../../x"))
        # The newest file without any snapshot is skipped in favour of the next one.
        self.rollout("10", "aaaaaaaa-0000-0000-0000-000000000003", [{"type": "session_meta", "payload": {}}])
        self.assertEqual(agents.codex_rate_limits()["weekly"]["used_percent"], 87.0)

    def test_large_rollout(self):
        filler = [{"type": "response_item", "payload": {"text": "x" * 200}} for _ in range(50)]
        early = self.token_count("2026-11-09T01:00:00Z", {"primary": {"used_percent": 20.0, "window_minutes": 10080, "resets_at": 1}})
        with patch.object(agents, "ROLLOUT_TAIL", 1024):
            self.rollout("09", self.THREAD, [early] + filler)
            self.assertEqual(agents.codex_rate_limits(self.THREAD)["weekly"]["used_percent"], 20.0)  # full read fallback
            late = self.token_count("2026-11-09T02:00:00Z", {"primary": {"used_percent": 21.0, "window_minutes": 10080, "resets_at": 1}})
            self.rollout("09", self.THREAD, [early] + filler + [late])
            self.assertEqual(agents.codex_rate_limits(self.THREAD)["weekly"]["used_percent"], 21.0)

    def test_missing_home_and_fake_mode(self):
        with patch.dict(os.environ, {"CODEX_HOME": str(self.home / "没有")}):
            self.assertIsNone(agents.codex_rate_limits())
        with patch.dict(os.environ, {"RESEARCH_NOTEBOOK_FAKE_AGENTS": "1"}):
            del os.environ["CODEX_HOME"]
            self.assertIsNone(agents._codex_home())  # fake mode never reads the real account


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="settings-", ignore_cleanup_errors=True)
        self.root = Path(self.temp.name) / "研究 根目录"
        self.root.mkdir()
        self.settings = agents.Settings(self.root)
        self.path = self.root / agents.SETTINGS_NAME

    def tearDown(self):
        self.temp.cleanup()

    def test_missing_file_gives_defaults(self):
        data, revision = self.settings.load()
        self.assertEqual((revision, data), ("", agents.DEFAULT_SETTINGS))
        self.assertEqual(data["roles"]["author"][0], {"provider": "codex", "model": "gpt-6.1-sol", "effort": "high"})
        self.assertEqual(data["limits"], {"codex_stop_percent": 90, "claude_per_call_usd": 0.5, "claude_month_usd": 10.0,
                                          "agy_cooldown_hours": 5, "rounds_default": 2, "max_runs_per_round": 2,
                                          "call_timeout_minutes": 25})
        self.assertEqual(data["git"], {"auto_commit": True, "auto_push": True, "remote": "origin", "branch": "main"})
        data["limits"]["codex_stop_percent"] = 1
        self.assertEqual(agents.DEFAULT_SETTINGS["limits"]["codex_stop_percent"], 90)

    def test_save_and_load_keep_unknown_keys(self):
        data, revision = self.settings.load()
        data["limits"]["claude_month_usd"] = 3.5
        data["note"], data["limits"]["custom"], data["roles"]["author"][0]["extra"] = "自己的备注", 7, "x"
        _, new_revision = self.settings.save(data, revision)
        raw = self.path.read_bytes()
        self.assertEqual(new_revision, hashlib.sha256(raw).hexdigest())
        loaded, loaded_revision = self.settings.load()
        self.assertEqual(loaded_revision, new_revision)
        self.assertEqual((loaded["limits"]["claude_month_usd"], loaded["note"], loaded["limits"]["custom"]), (3.5, "自己的备注", 7))
        self.assertEqual(loaded["roles"]["author"][0]["extra"], "x")
        self.assertEqual(json.loads(raw.decode("utf-8")), loaded)
        self.assertIn("自己的备注", raw.decode("utf-8"))  # plain UTF-8, not \\u escapes
        self.assertEqual([p.name for p in self.root.iterdir()], [agents.SETTINGS_NAME])  # no temp file left

    def test_stale_revision_is_refused(self):
        data, revision = self.settings.load()
        self.settings.save(data, revision)
        with self.assertRaises(ValueError) as caught:
            self.settings.save(data, revision)
        self.assertIn("刷新", str(caught.exception))
        with self.assertRaises(ValueError):
            self.settings.save(data, "0" * 64)

    def test_missing_keys_are_filled(self):
        self.path.write_text(json.dumps({"limits": {"codex_stop_percent": 80},
                                         "roles": {"engineer": [{"provider": "codex", "model": "gpt-7"}]}}), encoding="utf-8")
        data, revision = self.settings.load()
        self.assertEqual(revision, hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertEqual((data["limits"]["codex_stop_percent"], data["limits"]["rounds_default"], data["schema"]), (80, 2, 1))
        self.assertEqual(data["roles"]["engineer"], [{"provider": "codex", "model": "gpt-7"}])
        self.assertEqual(data["roles"]["reviewer"], agents.DEFAULT_SETTINGS["roles"]["reviewer"])
        self.assertEqual(data["git"], agents.DEFAULT_GIT)
        self.assertEqual(self.settings.last_error, "")

    def test_invalid_values_in_the_file_fall_back(self):
        self.path.write_text(json.dumps({"limits": {"call_timeout_minutes": "abc", "rounds_default": 9},
                                         "roles": {"engineer": [{"provider": "agy", "model": "gemini-4-argon-high"}]}}), encoding="utf-8")
        data, _ = self.settings.load()
        self.assertEqual((data["limits"]["call_timeout_minutes"], data["limits"]["rounds_default"]), (25, 2))
        self.assertEqual(data["roles"]["engineer"], agents.DEFAULT_SETTINGS["roles"]["engineer"])
        self.assertTrue(self.settings.last_error)

    def test_broken_json_uses_defaults_and_can_be_replaced(self):
        self.path.write_text("{ broken", encoding="utf-8")
        data, revision = self.settings.load()
        self.assertEqual(data, agents.DEFAULT_SETTINGS)
        self.assertEqual(revision, hashlib.sha256(b"{ broken").hexdigest())
        self.assertIn("无法解析", self.settings.last_error)
        self.settings.save(data, revision)
        self.assertEqual(self.settings.load()[0], agents.DEFAULT_SETTINGS)

    def test_validation(self):
        base, revision = self.settings.load()

        def changed(change):
            data = copy.deepcopy(base)
            change(data)
            return data
        bad = {
            "命令行只能是": changed(lambda d: d["roles"]["author"][0].update(provider="openai")),
            "模型不能为空": changed(lambda d: d["roles"]["reviewer"][0].update(model="")),
            "工程只能用 Codex": changed(lambda d: d["roles"]["engineer"].append({"provider": "agy", "model": "gemini-4-argon-high"})),
            "Antigravity 仅用于 Gemini": changed(lambda d: d["roles"]["author"].append({"provider": "agy", "model": "claude-opus-5-5-high"})),
            "停用线": changed(lambda d: d["limits"].update(codex_stop_percent=150)),
            "默认轮数": changed(lambda d: d["limits"].update(rounds_default=True)),
            "超时": changed(lambda d: d["limits"].update(call_timeout_minutes=2.5)),
            "推理强度": changed(lambda d: d["roles"]["author"][0].update(effort='high"\nmodel_provider="x')),
            "至少需要一个候选": changed(lambda d: d["roles"].update(drift=[])),
            "自动推送": changed(lambda d: d["git"].update(auto_push="yes")),
            "分支": changed(lambda d: d["git"].update(branch="-x")),
            "模型名称": changed(lambda d: d["roles"]["author"][0].update(model="--dangerously-bypass-approvals-and-sandbox")),
        }
        for expected, data in bad.items():
            with self.subTest(expected):
                with self.assertRaises(ValueError) as caught:
                    self.settings.save(data, revision)
                self.assertIn(expected, str(caught.exception))
                self.assertTrue(str(caught.exception).startswith("设置有误"))
        with self.assertRaises(ValueError):
            self.settings.save("不是对象", revision)
        self.assertFalse(self.path.exists())


def make_result(**changes):
    base = dict(ok=True, text="答", provider="claude", model="claude-haiku-5-5", model_reported="claude-haiku-5-5",
                family="anthropic", bucket="claude", usage={"input_tokens": 10, "output_tokens": 5, "cached_input_tokens": 0,
                                                            "reasoning_tokens": 0, "total_tokens": 15},
                cost_usd=0.01, session_id="s", duration_s=1.234, exit_code=0, error_class="", error="", workdir=Path("."),
                stdout_path=Path("_stdout.txt"), stderr_path=Path("_stderr.txt"), rate_limits=None)
    base.update(changes)
    return agents.CallResult(**base)


def usage_row(now, hours_ago, bucket, *, ok=True, error_class="", cost=None, tokens=0, rate_limits=None):
    row = {"time": (now - timedelta(hours=hours_ago)).isoformat(), "provider": bucket.split(":")[0], "model": "m",
           "bucket": bucket, "ok": ok, "error_class": error_class, "usage": {"total_tokens": tokens}, "cost_usd": cost}
    if rate_limits:
        row["rate_limits"] = rate_limits
    return row


def write_rows(root, rows):
    path = Path(root) / ".runtime" / "usage.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


class UsageLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="usage-", ignore_cleanup_errors=True)
        self.root = Path(self.temp.name) / "研究 根目录"
        self.live = None
        self.patch = patch.object(agents, "codex_rate_limits", lambda *a, **k: self.live)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def test_record_is_thread_safe(self):
        ledger = agents.UsageLedger(self.root)

        def work():
            for _ in range(10):
                ledger.record(make_result(), role="reviewer", ref="C04-Q01", round_id="第01轮_20261110-153000")
        threads = [threading.Thread(target=work) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        agents.UsageLedger(self.root).record(make_result(error_class="quota", ok=False), "test", "", "")
        rows = [json.loads(x) for x in (self.root / ".runtime" / "usage.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(rows), 81)
        for key in ("time", "provider", "model", "family", "bucket", "role", "ref", "round_id", "ok", "error_class",
                    "usage", "cost_usd", "duration_s"):
            self.assertIn(key, rows[0])
        self.assertEqual((rows[0]["round_id"], rows[0]["usage"]["total_tokens"], rows[0]["duration_s"]),
                         ("第01轮_20261110-153000", 15, 1.23))
        self.assertEqual((rows[-1]["role"], rows[-1]["error_class"], rows[-1]["ok"]), ("test", "quota", False))

    def test_summary(self):
        now = datetime(2026, 11, 11, 4, 0, tzinfo=timezone.utc)  # Wednesday; the boundaries below hold in any time zone
        old_limits = {"observed_at": "2026-11-11T01:00:00Z", "weekly": {"used_percent": 50.0, "resets_at": 1, "window_minutes": 10080}}
        write_rows(self.root, [
            usage_row(now, 1, "claude", cost=0.5, tokens=100),
            usage_row(now, 30, "claude", ok=False, error_class="quota", cost=0.25),
            usage_row(now, 24 * 9, "claude", cost=1.0, tokens=1000),
            usage_row(now, 24 * 40, "claude", cost=5.0),
            usage_row(now, -1, "claude", cost=9.0),  # in the future of `now`: ignored
            usage_row(now, 2, "codex", tokens=40, rate_limits=old_limits),
            usage_row(now, 3, "agy:gemini", ok=False, error_class="network"),
        ])
        (self.root / ".runtime" / "usage.jsonl").open("a", encoding="utf-8").close()
        summary = agents.UsageLedger(self.root).summary(now)
        self.assertEqual(set(summary), set(agents.BUCKETS))
        claude = summary["claude"]
        self.assertEqual((claude["calls_today"], claude["calls_week"], claude["tokens_week"]), (1, 2, 100))
        self.assertAlmostEqual(claude["cost_month_usd"], 1.75)
        self.assertEqual(claude["last_error_class"], "")
        self.assertEqual(claude["last_quota_error_at"], (now - timedelta(hours=30)).isoformat())
        self.assertEqual((summary["agy:gemini"]["last_error_class"], summary["agy:other"]["calls_week"]), ("network", 0))
        self.assertEqual(summary["codex"]["rate_limits"], old_limits)
        self.live = {"observed_at": "2026-11-11T03:00:00Z", "weekly": {"used_percent": 60.0}}
        self.assertEqual(agents.UsageLedger(self.root).summary(now)["codex"]["rate_limits"]["weekly"]["used_percent"], 60.0)
        self.live = {"observed_at": "2026-11-10T03:00:00Z", "weekly": {"used_percent": 30.0}}
        self.assertEqual(agents.UsageLedger(self.root).summary(now)["codex"]["rate_limits"], old_limits)

    def test_summary_skips_malformed_lines(self):
        path = self.root / ".runtime" / "usage.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text("not json\n[1, 2]\n{\"time\": \"bad\"}\n", encoding="utf-8")
        self.assertEqual(agents.UsageLedger(self.root).summary()["claude"]["calls_week"], 0)


class QuotaTests(FakeEnvCase):
    def setUp(self):
        super().setUp()
        self.root = self.base / "研究"
        self.root.mkdir()
        self.settings = copy.deepcopy(agents.DEFAULT_SETTINGS)
        self.now = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)

    def quota(self, limits=None):
        with patch.object(agents, "codex_rate_limits", lambda *a, **k: limits):
            return agents.quota_status(self.root, self.settings, now=self.now)

    def window(self, used, hours_to_reset=48, minutes=10080):
        return {"used_percent": used, "resets_at": int((self.now + timedelta(hours=hours_to_reset)).timestamp()),
                "window_minutes": minutes}

    def test_shape_and_fresh_state(self):
        quota = self.quota()
        self.assertEqual(set(quota), set(agents.BUCKETS))
        for item in quota.values():
            self.assertEqual(set(item), {"available", "level", "text", "detail"})
            self.assertEqual((item["available"], item["level"]), (True, "ok"))
        self.assertEqual(quota["claude"]["text"], "本月 $0 / $10")
        self.assertEqual(quota["agy:gemini"]["text"], "本周 0 次，额度未公开")
        self.assertIn("额度未知", quota["codex"]["text"])
        roles = agents.resolve_roles(self.settings, quota, ["author", "reviewer", "engineer", "drift"])
        self.assertEqual(roles["author"]["provider"], "codex")

    def test_codex_stop_percent(self):
        for used, level in ((50, "ok"), (80, "warn"), (89.9, "warn"), (90, "blocked"), (97, "blocked")):
            item = self.quota({"weekly": self.window(used), "five_hour": None})["codex"]
            self.assertEqual((item["level"], item["available"]), (level, level != "blocked"), used)
        reset = datetime.fromtimestamp(self.window(87)["resets_at"], timezone.utc).astimezone().strftime("%m-%d %H:%M")
        self.assertEqual(self.quota({"weekly": self.window(87)})["codex"]["text"], f"周额度已用 87%（{reset} 重置）")
        self.settings["limits"]["codex_stop_percent"] = 80
        self.assertEqual(self.quota({"weekly": self.window(85)})["codex"]["level"], "blocked")

    def test_codex_five_hour_window_and_reset_windows(self):
        item = self.quota({"weekly": self.window(10), "five_hour": self.window(95, 2, 300)})["codex"]
        self.assertEqual(item["level"], "blocked")
        self.assertIn("5 小时额度已用 95%", item["text"])
        self.assertEqual(self.quota({"weekly": self.window(99, hours_to_reset=-1)})["codex"]["level"], "ok")
        self.assertEqual(self.quota({"weekly": self.window(40), "reached": "primary"})["codex"]["level"], "blocked")

    def test_codex_quota_error_without_a_newer_snapshot(self):
        write_rows(self.root, [usage_row(self.now, 1, "codex", ok=False, error_class="quota")])
        self.assertEqual(self.quota(None)["codex"]["level"], "blocked")
        newer = {"observed_at": (self.now - timedelta(minutes=10)).isoformat(), "weekly": self.window(40)}
        self.assertEqual(self.quota(newer)["codex"]["level"], "ok")

    def test_agy_cooldown_after_a_quota_error(self):
        write_rows(self.root, [usage_row(self.now, 2, "agy:gemini", ok=False, error_class="quota"), usage_row(self.now, 3, "agy:other")])
        quota = self.quota()
        self.assertEqual((quota["agy:gemini"]["level"], quota["agy:gemini"]["available"]), ("blocked", False))
        self.assertIn("后恢复", quota["agy:gemini"]["text"])
        self.assertEqual((quota["agy:other"]["level"], quota["agy:other"]["text"]), ("ok", "本周 1 次，额度未公开"))
        self.now += timedelta(hours=4)  # six hours after the error, past the 5 hour pause
        self.assertEqual(self.quota()["agy:gemini"]["level"], "ok")

    def test_agy_cooldown_lifted_by_a_later_success_or_shorter_setting(self):
        write_rows(self.root, [usage_row(self.now, 2, "agy:gemini", ok=False, error_class="quota"), usage_row(self.now, 1, "agy:gemini")])
        self.assertEqual(self.quota()["agy:gemini"]["level"], "ok")
        write_rows(self.root, [usage_row(self.now, 2, "agy:other", ok=False, error_class="quota")])
        self.assertEqual(self.quota()["agy:other"]["level"], "blocked")
        self.settings["limits"]["agy_cooldown_hours"] = 1
        self.assertEqual(self.quota()["agy:other"]["level"], "ok")

    def test_claude_month_budget(self):
        write_rows(self.root, [usage_row(self.now, 2, "claude", cost=9.6), usage_row(self.now, 24 * 40, "claude", cost=50.0)])
        item = self.quota()["claude"]
        self.assertEqual(item["level"], "blocked")
        self.assertTrue(item["text"].startswith("本月 $9.60 / $10"))
        self.settings["limits"]["claude_month_usd"] = 20
        item = self.quota()["claude"]
        self.assertEqual((item["level"], item["text"], item["detail"]["cost_month_usd"]), ("ok", "本月 $9.60 / $20", 9.6))
        self.settings["limits"]["claude_month_usd"] = 12
        self.assertEqual(self.quota()["claude"]["level"], "warn")


class ResolveRolesTests(unittest.TestCase):
    ALL = ["author", "reviewer", "engineer", "drift", "writer", "referee"]

    def setUp(self):
        self.settings = copy.deepcopy(agents.DEFAULT_SETTINGS)

    @staticmethod
    def quota(*blocked):
        return {b: {"available": b not in blocked, "level": "blocked" if b in blocked else "ok",
                    "text": "额度用完" if b in blocked else "", "detail": {}} for b in agents.BUCKETS}

    def test_defaults(self):
        roles = agents.resolve_roles(self.settings, self.quota(), self.ALL)
        self.assertEqual(list(roles), self.ALL)
        self.assertEqual(roles["author"], {"provider": "codex", "model": "gpt-6.1-sol", "effort": "high", "family": "openai",
                                           "bucket": "codex", "label": "GPT（gpt-6.1-sol）"})
        self.assertEqual((roles["reviewer"]["model"], roles["reviewer"]["label"]), ("gemini-4-argon-high", "Gemini（gemini-4-argon-high）"))
        self.assertEqual((roles["drift"]["model"], roles["drift"]["label"]), ("claude-haiku-5-5", "Claude（claude-haiku-5-5）"))
        self.assertEqual((roles["referee"]["model"], roles["referee"]["bucket"]), ("claude-opus-5-5", "claude"))
        self.assertEqual((roles["engineer"]["provider"], roles["writer"]["family"]), ("codex", "google"))
        self.assertEqual(roles["reviewer"]["effort"], "")
        for role in ("reviewer", "drift", "referee"):
            self.assertNotEqual(roles[role]["family"], roles["author"]["family"])

    def test_codex_blocked(self):
        roles = agents.resolve_roles(self.settings, self.quota("codex"), ["author", "reviewer", "drift", "referee", "writer"])
        self.assertEqual((roles["author"]["model"], roles["author"]["family"]), ("claude-opus-5-5", "anthropic"))
        self.assertEqual(roles["reviewer"]["model"], "gemini-4-argon-high")
        self.assertEqual(roles["drift"]["model"], "gemini-3.8-flash-medium")
        self.assertEqual(roles["referee"]["model"], "gemini-4-argon-high")
        with self.assertRaises(agents.RoleError) as caught:
            agents.resolve_roles(self.settings, self.quota("codex"), ["author", "engineer"])
        self.assertIn("工程", str(caught.exception))
        self.assertIn("暂不可用", str(caught.exception))

    def test_claude_blocked_falls_back_to_gemini(self):
        roles = agents.resolve_roles(self.settings, self.quota("claude"), ["author", "drift", "referee"])
        self.assertEqual(roles["drift"]["model"], "gemini-3.8-flash-medium")
        self.assertEqual((roles["referee"]["provider"], roles["referee"]["model"]), ("agy", "gemini-4-argon-high"))

    def test_same_family_candidates_are_refused(self):
        self.settings["roles"]["reviewer"] = [{"provider": "codex", "model": "gpt-6.1-sol"},
                                              {"provider": "agy", "model": "gpt-oss-120b-medium"}]
        with self.assertRaises(agents.RoleError) as caught:
            agents.resolve_roles(self.settings, self.quota(), ["author", "reviewer"])
        self.assertIn("审查", str(caught.exception))
        self.assertIn("同属", str(caught.exception))
        self.settings["roles"]["writer"] = [{"provider": "codex", "model": "gpt-6.1-sol"}]  # no family rule for the writer
        self.assertEqual(agents.resolve_roles(self.settings, self.quota(), ["author", "writer"])["writer"]["family"], "openai")

    def test_everything_blocked(self):
        with self.assertRaises(agents.RoleError) as caught:
            agents.resolve_roles(self.settings, self.quota(*agents.BUCKETS), ["author"])
        self.assertIn("作者", str(caught.exception))

    def test_engineer_is_codex_only(self):
        self.settings["roles"]["engineer"] = [{"provider": "agy", "model": "gemini-4-argon-high"}]
        with self.assertRaises(agents.RoleError) as caught:
            agents.resolve_roles(self.settings, self.quota(), ["engineer"])
        self.assertIn("只能用 Codex", str(caught.exception))

    def test_family_rule_without_author_in_the_request(self):
        self.settings["roles"]["reviewer"] = [{"provider": "codex", "model": "gpt-6.1-sol"}, {"provider": "agy", "model": "gemini-4-argon-high"}]
        roles = agents.resolve_roles(self.settings, self.quota(), ["reviewer"])
        self.assertEqual(list(roles), ["reviewer"])
        self.assertEqual(roles["reviewer"]["model"], "gemini-4-argon-high")

    def test_missing_role_uses_default_candidates(self):
        del self.settings["roles"]["drift"]
        self.assertEqual(agents.resolve_roles(self.settings, self.quota(), ["author", "drift"])["drift"]["model"], "claude-haiku-5-5")
        self.assertEqual(agents.resolve_roles({}, {}, ["author"])["author"]["model"], "gpt-6.1-sol")


if __name__ == "__main__":
    unittest.main()
