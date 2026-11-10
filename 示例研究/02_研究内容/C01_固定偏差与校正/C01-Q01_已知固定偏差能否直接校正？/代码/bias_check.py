# C01-Q01: synthetic acceptance example for an additive fixed-bias correction.
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
