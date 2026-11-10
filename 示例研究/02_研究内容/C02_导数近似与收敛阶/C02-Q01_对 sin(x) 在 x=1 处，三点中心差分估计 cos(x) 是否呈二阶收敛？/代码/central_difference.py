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
