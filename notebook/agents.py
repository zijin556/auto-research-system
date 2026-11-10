"""Model providers for the v2 workflow: calls, usage ledger, quota and settings.

Three local command-line agents are called headlessly, prompt on stdin:
Codex (ChatGPT subscription), Antigravity ``agy`` (Google AI subscription) and
Claude Code through the user's relay. A failed call is never retried here.

With ``RESEARCH_NOTEBOOK_FAKE_AGENTS=1`` every call runs ``tests/fake_cli.py``
with the same argument list instead of the real binary, so tests and demos
use no quota. Secrets are never read: the CLIs keep their own logins.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from .store import ConflictError

PROVIDERS = ("codex", "agy", "claude")
PROVIDER_NAMES = {"codex": "Codex", "agy": "Antigravity", "claude": "Claude Code"}
FAMILY_NAMES = {"openai": "GPT", "google": "Gemini", "anthropic": "Claude", "human": "你", "system": "系统", "unknown": "模型"}
ROLE_LABELS = {"author": "作者", "reviewer": "审查", "engineer": "工程", "writer": "写作", "drift": "漂移检查", "referee": "审稿"}
BUCKETS = ("codex", "agy:gemini", "agy:other", "claude")
BUCKET_LABELS = {"codex": "Codex", "agy:gemini": "Gemini（Antigravity）", "agy:other": "Claude 与 GPT（Antigravity）", "claude": "Claude（中转站）"}
# USD per 1M tokens, official list prices for display; the relay may charge differently.
PRICES = {
    "claude-fable-5-1": {"input": 10.0, "output": 50.0},
    "claude-opus-5-5": {"input": 4.0, "output": 20.0},
    "claude-sonnet-5-5": {"input": 2.0, "output": 10.0},
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50},
}
DEFAULT_SYSTEM = "你是严谨的科研审稿助手。只根据用户消息里给出的材料作答，不调用工具，不访问网络。用简体中文回答。"
AGY_SETTINGS = {"permissions": {"deny": ["read_file(*)", "write_file(*)", "command(*)", "mcp(*)", "read_url(*)", "execute_url(*)"]}, "useG1Credits": False}
CODEX_FEATURES_OFF = ("apps", "plugins", "remote_plugin", "computer_use", "browser_use", "multi_agent", "memories",
                      "code_mode", "code_mode_host", "skill_mcp_dependency_install")
CODEX_MIN_VERSION = (0, 160)
EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
# Model names go into argument lists: no leading '-', no quotes or spaces.
MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}")
THREAD_RE = re.compile(r"[A-Za-z0-9-]{8,80}")
REMOTE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
BRANCH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,99}")
MAX_SYSTEM_PROMPT = 20_000
SETTINGS_NAME = "00_模型与额度.json"
FAKE_ENV = "RESEARCH_NOTEBOOK_FAKE_AGENTS"
FAKE_CLI = Path(__file__).resolve().parent.parent / "tests" / "fake_cli.py"
POLL_S = 0.25

# Subscriptions only: API keys and the parent session's relay variables must not leak in.
STRIP_ENV = {"OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL", "OPENAI_ORG_ID", "OPENAI_ORGANIZATION",
             "CODEX_THREAD_ID", "CODEX_SESSION_ID", "CODEX_INTERNAL_ORIGINATOR_OVERRIDE", "GEMINI_API_KEY",
             "GOOGLE_API_KEY", "ANTHROPIC_API_KEY"}
KEEP_ENV = {"CLAUDE_CODE_GIT_BASH_PATH"}

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_NEW_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


# small helpers ---------------------------------------------------------------
def fake_mode() -> bool:
    return os.environ.get(FAKE_ENV, "") == "1"


def _int(value) -> int:
    if value is None or isinstance(value, bool):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _number(value):
    """A real int/float (not bool), else None."""
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _aware(now=None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    return now if now.tzinfo else now.astimezone()  # naive -> local time


def _parse_time(text):
    if not isinstance(text, str) or not text:
        return None
    try:
        value = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _local_hm(moment: datetime) -> str:
    return moment.astimezone().strftime("%m-%d %H:%M")


def _usd(value: float) -> str:
    value = float(value or 0)
    return f"${value:.0f}" if abs(value - round(value)) < 1e-9 else f"${value:.2f}"


def _utf8(text: str) -> bytes:
    return (text or "").encode("utf-8", errors="replace")


_SECRET_RES = (re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+"), re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"),
               re.compile(r"\b[A-Za-z0-9_\-]{40,}\b"))


def _redact(text: str) -> str:
    for pattern in _SECRET_RES:
        text = pattern.sub("***", text)
    return text


# families, buckets, labels ---------------------------------------------------
def family_of(model: str) -> str:
    name = (model or "").strip().lower() if isinstance(model, str) else ""
    if name.startswith(("gpt-", "codex")) or re.match(r"o\d", name):
        return "openai"
    if name.startswith("gemini-"):
        return "google"
    if name.startswith("claude-"):
        return "anthropic"
    return "unknown"


def bucket_of(provider: str, model: str) -> str:
    if provider == "agy":
        return "agy:gemini" if family_of(model) == "google" else "agy:other"
    return provider if isinstance(provider, str) else ""


def model_label(model: str, family: str | None = None) -> str:
    return f"{FAMILY_NAMES.get(family or family_of(model), '模型')}（{model}）"


# binaries and environment ----------------------------------------------------
def _local_appdata() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))


def call_root() -> Path:
    """Scratch folders of read-only calls (ASCII path under the user's home)."""
    return _local_appdata() / "ResearchNotebook" / "calls"


def work_root() -> Path:
    """Workspaces of engineer runs."""
    return _local_appdata() / "ResearchNotebook" / "work"


def child_env() -> dict:
    """Environment for the CLIs: no API keys, no relay or session variables of a parent Claude Code."""
    env = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if upper in STRIP_ENV or upper.startswith("ANTHROPIC_"):
            continue
        if upper.startswith("CLAUDE") and upper not in KEEP_ENV:  # e.g. CLAUDECODE, CLAUDE_CONFIG_DIR
            continue
        env[key] = value
    return env


def _run_quiet(args, timeout: float):
    """Run a short helper command (version, login status). Returns (exit code, output); never raises."""
    try:
        proc = subprocess.run([str(a) for a in args], stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout,
                              env=child_env(), creationflags=_NO_WINDOW)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None, ""
    return proc.returncode, (proc.stdout + b"\n" + proc.stderr).decode("utf-8", errors="replace")


def _version_tuple(text: str):
    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text or "")
    return tuple(int(g or 0) for g in match.groups()) if match else None


_VERSIONS: dict = {}
_VERSIONS_LOCK = threading.Lock()


def binary_version(path) -> str:
    """First line of ``<binary> --version`` (cached per file and modification time)."""
    try:
        stamp = Path(path).stat().st_mtime
    except OSError:
        return ""
    with _VERSIONS_LOCK:
        hit = _VERSIONS.get(str(path))
    if hit and hit[0] == stamp:
        return hit[1]
    code, out = _run_quiet([path, "--version"], 15)
    line = next((x.strip() for x in out.splitlines() if x.strip()), "")[:100] if code == 0 else ""
    with _VERSIONS_LOCK:
        _VERSIONS[str(path)] = (stamp, line)
    return line


def _which(name: str):
    found = shutil.which(name) or (shutil.which(name[:-4]) if os.name != "nt" and name.endswith(".exe") else None)
    return Path(found) if found else None


