"""Loopback-only transport. Research operations remain in Store."""
from __future__ import annotations

import json
import logging
import mimetypes
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

APP_ROOT = Path(__file__).resolve().parent.parent
STATIC = APP_ROOT / "static"
MAX_BODY = 48 * 1024 * 1024
LOG = logging.getLogger("research-notebook")
TEXT_TYPES = {".md", ".txt", ".py", ".m", ".json", ".csv", ".log", ".yaml", ".yml", ".toml", ".tex", ".r", ".ipynb"}
IMAGE_TYPES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


class NotebookServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, store):
        self.store = store
        self._semantic_manager = None
        self._semantic_lock = threading.Lock()
        self._v2 = None
        self._v2_lock = threading.Lock()
        super().__init__(address, Handler)

    def semantic_manager(self):
        with self._semantic_lock:
            if self._semantic_manager is None:
                from .semantic_jobs import SemanticJobs
                self._semantic_manager = SemanticJobs(self.store)
            return self._semantic_manager

    def v2(self):
        with self._v2_lock:
            if self._v2 is None:
                try:
                    from .v2api import V2
                    self._v2 = V2(self.store)
                except Exception as exc:  # noqa: BLE001 - keep the classic page usable
                    LOG.exception("v2 features unavailable")
                    self._v2 = _V2Unavailable(exc)
            return self._v2

    def server_close(self):
        if self._semantic_manager is not None:
            self._semantic_manager.close()
        if self._v2 is not None:
            self._v2.close()
        super().server_close()


class _V2Unavailable:
    """Placeholder when the v2 modules cannot start (the classic page still works)."""

    def __init__(self, exc):
        self.reason = f"新版功能没有启动：{exc}"

    def get(self, name, params):
        raise ValueError(self.reason)

    def action(self, name, data):
        raise ValueError(self.reason)

    def after_classic(self, action, data=None):
        return None

    def close(self):
        return None


