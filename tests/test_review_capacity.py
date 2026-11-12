"""Capacity, exact evidence coverage and verified unchanged-case selection."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from openweb_news import review_capacity as capacity
from openweb_news.common import digest


def source(count=5):
    prompt = 'A shared synthetic method document. ' * 40
    ident = digest(prompt)
    cases, outcomes, reviews = [], [], []
    for i in range(count):
        cid = f'case-{i}'
        cases.append(dict(compiled=dict(packet=dict(case_id=cid), prompt=dict(shared_document_sha256=ident)),
            raw='évidence-' * 650, original=dict(case_id=cid), unknown_fields=dict(retained=True)))
        outcomes.append(dict(case_id=cid, grade=None, positions={'raw':i}))
        reviews.append(dict(request=dict(case_id=cid, stage='publication', input={'original':i}),
                            response=dict(verdict={'unresolved':True})))
    return dict(cases=cases, report=dict(outcomes=outcomes, rows=[dict(provider='fictional', score=i) for i in range(count)], empty=[]),
        review_attempts=reviews, collection_requests=[], required_inspection=['inputs', 'dates', 'changes', 'ranks'],
        shared_documents={ident:prompt}, extra=dict(original=['retained', None, {}]))


def packet(data):
    return dict(prompt='Inspect every complete supplied unit; no guessing.', input=data,
                schema=dict(type='object'))


def declaration(window=16000, output=2000):
    return dict(model='synthetic', context_window_tokens=window,
                max_output_tokens=output, scaffolding_reserve_tokens=1000, source='Synthetic fixture only.')


def verdict(part, **changes):
    return dict(decision='continue', material_method_change=False,
        unexplained_material_differences=[], invariant_failures=[],
        inspection={k:'Synthetic inspection of '+k for k in part['packet']['input']['required_inspection']},
        reviewed_unit_ids=part['packet']['input']['unit_ids'], **changes)


class ReviewCapacityTests(unittest.TestCase):
    def test_partition_preserves_all_cases_reviews_unknown_fields_unicode_and_metadata(self):
        original = source()
        plan = capacity.plan(original, {'scope':'synthetic'}, declaration(), 30, packet)
        self.assertGreater(len(plan['parts']), 1)
        self.assertEqual(capacity.restore(plan), original)
        self.assertEqual(plan['source_sha256'], digest(original))
        for part in plan['parts']:
            self.assertTrue(capacity.measure(part['packet'], declaration())['fits'])
            for unit in part['packet']['input']['units']:
                if unit['kind'] == 'case':
                    self.assertEqual(len(unit['fragments']), 3)
        dropped = deepcopy(plan)
        dropped['parts'][0]['packet']['input']['units'].pop()
        with self.assertRaisesRegex(capacity.CapacityError, 'coverage'):
            capacity.restore(dropped)

    def test_reuse_rebases_paths_and_resends_changed_or_re_reviewed_cases(self):
        old = source(4)
        first = capacity.plan(old, {}, declaration(), 30, packet)
        authority = dict(plan={'sha256':digest(first)}, review={'sha256':'synthetic-continuing-review'})
        previous = dict(case_fingerprints=first['case_fingerprints'], source=old, authority=authority)
        new = source(5)
        # Change outcome0 and append a new review for case1. Cases2/3 stay exact.
        new['report']['outcomes'][0]['positions']['raw'] = 'newly changed'
        new['review_attempts'].append(dict(request=dict(case_id='case-1', stage='new-review'), response={'new':True}))
        new['cases'].reverse()
        second = capacity.plan(new, {}, declaration(), 30, packet, previous=previous)
        self.assertEqual({u['unit_id'] for u in second['reused']}, {'case:case-2','case:case-3'})
        sent = {u['unit_id'] for p in second['parts'] for u in p['packet']['input']['units']}
        self.assertTrue({'case:case-0','case:case-1','case:case-4'} <= sent)
        self.assertEqual(capacity.restore(second, old), new)
        tampered = deepcopy(old)
        tampered['cases'][2]['raw'] = 'changed old evidence'
        with self.assertRaisesRegex(capacity.CapacityError, 'certificate'):
            capacity.restore(second, tampered)

    def test_complete_plan_fails_before_dispatch_when_call_bound_or_single_case_cannot_fit(self):
        with self.assertRaisesRegex(capacity.CapacityError, 'call bound'):
            capacity.plan(source(), {}, declaration(), 1, packet)
        original = source(1)
        original['cases'][0]['raw'] *= 20
        with self.assertRaisesRegex(capacity.CapacityError, 'one complete'):
            capacity.plan(original, {}, declaration(), 100, packet)

    def test_large_report_maps_split_as_complete_values_without_discarding_rows(self):
        original = source(1)
        original['report']['map'] = {f'provider-{i}':dict(values='observed-' * 500) for i in range(12)}
        plan = capacity.plan(original, {}, declaration(), 100, packet)
        self.assertEqual(capacity.restore(plan), original)
        self.assertGreater(len(plan['parts']), 2)

    def test_exact_coverage_is_required_and_one_negative_part_stops(self):
        plan = capacity.plan(source(), {}, declaration(), 30, packet)
        responses = [verdict(p) for p in plan['parts']]
        self.assertEqual(capacity.combine(plan, responses)['decision'], 'continue')
        self.assertEqual(capacity.combine(plan, responses[:-1])['decision'], 'stop')
        responses[0]['reviewed_unit_ids'] = []
        with self.assertRaisesRegex(capacity.CapacityError, 'exact evidence units'):
            capacity.combine(plan, responses)
        responses[0] = verdict(plan['parts'][0])
        responses[0].update(decision='stop', invariant_failures=['Synthetic negative review.'])
        result = capacity.combine(plan, responses[:1])
        self.assertEqual(result['decision'], 'stop')
        self.assertIn('Synthetic negative review.', result['invariant_failures'])

    def test_conservative_measurement_includes_unicode_prompt_schema_and_output_echo(self):
        p = packet({'units':[], 'unit_ids':['x'*200], 'required_inspection':[]})
        first = capacity.measure(p, declaration())
        p['prompt'] += 'אבג' * 100
        second = capacity.measure(p, declaration())
        self.assertEqual(second['serialized_utf8_bytes'] - first['serialized_utf8_bytes'], 600)
        self.assertIsNone(second['token_count'])
        self.assertFalse(capacity.measure(p, declaration(output=100))['fits'])

    def test_capacity_declaration_binds_mode_model_and_explicit_sources(self):
        reviewers = {r:dict(model='synthetic') for r in ('codex', 'claude')}
        valid = dict(profile=capacity.rules()['profile'], kind='fixture', max_checkpoint_calls=20,
                     reviewers={r:declaration() for r in reviewers})
        self.assertEqual(capacity.validate(valid, reviewers, 'fixture'), valid)
        for bad in (None, dict(valid, kind='declared-live'), dict(valid, max_checkpoint_calls=0)):
            with self.assertRaises(capacity.CapacityError):
                capacity.validate(bad, reviewers, 'fixture')
        with self.assertRaises(capacity.CapacityError):
            capacity.validate(valid, reviewers, 'live')
        valid['reviewers']['claude']['model'] = 'different'
        with self.assertRaises(capacity.CapacityError):
            capacity.validate(valid, reviewers, 'fixture')

    def test_live_capacity_refusal_precedes_authentication_and_durable_send(self):
        from openweb_news import fixtures, question_workflow
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            cfg = fixtures.config(root / 'unused')
            p = dict(mode='live', configuration=cfg,
                review_capacity=dict(reviewers={role:declaration() for role in cfg['reviewers']}))
            oversized = packet(dict(text='Synthetic large input.' * 10000))
            with patch('openweb_news.review_pipeline.backends.subscription') as transport:
                with self.assertRaisesRegex(question_workflow.Blocked, 'capacity budget exceeded'):
                    question_workflow._agent(p, root, 'large-live-review', 'claude', oversized, None)
                transport.assert_not_called()
            self.assertFalse((root / 'agents/large-live-review/attempts.jsonl').exists())
            self.assertTrue((root / 'agents/large-live-review/blocked-packet.json').exists())


if __name__ == '__main__':
    unittest.main()
