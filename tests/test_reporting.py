from copy import deepcopy
import unittest

from openweb_news.reporting import compact_outcome
from openweb_news.review import settle


class ReportingTests(unittest.TestCase):
    def test_compaction_preserves_decoded_evidence_votes_and_different_conformance(self):
        judgment = dict(candidates=[dict(claim='Synthetic own evidence')])
        position = dict(state='valid', grade=2, policy_conformance='same_as_structural_base',
                        judgment=judgment, conformance_judgment=deepcopy(judgment), raw_verdict=dict(native='retained in attempt'),
                        independent=True, prior_verdict_exposure=False, raw_sha256='source-hash')
        outcome = settle(position, position)
        compact_outcome(outcome)
        self.assertEqual(outcome['grade'], 2)
        self.assertEqual(outcome['selected_roles'], ['pro','codex'])
        for p in outcome['positions'].values():
            self.assertEqual(p['judgment'], judgment)
            self.assertEqual(p['raw_sha256'], 'source-hash')
            self.assertNotIn('raw_verdict', p)
            self.assertNotIn('conformance_judgment', p)
        different = deepcopy(position)
        different['conformance_judgment'] = dict(candidates=[dict(claim='Different evidence')])
        value = dict(positions=dict(pro=different))
        compact_outcome(value)
        self.assertEqual(value['positions']['pro']['conformance_judgment'], different['conformance_judgment'])
        self.assertIn('raw_verdict', position)
