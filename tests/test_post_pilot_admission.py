from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from openweb_news import fixtures
from openweb_news._method.providers import configuration_catalog, project_response
from openweb_news.common import digest, encoded, records, write_private
from openweb_news.preparation import prepare, unresolved_window_paths, validate_geography
from openweb_news.projection import apply_profiles
from openweb_news.reuse import freeze
from openweb_news.runner import run
from .helpers import question, native_response


class PostPilotAdmissionTests(unittest.TestCase):
    profiles = {'searlo-news': 'searlo-native-news-v1', 'searlo-web': 'searlo-native-organic-v1'}

    def test_searlo_profile_is_explicit_no_fallback_and_keeps_native_order_and_bytes(self):
        for old in configuration_catalog():
            if old['configuration_id'] not in self.profiles:
                continue
            provider = apply_profiles([old], {old['configuration_id']: self.profiles[old['configuration_id']]})[0]
            native = native_response(provider)
            container = provider['result_fields']['container']
            native[container] *= 2
            native['searchParameters'] = dict(q='synthetic query')
            raw = encoded(native)
            self.assertEqual(project_response(old, raw, '2026-11-01T00:00:00Z')['status'], 'missing_results')
            result = project_response(provider, raw, '2026-11-01T00:00:00Z')
            self.assertEqual(result['raw_sha256'], digest(raw))
            self.assertEqual([p['path'] for p in result['positions']], [f'$.{container}[0]', f'$.{container}[1]'])
            self.assertEqual(result['response_metadata']['searchParameters']['value'], native['searchParameters'])
            self.assertEqual(project_response(provider, encoded({container: []}), '2026-11-01T00:00:00Z')['status'], 'success_empty')
            self.assertEqual(project_response(provider, encoded({'items': []}), '2026-11-01T00:00:00Z')['status'], 'missing_results')
            self.assertEqual(project_response(provider, encoded({container: None}), '2026-11-01T00:00:00Z')['status'], 'null_results')
            self.assertEqual(project_response(provider, encoded({container: {}}), '2026-11-01T00:00:00Z')['status'], 'non_list_results')
            self.assertEqual(old['result_fields']['container'], 'items')

    def test_bright_empty_body_stays_failure_distinct_from_native_empty_list(self):
        provider = next(p for p in configuration_catalog() if p['configuration_id']=='bright-data-serp')
        for raw in [b'', b' \n']:
            projected = project_response(provider, raw, '2026-11-01T00:00:00Z')
            self.assertEqual(projected['status'], 'invalid_json')
            self.assertEqual(projected['failure_detail'], 'empty_response_body')
            self.assertEqual(projected['positions'], [])
        self.assertEqual(project_response(provider, b'{"organic":[]}', '2026-11-01T00:00:00Z')['status'], 'success_empty')

    def test_saved_searlo_reprojection_reuses_all_searches_and_existing_judgments(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            cfg = fixtures.config(root/'old')
            cfg['questions'] = [question()]
            cfg['provider_ids'] = ['brave-news', 'searlo-news', 'searlo-web']
            cfg['tie_order'] = cfg['provider_ids']
            accounts = cfg['execution_policy']['scheduling']['accounts']
            accounts.pop('provider/OPENWEB_NEWS_EXA_API_KEY')
            accounts['provider/OPENWEB_NEWS_SEARLO_API_KEY'] = dict(concurrency=2, minimum_start_interval_seconds=0)
            original_search = fixtures.search
            def search(request, provider, folder):
                cid = provider['configuration_id']
                if cid not in self.profiles:
                    return original_search(request, provider, folder)
                selected = apply_profiles([provider], {cid: self.profiles[cid]})[0]
                raw = encoded(native_response(selected))
                return dict(raw_text=raw.decode(), http_status=200, complete=True, transport_success=True, captured_at='2026-11-01T00:00:00Z')
            source = prepare(cfg, mode='fixture')
            write_private(root/'source.json', source)
            with patch('openweb_news.fixtures.search', side_effect=search):
                first = run(source)
            self.assertEqual(len(first['outcomes']), 1)
            cfg['reuse_snapshot'] = freeze(root/'source.json', root/'snapshot')
            cfg['run_directory'] = str(root/'new')
            cfg['provider_projection_profiles'] = self.profiles
            target = prepare(cfg, mode='fixture')
            calls = []
            original_model = fixtures.model
            def model(role, compiled, folder):
                calls.append((role, compiled['packet']['case_id']))
                return original_model(role, compiled, folder)
            with patch('openweb_news.fixtures.search', side_effect=AssertionError('no paid recollection')), patch('openweb_news.fixtures.model', side_effect=model):
                result = run(target)
            self.assertEqual(len(result['outcomes']), 3)
            self.assertEqual(len(calls), 4)
            self.assertTrue(all(case != first['outcomes'][0]['case_id'] for _, case in calls))
            self.assertEqual(sum(e['event']=='started' and e['task_id'].startswith('search-') for e in records(root/'new/attempts.jsonl')), 3)

    def test_question_admission_rejects_unresolved_or_contradictory_draft_semantics(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg = fixtures.config(Path(temp).resolve()/'run')
            for geography in [dict(applies_to='', regions=['Europe']), dict(applies_to='event location', regions=[' unspecified ']),
                              dict(applies_to='no_geographic_restriction', regions=['Europe'])]:
                q = question()
                q['geography'] = geography
                with self.assertRaises(ValueError):
                    validate_geography(q)
            candidate = deepcopy(cfg)
            candidate['questions'][0]['provider_query_template'] = 'Legitimate reusable date template {window}'
            prepare(candidate, mode='fixture')
            candidate['questions'][0]['contract']['qualifying_predicates'] = ['Event in {window}']
            self.assertEqual(unresolved_window_paths(candidate['questions'][0]['contract']), ['$.qualifying_predicates[0]'])
            with self.assertRaisesRegex(ValueError, 'unresolved'):
                prepare(candidate, mode='fixture')
            q = question()
            q['geography'] = dict(applies_to='plant location', regions=['Europe'])
            validate_geography(q)
            self.assertEqual(q['geography']['regions'], ['Europe'])
