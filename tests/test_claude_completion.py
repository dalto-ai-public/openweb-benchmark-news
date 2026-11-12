from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from openweb_news import fixtures,subscription_health as health
from openweb_news.common import digest,encoded,load,records
from openweb_news.preparation import prepare
from openweb_news.runner import run
from openweb_news.completion_audit import audit_ledger
from .test_subscription_health import native_claude


class ClaudeCompletionTests(unittest.TestCase):
    def test_recovered_completion_keeps_health_and_rejects_bad_records(self):
        with TemporaryDirectory()as temp:
            root=Path(temp);cfg=fixtures.config(root/'run')
            with patch('openweb_news.fixtures.model',side_effect=native_claude(fixtures.model)):
                run(prepare(cfg,mode='fixture'))
            event=next(e for e in records(root/'run/attempts.jsonl')if e['event']=='finished'and e['task_id'].startswith('claude-'))
            folder=root/'run/attempts'/event['attempt_id'];request=load(folder/'request.json');response=load(folder/'result.json')
            events=list(records(folder/'stdout.jsonl'))
            for e in events:e['session_id']='fixture-session'
            retry=dict(type='system',subtype='api_retry',attempt=1,max_retries=10,retry_delay_ms=500,error_status=None,error='unknown',session_id='fixture-session',uuid='retry-1')
            events.insert(1,retry)
            path=folder/'stdout.jsonl'
            def save(es):path.write_bytes(b''.join(encoded(e)for e in es))
            save(events);proof=health.recovered_completion(request,response,folder)
            self.assertIsNotNone(proof);self.assertEqual(proof['profile'],'claude-retried-terminal-completion-v1')
            self.assertFalse(proof['normal_health']);self.assertFalse(health.audit(request,response,folder))
            stored=dict(response,subscription_completion_evidence=proof,native_completion_verified=True)
            self.assertTrue(health.terminal_verified(request,stored,folder));self.assertFalse(health.verified(request,stored,folder))
            altered=deepcopy(request);altered['blind_packet']['input']['changed']=True
            self.assertFalse(health.terminal_verified(altered,stored,folder))
            for change in ('session','final_session','error_status','error_type','retry_type','attempt','delay','missing_status','extra_auth','post_final','pre_init','missing_final','double_final','failed_final','verdict','model','tools','quota','auth','api_failure','native_bytes'):
                es=deepcopy(events);raw=deepcopy(response)
                if change=='session':es[1]['session_id']='other'
                if change=='final_session':es[-1]['session_id']='other'
                if change=='error_status':es[1]['error_status']=401
                if change=='error_type':es[1]['error']='rate_limit_error'
                if change=='retry_type':es[1]['subtype']='other'
                if change=='attempt':es[1]['attempt']=True
                if change=='delay':es[1]['retry_delay_ms']=-1
                if change=='missing_status':del es[1]['error_status']
                if change=='extra_auth':es[1]['auth_error']=True
                if change=='post_final':es.append(deepcopy(es[1]))
                if change=='pre_init':es.insert(0,es.pop(1))
                if change=='missing_final':es.pop()
                if change=='double_final':es.append(deepcopy(es[-1]))
                if change=='failed_final':es[-1]['is_error']=True
                if change=='verdict':raw['verdict']['reason']='changed'
                if change=='model':es[0]['model']='different'
                if change=='tools':es[0]['tools'].append('Read')
                if change=='quota':es[-2]['rate_limit_info']['status']='rejected'
                if change=='auth':es.insert(2,dict(type='auth_status',session_id='fixture-session'))
                if change=='api_failure':raw['fatal_configuration']=True
                save(es)
                if change=='native_bytes':path.write_bytes(path.read_bytes()+b'broken')
                with self.subTest(change=change):self.assertIsNone(health.recovered_completion(request,raw,folder))
            save(events)
            self.assertEqual(health.terminal_bind(request,response,folder),proof)
            audit=audit_ledger(root/'run',root/'audit')
            self.assertEqual(audit['unclassified'],0)
            self.assertEqual(audit['counts']['claude:recovered'],1)
            with self.assertRaises(ValueError):audit_ledger(root/'run',root/'run/audit')
            self.assertFalse((root/'run/audit').exists())
            (folder/'request.json').write_bytes(encoded(dict(request,backend='different')))
            with self.assertRaises(ValueError):audit_ledger(root/'run',root/'tampered')
            self.assertFalse((root/'tampered').exists())

