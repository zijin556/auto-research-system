import base64
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from notebook.store import Store, ConflictError
from notebook.semantic import prepare_semantic_input, preview_semantic, import_semantic


class SemanticTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="semantic-test-")
        self.root = Path(self.temp.name)
        self.store = Store(self.root)
        self.raw = '用户：中心差分和向前差分的收敛阶不同吗？\r\n\r\nAI：分别核查这两个问题。\r\n\r\n```python shared.py\r\nprint("comparison")\r\n```\r\n\r\n```csv center.csv\r\nh,error\r\n.1,.01\r\n```\r\n'
        self.input = {'text': self.raw, 'files': [{'filename': 'unknown.bin', 'base64': base64.b64encode(b'\x00\xfftrace').decode()}]}
        self.prepared = prepare_semantic_input(self.store, self.input)
        source_ids = [b['id'] for b in self.prepared['blocks']]
        self.plan = {'frameworks': [{'key':'f1','existing_id':'','title':'离散误差','summary':'比较两类差分的误差','source_ids':source_ids[:1]}],
            'contents': [{'key':'c1','existing_id':'','title':'收敛性','summary':'比较中心与向前差分','source_ids':source_ids[:1],'framework_keys':['f1']}],
            'questions': [{'key':'q1','existing_id':'','title':'中心差分是否二阶？','summary':'本轮讨论中心差分。','source_ids':source_ids,'content_key':'c1','criterion':'核对收敛阶','candidate_conclusion':'尚待运行','scope':'合成函数','material_ids':['M001','M002']},
                          {'key':'q2','existing_id':'','title':'向前差分是否一阶？','summary':'本轮讨论向前差分。','source_ids':source_ids[:3],'content_key':'c1','criterion':'','candidate_conclusion':'','scope':'','material_ids':['M001']}],
            'unassigned_source_ids': [], 'unassigned_material_ids':['M003'], 'notes':['共享程序同时关联两个问题，未知材料留待归属。']}
        # Include the source code fence in both questions regardless of blank splitting.
        code_bid = self.prepared['materials'][0]['source_block_id']
        if code_bid not in self.plan['questions'][1]['source_ids']:
            self.plan['questions'][1]['source_ids'].append(code_bid)

    def tearDown(self):
        self.temp.cleanup()

    def payload(self, plan=None):
        return {**self.input, 'plan': copy.deepcopy(plan or self.plan), 'catalog_revision':self.prepared['catalog_revision'], 'model_info':{'provider':'test-double','actual_model_call':False}}

    def save(self, payload=None, rid='semantic-test-0001'):
        payload = payload or self.payload()
        preview = preview_semantic(self.store, payload)
        payload = {**payload,'token':preview['token'],'request_id':rid}
        return import_semantic(self.store,payload),payload

    def test_prepare_keeps_all_source_and_exact_fence_bytes(self):
        self.assertEqual(''.join(b['text'] for b in self.prepared['blocks']),self.raw)
        self.assertEqual(self.prepared['materials'][0]['text_preview'],'print("comparison")\r\n')
        self.assertEqual(self.prepared['materials'][-1]['source'],'attachment')
        self.assertEqual(self.prepared['materials'][-1]['attachment_index'],0)
        self.assertEqual(self.prepared['materials'][-1]['text_preview'],'')
        self.assertEqual(self.store.state()['questions'],[])

    def test_multiquestion_shared_files_raw_audit_and_unassigned_survive(self):
        saved,_ = self.save()
        self.assertEqual(len(saved['question_ids']),2)
        self.assertEqual((self.root/saved['source_path']).read_bytes(),self.raw.encode())
        for question in saved['questions']:
            code = next(f for f in question['files'] if f['material_id']=='M001')
            self.assertEqual((self.root/code['path']).read_bytes(),b'print("comparison")\r\n')
            self.assertEqual((self.root/code['origin_path']).read_bytes(),b'print("comparison")\r\n')
            self.assertEqual(self.store.detail(question['id'])['status'],'discussing')
        batch = (self.root/saved['source_path']).parent
        manifest = json.loads((batch/'归档索引.json').read_text(encoding='utf-8'))
        unassigned = next(m for m in manifest['materials'] if m['id']=='M003')
        self.assertEqual(self.store.read_file(unassigned['path']).read_bytes(),b'\x00\xfftrace')
        arbitrary = batch/'unregistered.txt'
        arbitrary.write_text('private',encoding='utf-8')
        with self.assertRaises(PermissionError): self.store.read_file(arbitrary.relative_to(self.root).as_posix())
        self.assertEqual(self.store.read_file(saved['batch_path']),batch/'本轮整理.md')

    def test_duplicate_submit_is_idempotent_and_new_round_versions_files(self):
        saved,payload = self.save()
        self.assertEqual(import_semantic(self.store,payload),saved)
        fresh = prepare_semantic_input(self.store,self.input)
        p = copy.deepcopy(self.plan)
        p['frameworks'][0]['existing_id']=saved['framework_ids'][0]
        p['contents'][0]['existing_id']=saved['content_ids'][0]
        for q,old in zip(p['questions'],saved['question_ids']):q['existing_id']=old
        before = self.store.detail(saved['question_ids'][0])['body']
        new,_ = self.save({**self.input,'plan':p,'catalog_revision':fresh['catalog_revision']},'semantic-test-0002')
        self.assertEqual(new['question_ids'],saved['question_ids'])
        self.assertEqual(len(self.store.state()['questions']),2)
        self.assertEqual(self.store.detail(saved['question_ids'][0])['body'],before)
        self.assertEqual(self.store.detail(saved['question_ids'][0])['counts']['codes'],2)

    def test_unknown_missing_and_inconsistent_source_rejected_before_writes(self):
        variants=[]
        p=copy.deepcopy(self.plan);p['questions'][0]['source_ids'].append('B999');variants.append(p)
        p=copy.deepcopy(self.plan);p['questions'][0]['material_ids'].append('M999');variants.append(p)
        p=copy.deepcopy(self.plan);p['unassigned_material_ids']=[];variants.append(p)
        p=copy.deepcopy(self.plan);p['questions'][0]['source_ids']=[];variants.append(p)
        p=copy.deepcopy(self.plan);p['questions'][0]['existing_id']='C99-Q99';variants.append(p)
        p=copy.deepcopy(self.plan);p['questions'][0]['summary']='<!-- field:raw -->';variants.append(p)
        for plan in variants:
            with self.subTest(plan=plan), self.assertRaises(ValueError):preview_semantic(self.store,self.payload(plan))
        self.assertEqual(self.store.state()['questions'],[])
        self.assertFalse((self.root/'04_交接包/AI整理').exists())

    def test_stale_catalog_and_modified_preview_rejected(self):
        payload=self.payload();preview=preview_semantic(self.store,payload)
        mutated=copy.deepcopy(payload);mutated['plan']['questions'][0]['summary']='different'
        with self.assertRaises(ConflictError):import_semantic(self.store,{**mutated,'token':preview['token'],'request_id':'semantic-test-0003'})
        self.store.perform('save_content',{'title':'外部新增内容'})
        with self.assertRaises(ConflictError):preview_semantic(self.store,payload)
        self.assertEqual(self.store.state()['questions'],[])

    def test_project_context_is_included_and_changes_invalidate_plan(self):
        project = self.store.state()['project']
        self.store.perform('save_project',{'revision':project['revision'],'brief':'研究中心差分', 'background':'比较数值近似的收敛性','configuration':'Python 双精度，x=1','symbols':'h 为步长','rules':'保留原始结果'})
        prepared = prepare_semantic_input(self.store,self.input)
        context = prepared['catalog']['project_context']
        self.assertEqual(context['brief'],'研究中心差分')
        self.assertEqual(context['background'],'比较数值近似的收敛性')
        self.assertEqual(context['configuration'],'Python 双精度，x=1')
        self.assertEqual(context['symbols'],'h 为步长')
        self.assertNotIn('path',context)
        self.assertNotIn('root',context)
        payload = {**self.payload(),'catalog_revision':prepared['catalog_revision']}
        preview_semantic(self.store,payload)
        current = self.store.state()['project']
        self.store.perform('save_project',{'revision':current['revision'],'configuration':'Python 双精度，x=2'})
        with self.assertRaises(ConflictError):preview_semantic(self.store,payload)

    def test_transaction_restores_every_batch_write_after_failure(self):
        before={p.relative_to(self.root):p.read_bytes() for p in self.root.rglob('*') if p.is_file() and '.history' not in p.parts and '.runtime' not in p.parts}
        original=self.store._add_code
        calls=0
        def fail_second(data):
            nonlocal calls
            calls+=1
            if calls==2:raise OSError('test injected failure')
            return original(data)
        with patch.object(self.store,'_add_code',side_effect=fail_second),self.assertRaises(OSError):self.save()
        after={p.relative_to(self.root):p.read_bytes() for p in self.root.rglob('*') if p.is_file() and '.history' not in p.parts and '.runtime' not in p.parts}
        self.assertEqual(before,after)

    def test_existing_question_cannot_move_to_new_parent(self):
        saved,_=self.save()
        prepared=prepare_semantic_input(self.store,self.input)
        p=copy.deepcopy(self.plan);p['questions'][0]['existing_id']=saved['question_ids'][0]
        with self.assertRaises(ValueError):preview_semantic(self.store,{**self.input,'plan':p,'catalog_revision':prepared['catalog_revision']})


if __name__=='__main__':unittest.main()
