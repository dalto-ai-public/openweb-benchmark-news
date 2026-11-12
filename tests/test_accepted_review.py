from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from types import SimpleNamespace
import unittest

from openweb_news import accepted_review,profile_authority,adoption,settlement,judging,checkpoint
from openweb_news.common import digest,load,write_private
from openweb_news.reporting import write_reports
from . import test_publication as pubtests
from . import test_subscription_health as native_tests


class AcceptedReviewTests(unittest.TestCase):
    def evidence(self,root):
        f=pubtests.PublicationTests();f.setUp();parent,cases,native=f.parent(root)
        base=deepcopy(parent['outcomes'][0])
        for cid,grade in [('unknown-second',None),('known',1)]:
            packet=deepcopy(f.compiled['packet']);packet['case_id']=cid
            packet['returned_url']='https://example.test/'+cid
            compiled=judging.compile_packet(packet,f.compiled['input']['captured_at'],config=f.provider)
            cases[cid]=dict(compiled=compiled,provider=f.provider)
            item=deepcopy(base);item.update(case_id=cid,packet_sha256=compiled['packet_sha256'],rank=len(parent['outcomes'])+1)
            if grade is not None:item.update(grade=grade,status='determined')
            parent['outcomes'].append(item)
        parent.update(outcome_counts={'unknown':2,'1':1},execution=dict(state='completed'))
        settlement._rescore(parent,cases,native)
        source=root/'source/report/report.json';write_reports(source.parent,parent)
        authority=dict(package_path=str(root/'authority/package.json'),package_sha256='accepted',approval_sha256='owner',
            profile_bindings=profile_authority.bindings(native['configuration']['reviewers']))
        return f,parent,cases,native,source,authority

    def test_invalid_is_local_terminal_reuse_and_zero_send_report_resume(self):
        with TemporaryDirectory()as temp:
            root=Path(temp);f,parent,cases,native,source,authority=self.evidence(root)
            with patch.object(settlement,'load_parent',return_value=(parent,cases,native)),\
                 patch.object(profile_authority,'read',return_value=authority):
                p=accepted_review.prepare(source,authority['package_path'],root/'pipeline',allow_inference=True,mode='fixture')
                self.assertEqual(p['review_tasks'],4)
                child=load(root/'pipeline/publication/package.json')
                self.assertNotIn('known',{j['case_id']for j in child['jobs']})
                called=[]
                def call(job,folder):
                    called.append(job['task_id']);v=f.output(2);v['case_id']=job['case_id']
                    if job['case_id']=='unknown-second'and job['role']=='claude':v['schema_version']=99
                    return dict(verdict=v,transport_success=True,native_completion_verified=True)
                report=accepted_review.run(root/'pipeline/review-checkpoint.json',{},call=call)
                own={o['case_id']:o for o in report['outcomes']}
                self.assertEqual(own['known'],next(o for o in parent['outcomes']if o['case_id']=='known'))
                self.assertEqual(own[f.compiled['packet']['case_id']]['grade'],2)
                self.assertIsNone(own['unknown-second']['grade'])
                self.assertEqual(load(root/'pipeline/closure.json')['execution']['state'],'completed')
                self.assertFalse(load(root/'pipeline/review-summary.json')['calibration_repeated'])
                self.assertEqual(len(called),4)
                accepted_review.run(root/'pipeline/review-checkpoint.json',{},call=lambda *_:self.fail('resume dispatch'))
                self.assertEqual(settlement.selected(report,cases,native['configuration']['reviewers'],authority=authority),[])
                before=(root/'pipeline/report/report.json').read_bytes()
                with patch.object(settlement,'load_parent',return_value=(report,cases,native)):
                    zero=accepted_review.prepare(root/'pipeline/report/report.json',authority['package_path'],root/'zero',maximum_tasks=0)
                    approval=dict(approved=True,package_sha256=digest(zero),approved_by='owner',approved_at='synthetic')
                    with patch('openweb_news.review_pipeline.backends.subscription',side_effect=AssertionError('zero-send login')):
                        accepted_review.run(root/'zero/review-checkpoint.json',approval)
                        accepted_review.run(root/'zero/review-checkpoint.json',approval)
                    self.assertEqual((root/'zero/report/report.json').read_bytes(),before)
                    self.assertFalse((root/'zero/publication/run/attempts.jsonl').exists())
                changed=dict(authority,approval_sha256='changed')
                with patch.object(profile_authority,'read',return_value=changed):
                    with self.assertRaises(ValueError):accepted_review.verify(p,root/'pipeline')

    def test_task_scope_and_model_identity_rejected_before_dispatch(self):
        with TemporaryDirectory()as temp:
            root=Path(temp);_,parent,cases,native,source,authority=self.evidence(root)
            with patch.object(settlement,'load_parent',return_value=(parent,cases,native)),\
                 patch.object(profile_authority,'read',return_value=authority):
                with self.assertRaises(ValueError):accepted_review.prepare(source,authority['package_path'],root/'no-sends')
                self.assertFalse((root/'no-sends').exists())
                accepted_review.prepare(source,authority['package_path'],root/'no-sends',allow_inference=True,mode='fixture')
                with self.assertRaises(ValueError):accepted_review.prepare(source,authority['package_path'],root/'too-many',allow_inference=True,maximum_tasks=2)
                self.assertFalse((root/'too-many').exists())

    def test_completed_nonzero_native_child_replays_and_resumes_without_login(self):
        with TemporaryDirectory()as temp:
            root=Path(temp).resolve();f,parent,cases,native,source,authority=self.evidence(root)
            original_load=settlement.load_parent
            def parent_loader(path,**kwargs):
                return(parent,cases,native)if Path(path).resolve()==source.resolve()else original_load(path,**kwargs)
            with patch.object(settlement,'load_parent',side_effect=parent_loader),\
                 patch.object(profile_authority,'read',return_value=authority):
                p=accepted_review.prepare(source,authority['package_path'],root/'pipeline',allow_inference=True)
                approval=dict(approved=True,package_sha256=digest(p),approved_by='owner',approved_at='synthetic')
                def original(role,compiled,folder):
                    value=f.output(1);value['case_id']=compiled['packet']['case_id']
                    return dict(verdict=value,transport_success=True,complete=True,served_model='synthetic-model')
                emitters={'codex':native_tests.native_model(original),'claude':native_tests.native_claude(original)}
                def emit(role,packet,folder):
                    compiled=cases[packet['input']['case_id']]['compiled']
                    proxy=dict(compiled,input=packet['input'],prompt=packet['prompt'],schema=packet['schema'],
                        input_sha256=digest(packet['input']),schema_sha256=digest(packet['schema']),prompt_sha256=digest(packet['prompt'].encode()))
                    return emitters[role](role,proxy,folder)
                def subscription(config,packet,folder,**kwargs):
                    return dict(transport_success=True)if kwargs.get('preflight_only')else emit('claude',packet,folder)
                class Runtime:
                    def __init__(runtime,*a,**kw):runtime.client=SimpleNamespace(failure=None)
                    def __enter__(runtime):return runtime
                    def __exit__(runtime,*a):pass
                    def review(runtime,packet,folder):return emit('codex',packet,folder)
                with patch('openweb_news.review_pipeline.backends.subscription',side_effect=subscription),\
                     patch('openweb_news.codex_runtime.CodexRuntime',Runtime),\
                     patch('openweb_news.quota.QuotaGate',return_value=None):
                    result=accepted_review.run(root/'pipeline/review-checkpoint.json',approval)
                self.assertEqual(result['publication_review']['profile'],authority['profile_bindings']['checker_profile'])
                replay,_,_=settlement.load_parent(root/'pipeline/report/report.json')
                self.assertEqual(replay['outcomes'],result['outcomes'])
                ledger=root/'pipeline/publication/run/attempts.jsonl';before=ledger.read_bytes()
                with patch('openweb_news.review_pipeline.backends.subscription',side_effect=AssertionError('completed login')):
                    accepted_review.run(root/'pipeline/review-checkpoint.json',approval)
                self.assertEqual(ledger.read_bytes(),before)

    def test_native_hold_still_reports_unaffected_cases(self):
        with TemporaryDirectory()as temp:
            root=Path(temp);f,parent,cases,native,source,authority=self.evidence(root)
            with patch.object(settlement,'load_parent',return_value=(parent,cases,native)),\
                 patch.object(profile_authority,'read',return_value=authority):
                accepted_review.prepare(source,authority['package_path'],root/'pipeline',allow_inference=True,mode='fixture')
                def call(job,folder):
                    if job['case_id']=='unknown-second'and job['role']=='claude':
                        return dict(transport_success=False,native_completion_verified=False,dispatch_state='not_sent')
                    v=f.output(2);v['case_id']=job['case_id']
                    return dict(verdict=v,transport_success=True,native_completion_verified=True)
                report=accepted_review.run(root/'pipeline/review-checkpoint.json',{},call=call)
                own={o['case_id']:o for o in report['outcomes']}
                self.assertEqual(own[f.compiled['packet']['case_id']]['grade'],2)
                self.assertIsNone(own['unknown-second']['grade'])
                self.assertEqual(load(root/'pipeline/closure.json')['execution']['state'],'completed_with_holds')
                self.assertTrue((root/'pipeline/report/report.html').exists())

    def test_checkpoint_resume_uses_automatic_review_without_rewriting_baseline(self):
        with TemporaryDirectory()as temp:
            root=Path(temp)/'checkpoint';source=root/'report/report.json'
            write_private(source,dict(frozen_baseline=True));write_private(root/'closure.json',dict(base_completed=True))
            p=dict(accepted_profile=dict(package_path='accepted-profile'),accepted_review_directory=str(root.parent/'checkpoint-accepted-review'),accepted_review_task_bound=4)
            approval=dict(decision='approved',package_sha256=digest(p),approved_by='owner',approved_at='synthetic')
            destination=Path(p['accepted_review_directory']);count=[]
            def prepare_child(parent,profile,directory,**kwargs):
                count.append('prepared');child=dict(parent_report_path=str(source.resolve()),accepted_profile=p['accepted_profile'],
                    allow_inference=True,maximum_review_tasks=4,mode='live')
                write_private(destination/'review-checkpoint.json',child);return child
            def finish_child(path,receipt):
                self.assertEqual(receipt['parent_checkpoint_sha256'],digest(p))
                write_private(destination/'closure.json',dict(execution=dict(state='completed')),replace=True)
                return dict(questions=[{}],outcome_counts={'2':1},phase_costs={})
            before=source.read_bytes()
            with patch.object(checkpoint,'verify_checkpoint',return_value=(p,{},[])),\
                 patch.object(checkpoint,'run',side_effect=AssertionError('native redispatch')),\
                 patch.object(settlement,'load_parent',return_value=({}, {}, {})),\
                 patch.object(accepted_review,'prepare',side_effect=prepare_child),\
                 patch.object(accepted_review,'run',side_effect=finish_child):
                checkpoint._run_checkpoint(root/'checkpoint.json',approval)
                checkpoint._run_checkpoint(root/'checkpoint.json',approval)
            self.assertEqual(count,['prepared'])
            self.assertEqual(source.read_bytes(),before)
            self.assertEqual(load(root/'pipeline-closure.json')['execution']['state'],'completed')

    def test_legacy_terminal_reuse_requires_same_acceptance_context(self):
        with TemporaryDirectory()as temp:
            root=Path(temp);f,parent,cases,native,_,authority=self.evidence(root)
            cid=f.compiled['packet']['case_id'];packet=f.packet
            parent['publication_review']=dict(records=[dict(case_id=cid,terminal_key='old-key',terminal=True,
                checker_profile=authority['profile_bindings']['checker_profile'],provenance={role:dict(
                native_completion_verified=True,input_sha256=packet['input_sha256'])for role in('codex','claude')})])
            self.assertIn(cid,{r['case_id']for r in settlement.selected(parent,cases,native['configuration']['reviewers'],authority=authority)})
            parent['checker_acceptance']=dict(approval_sha256=authority['approval_sha256'])
            self.assertNotIn(cid,{r['case_id']for r in settlement.selected(parent,cases,native['configuration']['reviewers'],authority=authority)})