class ReviewIntegrationTests(unittest.TestCase):
    def test_native_retry_completes_review_and_resumes_without_health_exemption(self):
        from types import SimpleNamespace
        from .test_review_pipeline import AdjudicationTests
        from openweb_news import adjudication,review_pipeline,representation
        from openweb_news.common import asset_bindings,code_bindings,write_private
        from openweb_news.review import settle
        f=AdjudicationTests();f.setUp()
        with TemporaryDirectory()as temp:
            root=Path(temp);cid=f.compiled['packet']['case_id'];q=deepcopy(f.compiled['packet']['question']);q.update(family_id='family',need_id='need')
            original=dict(case_id=cid,question_id=q['question_id'],configuration_id=f.config['configuration_id'],rank=1,packet_sha256=f.compiled['packet_sha256'],**settle(f.positions['pro'],f.positions['codex']))
            case=dict(compiled=f.compiled,verdicts={'pro':f.raw,'codex':f.raw},original=original,provenance={},provider=f.config)
            write_private(root/'inputs/case.json',case)
            configs={r:dict(backend=r+'-subscription',model='fixture-model',reasoning_effort='high',timeout_seconds=1,**({'transport':'app-server-stdio-v1'}if r=='codex'else{}))for r in ('codex','claude')}
            jobs=[dict(task_id='adjudication-'+r+'-'+cid,stage='adjudication',role=r,case_id=cid,blind_packet=f.packet,kind='model',backend=r+'-subscription',backend_config=configs[r],reservation_usd='0',reservation_basis='configured subscription; quota and time separate')for r in configs]
            brave=dict(questions=[q],configuration=dict(run_directory=str(root/'brave/run')))
            write_private(root/'brave/package.json',brave)
            package=dict(profile=review_pipeline.PROFILE,mode='live',code=code_bindings(),assets=asset_bindings(),incremental_api_calls=0,maximum_new_subscription_tasks=2,jobs=jobs,reviewers=configs,
                cases=[dict(case_id=cid,path='inputs/case.json',sha256=digest((root/'inputs/case.json').read_bytes()))],text_profile=representation.PROFILE,adjudication_profile=adjudication.PROOF_PROFILE,
                run_directory=str(root/'run'),scheduling={r+'-subscription':dict(concurrency=1,minimum_start_interval_seconds=0)for r in configs},source_collection=[dict(question_id=q['question_id'],configuration_id=f.config['configuration_id'],status='success')],
                providers=[f.config],questions=[q],coverage_target=.8,tie_order=[f.config['configuration_id']],source_method=4,source_report_sha256='fixture',source_package_sha256='source',replacement_package_sha256=digest(brave))
            from openweb_news.recovery import sibling_rule
            package['recovery']={r+'-subscription':sibling_rule('judge',1)for r in configs}
            write_private(root/'package.json',package);calls=[];observed=[]
            def response(role):return dict(transport_success=True,dispatch_state='completed',selected_backend=role+'-subscription',account_mode='subscription',auth_observation='chatgpt'if role=='codex'else'claude.ai',
                requested_model='fixture-model',requested_settings=dict(reasoning_effort='high'),verdict=deepcopy(f.answer))
            class Runtime:
                def __init__(self,*args,**kwargs):self.client=SimpleNamespace(failure=None)
                def __enter__(self):return self
                def __exit__(self,*args):pass
                def review(self,packet,folder):
                    calls.append('codex');raw=response('codex');raw.update(complete=True,selected_transport='app-server-stdio-v1',thread_id='thread',turn_id='turn')
                    write_private(folder/'runtime-thread.json',dict(instructionSources=[],model='fixture-model',modelProvider='openai',reasoningEffort='high',approvalPolicy='never',sandbox=dict(type='readOnly',networkAccess=False),thread=dict(id='thread',ephemeral=True,turns=[])))
                    write_private(folder/'runtime-request.json',dict(thread_start=dict(model='fixture-model',baseInstructions=packet['prompt'],ephemeral=True,approvalPolicy='never',sandbox='read-only',developerInstructions='',dynamicTools=[],allowProviderModelFallback=False),turn_start=dict(threadId='thread',model='fixture-model',effort='high',outputSchema=packet['schema'],input=[dict(type='text',text=encoded(packet['input']).decode())])))
                    events=[dict(method='item/completed',params=dict(threadId='thread',turnId='turn',item=dict(type='userMessage',content=[dict(text=encoded(packet['input']).decode())]))),dict(method='item/completed',params=dict(threadId='thread',turnId='turn',item=dict(type='agentMessage',text=encoded(raw['verdict']).decode()))),dict(method='turn/completed',params=dict(threadId='thread',turn=dict(id='turn',status='completed')))]
                    write_private(folder/'runtime-events.jsonl',b''.join(encoded(e)for e in events));return raw
            def subscription(config,packet,folder,preflight_only=False,**kwargs):
                if preflight_only:return dict(transport_success=True)
                calls.append('claude');raw=response('claude');raw.update(exit_code=0,served_model='fixture-model')
                events=[dict(type='system',subtype='init',model='fixture-model',tools=['StructuredOutput'],plugins=[],skills=[],mcp_servers=[],analytics_disabled=True,product_feedback_disabled=True,session_id='session'),
                        dict(type='system',subtype='api_retry',attempt=1,max_retries=10,retry_delay_ms=1,error_status=None,error='unknown',session_id='session',uuid='retry'),
                        dict(type='result',subtype='success',is_error=False,structured_output=raw['verdict'],session_id='session')]
                write_private(folder/'stdout.jsonl',b''.join(encoded(e)for e in events));write_private(folder/'stderr.txt',b'');return raw
            class Gate:
                def __init__(self,*args):pass
                def capacity(self,*args):return dict(stopped=False,ready_at=0)
            from openweb_news.scheduling import Pipeline
            original_observe=Pipeline.observe
            def observe(self,*args,**kwargs):
                observed.append((args[1],kwargs['native_completion']))
                return original_observe(self,*args,**kwargs)
            with patch('openweb_news.codex_runtime.CodexRuntime',Runtime),patch('openweb_news.review_pipeline.backends.subscription',side_effect=subscription),patch('openweb_news.quota.QuotaGate',Gate),patch.object(Pipeline,'observe',observe):
                report=review_pipeline.run_review(root/'package.json',approval=dict(approved=True,package_sha256=digest(package)))
                self.assertEqual(report['execution']['state'],'completed');self.assertEqual(report['outcome_counts'],{'2':1})
                self.assertIn(('claude-subscription',False),observed);self.assertIn(('codex-subscription',True),observed)
                replay=review_pipeline.run_review(root/'package.json',approval=dict(approved=True,package_sha256=digest(package)))
                self.assertEqual(replay['outcome_counts'],report['outcome_counts']);self.assertCountEqual(calls,['codex','claude'])

            from openweb_news.phase_review import completed_bundle
            from openweb_news.review_bundle import PROFILE as BUNDLE_PROFILE
            bundle=dict(profile=BUNDLE_PROFILE,brave_package_sha256=digest(brave),source_package_sha256='source',source_report_sha256='fixture',question_ids=[q['question_id']])
            write_private(root/'bundle.json',bundle)
            write_private(root/'approval.json',dict(decision='approved',package_sha256=digest(bundle)))
            # The fixture's review files live directly under root; bind that
            # same completed run into the ordinary bundle directory layout.
            import shutil
            (root/'review').mkdir()
            shutil.copy(root/'package.json',root/'review/package.json')
            shutil.copytree(root/'inputs',root/'review/inputs')
            shutil.copytree(root/'run',root/'review/run')
            write_private(root/'review/approval.json',dict(approved=True,package_sha256=digest(package),parent_bundle_sha256=digest(bundle)))
            write_private(root/'brave/run/report.json',dict(package_sha256=digest(brave),execution=dict(state='completed')))
            write_private(root/'brave/run/attempts.jsonl',b'')
            with patch('openweb_news.accounting.report_costs',return_value={}):
                binding,*_=completed_bundle(root/'bundle.json')
            self.assertEqual(binding['native_subscription_proofs'],1)
            self.assertEqual(len(binding['recovered_terminal_completions']),1)
            self.assertEqual(binding['recovered_terminal_completions'][0]['profile'],'claude-retried-terminal-completion-v1')


if __name__=='__main__':unittest.main()
