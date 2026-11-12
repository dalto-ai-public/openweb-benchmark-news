from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from openweb_news import fixtures, judging, subscription_health
from openweb_news.common import digest, encoded, load, records, write_private
from openweb_news.preparation import prepare
from openweb_news.recovery import Recovery, sibling_rule
from openweb_news.reuse import freeze
from openweb_news.runner import run
from .helpers import question


def native_model(original):
    def model(role, compiled, folder):
        raw = original(role, compiled, folder)
        if role != 'codex':
            return raw
        packet = judging.blind_packet(compiled)
        # Deliberately invalid quotes: native completion must not accept this vote.
        raw['verdict']['candidates'][0]['support'][0]['quote'] = ''
        tid, uid = 'thread-' + compiled['packet']['case_id'], 'turn-' + compiled['packet']['case_id']
        config = dict(model='synthetic-model', reasoning_effort='high')
        raw.update(complete=True, dispatch_state='completed', account_mode='subscription', auth_observation='chatgpt',
                   selected_backend='codex-subscription', selected_transport='app-server-stdio-v1',
                   requested_model=config['model'], requested_settings=dict(reasoning_effort='high'), thread_id=tid, turn_id=uid)
        write_private(folder / 'runtime-thread.json', dict(instructionSources=[], model=config['model'], modelProvider='openai',
            reasoningEffort='high', approvalPolicy='never', sandbox=dict(type='readOnly', networkAccess=False), thread=dict(id=tid, ephemeral=True, turns=[])))
        write_private(folder / 'runtime-request.json', dict(thread_start=dict(model=config['model'], baseInstructions=packet['prompt'],
            ephemeral=True, approvalPolicy='never', sandbox='read-only', developerInstructions='', dynamicTools=[], allowProviderModelFallback=False),
            turn_start=dict(threadId=tid, model=config['model'], effort='high', input=[dict(type='text', text=encoded(packet['input']).decode())], outputSchema=packet['schema'])))
        events = [dict(method='item/completed', params=dict(threadId=tid, turnId=uid, item=dict(type='userMessage', content=[dict(text=encoded(packet['input']).decode())]))),
                  dict(method='item/completed', params=dict(threadId=tid, turnId=uid, item=dict(type='agentMessage', text=json.dumps(raw['verdict'])))),
                  dict(method='turn/completed', params=dict(threadId=tid, turn=dict(id=uid, status='completed')))]
        write_private(folder / 'runtime-events.jsonl', b''.join(encoded(e) for e in events))
        return raw
    return model


def native_claude(original):
    def model(role, compiled, folder):
        raw = original(role, compiled, folder)
        if role != 'claude':
            return raw
        raw['verdict']['candidates'][0]['support'][0]['quote'] = ''
        raw.update(dispatch_state='completed', account_mode='subscription', auth_observation='claude.ai',
                   selected_backend='claude-subscription', requested_model='synthetic-model',
                   requested_settings=dict(reasoning_effort='high'), exit_code=0)
        events = [dict(type='system', subtype='init', model='synthetic-model', analytics_disabled=True,
                       product_feedback_disabled=True, tools=['StructuredOutput'], mcp_servers=[], skills=[], plugins=[]),
                  dict(type='assistant', message=dict(model='synthetic-model', content=[dict(type='tool_use', name='StructuredOutput')])),
                  dict(type='rate_limit_event', rate_limit_info=dict(status='allowed', overageStatus='rejected')),
                  dict(type='result', subtype='success', is_error=False, structured_output=raw['verdict'])]
        write_private(folder/'stdout.jsonl', b''.join(encoded(e) for e in events))
        write_private(folder/'stderr.txt', b'')
        return raw
    return model


