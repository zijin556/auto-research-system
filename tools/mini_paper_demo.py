"""Create an isolated six-chapter notebook with one executed theory example.

The script only writes to 示例研究. It never calls a model or changes formal
research records. A receipt makes repeated completed invocations read-only.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
from notebook.store import Store  # noqa: E402

ROOT = APP / "示例研究"
RECEIPT = ROOT / "04_交接包" / "小论文示例" / "完成记录.json"
FRAMEWORK = "从理论边界到方法验证（六章示例）"
QUESTION = "三步相移能否准确恢复正弦调制幅值，二次谐波会怎样改变结论？"
CHAPTERS = [
    ("第1章 绪论", "从测量目标与现有方法的不足出发，提出总体方案、三个待解决问题和技术路线。背景与现状需要真实文献支撑；创新点须等第3—5章产生可核验证据后再总结。"),
    ("第2章 成像模型与解调理论", "统一符号、前向模型、可辨识条件与适用边界。每个基础命题先给出假设和推导，再用解析特例、独立实现与反例检查。下方三步相移小论文是一个已执行的合成算例。"),
    ("第3章 非理想照明下的解调方法", "待论证方向：谐波、相移误差等非理想条件是否导致现有方法失效，能否提出有条件改善的估计方法？先选一个明确失效条件，再冻结基线与判据。这里尚无方法创新或性能结论。"),
    ("第4章 采样效率与重建稳定性", "待论证方向：减少采集次数时，哪些可辨识条件与稳定性约束必须保留？将采样数、误差和耗时分开比较；采用相同数据、相同预算的基线与消融。这里尚无方法创新或性能结论。"),
    ("第5章 系统实现与独立验证", "待论证方向：方法能否在独立样品、不同参数与真实仪器约束下工作？区分开发数据、标定数据和独立验证数据，并记录失败样本。真实实验尚未进行。"),
    ("第6章 总结与展望", "依据最终采用框架汇总已成立的贡献、适用条件、局限和未解决问题。只引用前五章已有证据，不在总结中新增未论证的结论。"),
]

CODE = r'''"""A deterministic synthetic check of three-step phase demodulation.

Usage: python three_step_demodulation.py --case ideal|harmonic --output-dir DIR
No real instrument data are used. Do not overwrite a previous run directory.
"""
import argparse
import cmath
import csv
import json
import math
from pathlib import Path


def synthesize(phi, a, b, c):
    return [a + b * math.cos(phi + theta)
            + c * math.cos(2 * phi + 2 * theta)
            for theta in [0, 2 * math.pi / 3, 4 * math.pi / 3]]


def demodulate(intensities):
    return (2 / 3) * sum(value * cmath.exp(-1j * 2 * math.pi * k / 3)
                         for k, value in enumerate(intensities))


def amplitude_from_differences(intensities):
    # Independent real-valued algebraic expression, no complex Fourier sum.
    first, second, third = intensities
    return math.sqrt((2 / 9) * ((first - second) ** 2
                              + (second - third) ** 2
                              + (third - first) ** 2))


def run(case, output):
    output.mkdir(parents=True, exist_ok=True)
    if any((output / name).exists() for name in ['samples.csv', 'summary.json']):
        raise ValueError('Choose a fresh output directory; previous outputs are immutable.')
    a, b, c, count, tolerance = 100.0, 2.0, (0.0 if case == 'ideal' else 0.2), 720, 1e-10
    rows = []
    for index in range(count):
        phi = 2 * math.pi * index / count
        intensities = synthesize(phi, a, b, c)
        recovered = demodulate(intensities)
        analytic = b * cmath.exp(1j * phi) + c * cmath.exp(-2j * phi)
        independent = amplitude_from_differences(intensities)
        rows.append({'index': index, 'phi_rad': phi, 'I0': intensities[0],
                     'I1': intensities[1], 'I2': intensities[2], 'amplitude': abs(recovered),
                     'target_amplitude': b, 'amplitude_error': abs(recovered) - b,
                     'complex_analytic_residual': abs(recovered - analytic),
                     'independent_amplitude_difference': abs(abs(recovered) - independent)})
    with (output / 'samples.csv').open('w', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]), lineterminator='\n')
        writer.writeheader()
        writer.writerows(rows)
    error = max(abs(row['amplitude_error']) for row in rows)
    analytic_error = max(row['complex_analytic_residual'] for row in rows)
    independent_error = max(row['independent_amplitude_difference'] for row in rows)
    bounds = [min(row['amplitude'] for row in rows), max(row['amplitude'] for row in rows)]
    # Frozen before execution: exact-model recovery and explicit boundary check.
    passed = (error < tolerance if case == 'ideal' else
              error > 0.19 and abs(bounds[0] - 1.8) < tolerance and abs(bounds[1] - 2.2) < tolerance)
    passed = passed and analytic_error < tolerance and independent_error < tolerance
    summary = {'case': case, 'a': a, 'b': b, 'c': c, 'phase_samples': count,
               'tolerance': tolerance, 'amplitude_min': bounds[0], 'amplitude_max': bounds[1],
               'max_abs_amplitude_error': error, 'max_relative_amplitude_error': error / b,
               'max_complex_analytic_residual': analytic_error,
               'max_independent_amplitude_difference': independent_error,
               'criterion_met': passed,
               'scope': 'Synthetic exact three-sample model; no noise, phase error or instrument data.'}
    (output / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(summary, indent=2))
    return 0 if passed else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--case', choices=['ideal', 'harmonic'], required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(run(args.case, args.output_dir))
'''

DERIVATION = r"""# 本次推导说明

