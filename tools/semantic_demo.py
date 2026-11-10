"""Build an unlabelled, interleaved discussion with genuine numerical outputs.

Only writes disposable assets under .runtime/semantic-demo-assets.
No research records or model calls are made here.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / '.runtime' / 'semantic-demo-assets'

CODE = r'''"""Compare derivative approximations for one synthetic function and point."""
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
'''


def write(path, value):
    path.write_text(value, encoding='utf-8', newline='\n')


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    program = OUT / 'difference_compare.py'
    write(program, CODE)
    execution = subprocess.run([sys.executable, str(program)], cwd=OUT,
                               text=True, capture_output=True, encoding='utf-8', check=True)
    write(OUT/'stdout.log', execution.stdout)
    summary = json.loads((OUT/'comparison_summary.json').read_text(encoding='utf-8'))
    central = (OUT/'central.csv').read_text(encoding='utf-8')
    forward = (OUT/'forward.csv').read_text(encoding='utf-8')
    # Deliberately not interpretable as an outcome of either numerical question.
    (OUT/'legacy_trace.bin').write_bytes(bytes.fromhex('4c454741435900ff102030405060708090a0b0c0d0e0f0'))
    conversation = f'''用户：继续前面那个 sin 函数求导的例子。我们之前在 x=1 用左右两边的值做差，误差阶差不多是 2。我想把这轮复算继续放在原来的问题里，不要另起一份重复的讨论。

AI（演示讨论）：双侧中心差分是 [f(x+h)-f(x-h)]/(2h)。泰勒展开相减后，主误差项是 h²f''' + "'''" + f'''(x)/6。对 sin(x) 在 x=1，二阶主项不为零。本轮仍用 h=0.2、0.1、0.05、0.025，预先约定三个观测阶都在 [1.98,2.02] 内才算与本算例的二阶预测相符。

用户：我还有个不同的问题。如果我只能测右边一个点，把左右差分换成 [sin(1+h)-sin(1)]/h，会不会仍然是二阶？这需要单独记下来，别覆盖刚才那个问题。

AI（演示讨论）：这个向前差分通常只有一阶。展开后是 f'(x)+h f''(x)/2+O(h²)，对当前位置 f''(1)=-sin(1) 不为零。我们另外检验“只能取右侧样本时误差是否降为一阶”。同样四个步长，预先约定三个观测阶在 [0.95,1.08] 内。本轮没有真实仪器测量，输入仍是合成函数。

用户：先把中心差分的推导留着。程序能不能共用一份，分别输出两个表？我不想为了两件事复制两份几乎一样的程序。

AI（演示讨论）：可以。下面的程序同时计算两种差分，central.csv 对应前面的双侧二阶问题，forward.csv 对应新提出的单侧一阶问题。代码是两个问题共用的实现。

```python difference_compare.py
{CODE}```

用户：我回头补一句，双侧问题继续沿用已有的导数近似那部分即可；刚才的单侧问题也是这一部分的延伸，不需要新建一个不相干的论文方向。

执行回传：以下两个表由本机实际运行上面的程序产生，不是 AI 的预测。

```csv central.csv
{central}```

用户：这是前面那个双侧公式的复算。下面这张才是只能向右取样的计算。

```csv forward.csv
{forward}```

AI（演示分析）：双侧的三个观测阶分别是 {', '.join(f'{v:.6f}' for v in summary['central']['orders'])}，与二阶预测一致；单侧分别是 {', '.join(f'{v:.6f}' for v in summary['forward']['orders'])}，与一阶预测一致。两个结论都只针对 sin(x)、x=1 和本次四个步长，不能推广成真实测量系统的性能结论。

用户：两个问题的程序和对应输出要分别找得到，共用代码也要能追溯。我还附上了 comparison_summary.json，它把这两个表的阶数都汇总了，应该同时关联这两个问题。

用户：另外夹带的 legacy_trace.bin 是以前收到的文件，我现在不知道它的来源、对象或参数。请先保留原件，它不能拿来证明这两个差分结论。

备注：这是为验收语义归档而编写的交错讨论，没有调用外部 AI 生成对话；程序和数值结果实际执行过。两个判断都先作为候选分析保存，尚未由本人签收。
'''
    write(OUT/'无标签多问题讨论.md', conversation)
    manifest = {'executed_at': datetime.now(timezone.utc).isoformat(),
                'command': [sys.executable, str(program)], 'exit_code': execution.returncode,
                'code_sha256': digest(program),
                'outputs': {name: digest(OUT/name) for name in ['central.csv','forward.csv','comparison_summary.json','stdout.log']},
                'provenance': 'Locally executed synthetic example; no external AI service called.'}
    write(OUT/'execution_manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2)+'\n')
    expected = {'purpose': 'Independent evaluation ground truth, do not include this file in the model prompt.',
                'question_count': 2,
                'reuse': {'existing_question_id': 'C02-Q01', 'meaning': 'sin derivative at x=1, central difference order 2'},
                'new': {'meaning': 'sin derivative at x=1, forward difference order 1', 'content_id': 'C02', 'framework_id': 'F03'},
                'materials': {'difference_compare.py': ['central','forward'], 'central.csv': ['central'],
                              'forward.csv': ['forward'], 'comparison_summary.json': ['central','forward'],
                              'legacy_trace.bin': []},
                'candidate_only': True, 'results': summary,
                'uploads': ['comparison_summary.json','legacy_trace.bin']}
    write(OUT/'expected_mapping.json', json.dumps(expected, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps({'discussion': str(OUT/'无标签多问题讨论.md'), 'uploads': expected['uploads'],
                      'orders': summary, 'code_sha256': digest(program)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
