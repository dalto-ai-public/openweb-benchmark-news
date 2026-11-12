from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from openweb_news import adjudication, representation, review_pipeline
from openweb_news.common import asset_bindings, code_bindings, digest, load, write_private
from openweb_news.judging import decode_position
from openweb_news.review import settle
from openweb_news._method import text_dates, two_step
from . import test_execution
from openweb_news.resilience import POLICY
from openweb_news.recovery import sibling_rule


class RepresentationTests(unittest.TestCase):
    def test_space_equivalence_preserves_raw_spans_and_old_profiles(self):
        for char in representation._SPACES:
            with self.subTest(codepoint=hex(ord(char))):
                text='Prefix A'+char+'B suffix'
                for profile in ('raw-v1',representation.PROFILE):
                    with representation.selected(profile),self.assertRaises(ValueError):text_dates.exact_span(text,'A B',1)
                with representation.selected(representation.SPACE_PROFILE):
                    span=text_dates.exact_span(text,'A B',1)
                    self.assertEqual(span['quote'],text[span['start']:span['end']])
                    self.assertEqual((span['start'],span['end']),(7,10))
                    self.assertEqual(span['mode'],representation.SPACE_PROFILE+'_to_raw_span')
                    self.assertEqual(text_dates.exact_span('A B','A'+char+'B',1)['quote'],'A B')

    def test_space_profile_does_not_fold_words_or_ambiguous_occurrences(self):
        with representation.selected(representation.SPACE_PROFILE):
            for text,quote in [('A  B','A B'),('A\tB','A B'),('A\nB','A B'),('A\u200bB','A B'),('A-B','A B'),('A\u2011B','A-B'),('A&amp;B','A&B'),('<b>A</b> B','A B'),('Alice\u00a0Jones','Alicia Jones')]:
                with self.subTest(text=text),self.assertRaises(ValueError):text_dates.exact_span(text,quote,1)
            text='A\u00a0B / A\u202fB'
            with self.assertRaises(ValueError):text_dates.exact_span(text,'A B',0)
            self.assertEqual(text_dates.exact_span(text,'A B',2)['start'],6)
            with self.assertRaises(ValueError):text_dates.exact_span(text,'A B',3)
            # Successful historical literal/visible choices keep their priority.
            text='A\u00a0B / A B'
            self.assertEqual(text_dates.exact_span(text,'A B',1)['start'],6)
            text='**A\u00a0B** uses C\\&D'
            span=text_dates.exact_span(text,'A B uses C&D',1)
            self.assertEqual(span['quote'],text[span['start']:span['end']])
            for field in ('native','url'):
                packet=dict(allowed_text=[],native_metadata=[dict(field='native',text='A\u00a0B')],returned_url='A\u00a0B')
                with self.assertRaises(ValueError):two_step.resolve_quotes(packet,[dict(field=field,quote='A B',occurrence=1)])

    def test_explicit_raw_mapped_profiles_and_exact_offsets(self):
        text = 'Hello A\\&B uses foo\\.'
        with self.assertRaises(ValueError):
            text_dates.exact_span(text, 'A&B', 1)
        with representation.selected(representation.PROFILE):
            for quote in ('Hello A&B uses foo.', 'A&B', 'foo.'):
                span = text_dates.exact_span(text, quote, 1)
                self.assertEqual(span['quote'], text[span['start']:span['end']])
                self.assertEqual(representation.mapped(span['quote'])[0],quote)
            with self.assertRaises(ValueError):
                text_dates.exact_span('A\\. A\\.', 'A.', 0)
            with self.assertRaises(ValueError):
                text_dates.exact_span('A\\. A\\.', 'A.', 3)
            with self.assertRaises(ValueError):
                text_dates.exact_span('foo\\. foo.', 'foo.', 0)
            self.assertEqual(text_dates.exact_span('foo\\. foo.','foo.',1)['start'],0)
            self.assertEqual(text_dates.exact_span('foo\\. foo.','foo.',2)['start'],6)
            for text,quote in [('<strong>foo</strong> bar','foo bar'),('A&amp;B','A&B'),('foo  bar','foo bar'),('Alice','Alicia')]:
                with self.assertRaises(ValueError):text_dates.exact_span(text,quote,1)
        with self.assertRaises(ValueError):text_dates.exact_span('foo\\.','foo.',1)

    def test_no_metadata_or_url_reconciliation(self):
        packet={'allowed_text':[], 'native_metadata':[{'field':'native','text':'A&amp;B'}], 'returned_url':''}
        with representation.selected(representation.PROFILE),self.assertRaises(ValueError):
            two_step.resolve_quotes(packet,[dict(field='native',quote='A&B',occurrence=1)])


