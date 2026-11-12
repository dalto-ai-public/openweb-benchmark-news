import json
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest

from openweb_news import date_replay,judging,representation,review_pipeline,publication
from openweb_news.common import digest,write_private,load
from . import test_publication


class DateReplayTests(unittest.TestCase):
    def setup_source(self,root):
        f=test_publication.PublicationTests();f.setUp();compiled=f.compiled
        position=judging.decode_position(f.raw,compiled);position.update(independent=True,prior_verdict_exposure=False)
        case=dict(compiled=compiled,provider=f.provider,verdicts={r:deepcopy(f.raw)for r in ('pro','codex')},original=dict(grade=None,positions={r:deepcopy(position)for r in ('pro','codex')}))
        pkgroot=root/'pipeline';file=pkgroot/'inputs/case.json';write_private(file,case)
        cid=compiled['packet']['case_id']
        write_private(pkgroot/'run/attempts.jsonl',b'')
        p=dict(profile=review_pipeline.PROFILE,text_profile=representation.PROFILE,jobs=[],cases=[dict(case_id=cid,path='inputs/case.json',sha256=digest(file.read_bytes()))])
        write_private(pkgroot/'package.json',p)
        report=dict(outcomes=[dict(case_id=cid,packet_sha256=compiled['packet_sha256'],question_id=compiled['packet']['question']['question_id'],configuration_id=f.provider['configuration_id'],grade=None)],publication_review=dict(records=[]))
        source=root/'source/report.json';write_private(source,report)
        return dict(source_report=source,review_packages=[pkgroot/'package.json'],publication_packages=[],directory=root/'diagnostic',expected_source_sha256=digest(source.read_bytes()))

    def test_replay_is_offline_bound_and_never_adopts(self):
        with TemporaryDirectory()as temp:
            args=self.setup_source(Path(temp));before=args['source_report'].read_bytes()
            with patch('socket.socket.connect',side_effect=AssertionError('network forbidden')),patch('subprocess.Popen',side_effect=AssertionError('process forbidden')):
                summary=date_replay.replay(**args)
            self.assertEqual(summary['cases'],1);self.assertEqual(summary['model_calls'],0);self.assertFalse(summary['adopted'])
            self.assertEqual(args['source_report'].read_bytes(),before)
            self.assertEqual(summary['outcome_counts']['baseline'],{'unknown':1})
            with self.assertRaisesRegex(ValueError,'fresh diagnostic'):date_replay.replay(**args)

    def test_tamper_missing_coverage_and_wrong_baseline_fail_closed(self):
        for kind in ('source_hash','case_hash','coverage','baseline'):
            with self.subTest(kind=kind),TemporaryDirectory()as temp:
                root=Path(temp);args=self.setup_source(root)
                if kind=='source_hash':args['expected_source_sha256']='0'*64
                if kind=='case_hash':(root/'pipeline/inputs/case.json').write_text('{}')
                if kind=='coverage':
                    p=load(root/'pipeline/package.json');p['cases']=[];write_private(root/'pipeline/package.json',p,replace=True)
                if kind=='baseline':
                    r=load(args['source_report']);r['outcomes'][0]['grade']=2;write_private(args['source_report'],r,replace=True);args['expected_source_sha256']=digest(args['source_report'].read_bytes())
                with self.assertRaises(ValueError):date_replay.replay(**args)
                self.assertFalse(args['directory'].exists())

    def test_third_review_must_match_canonical_case(self):
        with TemporaryDirectory()as temp:
            root=Path(temp);args=self.setup_source(root)
            p=load(root/'pipeline/package.json');case=load(root/'pipeline/inputs/case.json');cid=case['compiled']['packet']['case_id']
            task='third-claude-'+cid;p['jobs']=[dict(task_id=task,blind_packet=dict(wrong='input'))]
            raw={task:dict(verdict=case['verdicts']['pro'])}
            with self.assertRaisesRegex(ValueError,'canonical case'):date_replay._case_grade(case,p,raw)

    def test_publication_review_must_match_canonical_case(self):
        with TemporaryDirectory()as temp:
            root=Path(temp);args=self.setup_source(root)
            case=load(root/'pipeline/inputs/case.json');cid=case['compiled']['packet']['case_id']
            r=load(args['source_report']);r['publication_review']['records']=[dict(case_id=cid)];write_private(args['source_report'],r,replace=True)
            args['expected_source_sha256']=digest(args['source_report'].read_bytes())
            pub=root/'publication';file=pub/'inputs/case.json';write_private(file,case);write_private(pub/'run/attempts.jsonl',b'')
            p=dict(jobs=[dict(task_id='publication-'+role+'-'+cid,role=role,blind_packet=dict(wrong='input'))for role in ('codex','claude')],cases=[dict(case_id=cid,path='inputs/case.json',sha256=digest(file.read_bytes()))])
            write_private(pub/'package.json',p);args['publication_packages']=[pub/'package.json']
            with patch('openweb_news.date_replay._responses',return_value={}):
                with self.assertRaisesRegex(ValueError,'canonical case'):date_replay.replay(**args)

    def test_publication_override_is_retained_in_effective_outcomes(self):
        with TemporaryDirectory()as temp:
            root=Path(temp);args=self.setup_source(root)
            f=test_publication.PublicationTests();f.setUp()
            case=load(root/'pipeline/inputs/case.json');cid=case['compiled']['packet']['case_id']
            own={r:dict(verdict=f.output(2),native_completion_verified=True,transport_success=True)for r in ('codex','claude')}
            result=publication.corroborate(f.compiled,f.packet,own)
            self.assertTrue(result['accepted'])
            record=dict(case_id=cid,provenance={r:dict(response_sha256=digest(raw))for r,raw in own.items()},**result)
            report=load(args['source_report']);report['outcomes'][0]['grade']=2;report['publication_review']['records']=[record]
            write_private(args['source_report'],report,replace=True);args['expected_source_sha256']=digest(args['source_report'].read_bytes())
            pub=root/'publication';write_private(pub/'inputs/case.json',case);write_private(pub/'run/attempts.jsonl',b'')
            jobs=[dict(task_id='publication-'+r+'-'+cid,role=r,blind_packet=f.packet)for r in own]
            write_private(pub/'package.json',dict(jobs=jobs,cases=[dict(case_id=cid,path='inputs/case.json',sha256=digest((pub/'inputs/case.json').read_bytes()))]))
            args['publication_packages']=[pub/'package.json']
            responses={j['task_id']:own[j['role']]for j in jobs}
            with patch('openweb_news.date_replay._responses',side_effect=lambda p,r:responses if r==pub.resolve() else {}):
                summary=date_replay.replay(**args)
            effective=load(args['directory']/'effective-outcomes.json')[0]
            self.assertEqual(effective['grade'],2);self.assertEqual(summary['outcome_counts']['combined'],{'2':1})
            self.assertEqual(effective['selected_roles'],['publication-review-codex','publication-review-claude'])