这是本次由 Codex 代理形成的推导记录，不是对过去多模型对话的复原。下面的理论与数值实验使用人为定义的强度模型，没有引用真实仪器数据，也没有进行新的外部模型调用。

研究命题：在三次相移严格为 0、2π/3、4π/3，且强度只含直流与基频正弦项时，三步解调恢复的调制幅值是否等于真实基频幅值 b？加入二次谐波后该命题是否仍成立？这里的 b 不等同于归一化调制度 b/a。

定义 θ_k=2πk/3，I_k=a+b cos(φ+θ_k)，k=0,1,2。规定 b>0，a≥b，三次采集的 a、b、φ 不变，且无噪声、饱和、量化与相移误差。

令 Z=(2/3)Σ I_k exp(-iθ_k)。对三次单位根求和，Σ exp(-iθ_k)=Σ exp(-2iθ_k)=0，因此直流项与负频项消失，Z=b exp(iφ)。所以 |Z|=b，arg Z=φ mod 2π。这是给定模型下的代数恒等式。

将模型改为 I_k=a+b cos(φ+θ_k)+c cos(2φ+2θ_k)，可得额外项 c exp(-2iφ)，即 Z=b exp(iφ)+c exp(-2iφ)。从而 |Z|²=b²+c²+2bc cos(3φ)。当 b=2、c=0.2 时，幅值范围为 [1.8,2.2]，偏差最高为真实 b 的 10%。这不否定前一个条件命题，而是否定去掉“仅含基频”条件后的推广。

独立核对采用全实数表达式 b_hat=sqrt(2/9*((I0-I1)^2+(I1-I2)^2+(I2-I0)^2))，与复数解调实现分开计算。它核对计算路径的一致性，不能替代真实数据验证。

预先冻结的测试为 a=100、b=2，φ_j=2πj/720，j=0,…,719。理想模型 c=0 要求最大幅值误差小于 1e-10；加入 c=0.2 后要求出现大于 0.19 的绝对幅值误差、幅值上下界与 [1.8,2.2] 相差小于 1e-10。两种模型都要求复数解析残差、独立实数表达式差异小于 1e-10。

