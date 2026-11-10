"""自动检测本机已安装并登录的 AI 命令行工具（Codex / Antigravity / Claude Code），
读取本机可用模型，并自动生成适配当前环境的 00_模型与额度.json。

用法：
  py -3.12 -X utf8 tools/auto_configure.py          # 自动检测并写入 00_模型与额度.json
  py -3.12 -X utf8 tools/auto_configure.py --dry-run # 仅输出检测结果与建议配置，不修改文件
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from notebook import agents  # noqa: E402


def detect_codex_models() -> list[str]:
    cache = Path.home() / ".codex" / "models_cache.json"
    out = []
    if cache.is_file():
        try:
            data = json.loads(cache.read_text(encoding="utf-8", errors="replace"))
            for item in data.get("models") or []:
                slug = item.get("slug") if isinstance(item, dict) else None
                if isinstance(slug, str) and slug.startswith(("gpt-", "o")):
                    out.append(slug)
        except (OSError, ValueError):
            pass
    return out or ["gpt-6.1-sol"]


def detect_claude_models() -> dict[str, str]:
    settings = Path.home() / ".claude" / "settings.json"
    defaults = {
        "opus": "claude-opus-5-5",
        "sonnet": "claude-sonnet-5-5",
        "haiku": "claude-haiku-5-5",
    }
    if settings.is_file():
        try:
            data = json.loads(settings.read_text(encoding="utf-8", errors="replace"))
            env = data.get("env") if isinstance(data.get("env"), dict) else {}
            if env.get("ANTHROPIC_DEFAULT_OPUS_MODEL"):
                defaults["opus"] = str(env["ANTHROPIC_DEFAULT_OPUS_MODEL"])
            if env.get("ANTHROPIC_DEFAULT_SONNET_MODEL"):
                defaults["sonnet"] = str(env["ANTHROPIC_DEFAULT_SONNET_MODEL"])
            if env.get("ANTHROPIC_DEFAULT_HAIKU_MODEL"):
                defaults["haiku"] = str(env["ANTHROPIC_DEFAULT_HAIKU_MODEL"])
        except (OSError, ValueError):
            pass
    return defaults


def build_config(status: dict) -> tuple[dict, list[str]]:
    notes = []
    codex_ok = bool(status.get("codex", {}).get("installed") and status.get("codex", {}).get("logged_in") is not False)
    agy_ok = bool(status.get("agy", {}).get("installed") and status.get("agy", {}).get("logged_in") is not False)
    claude_ok = bool(status.get("claude", {}).get("installed") and status.get("claude", {}).get("logged_in") is not False)

    codex_models = detect_codex_models()
    claude_models = detect_claude_models()
    gpt_main = {"provider": "codex", "model": codex_models[0], "effort": "high"}
    gemini_strong = {"provider": "agy", "model": "gemini-4-argon-high"}
    gemini_fast = {"provider": "agy", "model": "gemini-3.8-flash-medium"}
    claude_strong = {"provider": "claude", "model": claude_models["opus"]}
    claude_fast = {"provider": "claude", "model": claude_models["haiku"]}

    available_families = sum([codex_ok, agy_ok, claude_ok])
    strict_family = available_families >= 2

    def filter_avail(cands: list[dict]) -> list[dict]:
        avail = {"codex": codex_ok, "agy": agy_ok, "claude": claude_ok}
        picked = [dict(c) for c in cands if avail.get(c["provider"])]
        return picked or [dict(c) for c in cands]

    roles = {
        "author": filter_avail([gpt_main, claude_strong, gemini_strong]),
        "reviewer": filter_avail([gemini_strong, gpt_main, claude_strong]),
        "engineer": [dict(gpt_main)],
        "writer": filter_avail([gemini_strong, gpt_main, claude_strong]),
        "drift": filter_avail([claude_fast, gemini_fast, gpt_main]),
        "referee": filter_avail([claude_strong, gemini_strong, gpt_main]),
    }

    if not codex_ok:
        notes.append("未检测到可用的 Codex 命令行：工程验证（自动运行 Python 核对脚本）将暂停，其余讨论角色已自动切换到可用模型。")
    if available_families == 1:
        notes.append("当前仅检测到 1 家可用模型工具：已自动设置 strict_family=false，允许同一模型家族兼任作者与审查角色。")
    elif available_families == 0:
        notes.append("尚未检测到已安装并登录的模型工具（codex / agy / claude），已生成默认模板配置。")

    cfg = {
        "schema": 1,
        "strict_family": strict_family,
        "roles": roles,
        "limits": dict(agents.DEFAULT_LIMITS),
        "git": {
            "auto_commit": True,
            "auto_push": False,
            "remote": "origin",
            "branch": "main",
        },
    }
    return cfg, notes


def main() -> int:
    parser = argparse.ArgumentParser(description="自动检测本机 AI 工具并生成 00_模型与额度.json")
    parser.add_argument("--dry-run", action="store_true", help="仅打印检测结果，不写入配置文件")
    args = parser.parse_args()

    status = agents.provider_status()
    print("=== 本机 AI 命令行工具检测结果 ===")
    for key, info in status.items():
        inst = "已安装" if info.get("installed") else "未找到"
        ver = f" ({info.get('version')})" if info.get("version") else ""
        login = info.get("login_text") or ("已登录" if info.get("logged_in") else "未登录")
        print(f"- {key:6s}: {inst}{ver} | 登录状态：{login}")

    cfg, notes = build_config(status)
    if notes:
        print("\n=== 配置说明 ===")
        for note in notes:
            print(f"* {note}")

    target = ROOT / agents.SETTINGS_NAME
    if args.dry_run:
        print("\n=== 建议生成的 00_模型与额度.json ===")
        print(json.dumps(cfg, ensure_ascii=False, indent=2))
        return 0

    target.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\n已写入配置文件：{target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
