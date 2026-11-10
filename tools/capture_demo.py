"""Build and run the small, reproducible discussion-capture demonstration.

Only writes to the explicitly chosen demo asset folder. The web application does
not execute pasted code; this preparation script runs the known example below.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


FRAMEWORK = "数值方法的误差与验证"
CONTENT = "导数近似与收敛阶"
QUESTION = "对 sin(x) 在 x=1 处，三点中心差分估计 cos(x) 是否呈二阶收敛？"
ROUTE = f"论文框架：{FRAMEWORK}\n研究内容：{CONTENT}\n研究问题：{QUESTION}\n"

CODE = '''"""Check central-difference convergence on a fixed smooth function.

This synthetic numerical example is not a validation of a physical experiment.
Run: python central_difference.py --output-dir <folder>
"""
import argparse
import csv
import json
import math
from pathlib import Path


def run(output_dir):
    x = 1.0
    expected = math.cos(x)
    rows = []
    previous_error = None
    for h in [0.2, 0.1, 0.05, 0.025]:
        estimate = (math.sin(x + h) - math.sin(x - h)) / (2.0 * h)
        error = abs(estimate - expected)
        order = None if previous_error is None else math.log2(previous_error / error)
        rows.append({"h": h, "estimate": estimate, "exact": expected,
                     "abs_error": error, "observed_order": order})
        previous_error = error
    output_dir.mkdir(parents=True, exist_ok=True)
    with (output_dir / "convergence.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    orders = [row["observed_order"] for row in rows[1:]]
    passed = all(1.98 <= value <= 2.02 for value in orders)
    summary = {"function": "sin(x)", "x": x, "exact_derivative": expected,
               "h_values": [row["h"] for row in rows],
               "criterion": "all three observed orders are in [1.98, 2.02]",
               "observed_orders": orders, "criterion_met": passed,
               "scope": "One synthetic function and point, with these four step sizes only."}
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\\n", encoding="utf-8")
    print("Central difference of sin(x), x=1.0")
    print("Reference derivative: {:.15f}".format(expected))
    for row in rows:
        order_text = "--" if row["observed_order"] is None else "{:.6f}".format(row["observed_order"])
        print("h={:.3f}  abs_error={:.12g}  observed_order={}".format(row["h"], row["abs_error"], order_text))
    print("Criterion met: {}".format(passed))
    return 0 if passed else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parent)
    arguments = parser.parse_args()
    raise SystemExit(run(arguments.output_dir))
'''


def build(output: Path) -> dict:
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    program = output / "central_difference.py"
    program.write_text(CODE, encoding="utf-8", newline="\n")
    command = [sys.executable, str(program), "--output-dir", str(output)]
    completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", check=True)
    (output / "stdout.log").write_text(completed.stdout, encoding="utf-8")
    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    with (output / "convergence.csv").open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 4
    assert summary["criterion_met"] is True
    assert all(1.98 <= value <= 2.02 for value in summary["observed_orders"])
    assert all(float(left["abs_error"]) > float(right["abs_error"])
               for left, right in zip(rows, rows[1:]))
    # Independent identity: the central difference equals cos(x) * sin(h) / h.
    import math
    for row in rows:
        h = float(row["h"])
        analytic_error = abs(math.cos(1.0) * (math.sin(h) / h - 1.0))
        assert abs(float(row["abs_error"]) - analytic_error) < 1e-13

    provenance = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "command": command,
        "exit_code": completed.returncode,
        "code_sha256": hashlib.sha256(program.read_bytes()).hexdigest(),
        "files": {name: hashlib.sha256((output / name).read_bytes()).hexdigest()
                  for name in ["convergence.csv", "summary.json", "stdout.log"]},
        "provenance": "Locally executed software demonstration; no AI provider was called.",
    }
    (output / "execution_manifest.json").write_text(
        json.dumps(provenance, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    initial = ROUTE + r"""
【说明】这是一段为演示整理流程编写的示例讨论，没有调用 GPT、Gemini 或 Claude。以下数值任务使用合成函数，不代表真实科研数据。