未解决问题：真实 DMD 照明的谐波分量是否符合这个简化模型、相移误差与空间变化如何影响可辨识性，需要另立小问题并引入真实条件。
"""


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def ensure_record(store: Store, collection: str, title: str, action: str, data: dict):
    matches = [value for value in store.state()[collection] if value['title'] == title]
    if len(matches) > 1:
        raise RuntimeError(f'Found duplicate example records: {title}')
    return matches[0] if matches else store.perform(action, {'title': title, **data})


def run_case(store, question, code, plan, title, case):
    previous = [run for run in store.detail(question['id'])['runs'] if run['title'] == title]
    if previous:
        run = previous[0]
        output = ROOT / Path(run['path']).parent
        if run['outcome'] != 'pass' or not (output / '执行清单.json').exists():
            raise RuntimeError('An unfinished example run exists. Inspect it before retrying.')
        return run, json.loads((output / 'summary.json').read_text(encoding='utf-8'))
    run = store.perform('create_run', {
        'question_id': question['id'], 'title': title, 'plan_id': plan['id'],
        'target_step': '公式（2）的基频恢复' if case == 'ideal' else '公式（5）的二次谐波混叠与反例',
        'code_paths': [code['path']],
        'mapping': '| 推导 | 实现 |\n| --- | --- |\n| 强度模型 | synthesize |\n| 复数解调与幅值 | demodulate |\n| 独立全实数核对 | amplitude_from_differences |\n| 解析混叠表达式 | run 中 analytic |',
        'parameters': f'Python {sys.version.split()[0]}；a=100，b=2，c={0 if case == "ideal" else 0.2}；720个等间隔相位；双精度。',
    })
    output = ROOT / Path(run['path']).parent
    snapshot = ROOT / run['code_snapshots'][0]['path']
    command = [sys.executable, str(snapshot), '--case', case, '--output-dir', str(output)]
    execution = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', timeout=30)
    (output / 'stdout.log').write_text(execution.stdout, encoding='utf-8')
    (output / 'stderr.log').write_text(execution.stderr, encoding='utf-8')
    if execution.returncode:
        raise RuntimeError(f'Example computation failed: {execution.returncode}; see {output}')
    summary = json.loads((output / 'summary.json').read_text(encoding='utf-8'))
    assert summary['criterion_met']
    manifest = {
        'created_at': datetime.now(timezone.utc).isoformat(), 'command': command,
        'exit_code': execution.returncode, 'python': sys.version,
        'code_snapshot': run['code_snapshots'][0], 'plan_snapshot': run['plan_snapshot'],
        'plan_sha256': digest(ROOT / run['plan_snapshot']),
        'files': {name: digest(output / name) for name in ['samples.csv', 'summary.json', 'stdout.log', 'stderr.log']},
        'provenance': 'Locally executed synthetic numerical example. No external model call. No real instrument data.',
    }
    (output / '执行清单.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    current = next(value for value in store.detail(question['id'])['runs'] if value['id'] == run['id'])
    actual = (f"720 个相位；恢复幅值范围 [{summary['amplitude_min']:.12g}, {summary['amplitude_max']:.12g}]；"
              f"最大绝对幅值误差 {summary['max_abs_amplitude_error']:.5g}；"
              f"最大解析复数残差 {summary['max_complex_analytic_residual']:.5g}。")
    run = store.perform('update_run', {
        'question_id': question['id'], 'id': run['id'], 'revision': current['revision'],
        'outcome': 'pass', 'actual': actual,
        'command': subprocess.list2cmdline(command),
        'analysis': '满足预先冻结的数值核对判据。' + ('仅支持理想模型下的实现核对。' if case == 'ideal' else '反例被成功复现；运行达标不表示无谐波模型适用于有谐波情形。'),
    })
    return run, summary


def main():
    if RECEIPT.exists():
        receipt = json.loads(RECEIPT.read_text(encoding='utf-8'))
        for relative, expected in receipt['evidence_hashes'].items():
            if digest(ROOT / relative) != expected:
                raise RuntimeError(f'Existing example evidence changed: {relative}')
        print(json.dumps({'already_complete': True, **receipt}, ensure_ascii=False, indent=2))
        return
    store = Store(ROOT)
    chapters = [ensure_record(store, 'contents', title, 'save_content', {'objective': objective})
                for title, objective in CHAPTERS]
    framework = ensure_record(store, 'frameworks', FRAMEWORK, 'save_framework', {
        'status': 'candidate', 'content_ids': [chapter['id'] for chapter in chapters],
        'mainline': '一个六章论文组织示例：先界定解调理论的适用条件，再讨论非理想方法、采样稳定性及独立验证。只有第2章的小问题已运行；其余章节是待讨论的研究结构。',
        'chapters': '\n'.join(f'{index + 1}. {title.removeprefix("第" + str(index + 1) + "章 ")}' for index, (title, _) in enumerate(CHAPTERS)),
        'risks': '这是软件与论证流程的演示框架，不是已经通过审查的博士论文选题。第3—5章尚需文献定位、创新性审查、可用数据与工作量论证。',
    })
    question = ensure_record(store, 'questions', QUESTION, 'create_question', {
        'content_id': chapters[1]['id'], 'question': QUESTION,
        'why': '给后续非理想照明与解调方法建立可审查的基础命题，并明确什么时候不能直接使用理想公式。',
        'criterion': '理想模型最大幅值误差 < 1e-10；谐波模型复现 [1.8,2.2] 范围与 >0.19 的绝对误差；两种情形解析复数残差及独立实数核对差异均 < 1e-10。只验算指定合成模型。',
        'outline': '定义假设 → 三次单位根恒等式 → 基频恢复 → 二次谐波反例 → 冻结判据并执行 → 有条件结论。',
    })
    detail = store.detail(question['id'])
    discussions = [item for item in detail['discussions'] if item['title'] == '本次推导说明：条件命题与谐波反例']
    discussion = discussions[0] if discussions else store.perform('save_discussion', {
        'question_id': question['id'], 'ai': 'Codex（本次示例推导）',
        'title': '本次推导说明：条件命题与谐波反例', 'raw': DERIVATION,
        'summary': '条件命题在纯基频模型下可代数证明。加入二次谐波后出现混叠项；必须保留原假设。',
        'unresolved': '真实照明谐波、相移误差和实际测量尚未检查；需用户审查是否值得扩展。',
    })
    code_files = [item for item in detail['files'] if item['name'] == 'three_step_demodulation.py']
    code = code_files[0] if code_files else store.perform('add_code', {
        'question_id': question['id'], 'filename': 'three_step_demodulation.py', 'text': CODE,
        'purpose': '以复数和全实数两条计算路径验算三步相移，输出纯基频与二次谐波反例的逐点数据与汇总。',
    })
    if (ROOT / code['path']).read_text(encoding='utf-8') != CODE:
        raise RuntimeError('Existing example code differs; refusing to overwrite it.')
    plans = [item for item in store.detail(question['id'])['plans'] if item['title'] == '三步相移条件命题：预先冻结的数值核对']
    plan = plans[0] if plans else store.perform('save_plan', {
        'question_id': question['id'], 'title': '三步相移条件命题：预先冻结的数值核对',
        'method': 'simulation', 'target': '检查推导的实现一致性及一个明确反例，不以数值实验替代代数证明。',
        'criterion': question['criterion'], 'inputs': 'a=100，b=2，c∈{0,0.2}；φ_j=2πj/720，j=0,…,719；无噪声、相移误差、量化与饱和。',
        'expected': 'c=0 时恢复幅值恒为2；c=0.2 时为 sqrt(4.04+0.8*cos(3φ))，幅值范围[1.8,2.2]。',
        'source_ids': [discussion['id']],
    })
    ideal_run, ideal = run_case(store, question, code, plan, '纯基频模型：恢复恒定幅值', 'ideal')
    harmonic_run, harmonic = run_case(store, question, code, plan, '二次谐波反例：幅值出现周期偏差', 'harmonic')
    detail = store.detail(question['id'])
    base = (ROOT / detail['body_path']).parent

    def link(path, label):
        relative = os.path.relpath(ROOT / path, base).replace('\\', '/')
        return f'[{label}]({quote(relative, safe="/")})'

    ideal_dir = Path(ideal_run['path']).parent.as_posix()
    harmonic_dir = Path(harmonic_run['path']).parent.as_posix()
    body = r"""## 摘要

