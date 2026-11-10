# v2 technical spec — layered AI discussion, three providers, ledger, git

App root: `<project-root>` (Python 3.12 stdlib only, run with `py -3.12 -X utf8`).
User-facing summary of the workflow: `工作流说明.md` (read it first; it is the product requirement).
Existing backend: `notebook/store.py` (records, revisions, history), `notebook/server.py` (loopback HTTP), `notebook/semantic_jobs.py` (Codex job pattern), `notebook/prompts.py`. Existing UI: `static/index.html`, `static/app.js` (kept as "经典版" at `/classic`).
Existing tests: `py -3.12 -X utf8 -m unittest discover -s tests` (100 tests, all must keep passing).

Hard rules for every module:
- Standard library only (no pip installs). UTF-8 everywhere. Windows paths may contain Chinese characters and spaces.
- Never read, print, log or copy secret values (e.g. `ANTHROPIC_AUTH_TOKEN`, `~/.codex/auth.json`). Never write secrets into the research root.
- Subprocesses: argument lists only, never `shell=True`; prompts go through stdin (Windows command line limit 32K).
- Do not modify files outside the module(s) you own unless the task says so. Do not touch `示例研究/` data except in temp copies inside tests.
- Tests use `unittest` + `tempfile` directories; no real model calls in tests (use the fake CLI, section A.6).
- All user-visible strings are Simplified Chinese, plain and short.

Item references used everywhere:
- `FW` — the thesis framework layer (one per project). Folder: `01_论文框架/`.
- `F01` — one framework record (`01_论文框架/F01_<slug>.md`), a document inside the FW layer.
- `C04` — a chapter (= existing "content" record, `02_研究内容/C04_<slug>/`). Chapter number = position in a framework's `content_ids` (1-based).
- `C04-Q01` — a small question (`02_研究内容/C04_<slug>/C04-Q01_<slug>/`).

Per-item workflow files (all plain UTF-8 Markdown/JSON, tracked by git):

| level | item dir | anchor | ledger | ledger log | rounds | document |
|---|---|---|---|---|---|---|
| FW | `01_论文框架/` | `锚点.md` | `台账.md` | `台账日志.md` | `讨论/` | the framework records (fields mainline/chapters/risks) |
| chapter | content dir | `锚点.md` | `台账.md` | `台账日志.md` | `讨论/` | `草稿.md` |
| question | question dir | `锚点.md` | `台账.md` | `台账日志.md` | `讨论/` | `推导笔记.md` |

Also per item: `讨论/状态.json` (stage of the research-chapter flow, open "需要你决定" items) and, for questions, `复推/V01.md…` (independent re-derivation records).

Chapter roles (stored as metadata key `role` in the content record): `intro` 绪论, `theory` 理论, `research` 研究问题, `framework` 随框架而定, `conclusion` 总结与展望, `other` 其他.

---

## A. `notebook/agents.py` — providers, calls, usage, quota, settings

