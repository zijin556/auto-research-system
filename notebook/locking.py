"""Prevent competing server/CLI writers for one research root."""
from __future__ import annotations
import os
from pathlib import Path


class WorkspaceLock:
    def __init__(self, root):
        self.directory = Path(root) / ".runtime"
        self.handle = None

    def __enter__(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        self.handle = (self.directory / "workspace.lock").open("a+b")
        self.handle.seek(0, 2)
        if not self.handle.tell():
            self.handle.write(b"0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.handle.close()
            raise RuntimeError("这个研究目录已有系统正在运行，请使用现有页面。") from exc
        return self

    def __exit__(self, *args):
        if self.handle:
            self.handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