用户：我想先验证这个具体问题。不要直接给最后结论，请解释为什么中心差分是二阶的，再给一个最小程序。

AI（示例）：设 f 在 x 附近足够光滑。将 f(x+h) 与 f(x-h) 的泰勒展开相减，得到

$$D_h f(x)=\frac{f(x+h)-f(x-h)}{2h}=f'(x)+\frac{h^2}{6}f'''(x)+O(h^4).$$

因此，截断误差的主项通常与 h² 成正比。对 f(x)=sin(x)，可以直接使用恒等式 D_h f(x)=cos(x)sin(h)/h；在 x=1 处，cos(1) 不为零，二阶主项不会消失。

用户：我理解了。那就固定 x=1，用 h=0.2、0.1、0.05、0.025，比较计算值与 cos(1)，并算相邻两次误差的比值。

AI（示例）：用 p=log₂(e_h/e_(h/2)) 估计收敛阶。本轮事先约定：三个 p 都在 1.98 到 2.02 之间，才算这个算例与二阶预测一致。先保存下面的程序，运行前还不能认为数值验证已经完成。

代码文件：central_difference.py
```python central_difference.py
""" + CODE + """```

下一步：执行程序，保留 convergence.csv、summary.json 和实际终端输出，再检查这些结果是否满足本轮判据。
适用范围：这里只检查一个光滑函数、一个位置和四个步长；过小步长会使舍入误差影响差分，不能用这个算例证明所有问题都具有二阶收敛。
"""
    csv_text = (output / "convergence.csv").read_text(encoding="utf-8")
    followup = ROUTE + """
【说明】这是上述软件演示的结果回传。下面的数据来自本机实际执行 central_difference.py；不是 AI 预测或编造的数据。

用户：程序已经执行完成，请把这次输出放回同一个问题下面。运行记录和验证结论要分开保留。

运行方式：python central_difference.py --output-dir .
结果文件：convergence.csv
```csv convergence.csv
""" + csv_text + """```

实际终端输出：
```text stdout.log
""" + completed.stdout + """```

AI（示例分析）：误差随步长减半约缩小为原来的四分之一，三个观测收敛阶都满足事先的 [1.98, 2.02] 判据。这支持本次算例与二阶理论一致。

结论：待本人确认——对 sin(x) 在 x=1 处，h=0.2、0.1、0.05、0.025 的中心差分输出符合二阶收敛预期。请保留程序、输出和上述限制，再由我确认阶段判断。
适用范围：这个候选结论只覆盖本次算例，不是对全部函数、全部步长或真实测量系统的验证。
"""
    judgment = """建议的人工阶段判断（此文件本身不改变问题状态）

判断：验证支持
结论：对 sin(x) 在 x=1 处，本轮四个步长的中心差分结果符合二阶收敛预测；三个观测收敛阶都位于事先约定的 [1.98, 2.02]。
依据：理论讨论、central_difference.py、convergence.csv、stdout.log、summary.json；执行版本见 execution_manifest.json 的 SHA256。
适用范围：单一合成函数、固定位置与本轮四个步长；不覆盖极小步长的舍入误差区间，也不代表真实实验系统性能。
对论文的意义：可作为数值方法误差阶的最小复现实例，说明实现和推导在该条件下相符。
"""
    (output / "01_首次讨论.md").write_text(initial, encoding="utf-8", newline="\n")
    (output / "02_结果回传.md").write_text(followup, encoding="utf-8", newline="\n")
    (output / "03_建议阶段判断.md").write_text(judgment, encoding="utf-8", newline="\n")
    (output / "完整讨论与结果.md").write_text(initial + "\n\n---\n\n" + followup, encoding="utf-8", newline="\n")
    report = {"output": str(output), "observed_orders": summary["observed_orders"],
              "criterion_met": summary["criterion_met"],
              "code_sha256": provenance["code_sha256"],
              "files": sorted(path.name for path in output.iterdir() if path.is_file())}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path,
                        default=Path(__file__).resolve().parents[1] / ".runtime" / "capture-demo-assets")
    args = parser.parse_args()
    build(args.output)
