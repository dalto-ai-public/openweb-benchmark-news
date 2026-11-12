from copy import deepcopy
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from openweb_news import fixtures, judging, reuse
from openweb_news.common import digest, encoded, load, records, write_private
from openweb_news.preparation import prepare
from openweb_news.recovery import sibling_rule
from openweb_news.runner import run
from openweb_news.scheduling import Admission, Pipeline
from .helpers import question


class GeographyProjectionTests(unittest.TestCase):
    def source(self, root, fault=None):
        cfg = fixtures.config(root / 'old')
        cfg['questions'] = [question('draft'), question('unchanged')]
        cfg['questions'][0]['geography'] = dict(applies_to='Only geography explicitly stated in the query; no inferred headquarters.', regions=['unspecified'])
        cfg['execution_policy']['scheduling']['recovery'] = {k: sibling_rule('provider' if k.startswith('provider/') else 'judge', v['concurrency']) for k, v in cfg['execution_policy']['scheduling']['accounts'].items()}
        for rule in cfg['execution_policy']['scheduling']['recovery'].values():
            rule['cooldown_seconds'] = 0
        original = fixtures.model
        def model(role, compiled, folder):
            raw = original(role, compiled, folder)
            if role != 'codex' or compiled['packet']['question']['question_id'] != 'draft':
                return raw
            blind = judging.blind_packet(compiled)
            thread, turn = 'thread-' + compiled['packet']['case_id'], 'turn-' + compiled['packet']['case_id']
            if fault != 'valid':
                for candidate in raw['verdict']['candidates']:
                    candidate['conditions'] = [c for c in candidate['conditions'] if c['predicate_id'] != 'geography']
            raw.update(complete=True, dispatch_state='completed', account_mode='subscription', auth_observation='chatgpt',
                       selected_transport='app-server-stdio-v1', thread_id=thread, turn_id=turn)
            write_private(folder/'runtime-thread.json', dict(instructionSources=[], model='synthetic-model', modelProvider='openai',
                reasoningEffort='high', approvalPolicy='never', sandbox=dict(type='readOnly', networkAccess=False), thread=dict(id=thread, ephemeral=True, turns=[])))
            events = [dict(method='item/completed', params=dict(threadId=thread, item=dict(type='userMessage', content=[dict(text=encoded(blind['input']).decode())]))),
                      dict(method='item/completed', params=dict(threadId=thread, item=dict(type='agentMessage', text=json.dumps(raw['verdict'])))),
                      dict(method='turn/completed', params=dict(threadId=thread, turn=dict(id=turn, status='completed')))]
            if fault == 'native-error':
                events.insert(1, dict(method='error', params=dict(threadId=thread, error=dict(codexErrorInfo='rateLimitExceeded'), willRetry=True)))
            if fault == 'wrong-echo':
                events[0]['params']['item']['content'][0]['text'] = '{}'
            if fault == 'uncertain':
                raw.update(transport_success=False, complete=False, dispatch_state='uncertain')
            write_private(folder/'runtime-events.jsonl', b''.join(encoded(e) for e in events))
            return raw
        # Simulate a ledger admitted by the pre-fix runner; current admission must reject it.
        with patch('openweb_news.preparation.validate_geography'), patch('openweb_news.fixtures.model', side_effect=model):
            source = prepare(cfg, mode='fixture')
            report = run(source)
        write_private(root/'source.json', source)
        return cfg, source, report

    def target(self, root, cfg, source):
        cfg = deepcopy(cfg)
        cfg['run_directory'] = str(root/'new')
        cfg['questions'][0]['geography'] = dict(applies_to='no_geographic_restriction', regions=['global'])
        cfg['questions'][0]['revision'] += 1
        baseline = prepare(cfg, mode='fixture')
        cfg['reuse_snapshot'] = reuse.freeze(root/'source.json', root/'snapshot')
        cfg['question_projection_compatibility'] = dict(profile='canonical-unrestricted-geography-v1', source_package_sha256=digest(source),
            target_question_bindings_sha256=digest(baseline['question_bindings']), question_ids=['draft'])
        return cfg

    def test_admission_rejects_unresolved_draft_before_creating_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = fixtures.config(root/'never-created')
            cfg['questions'][0]['geography']['regions'] = ['unspecified']
            with self.assertRaises(ValueError):
                prepare(cfg, mode='live')
            self.assertFalse((root/'never-created').exists())

    def test_projection_reuses_searches_and_unchanged_votes_and_uses_distinct_changed_tasks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            cfg, source, old = self.source(root)
            target_cfg = self.target(root, cfg, source)
            target = prepare(target_cfg, mode='fixture')
            self.assertEqual(source['search_requests'], target['search_requests'])
            original = fixtures.model
            calls = []
            def model(role, compiled, folder):
                calls.append((role, compiled['packet']['question']['question_id']))
                return original(role, compiled, folder)
            with patch('openweb_news.fixtures.search', side_effect=AssertionError('no recollection')), patch('openweb_news.fixtures.model', side_effect=model):
                result = run(target)
            self.assertTrue(calls)
            self.assertTrue(all(qid == 'draft' for _, qid in calls))
            old_rows = {o['case_id']: o for o in old['outcomes'] if o['question_id'] == 'unchanged'}
            self.assertEqual({o['case_id']: o for o in result['outcomes'] if o['question_id'] == 'unchanged'}, old_rows)
            old_changed = {o['case_id'] for o in old['outcomes'] if o['question_id'] == 'draft'}
            new_changed = {o['case_id'] for o in result['outcomes'] if o['question_id'] == 'draft'}
            self.assertFalse(old_changed & new_changed)
            self.assertEqual(len(reuse.superseded_geography_health(target)), 2)
            target_cfg['questions'][0]['provider_query'] += ' changed query'
            with self.assertRaises(ValueError):
                prepare(target_cfg, mode='fixture')

    def test_native_error_or_wrong_echo_does_not_retire_health_and_uncertainty_blocks_projection(self):
        for fault in ('native-error', 'wrong-echo', 'uncertain'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp).resolve()
                cfg, source, _ = self.source(root, fault)
                target_cfg = self.target(root, cfg, source)
                if fault == 'uncertain':
                    with self.assertRaises(ValueError):
                        prepare(target_cfg, mode='fixture')
                else:
                    target = prepare(target_cfg, mode='fixture')
                    self.assertEqual(reuse.superseded_geography_health(target), set())

    def test_old_valid_successes_are_not_counted_for_new_trial_advancement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            cfg, source, _ = self.source(root, 'valid')
            target = prepare(self.target(root, cfg, source), mode='fixture')
            retired = reuse.superseded_geography_health(target)
            self.assertEqual(len(retired), 2)
            report = run(target)
            self.assertEqual(report['execution']['committed_and_held_usd'], '0')
            health = next(s for s in report['execution']['recovery'] if s['account'] == 'codex-subscription' and s['configuration'] is None)
            self.assertEqual(health['successes'], 4)  # Two unchanged + two corrected; not two retired successes.
            self.assertEqual(health['pauses'], 0)

    def test_retirement_and_collection_revisions_survive_normal_chained_reuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            cfg, source, _ = self.source(root, 'valid')
            target_cfg = self.target(root, cfg, source)
            candidate = prepare(target_cfg, mode='fixture')
            retired = reuse.superseded_geography_health(candidate)
            receipt = dict(profile='canonical-geography-health-retirement-v1', source_snapshot=target_cfg['reuse_snapshot'],
                           projection=target_cfg['question_projection_compatibility'], projected_questions=candidate['questions'],
                           attempt_ids=sorted(retired))
            write_private(root/'retired.json', receipt)
            target_cfg['semantic_health_retirement'] = dict(manifest_path=str(root/'retired.json'), sha256=digest(receipt))
            target = prepare(target_cfg, mode='fixture')
            first = run(target)
            write_private(root/'target.json', target)
            chained_cfg = deepcopy(target_cfg)
            chained_cfg['run_directory'] = str(root/'third')
            chained_cfg['reuse_snapshot'] = reuse.freeze(root/'target.json', root/'snapshot-two')
            del chained_cfg['question_projection_compatibility']
            chained = prepare(chained_cfg, mode='fixture')
            self.assertEqual(chained['search_requests'], source['search_requests'])
            self.assertEqual(reuse.superseded_geography_health(chained), retired)
            with patch('openweb_news.fixtures.search', side_effect=AssertionError('no repeat')), patch('openweb_news.fixtures.model', side_effect=AssertionError('no repeat')):
                result = run(chained)
            self.assertEqual(result['outcomes'], first['outcomes'])
            self.assertEqual(result['execution']['recovery'], first['execution']['recovery'])
            del chained_cfg['semantic_health_retirement']
            with self.assertRaises(ValueError):
                prepare(chained_cfg, mode='fixture')
