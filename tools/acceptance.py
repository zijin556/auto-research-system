"""Run the synthetic file workflow through the real HTTP API, not a mock."""
from __future__ import annotations
import base64
import json
import subprocess
import sys
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener

APP_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_ROOT = (APP_ROOT / "示例研究").resolve()
BASE = "http://127.0.0.1:8788"
OPENER = build_opener(ProxyHandler({}))

CODE = '''# C01-Q01: synthetic acceptance example for an additive fixed-bias correction.
import argparse
import csv
import json
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--bias", type=float, required=True)
args = parser.parse_args()
true_bias = 2.0
xs = [0.0, 10.0, 20.0]
ys = [x + true_bias for x in xs]
corrected = [y - args.bias for y in ys]
errors = [abs(a - b) for a, b in zip(corrected, xs)]
with Path("metrics.csv").open("w", encoding="utf-8", newline="") as file:
    writer = csv.writer(file)
    writer.writerow(["truth", "measured", "corrected", "absolute_error"])
    writer.writerows(zip(xs, ys, corrected, errors))
summary = {"correction_bias": args.bias, "max_absolute_error": max(errors),
           "criterion": "max_absolute_error < 1e-8", "meets_criterion": max(errors) < 1e-8,
           "data_kind": "synthetic software acceptance only"}
Path("summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
print(json.dumps(summary))
'''


def api(route, data=None):
    request = Request(BASE + route, data=None if data is None else json.dumps(data, ensure_ascii=False).encode("utf-8"), headers={"Content-Type": "application/json", "X-Research-Notebook": "1"})
    with OPENER.open(request, timeout=45) as response:
        return json.load(response)


def action(name, **data):
    return api("/api/action", {"action": name, **data})


def main():
    health = api("/api/health")
    if health.get("app") != "research-file-system" or Path(health["root"]).resolve() != EXAMPLE_ROOT:
        raise RuntimeError("Acceptance must target the isolated example root")
    receipt = EXAMPLE_ROOT / ".runtime" / "acceptance.json"
    if receipt.exists():
        print(receipt.read_text(encoding="utf-8"))
        return
    qid = "C01-Q01"
    question = api("/api/question?id=" + qid)
    plan = question["plans"][0]
    discussion = question["discussions"][0]
    runs = []
    statuses = []
    for bias in (2.1, 2.0):
        code = action("add_code", question_id=qid, filename="bias_check.py", text=CODE, purpose=f"软件验收：核对 y=x+2 后减去 {bias} 的残差。")
        run = action("create_run", question_id=qid, title="首次核对" if bias == 2.1 else "修正偏差参数", plan_id=plan["id"], code_paths=[code["path"]], target_step="推导中的 x_hat=y-b", command=f'python bias_check.py --bias {bias}', parameters=f"Python {sys.version.split()[0]}；x=[0,10,20]；true_bias=2；correction_bias={bias}；无随机项。", expected=plan["expected"], mapping=f"| 公式/步骤 | 代码位置 | 实现说明 |\n|---|---|---|\n| y=x+b | {code['name']}:12 | true_bias=2，无近似 |\n| x_hat=y-b | {code['name']}:13 | 校正偏差由 --bias 指定 |\n| 绝对误差 | {code['name']}:14 | 与三个已知输入比较 |")
        action("generate_prompt", question_id=qid, template="P7", source_ids=[plan["id"], run["id"], "body"], focus="软件验收用交接快照，未发送给任何模型。")
        code_path = (EXAMPLE_ROOT / code["path"]).resolve()
        run_dir = (EXAMPLE_ROOT / run["path"]).resolve().parent
        if not code_path.is_relative_to(EXAMPLE_ROOT) or not run_dir.is_relative_to(EXAMPLE_ROOT):
            raise RuntimeError("Example path escaped its root")
        actual_command = [sys.executable, "-X", "utf8", str(code_path), "--bias", str(bias)]
        completed = subprocess.run(actual_command, cwd=run_dir, capture_output=True, text=True, encoding="utf-8", timeout=30, check=True)
        summary = json.loads(completed.stdout)
        (run_dir / "stdout.log").write_text(completed.stdout, encoding="utf-8")
        action("attach_file", question_id=qid, run_id=run["id"], filename="结果说明.txt", base64=base64.b64encode("软件验收示例；数据为合成值，不表示真实设备性能。".encode("utf-8")).decode("ascii"))
        run = action("update_run", question_id=qid, id=run["id"], revision=run["revision"], command=subprocess.list2cmdline(actual_command), outcome="pass" if summary["meets_criterion"] else "fail", actual=json.dumps(summary, ensure_ascii=False, indent=2), analysis="已知真偏差为2，首轮使用2.1导致固定残差；修正后与预期一致。仅用于验收文件关联。")
        assert run["criterion"] == plan["criterion"]
        status_before = api("/api/question?id=" + qid)["status"]
        assert status_before == "needs_judgment", status_before
        decision = "review" if bias == 2.1 else "supported"
        judgment = action("save_judgment", question_id=qid, decision=decision, text="软件验收判断：首轮参数与假设不一致，先复查实现，不能据此否定公式。" if bias == 2.1 else "软件验收判断：修正偏差参数后，三个合成输入均满足本轮判据。", evidence_ids=[run["id"], discussion["id"], "body"], scope="仅限已知固定偏差的加性模型与本例三个合成输入，不代表真实测量性能。", meaning="演示如何保留失败记录、修正后的结果和判断依据。")
        detail = api("/api/question?id=" + qid)
        assert detail["status"] == decision, detail["status"]
        statuses.append({"run": run["id"], "before_judgment": status_before, "after_judgment": detail["status"]})
        runs.append({"id": run["id"], "path": run["path"], "code": code["path"], "summary": summary, "judgment": judgment["id"]})
    content = api("/api/content?id=C01")
    action("append_draft", content_id="C01", question_id=qid, revision=content["draft_revision"], text="本段为软件流程验收示例。在加性模型 y=x+b 且偏差准确已知的条件下，校正值为 y-b。首次实现核对使用了不同的校正参数，产生固定残差；保留该记录并修正参数后，三个合成输入满足预设判据。该数值核对仅说明本例实现与模型一致，不代表真实设备或未知偏差下的性能。")
    action("generate_prompt", question_id=qid, template="P9", focus="只整理这一份软件验收示例，不能作为用户真实论文成果。")
    final = api("/api/question?id=" + qid)
    final_content = api("/api/content?id=C01")
    assert len(final["runs"]) >= 2
    assert len(final["judgments"]) >= 2
    assert "适用条件与局限" in final_content["draft"]
    report = {"purpose": "synthetic end-to-end software acceptance", "root": str(EXAMPLE_ROOT), "question": qid, "states": statuses, "runs": runs, "final_status": final["status"], "files": len(final["files"]), "draft_path": final_content["draft_path"], "paid_model_calls": 0}
    receipt.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
