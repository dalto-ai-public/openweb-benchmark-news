from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest

from openweb_news import batch,checkpoint_recovery as recovery,settlement,profile_authority
from openweb_news.common import load,write_private,digest
from openweb_news.preparation import prepare
from openweb_news.runner import run
from . import test_batch


class CheckpointRecoveryTests(unittest.TestCase):
    def test_saved_stage_continuation_reports_and_resumes_without_native_dispatch(self):
        with TemporaryDirectory()as temp:
            root=Path(temp).resolve()
            parent,cases,template,parent_path,selection,authority,_=test_batch.BatchTests().setup_parent(root)
            # This controller test uses determined parent results. Terminal
            # unknown propagation is exercised separately by the batch tests.
            cid=parent['outcomes'][0]['case_id']
            parent['outcomes'][0]['grade']=cases[cid]['original']['grade']
            cases[cid]['compiled']['input']['publication_estimate_applicability']=None
            parent['publication_review']['records']=[]
            write_private(parent_path,parent,replace=True)
            original_loader=settlement.load_parent;plan=batch.native_plan
            def loader(path,**kwargs):
                return(parent,cases,template)if Path(path).resolve()==parent_path else original_loader(path,**kwargs)
            def fixture_plan(*a,**kw):
                cfg,_=plan(*a,**kw);return cfg,prepare(cfg,mode='fixture')
            with patch.object(settlement,'load_parent',side_effect=loader),patch.object(profile_authority,'read',return_value=authority),patch.object(batch,'native_plan',side_effect=fixture_plan):
                original=batch.prepare(parent_path,selection,10,root/'original',ceiling='600',accepted_profile=authority['package_path'])
                native=load(root/'original/native/package.json');native_report=run(native)
                approval=dict(decision='approved',package_sha256=digest(original),approved_by='owner',approved_at='fixture')
                write_private(root/'original/approval.json',approval)
                component=batch.parent_component(parent_path,(parent,cases,template))
                audit=dict(source_ledger_sha256='synthetic-ledger',counts={},unclassified=[],records=[])
                def source(*a,**kw):return original,native,[component],approval,native_report,audit
                before=(root/'original/native/run/report.json').read_bytes()
                with patch.object(recovery,'source',side_effect=source):
                    write_private(root/'original/review/package.json',dict(existing='downstream-preparation'))
                    with self.assertRaises(ValueError):recovery.prepare(root/'original/checkpoint.json',root/'rejected')
                    self.assertFalse((root/'rejected').exists())
                    (root/'original/review/package.json').unlink()
                    p=recovery.prepare(root/'original/checkpoint.json',root/'recovery')
                    self.assertEqual(p['review_tasks'],0)
                    self.assertEqual(recovery.prepare(root/'original/checkpoint.json',root/'recovery'),p)
                    with self.assertRaises(ValueError):recovery.prepare(root/'original/checkpoint.json',root/'duplicate')
                    self.assertFalse((root/'duplicate').exists())
                    with (patch('openweb_news.runner.run',side_effect=AssertionError('native dispatch')),
                          patch('openweb_news.backends.https',side_effect=AssertionError('paid API dispatch'))):
                        result=recovery.run(root/'recovery/package.json')
                        self.assertEqual(result['state'],'completed')
                        final=Path(result['report']).with_suffix('.json')
                        report,_,_=settlement.load_parent(final)
                        self.assertEqual(len(report['questions']),12)
                        self.assertEqual(report['outcomes'][:len(parent['outcomes'])],parent['outcomes'])
                        files={str(p):digest(p.read_bytes())for p in(root/'recovery/report').iterdir()if p.is_file()}
                        recovery.run(root/'recovery/package.json')
                        self.assertEqual(files,{str(p):digest(p.read_bytes())for p in(root/'recovery/report').iterdir()if p.is_file()})
                    self.assertEqual((root/'original/native/run/report.json').read_bytes(),before)
                    self.assertFalse((root/'recovery/native').exists())
                    self.assertEqual(load(root/'recovery/closure.json')['new_pro_calls'],0)
                    approval['approved_by']='changed'
                    with self.assertRaises(ValueError):recovery.verify(root/'recovery/package.json')

    def test_method_code_changes_rejected_before_saved_stage_derivation(self):
        with TemporaryDirectory()as temp:
            root=Path(temp);from openweb_news.common import code_bindings,asset_bindings
            for name in ('judging.py','checkpoint.py','settlement.py'):
                p=dict(profile=batch.PROFILE,code=dict(code_bindings(),**{name:'changed'}),assets=asset_bindings())
                write_private(root/'checkpoint.json',p,replace=True)
                with patch.object(batch,'verify',side_effect=AssertionError('invalid source reached derivation')):
                    with self.subTest(name=name),self.assertRaises(ValueError):recovery.prepare(root/'checkpoint.json',root/'recovery')
            self.assertFalse((root/'recovery').exists())