class AdjudicationTests(unittest.TestCase):
    def setUp(self):
        fixture=test_execution.PolicyTests();fixture.setUp()
        self.compiled,self.raw,self.config=fixture.compiled,fixture.raw,fixture.config
        pos=decode_position(self.raw,self.compiled)
        pos.update(independent=True,prior_verdict_exposure=False)
        self.positions={'pro':deepcopy(pos),'codex':deepcopy(pos)}
        self.ids=adjudication.eligible(self.compiled,self.positions,self.config)
        self.assertEqual(self.ids,['N1'])
        self.packet=adjudication.packet_for(self.compiled,self.ids)
        field=self.compiled['packet']['allowed_text'][0]
        quote=dict(field=field['field'],quote=field['text'],occurrence=1)
        self.answer=dict(disposition='confirm',selected_native_date_ids=self.ids,
                         checks=[dict(check=k,status='pass',reason='Own fixed fictional report supports this requirement.',support=[quote])for k in adjudication.CHECKS],
                         contrary_reading='A changing collection date would not date this fixed report.',reason='Existing publication rule applies with all other predicates preserved.')
        self.responses={r:dict(verdict=deepcopy(self.answer),transport_success=True,native_completion_verified=True)for r in('codex','claude')}

    def test_blind_corroboration_preserves_original_positions(self):
        original=deepcopy(self.positions)
        adjudication.reject_exposure(self.packet['input'])
        effective,record=adjudication.corroborate(self.compiled,self.positions,self.packet,self.responses,self.config)
        self.assertTrue(record['accepted'],record)
        self.assertEqual(settle(effective['pro'],effective['codex'])['grade'],2)
        self.assertEqual(self.positions,original)
        self.assertIsNone(settle(original['pro'],original['codex'])['grade'])

    def test_disagreement_bad_span_missing_native_proof_and_wrong_field(self):
        for change in ('reject','bad_span','native','field','missing_check'):
            with self.subTest(change=change):
                responses=deepcopy(self.responses)
                answer=responses['claude']['verdict']
                if change=='reject':answer['disposition']='reject'
                if change=='bad_span':answer['checks'][0]['support'][0]['quote']='not present'
                if change=='native':responses['claude']['native_completion_verified']=False
                if change=='field':answer['selected_native_date_ids']=['N999']
                if change=='missing_check':answer['checks'].pop()
                effective,record=adjudication.corroborate(self.compiled,self.positions,self.packet,responses,self.config)
                self.assertFalse(record['accepted'])
                self.assertIsNone(settle(effective['pro'],effective['codex'])['grade'])

    def test_unapproved_date_mapping_and_nonpublication_gap_stay_ineligible(self):
        for change in ('mapping','gap','base'):
            compiled,positions=deepcopy(self.compiled),deepcopy(self.positions)
            if change=='mapping':
                compiled['input']['publication_estimate_applicability']=None
                compiled['input_sha256']=digest(compiled['input'])
            if change=='gap':positions['codex']['judgment']['candidates'][0]['conditions'].append(dict(predicate_id='source_rule',status='unknown'))
            if change=='base':positions['codex']['structural_base_grade']=0
            self.assertFalse(adjudication.eligible(compiled,positions,self.config))

    def test_pipeline_corrects_reports_and_resumes_without_new_call(self):
        with tempfile.TemporaryDirectory()as temp:
            root=Path(temp);cid=self.compiled['packet']['case_id'];q=deepcopy(self.compiled['packet']['question']);q.update(family_id='fixture-family',need_id='fixture-need')
            original=dict(case_id=cid,question_id=q['question_id'],configuration_id='valyu-search',rank=1,packet_sha256=self.compiled['packet_sha256'],**settle(self.positions['pro'],self.positions['codex']))
            case=dict(compiled=self.compiled,verdicts={'pro':self.raw,'codex':self.raw},original=original,provenance={},provider=self.config)
            write_private(root/'inputs/case.json',case)
            config={r:dict(backend=r+'-subscription',model='fixture',reasoning_effort='low',timeout_seconds=1)for r in('codex','claude')}
            jobs=[]
            for role in('codex','claude'):
                jobs.append(dict(task_id='adjudication-'+role+'-'+cid,stage='adjudication',role=role,case_id=cid,blind_packet=self.packet,kind='model',backend=role+'-subscription',backend_config=config[role],reservation_usd='0',reservation_basis='configured subscription; quota and time separate'))
            package=dict(profile=review_pipeline.PROFILE,mode='fixture',code=code_bindings(),assets=asset_bindings(),incremental_api_calls=0,
                         maximum_new_subscription_tasks=2,jobs=jobs,reviewers=config,cases=[dict(case_id=cid,path='inputs/case.json',sha256=digest((root/'inputs/case.json').read_bytes()))],
                         text_profile=representation.PROFILE,adjudication_profile=adjudication.PROOF_PROFILE,run_directory=str(root/'run'),scheduling={r+'-subscription':dict(concurrency=1,minimum_start_interval_seconds=0)for r in config},
                         source_collection=[dict(question_id=q['question_id'],configuration_id='valyu-search',status='success')],
                         providers=[dict(configuration_id='valyu-search')],questions=[q],coverage_target=.8,tie_order=['valyu-search'],source_method=4,source_report_sha256='source')
            write_private(root/'package.json',package)
            called=[]
            def call(job,folder):
                called.append(job['task_id']);return dict(self.responses[job['role']],dispatch_state='completed',complete=True)
            report=review_pipeline.run_review(root/'package.json',call=call)
            self.assertEqual(report['outcome_counts'],{'2':1})
            self.assertEqual(report['review_pipeline']['changes']['policy_recoveries'],1)
            self.assertEqual(len(called),2)
            replay=review_pipeline.run_review(root/'package.json',call=lambda *_:self.fail('resume resent a task'))
            self.assertEqual(replay['execution']['reused_tasks'],2)
            self.assertEqual(replay['outcome_counts'],report['outcome_counts'])
            self.assertIsNone(load(root/'inputs/case.json')['original']['grade'])
            altered=deepcopy(package);altered['jobs'][0]['blind_packet']['input']['prior_labels']=[2]
            with self.assertRaises(ValueError):review_pipeline.verify(altered,root)
            # Exercise automatic native-runtime restart separately from the
            # completion auditor (whose real sidecar checks have their own tests).
            native=deepcopy(package);native.update(mode='live',run_directory=str(root/'native-run'),automatic_recovery=deepcopy(POLICY),
                max_attempts_per_task=3,retry_policy=dict(http_statuses=[429,500],backoff_seconds=[0,0]),
                recovery={r+'-subscription':dict(sibling_rule('judge',1),cooldown_seconds=0,transient_pause_action='cooldown')for r in config})
            write_private(root/'native-package.json',native)
            instances=[];calls=[];answer=deepcopy(self.answer);permanent=[False]
            class Runtime:
                def __init__(self,*args,**kwargs):self.client=SimpleNamespace(failure=None,quota_evidence=[]);self.number=len(instances);instances.append(self)
                def __enter__(self):return self
                def __exit__(self,*args):pass
                def review(self,packet,folder):
                    calls.append('codex')
                    if self.number==0 or permanent[0]:
                        self.client.failure='Codex runtime exited'
                        return dict(transport_success=False,dispatch_state='uncertain',complete=False,fatal_configuration=True,recoverable_runtime_failure=not permanent[0])
                    return dict(transport_success=True,dispatch_state='completed',complete=True,verdict=answer)
            class Gate:
                def __init__(self,*args):pass
                def capacity(self,*args):return dict(stopped=False,ready_at=0)
            def subscription(config,packet,folder,preflight_only=False,**kwargs):
                if preflight_only:return dict(transport_success=True)
                calls.append('claude');return dict(transport_success=True,dispatch_state='completed',complete=True,verdict=answer)
            with patch('openweb_news.codex_runtime.CodexRuntime',Runtime),patch('openweb_news.quota.QuotaGate',Gate),patch('openweb_news.review_pipeline.backends.subscription',side_effect=subscription),patch('openweb_news.review_pipeline.bind_completion',side_effect=lambda j,r,f:{'test_proof':True}if r.get('transport_success')else None),patch('openweb_news.review_pipeline.verified_completion',side_effect=lambda j,r,f:r.get('subscription_completion_evidence')=={'test_proof':True}):
                result=review_pipeline.run_review(root/'native-package.json',approval=dict(approved=True,package_sha256=digest(native)))
            self.assertEqual(result['execution']['state'],'completed')
            self.assertEqual(result['outcome_counts'],{'2':1})
            self.assertEqual(calls.count('codex'),2)
            self.assertEqual(calls.count('claude'),1)
            self.assertEqual(len(instances),2)
            permanent[0]=True;instances.clear();calls.clear()
            fatal=deepcopy(native);fatal['run_directory']=str(root/'fatal-run');write_private(root/'fatal-package.json',fatal)
            with patch('openweb_news.codex_runtime.CodexRuntime',Runtime),patch('openweb_news.quota.QuotaGate',Gate),patch('openweb_news.review_pipeline.backends.subscription',side_effect=subscription),patch('openweb_news.review_pipeline.bind_completion',side_effect=lambda j,r,f:{'test_proof':True}if r.get('transport_success')else None),patch('openweb_news.review_pipeline.verified_completion',side_effect=lambda j,r,f:r.get('subscription_completion_evidence')=={'test_proof':True}):
                held=review_pipeline.run_review(root/'fatal-package.json',approval=dict(approved=True,package_sha256=digest(fatal)))
            self.assertEqual(held['execution']['state'],'completed_with_holds')
            self.assertEqual(held['execution']['permanent_stop_roles'],['codex'])
            self.assertEqual(calls.count('codex'),1)
            self.assertEqual(len(instances),1)


if __name__=='__main__':unittest.main()