class ProfileAuthorityTests(unittest.TestCase):
    def test_binding_receipt_and_original_failed_calibration_are_sealed(self):
        with TemporaryDirectory()as temp:
            root=Path(temp);f=pubtests.PublicationTests();f.setUp();_,_,native=f.parent(root)
            reviewers=native['configuration']['reviewers'];cp=root/'calibration/package.json'
            write_private(cp,dict(fixture=True));write_private(cp.parent/'run/calibration.json',dict(calibration_pass=False))
            p=dict(profile=adoption.PROFILE,output_directory=str(root/'accepted'),
                profile_bindings=profile_authority.bindings(reviewers),checker_profile=profile_authority.publication.CHECKER_PROFILE,
                calibration_pass=False,calibration_package_path=str(cp),calibration_package_sha256=digest(cp.read_bytes()),
                calibration_sha256=digest((cp.parent/'run/calibration.json').read_bytes()),source_report_sha256='source',promoted_case_ids=[])
            path=root/'accepted/package.json';write_private(path,p)
            approval=dict(approved=True,package_sha256=digest(p),approved_by='owner',approved_at='synthetic',accept_failed_calibration=True)
            write_private(path.parent/'approval.json',approval)
            report=dict(package_sha256=digest(p),checker_acceptance=dict(approval_sha256=digest(approval)))
            report_path=path.parent/'report/report.json';write_private(report_path,report)
            write_private(path.parent/'closure.json',adoption._closure(p,digest(report_path.read_bytes())))
            self.assertTrue(profile_authority.read(path,reviewers)['one_time_owner_exception'])
            wrong=deepcopy(reviewers);wrong['codex']['reasoning_effort']='changed'
            with self.assertRaises(ValueError):profile_authority.read(path,wrong)
            write_private(cp.parent/'run/calibration.json',dict(calibration_pass=True),replace=True)
            with self.assertRaises(ValueError):profile_authority.read(path,reviewers)


if __name__=='__main__':unittest.main()
