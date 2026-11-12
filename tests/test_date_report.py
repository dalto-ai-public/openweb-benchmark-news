from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest

from openweb_news import date_report,review_pipeline
from openweb_news.common import load,write_private,digest
from openweb_news._method import scoring
from openweb_news.checkpoint import COST_KEYS
from . import test_date_replay
import json
from contextlib import contextmanager


class DateReportTests(unittest.TestCase):
    def test_recollection_source_rejects_unaccepted_target_tamper_and_other_method_changes(self):
        from copy import deepcopy
        from openweb_news.request_profiles import MIXED_START_PROFILE
        with TemporaryDirectory()as temp:
            root=Path(temp)
            def ref(path):return dict(path=str(path),sha256=digest(path.read_bytes()))
            binding=dict(provider_ids=['brave-web','exa-highlights'],text_profile='unicode-spaces-v1',
                date_profile='quoted-date-reading-v4',semantic_code={'synthetic':'old'},method_options={})
            method=dict(profile_bindings=binding)
            write_private(root/'old-method/package.json',method)
            write_private(root/'old-method/approval.json',dict(approved=True,accept_method=True,
                package_sha256=digest(method),approved_by='synthetic owner',approved_at='fixture'))
            view=dict(questions=[],collection=[],phase_costs={},outcomes=[])
            write_private(root/'source.json',view);write_private(root/'view.json',view);write_private(root/'candidate.json',view)
            d=dict(profile='native-literal-repair-owner-disposition-v1',method=ref(root/'old-method/package.json'),
                source_report=ref(root/'source.json'),candidate_report=ref(root/'candidate.json'))
            write_private(root/'repair/package.json',d)
            approval=dict(approved=True,accept_corrected_method=True,accept_saved_response_repair=True,
                package_sha256=digest(d),approved_by='synthetic owner',approved_at='fixture')
            write_private(root/'repair/approval.json',approval)
            reference=date_report.recollection_reference(root/'view.json',root/'repair/package.json',[],[],[])
            target=dict(profile_bindings=deepcopy(binding))
            target['profile_bindings']['method_options']['provider_date_profiles']={'brave-web':MIXED_START_PROFILE}
            # This envelope intentionally has no native evidence. A valid
            # envelope reaches that specific guard; it cannot become a source.
            with self.assertRaisesRegex(ValueError,'lacks original native evidence'):
                date_report._recollection_evidence(reference,target)
            for key in ('text_profile','provider_ids'):
                altered=deepcopy(target);altered['profile_bindings'][key]='changed'
                with self.assertRaisesRegex(ValueError,'another selected method choice'):
                    date_report._recollection_evidence(reference,altered)
            changed=deepcopy(target);changed['profile_bindings']['method_options']['provider_date_profiles']={}
            with self.assertRaisesRegex(ValueError,'exactly the reviewed start-control'):
                date_report._recollection_evidence(reference,changed)
            for file in (root/'view.json',root/'source.json',root/'candidate.json',root/'repair/approval.json'):
                before=file.read_bytes();write_private(file,dict(tampered=True),replace=True)
                with self.assertRaisesRegex(ValueError,'source bytes changed'):
                    date_report._recollection_evidence(reference,target)
                write_private(file,before,replace=True)
            invalid=dict(approval,approved=False);write_private(root/'repair/approval.json',invalid,replace=True)
            changed=deepcopy(reference);changed['receipt']=ref(root/'repair/approval.json')
            with self.assertRaisesRegex(ValueError,'repair was not accepted'):
                date_report._recollection_evidence(changed,target)
            # Public authority verification must fail before any source replay.
            from openweb_news import profile_authority
            with patch.object(profile_authority,'read',side_effect=ValueError('target not accepted')):
                target.update(package_path='synthetic-unaccepted',profile_bindings=dict(target['profile_bindings'],reviewers={}))
                with self.assertRaisesRegex(ValueError,'target not accepted'):
                    date_report.recollection_source(reference,target)

    def test_historical_loader_recomputes_results_and_rejects_semantic_drift(self):
        with TemporaryDirectory()as temp:
            root=Path(temp);args=self.source(root)
            p=date_report.prepare(**args,output=root/'rebuild',maximum_new_third=0,mode='fixture')
            native=root/'native.json';write_private(native,{})
            p.update(mode='live',native_package_path=str(native.resolve()),native_package_sha256=digest(native.read_bytes()),native_packages=[])
            write_private(root/'rebuild/package.json',p,replace=True)
            receipt=dict(approved=True,package_sha256=digest(p),approved_by='synthetic',approved_at='2000-01-01T00:00:00Z')
            expected=date_report.run(root/'rebuild/package.json',receipt)
            changed=dict(p['code'],**{'new_orchestration.py':'different'})
            path=root/'rebuild/report/report.json';before=path.read_bytes();original_build=date_report._build
            # Native-authenticity is tested against real saved evidence separately;
            # this control isolates the historical replay and its semantic oracle.
            with patch('openweb_news.source_audit.audit',return_value=dict(fixture=True)),\
                 patch('openweb_news.date_report.code_bindings',return_value=changed),\
                 patch('openweb_news.date_replay.code_bindings',return_value=changed),\
                 patch('openweb_news.review_pipeline.review_execution',side_effect=AssertionError('no dispatch')):
                report,_,_=date_report.load_completed(path)
                self.assertEqual(report['outcomes'],expected['outcomes'])
                for kind in ('outcomes','scores','active_review_profile'):
                    def drift(*args,**kwargs):
                        result=original_build(*args,**kwargs)
                        if kind=='outcomes':result[kind][0]['grade']=0
                        else:result[kind]['unexpected']='changed semantics'
                        return result
                    with self.subTest(kind=kind),patch('openweb_news.date_report._build',side_effect=drift):
                        with self.assertRaisesRegex(ValueError,'does not replay'):date_report.load_completed(path)
                with self.assertRaises(ValueError):date_report.run(root/'rebuild/package.json',receipt)
            self.assertEqual(path.read_bytes(),before)

    def test_space_adoption_reuses_saved_third_without_dispatch_and_historical_read_is_not_live(self):
        with TemporaryDirectory()as temp:
            root=Path(temp);args=self.source(root)
            p=date_report.prepare(**args,output=root/'first',maximum_new_third=0,mode='fixture')
            approval=dict(approved=True,package_sha256=digest(p),approved_by='synthetic',approved_at='2000-01-01T00:00:00Z')
            date_report.run(root/'first/package.json',approval)
            source=root/'first/report/report.json';before=source.read_bytes()
            adopted=date_report.prepare(**args,output=root/'second',maximum_new_third=0,mode='fixture',
                text_profile='unicode-spaces-v1',reused_third_package=root/'first/package.json',
                comparison_report=source,expected_comparison_sha256=digest(before))
            receipt=dict(approval,package_sha256=digest(adopted))
            with patch('openweb_news.review_pipeline.review_execution',side_effect=AssertionError('no dispatch')):
                result=date_report.run(root/'second/package.json',receipt)
            self.assertEqual(result['active_review_profile']['text_profile'],'unicode-spaces-v1')
            self.assertEqual(source.read_bytes(),before);self.assertEqual(result['execution']['new_third_tasks'],0)
            with patch('openweb_news.date_report.code_bindings',return_value={'changed.py':'new'}):
                date_report.verify(root/'second/package.json',historical=True)
                with self.assertRaises(ValueError):date_report.verify(root/'second/package.json')
                with self.assertRaises(ValueError):date_report.run(root/'second/package.json',receipt)
            old=load(source);old['outcomes'][0]['grade']=2;write_private(source,old,replace=True)
            with self.assertRaisesRegex(ValueError,'known comparison'):
                date_report.prepare(**args,output=root/'bad',maximum_new_third=0,mode='fixture',
                    comparison_report=source,expected_comparison_sha256=digest(source.read_bytes()))
            self.assertFalse((root/'bad/package.json').exists())

    def source(self,root):
        args=test_date_replay.DateReplayTests().setup_source(root)
        package=load(root/'pipeline/package.json');case=load(root/'pipeline/inputs/case.json')
        cid=case['compiled']['packet']['case_id'];provider=case['provider']['configuration_id'];q=case['compiled']['packet']['question']
        package.update(coverage_target=1.0,tie_order=[provider],reviewers={'claude':{'backend':'claude-subscription','model':'synthetic'}},scheduling={},recovery={},retry_policy={},max_attempts_per_task=1)
        write_private(root/'pipeline/package.json',package,replace=True)
        positions,replay=review_pipeline.decode(case,package['text_profile'])
        o=dict(replay,case_id=cid,packet_sha256=case['compiled']['packet_sha256'],question_id=q['question_id'],configuration_id=provider,rank=1)
        cell={(provider,q['question_id']):dict(status='success',ranked=[dict(rank=1,evidence_id=cid,url=case['compiled']['packet']['returned_url'])])}
        r=dict(mode='fixture',method_version='synthetic',questions=[q],outcomes=[o],collection=[dict(configuration_id=provider,question_id=q['question_id'],status='success')],phase_costs={k:'0'for k in COST_KEYS},report_limits=[],publication_review=dict(records=[]))
        r['scores']=scoring.score([q],[provider],cell,{cid:None},{cid:'unknown'},1.0,[provider])
        write_private(args['source_report'],r,replace=True);args['expected_source_sha256']=digest(args['source_report'].read_bytes());args.pop('directory')
        return args

    def test_report_only_rebuild_is_replayable_without_dispatch_or_cost_change(self):
        with TemporaryDirectory()as temp:
            root=Path(temp);args=self.source(root);before=args['source_report'].read_bytes()
            p=date_report.prepare(**args,output=root/'rebuild',maximum_new_third=0,mode='fixture')
            receipt=dict(approved=True,package_sha256=digest(p),approved_by='synthetic-test',approved_at='2000-01-01T00:00:00Z')
            with patch('openweb_news.review_pipeline.review_execution',side_effect=AssertionError('no dispatch')):
                report=date_report.run(root/'rebuild/package.json',receipt)
                resumed=date_report.run(root/'rebuild/package.json',receipt)
            self.assertEqual(report['outcome_counts'],{'unknown':1});self.assertEqual(report['phase_costs'],{k:'0'for k in COST_KEYS})
            self.assertEqual(resumed['scores'],json.loads(json.dumps(report['scores'])));self.assertEqual(args['source_report'].read_bytes(),before)
            self.assertTrue((root/'rebuild/report/report.html').exists())
            with self.assertRaisesRegex(ValueError,'fixture rebuild'):date_report.load_completed(root/'rebuild/report/report.json')

    def test_unapproved_or_changed_rebuild_rejects_before_execution(self):
        with TemporaryDirectory()as temp:
            root=Path(temp);args=self.source(root);p=date_report.prepare(**args,output=root/'rebuild',maximum_new_third=0,mode='fixture')
            with self.assertRaisesRegex(ValueError,'approval'):date_report.run(root/'rebuild/package.json',{})
            p['jobs']=[dict(role='pro')];write_private(root/'rebuild/package.json',p,replace=True)
            with self.assertRaises(ValueError):date_report.verify(root/'rebuild/package.json')

    def test_nonempty_roster_is_blind_bounded_and_resume_does_not_dispatch(self):
        with TemporaryDirectory()as temp:
            root=Path(temp);args=self.source(root)
            source=load(args['source_report']);source['outcomes'][0]['grade']=0
            write_private(args['source_report'],source,replace=True);args['expected_source_sha256']=digest(args['source_report'].read_bytes())
            # Isolate the controller's routing from date-decoding tests: the saved
            # known grade becomes unknown and needs exactly one independent third.
            original=date_report.date_replay.replay
            def replay(**kw):
                if kw.get('third_package'):
                    write_private(Path(kw['directory'])/'summary.json',dict(controller_fixture=True));return {}
                report=load(args['source_report']);report['outcomes'][0]['grade']=None
                write_private(args['source_report'],report,replace=True)
                changed=dict(kw,expected_source_sha256=digest(args['source_report'].read_bytes()))
                try:return original(**changed)
                finally:write_private(args['source_report'],source,replace=True)
            with patch('openweb_news.date_report.date_replay.replay',side_effect=replay),patch('openweb_news.date_report.needs_third',return_value=True):
                p=date_report.prepare(**args,output=root/'rebuild',maximum_new_third=1,mode='fixture')
            self.assertEqual(len(p['jobs']),1);job=p['jobs'][0]
            self.assertEqual((job['role'],job['stage'],job['reservation_usd']),('claude','third','0'))
            self.assertNotIn('verdicts',job['blind_packet']);self.assertEqual(date_report.verify(root/'rebuild/package.json'),p)
            receipt=dict(approved=True,package_sha256=digest(p),approved_by='synthetic-test',approved_at='2000-01-01T00:00:00Z')
            calls=[]
            source['package_sha256']=digest(p)
            source['execution']=dict(state='completed');source['outcome_counts']={'0':1}
            @contextmanager
            def execution(package):
                calls.append(package['jobs']);yield ({job['task_id']:dict(native_completion_verified=True)},set(),[],0,root/'rebuild/run')
            def write(directory,value):write_private(directory/'report.json',value)
            with patch('openweb_news.review_pipeline.review_execution',execution),patch('openweb_news.date_report.date_replay.replay',side_effect=replay),patch('openweb_news.date_report._build',return_value=source),patch('openweb_news.date_report.write_reports',side_effect=write):
                date_report.run(root/'rebuild/package.json',receipt)
                date_report.run(root/'rebuild/package.json',receipt)
            self.assertEqual(len(calls),1)

    def test_live_source_audit_failure_prevents_package_or_dispatch(self):
        with TemporaryDirectory()as temp:
            root=Path(temp);args=self.source(root)
            with patch('openweb_news.source_audit.audit',side_effect=ValueError('invalid native proof')),patch('openweb_news.review_pipeline.review_execution',side_effect=AssertionError('no dispatch')):
                with self.assertRaisesRegex(ValueError,'invalid native proof'):
                    date_report.prepare(**args,output=root/'rebuild',native_packages=[root/'native.json'])
            self.assertFalse((root/'rebuild/package.json').exists())


