"""Independent boundary checks for model-proposed multi-question archives."""
import base64
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from notebook.semantic import prepare_semantic_input, preview_semantic, import_semantic
from notebook.store import Store, ConflictError


class SemanticIndependentReview(unittest.TestCase):
    def setUp(self):
        runtime = Path(__file__).resolve().parents[1] / '.runtime'
        runtime.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='semantic-review-', dir=runtime)
        self.root = Path(self.temp.name)
        self.store = Store(self.root)
        c = self.store.perform('save_content', {'title': '数值求导'})
        f = self.store.perform('save_framework', {'title': '误差分析', 'content_ids': [c['id']]})
        q = self.store.perform('create_question', {'content_id': c['id'], 'title': '双侧差分的收敛性'})
        self.cid, self.fid, self.qid = c['id'], f['id'], q['id']
        self.store.perform('save_body', {'question_id': self.qid, 'revision': q['body_revision'], 'text': '# 已有正文\n不得覆盖。'})
        raw = ('用户：继续之前左右差分的复算。\r\n\r\n'
               '用户：单边差分要另列问题，代码可以共用。\r\n\r\n'
               '```python shared.py\r\nprint("shared, not automatically executed")\r\n```\r\n\r\n'
               '```csv forward.csv\r\nh,error\r\n0.1,0.05\r\n```\r\n')
        self.payload = {'text': raw, 'files': [{'filename': 'unknown.bin', 'base64': base64.b64encode(b'\0\xffunknown').decode()}]}
        prepared = prepare_semantic_input(self.store, self.payload)
        left, right, empty = [], [], []
        shared_mid, result_mid, unknown_mid = '', '', ''
        for block in prepared['blocks']:
            if not block['text'].strip():
                empty.append(block['id'])
            elif 'shared.py' in block['text']:
                left.append(block['id']); right.append(block['id'])
                shared_mid = block['material_ids'][0]
            elif '左右' in block['text']:
                left.append(block['id'])
            else:
                right.append(block['id'])
                if 'forward.csv' in block['text']:
                    result_mid = block['material_ids'][0]
        unknown_mid = next(m['id'] for m in prepared['materials'] if m['filename'] == 'unknown.bin')
        plan = {
            'frameworks': [{'key': 'frame', 'existing_id': self.fid, 'title': '模型企图改名', 'summary': '沿用原框架', 'source_ids': left[:1]}],
            'contents': [{'key': 'content', 'existing_id': self.cid, 'title': '模型企图改名', 'summary': '沿用原内容', 'source_ids': left[:1], 'framework_keys': ['frame']}],
            'questions': [
                {'key': 'left', 'existing_id': self.qid, 'title': '模型企图改名', 'summary': '继续双侧差分', 'source_ids': left, 'content_key': 'content', 'criterion': '', 'candidate_conclusion': '待核查', 'scope': '当前算例', 'material_ids': [shared_mid]},
                {'key': 'right', 'existing_id': '', 'title': '单边差分的收敛性', 'summary': '独立的一阶误差问题', 'source_ids': right, 'content_key': 'content', 'criterion': '', 'candidate_conclusion': '待核查', 'scope': '当前算例', 'material_ids': [shared_mid, result_mid]}
            ],
            'unassigned_source_ids': empty, 'unassigned_material_ids': [unknown_mid], 'notes': ['unknown.bin 无法确定归属']}
        self.data = {**self.payload, 'catalog_revision': prepared['catalog_revision'], 'plan': plan, 'model_info': {'provider': 'test-fixture'}}
        self.preview = preview_semantic(self.store, self.data)

    def tearDown(self):
        self.temp.cleanup()

    def request(self):
        return {**copy.deepcopy(self.data), 'token': self.preview['token'], 'request_id': 'review-batch-0001'}

    def files(self):
        return {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob('*') if p.is_file() and '.history' not in p.parts and '.runtime' not in p.relative_to(self.root).parts}

    def test_exact_original_shared_copies_existing_body_and_candidate_status(self):
        old_body = self.store.detail(self.qid)['body']
        result = import_semantic(self.store, self.request())
        self.assertEqual(len(result['question_ids']), 2)
        self.assertEqual(self.store.read_file(result['source_path']).read_bytes(), self.payload['text'].encode())
        self.assertEqual(self.store.detail(self.qid)['body'], old_body)
        self.assertEqual(self.store.detail(self.qid)['title'], '双侧差分的收敛性')
        shared = [next(f for f in q['files'] if f.get('name', Path(f['path']).name) == 'shared.py') for q in result['questions']]
        self.assertEqual(shared[0]['origin_path'], shared[1]['origin_path'])
        self.assertEqual(shared[0]['origin_sha256'], shared[1]['origin_sha256'])
        for item in shared:
            self.assertEqual(self.store.read_file(item['path']).read_bytes(), b'print("shared, not automatically executed")\r\n')
        for qid in result['question_ids']:
            self.assertEqual(self.store.detail(qid)['status'], 'discussing')
            self.assertEqual(len(self.store.detail(qid)['judgments']), 0)
        manifest = self.root / Path(result['batch_path']).parent / '归档索引.json'
        import json
        index = json.loads(manifest.read_text(encoding='utf-8'))
        unknown = next(m for m in index['materials'] if m['filename'] == 'unknown.bin')
        self.assertEqual(self.store.read_file(unknown['path']).read_bytes(), b'\0\xffunknown')
        rogue = manifest.parent / 'secret.txt'
        rogue.write_text('not registered', encoding='utf-8')
        with self.assertRaises(PermissionError):
            self.store.read_file(rogue.relative_to(self.root).as_posix())

    def test_invalid_model_references_rejected_before_writes(self):
        for mutation in ['unknown-source', 'unknown-material', 'unknown-entity', 'omitted-material', 'invented-code', 'source-mismatch']:
            data = copy.deepcopy(self.data)
            q = data['plan']['questions'][0]
            if mutation == 'unknown-source': q['source_ids'].append('B999')
            if mutation == 'unknown-material': q['material_ids'].append('M999')
            if mutation == 'unknown-entity': q['existing_id'] = 'C99-Q99'
            if mutation == 'omitted-material': data['plan']['unassigned_material_ids'] = []
            if mutation == 'invented-code': q['code_text'] = 'invented code'
            if mutation == 'source-mismatch': q['source_ids'] = q['source_ids'][:1]
            with self.subTest(mutation=mutation):
                before = self.files()
                with self.assertRaises(ValueError): preview_semantic(self.store, data)
                self.assertEqual(self.files(), before)

    def test_stale_catalog_and_modified_plan_do_not_write(self):
        before = self.files()
        bad = self.request()
        bad['plan']['questions'][1]['summary'] += 'changed after preview'
        with self.assertRaises(ConflictError): import_semantic(self.store, bad)
        self.assertEqual(self.files(), before)
        q = self.store.detail(self.qid)
        self.store.perform('update_question', {'id': q['id'], 'revision': q['revision'], 'why': 'changed elsewhere'})
        before = self.files()
        with self.assertRaises(ConflictError): import_semantic(self.store, self.request())
        self.assertEqual(self.files(), before)

    def test_identical_archive_retry_is_idempotent_and_other_payload_rejected(self):
        request = self.request()
        result = import_semantic(self.store, request)
        before = self.files()
        self.assertEqual(import_semantic(self.store, request), result)
        self.assertEqual(self.files(), before)
        request['text'] += '\nchanged'
        with self.assertRaises(ConflictError): import_semantic(self.store, request)

    def test_write_failure_rolls_back_all_research_changes(self):
        before = self.files()
        original = self.store._atomic
        writes = [0]
        def failing(*args, **kwargs):
            writes[0] += 1
            if writes[0] == 12: raise OSError('injected write failure')
            return original(*args, **kwargs)
        with patch.object(self.store, '_atomic', side_effect=failing):
            with self.assertRaises(OSError): import_semantic(self.store, self.request())
        self.assertGreaterEqual(writes[0], 12)
        self.assertEqual(self.files(), before)


if __name__ == '__main__':
    unittest.main()