本文研究一个范围明确的基础问题：三步相移能否从理想正弦强度中准确恢复基频幅值，以及该结论对二次谐波是否稳健。首先利用三次单位根给出闭式推导；随后构造二次谐波反例，最后用两个独立计算表达式进行数值核对。结果表明：纯基频假设下的恢复是代数恒等式；当基频幅值为2、二次谐波幅值为0.2时，恢复幅值在1.8到2.2之间变化。数值检查支持这些指定模型的实现一致性，不能代表真实仪器性能。

## 1. 问题与模型假设

对同一个采样位置进行三次采集。假设三次采集期间直流强度 $a$、基频幅值 $b$ 和初始相位 $\phi$ 不变；已知相移严格为 $\theta_k=2\pi k/3$。规定 $b>0$、$a\geq b$，且暂不考虑噪声、相移误差、饱和与量化。

$$
I_k=a+b\cos(\phi+\theta_k),\qquad k=0,1,2.\tag{1}
$$

这里的 $b$ 是强度基频的调制幅值，不等同于归一化调制度 $m=b/a$，也不是表面高度。把该解调量进一步换算为形貌，需要另行建立成像、标定与高度估计关系。

## 2. 理论推导

### 2.1 三步解调的条件命题

令 $Z$ 为三次采样的复数解调量。将式（1）的余弦展开为正、负频指数项，得到

$$
\begin{aligned}
Z&=\frac{2}{3}\sum_{k=0}^{2}I_k e^{-i\theta_k}\\
&=\frac{2a}{3}\sum_{k=0}^{2}e^{-i\theta_k}
 +\frac{b e^{i\phi}}{3}\sum_{k=0}^{2}1
 +\frac{b e^{-i\phi}}{3}\sum_{k=0}^{2}e^{-2i\theta_k}.
