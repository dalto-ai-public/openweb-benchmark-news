from collections import Counter
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from openweb_news import fixtures, judging, reuse, subscription_health
from openweb_news._method.providers import configuration_catalog
from openweb_news.common import asset_bindings, code_bindings, digest, load, records, write_private
from openweb_news.preparation import prepare, verify_package
from openweb_news.runner import run
from openweb_news.transport import Credentials
from .helpers import question
from . import test_provider_integration as provider_tests
from . import test_subscription_health as health_tests


def proof(source):
    return dict(profile='provider-integration-benchmark-v1', source_package_sha256=digest(source),
        source_code_sha256=digest(source['code']), target_code_sha256=digest(code_bindings()),
        source_assets_sha256=digest(source['assets']), target_assets_sha256=digest(asset_bindings()))


class BenchmarkContinuationTests(unittest.TestCase):
    def config(self, directory, ids=None):
        cfg = provider_tests.ProviderIntegrationTests().config(directory, ids)
        cfg['questions'] = [question('carry'), question('synthetic-disagreement')]
        cfg['execution_policy']['spending_ceiling_usd'] = '150'
        return cfg

    def target(self, root, source, *, name='judging'):
        write_private(root/'collection.json', source)
        cfg = dict(deepcopy(source['configuration']), questions=deepcopy(source['questions']),
                   provider_ids=[p['configuration_id'] for p in source['providers']], run_directory=str(root/name),
                   reuse_snapshot=reuse.freeze(root/'collection.json', root/(name+'-snapshot')))
        cfg.pop('execution_stage')
        cfg.pop('provider_test_reuse', None)
        cfg['benchmark_continuation_reuse'] = proof(source)
        return cfg

    def test_collection_to_judging_reuses_votes_and_dropped_history_then_resume_and_chain(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            initial_cfg = self.config(root/'initial')
            initial = prepare(initial_cfg, mode='fixture')
            write_private(root/'initial.json', initial)
            run(initial)
            cfg = self.config(root/'collection', ['exa-highlights', 'keenable-pro', 'seltz-web-pro'])
            cfg.update(execution_stage='collection-only-v1', reuse_snapshot=reuse.freeze(root/'initial.json', root/'old'))
            cfg['execution_policy']['scheduling']['accounts'].update(initial_cfg['execution_policy']['scheduling']['accounts'])
            cfg['provider_test_reuse'] = provider_tests.ProviderIntegrationTests().proof(initial, cfg)
            collection = prepare(cfg, mode='fixture')
            run(collection)
            # Model an installed earlier preparation module; accounting, prompts,
            # projections and all provider definitions are unchanged.
            collection['code']['preparation.py'] = '0'*64
            write_private(root/'collection/binding.json', dict(package_sha256=digest(collection), mode='fixture'), replace=True)
            target_cfg = self.target(root, collection)
            package = prepare(target_cfg, mode='fixture')
            verify_package(package)
            self.assertEqual(package['required_credential_references'], [judging.METHOD['primary']['secret_ref']])
            self.assertEqual(package['continuation_plan']['counts'], {
                'pro/retained-no-send':2, 'codex/retained-no-send':2, 'claude/not-in-fixed-route':1,
                'claude/retained-no-send':1, 'pro/new':4, 'codex/new':4, 'claude/conditional-new':4})
            calls = []
            original = fixtures.model
            def model(role, compiled, folder):
                calls.append((role, compiled['packet']['case_id']))
                return original(role, compiled, folder)
            prefix = list(records(root/'collection/attempts.jsonl'))
            with patch('openweb_news.fixtures.search', side_effect=AssertionError('no repeated collection')), patch('openweb_news.fixtures.model', side_effect=model):
                report = run(package)
            self.assertEqual(Counter(role for role, _ in calls), {'pro':4, 'codex':4, 'claude':2})
            self.assertEqual(len(calls), len(set(calls)))
            self.assertEqual(len(report['outcomes']), 6)
            self.assertEqual(list(records(root/'judging/attempts.jsonl'))[:len(prefix)], prefix)
            with patch('openweb_news.fixtures.search', side_effect=AssertionError('no search')), patch('openweb_news.fixtures.model', side_effect=AssertionError('no repeat')):
                self.assertEqual(run(package)['outcome_counts'], report['outcome_counts'])
            write_private(root/'judging.json', package)
            target_cfg.update(run_directory=str(root/'chain'), reuse_snapshot=reuse.freeze(root/'judging.json', root/'chain-snapshot'))
            chained = prepare(target_cfg, mode='fixture')
            self.assertEqual(chained['required_credential_references'], [])
            self.assertEqual(chained['scope']['dispatchable_primary_tasks'], 0)
            self.assertEqual(chained['scope']['dispatchable_second_tasks'], 0)
            with patch('openweb_news.fixtures.search', side_effect=AssertionError('no search')), patch('openweb_news.fixtures.model', side_effect=AssertionError('no repeat')):
                self.assertEqual(run(chained)['outcome_counts'], report['outcome_counts'])
            target_cfg.pop('benchmark_continuation_reuse')
            with self.assertRaisesRegex(ValueError, 'adopted benchmark migration|scheduling accounts'):
                prepare(target_cfg, mode='fixture')

    def test_live_prices_survive_a_second_hop_with_excluded_providers_and_exact_approval(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            cfg = self.config(root/'initial')
            cfg['execution_stage'] = 'collection-only-v1'
            initial = prepare(cfg, mode='live')
            catalog = {p['configuration_id']:p for p in initial['providers']}
            credentials = Credentials(environment={p['auth']['secret_ref']:'synthetic-dummy' for p in initial['providers']})
            def approve(p):
                return dict(package_sha256=digest(p), decision='approved', approved_by='synthetic owner', approved_at='2026-11-01T00:00:00Z')
            def send(request, auth, credentials, folder, **kwargs):
                return fixtures.search(request, catalog[request['configuration_id']], folder)
            with patch('openweb_news.runner.Credentials', return_value=credentials), patch('openweb_news.runner.https', side_effect=send):
                first = run(initial, approval=approve(initial))
            write_private(root/'initial.json', initial)
            cfg = self.config(root/'collection', ['exa-highlights'])
            cfg.update(execution_stage='collection-only-v1', reuse_snapshot=reuse.freeze(root/'initial.json', root/'old'))
            cfg['execution_policy']['scheduling']['accounts'].update(initial['configuration']['execution_policy']['scheduling']['accounts'])
            cfg['provider_test_reuse'] = provider_tests.ProviderIntegrationTests().proof(initial, cfg)
            collection = prepare(cfg, mode='live')
            with patch('openweb_news.runner.Credentials', return_value=credentials), patch('openweb_news.runner.https', side_effect=AssertionError('no repeat')):
                run(collection, approval=approve(collection))
            collection['code']['preparation.py'] = '0'*64
            write_private(root/'collection/binding.json', dict(package_sha256=digest(collection),mode='live'),replace=True)
            target_cfg = self.target(root, collection)
            package = prepare(target_cfg, mode='live')
            forecast = package['reservation_forecast']
            self.assertEqual(forecast['carried_committed_and_held_usd'], first['execution']['committed_and_held_usd'])
            self.assertGreater(float(forecast['base_model_usd']), 0)
            self.assertEqual(forecast['base_search_usd'], '0')
            self.assertEqual(package['required_credential_references'], [judging.METHOD['primary']['secret_ref']])
            with patch('openweb_news.runner.Credentials', side_effect=AssertionError('approval before credentials')):
                with self.assertRaisesRegex(ValueError, 'exact execution package'):
                    run(package, approval=approve(collection))
            self.assertFalse((root/'judging').exists())

    def test_changed_inputs_or_unbound_migration_fail_before_run(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            cfg = self.config(root/'collection')
            cfg['execution_stage'] = 'collection-only-v1'
            source = prepare(cfg, mode='fixture')
            run(source)
            target = self.target(root, source)
            prepare(target, mode='fixture')
            for fault in ('missing', 'hash', 'question', 'reviewer', 'provider', 'method', 'code'):
                bad = deepcopy(target)
                kwargs = {}
                if fault == 'missing': bad.pop('benchmark_continuation_reuse')
                elif fault == 'hash': bad['benchmark_continuation_reuse']['source_package_sha256'] = '0'*64
                elif fault == 'question': bad['questions'][0]['revision'] += 1
                elif fault == 'reviewer': bad['reviewers']['codex']['reasoning_effort'] = 'low'
                elif fault == 'provider': bad['provider_overrides'] = {'exa-highlights':dict(secret_ref='OTHER')}
                elif fault == 'method':
                    assets = asset_bindings(); assets['method.json'] = '1'*64
                    bad['benchmark_continuation_reuse']['target_assets_sha256'] = digest(assets)
                    kwargs['assets'] = assets
                else:
                    code = code_bindings(); code['judging.py'] = '1'*64
                    bad['benchmark_continuation_reuse']['target_code_sha256'] = digest(code)
                    kwargs['code'] = code
                with self.subTest(fault=fault), patch('openweb_news.preparation.asset_bindings', return_value=kwargs.get('assets',asset_bindings())), patch('openweb_news.preparation.code_bindings', return_value=kwargs.get('code',code_bindings())):
                    with self.assertRaises(ValueError): prepare(bad, mode='fixture')
            self.assertFalse((root/'judging').exists())

    def test_cache_rates_and_lifecycle_cannot_change_during_migration_or_chain(self):
        from .test_caching import cache_config
        from openweb_news import caching
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve()
            cfg=self.config(root/'collection')
            cfg.update(execution_stage='collection-only-v1',system_cache=cache_config())
            cfg['execution_policy']['budget_accounting']=caching.PROFILE
            empty=dict(status='native_result',title=dict(state='missing'),content=dict(state='missing'),url=dict(state='missing'),metadata={},additional_content={},native_dates=dict(observations=[]))
            provider=next(p for p in configuration_catalog(include_optional=True) if p['configuration_id']==cfg['provider_ids'][0])
            for q in cfg['questions']:
                wire=judging.build_pro_request(judging.compile_packet(judging.packet_for(q,provider,empty,'cache-plan'),'2000-01-01T00:00:00Z',config=provider))
                cfg['system_cache']['estimated_system_tokens'][digest(caching.payload(wire))]=4500
            source=prepare(cfg,mode='fixture');run(source)
            target=self.target(root,source)
            package=prepare(target,mode='fixture');run(package)
            write_private(root/'judging.json',package)
            chained=deepcopy(target)
            chained.update(run_directory=str(root/'chain'),reuse_snapshot=reuse.freeze(root/'judging.json',root/'chain-snapshot'))
            for original in (target,chained):
                for key,value in [('cached_input_per_million_usd','0'),('ttl_seconds',7200)]:
                    changed=deepcopy(original);changed['system_cache'][key]=value
                    with self.subTest(chain=original is chained,key=key), self.assertRaisesRegex(ValueError,'cache accounting/lifecycle'):
                        prepare(changed,mode='fixture')

    def test_saved_eligible_http_retry_is_planned_with_credentials_and_conditional_third(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve()
            cfg=self.config(root/'initial',['exa-highlights']);cfg['questions']=[question('carry')]
            cfg['execution_policy']['max_attempts_per_task']=3
            cfg['execution_policy']['retry_policy'].update(max_total=None,max_model=None,max_search=None,http_statuses=[503],backoff_seconds=[0,0])
            initial=prepare(cfg,mode='fixture');write_private(root/'initial.json',initial)
            original=fixtures.model
            def fail(role,compiled,folder):
                raw=original(role,compiled,folder)
                if role=='pro':raw.update(transport_success=False,http_status=503)
                elif role=='codex':raw['verdict']['candidates'][0]['support'][0]['quote']=''
                return raw
            with patch('openweb_news.fixtures.model',side_effect=fail):run(initial)
            cfg.update(execution_stage='collection-only-v1',run_directory=str(root/'collection'),reuse_snapshot=reuse.freeze(root/'initial.json',root/'old'))
            cfg['provider_test_reuse']=provider_tests.ProviderIntegrationTests().proof(initial,cfg)
            collection=prepare(cfg,mode='fixture');run(collection)
            target=self.target(root,collection);package=prepare(target,mode='fixture')
            self.assertEqual(package['continuation_plan']['counts'],{'pro/covered-retry':1,'codex/retained-no-send':1,'claude/conditional-new':1})
            self.assertEqual(package['required_credential_references'],[judging.METHOD['primary']['secret_ref']])
            self.assertEqual([b['maximum_new_attempts']for b in package['continuation_plan']['request_bindings']],[2,0,3])
            calls=[]
            def model(role,compiled,folder):calls.append(role);return original(role,compiled,folder)
            with patch('openweb_news.fixtures.search',side_effect=AssertionError('no search')),patch('openweb_news.fixtures.model',side_effect=model):
                run(package,retry_failed=True)
            self.assertEqual(calls,['pro','claude'])
            # Exhausted shared retry allowance turns the saved failure back into
            # retained work; it does not require a key or a fabricated send.
            target['execution_policy']['retry_policy']['max_model']=0
            exhausted=prepare(target,mode='fixture')
            self.assertEqual(exhausted['required_credential_references'],[])
            self.assertEqual(exhausted['continuation_plan']['counts']['pro/retained-no-send'],1)

    def test_failed_or_uncertain_search_never_becomes_permission_to_recollect(self):
        for fault in ('failed', 'uncertain', 'empty'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as temp:
                root = Path(temp).resolve()
                cfg = self.config(root/'collection')
                cfg['execution_stage'] = 'collection-only-v1'
                source = prepare(cfg, mode='fixture')
                original = fixtures.search
                def search(request, provider, folder):
                    raw = original(request, provider, folder)
                    if fault == 'failed': raw.update(transport_success=False,http_status=503)
                    elif fault == 'uncertain': raw.update(complete=False,dispatch_state='uncertain')
                    else:
                        from openweb_news.common import encoded
                        payload = []
                        for part in reversed(provider['result_fields']['container'].split('.')): payload = {part:payload}
                        raw['raw_text'] = encoded(payload).decode()
                    return raw
                with patch('openweb_news.fixtures.search', side_effect=search): run(source)
                target = self.target(root, source)
                if fault == 'empty':
                    package = prepare(target, mode='fixture')
                    self.assertEqual(package['continuation_plan']['current_occurrences'], 0)
                    with patch('openweb_news.fixtures.search', side_effect=AssertionError('no empty retry')), patch('openweb_news.fixtures.model', side_effect=AssertionError('no empty judges')):
                        self.assertEqual(run(package)['outcomes'], [])
                else:
                    with self.assertRaisesRegex(ValueError, 'successful saved collection'): prepare(target, mode='fixture')
                if fault != 'empty': self.assertFalse((root/'judging').exists())

    def test_old_codex_stop_survives_collection_and_needs_a_separate_exact_epoch(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            cfg = health_tests.NativeHealthTests().config(root)
            cfg['questions'] = [question('cohort-'+str(i)) for i in range(54)]
            cfg['execution_policy']['scheduling']['accounts']['codex-subscription']['concurrency'] = 1
            cfg['execution_policy']['scheduling']['recovery']['codex-subscription'].update(initial_concurrency=1,maximum_concurrency=1,reduced_concurrency=1)
            initial = prepare(cfg, mode='fixture')
            write_private(root/'initial.json', initial)
            with patch('openweb_news.fixtures.model', side_effect=health_tests.native_model(fixtures.model)): run(initial)
            cfg.update(execution_stage='collection-only-v1',run_directory=str(root/'collection'),reuse_snapshot=reuse.freeze(root/'initial.json',root/'old'))
            cfg['provider_test_reuse'] = provider_tests.ProviderIntegrationTests().proof(initial,cfg)
            collection = prepare(cfg, mode='fixture')
            run(collection)
            target = self.target(root,collection)
            stopped = prepare(target,mode='fixture')
            with patch('openweb_news.fixtures.search',side_effect=AssertionError('no search')), patch('openweb_news.fixtures.model',side_effect=AssertionError('old stop remains')):
                held = run(stopped)
            state = next(s for s in held['execution']['recovery'] if s['account']=='codex-subscription' and s['configuration'] is None)
            self.assertEqual(state['mode'],'stopped')
            target['run_directory'] = str(root/'epoch')
            target['subscription_completion_health'] = dict(profile=subscription_health.PROFILE,roles=['codex'])
            target['subscription_health_transition'] = dict(profile='native-subscription-health-epoch-v1',source_snapshot=target['reuse_snapshot'],
                source_package_sha256=digest(collection),roles=['codex'],
                attempt_ids=sorted(e['attempt_id'] for e in records(root/'collection/attempts.jsonl') if e['event']=='finished' and e['task_id'].startswith('codex-')),
                archived_recovery_sha256=digest(load(root/'collection/report.json')['execution']['recovery']))
            from .test_concurrency import configuration
            historical = sorted(e['task_id'][6:] for e in records(root/'collection/attempts.jsonl') if e['event']=='finished' and e['task_id'].startswith('codex-'))
            remaining = sorted(r['case_id'] for r in held['outcomes'] if r['case_id'] not in historical)
            self.assertEqual(len(remaining),102)
            target['execution_policy']['scheduling']['accounts']['codex-subscription']['concurrency']=4
            target['execution_policy']['scheduling']['recovery']['codex-subscription'].update(initial_concurrency=4,maximum_concurrency=16,reduced_concurrency=2)
            target['codex_concurrency_trial']=dict(configuration(),historical_cases=historical,remaining_case_cohorts={case:i//34 for i,case in enumerate(remaining)})
            package = prepare(target,mode='fixture')
            with patch('openweb_news.fixtures.search',side_effect=AssertionError('no search')), patch('openweb_news.fixtures.model',side_effect=health_tests.native_model(fixtures.model)):
                report = run(package)
            self.assertEqual([s['measured_tasks'] for s in report['execution']['codex_concurrency_trial']['stages']],[34,34,34])
            self.assertTrue(all(r['positions']['codex']['state']=='invalid' for r in report['outcomes']))
            state=next(s for s in report['execution']['recovery'] if s['account']=='codex-subscription' and s['configuration'] is None)
            self.assertEqual((state['mode'],state['pauses']),('normal',0))
