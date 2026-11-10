"""Git versioning of one research root: local commits and an optional push.

The root is versioned only when it is the top level of its own repository. A
root nested inside another work tree (the example research inside the app
folder) is reported and never initialised: an embedded repository would break
the outer one.

Rules kept here:
- every git call is an argument list (never a shell) that starts with
  ``-C root -c core.autocrlf=false -c core.safecrlf=false -c core.quotepath=false``,
  runs without a console window, has a timeout and reads/writes UTF-8; commit
  messages and path lists go through stdin;
- ``.gitattributes`` keeps ``* -text`` as its last rule, so raw bytes (and the
  SHA-256 revisions computed from them) survive commit and checkout;
- files larger than ``LARGE_FILE_BYTES`` never enter the object store;
- never force-push; URLs are shown without user info and credentials are
  never accepted into the remote URL.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import stat
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

LOG = logging.getLogger("research-notebook.vcs")
LOG.addHandler(logging.NullHandler())  # the server's logging config decides where messages go

LARGE_FILE_BYTES = 50 * 1024 * 1024
COALESCE_SECONDS = 3.0
RECENT_SECONDS = 5.0                       # files changed this recently are probably still being written
RETRY_DELAYS = (5.0, 30.0, 120.0)          # a failed auto-commit is retried this many times
COMMIT_BUDGET_BYTES = 200 * 1024 * 1024    # new data per commit; the rest waits for the next commit
PUSH_IDLE_SECONDS = 120                    # a push is stopped only after this long without progress
PUSH_MAX_SECONDS = 2 * 3600
INIT_MESSAGE = "初始化：科研文件管理系统版本库"
ATTRIBUTES_RULE = "* -text"
REQUIRED_IGNORES = (".runtime/", ".history/", "__pycache__/", "*.pyc", ".tmp-*", "*.mat", "*.bmp", "*.tif",
                    "*.tiff", "*.pt", "*.pth", "*.zip", "*.tgz", "验收截图/*.tmp")
DEFAULT_SETTINGS = {"auto_commit": True, "auto_push": True, "remote": "origin", "branch": "main"}
GIT_OPTIONS = ("core.autocrlf=false", "core.safecrlf=false", "core.quotepath=false",
               # Deep Chinese paths can pass 260 characters; keep messages and log output UTF-8
               # even when a user's global config asks for another encoding.
               "core.longpaths=true", "i18n.commitEncoding=utf-8", "i18n.logOutputEncoding=utf-8")
TIMEOUT = 60
LONG_TIMEOUT = 600
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
LOG_FORMAT = "%x1e%H%x1f%h%x1f%aI%x1f%B%x1f"  # used with -z; --name-only output follows the last \x1f
REV_RE = re.compile(r"[0-9a-fA-F]{7,40}")
REMOTE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
BRANCH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,99}")
USERINFO_RE = re.compile(r"(?i)\b((?:https?|ftps?)://)[^/\s]*@")
HELPER_URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*::")
GIT_LOCK_RE = re.compile(rb"\.git[/\\][^'\"\r\n]*\.lock")  # index.lock, HEAD.lock, refs/heads/main.lock
BUSY_STATES = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply")
# Variables that would point git at another repository than the research root.
LOCATION_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_NAMESPACE", "GIT_PREFIX")
NO_GIT = "未找到 git，版本管理不可用。"
NO_REPO = "尚未建立版本库。"
PUSH_FAILURES = (
    ("rejected", ("[rejected]", "non-fast-forward", "fetch first", "updates were rejected"),
     "远程仓库有本机没有的提交，推送被拒绝。请先在 git 中拉取并合并；系统不会强制推送。"),
    ("auth", ("authentication failed", "could not read username", "could not read password",
              "terminal prompts disabled", "permission denied", "access denied", "invalid username or password",
              "returned error: 403", "returned error: 401"),
     "推送需要登录或没有写入权限，请检查 GitHub 凭据。"),
    ("not_found", ("repository not found", "does not appear to be a git repository", "not found"),
     "远程仓库不存在或地址有误。"),
    ("network", ("could not resolve host", "unable to access", "failed to connect", "timed out",
                 "connection was reset", "connection refused", "network is unreachable", "could not connect"),
     "无法连接远程仓库，请检查网络后再推送。"),
)


class GitError(RuntimeError):
    """A git operation failed. The message is short Chinese text for the page."""


class GitTimeout(GitError):
    """A git command ran past its timeout and was stopped."""


@dataclass
class GitResult:
    returncode: int
    stdout: str | bytes
    stderr: str


@dataclass
class _Entry:
    kind: str        # '1' changed, 'u' unmerged, '?' untracked
    x: str           # index status
    y: str           # work-tree status
    index_oid: str
    path: str


@dataclass
class _Scan:
    oid: str = ""
    head: str = ""
    ahead: int | None = None
    entries: list = field(default_factory=list)

    @property
    def detached(self):
        return self.head == "(detached)"


def find_git() -> str | None:
    # On Windows only a real git.exe: a .cmd/.bat shim would run through cmd.exe,
    # which expands '%' in arguments such as --format=%H.
    found = shutil.which("git.exe" if os.name == "nt" else "git")
    if found:
        return found
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramW6432"),
                 os.path.join(os.environ["LOCALAPPDATA"], "Programs") if os.environ.get("LOCALAPPDATA") else None):
        if base and Path(base, "Git", "cmd", "git.exe").is_file():
            return str(Path(base, "Git", "cmd", "git.exe"))
    return None


def redact(text: str) -> str:
    """Drop user info (user names, passwords, tokens) from http(s)/ftp URLs."""
    return USERINFO_RE.sub(r"\1", text or "")


def _git_env():
    env = os.environ.copy()
    for key in LOCATION_ENV:
        env.pop(key, None)
    env["GIT_TERMINAL_PROMPT"] = "0"   # no hidden console prompt that could hang a worker
    env["GIT_OPTIONAL_LOCKS"] = "0"    # status must not take index.lock from the user's own git
    return env


def _kill_tree(proc):
    if os.name == "nt":
        # cmd\git.exe is a launcher; kill the whole tree so no child keeps index.lock.
        taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "taskkill.exe")
        try:
            subprocess.run([str(taskkill), "/PID", str(proc.pid), "/T", "/F"], stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            pass
    try:
        proc.kill()
    except OSError:
        pass


def _execute(cmd, data, timeout, idle=None):
    """Run git. With idle set, timeout is the overall cap and the process is stopped only
    after `idle` seconds without any output (used for push --progress on slow uploads)."""
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE if data is not None else subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=_git_env(), creationflags=NO_WINDOW)
    except OSError as exc:
        raise GitError(f"无法启动 git：{exc.strerror or exc}") from exc
    if idle is None:
        try:
            out, err = proc.communicate(data, timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            try:
                proc.communicate(timeout=10)
            except (subprocess.TimeoutExpired, OSError, ValueError):
                pass
            raise GitTimeout(f"git 操作超过 {timeout} 秒未完成，已停止。") from None
        return proc.returncode, out or b"", err or b""
    chunks = {"out": [], "err": []}
    last = [time.monotonic()]

    def pump(stream, key):
        for block in iter(lambda: stream.read1(4096) if hasattr(stream, "read1") else stream.read(4096), b""):
            chunks[key].append(block)
            last[0] = time.monotonic()

    readers = [threading.Thread(target=pump, args=(proc.stdout, "out"), daemon=True),
               threading.Thread(target=pump, args=(proc.stderr, "err"), daemon=True)]
    for reader in readers:
        reader.start()
    if data is not None:
        try:
            proc.stdin.write(data)
        except OSError:
            pass
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass
    started = time.monotonic()
    while proc.poll() is None:
        now = time.monotonic()
        if now - last[0] > idle or now - started > timeout:
            _kill_tree(proc)
            for reader in readers:
                reader.join(timeout=5)
            reason = f"{int(idle)} 秒没有进展" if now - last[0] > idle else f"超过 {int(timeout)} 秒"
            raise GitTimeout(f"git 操作{reason}，已停止。")
        time.sleep(0.5)
    for reader in readers:
        reader.join(timeout=10)
    return proc.returncode, b"".join(chunks["out"]), b"".join(chunks["err"])


def _tail(text, limit=300):
    lines = [line.strip() for line in redact(text).splitlines() if line.strip() and not line.startswith("hint:")]
    tail = " / ".join(lines[-3:])
    return tail if len(tail) <= limit else "…" + tail[-limit:]


def _failure(command, stderr):
    low = stderr.lower()
    if "please tell me who you are" in low or "empty ident name" in low or "auto-detect email" in low \
            or "no name was given" in low or "no email was given" in low:
        return ('git 还没有设置用户名和邮箱，无法提交。请运行 git config --global user.name "你的名字" '
                '和 git config --global user.email "你的邮箱"。')
    if "index.lock" in low:
        return "git 版本库被占用（.git/index.lock）。如果没有其他 git 程序在运行，请删除该文件后重试。"
    if "dubious ownership" in low:
        return "git 拒绝读取这个目录（safe.directory 归属检查未通过），请先在 git 中把它加入 safe.directory。"
    if command == "commit" and "hook" in low:
        return f"提交被 git 钩子拒绝：{_tail(stderr)}"
    return f"git {command} 失败：{_tail(stderr)}"


def _push_failure(stderr):
    low = stderr.lower()
    for code, needles, message in PUSH_FAILURES:
        if any(needle in low for needle in needles):
            return code, message
    return "error", f"推送失败：{_tail(stderr)}"


def _now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _same_path(a, b):
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _nul(items):
    return b"".join(item.encode("utf-8", "surrogateescape") + b"\0" for item in items)


def _display(path):
    return path.encode("utf-8", "surrogateescape").decode("utf-8", "replace")


def _count(text):
    text = text.strip()
    return int(text) if text.isdigit() else 0


def _valid_remote(name):
    return isinstance(name, str) and bool(REMOTE_RE.fullmatch(name)) and ".." not in name and not name.endswith(".lock")


def _valid_branch(name):
    return (isinstance(name, str) and bool(BRANCH_RE.fullmatch(name)) and not any(bad in name for bad in ("..", "//", "@{"))
            and not name.endswith(("/", ".", ".lock")))


def clean_path(path) -> str:
    """A repository path: relative, POSIX separators, no '.'/'..' or empty segments."""
    if not isinstance(path, str) or not path:
        raise ValueError("请指定文件路径。")
    if len(path) > 1024 or "\\" in path or ":" in path or path.startswith("/") \
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in path) \
            or any(part in ("", ".", "..") for part in path.split("/")):
        raise ValueError("文件路径无效：只接受研究目录内以 / 分隔的相对路径。")
    return path


def clean_remote_url(url) -> str:
    if not isinstance(url, str):
        raise ValueError("远程仓库地址必须是文本。")
    url = url.strip()
    if not url or len(url) > 2048:
        raise ValueError("请填写远程仓库地址。")
    if url.startswith("-") or any(ord(ch) < 32 or ord(ch) == 127 for ch in url):
        raise ValueError("远程仓库地址无效。")
    if HELPER_URL_RE.match(url):
        raise ValueError("不支持这种远程仓库地址。")
    if USERINFO_RE.match(url):
        raise ValueError("远程地址里不要写用户名、密码或令牌；请用 Git 凭据管理器登录 GitHub。")
    return url


def _read_bytes(path):
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(".tmp-" + uuid.uuid4().hex)  # matches the ignored '.tmp-*'
    try:
        with open(temp, "xb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
    except BaseException:
        try:
            temp.unlink()
        except OSError:
            pass
        raise


def _join_messages(messages, limit=20):
    if len(messages) > limit:
        return "；".join(messages[:limit]) + f"；等共 {len(messages)} 项"
    return "；".join(messages)


class _WorkTreeLocks:
    """Take the optional write lock first, then the repository lock.

    The write lock can be released early (once git no longer reads the work
    tree) while the repository lock stays held. Always in this order, so a
    caller that holds the write lock may call any Repo method.
    """

    def __init__(self, outer, inner):
        self.outer, self.inner, self.outer_held = outer, inner, False

    def __enter__(self):
        if self.outer is not None:
            self.outer.acquire()
            self.outer_held = True
        try:
            self.inner.acquire()
        except BaseException:
            self.release_outer()
            raise
        return self

    def release_outer(self):
        if self.outer_held:
            self.outer_held = False
            self.outer.release()

    def __exit__(self, *exc):
        self.inner.release()
        self.release_outer()
        return False


class Repo:
    """Git operations for one research root. Thread-safe.

    ``_lock`` serialises every local git command and file written here. A push
    runs its network part outside it (one push at a time) so status() and
    commits do not wait for a slow network. Queue bookkeeping for the async
    workers uses ``_state`` and never waits for git.

    ``write_lock`` (optional, e.g. ``Store.lock``) is held while git reads the
    work tree (status scan, staging). On Windows a file that git is hashing
    cannot be replaced, so a store save at that moment would fail.
    """

    def __init__(self, root: Path, settings_getter=None, *, large_file_bytes: int | None = None,
                 coalesce_seconds: float = COALESCE_SECONDS, write_lock=None, recent_seconds: float = 0.0):
        self.root = Path(root).absolute().resolve()
        self.settings_getter = settings_getter
        self.large_file_bytes = LARGE_FILE_BYTES if large_file_bytes is None else int(large_file_bytes)
        self.coalesce_seconds = float(coalesce_seconds)
        self.recent_seconds = float(recent_seconds)
        self.write_lock = write_lock
        self._git_path = None            # None: not probed yet; '': git unavailable
        self._probe_lock = threading.Lock()
        self._lock = threading.RLock()
        self._push_mutex = threading.Lock()
        self._state = threading.Condition()
        self._pending = []
        self._deadline = 0.0
        self._flush = False
        self._closing = False
        self._retries = 0
        self._committing = False
        self._commit_thread = None
        self._push_thread = None
        self._push_again = False
        self._last_error = ""
        self._skipped_large = []
        self._embedded_list = []
        self._budget_skipped = []
        self._untracked_large = []

    # infrastructure ------------------------------------------------------------
    def _config(self):
        # Never call the settings getter while holding _lock or _state: it may take other locks.
        data = {}
        if self.settings_getter is not None:
            try:
                value = self.settings_getter()
            except Exception:  # noqa: BLE001 - settings are optional; defaults apply
                LOG.warning("读取 git 设置失败，使用默认设置", exc_info=True)
                value = None
            if isinstance(value, dict):
                data = value
        config = dict(DEFAULT_SETTINGS)
        for key in ("auto_commit", "auto_push"):
            if isinstance(data.get(key), bool):
                config[key] = data[key]
        for key in ("remote", "branch"):
            if isinstance(data.get(key), str) and data[key].strip():
                config[key] = data[key].strip()
        return config

    def available(self) -> bool:
        if self._git_path is None:
            with self._probe_lock:
                if self._git_path is None:
                    path, found = find_git(), ""
                    if path:
                        try:
                            code, out, _ = _execute([path, "--version"], None, 15)
                            if code == 0 and out.startswith(b"git version"):
                                found = path
                        except GitError:
                            pass
                    self._git_path = found
        return bool(self._git_path)

    def _git(self, args, *, data=None, timeout=TIMEOUT, binary=False, literal=False, check=True, retry_lock=False, idle=None):
        if not self.available():
            raise GitError(NO_GIT)
        cmd = [self._git_path, "-C", str(self.root)]
        for option in GIT_OPTIONS:
            cmd += ["-c", option]
        if literal:
            cmd.append("--literal-pathspecs")
        cmd += list(args)
        if isinstance(data, str):
            data = data.encode("utf-8")
        for attempt in range(4):
            started = time.time()
            try:
                code, out, err = _execute(cmd, data, timeout, idle=idle)
            except GitTimeout:
                self._remove_own_lock(started)
                raise
            # An editor or the user's own git may hold index.lock for a moment.
            if code == 0 or not retry_lock or not GIT_LOCK_RE.search(err) or attempt == 3:
                break
            time.sleep(0.25 * (attempt + 1))
        error = err.decode("utf-8", "replace")
        result = GitResult(code, out if binary else out.decode("utf-8", "replace"), error)
        if check and code != 0:
            raise GitError(_failure(args[0], error))
        return result

    def _remove_own_lock(self, started):
        """A git we killed may leave index.lock behind; remove it only if it was created after we started."""
        lock = self.root / ".git" / "index.lock"
        try:
            if lock.is_file() and lock.stat().st_mtime >= started - 1:
                lock.unlink()
                LOG.warning("已删除被中止的 git 留下的 index.lock")
        except OSError:
            pass

    def _locate(self):
        """(git_dir, kind, message); kind is ok | none | nested | broken."""
        result = self._git(["rev-parse", "--show-toplevel", "--absolute-git-dir"], check=False)
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        if result.returncode != 0 or len(lines) < 2:
            if "dubious ownership" in result.stderr.lower():
                return None, "broken", _failure("rev-parse", result.stderr)
            if (self.root / ".git").exists():
                return None, "broken", f"版本库无法读取：{_tail(result.stderr)}"
            return None, "none", NO_REPO
        top = Path(lines[0])
        if not _same_path(top, self.root):
            return None, "nested", f"这个目录位于另一个 git 仓库（{top}）之内，不单独建立版本库。"
        return Path(lines[1]), "ok", ""

    def _require_repo(self, error=GitError):
        if not self.available():
            raise error(NO_GIT)
        git_dir, _, message = self._locate()
        if git_dir is None:
            raise error(message)
        return git_dir

    def _has_head(self):
        return self._git(["rev-parse", "--verify", "-q", "HEAD"], check=False).returncode == 0

    def _scan(self):
        raw = self._git(["status", "--porcelain=v2", "--branch", "-z", "--untracked-files=all", "--no-renames"],
                        binary=True).stdout
        scan, records, index = _Scan(), raw.split(b"\0"), 0
        while index < len(records):
            record = records[index].decode("utf-8", "surrogateescape")
            index += 1
            if record.startswith("# "):
                key, _, value = record[2:].partition(" ")
                if key == "branch.oid":
                    scan.oid = "" if value == "(initial)" else value
                elif key == "branch.head":
                    scan.head = value
                elif key == "branch.ab":
                    try:
                        scan.ahead = int(value.split()[0])
                    except (ValueError, IndexError):
                        pass
            elif record.startswith("1 "):
                parts = record.split(" ", 8)
                if len(parts) == 9:
                    scan.entries.append(_Entry("1", parts[1][0], parts[1][1], parts[7], parts[8]))
            elif record.startswith("2 "):  # not produced with --no-renames; parsed defensively
                parts = record.split(" ", 9)
                if len(parts) == 10:
                    scan.entries.append(_Entry("1", parts[1][0], parts[1][1], parts[7], parts[9]))
                index += 1  # the original path is its own record
            elif record.startswith("u "):
                parts = record.split(" ", 10)
                if len(parts) == 11:
                    scan.entries.append(_Entry("u", parts[1][0], parts[1][1], "", parts[10]))
            elif record.startswith("? "):
                scan.entries.append(_Entry("?", "?", "?", "", record[2:]))
        return scan

    def _size(self, relative):
        try:
            info = os.lstat(self.root / relative)
        except (OSError, ValueError):
            return None
        return info.st_size if stat.S_ISREG(info.st_mode) else None

    def _large_in_worktree(self, entries):
        large = set()
        for entry in entries:
            if entry.kind in ("1", "?"):
                size = self._size(entry.path)
                if size is not None and size > self.large_file_bytes:
                    large.add(entry.path)
        return large

    def _embedded_repos(self, entries):
        """Untracked or changed folders that are git repositories of their own (would become empty gitlinks)."""
        found = set()
        for entry in entries:
            if entry.kind not in ("1", "?"):
                continue
            path = entry.path.rstrip("/")
            candidate = self.root / path
            if candidate.is_dir() and (candidate / ".git").exists():
                found.add(path)
        return found

    def _recent(self, entries, now=None):
        """Changed files modified in the last few seconds: probably still being written by another program."""
        now = time.time() if now is None else now
        recent = set()
        for entry in entries:
            if entry.kind not in ("1", "?"):
                continue
            try:
                info = os.lstat(self.root / entry.path)
            except (OSError, ValueError):
                continue
            if stat.S_ISREG(info.st_mode) and now - info.st_mtime < self.recent_seconds:
                recent.add(entry.path)
        return recent

    def _large_staged(self, staged):
        large, unknown = set(), {}
        for entry in staged:
            if entry.x not in ("A", "M", "T"):
                continue
            # When the work tree matches the index ('.'), its size is the staged blob size.
            size = self._size(entry.path) if entry.y == "." else None
            if size is None:
                unknown.setdefault(entry.index_oid, []).append(entry.path)
            elif size > self.large_file_bytes:
                large.add(entry.path)
        if unknown:
            out = self._git(["cat-file", "--batch-check=%(objectname) %(objectsize)"],
                            data="".join(oid + "\n" for oid in unknown)).stdout
            for line in out.splitlines():
                oid, _, size = line.partition(" ")
                if size.strip().isdigit() and int(size) > self.large_file_bytes:
                    large.update(unknown.get(oid, ()))
        return large

    def _stage(self, large):
        args, data = ["add", "-A", "--ignore-errors"], None
        if large:
            # Large files are excluded before staging so their content never enters .git/objects.
            args += ["--pathspec-from-file=-", "--pathspec-file-nul"]
            data = _nul(["."] + [f":(exclude,literal){path}" for path in sorted(large)])
        result = self._git(args, data=data, timeout=LONG_TIMEOUT, retry_lock=True, check=False)
        if result.returncode == 0:
            return []
        if "fatal:" in result.stderr.lower():
            raise GitError(_failure("add", result.stderr))
        # --ignore-errors: unreadable files (e.g. open in another program) wait for the next commit.
        unreadable = re.findall(r"unable to index file '(.+?)'", result.stderr)
        if unreadable:
            return [f"无法读取，暂未提交（可能正被其他程序占用）：{redact(path)}" for path in unreadable[:10]]
        return [f"部分文件暂未提交：{_tail(result.stderr)}"]

    def _merge_attributes(self):
        path = self.root / ".gitattributes"
        old = _read_bytes(path)
        if old is None:
            _atomic_write(path, (ATTRIBUTES_RULE + "\n").encode("utf-8"))
            return True
        text = old.decode("utf-8", "surrogateescape")
        lines = text.splitlines()
        rules = [line.strip().lstrip("\ufeff") for line in lines]
        rules = [rule for rule in rules if rule and not rule.startswith("#")]
        if rules and rules[-1].split() == ATTRIBUTES_RULE.split():
            return False
        # A later line could set 'text' again; '* -text' must be the last rule.
        newline = "\r\n" if "\r\n" in text else "\n"
        kept = [line for line in lines if line.strip().lstrip("\ufeff").split() != ATTRIBUTES_RULE.split()]
        while kept and not kept[-1].strip():
            kept.pop()
        _atomic_write(path, (newline.join(kept + [ATTRIBUTES_RULE]) + newline).encode("utf-8", "surrogateescape"))
        return True

    def _merge_ignore(self):
        path = self.root / ".gitignore"
        text = (_read_bytes(path) or b"").decode("utf-8", "surrogateescape")
        present = {line.strip().lstrip("\ufeff") for line in text.splitlines()}
        missing = [line for line in REQUIRED_IGNORES if line not in present]
        if not missing:
            return False
        newline = "\r\n" if "\r\n" in text else "\n"
        if text and not text.endswith(("\n", "\r")):
            text += newline
        _atomic_write(path, (text + newline.join(missing) + newline).encode("utf-8", "surrogateescape"))
        return True

    def _ensure_files(self):
        changed = []
        if self._merge_attributes():
            changed.append(".gitattributes")
        if self._merge_ignore():
            changed.append(".gitignore")
        # .git/info/attributes has the highest precedence: a nested .gitattributes (for example in a
        # toolbox copied from GitHub) cannot turn line-ending conversion back on.
        try:
            info = self.root / ".git" / "info" / "attributes"
            if (self.root / ".git").is_dir():
                text = (_read_bytes(info) or b"").decode("utf-8", "replace")
                if ATTRIBUTES_RULE not in [line.strip() for line in text.splitlines()]:
                    info.parent.mkdir(parents=True, exist_ok=True)
                    _atomic_write(info, (text.rstrip("\n") + ("\n" if text.strip() else "") + ATTRIBUTES_RULE + "\n").encode("utf-8"))
        except OSError:
            LOG.warning("无法写入 .git/info/attributes", exc_info=True)
        return changed

    def _log(self, extra, path=None):
        """Commits as {'sha','short','date','message'}; with path also 'path' (its name in that commit)."""
        args = ["log", "-z", "--no-color", "--no-show-signature", f"--format={LOG_FORMAT}", *extra]
        if path is not None:
            args += ["--follow", "--name-only", "--", path]
        out = self._git(args, literal=path is not None).stdout
        commits = []
        for record in out.split("\x1e")[1:]:
            fields = record.split("\x1f", 4)
            if len(fields) < 4:
                continue
            commit = {"sha": fields[0].strip(), "short": fields[1], "date": fields[2], "message": fields[3].strip()}
            if path is not None:
                names = [name for name in (fields[4] if len(fields) > 4 else "").replace("\n", "\0").split("\0") if name]
                commit["path"] = names[0] if names else path
            commits.append(commit)
        return commits

    def _blob(self, rev, path):
        result = self._git(["cat-file", "blob", f"{rev}:{path}"], binary=True, check=False)
        return result.stdout if result.returncode == 0 else None

    def _commit(self, message, only_if_unborn=False):
        # add, reset and commit all read work-tree files, so the write lock is held throughout.
        with _WorkTreeLocks(self.write_lock, self._lock):
            git_dir = self._require_repo()
            if only_if_unborn and self._has_head():
                return None
            self._ensure_files()
            if any((git_dir / name).exists() for name in BUSY_STATES):
                raise GitError("版本库正在进行合并、变基或拣选，自动提交已暂停。请先在 git 中完成或放弃该操作。")
            scan = self._scan()
            if scan.detached:
                raise GitError("当前不在任何分支上（HEAD 处于分离状态），自动提交已暂停。请先切回分支。")
            if any(entry.kind == "u" for entry in scan.entries):
                raise GitError("有未解决的合并冲突，自动提交已暂停。")
            large = self._large_in_worktree(scan.entries)
            embedded = self._embedded_repos(scan.entries)
            # Files the store itself just wrote are safe (the store lock is held); only other
            # programs' fresh files are deferred.
            recent = {p for p in self._recent(scan.entries) if not self._own_file(p)} if self.recent_seconds else set()
            budget = self._new_data_budget(scan.entries, large | embedded | recent)
            self._embedded_list = sorted(embedded)
            self._budget_skipped = sorted(budget)
            excluded = large | embedded | recent | budget
            warnings = self._stage(excluded)
            for warning in warnings:
                LOG.warning("%s", warning)
            staged = [entry for entry in self._scan().entries if entry.kind == "1" and entry.x != "."]
            # Catches large blobs staged earlier by hand; reset restores HEAD's entry (or drops a new one).
            large_staged = self._large_staged(staged)
            if large_staged:
                self._git(["reset", "-q", "--pathspec-from-file=-", "--pathspec-file-nul"],
                          data=_nul(sorted(large_staged)), literal=True, timeout=LONG_TIMEOUT, retry_lock=True)
            # A tracked file that grew past the limit: stop tracking it, so git keeps no stale copy.
            grown = sorted(p for p in large if self._tracked(p))
            if grown:
                self._git(["rm", "-q", "--cached", "--pathspec-from-file=-", "--pathspec-file-nul"],
                          data=_nul(grown), literal=True, timeout=LONG_TIMEOUT, retry_lock=True, check=False)
                self._untracked_large = sorted(set(getattr(self, "_untracked_large", [])) | set(grown))
            files = sum(1 for entry in staged if entry.path not in large_staged) + len(grown)
            skipped = [_display(path) for path in sorted(large | large_staged)]
            self._skipped_large = [{"path": _display(p), "size": self._size(p) or 0} for p in sorted(large | large_staged)]
            if skipped:
                LOG.info("超过 %d 字节、未提交到 git 的文件：%s", self.large_file_bytes, "、".join(skipped))
            if not files:
                return {"deferred": True} if recent else None
            self._git(["commit", "-q", "-F", "-"], data=message, timeout=LONG_TIMEOUT, retry_lock=True)
            head = self._log(["-n", "1"])[0]
        return {"sha": head["sha"], "short": head["short"], "files": files, "skipped_large": skipped,
                "message": message, "warnings": warnings, "deferred": bool(recent), "embedded_repos": sorted(embedded),
                "untracked_large": grown, "over_budget": sorted(budget)}

    def _own_file(self, path):
        return path.endswith((".md", ".json")) and not path.startswith(("资料/",))

    def _tracked(self, path):
        result = self._git(["ls-files", "--error-unmatch", "--", path], literal=True, check=False)
        return result.returncode == 0

    def _new_data_budget(self, entries, excluded):
        """New data in one commit is capped; beyond it files wait (and are listed) instead of one huge push."""
        total, over = 0, set()
        for entry in sorted(entries, key=lambda e: e.path):
            if entry.kind not in ("1", "?") or entry.path in excluded:
                continue
            size = self._size(entry.path) or 0
            if total + size > COMMIT_BUDGET_BYTES:
                over.add(entry.path)
            else:
                total += size
        return over

    def _read_push_record(self):
        try:
            data = json.loads((self.root / ".runtime" / "git_push.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _write_push_record(self, record):
        try:
            _atomic_write(self.root / ".runtime" / "git_push.json",
                          json.dumps(record, ensure_ascii=False, indent=2).encode("utf-8"))
        except OSError:
            LOG.warning("无法写入 .runtime/git_push.json", exc_info=True)

    # public API ----------------------------------------------------------------------
    def is_repo(self) -> bool:
        if not self.available():
            return False
        try:
            with self._lock:
                return self._locate()[0] is not None
        except GitError:
            return False

    def ensure(self) -> dict:
        """Create the repository if needed and keep .gitattributes/.gitignore correct.

        A fresh repository (or one without commits) gets the initial commit.
        Problems are reported in the returned status' 'message', not raised.
        """
        config = self._config()
        initialized, problem = False, ""
        if self.available():
            try:
                with self._lock:
                    _, kind, problem = self._locate()
                    if kind == "none":
                        branch = config["branch"] if _valid_branch(config["branch"]) else "main"
                        self.root.mkdir(parents=True, exist_ok=True)
                        self._git(["init", "-q", f"--initial-branch={branch}"])
                        initialized = True
                        self._require_repo()
                        problem = ""
                    if kind in ("ok", "none"):
                        self._ensure_files()
                if kind in ("ok", "none"):
                    initialized = self._commit(INIT_MESSAGE, only_if_unborn=True) is not None or initialized
            except (GitError, OSError) as exc:
                problem = str(exc)
        result = self.status()
        result["initialized"] = initialized
        if problem:
            result["message"] = problem
        return result

    def commit(self, message: str) -> dict | None:
        """Stage everything (respecting .gitignore) except files over the size limit and commit.

        Returns {'sha','short','files','skipped_large','message','warnings'}, or None
        when nothing is staged. Raises GitError when the commit cannot be made.
        """
        if not isinstance(message, str) or not message.strip() or "\0" in message:
            raise ValueError("提交说明不能为空。")
        result = self._commit(message.strip())
        with self._state:
            self._last_error = ""
        return result

    def commit_async(self, message: str) -> None:
        """Queue a commit; messages arriving within the coalescing window share one commit."""
        if not self._config()["auto_commit"]:
            return None
        text = (" ".join(message.split()) if isinstance(message, str) else "") or "更新研究资料"
        with self._state:
            if not self._pending:
                self._deadline = time.monotonic() + self.coalesce_seconds
            if text not in self._pending:
                self._pending.append(text)
            if self._commit_thread is None:
                self._commit_thread = threading.Thread(target=self._commit_worker, name="vcs-commit", daemon=True)
                self._commit_thread.start()
            self._state.notify_all()
        return None

    def _commit_worker(self):
        while True:
            with self._state:
                if not self._pending:
                    self._commit_thread = None
                    self._state.notify_all()
                    return
                while not self._flush:
                    remaining = self._deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self._state.wait(remaining)
                messages, self._pending = self._pending, []
                self._committing = True
            result, error = None, ""
            try:
                result = self.commit(_join_messages(messages))
            except (GitError, ValueError, OSError) as exc:
                error = str(exc)
                LOG.warning("自动提交失败：%s", error)
            except Exception:  # noqa: BLE001 - the worker must survive and report
                error = "自动提交失败，请查看服务日志。"
                LOG.exception("git commit worker failed")
            with self._state:
                self._committing = False
                if error:
                    self._last_error = error
                    # Keep the messages: retry with backoff instead of losing this change's description.
                    self._retries = getattr(self, "_retries", 0) + 1
                    if self._retries <= len(RETRY_DELAYS) and not self._closing:
                        for text in reversed(messages):
                            if text not in self._pending:
                                self._pending.insert(0, text)
                        self._deadline = time.monotonic() + RETRY_DELAYS[self._retries - 1]
                else:
                    self._retries = 0
                    self._last_error = ""
                    if result is not None and result.get("deferred"):
                        # Files still being written were left out; commit them once they settle.
                        if "补充提交仍在写入的文件" not in self._pending:
                            self._pending.append("补充提交仍在写入的文件")
                        self._deadline = time.monotonic() + RECENT_SECONDS + 1
                self._state.notify_all()
            if result is not None and result.get("sha") and self._config()["auto_push"]:
                self.push_async()

    def push(self, timeout: int = 120) -> dict:
        """'git push -u <remote> <branch>' (never forced). The result is also saved to .runtime/git_push.json."""
        config = self._config()
        remote, branch = config["remote"], config["branch"]
        with self._push_mutex:
            try:
                record = self._push(remote, branch, timeout)
            except GitTimeout:
                record = self._push_record(False, "timeout", f"推送超过 {timeout} 秒未完成，已停止；下次提交后会再推送。", remote, branch)
            except GitError as exc:
                record = self._push_record(False, "error", str(exc), remote, branch)
            with self._lock:
                self._write_push_record(record)
            return record

    @staticmethod
    def _push_record(ok, code, message, remote, branch, detail=""):
        return {"ok": ok, "at": _now(), "message": message, "code": code, "remote": remote, "branch": branch,
                "detail": detail}

    def _push(self, remote, branch, timeout):
        def record(ok, code, message, detail=""):
            return self._push_record(ok, code, message, remote, branch, detail)
        if not self.available():
            return record(False, "unavailable", "未找到 git，无法推送。")
        if not _valid_remote(remote):
            return record(False, "error", f"设置中的远程仓库名称无效：{remote}")
        if not _valid_branch(branch):
            return record(False, "error", f"设置中的分支名称无效：{branch}")
        with self._lock:
            git_dir, _, message = self._locate()
            if git_dir is None:
                return record(False, "not_repo", message)
            if self._git(["remote", "get-url", remote], check=False).returncode != 0:
                return record(False, "no_remote", f"还没有设置远程仓库 {remote}，未推送。")
            if self._git(["rev-parse", "--verify", "-q", f"refs/heads/{branch}"], check=False).returncode != 0:
                # Push whatever branch is checked out (for example 'master' after a plain git init).
                current = self._git(["symbolic-ref", "--short", "-q", "HEAD"], check=False).stdout.strip()
                if not current or not _valid_branch(current):
                    return record(False, "no_branch", f"本地还没有分支 {branch} 的提交，未推送。")
                branch = current
        # The network part runs outside _lock; _push_mutex keeps pushes one at a time.
        # Stopped only after a long time without progress, so a slow upload can finish.
        result = self._git(["push", "--progress", "-u", remote, f"HEAD:refs/heads/{branch}"], timeout=max(timeout, PUSH_MAX_SECONDS),
                           idle=PUSH_IDLE_SECONDS, check=False)
        if result.returncode == 0:
            return record(True, "ok", f"已推送到 {remote}/{branch}。")
        code, message = _push_failure(result.stderr)
        LOG.warning("git push 失败：%s", _tail(result.stderr))
        return record(False, code, message, _tail(result.stderr, 500))

    def push_async(self) -> None:
        """Push on the single push worker; a request during a running push schedules one more run."""
        with self._state:
            if self._push_thread is not None:
                self._push_again = True
                return None
            self._push_again = False
            self._push_thread = threading.Thread(target=self._push_worker, name="vcs-push", daemon=True)
            self._push_thread.start()
        return None

    def _push_worker(self):
        while True:
            try:
                self.push()
            except Exception:  # noqa: BLE001 - push() reports failures itself; keep the worker alive
                LOG.exception("git push worker failed")
            with self._state:
                if not self._push_again:
                    self._push_thread = None
                    self._state.notify_all()
                    return
                self._push_again = False

    def status(self) -> dict:
        """Never raises: problems are reported in 'message'."""
        config = self._config()
        with self._state:
            pending, pushing, last_error = bool(self._pending) or self._committing, self._push_thread is not None, self._last_error
        result = {"available": False, "is_repo": False, "branch": "", "dirty": False, "ahead": 0,
                  "remote": config["remote"], "remote_url": "", "last_commit": None, "last_push": None,
                  "message": "", "pending": pending, "pushing": pushing, "last_error": last_error, "skipped_large": []}
        if not self.available():
            result["message"] = NO_GIT
            return result
        result["available"] = True
        with _WorkTreeLocks(self.write_lock, self._lock) as locks:
            result["last_push"] = self._read_push_record()
            try:
                git_dir, _, message = self._locate()
                if git_dir is None:
                    result["message"] = message
                    return result
                result["is_repo"] = True
                scan = self._scan()
                locks.release_outer()  # the rest reads only .git
                large = self._large_in_worktree(scan.entries)
                embedded = self._embedded_repos(scan.entries)
                result["branch"] = "" if scan.detached else scan.head
                result["dirty"] = any(entry.path not in large and entry.path.rstrip("/") not in embedded for entry in scan.entries)
                result["skipped_large"] = [{"path": _display(p), "size": self._size(p) or 0} for p in sorted(large)]
                result["untracked_large"] = list(self._untracked_large)
                result["embedded_repos"] = sorted(embedded)
                result["over_budget"] = list(self._budget_skipped)
                result["ignored_data"] = self._ignored_data()
                if last_error:
                    result["last_error_hint"] = ("如果没有其他 git 程序在运行，可以删除 .git/index.lock 后在设置里点“立即推送”。"
                                                 if "index.lock" in last_error else "系统会自动重试几次；仍然失败时请查看服务日志。")
                if scan.detached:
                    result["message"] = "HEAD 处于分离状态，自动提交已暂停。"
                if scan.oid:
                    result["ahead"] = self._ahead(scan, config)
                    last = self._log(["-n", "1"])
                    result["last_commit"] = last[0] if last else None
                if _valid_remote(config["remote"]):
                    url = self._git(["remote", "get-url", config["remote"]], check=False)
                    result["remote_url"] = redact(url.stdout.strip()) if url.returncode == 0 else ""
                record = result["last_push"]
                if record and not record.get("ok") and scan.oid:
                    # A push that was reported as failed may still have arrived (for example after a restart).
                    remote_ref = self._git(["rev-parse", "-q", "--verify", f"refs/remotes/{config['remote']}/{result['branch']}"], check=False).stdout.strip()
                    if remote_ref and remote_ref == scan.oid:
                        record = dict(record, ok=True, code="ok", message="已推送（之前的失败记录已过期）。")
                        result["last_push"] = record
                        self._write_push_record(record)
            except GitError as exc:
                result["message"] = str(exc)
        return result

    def _ignored_data(self):
        """Research data files kept out of git by the data patterns: how many, and some examples."""
        result = self._git(["status", "--porcelain", "--ignored", "-z", "--untracked-files=all", "--",
                            "01_论文框架", "02_研究内容"], literal=True, check=False)
        patterns = (".mat", ".bmp", ".tif", ".tiff", ".pt", ".pth", ".zip", ".tgz")
        paths = [item[3:] for item in result.stdout.split("\0") if item.startswith("!! ") and item.lower().endswith(patterns)]
        return {"count": len(paths), "examples": [_display(p) for p in paths[:20]]}

    def _ahead(self, scan, config):
        if scan.ahead is not None:  # the branch has an upstream
            return scan.ahead
        # Before the first 'push -u': commits that no branch of the remote has (all of them if never pushed).
        args = ["rev-list", "--count", "HEAD"]
        if _valid_remote(config["remote"]):
            args += ["--not", f"--remotes={config['remote']}"]
        return _count(self._git(args).stdout)

    def history(self, path: str, limit: int = 50) -> list[dict]:
        """Commits touching path (following renames), newest first: [{'sha','short','date','message'}]."""
        path = clean_path(path)
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ValueError("条数必须是整数。")
        limit = max(1, min(limit, 1000))
        if not self.available():
            return []
        with self._lock:
            if self._locate()[0] is None or not self._has_head():
                return []
            return self._log(["-n", str(limit)], path=path)

    def show(self, rev: str, path: str) -> bytes:
        """Raw bytes of path at commit rev (7–40 hex characters).

        If path had another name at rev, the name that history() reports for that commit is used.
        """
        if not isinstance(rev, str) or not REV_RE.fullmatch(rev):
            raise ValueError("版本号无效：应为 7–40 位十六进制提交号。")
        path = clean_path(path)
        with self._lock:
            self._require_repo(FileNotFoundError)
            # cat-file prints the stored blob exactly as 'git show rev:path' does, with no conversion.
            data = self._blob(rev, path)
            if data is None:
                commit = self._git(["rev-parse", "--verify", "-q", f"{rev}^{{commit}}"], check=False).stdout.strip()
                if commit:
                    for entry in self._log([], path=path):
                        if entry["sha"] == commit and entry["path"] != path:
                            data = self._blob(commit, entry["path"])
                            break
        if data is None:
            raise FileNotFoundError("这个版本中没有该文件。")
        return data

    def set_remote(self, url: str) -> dict:
        url = clean_remote_url(url)
        remote = self._config()["remote"]
        if not _valid_remote(remote):
            raise ValueError(f"设置中的远程仓库名称无效：{remote}")
        with self._lock:
            self._require_repo()
            exists = self._git(["remote", "get-url", remote], check=False).returncode == 0
            self._git(["remote", "set-url" if exists else "add", remote, url])
        return self.status()

    def close(self, timeout: float = 10) -> None:
        """Commit anything queued now and wait (up to timeout seconds) for the commit and push workers."""
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._state:
            self._flush = True
            self._closing = True
            self._state.notify_all()
            for attribute in ("_commit_thread", "_push_thread"):
                while getattr(self, attribute) is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self._state.wait(remaining)
            if self._commit_thread is None:
                self._flush = False