def _newest(paths):
    found = [p for p in paths if p.is_file()]
    return max(found, key=lambda p: p.stat().st_mtime) if found else None


def find_binary(provider: str):
    """Resolve the real CLI: env override, then the verified install locations (never ``.cmd`` shims)."""
    override = os.environ.get(f"RESEARCH_NOTEBOOK_{str(provider).upper()}", "").strip()
    if override:
        path = Path(override)
        return path if path.is_absolute() and path.is_file() else None
    packages = _local_appdata() / "Microsoft" / "WinGet" / "Packages"
    try:
        if provider == "codex":
            winget = _newest(packages.glob("OpenAI.Codex_*/codex-x86_64-pc-windows-msvc.exe"))
            if winget:
                return winget
            found = _which("codex.exe")  # the desktop bundle on PATH may be stale (0.130 fails on the user's config)
            version = _version_tuple(binary_version(found)) if found else None
            return found if version and version[:2] >= CODEX_MIN_VERSION else None
        if provider == "agy":
            return _newest(packages.glob("Google.AntigravityCLI_*/agy.exe")) or _which("agy.exe")
        if provider == "claude":
            local_bin = _local_appdata() / "Programs" / "claude" / "claude.exe"
            return _which("claude.exe") or (local_bin if local_bin.is_file() else None)
    except OSError:
        return None
    return None


# call API -----------------------------------------------------------------------
@dataclass
class CallSpec:
    provider: str                     # 'codex' | 'agy' | 'claude'
    model: str
    prompt: str
    mode: str = "ask"                 # 'ask' (no tools, read-only) | 'engineer' (codex only)
    effort: str = ""                  # codex model_reasoning_effort ('' -> 'high')
    workdir: Path | None = None       # None -> new empty folder under call_root()
    timeout_s: int = 1500
    budget_usd: float | None = None   # claude only -> --max-budget-usd
    system_prompt: str = ""           # claude: --system-prompt; codex/agy: prepended to the prompt
    web_search: bool = False          # codex ask only
    label: str = ""


@dataclass
class CallResult:
    ok: bool
    text: str
    provider: str
    model: str
    model_reported: str
    family: str
    bucket: str
    usage: dict
    cost_usd: float | None
    session_id: str
    duration_s: float
    exit_code: int | None
    error_class: str
    error: str
    workdir: Path
    stdout_path: Path
    stderr_path: Path
    rate_limits: dict | None = None


def validate_spec(spec: CallSpec) -> str:
    """'' when the spec can be run, else a short Chinese reason."""
    if spec.provider not in PROVIDERS:
        return "未知的命令行（只能是 codex、agy 或 claude）"
    if not isinstance(spec.model, str) or not MODEL_RE.fullmatch(spec.model):
        return "模型名称不合法"
    if spec.mode not in ("ask", "engineer"):
        return "调用方式只能是 ask 或 engineer"
    if spec.mode == "engineer" and spec.provider != "codex":
        return "只有 Codex 能做工程（写代码并运行）"
    if (spec.effort or "") not in ("",) + EFFORTS:
        return "推理强度不合法"
    if not isinstance(spec.prompt, str) or not spec.prompt.strip():
        return "提示词为空"
    if not isinstance(spec.system_prompt, str) or len(spec.system_prompt) > MAX_SYSTEM_PROMPT:
        return "系统提示词过长"
    if not _number(spec.timeout_s) or spec.timeout_s < 1:
        return "超时时间不合法"
    if spec.budget_usd is not None and not (_number(spec.budget_usd) and 0 < spec.budget_usd <= 100):
        return "单次费用上限不合法"
    return ""


def _with_system(spec: CallSpec) -> str:
    return f"{spec.system_prompt}\n\n{spec.prompt}" if spec.system_prompt else spec.prompt


