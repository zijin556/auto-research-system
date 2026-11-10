"""Stand-in for the codex, agy and claude command lines (tests and demos; no model is called).

    python fake_cli.py <provider> <mode> [the real CLI's arguments ...]

``notebook/agents.run_call`` starts this script instead of the real binary when
``RESEARCH_NOTEBOOK_FAKE_AGENTS=1`` and keeps the real argument list, so the
argument building and output parsing paths are the real ones. The script reads
stdin like the real CLI (agy: one stream-json line; codex and claude: UTF-8
text), takes the template key from the prompt line ``【模板】<key>`` and looks it
up in the JSON file named by ``RESEARCH_NOTEBOOK_FAKE_REPLIES``::

    {"<key>": {"text": "...", "files": {"relative/path": "content"},
               "error": "quota" | "not_logged_in" | "network" | "budget" | "error",
               "sleep": seconds, "child": seconds, "usage": {...}}}

``files`` are written relative to the working folder in engineer mode only.
``sleep`` waits before answering; ``child`` starts a grandchild process that
sleeps that long and writes its PID to ``child.pid`` (to test killing the
process tree); ``usage`` overrides the token counts (provider field names).
A missing file or key gives a minimal valid reply. Unknown flags are ignored.
Every run writes ``_fake_call.json`` (argv, cwd, prompt, stdin encoding) into
the working folder for the tests to inspect.
"""
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

DEFAULT_TEXT = "结论摘要：（模拟）\n\n```台账变更\n无\n```\n"
VALUE_FLAGS = {
    "codex": {"--cd", "-C", "--output-last-message", "-o", "--model", "-m", "-c", "--config", "--sandbox", "-s",
              "--color", "--output-schema", "-p", "--profile", "--enable", "--disable", "-i", "--image", "--add-dir",
              "--local-provider"},
    "agy": {"--model", "--settings", "--input-format", "--output-format", "--print-timeout", "--mode", "--effort",
            "--add-dir", "--agent", "--conversation", "--json-schema", "--log-file", "--project", "-p", "--print",
            "--prompt"},
    "claude": {"--model", "--output-format", "--input-format", "--setting-sources", "--tools", "--permission-mode",
               "--max-budget-usd", "--system-prompt", "--append-system-prompt", "--fallback-model", "--mcp-config",
               "--settings", "--add-dir", "--allowedTools", "--disallowedTools", "--resume", "-r", "--session-id"},
}
WATCHED_ENV = ("OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL", "GEMINI_API_KEY", "GOOGLE_API_KEY", "CLAUDECODE",
               "CLAUDE_CONFIG_DIR", "CODEX_THREAD_ID")
ERRORS = {
    "codex": {
        "quota": "You've hit your usage limit. Visit https://chatgpt.com/codex/settings/usage to purchase more credits "
                 "or try again at 6:43 AM.",
        "not_logged_in": "unexpected status 401 Unauthorized: Your access token could not be refreshed because your "
                         "refresh token was already used. Please log out and sign in again.",
        "network": "stream disconnected before completion: error sending request for url "
                   "(https://chatgpt.com/backend-api/codex/responses)",
        "error": "unexpected status 400 Bad Request: {\"detail\":\"The requested model is not supported.\"}",
    },
    "agy": {
        "quota": "RESOURCE_EXHAUSTED: You have exhausted your capacity on this model. Your quota will reset after 4h59m.",
        "not_logged_in": "authentication required: You are not logged into Antigravity.",
        "network": "UNAVAILABLE: connection failed: dial tcp 142.250.72.10:443: connectex: No connection could be made",
        "error": "invalid model selection (unknown-model)",
    },
    "claude": {
        "quota": "Credit balance is too low",
        "not_logged_in": "Invalid API key · Please run /login",
        "network": "API Error: Unable to connect to API (ECONNREFUSED)",
        "error": "API Error: 400 {\"type\":\"error\",\"error\":{\"type\":\"invalid_request_error\",\"message\":\"model: unknown\"}}",
    },
}


def emit(obj):
    sys.stdout.buffer.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()


def err(text):
    sys.stderr.buffer.write((text + "\n").encode("utf-8", errors="replace"))
    sys.stderr.buffer.flush()


def parse_args(provider, argv):
    """{flag: [values]} and positional arguments; unknown flags are kept as booleans and ignored."""
    opts, positional, i = {}, [], 0
    flags = VALUE_FLAGS[provider]
    while i < len(argv):
        arg = argv[i]
        if arg.startswith("--") and "=" in arg:
            name, value = arg.split("=", 1)
            opts.setdefault(name, []).append(value)
        elif arg in flags and i + 1 < len(argv):
            opts.setdefault(arg, []).append(argv[i + 1])
            i += 1
        elif arg.startswith("-") and arg != "-":
            opts.setdefault(arg, []).append(True)
        else:
            positional.append(arg)
        i += 1
    return opts, positional


def last(opts, *names):
    for name in names:
        values = [v for v in opts.get(name, []) if isinstance(v, str)]
        if values:
            return values[-1]
    return ""


