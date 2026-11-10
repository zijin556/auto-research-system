"""Run the reviewed demo program that the browser actually archived."""
import hashlib
import json
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
opener = build_opener(ProxyHandler({}))


def api(path, data=None):
    request = Request('http://127.0.0.1:8788' + path,
                      data=json.dumps(data, ensure_ascii=False).encode() if data else None,
                      headers={'Content-Type': 'application/json', 'X-Research-Notebook': '1'})
    with opener.open(request, timeout=15) as response:
        return json.load(response)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


state = api('/api/state')
question = next(q for q in state['questions'] if q['title'].startswith('对 sin(x) 在 x=1'))
detail = api('/api/question?id=' + question['id'])
code_record = next(f for f in detail['files'] if f['name'] == 'central_difference.py')
example_root = (ROOT / '示例研究').resolve()
code = (example_root / code_record['path']).resolve()
assert code.is_relative_to(example_root)
original = ROOT / '.runtime/capture-demo-assets/central_difference.py'
assert digest(code) == digest(original), 'Archived code differs from reviewed demo'
output = ROOT / '.runtime/capture-demo-live'
output.mkdir(exist_ok=True)
command = [sys.executable, str(code), '--output-dir', str(output)]
run = subprocess.run(command, cwd=code.parent, capture_output=True, text=True, encoding='utf-8', check=True)
(output / 'stdout.log').write_text(run.stdout, encoding='utf-8', newline='\n')
summary = json.loads((output / 'summary.json').read_text(encoding='utf-8'))
assert summary['criterion_met'] is True
manifest = {'created_at': datetime.now(timezone.utc).isoformat(), 'python': platform.python_version(),
            'command': command, 'exit_code': run.returncode, 'code_sha256': digest(code),
            'question_title': question['title'], 'code_path': code_record['path'],
            'files': {name: digest(output / name) for name in ['convergence.csv', 'summary.json', 'stdout.log']},
            'provenance': 'Executed the archived demonstration code after browser capture. Synthetic function only.'}
(output / 'execution_manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
for name in ['convergence.csv', 'summary.json', 'stdout.log']:
    assert (output / name).read_text(encoding='utf-8') == (ROOT / '.runtime/capture-demo-assets' / name).read_text(encoding='utf-8'), name
project = state['project']
api('/api/action', {'action': 'save_project', 'revision': project['revision'],
                   'title': '科研文稿整理示例',
                   'brief': '软件操作示例：包含固定偏差校正与中心差分收敛两个合成算例，不属于正式研究成果。'})
print(json.dumps({'question_id': question['id'], 'code_sha256': digest(code),
                  'observed_orders': summary['observed_orders'], 'output': str(output)}, ensure_ascii=False))