### A.1 Binaries (verified on this machine; resolve in this order, never trust PATH order blindly)
- codex: newest `%LOCALAPPDATA%/Microsoft/WinGet/Packages/OpenAI.Codex_*/codex-x86_64-pc-windows-msvc.exe`; then `shutil.which('codex.exe')` **only if** its `--version` is ≥ 0.160 (the stale `%LOCALAPPDATA%/OpenAI/Codex/bin/codex.exe` is 0.130 and fails on the user's config). Verified: 0.166.0, `login status` → "Logged in using ChatGPT".
- agy: `%LOCALAPPDATA%/Microsoft/WinGet/Packages/Google.AntigravityCLI_*/agy.exe`, else `shutil.which('agy.exe')`. Verified 1.5.2.
- claude: `shutil.which('claude.exe')`, else `%LOCALAPPDATA%/Programs/claude/claude.exe`. Always call the `.exe`, never `.cmd` shims.
- Allow override via env `RESEARCH_NOTEBOOK_CODEX`, `RESEARCH_NOTEBOOK_AGY`, `RESEARCH_NOTEBOOK_CLAUDE` (absolute paths).

### A.2 Model families
`family_of(model) -> 'openai'|'google'|'anthropic'|'unknown'`: `gpt-*`, `o<digit>*`, `codex*`, `gpt-oss-*` → openai; `gemini-*` → google; `claude-*` → anthropic. Display names: openai→"GPT", google→"Gemini", anthropic→"Claude", human→"你", system→"系统".
Quota bucket: `bucket_of(provider, model)` → `codex` | `agy:gemini` (agy + google family) | `agy:other` (agy + claude/gpt models; Antigravity counts Claude+GPT in one bucket) | `claude` (relay).

### A.3 Call API
```python
@dataclass
class CallSpec:
    provider: str                 # 'codex' | 'agy' | 'claude'
    model: str
    prompt: str
    mode: str = 'ask'             # 'ask' (no tools, read-only) | 'engineer' (codex only: write+run code in workdir)
    effort: str = ''              # codex: model_reasoning_effort ('' -> 'high')
    workdir: Path | None = None   # None -> new empty dir under call_root()
    timeout_s: int = 1500
    budget_usd: float | None = None   # claude only -> --max-budget-usd
    system_prompt: str = ''       # claude: --system-prompt; codex/agy: prepended to the prompt
    web_search: bool = False      # codex ask only: web_search="live" (default "disabled")
    label: str = ''

@dataclass
class CallResult:
    ok: bool
    text: str                     # final answer text ('' on failure)
    provider: str
    model: str                    # requested
    model_reported: str           # from output if any, else ''
    family: str
    bucket: str
    usage: dict                   # ints: input_tokens, output_tokens, cached_input_tokens, reasoning_tokens, total_tokens (missing -> 0)
    cost_usd: float | None        # claude only (client estimate), else None
    session_id: str
    duration_s: float
    exit_code: int | None
    error_class: str              # '' | not_installed | not_logged_in | quota | budget | network | timeout | cancelled | error
    error: str                    # short Chinese message for the UI
    workdir: Path
    stdout_path: Path             # raw stdout saved in workdir/_stdout.txt
    stderr_path: Path             # raw stderr saved in workdir/_stderr.txt
    rate_limits: dict | None      # codex only, see A.5

def call_root() -> Path      # %LOCALAPPDATA%/ResearchNotebook/calls  (ASCII path under the user's home)
def work_root() -> Path      # %LOCALAPPDATA%/ResearchNotebook/work
def run_call(spec: CallSpec, cancel: threading.Event | None = None) -> CallResult
```
Behavior:
- `subprocess.Popen(args, cwd=workdir, stdin=PIPE, stdout=PIPE, stderr=PIPE, creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP, env=env)`; write UTF-8 bytes to stdin from a thread, close stdin; read stdout/stderr in threads (avoid pipe deadlock); poll every 0.25 s; on `cancel.is_set()` or timeout run `taskkill /PID <pid> /T /F` (Windows) / `proc.kill()` (other), then drain. Decode output as UTF-8 with `errors='replace'`.
- Environment: copy `os.environ`, remove `OPENAI_API_KEY, CODEX_API_KEY, OPENAI_BASE_URL, OPENAI_ORG_ID, OPENAI_ORGANIZATION, CODEX_THREAD_ID, CODEX_SESSION_ID, CODEX_INTERNAL_ORIGINATOR_OVERRIDE, GEMINI_API_KEY, GOOGLE_API_KEY, ANTHROPIC_API_KEY` (subscriptions only; the claude relay credentials come from `~/.claude/settings.json` via `--setting-sources user`, so `ANTHROPIC_*` from the parent env must also be removed to avoid overriding).
- Never auto-retry. Classification (case-insensitive substring on stderr+stdout+error fields): not_logged_in: `401`, `not logged in`, `authentication required`, `access token could not be refreshed`, `please run /login`, `invalid api key`, `not logged into antigravity`; quota: `usage limit`, `quota`, `rate limit`, `429`, `resource_exhausted`, `out of credits`, `credit balance is too low`, `spend limit`; budget: claude subtype `error_max_budget_usd`; network: `stream disconnected`, `connection failed`, `unable to connect`, `request timed out`, `no response from api`, `overloaded`, `529`, `at capacity`; else error.

### A.4 Provider argument lists
Codex ask (no tools, context is inline in the prompt):
```
[CODEX, 'exec', '--ignore-user-config', '--skip-git-repo-check', '--sandbox', 'read-only',
 '--cd', workdir, '--color', 'never', '--json', '--output-last-message', workdir/'_answer.md',
 '--model', model,
 '-c', 'model_reasoning_effort="<effort or high>"', '-c', 'model_provider="openai"', '-c', 'forced_login_method="chatgpt"',
 '-c', 'approval_policy="never"', '-c', 'web_search="disabled"'  (or "live" when spec.web_search),
 '-c', 'project_doc_max_bytes=0', '-c', 'features.shell_tool=false', '-c', 'features.apps=false',
 '-c', 'features.plugins=false', '-c', 'features.remote_plugin=false', '-c', 'features.computer_use=false',
 '-c', 'features.browser_use=false', '-c', 'features.multi_agent=false', '-c', 'features.memories=false',
 '-c', 'features.code_mode=false', '-c', 'features.code_mode_host=false',
 '-c', 'features.skill_mcp_dependency_install=false', '-c', 'skills.bundled.enabled=false', '-']
```
Do NOT pass `--ephemeral` (the rollout file holds rate-limit data). Final text: `_answer.md` if present, else the last `item.completed` event whose `item.type == 'agent_message'` → `item.text`. Usage: `turn.completed.usage` (sum if several). Session id: `thread.started.thread_id`. Failure: `turn.failed.error.message` / `error.message` / non-zero exit.
Codex engineer: same but `'--sandbox', 'workspace-write'`, drop `features.shell_tool=false`, add `'-c', 'sandbox_workspace_write.network_access=false'`, `'-c', 'windows.sandbox="elevated"'`; `web_search="disabled"`.
agy ask:
```
[AGY, '--input-format', 'stream-json', '--output-format', 'stream-json', '--model', model,
 '--disable-slash-commands', '--print-timeout', f'{max(1, timeout_s // 60)}m', '--settings', workdir/'_agy_settings.json']
```
`_agy_settings.json` = `{"permissions": {"deny": ["write_file(*)", "command(*)", "mcp(*)", "read_url(*)", "execute_url(*)"]}, "useG1Credits": false}`. stdin: one line `json.dumps({"event": "user", "message": {"content": prompt}}, ensure_ascii=True) + "\n"`, then close stdin. Output NDJSON events: `init` (`init.model`), `step_update`, `result` (payload under key `result`: `response`, `status`, `error`, `usage{input_tokens,output_tokens,thinking_tokens,cache_read_tokens,total_tokens}`, `conversation_id`). ok iff `result.status == 'SUCCESS'` and response non-empty. Do not pass `-p` (dropped in stream mode).
claude ask (relay; always pass an explicit model — the relay default `ANTHROPIC_MODEL` is the most expensive model):
```
[CLAUDE, '-p', '--output-format', 'json', '--model', model, '--setting-sources', 'user', '--strict-mcp-config',
 '--disable-slash-commands', '--no-session-persistence', '--tools', '', '--permission-mode', 'dontAsk',
 '--max-budget-usd', f'{budget:.2f}', '--system-prompt', system_prompt or DEFAULT_SYSTEM]
```
stdin = prompt text. Output JSON: `result` (text), `is_error`, `subtype`, `total_cost_usd`, `usage{input_tokens,output_tokens,cache_read_input_tokens,cache_creation_input_tokens}`, `modelUsage` (keys = model names → model_reported), `session_id`. `DEFAULT_SYSTEM` = "你是严谨的科研审稿助手。只根据用户消息里给出的材料作答，不调用工具，不访问网络。用简体中文回答。"

### A.5 Codex rate limits (no model call)
`codex_rate_limits(thread_id: str = '') -> dict | None`: read `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` (if thread_id given, the file whose name ends with `<thread_id>.jsonl`; else the most recently modified files, newest first, up to 10). Take the last line with `type == 'event_msg'` and `payload.type == 'token_count'` and non-null `payload.rate_limits`. Return
`{'plan_type', 'observed_at' (line timestamp), 'weekly': {'used_percent', 'resets_at' (unix s)} | None, 'five_hour': {...} | None, 'credits': {'has_credits', 'unlimited'}, 'reached': rate_limit_reached_type}`. Identify windows by `window_minutes` (10080 weekly, 300 five-hour), not by primary/secondary. Never return the credits balance number.
Verified sample: `{"primary": {"used_percent": 87.0, "window_minutes": 10080, "resets_at": 1794609820}, "secondary": null, "plan_type": "pro", ...}`.

### A.6 Fake CLI for tests and demos
`tests/fake_cli.py <provider> <mode>`: reads stdin like the real CLI (agy: one NDJSON line), finds the template key from a line `【模板】<key>` in the prompt, looks up a reply in the JSON file named by env `RESEARCH_NOTEBOOK_FAKE_REPLIES` (`{"<key>": {"text": "...", "files": {"relative/path": "content"}, "error": "quota"}}`; missing key → a minimal valid reply `结论摘要：（模拟）\n\n```台账变更\n无\n```\n`), writes `files` into cwd (engineer mode), and prints output in the provider's real format (codex JSONL with thread.started/item.completed/turn.completed incl. usage, and writes `--output-last-message` file when given; agy stream-json init/result; claude JSON with total_cost_usd 0.0012). `error` simulates failure signatures (e.g. `quota` → message "You've hit your usage limit", exit 1). When env `RESEARCH_NOTEBOOK_FAKE_AGENTS=1`, `run_call` replaces the binary with `[sys.executable, tests/fake_cli.py, provider, mode]` but keeps the remaining arguments (so argument parsing is exercised; the fake must accept and ignore unknown flags, and honor `--output-last-message`/`--cd`).

### A.7 Settings: `00_模型与额度.json` at the research root (tracked, no secrets)
```json
{
  "schema": 1,
  "roles": {
    "author":   [{"provider": "codex", "model": "gpt-6.1-sol", "effort": "high"}, {"provider": "agy", "model": "claude-opus-5-5-high"}, {"provider": "agy", "model": "gemini-4-argon-high"}],
    "reviewer": [{"provider": "agy", "model": "gemini-4-argon-high"}, {"provider": "codex", "model": "gpt-6.1-sol", "effort": "high"}, {"provider": "agy", "model": "claude-opus-5-5-high"}],
    "engineer": [{"provider": "codex", "model": "gpt-6.1-sol", "effort": "high"}],
    "writer":   [{"provider": "agy", "model": "gemini-4-argon-high"}, {"provider": "codex", "model": "gpt-6.1-sol", "effort": "high"}],
    "drift":    [{"provider": "agy", "model": "claude-sonnet-5-5-low"}, {"provider": "agy", "model": "gemini-3.8-flash-medium"}, {"provider": "claude", "model": "claude-haiku-5-5"}],
    "referee":  [{"provider": "agy", "model": "claude-opus-5-5-high"}, {"provider": "claude", "model": "claude-opus-5-5"}, {"provider": "agy", "model": "gemini-4-argon-high"}, {"provider": "codex", "model": "gpt-6.1-sol", "effort": "high"}]
  },
  "limits": {"codex_stop_percent": 90, "claude_per_call_usd": 0.5, "claude_month_usd": 10.0, "agy_cooldown_hours": 5, "rounds_default": 2, "max_runs_per_round": 2, "call_timeout_minutes": 25},
  "git": {"auto_commit": true, "auto_push": true, "remote": "origin", "branch": "main"}
}
```
Role labels: author 作者, reviewer 审查, engineer 工程, writer 写作, drift 漂移检查, referee 审稿.
`class Settings(root)`: `load() -> (data, revision)` (missing file → defaults, revision ''; unknown keys kept; missing keys filled from defaults); `save(data, revision)` validates (providers in the 3 names, non-empty models, numbers in sane ranges, engineer only `codex`), checks revision (sha256 of file bytes, `''` when missing) and raises `ValueError` with Chinese text on conflict/invalid, writes atomically (temp + `os.replace`).
Price table constant for display (USD per 1M tokens, official list prices, relay may differ): fable-5-1 10/50, opus-5-5 4/20, sonnet-5-5 2/10, haiku-5-5 0.10/0.50.

### A.8 Usage ledger and quota
`class UsageLedger(root)`: appends one JSON line per call to `root/.runtime/usage.jsonl` (thread-safe): `time, provider, model, family, bucket, role, ref, round_id, ok, error_class, usage, cost_usd, duration_s`. `summary(now=None) -> dict` per bucket: `calls_today, calls_week, tokens_week, last_error_class, last_quota_error_at`; claude: `cost_month_usd`; codex: latest `rate_limits` seen in ledger lines or via `codex_rate_limits()`.
`quota_status(root, settings) -> dict` keyed by bucket: `{available: bool, level: 'ok'|'warn'|'blocked', text: '周额度已用 87%（11-14 06:43 重置）', detail: {...}}`:
- codex: blocked if not installed, or weekly/5h `used_percent >= codex_stop_percent`; warn at ≥ stop-10.
- agy:gemini / agy:other: blocked for `agy_cooldown_hours` after a quota error in that bucket; otherwise ok, text shows calls this week ("本周 12 次，额度未公开").
- claude: blocked if `cost_month_usd + claude_per_call_usd > claude_month_usd`; text "本月 $0.84 / $10".
`resolve_roles(settings, quota, roles: list[str]) -> dict[role -> {'provider','model','effort','family','bucket','label'}]`: resolve `author` first (first available candidate), then every other requested role takes its first available candidate whose family differs from the author's family when role in {reviewer, drift, referee}. Raise `RoleError(Chinese message)` if a role cannot be filled. `label` = display name like "GPT（gpt-6.1-sol）".
`provider_status(check_login=False)`: per provider installed/path/version; codex logged_in via `login status` (12 s timeout); agy/claude logged_in only when check_login=True (otherwise `None` = 未检查).

### A.9 Tests `tests/test_agents.py`
Argument lists for all 3 providers × modes (exact flags, no `--ephemeral`, explicit claude model and budget, agy settings file content), stdin encoding (agy NDJSON ascii, others UTF-8), Chinese+space workdir path, parsing of each fake output format, error classification per signature, cancel kills the process tree and returns `cancelled`, timeout returns `timeout`, rate-limit parsing from a synthetic rollout file (window identification by minutes), settings load/save/validation/revision conflict, quota rules, resolve_roles family constraints and fallbacks.

---

## B. `notebook/ledger.py` — anchor, ledger, reply blocks, rules, revisions, placeholders

### B.1 Anchor file `锚点.md`
```markdown
# 锚点

<!-- anchor {"schema":1,"level":"question","version":2,"status":"confirmed","confirmed_at":"2026-11-10T07:30:00+00:00","updated_at":"…"} -->

## 要回答的问题

…

## 判据

…

## 假设

| 编号 | 内容 | 依据 | 失效条件 | 状态 | 来源 |
|---|---|---|---|---|---|
| H1 | … | … | … | 已确认 | 你 |
| H2 | … | … | … | 待批 | GPT·第3轮 |

## 范围

…

## 不做什么

…

## 变更申请

| 编号 | 项目 | 申请改为 | 理由 | 来源 | 状态 | 处理说明 |
|---|---|---|---|---|---|---|
| Q1 | 判据 | … | … | GPT·第3轮 | 待处理 | |
```
Slots per level (key, label in file):
- `framework_layer`: question 要回答的大问题; criterion 论文主张与成功标准; assumptions 前提与假设; scope 约束（设备、数据、时间、学校要求）; nongoals 不做什么
- `chapter`: question 本章回答的问题; criterion 判据与评测协议（数据、指标、基线）; assumptions 依赖与假设; scope 范围; nongoals 不做什么
- `question`: question 要回答的问题; criterion 判据; assumptions 假设; scope 范围; nongoals 不做什么
Assumption status: 已确认 | 待批 | 已拒绝. Request status: 待处理 | 已批准 | 已拒绝. Table cells: `|` escaped as `\|`, newlines as `<br>`. Unknown extra `##` sections are preserved verbatim (re-rendered before 变更申请). The `变更申请` section is omitted when empty.
API: `parse_anchor(text, level) -> Anchor`, `render_anchor(anchor) -> str`, `empty_anchor(level) -> Anchor`, dataclasses `Anchor(level, version, status, confirmed_at, updated_at, slots: dict, assumptions: list[Assumption], requests: list[ChangeRequest], extra: str)`, `Assumption(id, text, basis, failure, status, source)`, `ChangeRequest(id, slot, proposal, reason, source, status, note)`. Helpers: `next_assumption_id(anchor)`, `next_request_id(anchor)`, `anchor_blocking(anchor) -> list[ChangeRequest]` (status 待处理), `pending_assumptions(anchor)`, `render_anchor_section(anchor) -> str` (Markdown body for the mini-paper section "1 问题与假设": question, criterion, assumption list with ids and conditions, scope, non-goals; pending assumptions marked "（待批）").

### B.2 Ledger file `台账.md`
```markdown
# 台账

<!-- ledger {"schema":1,"next":5,"meta":{"L03":{"family":"openai","support":["openai","google"],"sha":"…","superseded_by":""}}} -->

| 编号 | 类型 | 内容 | 状态 | 依据 | 证据 | 备注 | 来源 | 更新 |
|---|---|---|---|---|---|---|---|---|
| L01 | 推导步 | 由式(2)得式(3)… | 确定 | H1,H2 | R01 | | GPT·第1轮 | 第2轮 |
```
Types: 推导步 | 论断 | 待验证 | 想法 | 文献. States: 待定 | 确定 | 否定. `entry_sha(entry)` = sha256 of normalized `type + '\n' + text`. Ids `L01…`, never reused (`next` counter). `meta` keeps machine data (source family, supporting families, sha at last change, superseded_by); if the user hand-edits the table, missing meta is reconstructed from the 来源 label (GPT→openai, Gemini→google, Claude→anthropic, 你→human, 系统→system).
API: `parse_ledger(text) -> Ledger`, `render_ledger(ledger) -> str`, `empty_ledger()`, `Entry(id, type, text, state, basis, evidence, note, source, updated, family, support: list, superseded_by)`, `counts(ledger) -> {'待定','确定','否定'}`, `rejected_list(ledger)` (state 否定 and not superseded), `find(ledger, id)`.
Ledger log `台账日志.md` (append-only): `append_log(path_text: str, heading: str, results: list[OpResult]) -> str` returns new text with a section `## <heading>` and a table `| 原文 | 处理 | 说明 |`.

### B.3 Reply grammar (what models are told to output; see C.2 REPLY_FORMAT)
Fenced blocks, opened by a line of ≥3 backticks or tildes followed by the block name, closed by a fence line of the same char and length ≥ opener (CommonMark rule, so ````正文修订 can contain ``` code):
- `正文修订`: first lines `节：<number or heading>` and `方式：替换|追加`, then the Markdown body.
- `台账变更`: lines `+ [新1] 类型 | 内容 | 依据[ | 替代 Lxx]`, `~ Lxx | 新状态 | 理由 | 证据`, `? 锚点项 | 申请改为 | 理由`, `! 需要人决定的问题`, or the single word `无`.
- `验证计划`: lines `标题：…`, `针对：L03`, `方向：支持|否定`, `方法：仿真|理论|实测`, `输入与参数：…`, `退化自检：…`, `判据：…`, `预期：…` (values may continue on following lines until the next `键：`).
- `小问题清单`: repeated `### 标题` + lines `问题：…`, `判据：…`, `依赖：…`, `承重：是|否`, `验证方式：…`.
- `候选框架`: one Markdown outline per block in import format (`# 框架名` / `## 第N章 标题 | role` / `### 小问题`), plus free lines `一句话主张：…`, `风险：…`.
- `复推`: lines `Lxx | 一致|不一致|无法核对 | 说明` (referee only).
- `漂移检查` answers are plain lines (no fence): `仍在回答锚点里的问题吗：…`, `用了锚点以外的假设吗：…`, `判据或范围被改变了吗：…`, `判定：未漂移|轻微|漂移`.
- Trailing summary lines anywhere: `结论摘要：…`, `主要分歧：…`, `建议的下一步：…`.
`split_fields(line)`: split on `|` except inside `$…$`, `$$…$$`, backticks, or when escaped `\|`; strip cells.
`parse_reply(text) -> Reply(text, revisions: list[Revision(section, mode, body, raw)], ops: list[LedgerOp(kind, temp, fields, raw)], plans: list[dict], questions: list[dict], candidates: list[str], verifications: list[dict(entry, verdict, note)], drift: dict|None (keys question_ok, extra_assumptions, criterion_changed, verdict), summary: dict, errors: list[str])`. Malformed lines → `errors` (never raise).

### B.4 Rules `apply_ops(ledger, anchor, ops, *, source, family, round_label, step, evidence) -> list[OpResult]`
`source` like "GPT·第3轮", `family` e.g. 'openai', `step` in {draft, attack, reply, drift, referee}, `evidence = {'runs': {'R02': {'outcome': 'pass', 'direction': '支持'|'否定', 'targets': {'L03': '<sha>'}}}, 'verifications': {'V01': {'entry': 'L03', 'verdict': '一致', 'family': 'anthropic'}}}`. Mutates ledger and anchor. `OpResult(raw, status '已执行'|'已拒绝'|'记为意见', reason, entry_id, blocking: bool)`; also returns the temp-tag map via `ledger.last_temp_map`.
- `+`: always creates state 待定 (ignore any state text). Type 假设 → becomes a 待批 assumption in the anchor (id H<n>, source), result 已执行 "已登记为待批假设 H5". Unknown type / empty text → 已拒绝. Exact duplicate (whitespace-normalized) of an existing entry → 已拒绝 "与 L02 重复"; duplicate of a rejected entry → 已拒绝 "已在淘汰清单（L04），不要换个说法再提". `替代 Lxx`: if Lxx is 待定 → Lxx becomes 否定 with note "被 L07 替代" and `superseded_by`; if Lxx is 确定 → new entry note "拟替代 L03（L03 已确定，需要证据或你的决定）". Basis ids not found (H/L) → still create, note "依据 H7 不存在".
- `~ Lxx | 确定 | 理由 | 证据`: evidence containing 人工/你的决定/用户/本人 → 已拒绝 "只有你本人能在页面上作出决定". `Rnn` accepted iff run outcome 'pass', direction 支持, `targets[Lxx] == entry_sha(entry)`; `Vnn` accepted iff verification.entry == Lxx, verdict 一致, family != entry.family. Accepted → state 确定, evidence set, updated=round_label. Otherwise 记为意见 with the precise reason, and add `family` to `support` (distinct families); note becomes "已获 GPT、Gemini 同意，缺证据" when ≥2 families.
- `~ Lxx | 否定 | …`: accepted with `Rnn` (pass, direction 否定, sha match) or `Vnn` (verdict 不一致, family differs). Evidence starting with `反例` → entry becomes 待定 with note "有反例待核：…", plus a new entry type 待验证 "核查反例：…（针对 Lxx）"; status 已执行. Else 记为意见.
- `~ Lxx | 待定 | …` from 确定/否定: accepted only with `反例…` (as above) or a fresh R/V that contradicts the current state; else 记为意见. Same state as current → 记为意见 "状态未变".
- `?`: slot 假设/新假设 → 待批 assumption (non-blocking); slot 问题/要回答的问题/判据/范围/不做什么/约束 or `H\d+` → ChangeRequest status 待处理, `blocking=True`.
- `!` → result 已执行 with `blocking=True`, reason = the question text (caller stores it as a decision item).
- User actions: `user_set_state(ledger, id, state, note, round_label='你')` (evidence "你的决定"); `user_decide_assumption(anchor, id, approve, note)`; `user_decide_request(anchor, id, approve, note)` (approve → status 已批准, slot text replaced by proposal for slot requests, anchor.version += 1, anchor.status='draft' so the user re-confirms; H requests replace that assumption's text).
- System action: `apply_run(ledger, *, run_id, outcome, direction, targets: dict, round_label) -> list[OpResult]`: pass+支持 → targets 确定 (sha must match); pass+否定 → 否定; fail/partial/error → note "Rnn 未达判据/出错，待分析" (state unchanged).

### B.5 Document revisions
`apply_revisions(doc_text, revisions, *, protected: dict[str, str] = None) -> (new_text, results: list[dict(section, mode, status '已应用'|'已拒绝'|'待你确认', reason)])`.
Headings `##`–`####`. Match `节` by numeric prefix (`2.3` matches a heading whose text starts with `2.3` followed by space, `.`, `、`, `．` or end) else by normalized title. 替换: replace the section body (keep the heading line unless the body starts with a heading, in which case replace the heading too). 追加: append to the end of the section. Not found: 追加 with a numeric prefix → insert a new heading after the parent section (`2.4` goes at the end of section `2`, heading level = parent level + 1), else at document end as `##`; 替换 → 已拒绝 "找不到这一节". `protected` maps section number → reason; e.g. question docs pass `{'1': '第 1 节由锚点生成，请用 ? 行申请修改锚点', '5': '__stage__'}` where `__stage__` means status 待你确认 (not applied; returned for the UI).
`set_section(doc_text, number, heading, body) -> str` (create or replace; used to write section 1 from the anchor).
`skeleton(level, role) -> str`: question → `## 摘要\n\n## 1 问题与假设\n\n## 2 理论推导\n\n## 3 验证方法\n\n## 4 结果与分析\n\n## 5 结论与适用范围\n`; chapter by role: intro → 研究背景与意义 / 国内外研究现状 / 存在的问题 / 本文总体方案与技术路线 / 主要创新点 / 论文结构; theory → 引言 / 符号与假设 / 主要命题与推导 / 数值核对 / 本章小结（交给后续各章：符号表、假设表、误差模型）; research and framework → 引言（缺口与本章贡献）/ 方法 / 理论分析 / 实验设计（评测协议）/ 结果与分析 / 讨论与局限 / 本章小结; conclusion → 全文结论 / 主要创新点 / 局限 / 展望.

### B.6 Result placeholders
`resolve_placeholders(text, lookup) -> (new_text, missing: list[str])` replaces `{{R01.key}}`, `{{R01.a.b|.3e}}` with `[<value>](<href> "R01.key")` where `lookup(run_id, dotted_key) -> (value, href) | None`. Formatting: explicit spec via `format(value, spec)`; default float `f'{v:.6g}'`, int `str`, bool 是/否, str as-is, list/dict JSON (ensure_ascii False). Unresolved placeholders are left unchanged and listed in `missing`. `check_result_links(text, lookup) -> list[dict(key, shown, actual, ok)]` re-checks already-rendered links whose title matches `R\d+\.[\w.]+`.

### B.7 Tests `tests/test_ledger.py`
Round-trip of anchor and ledger files (escapes, Chinese, `$a|b$` in cells), parse_reply for every block incl. nested fences and malformed lines, every rule in B.4 (each accept/reject branch), revisions (numeric match, title match, insert new subsection, protected 1 and 5), placeholders (nested keys, format spec, missing).

---

## C. `notebook/round_prompts.py` — Chinese prompt templates

### C.1 API
`TEMPLATES: dict[str, dict]` with keys below; each `{'title', 'role', 'text'}`; `text` is a Python `str.format`-free template using `<<name>>` markers. `build(key, ctx: dict) -> str` replaces markers with `ctx` values (missing → `（无）`), always starts the prompt with the line `【模板】<key>` and appends `REPLY_FORMAT` unless the template sets `'reply_format': False`. `REPLY_FORMAT`, `DRIFT_FORMAT` are module constants. Adapt wording from `优化方案_20261109/03_新增提示词.md` (already reviewed by the user) and the ARIS mechanisms named in `工作流说明.md`; keep each template ≤ 40 lines; plain, specific instructions.
Context markers available: `<<project>>` (project card brief/background/symbols/rules), `<<anchor>>`, `<<ledger>>`, `<<rejected>>` (淘汰清单), `<<doc>>` (current document), `<<level>>`, `<<chapter>>` (chapter number/title/role and chapter anchor), `<<upstream>>` (confirmed chapter-2 propositions and other upstream conclusions), `<<focus>>` (user's note for this round), `<<round>>` (第N轮), `<<rejected_reason>>` (上次被打回的理由), `<<draft_blocks>>` (author's revision/ledger/plan blocks of this round, with prose removed), `<<attack>>`, `<<plan>>`, `<<code_files>>`, `<<results>>` (summary.json of runs, truncated), `<<frameworks>>`, `<<proposals>>`, `<<attacks>>`, `<<stage>>`, `<<ideas>>`, `<<staged_step>>`.

### C.2 Templates (key → role: purpose)
- `open_question` author: draft the anchor (5 slots, assumptions with basis/failure) as a `锚点草稿` block (```锚点草稿 with `## 要回答的问题` … sections) and the mini-paper skeleton/abstract via 正文修订. 
- `open_chapter` author: chapter anchor draft + chapter skeleton.
- `open_framework_layer` author: thesis-level anchor draft from the project card.
- `q_draft` author: advance one step of the mini paper (derive / revise / propose checks), list H and L used, list approximations with validity conditions, propose `验证计划` blocks for numerically checkable steps (must include a degenerate case with known analytic answer; two independent computation paths where possible), handle `<<rejected_reason>>` first.
- `q_attack` reviewer: attack only this round's blocks: counterexample with numbers, boundary/failure conditions, hidden assumptions not in the anchor, falsifiability of each 验证计划 (flag plans that would pass regardless); give each item 站得住 / 有条件成立 / 不成立 / 资料不足; may add `+ 待验证` and `~ … 反例: …` lines; do not propose alternatives.
- `q_reply` author: answer each attack point with exactly one of 接受（附正文修订）/ 拒绝（附可核对理由）/ 需要实验（附验证计划）.
- `q_verify` engineer (codex engineer mode): implement and run one approved plan in the current folder (see D.6 for files); first line comment of each code file: question id, plan id, equation numbers; selfcheck first; outputs to `输出/`; `summary.json` with `selfcheck_passed`, `criteria` [{name, predicted, observed, threshold, comparator ('<','<=','>','>=','abs<'), passed}], `independent_check`, `parameters`; `对照表.md` (式号 → 文件:行号 → 实现说明); `运行命令.txt`; use `py -3.12`; never claim the hypothesis is proven; `'reply_format': False`.
- `explain` author: learning mode follow-up — explain the staged step slower, no new claims, no ledger changes unless asked.
- `drift` drift: fresh check, `'reply_format': False`, answer the 4 DRIFT_FORMAT lines only; sees anchor, ledger (changed rows marked), author's 结论摘要 — nothing else.
- `referee` referee: independent review of the item from files given inline (anchor, doc, ledger, results, code); re-derive every 确定 and "已获同意" entry and output a `复推` block; check formula↔code mapping, hand-written numbers in section 4, score 1–10 with the 3 most serious weaknesses and the smallest fix for each; never sees discussions.
- `ch_outline` author: chapter outline (正文修订 on 草稿.md) + `小问题清单` (3–8 questions, dependency order, first one doable in days).
- `ch_attack` reviewer: attack the chapter outline/question list (coverage of the chapter question, dependencies on chapter 2, falsifiability, overlap with other chapters).
- `fw_propose` author or writer (two independent calls): 1–2 frameworks under the hard constraints (7 chapters: 1 绪论, 2 理论, 3–5 研究问题 each with an innovation, 6 随框架而定 (state its role), 7 总结与展望), output `候选框架` blocks.
- `fw_attack` reviewer-side (each attacks the other's proposals): innovation real? gaps quantifiable? does ch2 support 3–6? hardware feasibility? collision risk with DOI/arXiv or "未核实"; severity 致命/严重/可修补.
- `fw_synthesize` referee: merge into 2–3 clearly different candidates (`候选框架` blocks), keep big differences, list absorbed attacks and remaining risks, comparison table, list the trade-offs the user must decide; don't choose.
- `fw_refine` author: revise one framework record's fields (`正文修订` with 节：论文主线 | 章节安排 | 优点与风险).
- `innov_s1` author 量化缺口; `innov_s2` writer 发散 8–12 想法 (`+ 想法` lines; respect 淘汰清单); `innov_s3` author 可行性筛选 (keep ~3, `~ Lxx | 否定 | 理由 | ` lines are opinions only — the user picks); `innov_s4` author with web search: 查新, each related work as `+ 文献 | DOI或arXiv号 · 标题 · 与候选的区别 | 针对 Lxx` (ABANDON only when naming a specific paper that already did it; proximity is information, not a verdict); `innov_s5` author 杀测设计 (`验证计划` with 方向 否定, cheapest experiment most likely to kill the idea); `innov_s6` author 方法化 (method section via 正文修订 + `小问题清单`: correctness on synthetic data, baseline comparison, ablation per component, robustness, real data).
- `intro_story` writer, `intro_final` writer (only 确定 entries and 已核实 文献; each innovation cites chapter and ledger ids), `conclusion` writer (确定→结论, 否定→局限, 待定→展望, cite ids), `assemble` writer (assemble adopted mini-papers into the chapter draft; only intro/transition/notation unification; mark new paragraphs "（组装时新增）"; keep formulas, placeholders and links unchanged).
REPLY_FORMAT must state: number claims C1…; no hand-written result numbers (use `{{R01.key}}`); block syntax from B.3 (use four backticks for 正文修订); sections 1 and 5 of a mini paper are not editable (1 comes from the anchor; 5 is written by the user); new assumptions only via `? 假设 | …`; `+` lines never carry a state; evidence rules (R/V only; 反例 → 待核); never write 人工; end with the three summary lines.

### C.3 Tests `tests/test_round_prompts.py`
Every key builds with an empty ctx and with a full ctx; the `【模板】` line; REPLY_FORMAT present/absent per template; no unreplaced `<<…>>` markers; templates mention the constraints above (spot-check strings).

---

## D. `notebook/rounds.py` + `notebook/v2api.py` (written by the lead; listed here so other modules match)

### D.1 Recipes (`recipe` key → steps)
- question: `open` (open_question), `advance` (q_draft → q_attack → q_reply → [q_verify ×≤max_runs] → drift), `referee` (referee), `explain` (learning-mode only).
- chapter: `open` (open_chapter), `outline` (ch_outline → ch_attack → q_reply-style reply → drift), research chapters add `innov` (current stage S1–S6, each one call; S5 also runs q_verify on its plans), intro/conclusion add `write` (intro_story | intro_final | conclusion), any chapter `assemble`, `referee`.
- FW: `open` (open_framework_layer), `propose` (fw_propose author ∥ writer → fw_attack ×2 ∥ → fw_synthesize), `refine` (fw_refine on a chosen F record → q_attack → q_reply).
Default recipe: anchor missing or never confirmed → `open`; else question `advance`, chapter `outline` (research: `innov`), FW `propose`.

### D.2 Session = one start request (1–5 rounds). One active session at a time (global). Each round folder: `讨论/第03轮_20261110-153000/` with `任务.md`, per step `NN_<步骤>_<模型名>.md` (header: role, provider, model, time, usage) and `NN_<步骤>_<模型名>.提示词.md`, `简报.md`, `round.json`. Learning mode: pause after q_attack with staged revisions in `候选修订.json`; `continue` applies them and runs q_reply…; `ask` runs `explain`; `reject` saves `打回理由.md` and ends the round (next q_draft gets `<<rejected_reason>>`).
Stop after a round if: rounds done; blocking requests or `!` decisions; drift verdict 漂移; any call failed/cancelled/unknown; no progress (no ledger change, no revision applied, no run); all 待验证 entries resolved and ≥1 run in this item; quota/role resolution failure; user cancel.

### D.3 Commit policy: after each round `git commit -m "<ref> 第N轮：<recipe>（<roles>）"`; after each user write action in v2 or classic UI `"<ref> 手动：<action>"`; push after each commit when `git.auto_push`.

### D.4 HTTP (all GET paths under `/api/v2/`; POST via existing `/api/action` with `action` names prefixed `v2_`; same guards and headers as today)
GET `tree`:
```json
{"project": {"title": "我的论文研究", "is_example": false},
 "framework_layer": {"anchor_status": "none|draft|confirmed", "pending": 0},
 "frameworks": [{"id": "F01", "title": "…", "status": "active|candidate|parked",
   "chapters": [{"id": "C01", "no": 1, "title": "绪论", "role": "intro", "role_label": "绪论", "anchor_status": "none|draft|confirmed", "pending": 0,
     "questions": [{"id": "C01-Q01", "title": "…", "status": "discussing", "status_label": "讨论中", "anchor_status": "confirmed", "ledger": {"待定": 2, "确定": 1, "否定": 0}, "pending": 1}]}]}],
 "unplaced": [{"id": "C09", "title": "…", "role": "research", "questions": []}],
 "session": null,
 "pending_total": 3, "git": {"is_repo": true, "dirty": false, "ahead": 0, "last_push": {"ok": true, "at": "…"}}}
```
GET `item?ref=C04-Q01&fw=F01`:
```json
{"ref": "C04-Q01", "level": "question|chapter|framework_layer|framework", "title": "…", "fw": "F01",
 "breadcrumb": [{"ref": "FW", "title": "论文框架"}, {"ref": "C04", "title": "第2章 理论基础"}, {"ref": "C04-Q01", "title": "…"}],
 "chapter": {"id": "C04", "no": 2, "title": "理论基础", "role": "theory", "role_label": "理论"},
 "doc": {"path": "02_研究内容/…/推导笔记.md", "text": "…", "revision": "<sha>", "kind": "mini_paper|chapter_draft|framework_fields"},
 "anchor": {"path": "…/锚点.md", "revision": "<sha or ''>", "exists": true, "level": "question", "version": 2, "status": "draft|confirmed", "confirmed_at": "…",
   "slots": [{"key": "question", "label": "要回答的问题", "text": "…"}],
   "assumptions": [{"id": "H1", "text": "…", "basis": "…", "failure": "…", "status": "已确认|待批|已拒绝", "source": "你"}],
   "requests": [{"id": "Q1", "slot": "判据", "proposal": "…", "reason": "…", "source": "GPT·第3轮", "status": "待处理|已批准|已拒绝", "note": ""}]},
 "ledger": {"path": "…", "revision": "<sha>", "entries": [{"id": "L01", "type": "推导步", "text": "…", "state": "待定|确定|否定", "basis": "H1", "evidence": "", "note": "", "source": "GPT·第1轮", "updated": "第1轮"}], "counts": {"待定": 1, "确定": 0, "否定": 0}},
 "pending": [{"kind": "request|assumption|decision|learn|questions|candidates|ideas|refs", "id": "Q1", "text": "…", "round_id": "…"}],
 "rounds": [{"id": "r-20261110-153000", "no": 3, "title": "第3轮 · 推进", "status": "done|running|paused|failed|cancelled|stopped", "started_at": "…", "stop_reason": "…", "brief_path": "…/简报.md", "dir": "…"}],
 "latest_brief": "<markdown of the newest 简报.md or ''>",
 "session": null,
 "materials": {"codes": [{"name": "check.py", "path": "…", "purpose": "…"}], "runs": [{"id": "R01", "title": "…", "path": "…/运行记录.md", "outcome": "pass", "outcome_label": "满足判据", "files": [{"name": "summary.json", "path": "…"}]}], "verifications": [{"id": "V01", "path": "…", "entry": "L03", "verdict": "一致"}], "legacy_discussions": [{"id": "D01", "title": "…", "path": "…"}]},
 "upstream": [{"assumption": "H2", "ref": "C04-Q01", "message": "上游结论已变化，需复查"}],
 "children": [{"ref": "C04-Q01", "title": "…", "status_label": "…"}],
 "frameworks": [],
 "stage": {"current": "S3", "stages": [{"key": "S1", "label": "量化缺口", "done": true}]},
 "proposed_questions": [{"title": "…", "question": "…", "criterion": "…", "depends": "…", "load_bearing": true, "method": "仿真"}],
 "candidates": [{"index": 0, "title": "…", "markdown": "…"}],
 "recipes": [{"key": "advance", "label": "推进一轮"}], "default_recipe": "advance",
 "status": "discussing", "status_label": "讨论中", "judgment": null, "can_adopt": true}
```
For level `framework_layer` (`ref=FW`): `doc` is null and `frameworks` lists `{id, title, status, mainline, chapters_text, risks, chapters: [{id, no, title, role}]}`. For level `framework` (`ref=F01`): `doc.kind = framework_fields` with `doc.fields = {mainline, chapters, risks}` and `doc.revision`.
GET `session?id=…`:
```json
{"id": "s-…", "ref": "C04-Q01", "recipe": "advance", "mode": "auto|learn", "status": "queued|running|paused|done|stopped|failed|cancelled",
 "rounds_planned": 2, "round_index": 1, "round": {"id": "r-…", "no": 3, "dir": "…"},
 "steps": [{"key": "draft", "label": "作者推进", "role": "author", "model_label": "GPT（gpt-6.1-sol）", "status": "pending|running|done|skipped|failed", "duration_s": 41.2, "output_path": "…", "error": ""}],
 "learn": {"staged": [{"section": "2.3 …", "mode": "替换", "old": "…", "new": "…"}], "attack": "…", "explain": ""},
 "stop_reason": "", "message": "正在请审查（Gemini）找反例…"}
```
GET `settings` → `{"settings": {...}, "revision": "…", "providers": {...provider_status}, "quota": {...quota_status}, "prices": {...}}`; GET `usage` → `{"quota": {...}, "summary": {...}}`; GET `history?path=…` → `{"commits": [{"sha", "short", "date", "message"}]}`; GET `version?path=…&rev=…` → `{"text": "…"}`; GET `git` → status.
POST actions: `v2_start` {ref, fw, recipe, mode, rounds, focus, request_id} → session; `v2_cancel` {session_id}; `v2_learn` {session_id, decision: continue|ask|reject, note}; `v2_save_doc` {ref, text, revision} (framework: {ref, fields, revision}); `v2_save_anchor` {ref, revision, slots: {key: text}, assumptions: [{id?, text, basis, failure}]}; `v2_confirm_anchor` {ref, revision}; `v2_decide` {ref, kind: request|assumption|decision, id, approve: bool, note}; `v2_set_entry` {ref, id, state, note}; `v2_adopt` {ref, decision: supported|refuted|inconclusive, text, scope}; `v2_create_framework` {title}; `v2_create_questions` {ref (chapter), items: [{title, question, criterion}]}; `v2_create_from_candidate` {round_dir, index, title}; `v2_set_chapter` {ref, role, title}; `v2_pick_ideas` {ref, keep: [ids]}; `v2_verify_refs` {ref, ids: [ids]}; `v2_next_stage` {ref}; `v2_settings_save` {settings, revision}; `v2_test_provider` {provider, model}; `v2_git_push` {}; `v2_restore` {path, rev, revision}. All return the updated `item` (or `tree`/`session`/`settings` where that is the natural object). Errors: HTTP 400/403/404/409 JSON `{error, type}` as today.

### D.5 Frontend contract (static/v2)
`/` serves `static/v2/index.html`; `/classic` serves the old `static/index.html`. Hash routes `#/FW`, `#/F01`, `#/C04?fw=F01`, `#/C04-Q01?fw=F01`; default route = last visited (localStorage) else `#/FW`.

### D.6 Engineer workspace
`work_root()/<round-id>_<plan-id>/` containing `代码/` (copies of the question's code files), `计划.md`, `正文摘录.md` (sections 2–3 of the doc), empty `输出/`. After the call the lead's code imports new/changed `*.py|*.m|*.jl|*.r` files from the workspace root or `代码/` via `add_code`, every file under `输出/` via `attach_file(run_id)` (skip > 32 MB), `对照表.md` → run mapping, `运行命令.txt` → command, decides the outcome from `summary.json` (missing → error; `selfcheck_passed` false → fail; any recomputed criterion failing → fail; reported `passed` contradicting `observed` vs `threshold` → error "summary 中的 passed 与数值不符"), then `update_run`.

---

## E. `notebook/vcs.py` — git

```python
class Repo:
    def __init__(self, root: Path, settings_getter=None)  # settings_getter() -> settings['git']
    def available(self) -> bool
    def is_repo(self) -> bool
    def ensure(self) -> dict          # git init -b main if needed; write .gitattributes; merge required .gitignore lines; initial commit "初始化：科研文件管理系统版本库"; returns status
    def commit(self, message: str) -> dict | None   # stage all (respecting .gitignore), unstage + list files > 50 MB, commit if anything staged; returns {'sha','short','files': n,'skipped_large': [...]} or None when nothing to commit
    def commit_async(self, message: str) -> None    # coalesce: messages within 3 s are merged into one commit (joined by '；'), runs on a single worker thread; then push_async() when auto_push
    def push(self, timeout: int = 120) -> dict      # 'git push -u <remote> <branch>'; never force; result stored in .runtime/git_push.json {'ok','at','message'}
    def push_async(self) -> None                    # single worker; if a push is running, schedule one more afterwards
    def status(self) -> dict                        # {'available','is_repo','branch','dirty','ahead','remote','remote_url','last_commit': {...}|None,'last_push': {...}|None}
    def history(self, path: str, limit: int = 50) -> list[dict]   # git log --follow; [{'sha','short','date','message'}]
    def show(self, rev: str, path: str) -> bytes    # validate rev (hex 7–40) and path (relative POSIX, no '..'), 'git show rev:path'
    def set_remote(self, url: str) -> dict
    def close(self, timeout=10)                     # flush pending commit/push threads (server shutdown)
```
Every git invocation: `['git', '-C', root, '-c', 'core.autocrlf=false', '-c', 'core.safecrlf=false', '-c', 'core.quotepath=false', ...]`, `encoding='utf-8'`, `CREATE_NO_WINDOW`, timeouts, never `shell=True`, never print credentials. `.gitattributes` content: `* -text` (critical: revisions are SHA-256 of raw bytes; CRLF conversion would flip judged questions to 需复查). Required `.gitignore` lines (keep existing ones): `.runtime/`, `.history/`, `__pycache__/`, `*.pyc`, `.tmp-*`, `*.mat`, `*.bmp`, `*.tif`, `*.tiff`, `*.pt`, `*.pth`, `*.zip`, `*.tgz`, `验收截图/*.tmp`. Thread-safe (one lock). Tests `tests/test_vcs.py` with temp repos (set `user.name/email` locally in the temp repo; use a local bare repo as remote to test push), CRLF preservation (`\r\n` bytes survive commit/checkout), large-file skipping (use a sparse/small threshold override in tests), coalescing, history/show validation.

---

## F. Frontend `static/v2/` (index.html, app.js, style.css)

Plain ES2020, no frameworks, no external network. Load `/static/vendor/katex/katex.min.css`, `/static/vendor/katex/katex.min.js`, `/static/vendor/katex/contrib/auto-render.min.js` (delimiters `$$…$$`, `$…$`, `\(…\)`, `\[…\]`). POST with headers `X-Research-Notebook: 1` and `Content-Type: application/json` to `/api/action` with `{action, ...}`. CSP forbids inline scripts and inline event handlers: use `addEventListener` and event delegation (`data-action` attributes).
Layout (desktop ≥ 1100 px; three columns; on narrow screens the right panel moves below the document and the tree becomes a toggle):
- Header: project title · framework picker (frameworks of the tree; selecting sets `fw`) · quota chips (codex / Gemini / Claude: text from `quota[*].text`, colour by level) · git chip (已同步 / 未推送 n / 推送失败) · buttons 设置, 经典版 (link `/classic`).
- Left tree: 论文框架 (FW) → for the selected framework: 第N章 title (role) → small questions (status dot + title); badges for pending counts; "+ 新建框架" at FW level; "+ 新建小问题" under a chapter (prompt for title). Unplaced chapters listed under "未放入框架".
- Main: breadcrumb; title; **anchor card** (status badge 草稿/已确认 v2; slots as short text; assumptions table; pending assumptions/requests highlighted; buttons 编辑锚点 (inline form: 5 textareas + assumption rows add/remove), 确认锚点); **document** (rendered Markdown + KaTeX; click 编辑 → textarea editor with 保存 (Ctrl+S) / 取消 / 历史版本; 409 conflict keeps the text and shows 提示); for FW: framework cards (主推 badge, mainline, chapter list, 设为主推 / 打开); for chapters: question list and, if present, `proposed_questions` checklist with 建立选中的小问题; research chapters: stage bar S1–S6 with 下一阶段; **ledger** (counts chips 待定/确定/否定; table with filters; per-entry menu 设为确定/设为否定/设为待定 with optional note → `v2_set_entry`; 否定 rows collapsed under 淘汰清单); **materials** (codes, runs with outcome badges and files, verifications, links open the file viewer).
- Right panel: **下一轮** box (recipe select defaulting to `default_recipe`; mode 自动/学习; rounds 1–5 (default from settings); 本轮重点 textarea; 开始 button) or the **running session** (steps with status icons, model labels, elapsed time, 取消; learning-mode pause shows staged revisions as old/new side by side plus the attack text, buttons 继续 / 追问 / 打回 (reason textarea)); **待你决定** list (`pending`; request/assumption/decision → 批准/拒绝 + note; questions/candidates/ideas/refs → jump to their section); **最近简报** (render `latest_brief`); **历史轮次** list (open 简报 or round files).
- Poll `GET /api/v2/session?id=` every 2 s while a session is queued/running; refresh `tree` and `item` when it ends. Refresh `tree` after every action.
- File viewer modal: `GET /api/file?path=` — Markdown rendered; code shown with line numbers and highlight for `#L10-L20` anchors (scroll into view); images inline; CSV as a table (first 200 rows); other → download link (`&download=1`).
- Markdown renderer (own, safe): headings, paragraphs, emphasis, inline code, fenced code, ordered/unordered/nested lists, tables (with `\|` escapes), blockquotes, horizontal rules, links (relative links resolve against the document's directory and open in the viewer; `#L` anchors kept; external `http(s)` links open in a new tab with `rel=noopener`), images (only relative paths → `/api/file?path=`), and math left intact for KaTeX. Escape all HTML. Links whose title matches `R\d+\.[\w.]+` render as result links (monospace, small "R01" tag).
- Settings modal (`GET /api/v2/settings`): role table (role label; primary candidate select from a list of known provider:model pairs: codex:gpt-6.1-sol, agy:gemini-4-argon-high, agy:gemini-4-argon-medium, agy:gemini-3.8-flash-medium, agy:claude-opus-5-5-high, agy:claude-sonnet-5-5-medium, agy:claude-sonnet-5-5-low, claude:claude-opus-5-5, claude:claude-sonnet-5-5, claude:claude-haiku-5-5, claude:claude-fable-5-1; the remaining candidates shown as 备选 text), limits (numbers), git (auto_commit / auto_push toggles, remote URL read-only), price table, provider status with 测试连接 buttons (confirm dialog: "会消耗少量额度，继续？"), save → `v2_settings_save`.
- History modal: commits for the current document path; view old version (rendered); 恢复此版本 → `v2_restore`.
- Adopt dialog (questions): decision select, text (prefilled from section 5 of the doc if any), scope (prefilled from anchor assumptions + scope) → `v2_adopt`.
- Visual style: calm document reading UI, system Chinese font stack, max reading width ~760 px, light/dark via `prefers-color-scheme`, clear focus styles, no heavy shadows. Every interactive element is a `<button>` or `<a>`; status labels are not buttons.
- Provide `static/v2/mock.json` sample data and support `?mock=1` (loads tree/item/session from the mock, POSTs are no-ops with a toast) so the UI can be checked without the backend.