\end{aligned}\tag{2}
$$

由三次单位根性质，两个非零频率求和均为0，而常数项求和为3。因此

$$
Z=b e^{i\phi},\qquad \widehat b=|Z|=b,\qquad
\widehat\phi=\arg Z=\phi\pmod{2\pi}.\tag{3}
$$

式（3）是在式（1）及上述假设下成立的精确代数结论。相位仍具有 $2\pi$ 周期歧义；当 $b=0$ 时，相位不可定义，所以该退化情形不属于本命题。

### 2.2 二次谐波构成的反例

现在只改变“纯基频”这一条假设，加入二次谐波：

$$
I_k=a+b\cos(\phi+\theta_k)+c\cos(2\phi+2\theta_k).\tag{4}
$$

对二次谐波的正、负频部分分别求和：正频部分含 $\sum e^{i\theta_k}=0$，负频部分含 $\sum e^{-3i\theta_k}=3$。因此额外分量没有全部抵消，而是进入同一解调量：

$$
Z=b e^{i\phi}+c e^{-2i\phi},\qquad
|Z|^2=b^2+c^2+2bc\cos(3\phi).\tag{5}
$$

当 $b=2$、$c=0.2$ 时，式（5）给出 $\widehat b\in[1.8,2.2]$。所以“有二次谐波时仍能准确恢复基频幅值”的推广不成立。这个反例并不推翻式（3），而是说明式（3）的纯基频条件必须保留。

## 3. 数值核对方法

在运行前固定 $a=100$、$b=2$、$c\in\{0,0.2\}$，并使用 $\phi_j=2\pi j/720$（$j=0,\ldots,719$）。第一条计算路径按式（2）执行复数求和；第二条路径直接计算全实数表达式

$$
\widehat b_{\rm real}=\sqrt{\frac{2}{9}\left[(I_0-I_1)^2+(I_1-I_2)^2+(I_2-I_0)^2\right]}.\tag{6}
$$

两条路径分别实现，并与式（5）的复数解析表达式核对。判据在创建运行记录前冻结：理想模型最大幅值误差小于 $10^{-10}$；谐波反例须出现大于0.19的绝对幅值误差，且幅值边界与[1.8,2.2]相差小于 $10^{-10}$。两种情形的解析复数残差和两条幅值计算路径的差异均须小于 $10^{-10}$。

FROZEN_PLAN

## 4. 结果与分析

下表来自本机实际执行的Python程序，每个算例均覆盖720个相位。表中的“通过”表示符合本次预先给定的核对判据；谐波反例通过意味着确实发现了偏差。

| 算例 | 恢复幅值范围 | 最大绝对幅值误差 | 最大解析复数残差 | 结果数据 |
| --- | --- | --- | --- | --- |
RESULT_ROWS

两条幅值实现的最大差异分别为 INDEPENDENT_ERRORS。理想模型的误差位于双精度浮点计算的舍入量级；加入二次谐波后，幅值随相位变化，最大相对误差为 HARMONIC_RELATIVE。后一结果与式（5）的预测一致，说明仅检查程序是否运行成功，不能决定一个更宽泛的理论命题是否成立。

运行与代码采用同一证据链：IDEAL_RUN → IDEAL_CODE → IDEAL_DATA；HARMONIC_RUN → HARMONIC_CODE → HARMONIC_DATA。每个运行目录均保留冻结计划、实际代码快照、原始输出、汇总数据和文件哈希。

## 5. 有条件结论与后续问题

在严格三步等间隔相移、纯基频且参数不随采集变化的条件下，三步解调能够恢复基频幅值；这是式（2）到式（3）所给出的代数结果。加入二次谐波后，式（5）给出不能忽略的混叠项，因此不能直接沿用纯基频结论。数值结果检查了两个指定模型与程序实现之间的一致性。

这些结果尚未包含DMD像素离散、真实照明频谱、成像传递函数、探测噪声和相移标定误差，也没有验证形貌精度。下一步应将“真实系统的主要谐波项是什么”或“如何在有限采集次数下抑制该混叠”分别作为新的小问题，不把本算例自动扩展成系统性能结论。

**审查状态：** 这是本次自动形成的条件结论草稿，等待用户审查；系统没有替用户签收科学结论。

## 原始材料