def check_args(provider, opts, positional):
    """The few rules of the real CLIs that a wrong argument list would break."""
    if provider == "codex":
        if not positional or positional[0] != "exec":
            return "fake codex: expected the 'exec' subcommand"
        if "--json" not in opts:
            return "fake codex: --json is required for machine-readable output"
        if positional[-1] != "-":
            return "fake codex: the prompt must come from stdin ('-')"
    if provider == "agy" and last(opts, "--input-format") == "stream-json" and last(opts, "--output-format") != "stream-json":
        return "--input-format stream-json requires --output-format stream-json"
    if provider == "claude" and "-p" not in opts and "--print" not in opts:
        return "fake claude: only print mode (-p) is supported"
    return ""


def read_prompt(provider, raw, opts):
    """(prompt, problem) from stdin bytes, the way the real CLI reads them."""
    if provider == "agy" and last(opts, "--input-format") == "stream-json":
        for line in raw.decode("utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                message = json.loads(line)
            except ValueError:
                return "", "malformed stream-json input line"
            if not isinstance(message, dict) or "event" not in message:
                return "", "stream-json input line has no event"
            if message["event"] != "user":
                err(f'warning: ignoring unsupported stream input message event "{message["event"]}"')
                continue
            content = (message.get("message") or {}).get("content")
            if isinstance(content, list):
                content = "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
            return (content, "") if isinstance(content, str) else ("", "message content must be text")
        return "", "no user message on stdin"
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return "", "Failed to read prompt from stdin: input is not valid UTF-8"
    return text.lstrip("\ufeff"), ""


def load_reply(key):
    path = os.environ.get("RESEARCH_NOTEBOOK_FAKE_REPLIES", "")
    if not key or not path or not os.path.isfile(path):
        return {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    entry = data.get(key) if isinstance(data, dict) else None
    if isinstance(entry, str):
        return {"text": entry}
    return entry if isinstance(entry, dict) else {}


def load_agy_settings(value):
    if not value:
        return None
    try:
        text = value if value.lstrip().startswith("{") else Path(value).read_text(encoding="utf-8-sig")
        return json.loads(text)
    except (OSError, ValueError):
        return None


def write_files(files):
    base = Path.cwd().resolve()
    for rel, content in (files or {}).items():
        target = (base / rel).resolve()
        if Path(rel).is_absolute() or ".." in Path(rel).parts or base not in target.parents:
            raise ValueError(f"fake_cli: refusing to write outside the working folder: {rel}")
        target.parent.mkdir(parents=True, exist_ok=True)
        data = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, indent=1)
        target.write_bytes(data.encode("utf-8"))


def spawn_child(seconds):
    """A grandchild that outlives this process unless the whole tree is killed."""
    proc = subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({float(seconds)!r})"],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    Path("child.pid.tmp").write_text(str(proc.pid), encoding="ascii")
    os.replace("child.pid.tmp", "child.pid")


def counts(prompt, text):
    return len(prompt.encode("utf-8")) // 3 + 1, len(text.encode("utf-8")) // 3 + 1


# output in each provider's real format ------------------------------------------
def codex_ok(text, prompt, opts, usage):
    thread = str(uuid.uuid4())
    n_in, n_out = counts(prompt, text)
    totals = {"input_tokens": n_in, "cached_input_tokens": n_in // 2, "output_tokens": n_out, "reasoning_output_tokens": 8}
    totals.update(usage)
    emit({"type": "thread.started", "thread_id": thread})
    emit({"type": "turn.started"})
    emit({"type": "item.completed", "item": {"id": "item_0", "type": "reasoning", "text": "**模拟推理**"}})
    emit({"type": "item.completed", "item": {"id": "item_1", "type": "agent_message", "text": text}})
    emit({"type": "turn.completed", "usage": totals})
    target = last(opts, "--output-last-message", "-o")
    if target:
        Path(target).write_bytes(text.encode("utf-8"))


def agy_ok(text, prompt, opts, usage):
    conversation = str(uuid.uuid4())
    n_in, n_out = counts(prompt, text)
    totals = {"input_tokens": n_in, "output_tokens": n_out, "thinking_tokens": 5, "cache_read_tokens": n_in // 2,
              "total_tokens": n_in + n_out}
    totals.update(usage)
    result = {"conversation_id": conversation, "status": "SUCCESS", "response": text, "duration_seconds": 0.1,
              "num_turns": 1, "usage": totals}
    if last(opts, "--output-format") == "json":
        emit(result)
        return
    init = {"cwd": os.getcwd(), "tools": [], "permission_mode": "request-review"}
    if last(opts, "--model"):
        init["model"] = last(opts, "--model")
    emit({"event": "init", "conversation_id": conversation, "init": init})
    emit({"event": "step_update", "step_update": {"conversation_id": conversation, "step_index": 0, "state": "DONE",
                                                  "step_type": "user_input"}})
    emit({"event": "step_update", "step_update": {"conversation_id": conversation, "step_index": 2, "state": "DONE",
                                                  "step_type": "agent_response", "text_delta": text, "duration_seconds": 0.1}})
    emit({"event": "result", "result": result})


def claude_ok(text, prompt, opts, usage):
    model = last(opts, "--model") or "claude-default"
    n_in, n_out = counts(prompt, text)
    totals = {"input_tokens": n_in, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": n_out,
              "server_tool_use": {"web_search_requests": 0}, "service_tier": "standard"}
    totals.update(usage)
    per_model = {"inputTokens": totals["input_tokens"], "outputTokens": totals["output_tokens"],
                 "cacheReadInputTokens": totals["cache_read_input_tokens"],
                 "cacheCreationInputTokens": totals["cache_creation_input_tokens"], "webSearchRequests": 0,
                 "costUSD": 0.0012, "contextWindow": 200000}
    emit({"type": "result", "subtype": "success", "is_error": False, "duration_ms": 100, "duration_api_ms": 80,
          "num_turns": 1, "result": text, "session_id": str(uuid.uuid4()), "total_cost_usd": 0.0012, "usage": totals,
          "modelUsage": {model: per_model}, "permission_denials": [], "uuid": str(uuid.uuid4())})


def emit_error(provider, kind, opts):
    """The provider's real failure signature; exit code 1."""
    if provider == "claude" and kind == "budget":
        emit({"type": "result", "subtype": "error_max_budget_usd", "is_error": True, "duration_ms": 100, "num_turns": 1,
              "session_id": str(uuid.uuid4()), "total_cost_usd": 0.06, "usage": {"input_tokens": 0, "output_tokens": 0},
              "modelUsage": {}, "errors": ["Reached maximum budget"]})
        return 1
    message = ERRORS[provider].get(kind, ERRORS[provider]["error"])
    if provider == "codex":
        emit({"type": "thread.started", "thread_id": str(uuid.uuid4())})
        emit({"type": "turn.started"})
        emit({"type": "error", "message": message})
        emit({"type": "turn.failed", "error": {"message": message}})
        err(f"ERROR: {message}")
    elif provider == "agy":
        conversation = str(uuid.uuid4())
        emit({"event": "init", "conversation_id": conversation, "init": {"cwd": os.getcwd(), "tools": []}})
        emit({"event": "result", "result": {"conversation_id": conversation, "status": "ERROR", "response": "",
                                            "error": message, "duration_seconds": 0.2, "num_turns": 1,
                                            "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}}})
        err(message)
    else:
        emit({"type": "result", "subtype": "success", "is_error": True, "duration_ms": 100, "num_turns": 1,
              "result": message, "session_id": str(uuid.uuid4()), "total_cost_usd": 0,
              "usage": {"input_tokens": 0, "output_tokens": 0}, "modelUsage": {}})
    return 1


def main(argv):
    if len(argv) < 2 or argv[0] not in VALUE_FLAGS:
        err("usage: fake_cli.py <codex|agy|claude> <mode> [the real CLI's arguments ...]")
        return 2
    provider, mode, rest = argv[0], argv[1], argv[2:]
    opts, positional = parse_args(provider, rest)
    problem = check_args(provider, opts, positional)
    if problem:
        err(problem)
        return 2
    if last(opts, "--cd", "-C"):
        os.chdir(last(opts, "--cd", "-C"))
    raw = sys.stdin.buffer.read()
    prompt, problem = read_prompt(provider, raw, opts)
    if problem:
        err(problem)
        return 1
    match = re.search(r"^【模板】\s*(\S+)", prompt, re.M)
    key = match.group(1) if match else ""
    entry = load_reply(key)
    record = {"provider": provider, "mode": mode, "argv": rest, "cwd": os.getcwd(), "stdin_ascii": raw.isascii(),
              "stdin_bytes": len(raw), "prompt": prompt, "template": key,
              "agy_settings": load_agy_settings(last(opts, "--settings")) if provider == "agy" else None,
              "env_seen": sorted(k for k in os.environ if k.upper() in WATCHED_ENV or k.upper().startswith("ANTHROPIC_"))}
    Path("_fake_call.json").write_bytes(json.dumps(record, ensure_ascii=False, indent=1).encode("utf-8"))
    if entry.get("child"):
        spawn_child(entry["child"])
    if entry.get("sleep"):
        time.sleep(float(entry["sleep"]))
    if entry.get("error"):
        return emit_error(provider, str(entry["error"]), opts)
    text = entry["text"] if isinstance(entry.get("text"), str) else DEFAULT_TEXT
    if mode == "engineer":
        write_files(entry.get("files"))
    usage = entry.get("usage") if isinstance(entry.get("usage"), dict) else {}
    {"codex": codex_ok, "agy": agy_ok, "claude": claude_ok}[provider](text, prompt, opts, usage)
    return 0


if __name__ == "__main__":
    try:
        code = main(sys.argv[1:])
    except Exception as exc:  # noqa: BLE001 - report like a CLI, in UTF-8
        err(f"fake_cli failed: {type(exc).__name__}: {exc}")
        code = 70
    sys.exit(code)
