from collections import Counter
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from openweb_news import fixtures, judging, reuse
from openweb_news._method.providers import configuration_catalog, build_request, project_response
from openweb_news.common import asset_bindings, code_bindings, digest, encoded, load, records, write_private
from openweb_news.preparation import prepare
from openweb_news.runner import run
from openweb_news.recovery import sibling_rule
from openweb_news.scheduling import account_bindings
from openweb_news.transport import Credentials
from .helpers import question


ROSTER = ['exa-highlights', 'perplexity-web', 'brave-web', 'firecrawl-web', 'searlo-web',
          'parallel-advanced', 'tavily-news', 'linkup-fast', 'you-com-web', 'valyu-search',
          'oxylabs-search', 'keenable-pro', 'seltz-web-pro']


class ProviderIntegrationTests(unittest.TestCase):
    def test_live_changed_code_migration_keeps_dropped_provider_costs_and_never_repeats(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve()
            cfg=self.config(root/'old')
            cfg['execution_stage']='collection-only-v1'
            source=prepare(cfg,mode='live')
            catalog={p['configuration_id']:p for p in configuration_catalog(include_optional=True)}
            credentials=Credentials(environment={p['auth']['secret_ref']:'synthetic-dummy-'+p['provider'] for p in catalog.values()})
            calls=[]
            def send(request,auth,credentials,folder,*,timeout_seconds):
                calls.append(request['configuration_id'])
                return fixtures.search(request,catalog[request['configuration_id']],folder)
            def approval(package):
                return dict(package_sha256=digest(package),decision='approved',approved_by='synthetic fixture owner',approved_at='2026-11-01T00:00:00Z')
            with patch('openweb_news.runner.Credentials',return_value=credentials),patch('openweb_news.runner.https',side_effect=send):
                first=run(source,approval=approval(source))
            self.assertGreater(float(first['execution']['committed_and_held_usd']),0)
            # Simulate a prior delivered reporting module, without executing old
            # code or modifying any real run. All recorded prices are LIVE prices.
            source['code']['reporting.py']='0'*64
            write_private(root/'source.json',source)
            write_private(root/'old/binding.json',dict(package_sha256=digest(source),mode='live'),replace=True)
            ref=reuse.freeze(root/'source.json',root/'snapshot')
            target=self.config(root/'new',['exa-highlights','seltz-web-pro'])
            target.update(execution_stage='collection-only-v1',reuse_snapshot=ref)
            target['execution_policy']['scheduling']['accounts'].update(cfg['execution_policy']['scheduling']['accounts'])
            target['provider_test_reuse']=self.proof(source,target)
            package=prepare(target,mode='live')  # Used to raise StopIteration on dropped Brave.
            self.assertEqual(package['required_credential_references'], ['OPENWEB_NEWS_SELTZ_API_KEY'])
            from decimal import Decimal
            forecast=package['reservation_forecast']
            carried=Decimal(first['execution']['committed_and_held_usd'])
            self.assertEqual(Decimal(forecast['carried_committed_and_held_usd']),carried)
            self.assertEqual(Decimal(forecast['remaining_admission_usd']),Decimal('1')-carried)
            self.assertEqual(forecast['remaining_search_tasks'],4)
            self.assertEqual(Decimal(forecast['base_search_usd']),Decimal('.04'))
            old=len(calls)
            with patch('openweb_news.runner.Credentials',return_value=credentials),patch('openweb_news.runner.https',side_effect=send),patch('openweb_news.backends.subscription',side_effect=AssertionError('no judge')):
                report=run(package,approval=approval(package))
            self.assertEqual(calls[old:],['seltz-web-pro']*4)
            self.assertGreater(float(report['execution']['committed_and_held_usd']),float(first['execution']['committed_and_held_usd']))
            old_events=list(records(root/'old/attempts.jsonl'))
            self.assertEqual(list(records(root/'new/attempts.jsonl'))[:len(old_events)],old_events)

    def test_live_provider_branch_empty_failure_retry_uncertainty_and_resume_without_models(self):
        for outcome in ('empty','transient','malformed','uncertain','auth'):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as temp:
                root=Path(temp).resolve()
                cfg=self.config(root/'run',['keenable-pro'])
                cfg['questions']=[question()]
                cfg['execution_stage']='collection-only-v1'
                policy=cfg['execution_policy']
                policy['max_attempts_per_task']=3
                policy['retry_policy']=dict(max_total=None,max_search=None,max_model=None,http_statuses=[429]+list(range(500,600)),backoff_seconds=[0,0])
                package=prepare(cfg,mode='live')
                approval=dict(package_sha256=digest(package),decision='approved',approved_by='synthetic fixture owner',approved_at='2026-11-01T00:00:00Z')
                calls=[]
                def send(request,auth,credentials,folder,*,timeout_seconds):
                    calls.append(request)
                    status=503 if outcome=='transient' and len(calls)==1 else 401 if outcome=='auth' else 200
                    raw=encoded({'results':[]}) if outcome!='malformed' else b'not JSON'
                    return dict(raw_text=raw.decode(),http_status=status,complete=outcome!='uncertain',
                                dispatch_state='uncertain' if outcome=='uncertain' else 'completed',transport_success=status==200 and outcome!='uncertain',captured_at='2026-11-01T00:00:00Z')
                credentials=Credentials(environment={'OPENWEB_NEWS_KEENABLE_API_KEY':'synthetic-dummy'})
                with patch('openweb_news.runner.Credentials',return_value=credentials),patch('openweb_news.runner.https',side_effect=send),patch('openweb_news.backends.subscription',side_effect=AssertionError('no subscription preflight')),patch('openweb_news.backends.api',side_effect=AssertionError('no model API')):
                    report=run(package,approval=approval,retry_failed=True)
                    resumed=run(package,approval=approval,retry_failed=True)
                self.assertEqual(len(calls),2 if outcome=='transient' else 1)
                if len(calls)==2:self.assertEqual(calls[0],calls[1])
                self.assertEqual(report['outcomes'],[])
                self.assertEqual(resumed['outcomes'],[])
                expected={'empty':'success_empty','transient':'success_empty','malformed':'invalid_json','uncertain':'incomplete_transport','auth':'authentication_error'}
                self.assertEqual(report['collection'][0]['status'],expected[outcome])
                self.assertEqual(report['scores'],None)

    def config(self, root, selected=None):
        cfg = fixtures.config(root)
        if selected:
            cfg['provider_ids'] = selected
            cfg['tie_order'] = selected
            providers = [p for p in configuration_catalog(include_optional=True) if p['configuration_id'] in selected]
            bindings = account_bindings(providers, cfg['reviewers'], judging.METHOD['primary'])
            cfg['execution_policy']['scheduling']['accounts'] = {
                name: dict(concurrency=2 if name.startswith('provider/') else 4, minimum_start_interval_seconds=0)
                for name in set(bindings.values())}
        return cfg

    def proof(self, source, cfg):
        old = {p['configuration_id'] for p in source['providers']}
        selected = set(cfg['provider_ids'])
        return dict(profile='provider-integration-collection-v1', source_package_sha256=digest(source),
                    source_code_sha256=digest(source['code']), target_code_sha256=digest(code_bindings()),
                    source_assets_sha256=digest(source['assets']), target_assets_sha256=digest(asset_bindings()),
                    retained_configuration_ids=sorted(old & selected), new_configuration_ids=sorted(selected-old))

    def test_collection_only_never_invokes_models_subscription_preflight_cache_or_scores(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            cfg = self.config(root/'run')
            cfg['execution_stage'] = 'collection-only-v1'
            package = prepare(cfg, mode='fixture')
            self.assertEqual(package['scope']['all_model_tasks'], 0)
            self.assertNotIn(judging.METHOD['primary']['secret_ref'], package['required_credential_references'])
            with patch('openweb_news.fixtures.model', side_effect=AssertionError('no model call')), patch('openweb_news.backends.subscription', side_effect=AssertionError('no subscription access')), patch('openweb_news.runner.scoring.score', side_effect=AssertionError('no quality scoring')):
                report = run(package)
            self.assertEqual(report['scores'], None)
            self.assertEqual(report['outcomes'], [])
            self.assertEqual(len(report['collection']), 8)
            self.assertEqual(Counter(e['task_id'].split('-')[0] for e in records(root/'run/attempts.jsonl') if e['event']=='started'), {'search':8})
            self.assertIn('No judge calls', (root/'run/report.html').read_text())
            self.assertFalse((root/'run/metrics.csv').exists())
            with patch('openweb_news.fixtures.search', side_effect=AssertionError('no repeated search')):
                resumed = run(package)
            self.assertEqual(report['collection'], resumed['collection'])

    def test_thirteen_unique_primary_providers_keep_established_settings(self):
        catalog = {p['configuration_id']: p for p in configuration_catalog(include_optional=True)}
        selected = [catalog[cid] for cid in ROSTER]
        self.assertEqual(len({p['provider'] for p in selected}), 13)
        self.assertTrue(catalog['brave-web']['base_body']['extra_snippets'])
        self.assertFalse(catalog['firecrawl-web']['base_body']['highlights'])
        self.assertEqual(catalog['tavily-news']['base_body']['topic'], 'news')
        self.assertEqual(catalog['valyu-search']['base_body']['country_code'], 'ALL')
        self.assertNotIn('snippet_max_length', catalog['keenable-pro']['base_body'])
        self.assertEqual(catalog['seltz-web-pro']['base_body'], dict(max_results=10, scope='web', tier='pro', fields=dict(content=False, snippets=True)))
        with tempfile.TemporaryDirectory() as temp:
            cfg = self.config(Path(temp).resolve()/'run', ROSTER)
            cfg['execution_stage'] = 'collection-only-v1'
            cfg['provider_projection_profiles'] = {'searlo-web':'searlo-native-organic-v1'}
            package = prepare(cfg, mode='fixture')
            with patch('openweb_news.fixtures.model', side_effect=AssertionError('no judge')):
                report = run(package)
            self.assertEqual(len(report['collection']), 52)
            self.assertEqual(report['outcomes'], [])

    def test_seltz_native_snippet_objects_keep_all_indices_and_never_use_body(self):
        provider = next(p for p in configuration_catalog(include_optional=True) if p['configuration_id']=='seltz-web-pro')
        raw = encoded(dict(documents=[dict(url='https://news.example/a', published_date='2026-10-20',
                         snippets=[dict(text='Exact native passage'), dict(text=''), {}, dict(text=123), 'bad', dict(text='Last native passage')])]))
        result = project_response(provider, raw, '2026-11-01T00:00:00Z')
        field = result['positions'][0]['content']
        self.assertEqual(field['native_value'][0], {'text':'Exact native passage'})
        self.assertEqual(field['value'], ['Exact native passage','',None,123,None,'Last native passage'])
        self.assertEqual([item['index'] for item in field['items']], list(range(6)))
        self.assertEqual(field['items'][0]['path'], '$.documents[0].snippets[0].text')
        packet = judging.packet_for(question(), provider, result['positions'][0], 'one')
        self.assertEqual([(r['field'],r['text']) for r in packet['allowed_text']], [('snippet[0]','Exact native passage'),('snippet[1]',''),('snippet[5]','Last native passage')])
        result = project_response(provider, encoded(dict(documents=[dict(url='https://news.example/a', content='Unexpected full article body', snippets=[dict(text='Own snippet')])])), '2026-11-01T00:00:00Z')
        self.assertEqual(result['status'], 'native_product_mismatch')
        self.assertEqual(result['native_product_issues'][0]['reason'], 'unexpected_full_document_content')
        self.assertEqual(result['positions'][0]['content']['value'], ['Own snippet'])

    def test_seltz_publication_mapping_keeps_event_window_out_of_request(self):
        provider = next(p for p in configuration_catalog(include_optional=True) if p['configuration_id']=='seltz-web-pro')
        q = question()
        q['contract']['temporal_requirements'] = [dict(id='p', basis='publication', start_inclusive='2026-10-01T00:00:00Z', end_exclusive='2026-11-01T00:00:00Z')]
        request = build_request(provider, q)
        self.assertEqual(request['body']['from_date'], '2026-10-01T00:00:00+00:00')
        self.assertEqual(request['body']['to_date'], '2026-11-01T00:00:00+00:00')
        q['contract']['temporal_requirements'][0]['basis'] = 'event'
        self.assertNotIn('from_date', build_request(provider,q)['body'])
        self.assertNotIn('to_date', build_request(provider,q)['body'])

    def test_roster_change_carries_all_history_but_dispatches_only_new_provider_cells(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            cfg = self.config(root/'source')
            source = prepare(cfg, mode='fixture')
            write_private(root/'source.json', source)
            initial = run(source)
            snapshot = reuse.freeze(root/'source.json', root/'snapshot')
            target = self.config(root/'new', ['exa-highlights','keenable-pro','seltz-web-pro'])
            target['execution_stage'] = 'collection-only-v1'
            target['reuse_snapshot'] = snapshot
            target['execution_policy']['scheduling']['accounts'].update(cfg['execution_policy']['scheduling']['accounts'])
            target['provider_test_reuse'] = self.proof(source,target)
            package = prepare(target, mode='fixture')
            calls=[]
            original=fixtures.search
            def search(request,provider,folder):
                calls.append(provider['configuration_id'])
                return original(request,provider,folder)
            with patch('openweb_news.fixtures.search', side_effect=search), patch('openweb_news.fixtures.model', side_effect=AssertionError('no regrading')):
                report = run(package)
            self.assertEqual(Counter(calls), {'keenable-pro':4,'seltz-web-pro':4})
            events = list(records(root/'new/attempts.jsonl'))
            original_events = list(records(root/'source/attempts.jsonl'))
            self.assertEqual(events[:len(original_events)], original_events)
            self.assertEqual(report['outcomes'], [])
            self.assertEqual(len(initial['outcomes']),8)
            self.assertEqual(len(report['collection']),12)
            bad = deepcopy(target)
            bad.pop('execution_stage')
            with self.assertRaises(ValueError):
                prepare(bad,mode='fixture')
            bad = deepcopy(target)
            bad['provider_overrides'] = {'exa-highlights':dict(secret_ref='different-account')}
            with self.assertRaises(ValueError):
                prepare(bad,mode='fixture')