class SavedReviewBindingTests(unittest.TestCase):
    def test_response_request_and_role_tampering_is_rejected(self):
        from copy import deepcopy
        for kind in ('clean','input','response','role','settings'):
            with self.subTest(kind=kind),TemporaryDirectory()as temp:
                root=Path(temp);folder=root/'run/attempts/one'
                config=dict(backend='codex-subscription',model='synthetic',reasoning_effort='high')
                job=dict(task_id='review-one',role='codex',backend=config['backend'],backend_config=config,
                         blind_packet=dict(input={'own':'synthetic'},prompt='synthetic',schema={}))
                request=deepcopy(job);raw=dict(transport_success=True,verdict={'synthetic':True})
                if kind=='input':request['blind_packet']['input']['own']='changed'
                elif kind=='role':request['role']='claude'
                elif kind=='settings':request['backend_config']['reasoning_effort']='low'
                write_private(folder/'request.json',request);write_private(folder/'result.json',raw)
                event=dict(event='finished',task_id=job['task_id'],attempt_id='one',result_path='attempts/one/result.json',
                    result_sha256=digest((folder/'result.json').read_bytes()),request_sha256=digest((folder/'request.json').read_bytes()))
                write_private(root/'run/attempts.jsonl',(json.dumps(event)+'\n').encode())
                if kind=='response':write_private(folder/'result.json',dict(raw,verdict={'changed':True}),replace=True)
                package=dict(jobs=[job],reviewers={'codex':config})
                with patch('openweb_news.subscription_health.terminal_verified',return_value=True):
                    if kind=='clean':self.assertTrue(date_replay._responses(package,root)['review-one']['native_completion_verified'])
                    else:
                        with self.assertRaises(ValueError):date_replay._responses(package,root)
