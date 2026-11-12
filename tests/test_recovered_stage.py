from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest

from openweb_news import clean_run, fixtures, interruption, profile_authority, reuse, review_pipeline
from openweb_news.common import digest, encoded, load, write_private
from openweb_news.ledger import Ledger
from openweb_news.preparation import prepare
from openweb_news.runner import run
from .recovery_completion_producer import capture
from .helpers import question
from .test_independent_method import accepted_fixture


class RecoveredStageTests(unittest.TestCase):
    def test_recovered_stage_finishes_without_native_calls_and_preserves_prefix(self):
        from .test_lean_checkpoints import LeanCheckpointTests
        with TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();helper,base,_,_=LeanCheckpointTests().setup_prefix(root)
            template=load(root/'template.json');cfg=deepcopy(template['configuration'])
            cfg.update(questions=template['questions'],provider_ids=[p['configuration_id']for p in template['providers']])
            cfg['execution_policy']['max_attempts_per_task']=3
            cfg['execution_policy']['retry_policy'].update(max_total=None,max_model=None,max_search=None)
            for rule in cfg['execution_policy']['scheduling']['recovery'].values():rule['cooldown_seconds']=0
            write_private(root/'held-template.json',prepare(cfg,mode='fixture'))
            old=clean_run.prepare(root/'held-template.json',base['selection_path'],root/'held',ceiling=base['shared_ceiling_usd'],
                accepted_profile=base['accepted_profile']['package_path'],archived_reference=base['archived_reference'],
                stage_counts=(1,),mode='fixture',verified_prefix=base['verified_seed'])
            owner=dict(decision='approved',package_sha256=digest(old),approved_by='synthetic owner',approved_at='fixture')
            original=fixtures.model
            def initial(role,compiled,folder):
                if role=='pro':return dict(transport_success=False,complete=False,dispatch_state='uncertain',error_type='TimeoutError')
                return original(role,compiled,folder)
            with patch('openweb_news.fixtures.model',side_effect=initial):
                held=clean_run.run(root/'held/clean-run.json',owner)
            self.assertEqual(held['state'],'completed_with_holds')
            source_path=root/'held/stage-1/native/package.json';source=load(source_path)
            native,receipt,method=self.recover(root/'recovery',source_path,source)
            before={p:p.read_bytes()for p in (source_path,root/'held/clean-run.json',root/'held/stage-1/native/run/report.json')}
            package=clean_run.prepare_recovered_stage(root/'held/clean-run.json',receipt,root/'finish',accepted_profile=method)
            approval=dict(decision='approved',package_sha256=digest(package),approved_by='synthetic owner',approved_at='fixture')
            path=root/'finish/continuation.json'
            with patch('openweb_news.clean_run.run_native',side_effect=AssertionError('no native redispatch')):
                result=clean_run.run(path,approval)
            self.assertEqual(result['state'],'awaiting_stage_review')
            report=load(result['evidence']['report']['path']);parent=load(load(base['verified_seed']['path'])['snapshot']['path'])
            previous=load(parent['report']['path'])
            retained={o['case_id']:o for o in previous['outcomes']}
            self.assertEqual({o['case_id']:o for o in report['outcomes']if o['case_id']in retained},retained)
            from openweb_news.accounting import report_costs
            from decimal import Decimal
            self.assertEqual(Decimal(report['phase_costs']['conservative_total_usd']),
                Decimal(previous['phase_costs']['conservative_total_usd'])+Decimal(report_costs(native,Path(native['configuration']['run_directory']))['conservative_total_usd']))
            self.assertEqual(before,{p:p.read_bytes()for p in before})
            helper.review(path,result,gate_sha256=result['gate_sha256'])
            self.assertEqual(clean_run.run(path,approval)['state'],'completed')
            clean_run._verified_prefix(clean_run._bound_file(root/'finish/stage-1/prefix.json'),package['accepted_profile'])

    def completed(self, root):
        cfg = fixtures.config(root/'source-run'); cfg['questions'] = [question('recovered-stage')]
        cfg['execution_policy'].update(spending_ceiling_usd='10',max_attempts_per_task=3)
        cfg['execution_policy']['retry_policy'].update(max_total=None,max_model=None,max_search=None)
        from openweb_news.recovery import sibling_rule
        cfg['execution_policy']['scheduling']['recovery']={account:sibling_rule(
            'provider' if account.startswith('provider/') else 'judge',row['concurrency'])
            for account,row in cfg['execution_policy']['scheduling']['accounts'].items()}
        for rule in cfg['execution_policy']['scheduling']['recovery'].values():rule['cooldown_seconds']=0
        cfg,_ = accepted_fixture(root/'authority',cfg)
        source = prepare(cfg,mode='fixture'); write_private(root/'source.json',source)
        original = fixtures.model
        def initial(role, compiled, folder):
            if role=='pro':
                return dict(transport_success=False,complete=False,dispatch_state='uncertain',error_type='TimeoutError')
            return original(role,compiled,folder)
        with patch('openweb_news.fixtures.model',side_effect=initial): run(source)
        return self.recover(root,root/'source.json',source)

    def recover(self, root, source_path, source):
        request = dict(kind='cache',role='pro',reservation_usd='0.01',wire=dict(method='POST',
            endpoint='https://generativelanguage.googleapis.com/v1beta/cachedContents',body={}))
        raw = encoded(dict(error=dict(code=402,status='RESOURCE_EXHAUSTED',message='Your prepayment credits are depleted. Synthetic.')))
        with Ledger(Path(source['configuration']['run_directory']),dict(package_sha256=digest(source),mode='fixture')).locked() as ledger:
            ledger.execute('cache-create-'+digest(request),request,lambda _:dict(transport_success=False,
                complete=True,dispatch_state='completed',http_status=402,raw_text=raw.decode(),retained_sha256=digest(raw)))
        reference = reuse.freeze(source_path,root/'snapshot')
        from openweb_news.common import records
        selected=[]
        for end in records(root/'snapshot/events.jsonl'):
            if end['event']!='finished':continue
            response=load(root/'snapshot'/end['result_path'])
            if response.get('dispatch_state')=='uncertain' or response.get('http_status')==402:
                selected.append(dict(task_id=end['task_id'],prior_attempt_id=end['attempt_id'],
                    request_sha256=end['request_sha256'],prior_result_sha256=end['result_sha256']))
        child=interruption.prepare_credit_restoration(source_path,reference,root/'native',selected,
            accepted_profile=source['configuration']['live_profile_authority']['package_path'],reason='Synthetic restored native')
        write_private(root/'owner.json',dict(decision='approved',package_sha256=digest(child),approved_by='synthetic owner',approved_at='fixture'))
        result=run(child,retry_failed=True);self.assertEqual(result['execution']['state'],'completed')
        receipt=capture(root/'native/package.json',root/'owner.json',root/'capture')
        newcfg=deepcopy(source['configuration']);newcfg['provider_ids']=[p['configuration_id']for p in source['providers']]
        method=profile_authority.prepare_method(newcfg,root/'new-method',mode='fixture')
        write_private(root/'new-method/approval.json',dict(approved=True,accept_method=True,
            package_sha256=digest(method),approved_by='synthetic owner',approved_at='fixture'))
        return child,receipt,root/'new-method/package.json'

    def test_completed_native_rebind_keeps_original_package_report_and_inputs(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();native,receipt,method=self.completed(root)
            before={p:p.read_bytes() for p in (root/'native/package.json',root/'native/run/report.json')}
            record=clean_run.native_authority_rebind(receipt,method)
            review=review_pipeline.prepare_review(root/'native/package.json',
                [q['question_id']for q in native['questions']],root/'review',authority_rebind=record)
            self.assertEqual(review['source_package_sha256'],digest(native))
            self.assertEqual(review['source_report_sha256'],digest(before[root/'native/run/report.json']))
            self.assertEqual(review['live_profile_authority'],record['new_authority'])
            self.assertNotEqual(review['live_profile_authority'],native['configuration']['live_profile_authority'])
            review_pipeline.verify(review,root/'review')
            self.assertEqual(before,{p:p.read_bytes()for p in before})
            bad=deepcopy(record);bad['source_package_sha256']='0'*64
            with self.assertRaises(ValueError):clean_run.verify_native_authority_rebind(native,bad)
            with self.assertRaises(ValueError):clean_run.verify_native_authority_rebind(native,{})
            changed=deepcopy(record['new_authority'])
            changed['profile_bindings']['semantic_code']['grading.py']='0'*64
            with patch('openweb_news.profile_authority.read',return_value=changed):
                with self.assertRaises(ValueError):clean_run.native_authority_rebind(receipt,method)

    def test_completion_receipt_tamper_or_incomplete_native_refused(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();native,receipt,method=self.completed(root)
            p=Path(receipt['path']);saved=p.read_bytes();changed=load(p)
            changed['runtime_code_sha256']='0'*64;write_private(p,changed,replace=True)
            with self.assertRaises(ValueError):clean_run.native_authority_rebind(receipt,method)
            modified=dict(path=str(p),sha256=digest(p.read_bytes()))
            with self.assertRaises(ValueError):clean_run.native_authority_rebind(modified,method)
            p.write_bytes(saved)
            changed=load(p);changed['producer_sha256']='0'*64
            write_private(p,changed,replace=True)
            with self.assertRaises(ValueError):
                clean_run.native_authority_rebind(clean_run._bound_file(p),method)
            p.write_bytes(saved)
            report=root/'native/run/report.json';body=load(report);body['execution']['state']='completed_with_holds'
            write_private(report,body,replace=True)
            with self.assertRaises(ValueError):
                capture(root/'native/package.json',root/'owner.json',root/'incomplete')