class NativeHealthTests(unittest.TestCase):
    def config(self, root, *, enabled=False):
        cfg = fixtures.config(root / 'run')
        cfg['execution_policy']['scheduling']['recovery'] = {
            k: sibling_rule('provider' if k.startswith('provider/') else 'judge', v['concurrency'])
            for k, v in cfg['execution_policy']['scheduling']['accounts'].items()}
        for rule in cfg['execution_policy']['scheduling']['recovery'].values():
            rule['cooldown_seconds'] = 0
        if enabled:
            cfg['subscription_completion_health'] = dict(profile=subscription_health.PROFILE, roles=['codex'])
        return cfg

    def test_native_invalid_votes_stay_invalid_but_opt_in_health_and_resume_are_clean(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            package = prepare(self.config(root, enabled=True), mode='fixture')
            with patch('openweb_news.fixtures.model', side_effect=native_model(fixtures.model)):
                report = run(package)
            codex = [r['positions']['codex'] for r in report['outcomes']]
            self.assertTrue(all(r['state'] == 'invalid' for r in codex))
            self.assertTrue(all('codex' not in r['selected_roles'] for r in report['outcomes']))
            state = next(s for s in report['execution']['recovery'] if s['account']=='codex-subscription' and s['configuration'] is None)
            self.assertEqual((state['mode'], state['pauses']), ('normal', 0))
            with patch('openweb_news.fixtures.model', side_effect=AssertionError('no resend')), patch('openweb_news.fixtures.search', side_effect=AssertionError('no recollection')):
                resumed = run(package)
            self.assertEqual(resumed['execution']['recovery'], report['execution']['recovery'])
            self.assertEqual(resumed['outcome_counts'], report['outcome_counts'])

    def test_native_audit_rejects_tampering_errors_routing_context_and_quota(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            package = prepare(self.config(root, enabled=True), mode='fixture')
            with patch('openweb_news.fixtures.model', side_effect=native_model(fixtures.model)):
                run(package)
            event = next(e for e in records(root/'run/attempts.jsonl') if e['event']=='finished' and e['task_id'].startswith('codex-'))
            folder = root/'run/attempts'/event['attempt_id']
            request, response = load(folder/'request.json'), load(folder/'result.json')
            self.assertTrue(subscription_health.verified(request, response, folder))
            for change in [dict(complete=False), dict(dispatch_state='uncertain'), dict(fatal_configuration=True), dict(native_quota_evidence=[dict(error=True)]), dict(auth_observation='apiKey'), dict(requested_model='wrong'), dict(error_type='AuthError')]:
                self.assertFalse(subscription_health.audit(request, dict(response, **change), folder))
            path = folder/'runtime-events.jsonl'
            original = path.read_bytes()
            for event in [dict(method='error', params=dict(error=dict(codexErrorInfo='rateLimitExceeded'))),
                          dict(method='item/started', params=dict(item=dict(type='commandExecution'))),
                          dict(method='turn/completed', params=dict(threadId='wrong', turn=dict(id=response['turn_id'], status='completed')))]:
                path.write_bytes(original + encoded(event))
                self.assertFalse(subscription_health.audit(request, response, folder))
                self.assertFalse(subscription_health.verified(request, response, folder))
            path.write_bytes(original + encoded(dict(method='warning', params={})))
            self.assertTrue(subscription_health.audit(request, response, folder))
            self.assertFalse(subscription_health.verified(request, response, folder))

    def test_recovered_terminal_proof_preserves_strict_health_and_full_bindings(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();package=prepare(self.config(root,enabled=True),mode='fixture')
            with patch('openweb_news.fixtures.model',side_effect=native_model(fixtures.model)):run(package)
            event=next(e for e in records(root/'run/attempts.jsonl')if e['event']=='finished'and e['task_id'].startswith('codex-'))
            folder=root/'run/attempts'/event['attempt_id'];request=load(folder/'request.json');response=load(folder/'result.json')
            path=folder/'runtime-events.jsonl';original=list(records(path))
            retry=dict(method='error',params=dict(threadId=response['thread_id'],turnId=response['turn_id'],willRetry=True,error=dict(codexErrorInfo=dict(responseStreamDisconnected=dict(httpStatusCode=403)))))
            def save(events):path.write_bytes(b''.join(encoded(e)for e in events))
            good=[retry]+original;save(good)
            proof=subscription_health.recovered_completion(request,response,folder)
            self.assertIsNotNone(proof);self.assertEqual(proof['error_count'],1);self.assertFalse(proof['normal_health'])
            self.assertFalse(subscription_health.audit(request,response,folder))
            self.assertFalse(subscription_health.verified(request,response,folder))
            self.assertEqual(proof['files']['runtime-events.jsonl'],digest(path.read_bytes()))
            for change in ('thread','turn','not_retry','kind','quota','after_terminal','missing_terminal','failed_terminal','verdict','schema','changed_bytes'):
                events=deepcopy(good);req=deepcopy(request);raw=deepcopy(response)
                if change=='thread':events[0]['params']['threadId']='wrong'
                if change=='turn':events[0]['params']['turnId']='wrong'
                if change=='not_retry':events[0]['params']['willRetry']=False
                if change=='kind':events[0]['params']['error']['codexErrorInfo']='authError'
                if change=='quota':events[0]['params']['error']['codexErrorInfo']={'responseStreamDisconnected':{'httpStatusCode':429}}
                if change=='after_terminal':events=original+[retry]
                if change=='missing_terminal':events=events[:-1]
                if change=='failed_terminal':events[-1]['params']['turn']['status']='failed'
                if change=='verdict':raw['verdict']['reason']='changed'
                if change=='schema':req['blind_packet']['schema']={}
                save(events)
                if change=='changed_bytes':path.write_bytes(path.read_bytes()+b'not-json')
                with self.subTest(change=change):self.assertIsNone(subscription_health.recovered_completion(req,raw,folder))
            save(good)
            self.assertEqual(subscription_health.recovered_completion(request,response,folder),proof)

    def test_recovered_v2_uses_bound_retry_invariants_and_retains_denials(self):
        with tempfile.TemporaryDirectory()as temp:
            root=Path(temp).resolve();package=prepare(self.config(root,enabled=True),mode='fixture')
            with patch('openweb_news.fixtures.model',side_effect=native_model(fixtures.model)):run(package)
            event=next(e for e in records(root/'run/attempts.jsonl')if e['event']=='finished'and e['task_id'].startswith('codex-'))
            folder=root/'run/attempts'/event['attempt_id'];request=load(folder/'request.json');response=load(folder/'result.json')
            response.pop('subscription_completion_evidence',None);response.pop('native_completion_verified',None)
            path=folder/'runtime-events.jsonl';original=list(records(path));tid,uid=response['thread_id'],response['turn_id']
            start=dict(method='turn/started',params=dict(threadId=tid,turn=dict(id=uid,status='inProgress')))
            retry=dict(method='error',params=dict(threadId=tid,turnId=uid,willRetry=True,
                error=dict(codexErrorInfo=dict(responseStreamDisconnected=dict(httpStatusCode=None)))))
            warning=dict(method='warning',params=dict(threadId=tid,message='Native transport reconnected.'))
            good=[start,retry,warning]+original
            def save(events):path.write_bytes(b''.join(encoded(e)for e in events))
            save(good);proof=subscription_health.recovered_completion(request,response,folder)
            self.assertEqual(proof['profile'],subscription_health.RECOVERED_CODEX_V2)
            self.assertFalse(proof['normal_health']);self.assertFalse(subscription_health.audit(request,response,folder))
            compiled=load(root/'run/compiled'/(event['task_id'][len('codex-'):]+'.json'))
            self.assertEqual(judging.decode_position(response['verdict'],compiled)['state'],'invalid')
            attached=dict(response,subscription_completion_evidence=proof,native_completion_verified=True)
            self.assertTrue(subscription_health.terminal_verified(request,attached,folder))
            future=deepcopy(good);future[1]['params']['error']['codexErrorInfo']='futureRecoverableTransport'
            save(future);self.assertEqual(subscription_health.recovered_completion(request,response,folder)['profile'],subscription_health.RECOVERED_CODEX_V2)
            for change in ('thread','turn','not_retry','auth','auth403','quota','spend','context','model','warning_thread',
                    'warning_turn','warning_after','delta_after','reasoning_after','after_terminal','second_terminal','failed_terminal','no_start','verdict','schema','runtime_failure'):
                events=deepcopy(good);req=deepcopy(request);raw=deepcopy(response)
                if change=='thread':events[1]['params']['threadId']='other'
                if change=='turn':events[1]['params']['turnId']='other'
                if change=='not_retry':events[1]['params']['willRetry']=False
                if change in {'auth','quota','spend','context'}:events[1]['params']['error']['codexErrorInfo']={'auth':'authError','quota':'rateLimitExceeded','spend':'workspaceOwnerCreditsDepleted','context':'contextWindowExceeded'}[change]
                if change=='auth403':events[1]['params']['error']['codexErrorInfo']={'authenticationError':{'httpStatusCode':403}}
                if change=='model':events.insert(2,dict(method='model/verification',params=dict(threadId=tid,turnId=uid,model='another-model')))
                if change=='warning_thread':events[2]['params']['threadId']='other'
                if change=='warning_turn':events[2]['params']['turnId']='other'
                if change=='warning_after':events=events[:2]+events[3:]+[events[2]]
                if change in {'delta_after','reasoning_after'}:events.append(dict(method='item/agentMessage/delta'if change=='delta_after'else'item/reasoning/textDelta',params=dict(threadId=tid,turnId=uid,delta='late output')))
                if change=='after_terminal':events.append(deepcopy(retry))
                if change=='second_terminal':events.append(deepcopy(original[-1]))
                if change=='failed_terminal':events[-1]['params']['turn']['status']='failed'
                if change=='no_start':events=events[1:]
                if change=='verdict':raw['verdict']['reason']='changed'
                if change=='schema':req['blind_packet']['schema']={}
                if change=='runtime_failure':events[1]['runtime_failure']='protocol failure'
                save(events)
                with self.subTest(change=change):self.assertIsNone(subscription_health.recovered_completion(req,raw,folder))
            save(good);self.assertEqual(subscription_health.recovered_completion(request,response,folder),proof)

    def test_ordinary_resume_preserves_old_stop_and_cannot_adopt_profile_without_transition(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            cfg = self.config(root)
            for name in ('codex-subscription',):
                cfg['execution_policy']['scheduling']['accounts'][name]['concurrency'] = 1
                cfg['execution_policy']['scheduling']['recovery'][name].update(initial_concurrency=1, maximum_concurrency=1, reduced_concurrency=1)
            source = prepare(cfg, mode='fixture')
            write_private(root/'source.json', source)
            with patch('openweb_news.fixtures.model', side_effect=native_model(fixtures.model)):
                report = run(source)
            state = next(s for s in report['execution']['recovery'] if s['account']=='codex-subscription' and s['configuration'] is None)
            self.assertEqual(state['mode'], 'stopped')
            with patch('openweb_news.fixtures.model', side_effect=AssertionError('no resend')), patch('openweb_news.fixtures.search', side_effect=AssertionError('no recollection')):
                self.assertEqual(run(source)['execution']['recovery'], report['execution']['recovery'])
            cfg['reuse_snapshot'] = freeze(root/'source.json', root/'snapshot')
            cfg['run_directory'] = str(root/'continued')
            cfg['subscription_completion_health'] = dict(profile=subscription_health.PROFILE, roles=['codex'])
            with self.assertRaisesRegex(ValueError, 'explicit health transition'):
                prepare(cfg, mode='fixture')
            attempts = sorted(e['attempt_id'] for e in records(root/'snapshot/events.jsonl') if e['event']=='finished' and e['task_id'].startswith('codex-'))
            cfg['subscription_health_transition'] = dict(profile='native-subscription-health-epoch-v1', source_snapshot=cfg['reuse_snapshot'],
                source_package_sha256=digest(source), roles=['codex'], attempt_ids=attempts, archived_recovery_sha256=digest(report['execution']['recovery']))
            missing = deepcopy(cfg)
            missing.pop('reuse_snapshot')
            with self.assertRaisesRegex(ValueError, 'retained exact-task reuse'):
                prepare(missing, mode='fixture')
            self.assertFalse((root/'continued').exists())
            for key, value in [('attempt_ids', []), ('archived_recovery_sha256', '0'*64)]:
                bad = deepcopy(cfg)
                bad['subscription_health_transition'][key] = value
                with self.assertRaisesRegex(ValueError, 'archived observations'):
                    prepare(bad, mode='fixture')
            with patch('openweb_news.reuse.committed', return_value=Decimal('1')):
                with self.assertRaisesRegex(ValueError, 'global spending admission'):
                    prepare(cfg, mode='fixture')
            target = prepare(cfg, mode='fixture')
            with patch('openweb_news.fixtures.model', side_effect=native_model(fixtures.model)), patch('openweb_news.fixtures.search', side_effect=AssertionError('no recollection')):
                repaired = run(target)
            self.assertTrue(all(r['positions']['codex']['state']=='invalid' for r in repaired['outcomes']))
            current = next(s for s in repaired['execution']['recovery'] if s['account']=='codex-subscription' and s['configuration'] is None)
            self.assertEqual((current['mode'], current['pauses']), ('normal', 0))
            self.assertEqual(sum(e['event']=='started' and e['task_id'].startswith('codex-') for e in records(root/'continued/attempts.jsonl')), 8)
            cfg['run_directory'] = str(root/'chain')
            write_private(root/'target.json', target)
            cfg['reuse_snapshot'] = freeze(root/'target.json', root/'chain-snapshot')
            chained = prepare(cfg, mode='fixture')
            with patch('openweb_news.fixtures.model', side_effect=AssertionError('no resend')), patch('openweb_news.fixtures.search', side_effect=AssertionError('no recollection')):
                self.assertEqual(run(chained)['execution']['recovery'], repaired['execution']['recovery'])
            bad = deepcopy(cfg)
            bad.pop('subscription_health_transition')
            with self.assertRaisesRegex(ValueError, 'adopted native health epoch'):
                prepare(bad, mode='fixture')

    def test_epoch_rejects_retained_quota_and_uncertain_delivery(self):
        for fault in ('quota', 'uncertain'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as temp:
                root = Path(temp).resolve()
                cfg = self.config(root)
                cfg['execution_policy']['scheduling']['accounts']['codex-subscription']['concurrency'] = 1
                cfg['execution_policy']['scheduling']['recovery']['codex-subscription'].update(initial_concurrency=1, maximum_concurrency=1, reduced_concurrency=1)
                original = native_model(fixtures.model)
                def model(role, compiled, folder):
                    raw = original(role, compiled, folder)
                    if role=='codex' and fault=='uncertain':
                        raw.update(complete=False, dispatch_state='uncertain')
                    return raw
                source = prepare(cfg, mode='fixture')
                write_private(root/'source.json', source)
                with patch('openweb_news.fixtures.model', side_effect=model):
                    first = run(source)
                if fault=='quota':
                    write_private(root/'run/codex-runtime/quota-events.jsonl', encoded(dict(event_id='retained-quota', observed_at=100,
                        reason='native_http_429', thread_id='native-thread')))
                cfg['reuse_snapshot'] = freeze(root/'source.json', root/'snapshot')
                cfg['run_directory'] = str(root/'continued')
                cfg['subscription_completion_health'] = dict(profile=subscription_health.PROFILE, roles=['codex'])
                cfg['subscription_health_transition'] = dict(profile='native-subscription-health-epoch-v1', source_snapshot=cfg['reuse_snapshot'],
                    source_package_sha256=digest(source), roles=['codex'], archived_recovery_sha256=digest(first['execution']['recovery']),
                    attempt_ids=sorted(e['attempt_id'] for e in records(root/'snapshot/events.jsonl') if e['event']=='finished' and e['task_id'].startswith('codex-')))
                with self.assertRaisesRegex(ValueError, 'native quota evidence|idle source'):
                    prepare(cfg, mode='fixture')
                self.assertFalse((root/'continued').exists())

    def test_native_completion_never_masks_transport_auth_or_uncertainty(self):
        for response in [dict(complete=False), dict(transport_success=False), dict(http_status=401), dict(http_status=429), dict(dispatch_state='uncertain')]:
            r = Recovery(dict(judge=sibling_rule('judge', 4)))
            for i in range(3):
                raw = dict(complete=True, transport_success=True, dispatch_state='completed', http_status=200)
                raw.update(response)
                r.observe(str(i), 'judge', 'codex', raw, 100+i, 'invalid', native_completion=True)
            self.assertIn(r.scope('judge')['mode'], {'cooldown', 'stopped'})

    def test_claude_native_health_privacy_model_tools_error_and_quota_negatives(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            cfg = self.config(root, enabled=True)
            cfg['subscription_completion_health']['roles'].append('claude')
            package = prepare(cfg, mode='fixture')
            with patch('openweb_news.fixtures.model', side_effect=native_claude(native_model(fixtures.model))):
                report = run(package)
            self.assertTrue(all(r['positions']['claude']['state']=='invalid' for r in report['outcomes'] if 'claude' in r['positions']))
            event = next(e for e in records(root/'run/attempts.jsonl') if e['event']=='finished' and e['task_id'].startswith('claude-'))
            folder = root/'run/attempts'/event['attempt_id']
            request, response = load(folder/'request.json'), load(folder/'result.json')
            self.assertTrue(subscription_health.verified(request, response, folder))
            events = list(records(folder/'stdout.jsonl'))
            for mutate in ('privacy', 'model', 'tool', 'error', 'quota', 'truncated', 'duplicate'):
                changed = deepcopy(events)
                if mutate == 'privacy': changed[0]['analytics_disabled'] = False
                elif mutate == 'model': changed[0]['model'] = 'wrong'
                elif mutate == 'tool': changed[1]['message']['content'][0]['name'] = 'Bash'
                elif mutate == 'error': changed[-1]['is_error'] = True
                elif mutate == 'quota': changed[2]['rate_limit_info']['status'] = 'rejected'
                elif mutate == 'truncated': changed.pop()
                else: changed.append(changed[-1])
                (folder/'stdout.jsonl').write_bytes(b''.join(encoded(e) for e in changed))
                self.assertFalse(subscription_health.audit(request, response, folder), mutate)
            (folder/'stdout.jsonl').write_bytes(b''.join(encoded(e) for e in events))

    def test_mixed_source_profile_is_preserved_when_only_claude_epoch_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            cfg = self.config(root, enabled=True)
            cfg['execution_policy']['scheduling']['accounts']['claude-subscription']['concurrency'] = 1
            cfg['execution_policy']['scheduling']['recovery']['claude-subscription'].update(initial_concurrency=1, maximum_concurrency=1, reduced_concurrency=1)
            source = prepare(cfg, mode='fixture')
            write_private(root/'source.json', source)
            with patch('openweb_news.fixtures.model', side_effect=native_claude(native_model(fixtures.model))):
                first = run(source)
            states = {s['account']: s for s in first['execution']['recovery'] if s['configuration'] is None}
            self.assertEqual(states['codex-subscription']['mode'], 'normal')
            self.assertEqual(states['claude-subscription']['mode'], 'stopped')
            cfg['reuse_snapshot'] = freeze(root/'source.json', root/'snapshot')
            cfg['run_directory'] = str(root/'continued')
            cfg['subscription_completion_health']['roles'].append('claude')
            cfg['subscription_health_transition'] = dict(profile='native-subscription-health-epoch-v1', source_snapshot=cfg['reuse_snapshot'],
                source_package_sha256=digest(source), roles=['claude'], archived_recovery_sha256=digest(first['execution']['recovery']),
                attempt_ids=sorted(e['attempt_id'] for e in records(root/'snapshot/events.jsonl') if e['event']=='finished' and e['task_id'].startswith('claude-')))
            target = prepare(cfg, mode='fixture')
            with patch('openweb_news.fixtures.model', side_effect=native_claude(native_model(fixtures.model))), patch('openweb_news.fixtures.search', side_effect=AssertionError('no recollection')):
                report = run(target)
            self.assertTrue(all(s['mode']=='normal' for s in report['execution']['recovery'] if s['configuration'] is None and s['account'].endswith('-subscription')))

    def test_remaining_trial_runner_and_chained_snapshot_never_relabel_or_repeat_paid_work(self):
        from .test_concurrency import configuration
        import time
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            cfg = self.config(root)
            cfg['questions'] = [question('cohort-'+str(i)) for i in range(54)]
            cfg['execution_policy']['scheduling']['accounts']['codex-subscription']['concurrency'] = 1
            cfg['execution_policy']['scheduling']['recovery']['codex-subscription'].update(initial_concurrency=1, maximum_concurrency=1, reduced_concurrency=1)
            source = prepare(cfg, mode='fixture')
            write_private(root/'source.json', source)
            with patch('openweb_news.fixtures.model', side_effect=native_model(fixtures.model)):
                first = run(source)
            cfg['reuse_snapshot'] = freeze(root/'source.json', root/'snapshot')
            cfg['run_directory'] = str(root/'continued')
            attempts = [e for e in records(root/'snapshot/events.jsonl') if e['event']=='finished' and e['task_id'].startswith('codex-')]
            historical = sorted(e['task_id'][6:] for e in attempts)
            remaining = sorted(r['case_id'] for r in first['outcomes'] if r['case_id'] not in historical)
            self.assertEqual(len(remaining), 102)
            cfg['subscription_completion_health'] = dict(profile=subscription_health.PROFILE, roles=['codex'])
            cfg['subscription_health_transition'] = dict(profile='native-subscription-health-epoch-v1', source_snapshot=cfg['reuse_snapshot'],
                source_package_sha256=digest(source), roles=['codex'], archived_recovery_sha256=digest(first['execution']['recovery']),
                attempt_ids=sorted(e['attempt_id'] for e in attempts))
            cfg['execution_policy']['scheduling']['accounts']['codex-subscription']['concurrency'] = 4
            cfg['execution_policy']['scheduling']['recovery']['codex-subscription'].update(initial_concurrency=4, maximum_concurrency=16, reduced_concurrency=2)
            cfg['codex_concurrency_trial'] = dict(configuration(), historical_cases=historical,
                remaining_case_cohorts={case:i//34 for i,case in enumerate(remaining)})
            target = prepare(cfg, mode='fixture')
            calls = []
            original = native_model(fixtures.model)
            def model(role, compiled, folder):
                self.assertEqual(role, 'codex')
                calls.append(compiled['packet']['case_id'])
                time.sleep(.008)
                return original(role, compiled, folder)
            with patch('openweb_news.fixtures.model', side_effect=model), patch('openweb_news.fixtures.search', side_effect=AssertionError('no recollection')):
                report = run(target)
            self.assertEqual(set(calls), set(remaining))
            trial = report['execution']['codex_concurrency_trial']
            self.assertEqual([s['measured_tasks'] for s in trial['stages']], [34,34,34])
            self.assertTrue(all(case not in trial['measurements'] for case in historical))
            write_private(root/'target.json', target)
            cfg['run_directory'] = str(root/'chained')
            cfg['reuse_snapshot'] = freeze(root/'target.json', root/'chain-snapshot')
            chained = prepare(cfg, mode='fixture')
            with patch('openweb_news.fixtures.model', side_effect=AssertionError('no repeated success')), patch('openweb_news.fixtures.search', side_effect=AssertionError('no recollection')):
                result = run(chained)
            self.assertEqual(result['outcome_counts'], report['outcome_counts'])
            self.assertEqual(result['execution']['codex_concurrency_trial']['measurements'], trial['measurements'])
