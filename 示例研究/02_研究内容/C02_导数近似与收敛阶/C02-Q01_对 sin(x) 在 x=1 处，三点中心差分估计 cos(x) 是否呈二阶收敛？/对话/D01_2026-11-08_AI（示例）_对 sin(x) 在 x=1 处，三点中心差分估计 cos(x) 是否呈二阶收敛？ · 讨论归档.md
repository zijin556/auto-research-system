# 对 sin(x) 在 x=1 处，三点中心差分估计 cos(x) 是否呈二阶收敛？ · 讨论归档

<!-- research-record {"id":"D01","question_id":"C02-Q01","title":"对 sin(x) 在 x=1 处，三点中心差分估计 cos(x) 是否呈二阶收敛？ · 讨论归档","created_at":"2026-11-08T07:52:03.532138+00:00","parent_id":"","prompt_id":"","capture_type":"对话粘贴","kind":"discussion","schema":1} -->

<!-- field:ai -->
## AI

AI（示例）
<!-- /field:ai -->

<!-- field:raw -->
## 原始粘贴内容

论文框架：数值方法的误差与验证
研究内容：导数近似与收敛阶
研究问题：对 sin(x) 在 x=1 处，三点中心差分估计 cos(x) 是否呈二阶收敛？

【说明】这是一段为演示整理流程编写的示例讨论，没有调用 GPT、Gemini 或 Claude。以下数值任务使用合成函数，不代表真实科研数据。

用户：我想先验证这个具体问题。不要直接给最后结论，请解释为什么中心差分是二阶的，再给一个最小程序。

AI（示例）：设 f 在 x 附近足够光滑。将 f(x+h) 与 f(x-h) 的泰勒展开相减，得到

$$D_h f(x)=\frac{f(x+h)-f(x-h)}{2h}=f'(x)+\frac{h^2}{6}f'''(x)+O(h^4).$$

因此，截断误差的主项通常与 h² 成正比。对 f(x)=sin(x)，可以直接使用恒等式 D_h f(x)=cos(x)sin(h)/h；在 x=1 处，cos(1) 不为零，二阶主项不会消失。

用户：我理解了。那就固定 x=1，用 h=0.2、0.1、0.05、0.025，比较计算值与 cos(1)，并算相邻两次误差的比值。

AI（示例）：用 p=log₂(e_h/e_(h/2)) 估计收敛阶。本轮事先约定：三个 p 都在 1.98 到 2.02 之间，才算这个算例与二阶预测一致。先保存下面的程序，运行前还不能认为数值验证已经完成。

代码文件：central_difference.py
```python central_difference.py
"""Check central-difference convergence on a fixed smooth function.

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
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
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
```

下一步：执行程序，保留 convergence.csv、summary.json 和实际终端输出，再检查这些结果是否满足本轮判据。
适用范围：这里只检查一个光滑函数、一个位置和四个步长；过小步长会使舍入误差影响差分，不能用这个算例证明所有问题都具有二阶收敛。

<!-- /field:raw -->

<!-- field:summary -->
## 要点

原文适用范围：

这里只检查一个光滑函数、一个位置和四个步长；过小步长会使舍入误差影响差分，不能用这个算例证明所有问题都具有二阶收敛。
<!-- /field:summary -->

<!-- field:understanding -->
## 我的理解


<!-- /field:understanding -->

<!-- field:unresolved -->
## 遗留问题

本次归档未自动运行代码或确认研究结论。
<!-- /field:unresolved -->
