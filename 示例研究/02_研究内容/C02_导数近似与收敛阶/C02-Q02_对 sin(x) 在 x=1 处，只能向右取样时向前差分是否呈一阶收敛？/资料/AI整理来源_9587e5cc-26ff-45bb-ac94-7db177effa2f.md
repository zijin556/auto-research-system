# 本轮 AI 整理来源

- [完整原始讨论](../../../../04_%E4%BA%A4%E6%8E%A5%E5%8C%85/AI%E6%95%B4%E7%90%86/9587e5cc-26ff-45bb-ac94-7db177effa2f/%E8%AE%A8%E8%AE%BA%E5%8E%9F%E6%96%87.md)
- [模型整理方案](../../../../04_%E4%BA%A4%E6%8E%A5%E5%8C%85/AI%E6%95%B4%E7%90%86/9587e5cc-26ff-45bb-ac94-7db177effa2f/%E6%A8%A1%E5%9E%8B%E6%95%B4%E7%90%86%E6%96%B9%E6%A1%88.json)

## 本题原文段落

### B003

用户：我还有个不同的问题。如果我只能测右边一个点，把左右差分换成 [sin(1+h)-sin(1)]/h，会不会仍然是二阶？这需要单独记下来，别覆盖刚才那个问题。



### B004

AI（演示讨论）：这个向前差分通常只有一阶。展开后是 f'(x)+h f''(x)/2+O(h²)，对当前位置 f''(1)=-sin(1) 不为零。我们另外检验“只能取右侧样本时误差是否降为一阶”。同样四个步长，预先约定三个观测阶在 [0.95,1.08] 内。本轮没有真实仪器测量，输入仍是合成函数。



### B005

用户：先把中心差分的推导留着。程序能不能共用一份，分别输出两个表？我不想为了两件事复制两份几乎一样的程序。



### B006

AI（演示讨论）：可以。下面的程序同时计算两种差分，central.csv 对应前面的双侧二阶问题，forward.csv 对应新提出的单侧一阶问题。代码是两个问题共用的实现。



### B007

```python difference_compare.py
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
```


### B009

用户：我回头补一句，双侧问题继续沿用已有的导数近似那部分即可；刚才的单侧问题也是这一部分的延伸，不需要新建一个不相干的论文方向。



### B010

执行回传：以下两个表由本机实际运行上面的程序产生，不是 AI 的预测。



### B013

用户：这是前面那个双侧公式的复算。下面这张才是只能向右取样的计算。



### B014

```csv forward.csv
method,h,estimate,exact,abs_error,observed_order
forward,0.2,0.4528405057966489,0.5403023058681398,0.08746180007149085,
forward,0.1,0.4973637525353891,0.5403023058681398,0.04293855333275065,1.0263794558088473
forward,0.05,0.5190448157224092,0.5403023058681398,0.02125749014573053,1.0143023162408986
forward,0.025,0.5297281866478754,0.5403023058681398,0.01057411922026441,1.0074337712044543
```


### B016

AI（演示分析）：双侧的三个观测阶分别是 1.997836, 1.999459, 1.999865，与二阶预测一致；单侧分别是 1.026379, 1.014302, 1.007434，与一阶预测一致。两个结论都只针对 sin(x)、x=1 和本次四个步长，不能推广成真实测量系统的性能结论。



### B017

用户：两个问题的程序和对应输出要分别找得到，共用代码也要能追溯。我还附上了 comparison_summary.json，它把这两个表的阶数都汇总了，应该同时关联这两个问题。



### B019

备注：这是为验收语义归档而编写的交错讨论，没有调用外部 AI 生成对话；程序和数值结果实际执行过。两个判断都先作为候选分析保存，尚未由本人签收。


## 本题材料的共同来源

- [difference_compare.py](../../../../04_%E4%BA%A4%E6%8E%A5%E5%8C%85/AI%E6%95%B4%E7%90%86/9587e5cc-26ff-45bb-ac94-7db177effa2f/%E5%8E%9F%E5%A7%8B%E6%9D%90%E6%96%99/M001/difference_compare.py) · SHA256 `1e8980c6a1df9bdd457881141f7354affb4c58b689710f384056d3c2a80e7554`
- [forward.csv](../../../../04_%E4%BA%A4%E6%8E%A5%E5%8C%85/AI%E6%95%B4%E7%90%86/9587e5cc-26ff-45bb-ac94-7db177effa2f/%E5%8E%9F%E5%A7%8B%E6%9D%90%E6%96%99/M003/forward.csv) · SHA256 `25b08fe393232321d2e651713f30c6b3cd93bd821f12f99688913e50cb42ea89`
- [comparison_summary.json](../../../../04_%E4%BA%A4%E6%8E%A5%E5%8C%85/AI%E6%95%B4%E7%90%86/9587e5cc-26ff-45bb-ac94-7db177effa2f/%E5%8E%9F%E5%A7%8B%E6%9D%90%E6%96%99/M004/comparison_summary.json) · SHA256 `5c49fae202ee6b8d4eacd247704900a474fbda32b5fb27d7e6f75aa35a07dacb`