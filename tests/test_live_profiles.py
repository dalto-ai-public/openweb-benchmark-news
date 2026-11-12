"""Offline profile authority and automated native/review integration proof."""
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from openweb_news import batch,checkpoint,fixtures,judging,profile_authority,review_pipeline,settlement,runner
from openweb_news.common import digest,encoded,load,write_private
from openweb_news.preparation import prepare
from openweb_news.runner import run
from . import test_batch,test_publication


class LiveProfileTests(TestCase):
    def test_live_authority_requires_own_receipt_and_current_semantics(self):
        with TemporaryDirectory()as temp:
            root=Path(temp).resolve();parent,cases,native,source,_,_,_=test_batch.BatchTests().setup_parent(root)
            b=profile_authority.live_bindings(native['configuration']['reviewers'])
            parent['active_review_profile']={k:b[k]for k in ('checker_profile','date_profile','text_profile')}
            write_private(source,parent,replace=True)
            with patch.object(settlement,'load_parent',return_value=(parent,cases,native)):
                p=profile_authority.prepare_live(source,root/'authority')
                self.assertFalse(load(root/'authority/approval-template.json')['approved'])
                self.assertEqual(p['profile_bindings']['selection_profile'],profile_authority.POLICY_SELECTION)
                with self.assertRaises(FileNotFoundError):profile_authority.read(root/'authority/package.json',b['reviewers'])
                receipt=dict(approved=True,package_sha256=digest(p),approved_by='synthetic-owner',approved_at='synthetic',accept_live_profiles=False)
                write_private(root/'authority/approval.json',receipt)
                with self.assertRaises(ValueError):profile_authority.read(root/'authority/package.json',b['reviewers'])
                receipt['accept_live_profiles']=True;write_private(root/'authority/approval.json',receipt,replace=True)
                authority=profile_authority.read(root/'authority/package.json',b['reviewers'])
                self.assertEqual(authority['source_report_sha256'],digest(source.read_bytes()))
                current=profile_authority.code_bindings()
                for module in ('date_reading.py','publication_reading.py','settlement.py','_method/availability.py'):
                    with self.subTest(module=module),patch.object(profile_authority,'code_bindings',return_value=dict(current,**{module:'changed'})):
                        with self.assertRaises(ValueError):profile_authority.read(root/'authority/package.json',b['reviewers'])
                self.assertEqual(profile_authority.profiles(dict(profile_bindings=profile_authority.bindings(b['reviewers']))),{})

    def test_native_date_reader_and_primary_envelope_are_bound(self):
        fixture=test_publication.PublicationTests();fixture.setUp()
        profiles=profile_authority.profiles(dict(profile_bindings=profile_authority.live_bindings({})))
        compiled=fixture.compiled;before=deepcopy(compiled)
        native=dict(modelVersion=judging.METHOD['primary']['model'],candidates=[dict(finishReason='STOP',content=dict(parts=[dict(text=encoded(fixture.raw).decode())]))])
        pro=dict(raw_text=encoded(native).decode(),http_status=200,complete=True,transport_success=True)
        second=dict(verdict=fixture.raw,transport_success=True)
        for role,raw in [('pro',pro),('codex',second),('claude',second)]:
            self.assertNotIn('date_comparisons',runner.decode_native(role,raw,compiled,{}))
            bound=runner.decode_native(role,raw,compiled,profiles)
            self.assertIn('date_comparisons',bound)
            expected=review_pipeline.decode_value(fixture.raw,compiled,profiles['text_profile'],date_profile=profiles['date_profile'])
            self.assertEqual(bound['date_comparisons'],expected['date_comparisons'])
            self.assertEqual(bound['state'],expected['state'])
        native['modelVersion']='wrong-model';pro['raw_text']=encoded(native).decode()
        self.assertEqual(runner.decode_native('pro',pro,compiled,profiles)['state'],'unjudged')
        self.assertEqual(compiled,before)

    def test_conditional_third_uses_bound_date_and_text_profiles_and_resumes(self):
        with TemporaryDirectory()as temp:
            root=Path(temp).resolve();cfg=fixtures.config(root/'native/run')
            cfg['questions']=[dict(cfg['questions'][0],question_id='synthetic-disagreement')]
            authority=dict(package_path=str(root/'synthetic-authority/package.json'),
                package_sha256='synthetic-package',approval_sha256='synthetic-receipt',
                profile_bindings=profile_authority.live_bindings(cfg['reviewers']))
            cfg['live_profile_authority']=authority;profiles=profile_authority.profiles(authority)
            package=prepare(cfg,mode='fixture')
            original_search=fixtures.search;original_model=fixtures.model;thirds={};calls=[]
            def search(request,provider,folder):
                raw=original_search(request,provider,folder)
                raw['raw_text']=raw['raw_text'].replace('fictional Aster','fictional\u00a0Aster')
                return raw
            def model(role,compiled,folder):
                calls.append((role,compiled['packet']['case_id']))
                raw=original_model(role,compiled,folder)
                if role=='claude':
                    raw['verdict']=json.loads(encoded(raw['verdict']).decode().replace('\u00a0',' '))
                    thirds[compiled['packet']['case_id']]=(deepcopy(raw),deepcopy(compiled))
                return raw
            with patch.object(profile_authority,'read',return_value=authority),patch.object(fixtures,'search',side_effect=search),patch.object(fixtures,'model',side_effect=model):
                report=run(package)
            self.assertEqual(report['live_profiles'],profiles)
            self.assertEqual(len(thirds),len(report['outcomes']))
            self.assertGreater(len(thirds),0)
            for outcome in report['outcomes']:
                self.assertTrue(outcome['third_required']);self.assertTrue(outcome['third_obtained'])
                self.assertEqual(outcome['states'],dict(pro='conforming_numeric',codex='conforming_numeric',claude='conforming_numeric'))
                position=outcome['positions']['claude']
                self.assertEqual(position['grade'],1)
                self.assertIn('date_comparisons',position)
                self.assertTrue(position['quote_reconciliations'])
                raw,compiled=thirds[outcome['case_id']]
                self.assertEqual(judging.decode_position(raw['verdict'],compiled)['state'],'invalid')
                expected=review_pipeline.decode_value(raw['verdict'],compiled,profiles['text_profile'],date_profile=profiles['date_profile'])
                self.assertEqual(position['date_comparisons'],expected['date_comparisons'])
                self.assertEqual(position['quote_reconciliations'],expected['quote_reconciliations'])
                self.assertEqual(sum(role=='claude'and cid==outcome['case_id']for role,cid in calls),1)
            with patch.object(profile_authority,'read',return_value=authority),patch.object(fixtures,'search',side_effect=AssertionError('no resume search')),patch.object(fixtures,'model',side_effect=AssertionError('no resume model')):
                resumed=run(package)
            self.assertEqual(resumed['outcomes'],report['outcomes'])

    def test_bound_batch_runs_first_pass_review_report_and_resume_offline(self):
        with TemporaryDirectory()as temp:
            root=Path(temp).resolve();parent,cases,native,source,selection,authority,_=test_batch.BatchTests().setup_parent(root)
            authority.update(package_sha256='synthetic-authority',source_report_sha256=digest(source.read_bytes()),profile_bindings=profile_authority.live_bindings(native['configuration']['reviewers']))
            profiles=profile_authority.profiles(authority)
            original_loader=settlement.load_parent;original_plan=batch.native_plan
            def loader(path,**kwargs):return(parent,cases,native)if Path(path).resolve()==source.resolve()else original_loader(path,**kwargs)
            def fixture_plan(*args,**kwargs):
                cfg,_=original_plan(*args,**kwargs);return cfg,prepare(cfg,mode='fixture')
            original_search=fixtures.search;original_model=fixtures.model;outputs={}
            def search(request,provider,folder):
                raw=original_search(request,provider,folder)
                raw['raw_text']=raw['raw_text'].replace('fictional Aster','fictional\u00a0Aster')
                return raw
            def model(role,compiled,folder):
                raw=original_model(role,compiled,folder)
                if role=='pro':raw['raw_text']=raw['raw_text'].replace('\\u00a0',' ' ).replace('\u00a0',' ')
                else:
                    raw['verdict']=json.loads(encoded(raw['verdict']).decode().replace('\\u00a0',' ').replace('\u00a0',' '))
                outputs[role,compiled['packet']['case_id']]=deepcopy(raw)
                return raw
            with patch.object(settlement,'load_parent',side_effect=loader),patch.object(profile_authority,'read',return_value=authority),patch.object(batch,'native_plan',side_effect=fixture_plan):
                p=batch.prepare(source,selection,1,root/'batch',ceiling='600',accepted_profile=authority['package_path'])
                self.assertEqual(p['live_profiles'],profiles)
                child=load(root/'batch/native/package.json')
                self.assertEqual(child['configuration']['live_profile_authority'],authority)
                with patch.object(fixtures,'search',side_effect=search),patch.object(fixtures,'model',side_effect=model):
                    native_report=run(child)
                self.assertEqual(native_report['live_profiles'],profiles)
                self.assertTrue(all(o['grade']==2 for o in native_report['outcomes']))
                for outcome in native_report['outcomes']:
                    compiled=load(root/'batch/native/run/compiled'/(outcome['case_id']+'.json'))
                    raw=review_pipeline._verdict('pro',outputs['pro',outcome['case_id']])
                    self.assertEqual(review_pipeline.decode_value(raw,compiled,'raw-v1')['state'],'invalid')
                    self.assertTrue(outcome['positions']['pro']['quote_reconciliations'])
                self.assertEqual(native_report['execution']['review_work']['conditional_third_required'],0)
                with patch.object(fixtures,'model',side_effect=AssertionError('native resume model')),patch.object(fixtures,'search',side_effect=AssertionError('native resume search')):
                    resumed=run(child)
                self.assertEqual(resumed['outcomes'],native_report['outcomes'])
                approval=dict(decision='approved',package_sha256=digest(p),approved_by='synthetic-owner',approved_at='synthetic')
                with patch.object(checkpoint,'_finish_accepted',return_value={'state':'completed'}),patch.object(checkpoint,'run',side_effect=AssertionError('native redispatch')):
                    checkpoint.run_checkpoint(root/'batch/checkpoint.json',approval)
                    review=load(root/'batch/review/package.json')
                    self.assertEqual(review['date_profile'],profiles['date_profile'])
                    self.assertEqual(review['text_profile'],profiles['text_profile'])
                    self.assertEqual(review['jobs'],[])
                    report=load(root/'batch/report/report.json')
                    self.assertEqual(report['live_profiles'],profiles)
                    self.assertEqual(report['outcomes'][:len(parent['outcomes'])],parent['outcomes'])
                    self.assertEqual(report['publication_review'],parent['publication_review'])
                    snapshot=(root/'batch/report/report.json').read_bytes()
                    with patch.object(fixtures,'model',side_effect=AssertionError('resume model')),patch.object(fixtures,'search',side_effect=AssertionError('resume search')):
                        checkpoint.run_checkpoint(root/'batch/checkpoint.json',approval)
                    self.assertEqual((root/'batch/report/report.json').read_bytes(),snapshot)
                    batch.load_completed(root/'batch/report/report.json')
                with self.assertRaises(ValueError):review_pipeline.prepare_review(root/'batch/native/package.json',[q['question_id']for q in p['new_questions']],root/'wrong',text_profile='raw-v1')
