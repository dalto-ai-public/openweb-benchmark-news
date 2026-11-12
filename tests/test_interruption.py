from copy import deepcopy
from collections import Counter
from decimal import Decimal
import http.client
import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from openweb_news import fixtures, reuse
from openweb_news.common import digest, encoded, load, records, write_private
from openweb_news.interruption import KEY, PROFILE, prepare_subscription, prepare_credit_restoration
from openweb_news.ledger import Ledger, UncertainAttempt
from openweb_news.preparation import prepare
from openweb_news.runner import run
from openweb_news.transport import Credentials, https
from .helpers import question, verdict
from .test_independent_method import accepted_fixture


class InterruptedHTTPTests(unittest.TestCase):
    def test_credit_restoration_continues_remaining_primary_without_repeating_saved_votes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve(); cfg = fixtures.config(root/'source')
            cfg['provider_ids']=cfg['provider_ids'][:1];cfg['tie_order']=cfg['provider_ids'][:]
            cfg['execution_policy']['scheduling']['accounts'].pop('provider/OPENWEB_NEWS_EXA_API_KEY')
            cfg['questions']=[question('restored-credit'),question('synthetic-disagreement')]
            cfg['execution_policy'].update(spending_ceiling_usd='10', max_attempts_per_task=3)
            cfg['execution_policy']['retry_policy'].update(max_total=None,max_model=None,max_search=None,http_statuses=[503])
            cfg,_=accepted_fixture(root/'authority',cfg)
            source=prepare(cfg,mode='fixture');write_private(root/'source.json',source)
            original=fixtures.model
            def initial(role,compiled,folder):
                qid=compiled['packet']['question']['question_id']
                if role=='pro' and qid=='restored-credit': return dict(transport_success=False,complete=False,dispatch_state='uncertain',error_type='TimeoutError')
                if role=='codex' and qid=='synthetic-disagreement': return dict(transport_success=False,complete=False,dispatch_state='uncertain',error_type='RuntimeStopped',fatal_configuration=True)
                return original(role,compiled,folder)
            with patch('openweb_news.fixtures.model',side_effect=initial):
                with self.assertRaises(RuntimeError):run(source)
            raw=encoded(dict(error=dict(code=402,status='RESOURCE_EXHAUSTED',message='Your prepayment credits are depleted. Synthetic fixture.')))
            request=dict(kind='cache',role='pro',reservation_usd='0.01',wire=dict(method='POST',endpoint='https://generativelanguage.googleapis.com/v1beta/cachedContents',body={}))
            with Ledger(root/'source',dict(package_sha256=digest(source),mode='fixture')).locked() as ledger:
                ledger.execute('cache-create-'+digest(request),request,lambda _:dict(transport_success=False,complete=True,dispatch_state='completed',http_status=402,raw_text=raw.decode(),retained_sha256=digest(raw)))
            ref=reuse.freeze(root/'source.json',root/'snapshot')
            selected=[]
            for e in records(root/'snapshot/events.jsonl'):
                if e['event']=='finished':
                    response=load(root/'snapshot'/e['result_path'])
                    if response.get('dispatch_state')=='uncertain' or response.get('http_status')==402:
                        selected.append(dict(task_id=e['task_id'],prior_attempt_id=e['attempt_id'],request_sha256=e['request_sha256'],prior_result_sha256=e['result_sha256']))
            child=prepare_credit_restoration(root/'source.json',ref,root/'child',selected,reason='Owner restored credits',accepted_profile=cfg['method_acceptance_package'])
            self.assertNotIn('method_acceptance_package',child['configuration'])
            self.assertEqual(child['configuration']['live_profile_authority']['package_path'],cfg['method_acceptance_package'])
            sent=[]
            def restored(role,compiled,folder):
                sent.append(role); return original(role,compiled,folder)
            with patch('openweb_news.fixtures.search',side_effect=AssertionError('saved searches must not repeat')),patch('openweb_news.fixtures.model',side_effect=restored):
                result=run(child,retry_failed=True)
            self.assertEqual(Counter(sent),dict(pro=1,codex=1,claude=1))
            self.assertEqual(result['execution']['state'],'completed')
            write_private(root/'child.json',child)
            reuse.freeze(root/'child.json',root/'child-snapshot')  # Named Pro uncertainty remains replayable.
            bad=deepcopy(child['configuration']);bad.update(questions=child['questions'],provider_ids=[p['configuration_id']for p in child['providers']])
            bad[KEY]['attempts']=[]
            with self.assertRaises(ValueError):prepare(bad,mode='fixture')
            with Ledger(root/'source',dict(package_sha256=digest(source),mode='fixture')).locked() as ledger:
                ledger.execute('cache-delete-'+digest(request),request,lambda _:dict(transport_success=False,complete=True,dispatch_state='completed',http_status=402,raw_text=raw.decode(),retained_sha256=digest(raw)))
            bad_ref=reuse.freeze(root/'source.json',root/'non-create-snapshot')
            e=list(records(root/'non-create-snapshot/events.jsonl'))[-1]
            wrong=[dict(task_id=e['task_id'],prior_attempt_id=e['attempt_id'],request_sha256=e['request_sha256'],prior_result_sha256=e['result_sha256'])]
            with self.assertRaises(ValueError):
                prepare_credit_restoration(root/'source.json',bad_ref,root/'wrong',wrong,reason='Not a cache creation',accepted_profile=cfg['method_acceptance_package'])

    def test_subscription_recovery_runs_eligible_claude_after_saved_pro(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve(); cfg = fixtures.config(root/'source')
            cfg['provider_ids'] = cfg['provider_ids'][:1]
            cfg['tie_order'] = cfg['provider_ids'][:]
            cfg['execution_policy']['scheduling']['accounts'].pop('provider/OPENWEB_NEWS_EXA_API_KEY')
            cfg['questions'] = [question('synthetic-disagreement')]
            cfg['execution_policy'].update(spending_ceiling_usd='10', max_attempts_per_task=3)
            cfg['execution_policy']['retry_policy'].update(max_total=None, max_model=None, max_search=None)
            cfg,_=accepted_fixture(root/'authority',cfg)
            source = prepare(cfg, mode='fixture'); write_private(root/'source.json', source)
            original = fixtures.model
            def initial(role, compiled, folder):
                if role == 'codex':
                    return dict(transport_success=False, complete=False, dispatch_state='uncertain',
                        error_type='FileNotFoundError', fatal_configuration=True)
                return original(role, compiled, folder)
            with patch('openweb_news.fixtures.model', side_effect=initial):
                with self.assertRaises(RuntimeError): run(source)
            ref = reuse.freeze(root/'source.json', root/'snapshot')
            e = next(e for e in records(root/'snapshot/events.jsonl')
                if e['event']=='finished' and e['task_id'].startswith('codex-'))
            selected = [dict(task_id=e['task_id'], prior_attempt_id=e['attempt_id'],
                request_sha256=e['request_sha256'], prior_result_sha256=e['result_sha256'])]
            child = prepare_subscription(root/'source.json', ref, root/'child', selected, reason='Synthetic repair',accepted_profile=cfg['method_acceptance_package'])
            sent = []
            def completed(role, compiled, folder):
                sent.append(role)
                if role == 'pro': self.fail('saved primary must not repeat')
                return original(role, compiled, folder)
            with patch('openweb_news.fixtures.model', side_effect=completed):
                result = run(child, retry_failed=True)
            self.assertEqual(sent, ['codex', 'claude'])
            self.assertEqual(result['execution']['state'], 'completed')
            self.assertFalse(any(p.get('pending_dispatch') for o in result['outcomes'] for p in o['positions'].values()))

    def test_subscription_recovery_resends_only_named_codex_and_defers_pro(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            cfg = fixtures.config(root/'source')
            cfg['questions'] = [question('subscription-repair')]
            cfg['execution_policy'].update(spending_ceiling_usd='10', max_attempts_per_task=3)
            cfg['execution_policy']['retry_policy'].update(max_total=None, max_model=None, max_search=None, http_statuses=[503])
            cfg,_=accepted_fixture(root/'authority',cfg)
            source = prepare(cfg, mode='fixture'); write_private(root/'source.json', source)
            original = fixtures.model
            def initial(role, compiled, folder):
                if role == 'codex':
                    return dict(transport_success=False, complete=False, dispatch_state='uncertain',
                        error_type='RuntimeStopped', fatal_configuration=True)
                if role == 'pro':
                    return dict(transport_success=False, complete=True, dispatch_state='completed',
                        http_status=503, raw_text='synthetic unavailable primary')
                return original(role, compiled, folder)
            with patch('openweb_news.fixtures.model', side_effect=initial):
                with self.assertRaises(RuntimeError):
                    run(source)
            events = list(records(root/'source/attempts.jsonl'))
            self.assertEqual({e['attempt_id'] for e in events if e['event']=='started'},
                {e['attempt_id'] for e in events if e['event']=='finished'})
            ref = reuse.freeze(root/'source.json', root/'snapshot')
            selected = []
            for e in events:
                if e['event']=='finished' and e['task_id'].startswith('codex-'):
                    selected.append(dict(task_id=e['task_id'], prior_attempt_id=e['attempt_id'],
                        request_sha256=e['request_sha256'], prior_result_sha256=e['result_sha256']))
            self.assertTrue(selected)
            child = prepare_subscription(root/'source.json', ref, root/'child', selected,
                reason='Synthetic owner-authorized interrupted subscription repair',accepted_profile=cfg['method_acceptance_package'])
            sent = []
            def completed(role, compiled, folder):
                sent.append(role)
                if role == 'pro': self.fail('subscription repair cannot dispatch Pro')
                return original(role, compiled, folder)
            with patch('openweb_news.fixtures.search', side_effect=AssertionError('no search dispatch')), \
                 patch('openweb_news.fixtures.model', side_effect=completed):
                result = run(child, retry_failed=True)
            self.assertEqual(sent.count('codex'), len(selected))
            self.assertEqual(set(sent), {'codex'})  # No third for a deferred primary.
            self.assertEqual(result['execution']['state'], 'completed_with_holds')
            with patch('openweb_news.fixtures.search', side_effect=AssertionError('no repeated searches')), \
                 patch('openweb_news.fixtures.model', side_effect=AssertionError('no repeated completed votes')):
                run(child, retry_failed=True)
            copied = (root/'child/run/attempts.jsonl').read_bytes()
            self.assertTrue(copied.startswith((root/'snapshot/events.jsonl').read_bytes()))
            for item in load(root/'snapshot/manifest.json')['files']:
                if item['path'].startswith('attempts/'):
                    self.assertEqual((root/'child/run'/item['path']).read_bytes(),
                        (root/'snapshot'/item['path']).read_bytes())
            bad = deepcopy(child['configuration'])
            bad.update(questions=child['questions'], provider_ids=[p['configuration_id'] for p in child['providers']])
            bad['execution_policy']['spending_ceiling_usd']='11'
            with self.assertRaises(ValueError): prepare(bad, mode='fixture')
            bad = deepcopy(child['configuration'])
            bad.update(questions=child['questions'], provider_ids=[p['configuration_id'] for p in child['providers']])
            bad[KEY]['attempts'][0]['prior_result_sha256']='0'*64
            with self.assertRaises(ValueError): prepare(bad, mode='fixture')
            # A completed primary is never eligible as an extra named resend.
            completed_pro = next(e for e in events if e['event']=='finished' and e['task_id'].startswith('pro-'))
            bad = deepcopy(child['configuration'])
            bad.update(questions=child['questions'], provider_ids=[p['configuration_id'] for p in child['providers']])
            bad[KEY]['attempts'].append(dict(task_id=completed_pro['task_id'], prior_attempt_id=completed_pro['attempt_id'],
                request_sha256=completed_pro['request_sha256'], prior_result_sha256=completed_pro['result_sha256']))
            with self.assertRaises(ValueError): prepare(bad, mode='fixture')

    def test_proven_preconnect_failures_retry_only_when_enabled_and_within_bound(self):
        from openweb_news.recovery import Recovery, sibling_rule
        failed = dict(complete=False, transport_success=False, dispatch_state='not_sent',
                      transport_stage='pre_http_connect', error_type='gaierror', http_status=None)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task, request = 'synthetic', dict(kind='search', reservation_usd='0.1')
            with Ledger(root, {}, retry_preconnect=True).locked() as ledger:
                calls = []
                for _ in range(4):
                    ledger.execute(task, request, lambda _: calls.append(1) or failed,
                                   max_attempts=3, retry_failed=True)
                self.assertEqual(len(calls), 3)
            recovery = Recovery(dict(provider=sibling_rule('provider', 2)))
            for i in range(2):
                recovery.observe(str(i), 'provider', 'search', failed, 100 + i)
            self.assertEqual(recovery.scope('provider', 'search')['mode'], 'normal')
            recovery.observe('third', 'provider', 'search', failed, 102)
            self.assertEqual(recovery.scope('provider', 'search')['mode'], 'cooldown')
            self.assertEqual(recovery.scope('provider', 'search')['pauses'], 1)

    def test_connect_failure_is_not_sent_but_request_and_read_failures_are_uncertain(self):
        for stage in ('dns', 'connect', 'request', 'read', 'complete'):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as tmp:
                connection = Mock()
                response = connection.getresponse.return_value
                response.status = 200
                response.getheaders.return_value = []
                response.read.return_value = b'{}'
                if stage == 'dns':
                    connection.connect.side_effect = socket.gaierror('synthetic DNS failure')
                elif stage == 'connect':
                    connection.connect.side_effect = ConnectionRefusedError()
                elif stage == 'request':
                    connection.request.side_effect = TimeoutError()
                elif stage == 'read':
                    response.read.side_effect = http.client.IncompleteRead(b'{', 1)
                with patch('openweb_news.transport.http.client.HTTPSConnection', return_value=connection):
                    result = https(dict(method='POST', endpoint='https://provider.example/search', body={}),
                                   None, Credentials(environment={}), Path(tmp), timeout_seconds=1)
                expected = 'not_sent' if stage in ('dns', 'connect') else 'completed' if stage == 'complete' else 'uncertain'
                self.assertEqual(result['dispatch_state'], expected)
                self.assertEqual(connection.request.call_count, 0 if expected == 'not_sent' else 1)
                self.assertEqual(result['transport_stage'], 'pre_http_connect' if expected == 'not_sent' else 'http_request_or_response')

    def test_unsent_connection_failure_remains_visible_in_collection_and_held_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = fixtures.config(Path(tmp) / 'run')
            cfg['questions'] = [question('connection-failure')]
            p = prepare(cfg, mode='fixture')
            failed = dict(complete=False, transport_success=False, dispatch_state='not_sent',
                          transport_stage='pre_http_connect', error_type='gaierror', http_status=None)
            with patch('openweb_news.fixtures.search', return_value=failed):
                report = run(p)
            self.assertEqual(len(report['execution']['held_tasks']), 2)
            self.assertTrue(all(r['transport_stage'] == 'pre_http_connect' for r in report['collection']))
            self.assertTrue(all(r['dispatch_state'] == 'not_sent' for r in report['execution']['held_tasks']))

    def test_named_uncertain_repeat_is_one_time_and_not_a_general_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = 'pro-occurrence-' + 'a' * 64
            request = dict(kind='model', role='pro', reservation_usd='0.1', wire={}, backend='gemini-api')
            failed = dict(complete=False, transport_success=False, dispatch_state='uncertain', error_type='TimeoutError')
            with Ledger(root, {}).locked() as ledger:
                ledger.execute(task, request, lambda _: failed)
            end = list(records(root / 'attempts.jsonl'))[-1]
            proof = dict(task_id=task, prior_attempt_id=end['attempt_id'],
                         request_sha256=end['request_sha256'], prior_result_sha256=end['result_sha256'])
            with Ledger(root, {}, interrupted_attempts=[proof]).locked() as ledger:
                ledger.execute(task, request, lambda _: failed, max_attempts=3, retry_failed=True)
                with self.assertRaises(UncertainAttempt):
                    ledger.execute(task, request, lambda _: self.fail('second uncertain result cannot repeat'),
                                   max_attempts=3, retry_failed=True)
            events = list(records(root / 'attempts.jsonl'))
            with self.assertRaises(ValueError):
                reuse.history(root, events)
            self.assertEqual(reuse.history(root, events, interrupted_attempts=[proof])['started_attempts'], 2)
            wrong = dict(proof, prior_result_sha256='0' * 64)
            with self.assertRaises(ValueError):
                reuse.history(root, events, interrupted_attempts=[wrong])

    def test_recovery_keeps_fixed_route_and_chains_into_next_phase(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            cfg = fixtures.config(root / 'source')
            cfg['questions'] = [question('equal'), question('different')]
            cfg['execution_policy'].update(spending_ceiling_usd='10', max_attempts_per_task=3)
            cfg['execution_policy']['retry_policy'].update(max_total=None, max_model=None, max_search=None)
            source = prepare(cfg, mode='fixture')
            write_private(root / 'source.json', source)
            interrupted = []
            lock = threading.Lock()
            original = fixtures.model

            def first(role, compiled, folder):
                with lock:
                    if role == 'pro' and len(interrupted) < 2:
                        interrupted.append(compiled['packet']['case_id'])
                        return dict(complete=False, transport_success=False, dispatch_state='uncertain', error_type='TimeoutError')
                return original(role, compiled, folder)

            with patch('openweb_news.fixtures.model', side_effect=first), \
                 patch('openweb_news.runner.reservation', return_value=Decimal('0.01')):
                initial = run(source)
            ref = reuse.freeze(root / 'source.json', root / 'snapshot')
            events = list(records(root / 'snapshot/events.jsonl'))
            selected = [e for e in events if e['event'] == 'finished'
                        and e['task_id'] in {'pro-' + c for c in interrupted}]
            cfg['run_directory'] = str(root / 'recovered')
            cfg['reuse_snapshot'] = ref
            cfg[KEY] = dict(profile=PROFILE, source_snapshot=ref, source_package_sha256=digest(source),
                source_code_sha256=digest(source['code']), target_code_sha256=digest(source['code']),
                attempts=[dict(task_id=e['task_id'], prior_attempt_id=e['attempt_id'],
                               request_sha256=e['request_sha256'], prior_result_sha256=e['result_sha256']) for e in selected],
                reason='Explicit synthetic network interruption recovery')
            recovered = prepare(cfg, mode='fixture')
            write_private(root / 'recovered.json', recovered)
            sent = []

            def completion(role, compiled, folder):
                sent.append((role, compiled['packet']['case_id']))
                if role == 'pro' and compiled['packet']['case_id'] == interrupted[1] and sent.count((role, interrupted[1])) == 1:
                    return dict(complete=False, transport_success=False, dispatch_state='not_sent',
                                transport_stage='pre_http_connect', error_type='gaierror', http_status=None)
                if role == 'pro' and compiled['packet']['case_id'] == interrupted[0]:
                    from openweb_news.judging import METHOD
                    v = verdict(compiled, 0)
                    return dict(complete=True, transport_success=True, http_status=200,
                                raw_text=json.dumps(dict(modelVersion=METHOD['primary']['model'],
                                    candidates=[dict(finishReason='STOP', content=dict(parts=[dict(text=json.dumps(v))]))])))
                return original(role, compiled, folder)

            with patch('openweb_news.fixtures.search', side_effect=AssertionError('saved searches must not repeat')), \
                 patch('openweb_news.fixtures.model', side_effect=completion), \
                 patch('openweb_news.runner.reservation', return_value=Decimal('0.01')):
                result = run(recovered, retry_failed=True)
            self.assertEqual(set(sent), {('pro', c) for c in interrupted})
            self.assertEqual(sent.count(('pro', interrupted[1])), 2)
            outcomes = {r['case_id']: r for r in result['outcomes']}
            self.assertFalse(outcomes[interrupted[1]]['third_required'])
            self.assertNotIn('claude', outcomes[interrupted[1]]['positions'])
            self.assertTrue(outcomes[interrupted[0]]['third_required'])
            self.assertIn('claude', outcomes[interrupted[0]]['positions'])
            self.assertEqual(Decimal(result['execution']['committed_and_held_usd']),
                             Decimal(initial['execution']['committed_and_held_usd']) + Decimal('0.03'))
            # The named repeat remains verifiable in a later ordinary expansion.
            cfg['reuse_snapshot'] = reuse.freeze(root / 'recovered.json', root / 'snapshot-2')
            cfg['reuse_scope'] = 'exact-method-expansion-v1'
            cfg['run_directory'] = str(root / 'next-phase')
            cfg['questions'].append(question('additional'))
            expanded = prepare(cfg, mode='fixture')
            with patch('openweb_news.runner.reservation', return_value=Decimal('0.01')):
                result = run(expanded)
            self.assertEqual(len(result['collection']), 6)
            bad = deepcopy(cfg)
            bad[KEY]['attempts'][0]['prior_result_sha256'] = '0' * 64
            with self.assertRaises(ValueError):
                prepare(bad, mode='fixture')


if __name__ == '__main__':
    unittest.main()
