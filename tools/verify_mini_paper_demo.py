"""Verify actual data, local document links and repeat-safe demo construction."""
from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote

APP = Path(__file__).resolve().parents[1]
ROOT = APP / '示例研究'
receipt = json.loads((ROOT / '04_交接包/小论文示例/完成记录.json').read_text(encoding='utf-8'))
body_path = ROOT / receipt['body_path']
body = body_path.read_text(encoding='utf-8')
assert len(receipt['chapter_ids']) == 6
assert receipt['status'] == 'needs_judgment'
assert '待用户审查' not in body or '系统没有替用户签收科学结论' in body
assert not list((body_path.parent / '结论').glob('*.md'))

links = re.findall(r'\[[^\]]*\]\(([^)]+)\)', body)
for link in links:
    target = (body_path.parent / unquote(link)).resolve()
    assert target.is_relative_to(ROOT.resolve()) and target.is_file(), link
assert len(links) >= 15
assert '\\tag{6}' in body
assert 'RESULT_ROWS' not in body

data_checks = []
original_summaries = [ROOT / p for p in receipt['evidence_hashes'] if p.endswith('/summary.json')]
assert len(original_summaries) == 2
for original_summary in sorted(original_summaries):
    run_dir = original_summary.parent
    summary = json.loads((run_dir / 'summary.json').read_text(encoding='utf-8'))
    manifest = json.loads((run_dir / '执行清单.json').read_text(encoding='utf-8'))
    assert manifest['exit_code'] == 0
    assert summary['criterion_met'] is True
    for name, digest in manifest['files'].items():
        assert hashlib.sha256((run_dir / name).read_bytes()).hexdigest() == digest
    rows = list(csv.DictReader((run_dir / 'samples.csv').open(encoding='utf-8')))
    assert len(rows) == 720 and len({row['phi_rad'] for row in rows}) == 720
    # Exact hand-computable checkpoints independent of the loop implementation.
    if summary['case'] == 'ideal':
        expected_at_zero = [102, 99, 99]
        expected_amplitudes = [2, 2]
    else:
        expected_at_zero = [102.2, 98.9, 98.9]
        expected_amplitudes = [2.2, 1.8]
    for key, expected in zip(['I0', 'I1', 'I2'], expected_at_zero):
        assert abs(float(rows[0][key]) - expected) < 1e-12
    for index, expected in zip([0, 120], expected_amplitudes):
        assert abs(float(rows[index]['amplitude']) - expected) < 1e-12
    for row in rows:
        assert min(float(row[key]) for key in ['I0', 'I1', 'I2']) > 0
        assert all(math.isfinite(float(value)) for value in row.values())
    data_checks.append({key: summary[key] for key in ['case', 'amplitude_min', 'amplitude_max',
                                                    'max_abs_amplitude_error', 'max_complex_analytic_residual']})

for relative, expected in receipt['evidence_hashes'].items():
    assert hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() == expected

# A completed rerun must preserve every file byte, not just refrain from IDs.
def snapshot():
    return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in ROOT.rglob('*') if path.is_file()
            and not any(part in {'.runtime', '.history', '__pycache__'} for part in path.relative_to(ROOT).parts)}

before = snapshot()
env = {**os.environ, 'PYTHONIOENCODING': 'utf-8'}
execution = subprocess.run([sys.executable, str(APP / 'tools/mini_paper_demo.py')],
                           capture_output=True, encoding='utf-8', timeout=30, env=env)
assert execution.returncode == 0, execution.stderr
assert json.loads(execution.stdout)['already_complete'] is True
assert snapshot() == before
report = {'passed': True, 'chapters': 6, 'paper_equations': 6, 'verified_manuscript_links': len(links),
          'run_data': data_checks, 'verified_evidence_files': len(receipt['evidence_hashes']),
          'idempotent_rerun': True, 'no_human_judgment': True, 'url': receipt['url']}
(APP / '.runtime/mini-paper-acceptance.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
print(json.dumps(report, ensure_ascii=False, indent=2))
