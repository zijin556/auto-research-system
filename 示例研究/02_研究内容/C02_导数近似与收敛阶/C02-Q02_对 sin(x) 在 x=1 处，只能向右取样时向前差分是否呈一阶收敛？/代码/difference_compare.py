"""Compare derivative approximations for one synthetic function and point."""
import csv
import json
import math
from pathlib import Path

output = Path(__file__).resolve().parent
x = 1.0
reference = math.cos(x)
steps = [0.2, 0.1, 0.05, 0.025]
summary = {}
for method in ['central', 'forward']:
    rows = []
    previous_error = None
    for h in steps:
        if method == 'central':
            estimate = (math.sin(x+h)-math.sin(x-h))/(2*h)
        else:
            estimate = (math.sin(x+h)-math.sin(x))/h
        error = abs(estimate-reference)
        order = None if previous_error is None else math.log2(previous_error/error)
        rows.append({'method': method, 'h': h, 'estimate': estimate,
                     'exact': reference, 'abs_error': error, 'observed_order': order})
        previous_error = error
    with (output / (method+'.csv')).open('w', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]), lineterminator='\n')
        writer.writeheader()
        writer.writerows(rows)
    observed = [row['observed_order'] for row in rows[1:]]
    bounds = [1.98, 2.02] if method == 'central' else [0.95, 1.08]
    summary[method] = {'orders': observed, 'criterion': bounds,
                       'criterion_met': all(bounds[0] <= p <= bounds[1] for p in observed)}
    print(method, 'orders:', ', '.join(f'{p:.6f}' for p in observed))
(output/'comparison_summary.json').write_text(json.dumps(summary, indent=2)+'\n', encoding='utf-8')
assert all(item['criterion_met'] for item in summary.values())
