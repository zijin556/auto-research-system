"""Add reproducible, inline result figures to the existing isolated mini-paper."""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import quote

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
from nb import client

ROOT = APP / '示例研究'
URL = 'http://127.0.0.1:8788'
RECEIPT = ROOT / '04_交接包/小论文示例/图形补充记录.json'


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def action(name, **data):
    return client(URL, '/api/action', {'action': name, **data})


def main():
    if RECEIPT.exists():
        previous = json.loads(RECEIPT.read_text(encoding='utf-8'))
        for path, expected in previous['files'].items():
            if digest(ROOT / path) != expected:
                raise RuntimeError('Previously generated figure evidence changed: '+path)
        print(json.dumps({'already_complete': True, **previous}, ensure_ascii=False, indent=2))
        return
    health = client(URL, '/api/health')
    if health.get('app') != 'research-file-system' or Path(health['root']).resolve() != ROOT.resolve():
        raise RuntimeError('Expected the isolated example service on port 8788.')
    original = json.loads((RECEIPT.parent / '完成记录.json').read_text(encoding='utf-8'))
    for path, expected in original['evidence_hashes'].items():
        if digest(ROOT/path) != expected:
            raise RuntimeError('Original evidence changed: '+path)
    q = client(URL, '/api/question?id='+original['question_id'])
    body = q['body']
    if '## 4. 结果与分析' not in body or '运行与代码采用同一证据链：' not in body:
        raise RuntimeError('The article structure changed; inspect before inserting figures.')
    runs = {r['id']: r for r in q['runs']}
    sources = {}
    for case, run_id in [('ideal', 'R01'), ('harmonic', 'R02')]:
        directory = (ROOT / runs[run_id]['path']).parent
        for filename in ['samples.csv', 'summary.json']:
            source = directory / filename
            sources[case+'_'+filename] = source
    code = action('add_code', question_id=q['id'], filename='plot_three_step_results.py',
                  text=(APP/'tools/plot_three_step_results.py').read_text(encoding='utf-8'),
                  purpose='读取本题两轮已保存结果，生成论文中的幅值/误差图和数值实现核对图；不重新模拟、不新增测量结论。')
    plan = action('save_plan', question_id=q['id'], title='既有结果的绘图与来源核对', method='theory',
                  target='将既有两轮720相位结果如实绘图并嵌入正文。',
                  criterion='输入哈希与原运行一致；每种模型720行；最大误差与原汇总一致；输出2张PNG与2份PDF，正文保留数据、代码和运行链接。',
                  inputs='原理想模型与二次谐波模型的 samples.csv / summary.json；Python + Matplotlib。',
                  expected='幅值分别为2及1.8—2.2，谐波相对误差约±10%；数值残差单独展示，不解释为测量精度。',
                  source_ids=['R01', 'R02'])
    run = action('create_run', question_id=q['id'], title='既有结果绘图：幅值偏差与数值核对', plan_id=plan['id'],
                 target_step='式(3)、式(5)、式(6)与结果段落的图形化解释。', code_paths=[code['path']],
                 mapping='图1a：amplitude 与式(5)预测；图1b：100*(amplitude-target_amplitude)/target_amplitude。\n图2a：complex_analytic_residual；图2b：independent_amplitude_difference。两图的统计均使用全部720行。')
    output = (ROOT / run['path']).parent
    inputs = {}
    for name, source in sources.items():
        attached = action('attach_file', question_id=q['id'], run_id=run['id'], filename=name,
                          base64=base64.b64encode(source.read_bytes()).decode('ascii'))
        inputs[name] = ROOT / attached['path']
    code_snapshot = ROOT / run['code_snapshots'][0]['path']
    command = [sys.executable, str(code_snapshot),
               '--ideal-csv', str(inputs['ideal_samples.csv']), '--harmonic-csv', str(inputs['harmonic_samples.csv']),
               '--ideal-summary', str(inputs['ideal_summary.json']), '--harmonic-summary', str(inputs['harmonic_summary.json']),
               '--output-dir', str(output)]
    env = {**os.environ, 'PYTHONIOENCODING': 'utf-8', 'MPLCONFIGDIR': str(APP/'.runtime/mplconfig')}
    process = subprocess.run(command, capture_output=True, encoding='utf-8', env=env, timeout=90)
    (output/'stdout.log').write_text(process.stdout, encoding='utf-8')
    (output/'stderr.log').write_text(process.stderr, encoding='utf-8')
    execution = {'command': command, 'exit_code': process.returncode, 'python': sys.version,
                 'code_sha256': digest(code_snapshot), 'source_runs': ['R01', 'R02'],
                 'input_sources': {name: {'source': str(source.relative_to(ROOT)).replace('\\','/'),
                                         'sha256': digest(source), 'snapshot': name} for name, source in sources.items()}}
    (output/'绘图执行记录.json').write_text(json.dumps(execution, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    fresh = client(URL, '/api/question?id='+q['id'])
    current = next(r for r in fresh['runs'] if r['id'] == run['id'])
    action('update_run', question_id=q['id'], id=run['id'], revision=current['revision'],
           outcome='pass' if process.returncode == 0 else 'error', command=subprocess.list2cmdline(command),
           actual='已从既有720相位数据生成2张PNG、2份矢量PDF，源数据未改动。' if process.returncode == 0 else process.stderr,
           analysis='本次仅完成结果可视化和来源检查，不新增物理实验、理论证明或人工结论。')
    if process.returncode:
        raise RuntimeError('Figure generation failed; logs preserved in '+str(output))
    qdir = (ROOT/q['body_path']).parent

    def link(path):
        return quote(os.path.relpath(path, qdir).replace('\\','/'), safe='/')

    common = f"[绘图程序]({link(code_snapshot)}) · [绘图记录]({link(ROOT/run['path'])})"
    fig1 = f"""
![图1 三步相移的幅值恢复及有符号相对误差]({link(output/'amplitude_and_bias.png')})

**图1｜理想恢复与谐波偏差。** (a) 蓝线为纯基频的数值恢复，目标幅值为2；橙色圆点为含二次谐波的数值结果，灰色虚线为式（5）的解析预测。(b) 有符号相对误差定义为 $100(\\hat b-b)/b$，谐波情形随相位出现约±10%的偏差。每种模型均计算720个相位，范围为 $[0,2\\pi)$；(a)中的橙色标记每20点显示一个，统计使用全部数据。强度为任意单位，无随机重复实验，因此不添加误差棒。

[原图]({link(output/'amplitude_and_bias.png')}) · [矢量 PDF]({link(output/'amplitude_and_bias.pdf')}) · [理想模型数据]({link(inputs['ideal_samples.csv'])}) · [谐波模型数据]({link(inputs['harmonic_samples.csv'])}) · {common}

图1直观显示，偏差随相位变化而非恒定偏移；它来自式（5）的谐波混叠项，不能把理想模型下的恢复结论直接推广到含谐波情形。

"""
    fig2 = f"""
![图2 复数解析核对及两条幅值计算路径的数值差异]({link(output/'numerical_consistency.png')})

**图2｜数值实现的一致性核对。** (a) 复数解调与解析表达式之间的绝对差异；(b) 复数幅值与独立实数表达式之间的绝对差异。两组数据均完整绘制720个相位，纵轴按 $10^{{-14}}$ 缩放，采用线性坐标以保留零值。这里展示的是指定模型和输入下的浮点计算差异，不是仪器噪声、不确定度或实测精度；它与图1中的谐波模型偏差具有不同含义。

[原图]({link(output/'numerical_consistency.png')}) · [矢量 PDF]({link(output/'numerical_consistency.pdf')}) · [数据与生成记录]({link(output/'figure_manifest.json')}) · {common}

"""
    updated = body.replace('两条幅值实现的最大差异', fig1+'两条幅值实现的最大差异', 1)
    updated = updated.replace('运行与代码采用同一证据链：', fig2+'运行与代码采用同一证据链：', 1)
    if updated == body or 'amplitude_and_bias.png' not in updated:
        raise RuntimeError('Figure insertion point missing; no manuscript change made.')
    action('save_body', question_id=q['id'], revision=q['body_revision'], text=updated)
    for source, expected in original['evidence_hashes'].items():
        if digest(ROOT/source) != expected:
            raise RuntimeError('Original evidence was modified.')
    files = [p for p in output.rglob('*') if p.is_file()]
    receipt = {'created_at': datetime.now(timezone.utc).isoformat(), 'question_id': q['id'],
               'run_id': run['id'], 'run_path': run['path'], 'body_path': q['body_path'],
               'previous_body_sha256': hashlib.sha256(body.encode('utf-8')).hexdigest(),
               'files': {str(p.relative_to(ROOT)).replace('\\','/'): digest(p) for p in files},
               'url': original['url']}
    RECEIPT.write_text(json.dumps(receipt, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(receipt, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