class Handler(BaseHTTPRequestHandler):
    server_version = "ResearchNotebook/1.0"

    def log_message(self, fmt, *args):
        LOG.info(fmt, *args)

    def guard(self, write=False):
        hosts = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        if self.headers.get("Host", "").lower() not in hosts:
            raise PermissionError("请从本机科研文件管理系统地址访问。")
        origin = self.headers.get("Origin")
        if origin and origin not in {"http://" + host for host in hosts}:
            raise PermissionError("不接受其他网站的请求。")
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            if write or self.headers.get("Sec-Fetch-Mode") != "navigate":
                raise PermissionError("不接受跨站请求。")
        if write:
            if self.headers.get("X-Research-Notebook") != "1":
                raise PermissionError("请从本机系统页面提交修改。")
            if self.headers.get_content_type() != "application/json":
                raise ValueError("请求格式应为 JSON。")

    def headers_for(self, size, kind, status=200, disposition=None):
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        # KaTeX sets inline layout styles, but no inline/user scripts are allowed.
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; font-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if disposition:
            self.send_header("Content-Disposition", disposition)
        self.end_headers()

    def send_bytes(self, body, kind="application/json; charset=utf-8", status=200):
        self.headers_for(len(body), kind, status)
        self.wfile.write(body)

    def send_json(self, obj, status=200):
        self.send_bytes(json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"), status=status)

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("无效的内容长度。") from exc
        if not 0 < length <= MAX_BODY:
            raise ValueError("单次请求不得超过 48 MB；大数据请留在原位并登记路径。")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise ValueError("请求未完整接收，请重试。")
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("请求不是有效的 UTF-8 JSON。") from exc
        if not isinstance(data, dict):
            raise ValueError("请求必须是对象。")
        return data

    def send_file(self, path, static=False, download=False):
        path = Path(path)
        extension = path.suffix.lower()
        disposition = None
        if static:
            kind = {".js": "application/javascript", ".css": "text/css", ".html": "text/html", ".md": "text/plain; charset=utf-8", ".json": "application/json; charset=utf-8", ".woff2": "font/woff2", ".woff": "font/woff", ".ttf": "font/ttf"}.get(extension)
            if not kind:
                raise FileNotFoundError("没有找到静态资源。")
        elif extension in TEXT_TYPES and not download:
            kind = "text/plain; charset=utf-8"
        elif extension in IMAGE_TYPES and not download:
            kind = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        else:
            # Never render uploaded HTML/SVG or run code in the browser origin.
            kind = "application/octet-stream"
            disposition = "attachment; filename*=UTF-8''" + quote(path.name)
        if not path.is_file():
            raise FileNotFoundError("文件不存在。")
        self.headers_for(path.stat().st_size, kind, disposition=disposition)
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                self.wfile.write(chunk)

    def do_GET(self):
        try:
            self.guard()
            request = urlsplit(self.path)
            route, params = unquote(request.path), parse_qs(request.query)
            store = self.server.store
            if route == "/api/health":
                return self.send_json({"app": "research-file-system", "root": str(store.root), "version": 1, "pid": os.getpid()})
            if route == "/api/state":
                state = store.state()
                state["is_example"] = Path(store.root).name == "示例研究"
                return self.send_json(state)
            if route == "/api/semantic-status":
                from .semantic_jobs import provider_status
                return self.send_json(provider_status())
            if route == "/api/semantic-job":
                return self.send_json(self.server.semantic_manager().snapshot(params.get("id", [""])[0]))
            if route == "/api/question":
                return self.send_json(store.detail(params.get("id", [""])[0]))
            if route == "/api/content":
                return self.send_json(store.content_detail(params.get("id", [""])[0]))
            if route == "/api/file":
                return self.send_file(store.read_file(params.get("path", [""])[0]), download=params.get("download", [""])[0] == "1")
            if route.startswith("/api/v2/"):
                return self.send_json(self.server.v2().get(route.removeprefix("/api/v2/"), params))
            if route in {"/", "/index.html"}:
                page = STATIC / "v2" / "index.html"
                return self.send_file(page if page.is_file() else STATIC / "index.html", static=True)
            if route.startswith("/static/"):
                relative = route.removeprefix("/static/")
                if "\\" in relative or ":" in relative or "\0" in relative or any(p.startswith(".") for p in Path(relative).parts):
                    raise PermissionError("无效的资源路径。")
                path = (STATIC / relative).resolve()
                if not path.is_relative_to(STATIC.resolve()):
                    raise PermissionError("资源超出范围。")
                return self.send_file(path, static=True)
            raise FileNotFoundError("页面不存在。")
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:
            self.error(exc)

    def do_POST(self):
        try:
            self.guard(write=True)
            data = self.read_json()
            route = urlsplit(self.path).path
            if route == "/api/action":
                action = data.pop("action", "")
                if not isinstance(action, str) or not action:
                    raise ValueError("未指定操作。")
                if action == "open_folder":
                    return self.send_json(self.open_folder(data.get("path", "")))
                if action == "start_semantic":
                    return self.send_json(self.server.semantic_manager().start(data))
                if action == "cancel_semantic":
                    return self.send_json(self.server.semantic_manager().cancel(data.get("job_id", "")))
                if action.startswith("v2_"):
                    return self.send_json(self.server.v2().action(action, data))
                if action == "import_semantic_job":
                    result = self.server.semantic_manager().archive(data)
                    self.version_hook(action, data)
                    return self.send_json(result)
                result = self.server.store.perform(action, data)
                # Every saved change in the classic page also becomes a git version.
                self.version_hook(action, data)
                return self.send_json(result)
            if route == "/api/stop":
                self.send_json({"ok": True, "message": "服务已停止，资料已保存在本地文件夹。"})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            raise FileNotFoundError("操作不存在。")
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:
            self.error(exc)

    def version_hook(self, action, data=None):
        """Best effort: a saved change must never fail because versioning did."""
        try:
            self.server.v2().after_classic(action, data)
        except Exception:  # noqa: BLE001
            LOG.exception("v2 versioning hook failed")

    def open_folder(self, relative):
        if not isinstance(relative, str) or "\0" in relative or ":" in relative or "\\" in relative:
            raise PermissionError("无效的文件夹路径。")
        raw = Path(relative)
        if raw.is_absolute() or any(p.startswith(".") for p in raw.parts):
            raise PermissionError("只能打开本研究目录中的资料。")
        if relative and raw.parts[0] not in {"00_项目卡.md", "01_论文框架", "02_研究内容", "03_提示词", "04_交接包", "总览.md"}:
            raise PermissionError("只能打开已登记的研究资料文件夹。")
        root = Path(self.server.store.root).resolve()
        target = (root / raw).resolve()
        if not target.is_relative_to(root) or not target.exists():
            raise PermissionError("文件夹不存在或超出研究目录。")
        folder = target if target.is_dir() else target.parent
        if os.name != "nt":
            return {"path": str(folder), "opened": False}
        os.startfile(str(folder))
        return {"path": str(folder), "opened": True}

    def error(self, exc):
        name = type(exc).__name__
        status = 409 if name in {"ConflictError", "RevisionConflict"} else 403 if isinstance(exc, PermissionError) else 404 if isinstance(exc, FileNotFoundError) else 400 if isinstance(exc, (ValueError, TypeError, KeyError)) else 500
        if status == 500:
            LOG.exception("Unexpected request failure")
        self.send_json({"error": str(exc) if status != 500 else "操作未完成，原资料未被主动删除。请查看 .runtime/server.error.log。", "type": name}, status)