- DERIVATION_LINK
- SOURCE_CODE
- IDEAL_MANIFEST
- HARMONIC_MANIFEST
"""
    replacements = {
        'FROZEN_PLAN': link(plan['path'], '运行前冻结的验证计划') + '；' + link(code['path'], '完整验证程序') + '。',
        'RESULT_ROWS': '\n'.join([
            f"| 纯基频，c=0 | [{ideal['amplitude_min']:.12g}, {ideal['amplitude_max']:.12g}] | {ideal['max_abs_amplitude_error']:.4g} | {ideal['max_complex_analytic_residual']:.4g} | {link(ideal_dir + '/summary.json', '汇总')} · {link(ideal_dir + '/samples.csv', '逐点数据')} |",
            f"| 二次谐波，c=0.2 | [{harmonic['amplitude_min']:.12g}, {harmonic['amplitude_max']:.12g}] | {harmonic['max_abs_amplitude_error']:.4g} | {harmonic['max_complex_analytic_residual']:.4g} | {link(harmonic_dir + '/summary.json', '汇总')} · {link(harmonic_dir + '/samples.csv', '逐点数据')} |",
        ]),
        'INDEPENDENT_ERRORS': f"{ideal['max_independent_amplitude_difference']:.4g}与{harmonic['max_independent_amplitude_difference']:.4g}",
        'HARMONIC_RELATIVE': f"{100 * harmonic['max_relative_amplitude_error']:.6g}%",
        'IDEAL_RUN': link(ideal_run['path'], '理想模型运行记录'),
        'IDEAL_CODE': link(ideal_run['code_snapshots'][0]['path'], '本次代码快照'),
        'IDEAL_DATA': link(ideal_dir + '/samples.csv', '理想模型逐点结果'),
        'HARMONIC_RUN': link(harmonic_run['path'], '谐波反例运行记录'),
        'HARMONIC_CODE': link(harmonic_run['code_snapshots'][0]['path'], '本次代码快照'),
        'HARMONIC_DATA': link(harmonic_dir + '/samples.csv', '谐波反例逐点结果'),
        'DERIVATION_LINK': link(discussion['path'], '本次推导说明（原始记录）'),
        'SOURCE_CODE': link(code['path'], '验证代码：three_step_demodulation.py'),
        'IDEAL_MANIFEST': link(ideal_dir + '/执行清单.json', '理想模型：实际命令、退出码与文件哈希'),
        'HARMONIC_MANIFEST': link(harmonic_dir + '/执行清单.json', '谐波反例：实际命令、退出码与文件哈希'),
    }
    for key, value in replacements.items():
        body = body.replace(key, value)
    store.perform('save_body', {'question_id': question['id'], 'revision': detail['body_revision'], 'text': body})
    for chapter, (_, objective) in zip(chapters, CHAPTERS):
        current = store.content_detail(chapter['id'])
        if current['draft'].strip() == f"# {chapter['title']} · 论文草稿":
            text = f"# {chapter['title']}\n\n{objective}\n\n"
            if chapter['id'] == chapters[1]['id']:
                relative = os.path.relpath(ROOT / detail['body_path'], (ROOT / current['draft_path']).parent).replace('\\', '/')
                text += f"## 已形成的小问题文稿\n\n[{QUESTION}]({quote(relative, safe='/')})\n\n包含模型假设、完整推导、可运行代码、两轮数值结果和反例边界；待用户审查。\n"
            store.perform('save_draft', {'content_id': chapter['id'], 'revision': current['draft_revision'], 'text': text})
    qfinal = store.detail(question['id'])
    assert len(qfinal['runs']) == 2 and len(qfinal['judgments']) == 0
    evidence = [code['path'], discussion['path'], plan['path']]
    for run in qfinal['runs']:
        evidence.extend([item['path'] for item in run['code_snapshots']])
        evidence.extend([item['path'] for item in run['result_files']])
    receipt = {'created_at': datetime.now(timezone.utc).isoformat(), 'framework_id': framework['id'],
               'chapter_ids': [chapter['id'] for chapter in chapters], 'question_id': question['id'],
               'body_path': qfinal['body_path'], 'status': qfinal['status'],
               'url': f"http://127.0.0.1:8788/#question/{question['id']}/body/{framework['id']}",
               'evidence_hashes': {relative: digest(ROOT / relative) for relative in sorted(set(evidence))},
               'notes': 'Six-chapter candidate example; only chapter 2 has executed evidence. No human judgment was written.'}
    RECEIPT.parent.mkdir(parents=True, exist_ok=True)
    RECEIPT.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(receipt, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