def command_for(spec: CallSpec, workdir, binary) -> tuple[list[str], bytes]:
    """Argument list and stdin bytes of one call (A.4); no process is started."""
    workdir = Path(workdir)
    if spec.provider == "codex":
        engineer = spec.mode == "engineer"
        search = "live" if spec.web_search and not engineer else "disabled"
        args = [str(binary), "exec", "--ignore-user-config", "--skip-git-repo-check",
                "--sandbox", "workspace-write" if engineer else "read-only",
                "--cd", str(workdir), "--color", "never", "--json",
                "--output-last-message", str(workdir / "_answer.md"), "--model", spec.model,
                "-c", f'model_reasoning_effort="{spec.effort or "high"}"', "-c", 'model_provider="openai"',
                "-c", 'forced_login_method="chatgpt"', "-c", 'approval_policy="never"', "-c", f'web_search="{search}"',
                "-c", "project_doc_max_bytes=0"]
        if not engineer:
            args += ["-c", "features.shell_tool=false"]
        for name in CODEX_FEATURES_OFF:
            args += ["-c", f"features.{name}=false"]
        args += ["-c", "skills.bundled.enabled=false"]
        if engineer:
            # Probed on this machine (2026-11-10): "elevated" cannot start the user's python.exe
            # (CreateProcessAsUserW access denied); "mxc" runs it and still blocks writes to the
            # research folder, the user profile and the network. Override with the env variable.
            mode = os.environ.get("RESEARCH_NOTEBOOK_CODEX_SANDBOX", "mxc")
            mode = mode if mode in {"mxc", "unelevated", "elevated"} else "mxc"
            args += ["-c", "sandbox_workspace_write.network_access=false", "-c", f'windows.sandbox="{mode}"']
        return args + ["-"], _utf8(_with_system(spec))
    if spec.provider == "agy":
        minutes = max(1, int(spec.timeout_s) // 60)
        args = [str(binary), "--input-format", "stream-json", "--output-format", "stream-json", "--model", spec.model,
                "--disable-slash-commands", "--print-timeout", f"{minutes}m", "--settings", str(workdir / "_agy_settings.json")]
        if spec.effort:
            args += ["--effort", spec.effort]
        line = json.dumps({"event": "user", "message": {"content": _with_system(spec)}}, ensure_ascii=True) + "\n"
        return args, line.encode("ascii")
    if spec.provider == "claude":
        budget = max(0.01, float(spec.budget_usd if spec.budget_usd is not None else DEFAULT_LIMITS["claude_per_call_usd"]))
        args = [str(binary), "-p", "--output-format", "json", "--model", spec.model, "--setting-sources", "user",
                "--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence", "--tools", "",
                "--permission-mode", "dontAsk", "--max-budget-usd", f"{budget:.2f}",
                "--system-prompt", spec.system_prompt or DEFAULT_SYSTEM]
        if spec.effort:
            args += ["--effort", spec.effort]
        return args, _utf8(spec.prompt)
    raise ValueError("未知的命令行")


# error classification ------------------------------------------------------------
NOT_LOGGED_IN = ("not logged in", "authentication required", "access token could not be refreshed", "please run /login",
                 "invalid api key", "not logged into antigravity", "secret keyring is locked", "auth error:")
QUOTA = ("usage limit", "quota", "rate limit", "resource_exhausted", "out of credits", "credit balance is too low",
         "spend limit", "spend cap", "temporarily limiting requests")
NETWORK = ("stream disconnected", "connection failed", "unable to connect", "request timed out", "no response from api",
           "overloaded", "at capacity", "error while reading the server response")
_CODES = {"not_logged_in": re.compile(r"(?<!\d)401(?!\d)"), "quota": re.compile(r"(?<!\d)429(?!\d)"),
          "network": re.compile(r"(?<!\d)529(?!\d)")}


def classify_error(text: str, subtype: str = "") -> str:
    """Error class of a failed call from its error text (case-insensitive substrings)."""
    if subtype == "error_max_budget_usd":
        return "budget"
    low = (text or "").lower()
    for name, words in (("not_logged_in", NOT_LOGGED_IN), ("quota", QUOTA), ("network", NETWORK)):
        if any(w in low for w in words) or _CODES[name].search(low):
            return name
    return "error"


ERROR_TEXT = {
    "not_installed": "没有找到 {name} 命令行",
    "not_logged_in": "{name} 未登录或登录已失效",
    "quota": "{name} 额度已用完或被限流",
    "budget": "超出单次费用上限",
    "network": "网络连接失败或服务繁忙",
    "error": "{name} 调用失败",
}
LOGIN_HINT = {"codex": "请在终端运行 codex login", "agy": "请在终端运行 agy 并登录", "claude": "请检查中转站设置"}


def error_text(error_class: str, provider: str, detail: str = "") -> str:
    base = ERROR_TEXT.get(error_class, ERROR_TEXT["error"]).format(name=PROVIDER_NAMES.get(provider, provider or "命令行"))
    detail = _redact(" ".join((detail or "").split()))[:300]
    text = f"{base}：{detail}" if detail else base
    hint = LOGIN_HINT.get(provider, "") if error_class == "not_logged_in" else ""
    return f"{text.rstrip('。. ')}。{hint}" if hint else text


# running one call ------------------------------------------------------------------
def _new_call_dir(provider: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = call_root() / f"{stamp}-{provider}-{uuid.uuid4().hex[:8]}"
    path.mkdir(parents=True, exist_ok=False)
    return path


def _feed(stream, data: bytes):
    try:
        stream.write(data)
        stream.flush()
    except (OSError, ValueError):
        pass  # the CLI exited before reading everything; its output tells why
    finally:
        try:
            stream.close()
        except (OSError, ValueError):
            pass


def _pump(stream, sink):
    try:
        while True:
            chunk = stream.read1(65536)
            if not chunk:
                break
            sink.write(chunk)
            sink.flush()
    except (OSError, ValueError):
        pass  # the sink was closed after a drain timeout
    finally:
        try:
            stream.close()  # closed by the reading thread itself, never while another thread reads it
        except (OSError, ValueError):
            pass


def _kill_tree(proc):
    """End the CLI and everything it started (taskkill /T on Windows)."""
    if os.name == "nt":
        taskkill = Path(os.environ.get("SystemRoot") or r"C:\Windows") / "System32" / "taskkill.exe"
        try:
            subprocess.run([str(taskkill) if taskkill.is_file() else "taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=20, creationflags=_NO_WINDOW)
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        if proc.poll() is None:
            proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def _wait(proc, cancel, timeout_s: float) -> str:
    """Poll every 0.25 s. Returns '' (exited), 'cancelled' or 'timeout' (process tree killed)."""
    deadline = time.monotonic() + timeout_s
    while proc.poll() is None:
        if cancel is not None and cancel.is_set():
            _kill_tree(proc)
            return "cancelled"
        if time.monotonic() >= deadline:
            _kill_tree(proc)
            return "timeout"
        if cancel is not None:
            cancel.wait(POLL_S)
        else:
            time.sleep(POLL_S)
    return ""


def _launch(args, workdir, stdin_bytes, stdout_path, stderr_path, cancel, timeout_s):
    """Start the CLI, feed stdin from a thread, drain both pipes into files. Returns (stopped, exit code, launch error)."""
    with open(stdout_path, "wb") as out_f, open(stderr_path, "wb") as err_f:
        try:
            proc = subprocess.Popen(args, cwd=str(workdir), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, creationflags=_NO_WINDOW | _NEW_GROUP, env=child_env())
        except OSError as exc:
            err_f.write(_utf8(f"{type(exc).__name__}: {exc}\n"))
            return "", None, exc
        threads = [threading.Thread(target=_feed, args=(proc.stdin, stdin_bytes), daemon=True),
                   threading.Thread(target=_pump, args=(proc.stdout, out_f), daemon=True),
                   threading.Thread(target=_pump, args=(proc.stderr, err_f), daemon=True)]
        for thread in threads:
            thread.start()
        stopped = _wait(proc, cancel, timeout_s)
        for thread in threads:
            thread.join(timeout=10 if stopped else 30)
        return stopped, proc.returncode, None


def _read_text(path: Path) -> str:
    try:
        return path.read_bytes().decode("utf-8", errors="replace").lstrip("\ufeff")
    except OSError:
        return ""


def _duration_text(seconds) -> str:
    seconds = int(seconds)
    return f"{seconds // 60} 分钟" if seconds >= 60 and seconds % 60 == 0 else f"{seconds} 秒"


def _time_limit_s(spec: CallSpec) -> float:
    """The earliest time limit of a call: ours, or agy's own --print-timeout (whole minutes)."""
    limit = float(spec.timeout_s)
    return min(limit, max(1, int(limit) // 60) * 60.0) if spec.provider == "agy" else limit


def _detail(parsed: dict, stderr_text: str, stdout_text: str, exit_code) -> str:
    for message in parsed["errors"]:
        if message and message.strip():
            return message.strip()
    for source in (stderr_text, "" if parsed["structured"] else stdout_text):
        lines = [x.strip() for x in source.splitlines() if x.strip()]
        if lines:
            return lines[-1]
    return f"退出码 {exit_code}" if exit_code not in (0, None) else "没有得到回复文本"


def _write_meta(workdir: Path, meta: dict):
    try:
        (workdir / "_call.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    except OSError:
        pass


def _result(spec: CallSpec, workdir: Path, started: float, *, text="", error_class="", error="", exit_code=None,
            parsed=None, rate_limits=None) -> CallResult:
    parsed = parsed or _parsed()
    model = spec.model if isinstance(spec.model, str) else ""
    provider = spec.provider if isinstance(spec.provider, str) else ""
    return CallResult(ok=not error_class, text=text if not error_class else "", provider=provider, model=model,
                      model_reported=parsed["model_reported"], family=family_of(model), bucket=bucket_of(provider, model),
                      usage=dict(parsed["usage"]), cost_usd=parsed["cost_usd"] if provider == "claude" else None,
                      session_id=parsed["session_id"], duration_s=round(time.monotonic() - started, 3), exit_code=exit_code,
                      error_class=error_class, error=error, workdir=Path(workdir), stdout_path=Path(workdir) / "_stdout.txt",
                      stderr_path=Path(workdir) / "_stderr.txt", rate_limits=rate_limits)


def _early(spec, workdir, started, meta, error_class, error) -> CallResult:
    """A call that never started a process (invalid spec, not installed, cancelled before start)."""
    try:
        (workdir / "_stdout.txt").write_bytes(b"")
        (workdir / "_stderr.txt").write_bytes(_utf8(error + "\n"))
    except OSError:
        pass
    meta.update(error_class=error_class, duration_s=0)
    _write_meta(workdir, meta)
    return _result(spec, workdir, started, error_class=error_class, error=error)


def run_call(spec: CallSpec, cancel: threading.Event | None = None) -> CallResult:
    """Run one CLI call. CLI failures come back as ok=False with error_class; nothing is retried."""
    started = time.monotonic()
    provider = spec.provider if spec.provider in PROVIDERS else "call"
    try:
        workdir = Path(spec.workdir).absolute() if spec.workdir is not None else _new_call_dir(provider)
        workdir.mkdir(parents=True, exist_ok=True)
    except (OSError, TypeError) as exc:
        workdir = Path(spec.workdir) if isinstance(spec.workdir, (str, Path)) else call_root()
        return _result(spec, workdir, started, error_class="error", error=f"无法创建工作目录：{exc}")
    fake = fake_mode()
    meta = {"provider": spec.provider, "model": spec.model, "mode": spec.mode, "label": spec.label,
            "started_at": _utc_iso(), "fake": fake}
    problem = validate_spec(spec)
    if problem:
        return _early(spec, workdir, started, meta, "error", problem)
    if cancel is not None and cancel.is_set():
        return _early(spec, workdir, started, meta, "cancelled", "已取消")
    binary = (FAKE_CLI if FAKE_CLI.is_file() else None) if fake else find_binary(spec.provider)
    if binary is None:
        return _early(spec, workdir, started, meta, "not_installed", error_text("not_installed", spec.provider))
    args, stdin_bytes = command_for(spec, workdir, binary)
    if fake:
        args = [sys.executable, str(FAKE_CLI), spec.provider, spec.mode] + args[1:]
    try:
        (workdir / "_answer.md").unlink(missing_ok=True)  # a stale answer must never pass for this call's reply
        if spec.provider == "agy":
            (workdir / "_agy_settings.json").write_text(json.dumps(AGY_SETTINGS, indent=2), encoding="utf-8")
    except OSError as exc:
        return _early(spec, workdir, started, meta, "error", f"无法准备工作目录：{exc}")
    meta["args"] = args
    stdout_path, stderr_path = workdir / "_stdout.txt", workdir / "_stderr.txt"
    try:
        stopped, exit_code, launch_error = _launch(args, workdir, stdin_bytes, stdout_path, stderr_path, cancel,
                                                   float(spec.timeout_s))
    except OSError as exc:  # the output files themselves could not be written
        return _result(spec, workdir, started, error_class="error", error=f"无法写入调用记录：{exc}")
    stdout_text, stderr_text = _read_text(stdout_path), _read_text(stderr_path)
    parsed = parse_output(spec.provider, stdout_text, workdir)
    text, error_class, error = "", "", ""
    if launch_error is not None:
        error_class = "not_installed" if isinstance(launch_error, FileNotFoundError) else "error"
        error = error_text(error_class, spec.provider, str(launch_error))
    elif stopped == "cancelled":
        error_class, error = "cancelled", "已取消"
    elif stopped == "timeout":
        error_class, error = "timeout", f"超过 {_duration_text(spec.timeout_s)}没有完成，已停止"
    elif exit_code == 0 and not parsed["failed"] and parsed["text"]:
        text = parsed["text"]
    else:
        blob = "\n".join(parsed["errors"] + [stderr_text] + ([] if parsed["structured"] else [stdout_text]))
        error_class = classify_error(blob, parsed["subtype"])
        limit = _time_limit_s(spec)
        if error_class in ("error", "network") and time.monotonic() - started >= max(limit - 5, limit * 0.9):
            # The CLI's own limit ended the call (agy --print-timeout): report it as the timeout it is.
            error_class, error = "timeout", f"超过 {_duration_text(limit)}没有完成，已停止"
        else:
            error = error_text(error_class, spec.provider, _detail(parsed, stderr_text, stdout_text, exit_code))
    rate_limits = None
    if spec.provider == "codex" and parsed["session_id"]:
        try:
            rate_limits = codex_rate_limits(parsed["session_id"])
        except Exception:  # noqa: BLE001 - rate limits are informational only
            rate_limits = None
    result = _result(spec, workdir, started, text=text, error_class=error_class, error=error, exit_code=exit_code,
                     parsed=parsed, rate_limits=rate_limits)
    meta.update(exit_code=exit_code, duration_s=result.duration_s, error_class=error_class, session_id=result.session_id)
    _write_meta(workdir, meta)
    return result


# output parsing --------------------------------------------------------------------
def _usage(input_tokens=0, output_tokens=0, cached=0, reasoning=0, total=0) -> dict:
    usage = {"input_tokens": _int(input_tokens), "output_tokens": _int(output_tokens),
             "cached_input_tokens": _int(cached), "reasoning_tokens": _int(reasoning)}
    usage["total_tokens"] = _int(total) or usage["input_tokens"] + usage["output_tokens"]
    return usage


def _parsed() -> dict:
    return {"text": "", "session_id": "", "usage": _usage(), "model_reported": "", "cost_usd": None,
            "errors": [], "failed": False, "subtype": "", "structured": False}


def _json_lines(text: str) -> list[dict]:
    events = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def parse_codex(stdout: str, workdir=None) -> dict:
    """``codex exec --json`` events; the final text prefers ``_answer.md`` (--output-last-message)."""
    res = _parsed()
    events = _json_lines(stdout)
    res["structured"] = bool(events)
    last, totals = "", {"in": 0, "cached": 0, "out": 0, "reasoning": 0}
    for event in events:
        kind = event.get("type")
        if kind == "thread.started":
            res["session_id"] = str(event.get("thread_id") or "")
        elif kind == "item.completed":
            item = event.get("item")
            if isinstance(item, dict) and (item.get("type") or item.get("item_type")) in ("agent_message", "assistant_message"):
                last = str(item.get("text") or "")
        elif kind == "turn.completed":
            usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}
            totals["in"] += _int(usage.get("input_tokens"))
            totals["cached"] += _int(usage.get("cached_input_tokens"))
            totals["out"] += _int(usage.get("output_tokens"))
            totals["reasoning"] += _int(usage.get("reasoning_output_tokens"))
        elif kind == "turn.failed":
            res["failed"] = True
            error = event.get("error")
            res["errors"].append(str(error.get("message") or "") if isinstance(error, dict) else str(error or ""))
        elif kind == "error":
            res["errors"].append(str(event.get("message") or ""))
    answer = _read_text(Path(workdir) / "_answer.md") if workdir is not None else ""
    res["text"] = answer.strip() or last.strip()
    res["usage"] = _usage(totals["in"], totals["out"], totals["cached"], totals["reasoning"])
    return res


def parse_agy(stdout: str) -> dict:
    """agy stream-json: ``init``, ``step_update``…, ``result`` (payload under the event's own name)."""
    res = _parsed()
    events = _json_lines(stdout)
    res["structured"] = bool(events)
    result, deltas = None, []
    for event in events:
        kind = event.get("event") or event.get("type")
        if kind == "init":
            init = event.get("init") if isinstance(event.get("init"), dict) else {}
            res["model_reported"] = str(init.get("model") or "")
            res["session_id"] = str(event.get("conversation_id") or res["session_id"])
        elif kind == "step_update":
            step = event.get("step_update") if isinstance(event.get("step_update"), dict) else {}
            if step.get("step_type") == "agent_response" and step.get("text_delta"):
                deltas.append(str(step["text_delta"]))
        elif kind == "result":
            result = event.get("result") if isinstance(event.get("result"), dict) else event
    if result is None and events and "status" in events[-1] and "event" not in events[-1]:
        result = events[-1]  # --output-format json: one bare result object
    if result is None:
        res["failed"] = True
        return res
    status = str(result.get("status") or "")
    res["session_id"] = str(result.get("conversation_id") or res["session_id"])
    res["text"] = str(result.get("response") or "").strip() or "".join(deltas).strip()
    usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
    res["usage"] = _usage(usage.get("input_tokens"), usage.get("output_tokens"), usage.get("cache_read_tokens"),
                          usage.get("thinking_tokens"), usage.get("total_tokens"))
    if status != "SUCCESS":
        res["failed"] = True
        res["errors"].append(str(result.get("error") or f"状态 {status or '未知'}"))
    elif result.get("error"):
        res["errors"].append(str(result["error"]))
    return res


def parse_claude(stdout: str) -> dict:
    """``claude -p --output-format json``: one result object."""
    res = _parsed()
    found = [e for e in _json_lines(stdout) if e.get("type") == "result"]
    obj = found[-1] if found else None
    if obj is None:
        try:
            whole = json.loads((stdout or "").strip() or "null")
        except ValueError:
            whole = None
        obj = whole if isinstance(whole, dict) else None
    if obj is None:
        res["failed"] = True
        return res
    res["structured"] = True
    res["subtype"] = str(obj.get("subtype") or "")
    res["session_id"] = str(obj.get("session_id") or "")
    cost = _number(obj.get("total_cost_usd"))
    res["cost_usd"] = float(cost) if cost is not None else None
    usage = obj.get("usage") if isinstance(obj.get("usage"), dict) else {}
    cached = _int(usage.get("cache_read_input_tokens"))
    prompt = _int(usage.get("input_tokens")) + cached + _int(usage.get("cache_creation_input_tokens"))
    res["usage"] = _usage(prompt, usage.get("output_tokens"), cached)
    models = obj.get("modelUsage") if isinstance(obj.get("modelUsage"), dict) else {}
    if models:
        res["model_reported"] = str(max(models, key=lambda k: _int((models[k] or {}).get("outputTokens"))
                                        if isinstance(models[k], dict) else 0))
    if obj.get("is_error") or res["subtype"] != "success":
        res["failed"] = True
        res["errors"].append(str(obj.get("result") or ""))
        res["errors"] += [e if isinstance(e, str) else json.dumps(e, ensure_ascii=False) for e in obj.get("errors") or []]
        if obj.get("api_error_status"):
            res["errors"].append(f"API Error: {obj['api_error_status']}")
    else:
        res["text"] = str(obj.get("result") or "").strip()
    return res


def parse_output(provider: str, stdout: str, workdir=None) -> dict:
    if provider == "codex":
        return parse_codex(stdout, workdir)
    if provider == "agy":
        return parse_agy(stdout)
    if provider == "claude":
        return parse_claude(stdout)
    return _parsed()


# Codex rate limits (no model call) --------------------------------------------------
ROLLOUT_TAIL = 1 << 20
ROLLOUT_MAX = 64 << 20


def _codex_home():
    home = os.environ.get("CODEX_HOME", "").strip()
    if home:
        return Path(home)
    return None if fake_mode() else Path.home() / ".codex"  # fake mode never reads the real account


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _snapshot(limits: dict, observed_at: str) -> dict:
    out = {"plan_type": limits.get("plan_type"), "observed_at": observed_at, "weekly": None, "five_hour": None,
           "credits": None, "reached": limits.get("rate_limit_reached_type")}
    for value in limits.values():
        if not isinstance(value, dict) or "window_minutes" not in value:
            continue
        minutes = _int(value.get("window_minutes"))
        key = {10080: "weekly", 300: "five_hour"}.get(minutes)
        used = _number(value.get("used_percent"))
        if key and out[key] is None:
            out[key] = {"used_percent": float(used or 0), "resets_at": _int(value.get("resets_at")), "window_minutes": minutes}
    credits = limits.get("credits")
    if isinstance(credits, dict):  # never the balance
        out["credits"] = {"has_credits": bool(credits.get("has_credits")), "unlimited": bool(credits.get("unlimited"))}
    return out


def _last_token_count(data: bytes):
    for raw in reversed(data.splitlines()):
        if b"token_count" not in raw or b"rate_limits" not in raw:
            continue
        try:
            line = json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError:
            continue
        payload = line.get("payload") if isinstance(line, dict) and line.get("type") == "event_msg" else None
        if isinstance(payload, dict) and payload.get("type") == "token_count" and isinstance(payload.get("rate_limits"), dict):
            return _snapshot(payload["rate_limits"], str(line.get("timestamp") or ""))
    return None


def _rollout_limits(path: Path):
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > ROLLOUT_TAIL:
                handle.seek(size - ROLLOUT_TAIL)
                data = handle.read().split(b"\n", 1)[-1]  # drop the cut first line
            else:
                data = handle.read()
            snap = _last_token_count(data)
            if snap is None and ROLLOUT_TAIL < size <= ROLLOUT_MAX:
                handle.seek(0)
                snap = _last_token_count(handle.read())
    except OSError:
        return None
    return snap


def codex_rate_limits(thread_id: str = "") -> dict | None:
    """Latest Codex rate-limit snapshot from ``~/.codex/sessions`` rollout files (windows identified by length)."""
    home = _codex_home()
    sessions = home / "sessions" if home else None
    if sessions is None or not sessions.is_dir():
        return None
    if thread_id and not THREAD_RE.fullmatch(str(thread_id)):
        return None
    try:
        pattern = f"*/*/*/rollout-*{thread_id}.jsonl" if thread_id else "*/*/*/rollout-*.jsonl"
        files = sorted((p for p in sessions.glob(pattern) if p.is_file()), key=_mtime, reverse=True)
    except OSError:
        return None
    for path in files[:10]:
        snap = _rollout_limits(path)
        if snap:
            return snap
    return None


# provider status ---------------------------------------------------------------
def _codex_login(binary) -> dict:
    code, out = _run_quiet([binary, "login", "status"], 12)
    low = out.lower()
    if code is None:
        return {"logged_in": None, "login_text": "无法检查登录状态"}
    if code == 0 and "logged in using chatgpt" in low:
        return {"logged_in": True, "login_text": "已登录（ChatGPT）"}
    if code == 0 and "logged in" in low and "not logged in" not in low:
        return {"logged_in": False, "login_text": "没有用 ChatGPT 登录（本系统只用订阅额度）"}
    return {"logged_in": False, "login_text": "未登录，请在终端运行 codex login"}


def _agy_login(binary) -> dict:
    code, out = _run_quiet([binary, "models"], 20)  # lists models; sends no prompt
    low = out.lower()
    if "not logged into antigravity" in low or "authentication required" in low:
        return {"logged_in": False, "login_text": "未登录，请在终端运行 agy 并登录"}
    if code == 0 and out.strip():
        return {"logged_in": True, "login_text": "已登录"}
    return {"logged_in": None, "login_text": "无法确认登录状态"}


def _claude_login(binary) -> dict:
    code, out = _run_quiet([binary, "auth", "status", "--json"], 20)
    start, end = out.find("{"), out.rfind("}")
    try:
        data = json.loads(out[start:end + 1]) if 0 <= start < end else {}
    except ValueError:
        data = {}
    if isinstance(data, dict) and data.get("loggedIn") is True:
        return {"logged_in": True, "login_text": "已登录"}
    # The relay credential lives in the user's Claude settings, which `auth status` does not report.
    return {"logged_in": None, "login_text": "中转站凭据无法离线确认，请用“测试连接”检查"}


def provider_status(check_login: bool = False) -> dict:
    """Per provider: installed, path, version and logged_in (None = 未检查)."""
    fake = fake_mode()
    out = {}
    for provider in PROVIDERS:
        info = {"name": PROVIDER_NAMES[provider], "installed": False, "path": "", "version": "",
                "logged_in": None, "login_text": "未检查", "fake": fake}
        if fake:
            info.update(installed=True, path=str(FAKE_CLI), version="模拟", logged_in=True, login_text="已登录（模拟）")
        else:
            binary = find_binary(provider)
            if binary is None:
                info["login_text"] = "未安装"
            else:
                info.update(installed=True, path=str(binary), version=binary_version(binary))
                if provider == "codex":
                    info.update(_codex_login(binary))
                elif check_login:
                    info.update((_agy_login if provider == "agy" else _claude_login)(binary))
        out[provider] = info
    return out


# settings ----------------------------------------------------------------------
_GPT = {"provider": "codex", "model": "gpt-6.1-sol", "effort": "high"}
DEFAULT_LIMITS = {"codex_stop_percent": 90, "claude_per_call_usd": 0.5, "claude_month_usd": 10.0, "agy_cooldown_hours": 5,
                  "rounds_default": 2, "max_runs_per_round": 2, "call_timeout_minutes": 25}
DEFAULT_GIT = {"auto_commit": True, "auto_push": True, "remote": "origin", "branch": "main"}
DEFAULT_SETTINGS = {
    "schema": 1,
    "roles": {
        "author": [dict(_GPT), {"provider": "claude", "model": "claude-opus-5-5"}, {"provider": "agy", "model": "gemini-4-argon-high"}],
        "reviewer": [{"provider": "agy", "model": "gemini-4-argon-high"}, dict(_GPT), {"provider": "claude", "model": "claude-opus-5-5"}],
        "engineer": [dict(_GPT)],
        "writer": [{"provider": "agy", "model": "gemini-4-argon-high"}, dict(_GPT)],
        "drift": [{"provider": "claude", "model": "claude-haiku-5-5"}, {"provider": "agy", "model": "gemini-3.8-flash-medium"}],
        "referee": [{"provider": "claude", "model": "claude-opus-5-5"},
                    {"provider": "agy", "model": "gemini-4-argon-high"}, dict(_GPT)],
    },
    "limits": dict(DEFAULT_LIMITS),
    "git": dict(DEFAULT_GIT),
}
LIMIT_RULES = {  # key: (label, kind, low, high)
    "codex_stop_percent": ("Codex 停用线（%）", float, 1, 100),
    "claude_per_call_usd": ("Claude 单次费用上限（美元）", float, 0.01, 100),
    "claude_month_usd": ("Claude 每月费用上限（美元）", float, 0, 100000),
    "agy_cooldown_hours": ("Antigravity 额度用完后的暂停时长（小时）", float, 0, 168),
    "rounds_default": ("默认轮数", int, 1, 5),
    "max_runs_per_round": ("每轮最多验证运行次数", int, 0, 10),
    "call_timeout_minutes": ("单次调用超时（分钟）", int, 1, 240),
}


def _limit_problem(key: str, value) -> str:
    label, kind, low, high = LIMIT_RULES[key]
    number = _number(value)
    if number is None or (kind is int and number != int(number)):
        return f"{label}应为{'整数' if kind is int else '数字'}"
    return "" if low <= number <= high else f"{label}应在 {low} 到 {high} 之间"


def _git_problem(key: str, value) -> str:
    if key in ("auto_commit", "auto_push"):
        return "" if isinstance(value, bool) else f"{'自动提交' if key == 'auto_commit' else '自动推送'}应为开或关"
    if key == "remote":
        return "" if isinstance(value, str) and REMOTE_RE.fullmatch(value) else "远程仓库名称不合法"
    if key == "branch":
        ok = isinstance(value, str) and BRANCH_RE.fullmatch(value) and ".." not in value and not value.endswith(("/", ".lock"))
        return "" if ok else "分支名称不合法"
    return ""


def _candidate_problem(role: str, index: int, item) -> str:
    where = f"{ROLE_LABELS.get(role, role)}的第 {index} 个候选"
    if not isinstance(item, dict):
        return f"{where}格式不对"
    if item.get("provider") not in PROVIDERS:
        return f"{where}：命令行只能是 codex、agy 或 claude"
    model = item.get("model")
    if not isinstance(model, str) or not model.strip():
        return f"{where}：模型不能为空"
    if not MODEL_RE.fullmatch(model):
        return f"{where}：模型名称只能含字母、数字和 . _ - : /"
    if item.get("effort", "") not in ("",) + EFFORTS:
        return f"{where}：推理强度不合法"
    if role == "engineer" and item["provider"] != "codex":
        return f"{where}：工程只能用 Codex"
    if item["provider"] == "agy" and family_of(model) != "google":
        return f"{where}：Antigravity 仅用于 Gemini 模型"
    return ""


def validate_settings(data) -> list[str]:
    """Chinese problems of a settings object about to be saved ([] = valid). Missing keys are allowed."""
    if not isinstance(data, dict):
        return ["设置应为一个对象"]
    problems = [] if data.get("schema", 1) == 1 else ["不支持的设置版本"]
    roles = data.get("roles", {})
    if not isinstance(roles, dict):
        problems.append("角色设置格式不对")
    else:
        for role, items in roles.items():
            if not isinstance(items, list) or not items:
                problems.append(f"{ROLE_LABELS.get(role, role)}至少需要一个候选模型")
                continue
            if len(items) > 10:
                problems.append(f"{ROLE_LABELS.get(role, role)}的候选最多 10 个")
            problems += [p for i, item in enumerate(items, 1) if (p := _candidate_problem(role, i, item))]
    for section, rules, check in (("limits", LIMIT_RULES, _limit_problem), ("git", DEFAULT_GIT, _git_problem)):
        values = data.get(section, {})
        if not isinstance(values, dict):
            problems.append(f"{'额度' if section == 'limits' else 'git'}设置格式不对")
            continue
        problems += [p for key in rules if key in values and (p := check(key, values[key]))]
    return problems


def normalize_settings(data) -> tuple[dict, list[str]]:
    """Fill missing keys from the defaults and replace invalid known values; unknown keys are kept."""
    problems = []
    out = copy.deepcopy(data) if isinstance(data, dict) else {}
    out.setdefault("schema", 1)
    roles = out.get("roles")
    if not isinstance(roles, dict):
        if "roles" in out:
            problems.append("角色设置格式不对，已用默认值")
        roles = {}
    for role, default in DEFAULT_SETTINGS["roles"].items():
        items = roles.get(role)
        if items is None:
            roles[role] = copy.deepcopy(default)
            continue
        good = [item for i, item in enumerate(items, 1) if not _candidate_problem(role, i, item)] if isinstance(items, list) else []
        if not good:
            problems.append(f"{ROLE_LABELS[role]}的候选无效，已用默认值")
            roles[role] = copy.deepcopy(default)
        elif len(good) != len(items):
            problems.append(f"{ROLE_LABELS[role]}有无效的候选，已忽略")
            roles[role] = good
    out["roles"] = roles
    for section, defaults, check in (("limits", DEFAULT_LIMITS, _limit_problem), ("git", DEFAULT_GIT, _git_problem)):
        values = out.get(section)
        if not isinstance(values, dict):
            if section in out:
                problems.append(f"{section} 格式不对，已用默认值")
            values = {}
        for key, default in defaults.items():
            problem = check(key, values[key]) if key in values else ""
            if key not in values or problem:
                if problem:
                    problems.append(problem + "，已用默认值")
                values[key] = default
        out[section] = values
    for key, (_, kind, _, _) in LIMIT_RULES.items():
        if kind is int:
            out["limits"][key] = int(out["limits"][key])
    return out, problems


_SETTINGS_LOCK = threading.Lock()


class Settings:
    """``00_模型与额度.json`` at the research root (tracked by git, never holds secrets)."""

    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / SETTINGS_NAME
        self.last_error = ""  # why the file was (partly) ignored on the last load, for display

    def _read(self):
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return None, ""
        return raw, hashlib.sha256(raw).hexdigest()

    def load(self) -> tuple[dict, str]:
        """(data with defaults filled in, revision = sha256 of the file bytes, '' when missing)."""
        self.last_error = ""
        try:
            raw, revision = self._read()
        except OSError as exc:
            self.last_error = f"无法读取 {SETTINGS_NAME}：{exc}"
            return copy.deepcopy(DEFAULT_SETTINGS), ""
        if raw is None:
            return copy.deepcopy(DEFAULT_SETTINGS), ""
        try:
            data = json.loads(raw.decode("utf-8-sig"))
            if not isinstance(data, dict):
                raise ValueError("最外层应为对象")
        except ValueError as exc:
            # Keep the revision of the broken file so that saving from the page can replace it.
            self.last_error = f"{SETTINGS_NAME} 无法解析，暂用默认设置：{exc}"
            return copy.deepcopy(DEFAULT_SETTINGS), revision
        data, problems = normalize_settings(data)
        self.last_error = "；".join(problems)
        return data, revision

    def save(self, data, revision: str = "") -> tuple[dict, str]:
        """Validate, check the revision and write atomically. Raises ValueError (Chinese) when invalid or stale."""
        problems = validate_settings(data)
        if problems:
            raise ValueError("设置有误：" + "；".join(problems[:6]))
        clean, _ = normalize_settings(data)
        payload = (json.dumps(clean, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        with _SETTINGS_LOCK:
            _, current = self._read()
            if (revision or "") != current:
                raise ConflictError("设置已在别处修改，请刷新后再保存")
            self.root.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_name(f".tmp-{uuid.uuid4().hex[:8]}-{self.path.name}")
            try:
                temp.write_bytes(payload)
                os.replace(temp, self.path)
            finally:
                temp.unlink(missing_ok=True)
        return clean, hashlib.sha256(payload).hexdigest()


# usage ledger --------------------------------------------------------------------
_USAGE_LOCK = threading.Lock()


def _empty_summary(bucket: str) -> dict:
    summary = {"calls_today": 0, "calls_week": 0, "tokens_week": 0, "last_error_class": "", "last_quota_error_at": "",
               "last_call_at": "", "last_ok_at": ""}
    if bucket == "claude":
        summary["cost_month_usd"] = 0.0
    if bucket == "codex":
        summary["rate_limits"] = None
    return summary


def _newer_limits(first, second):
    """The fresher of two rate-limit snapshots (by observed_at); ties go to the second."""
    if not isinstance(first, dict):
        return second if isinstance(second, dict) else None
    if not isinstance(second, dict):
        return first
    a, b = _parse_time(first.get("observed_at")), _parse_time(second.get("observed_at"))
    if a and b:
        return first if a > b else second
    return second if b or not a else first


class UsageLedger:
    """One JSON line per call in ``<root>/.runtime/usage.jsonl`` (append-only, thread-safe)."""

    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / ".runtime" / "usage.jsonl"

    def record(self, result, role: str = "", ref: str = "", round_id: str = "") -> dict:
        get = lambda name, default=None: getattr(result, name, default)  # noqa: E731
        usage = get("usage") if isinstance(get("usage"), dict) else {}
        line = {"time": _utc_iso(), "provider": get("provider", "") or "", "model": get("model", "") or "",
                "family": get("family", "") or "", "bucket": get("bucket", "") or "", "role": role or "", "ref": ref or "",
                "round_id": round_id or "", "ok": bool(get("ok", False)), "error_class": get("error_class", "") or "",
                "usage": {k: _int(v) for k, v in usage.items()}, "cost_usd": _number(get("cost_usd")),
                "duration_s": round(float(_number(get("duration_s")) or 0), 2)}
        if get("model_reported"):
            line["model_reported"] = str(get("model_reported"))
        if isinstance(get("rate_limits"), dict):
            line["rate_limits"] = get("rate_limits")
        data = (json.dumps(line, ensure_ascii=False, default=str) + "\n").encode("utf-8")
        with _USAGE_LOCK:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "ab") as handle:
                handle.write(data)
        return line

    def rows(self) -> list[dict]:
        try:
            raw = self.path.read_bytes()
        except OSError:
            return []
        rows = []
        for line in raw.decode("utf-8", errors="replace").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
        return rows

    def summary(self, now=None) -> dict:
        """Per bucket: calls today/this week (local time, week from Monday), tokens this week, last error class,
        last quota error; claude: cost this month; codex: the freshest rate limits (ledger or rollout files)."""
        now = _aware(now)
        day0 = now.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        week0, month0 = day0 - timedelta(days=day0.weekday()), day0.replace(day=1)
        out = {bucket: _empty_summary(bucket) for bucket in BUCKETS}
        marks, ledger_limits = {}, None
        for row in self.rows():
            when = _parse_time(row.get("time"))
            if when is None or when > now:
                continue
            bucket = row.get("bucket") or bucket_of(row.get("provider", ""), row.get("model", ""))
            if not isinstance(bucket, str) or not bucket:
                continue
            item = out.setdefault(bucket, _empty_summary(bucket))
            mark = marks.setdefault(bucket, {})
            usage = row.get("usage") if isinstance(row.get("usage"), dict) else {}
            if when >= day0:
                item["calls_today"] += 1
            if when >= week0:
                item["calls_week"] += 1
                item["tokens_week"] += _int(usage.get("total_tokens"))
            if "cost_month_usd" in item and when >= month0:
                item["cost_month_usd"] += float(_number(row.get("cost_usd")) or 0)
            for key, wanted in (("last_call_at", True), ("last_quota_error_at", row.get("error_class") == "quota"),
                                ("last_ok_at", bool(row.get("ok")))):
                if wanted and (key not in mark or when >= mark[key]):
                    mark[key] = when
                    item[key] = str(row.get("time"))
                    if key == "last_call_at":
                        item["last_error_class"] = str(row.get("error_class") or "")
            if bucket == "codex" and isinstance(row.get("rate_limits"), dict):
                ledger_limits = _newer_limits(ledger_limits, row["rate_limits"])
        if "cost_month_usd" in out["claude"]:
            out["claude"]["cost_month_usd"] = round(out["claude"]["cost_month_usd"], 6)
        try:
            live = codex_rate_limits()
        except Exception:  # noqa: BLE001 - informational only
            live = None
        out["codex"]["rate_limits"] = _newer_limits(ledger_limits, live)
        return out


# quota ---------------------------------------------------------------------------
def _limits(settings) -> dict:
    values = dict(DEFAULT_LIMITS)
    given = settings.get("limits") if isinstance(settings, dict) else None
    if isinstance(given, dict):
        values.update({k: given[k] for k in DEFAULT_LIMITS if k in given and not _limit_problem(k, given[k])})
    return values


def _quota(available: bool, level: str, text: str, detail: dict) -> dict:
    return {"available": available, "level": level, "text": text, "detail": detail}


def _cooldown_until(item: dict, hours, now: datetime, after=None):
    """End of the pause after the bucket's last quota error, or None when not blocked.
    A later successful call, or a usage snapshot newer than the error, lifts it."""
    error = _parse_time(item.get("last_quota_error_at"))
    if error is None:
        return None
    ok = _parse_time(item.get("last_ok_at"))
    if (ok and ok > error) or (after and after >= error):
        return None
    until = error + timedelta(hours=float(hours))
    return until if until > now else None


def _base_detail(bucket: str, item: dict, installed: bool) -> dict:
    return {"label": BUCKET_LABELS.get(bucket, bucket), "installed": installed, "calls_today": item["calls_today"],
            "calls_week": item["calls_week"], "tokens_week": item["tokens_week"],
            "last_error_class": item["last_error_class"], "last_quota_error_at": item["last_quota_error_at"]}


def _codex_quota(item: dict, limits: dict, installed: bool, now: datetime) -> dict:
    stop = float(limits["codex_stop_percent"])
    limits_seen = item.get("rate_limits") if isinstance(item.get("rate_limits"), dict) else {}
    detail = {**_base_detail("codex", item, installed), "stop_percent": stop, "rate_limits": limits_seen or None}
    if not installed:
        return _quota(False, "blocked", "未找到 Codex 命令行", detail)
    windows = []
    for key, name in (("weekly", "周额度"), ("five_hour", "5 小时额度")):
        window = limits_seen.get(key)
        if not isinstance(window, dict):
            continue
        resets = _int(window.get("resets_at"))
        if resets and resets <= now.timestamp():
            continue  # that window has been reset since the snapshot
        windows.append((name, float(_number(window.get("used_percent")) or 0), resets))
    until = _cooldown_until(item, limits["agy_cooldown_hours"], now, after=_parse_time(limits_seen.get("observed_at")))
    if until:
        detail["cooldown_until"] = until.isoformat(timespec="seconds")
        return _quota(False, "blocked", f"额度已用完，约 {_local_hm(until)} 后再试", detail)
    if not windows:
        return _quota(True, "ok", "额度未知（还没有读到用量记录）", detail)
    text = "；".join(f"{name}已用 {used:.0f}%" + (f"（{_local_hm(datetime.fromtimestamp(resets, timezone.utc))} 重置）" if resets else "")
                    for name, used, resets in windows)
    worst = max(used for _, used, _ in windows)
    if worst >= stop or limits_seen.get("reached"):
        return _quota(False, "blocked", f"{text}，已停用（停用线 {stop:.0f}%）", detail)
    return _quota(True, "warn" if worst >= stop - 10 else "ok", text, detail)


def _agy_quota(bucket: str, item: dict, limits: dict, installed: bool, now: datetime) -> dict:
    detail = {**_base_detail(bucket, item, installed), "cooldown_hours": limits["agy_cooldown_hours"]}
    if not installed:
        return _quota(False, "blocked", "未找到 Antigravity 命令行", detail)
    until = _cooldown_until(item, limits["agy_cooldown_hours"], now)
    if until:
        detail["cooldown_until"] = until.isoformat(timespec="seconds")
        return _quota(False, "blocked", f"额度用完，约 {_local_hm(until)} 后恢复", detail)
    return _quota(True, "ok", f"本周 {item['calls_week']} 次，额度未公开", detail)


def _claude_quota(item: dict, limits: dict, installed: bool, now: datetime) -> dict:
    cost, month, per_call = float(item.get("cost_month_usd") or 0), float(limits["claude_month_usd"]), float(limits["claude_per_call_usd"])
    detail = {**_base_detail("claude", item, installed), "cost_month_usd": round(cost, 4), "month_usd": month, "per_call_usd": per_call}
    if not installed:
        return _quota(False, "blocked", "未找到 Claude Code 命令行", detail)
    text = f"本月 {_usd(cost)} / {_usd(month)}"
    if cost + per_call > month:
        return _quota(False, "blocked", f"{text}，已到月度上限", detail)
    until = _cooldown_until(item, limits["agy_cooldown_hours"], now)
    if until:
        detail["cooldown_until"] = until.isoformat(timespec="seconds")
        return _quota(False, "blocked", f"{text}，中转站额度不足（约 {_local_hm(until)} 后再试）", detail)
    return _quota(True, "warn" if cost + per_call > 0.8 * month else "ok", text, detail)


def quota_status(root, settings, now=None) -> dict:
    """Quota per bucket ('codex', 'agy:gemini', 'agy:other', 'claude'): {available, level, text, detail}."""
    now = _aware(now)
    limits = _limits(settings)
    summary = UsageLedger(root).summary(now)
    fake = fake_mode()
    installed = {p: fake or find_binary(p) is not None for p in PROVIDERS}
    return {"codex": _codex_quota(summary["codex"], limits, installed["codex"], now),
            "agy:gemini": _agy_quota("agy:gemini", summary["agy:gemini"], limits, installed["agy"], now),
            "agy:other": _agy_quota("agy:other", summary["agy:other"], limits, installed["agy"], now),
            "claude": _claude_quota(summary["claude"], limits, installed["claude"], now)}


# roles ---------------------------------------------------------------------------
class RoleError(Exception):
    """No available model for a role (message in Chinese)."""


FAMILY_CHECK_ROLES = ("reviewer", "drift", "referee")


def _role_candidates(settings, role: str) -> list:
    table = settings.get("roles") if isinstance(settings, dict) else None
    items = table.get(role) if isinstance(table, dict) else None
    if not isinstance(items, list) or not items:
        items = DEFAULT_SETTINGS["roles"].get(role, [])
    return [c for c in items if isinstance(c, dict)]


def _pick(settings, quota: dict, role: str, avoid_family: str = "") -> dict:
    reasons = []
    for cand in _role_candidates(settings, role):
        provider, model = cand.get("provider"), cand.get("model")
        if provider not in PROVIDERS or not isinstance(model, str) or not MODEL_RE.fullmatch(model):
            continue
        family, bucket = family_of(model), bucket_of(provider, model)
        label = model_label(model, family)
        if role == "engineer" and provider != "codex":
            reasons.append(f"{label} 不能做工程（只能用 Codex）")
            continue
        if avoid_family and family == avoid_family:
            reasons.append(f"{label} 与作者同属 {FAMILY_NAMES.get(family, family)}")
            continue
        state = quota.get(bucket) if isinstance(quota, dict) else None
        if isinstance(state, dict) and not state.get("available", True):
            reasons.append(f"{label} 暂不可用（{state.get('text') or '额度不足'}）")
            continue
        effort = cand.get("effort") if cand.get("effort") in EFFORTS else ""
        return {"provider": provider, "model": model, "effort": effort, "family": family, "bucket": bucket, "label": label}
    raise RoleError(f"{ROLE_LABELS.get(role, role)}：" + ("；".join(reasons) if reasons else "没有配置候选模型"))


def resolve_roles(settings, quota, roles) -> dict:
    """Author first (first available candidate); reviewer, drift and referee must come from another model family."""
    wanted = list(dict.fromkeys(roles or []))
    strict_family = (settings.get("strict_family", True) is not False) if isinstance(settings, dict) else True
    author = None
    if "author" in wanted or (strict_family and any(r in FAMILY_CHECK_ROLES for r in wanted)):
        author = _pick(settings, quota or {}, "author")
    out = {}
    for role in wanted:
        if role == "author":
            out[role] = author
        else:
            avoid = author["family"] if (strict_family and author and role in FAMILY_CHECK_ROLES) else ""
            out[role] = _pick(settings, quota or {}, role, avoid)
    return out
