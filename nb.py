"""Local research file system: web application and shared CLI entry."""
from __future__ import annotations

import argparse
import json
import logging
import sys
import webbrowser
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener

APP_ROOT = Path(__file__).resolve().parent


def client(url, path, data=None):
    headers = {"X-Research-Notebook": "1", "Content-Type": "application/json"}
    request = Request(url + path, data=None if data is None else json.dumps(data, ensure_ascii=False).encode("utf-8"), headers=headers)
    with build_opener(ProxyHandler({})).open(request, timeout=15) as response:
        return json.load(response)


def main(argv=None):
    parser = argparse.ArgumentParser(description="科研文件管理系统")
    parser.add_argument("command", nargs="?", choices=["serve", "stop", "state", "sync", "action", "init-example"], default="serve")
    parser.add_argument("--root", type=Path, default=APP_ROOT)
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--data", type=Path, help="action 的 UTF-8 JSON 文件，包含 action 字段")
    args = parser.parse_args(argv)
    if sys.version_info < (3, 11):
        parser.error("需要 Python 3.11 或更新版本。")
    if not 1024 <= args.port <= 65535:
        parser.error("端口须在 1024 至 65535 之间。")
    root = args.root.resolve()
    url = f"http://127.0.0.1:{args.port}"
    health = None
    try:
        health = client(url, "/api/health")
    except (OSError, ValueError, URLError):
        pass
    if health is not None and (not isinstance(health, dict) or health.get("app") != "research-file-system" or not isinstance(health.get("root"), str) or Path(health["root"]).resolve() != root):
        parser.error("此端口正在服务其他目录，请使用另一个 --port。")
    if args.command == "stop":
        print(json.dumps(client(url, "/api/stop", {}) if health else {"message": "系统当前未运行。"}, ensure_ascii=False))
        return 0
    payload = None
    if args.command == "action":
        if not args.data:
            parser.error("action 需要 --data 指定 JSON 文件。")
        payload = json.loads(args.data.read_text(encoding="utf-8-sig"))
        if not isinstance(payload, dict) or not isinstance(payload.get("action"), str):
            parser.error("JSON 应为含 action 的对象。")
    if health:
        if args.command == "serve":
            if not args.no_browser:
                webbrowser.open(url)
            print(url)
            return 0
        if args.command in {"state", "sync", "action"}:
            result = client(url, "/api/state") if args.command == "state" else client(url, "/api/action", payload or {"action": "sync"})
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        parser.error("请停止示例服务后再初始化示例。")
    from notebook.locking import WorkspaceLock
    from notebook.store import Store
    with WorkspaceLock(root):
        store = Store(root)
        if args.command in {"state", "sync", "action", "init-example"}:
            if args.command == "init-example":
                from notebook.example import initialize_example
                result = initialize_example(store)
            elif args.command == "state":
                result = store.state()
            elif args.command == "sync":
                result = store.perform("sync", {})
            else:
                operation = payload.pop("action")
                result = store.perform(operation, payload)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        from notebook.server import NotebookServer
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
        server = NotebookServer(("127.0.0.1", args.port), store)
        print(f"科研文件管理系统：{url}\n资料目录：{root}", flush=True)
        if not args.no_browser:
            webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
