from copy import deepcopy
import unittest

from openweb_news import judging
from openweb_news._method import providers
from openweb_news.common import asset_path
from openweb_news.schema import validate
from .helpers import native_response, question, verdict


class CombinedContractTests(unittest.TestCase):
    def test_fact_citations_exclude_metadata_without_removing_date_witnesses(self):
        config = next(c for c in providers.configuration_catalog() if c['configuration_id'] == 'exa-highlights')
        q = question()
        q['contract']['temporal_requirements'] = [dict(id='publication-window', basis='publication',
            start_inclusive='2026-10-01T00:00:00Z', end_exclusive='2026-11-01T00:00:00Z')]
        native = native_response(config)
        native['results'][0]['publishedDate'] = '2026-10-15'
        position = providers.project_response(config, native, '2026-11-01T00:00:00Z')['positions'][0]
        compiled = judging.compile_packet(judging.packet_for(q, config, position, 'synthetic-date'),
                                          '2026-11-01T00:00:00Z', config=config)
        props = compiled['schema']['properties']
        fact_fields = props['candidates']['items']['properties']['support']['items']['properties']['field']['enum']
        self.assertEqual(fact_fields, ['title', 'snippet[0]', 'url'])
        self.assertIn('N1', props['dates']['items']['properties']['date_ids']['items']['enum'])
        self.assertIn('date:0', props['record_conditions']['items']['properties']['support']['items']['properties']['field']['enum'])
        value = verdict(compiled)
        value['dates'] = [dict(candidate_id='finding-1', predicate_id='publication:publication-window',
            link_status='established', role='first_publication', date_ids=['N1'], anchor_date_id='',
            additional_dates=[], context_support=[], reason='The mapped own first-publication estimate dates this article.')]
        decoded = judging.decode_position(value, compiled)
        self.assertEqual(decoded['state'], 'valid')
        self.assertEqual(decoded['grade'], 2)
        bad = deepcopy(value)
        metadata = next(r for r in compiled['packet']['native_metadata'] if r['field'] == 'date:0')
        bad['candidates'][0]['support'] = [dict(field='date:0', quote=metadata['text'], occurrence=0)]
        self.assertTrue(validate(bad, compiled['schema']))
        self.assertEqual(judging.decode_position(bad, compiled)['state'], 'invalid')

    def test_missing_evidence_lead_survives_and_flags_are_not_silently_rewritten(self):
        config = providers.configuration_catalog()[0]
        position = providers.project_response(config, native_response(config), '2026-11-01T00:00:00Z')['positions'][0]
        compiled = judging.compile_packet(judging.packet_for(question(), config, position, 'synthetic-lead'),
                                          '2026-11-01T00:00:00Z')
        lead = judging.decode_position(verdict(compiled, 1), compiled)
        self.assertEqual(lead['state'], 'valid')
        self.assertEqual(lead['structural_base_grade'], 1)
        self.assertEqual(lead['policy_conformance'], 'same_as_structural_base')
        inconsistent = verdict(compiled, 1)
        inconsistent['overall_grade'] = 0
        decoded = judging.decode_position(inconsistent, compiled)
        self.assertEqual(decoded['proposal_grade'], 0)
        self.assertEqual(decoded['structural_base_grade'], 1)
        self.assertEqual(decoded['policy_conformance'], 'requires_own_source_review')
        self.assertTrue(decoded['raw_verdict']['candidates'][0]['clear_lead'])
        paragraph = asset_path('lead-flag-clarification-v1.md').read_text().strip()
        self.assertTrue(compiled['prompt'].endswith('\n\n' + paragraph + '\n'))
        self.assertEqual(compiled['prompt'].count(paragraph), 1)
        wire = judging.build_pro_request(compiled)
        self.assertEqual(wire['body']['systemInstruction']['parts'][0]['text'],
                         judging.blind_packet(compiled)['prompt'])


if __name__ == '__main__':
    unittest.main()