class CheckerOnlyRepairTests(unittest.TestCase):
    """Synthetic transition proof; native authenticity is covered by source audit."""
    def source(self, root):
        from copy import deepcopy
        from . import test_native_date_reading
        from openweb_news import judging,publication,profile_authority,representation,settlement
        from openweb_news.review import settle
        from openweb_news.common import code_bindings
        f=test_native_date_reading.NativeDateReadingTests();f.setUp()
        config,compiled,_,value,review=f.case()
        packet=deepcopy(compiled['packet']);packet['question'].update(family_id='synthetic-family',need_id='synthetic-need')
        compiled=judging.compile_packet(packet,compiled['input']['captured_at'],config=config)
        cid=compiled['packet']['case_id'];q=compiled['packet']['question'];provider=config['configuration_id']
        raw=f.helper.model_value(compiled,value)
        positions={r:judging.decode_position(raw,compiled,date_profile='quoted-date-reading-v3')for r in ('pro','codex')}
        for pos in positions.values():pos.update(independent=True,prior_verdict_exposure=False,backend_observation=None)
        outcome=dict(settle(positions['pro'],positions['codex']),case_id=cid,question_id=q['question_id'],
                     configuration_id=provider,rank=1,packet_sha256=compiled['packet_sha256'])
        case=dict(compiled=compiled,provider=config,verdicts={r:deepcopy(raw)for r in positions},
                  original=dict(positions=positions,grade=None))
        pipeline=root/'pipeline';write_private(pipeline/'inputs/case.json',case);write_private(pipeline/'run/attempts.jsonl',b'')
        reviewers={r:dict(backend=r+'-subscription',model='synthetic',reasoning_effort='high')for r in ('codex','claude')}
        ordinary=dict(profile=review_pipeline.PROFILE,text_profile=representation.SPACE_PROFILE,jobs=[],
            cases=[dict(case_id=cid,path='inputs/case.json',sha256=digest((pipeline/'inputs/case.json').read_bytes()))],
            coverage_target=1,tie_order=[provider],reviewers=reviewers)
        write_private(pipeline/'package.json',ordinary)
        pubroot=root/'publication';packet=publication.packet_for(compiled)
        model=f.helper.model_value(compiled,review,publication.packet_for(compiled,_source=True))
        own={r:dict(verdict=deepcopy(model),transport_success=True,native_completion_verified=True)for r in reviewers}
        old=publication.corroborate(compiled,packet,own,checker_profile=publication.READING_PROFILE,date_profile='quoted-date-reading-v3')
        self.assertFalse(old['accepted'])
        provenance={r:dict(native_completion_verified=True,input_sha256=packet['input_sha256'],response_sha256=digest(response))for r,response in own.items()}
        record=dict(old,case_id=cid,configuration_id=provider,checker_profile=publication.READING_PROFILE,
            date_profile='quoted-date-reading-v3',text_profile=representation.SPACE_PROFILE,terminal_key='old-v3-key',provenance=provenance)
        write_private(pubroot/'inputs/case.json',case);write_private(pubroot/'run/attempts.jsonl',b'')
        write_private(pubroot/'package.json',dict(cases=[dict(case_id=cid,path='inputs/case.json',sha256=digest((pubroot/'inputs/case.json').read_bytes()))],
            jobs=[dict(task_id='publication-'+r+'-'+cid,role=r,blind_packet=packet)for r in reviewers]))
        cells={(provider,q['question_id']):dict(status='success',ranked=[dict(rank=1,evidence_id=cid,url=compiled['packet']['returned_url'])])}
        report=dict(mode='fixture',method_version='synthetic',questions=[q],outcomes=[outcome],
            collection=[dict(configuration_id=provider,question_id=q['question_id'],status='success')],
            phase_costs={k:'7.50'for k in COST_KEYS},report_limits=[],publication_review=dict(records=[record]),review_pipeline=dict(final_unknowns=1))
        report['scores']=scoring.score([q],[provider],cells,{cid:None},{cid:'synthetic'},1,[provider])
        source=root/'source/report.json';write_private(source,report)
        method=dict(profile=profile_authority.METHOD_PROFILE,mode='fixture',output_directory=str(root/'method'),
            method_options={},profile_bindings=dict(checker_profile=publication.READING_PROFILE,date_profile='quoted-date-reading-v4',
                text_profile=representation.SPACE_PROFILE,semantic_code=code_bindings(),reviewers=reviewers))
        write_private(root/'method/package.json',method)
        mr=dict(approved=True,accept_method=True,package_sha256=digest(method),approved_by='synthetic',approved_at='fixture')
        write_private(root/'method/approval.json',mr)
        authority=dict(profile=method['profile'],mode='fixture',package_path=str(root/'method/package.json'),
            package_sha256=digest(method),approval_sha256=digest(mr),profile_bindings=method['profile_bindings'],method_options={})
        d=dict(profile='native-literal-repair-owner-disposition-v1',source_report=dict(path=str(source),sha256=digest(source.read_bytes())),
            method=dict(path=str(root/'method/package.json'),sha256=digest((root/'method/package.json').read_bytes())),
            repaired_cases=[dict(case_id=cid,before=None,after=2,packet_sha256=compiled['packet_sha256'],
                compiled_input_sha256=compiled['input_sha256'],publication_packet_sha256=digest(packet),
                response_sha256={r:digest(response)for r,response in own.items()})])
        write_private(root/'disposition/package.json',d)
        dr=dict(approved=True,accept_corrected_method=True,accept_saved_response_repair=True,package_sha256=digest(d),approved_by='synthetic',approved_at='fixture')
        write_private(root/'disposition/approval.json',dr)
        args=dict(source_report=source,review_packages=[pipeline/'package.json'],publication_packages=[pubroot/'package.json'],
            expected_source_sha256=digest(source.read_bytes()),maximum_new_third=0,mode='fixture',
            date_profile='quoted-date-reading-v4',baseline_date_profile='quoted-date-reading-v3',
            accepted_profile=root/'method/package.json',repair_disposition=root/'disposition/package.json')
        responses={'publication-'+r+'-'+cid:response for r,response in own.items()}
        return args,authority,responses,cid,compiled

    def test_saved_terminal_pair_is_rebuilt_without_calls_and_retains_costs(self):
        from openweb_news import settlement
        with TemporaryDirectory()as temp:
            root=Path(temp).resolve();args,authority,responses,cid,compiled=self.source(root)
            before=args['source_report'].read_bytes()
            with patch('openweb_news.profile_authority.read',return_value=authority),\
                 patch('openweb_news.date_replay._responses',side_effect=lambda p,r:responses if r==root/'publication'else {}),\
                 patch('openweb_news.review_pipeline.review_execution',side_effect=AssertionError('no new dispatch')),\
                 patch('socket.socket.connect',side_effect=AssertionError('no network')):
                p=date_report.prepare(**args,output=root/'rebuild')
                self.assertEqual(p['jobs'],[])
                receipt=dict(approved=True,package_sha256=digest(p),approved_by='synthetic',approved_at='fixture')
                result=date_report.run(root/'rebuild/package.json',receipt)
                self.assertEqual(result['phase_costs'],load(args['source_report'])['phase_costs'])
                record=result['publication_review']['records'][0]
                self.assertTrue(record['accepted']);self.assertTrue(record['terminal']);self.assertEqual(record['grade'],2)
                self.assertEqual(record['source_terminal_key'],'old-v3-key')
                self.assertEqual(record['terminal_key'],settlement.identity(compiled,p['reviewers'],authority=authority))
                self.assertEqual(result['active_review_profile'],authority)
                self.assertEqual(result['review_pipeline']['final_unknowns'],0)
                self.assertEqual(date_report.run(root/'rebuild/package.json',receipt)['outcomes'],result['outcomes'])
            self.assertEqual(args['source_report'].read_bytes(),before)

    def test_missing_saved_pair_keeps_new_ordinary_review_requirement(self):
        from openweb_news import date_replay
        with TemporaryDirectory()as temp:
            root=Path(temp).resolve();args,_,_,cid,_=self.source(root)
            report=load(args['source_report']);report['publication_review']['records']=[]
            write_private(args['source_report'],report,replace=True)
            with patch('openweb_news.date_replay._responses',return_value={}):
                result=date_replay.replay(args['source_report'],args['review_packages'],[],root/'diagnostic',
                    expected_source_sha256=digest(args['source_report'].read_bytes()),date_profile=args['date_profile'],
                    baseline_date_profile=args['baseline_date_profile'])
            self.assertEqual(result['unresolved_review_requirements'],1)
            needs=load(root/'diagnostic/unresolved-review-requirements.json')
            self.assertEqual(needs[0]['case_id'],cid)
            self.assertIn('newly eligible adjudication',needs[0]['needs'][0])
            self.assertEqual(result['outcome_counts']['combined'],{'unknown':1})

    def test_repair_rejects_wrong_input_response_and_unlisted_changes(self):
        from copy import deepcopy
        for kind in ('input','response','case_roster'):
            with self.subTest(kind=kind),TemporaryDirectory()as temp:
                root=Path(temp).resolve();args,authority,responses,_,_=self.source(root)
                d=load(args['repair_disposition'])
                if kind=='input':d['repaired_cases'][0]['compiled_input_sha256']='wrong'
                elif kind=='response':d['repaired_cases'][0]['response_sha256']['codex']='wrong'
                else:d['repaired_cases']=[]
                write_private(args['repair_disposition'],d,replace=True)
                receipt=load(root/'disposition/approval.json');receipt['package_sha256']=digest(d)
                write_private(root/'disposition/approval.json',receipt,replace=True)
                with patch('openweb_news.profile_authority.read',return_value=authority),\
                     patch('openweb_news.date_replay._responses',side_effect=lambda p,r:responses if r==root/'publication'else {}):
                    with self.assertRaisesRegex(ValueError,'repair'):
                        date_report.prepare(**args,output=root/'rebuild')
