from copy import deepcopy
import json
import unittest

from openweb_news.comparison import compare_reports
from openweb_news._method.scoring import score


def report(qids=('q1', 'q2'), configs=('a', 'b'), *, changes=None, groups=None):
    questions = [dict(question_id=q, revision=1, family_id=(groups or {}).get(q, (q, q))[0],
                      need_id=(groups or {}).get(q, (q, q))[1], provider_query='Synthetic question ' + q,
                      contract=dict(requested_facets=[])) for q in qids]
    cells, collection, outcomes = {}, [], []
    for cid in configs:
        for qid in qids:
            spec = (changes or {}).get((cid, qid), {})
            grades = spec.get('grades', [2])
            status = spec.get('status', 'success' if grades else 'success_empty')
            ranked = []
            for rank, grade in enumerate(grades, 1):
                url = spec.get('urls', ['https://example.invalid/story'] * len(grades))[rank - 1]
                case = cid + '-' + qid + '-' + str(rank)
                outcomes.append(dict(configuration_id=cid, question_id=qid, case_id=case, rank=rank,
                                     grade=grade, returned_url=url, snippet='Synthetic snippet'))
                ranked.append(dict(rank=rank, evidence_id=case, url=url))
            cells[cid, qid] = dict(status=status, ranked=ranked)
            collection.append(dict(configuration_id=cid, question_id=qid, status=status))
    scores = score(questions, list(configs), cells, {o['case_id']: o['grade'] for o in outcomes},
                   {o['case_id']: 'synthetic' for o in outcomes}, .9, list(configs))
    return dict(questions=questions, collection=collection, outcomes=outcomes, scores=scores)


def table(comparison, metric='answer@3', view='delivered_utility'):
    return {row['configuration_id']: row for row in comparison['rankings'][view][metric]['table']}


class ComparisonTests(unittest.TestCase):
    def test_rank_flags_use_common_family_counts_and_keep_skipped_metrics(self):
        configs=('a','b','c','d');qids=('q1','q2','q3')
        old=report(qids,configs,changes={(cid,q):dict(grades=[2 if cid=='a'else 1])for cid in configs for q in qids})
        new=report(qids,configs,changes={(cid,q):dict(grades=[0 if cid=='a'else 2])for cid in configs for q in qids})
        result=compare_reports(old,new)
        flags=result['rank_review'];self.assertTrue(flags['descriptive_only'])
        self.assertTrue(any(r['configuration_id']=='a'and r['rank_delta']==3 for row in flags['rows']for r in row['movements']))
        small=compare_reports(report(('q1',),configs),report(('q1',),configs))['rank_review']
        self.assertTrue(all(r['skipped']and not r['movements']for r in small['rows']))

    def test_provider_review_is_derived_and_missing_metadata_stays_unknown(self):
        old=report(configs=('a','b'));new=report(configs=('a','b'))
        old['active_review_profile']=dict(profile_bindings=dict(provider_configurations={'a':'old','b':'same'}))
        new['active_review_profile']=dict(profile_bindings=dict(provider_configurations={'a':'new','b':'same'},
            date_trust=dict(selections={'a':dict(eligible_fields=['publication_date'])})),
            method_options=dict(provider_request_profiles={'b':'synthetic-request-profile'}))
        result=compare_reports(old,new)['provider_review']
        self.assertEqual(result['changed_providers'],['a'])
        self.assertEqual(result['selected_choice_review_providers'],['a','b'])
        self.assertEqual(result['unavailable_configuration_comparisons'],[])
        self.assertEqual(result['rows'][0]['selected_native_publication_fields'],['publication_date'])
        old.pop('active_review_profile')
        missing=compare_reports(old,new)['provider_review']
        self.assertEqual(missing['changed_providers'],[])
        self.assertEqual(missing['unavailable_configuration_comparisons'],['a','b'])

    def test_degradation_means_preserve_unknown_and_empty_coverage(self):
        old=report(changes={('a','q1'):dict(grades=[0,2,None]),('a','q2'):dict(grades=[])})
        new=report(changes={('a','q1'):dict(grades=[2,None,None]),('a','q2'):dict(grades=[])})
        snapshot=deepcopy((old,new));row=compare_reports(old,new)['providers']['a']
        self.assertEqual((old,new),snapshot)
        self.assertEqual(row['archived']['grade_counts'],{'0':1,'1':0,'2':1,'unknown':1})
        self.assertEqual(row['current']['grade_counts'],{'0':0,'1':0,'2':1,'unknown':2})
        self.assertEqual(row['archived']['mean_known_grade'],1)
        self.assertEqual(row['current']['mean_known_grade'],2)
        first,empty=row['question_grade_changes']
        self.assertEqual(first['current'],dict(mean_known_grade=2,known=1,unknown=2,returned=3))
        self.assertEqual(empty['current'],dict(mean_known_grade=None,known=0,unknown=0,returned=0))

    def test_explicit_tavily_mode_pairing_preserves_reports_and_discloses_change(self):
        old = report(configs=('tavily-news', 'b'), changes={('tavily-news', 'q1'): dict(grades=[0])})
        new = report(configs=('tavily-general', 'b'))
        snapshots = deepcopy((old, new))
        with self.assertRaises(ValueError):
            compare_reports(old, new)
        result = compare_reports(old, new, replacements={'tavily-general': 'tavily-news'})
        self.assertEqual((old, new), snapshots)
        self.assertEqual(result['configuration_replacements'], {'tavily-general': 'tavily-news'})
        own = result['providers']['tavily-general']
        self.assertEqual(own['archived_configuration_id'], 'tavily-news')
        self.assertEqual(own['current_configuration_id'], 'tavily-general')
        self.assertAlmostEqual(table(result)['tavily-general']['score_delta'], 1 / 6)
        self.assertEqual(result['rankings']['delivered_utility']['ndcg@3']['status'], 'unavailable')
        for replacements in ({'b': 'tavily-news'}, {'tavily-general': 'b'}, {'foreign': 'tavily-news'}):
            with self.subTest(replacements=replacements), self.assertRaises(ValueError):
                compare_reports(old, new, replacements=replacements)
        with self.assertRaises(ValueError):
            compare_reports(old, report(configs=('tavily-general', 'tavily-news')),
                replacements={'tavily-general': 'tavily-news'})

    def test_subset_uses_only_current_question_rows_not_archived_aggregate(self):
        old = report(changes={('a', 'q1'): dict(grades=[0]), ('a', 'q2'): dict(grades=[2])})
        new = report(qids=('q1',), configs=('a',))
        old['scores']['aggregates'] = {'poison': 'never compare full archived aggregates'}
        before = deepcopy(old)
        result = compare_reports(old, new)
        own = table(result)['a']
        self.assertEqual(result['scope']['cells'], 1)
        self.assertEqual(result['providers']['a']['archived']['returned_outcomes'], 1)
        self.assertEqual(own['archived_score'], 0)
        self.assertEqual(own['current_score'], 1 / 3)
        self.assertEqual(own['score_delta'], 1 / 3)
        self.assertEqual(result['rankings']['delivered_utility']['ndcg@3']['status'], 'unavailable')
        self.assertEqual(old, before)

    def test_current_only_questions_are_disclosed_not_compared(self):
        old=report(qids=('q1','q2'))
        new=report(qids=('q1','q2','q3'),changes={('a','q3'):dict(grades=[0])})
        snapshot=deepcopy((old,new));result=compare_reports(old,new)
        self.assertEqual(result['scope']['question_ids'],['q1','q2'])
        self.assertEqual(result['scope']['current_only_question_ids'],['q3'])
        self.assertEqual(result['scope']['current_total_questions'],3)
        self.assertEqual(result['providers']['a']['current']['cells'],2)
        self.assertEqual(table(result)['a']['score_delta'],0)
        self.assertEqual((old,new),snapshot)
        new['questions'][0]['provider_query']='Changed overlap'
        with self.assertRaises(ValueError):compare_reports(old,new)

    def test_overlap_does_not_admit_incomplete_families(self):
        groups={'q1':('shared','n1'),'q3':('shared','n3')}
        result=compare_reports(report(qids=('q1','q2'),groups=groups),report(qids=('q1','q2','q3'),groups=groups))
        metric=result['rankings']['delivered_utility']['answer@3']
        self.assertEqual(metric['included_family_ids'],['q2'])
        self.assertIn('shared',metric['excluded_family_ids'])

    def test_full_question_equality_includes_noncontract_fields_and_order_is_irrelevant(self):
        old = report()
        for field, value in [('revision', 2), ('contract', {'requested_facets': [], 'extra': True}),
                             ('provider_query', 'Changed question')]:
            new = report(qids=('q1',))
            new['questions'][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                compare_reports(old, new)
        new = report(qids=('q2', 'q1'))
        self.assertEqual(compare_reports(old, new)['scope']['question_ids'], ['q1', 'q2'])
        with self.assertRaises(ValueError):
            compare_reports(old, report(qids=('foreign',)))
        with self.assertRaises(ValueError):
            compare_reports(old, report(configs=('foreign',)))

    def test_missing_duplicate_and_foreign_cells_and_outcomes_rejected(self):
        old = report()
        mutations = [
            lambda r: r['collection'].pop(),
            lambda r: r['collection'].append(deepcopy(r['collection'][0])),
            lambda r: r['collection'][0].update(question_id='foreign'),
            lambda r: r['outcomes'].pop(),
            lambda r: r['outcomes'].append(deepcopy(r['outcomes'][0])),
            lambda r: r['outcomes'][0].update(configuration_id='foreign'),
            lambda r: r['scores']['question_rows'].pop(),
            lambda r: r['scores']['question_rows'].append(deepcopy(r['scores']['question_rows'][0])),
        ]
        for index, mutate in enumerate(mutations):
            new = report()
            mutate(new)
            with self.subTest(index=index), self.assertRaises(ValueError):
                compare_reports(old, new)
        # A missing final rank must fail even when remaining ranks have no gap.
        old = report(changes={('a', 'q1'): dict(grades=[2, 1])})
        new = deepcopy(old)
        new['outcomes'] = [o for o in new['outcomes'] if not (o['configuration_id'] == 'a' and o['question_id'] == 'q1' and o['rank'] == 2)]
        with self.assertRaises(ValueError):
            compare_reports(old, new)

    def test_invalid_ranks_grades_questions_and_statuses_rejected(self):
        old = report()
        for rank in (0, 11, True, 1.5, '1'):
            new = report()
            new['outcomes'][0]['rank'] = rank
            with self.subTest(rank=rank), self.assertRaises(ValueError):
                compare_reports(old, new)
        for grade in (-1, 3, True, '2'):
            new = report()
            new['outcomes'][0]['grade'] = grade
            with self.subTest(grade=grade), self.assertRaises(ValueError):
                compare_reports(old, new)
        new = report()
        new['questions'].append(deepcopy(new['questions'][0]))
        with self.assertRaises(ValueError):
            compare_reports(old, new)
        new = report()
        new['collection'][0]['status'] = 'success_empty'
        with self.assertRaises(ValueError):
            compare_reports(old, new)

    def test_repeated_urls_are_counted_by_rank_without_evidence_equivalence_gate(self):
        old = report(qids=('q1',), configs=('a',), changes={('a', 'q1'): dict(grades=[2, 1, None])})
        new = report(qids=('q1',), configs=('a',), changes={('a', 'q1'): dict(grades=[0, None, 2])})
        new['outcomes'][0].update(snippet='Changed snippet', native_date='2026-02-03')
        new['outcomes'][1]['returned_url'] = 'https://example.invalid/different'
        own = compare_reports(old, new)['providers']['a']
        self.assertEqual(own['matched_rank_positions'], 3)
        self.assertEqual(own['same_cell_rank_grade_transitions'], {'2->0': 1, '1->unknown': 1, 'unknown->2': 1})
        self.assertEqual(own['current']['unknown_outcomes'], 1)
        self.assertEqual(own['current']['unknown_returned_share'], 1 / 3)
        self.assertEqual(own['current']['unknown_scheduled_slot_share'], .1)
        self.assertEqual(table(compare_reports(old, new))['a']['score_delta'], -1 / 3)
        summary = json.dumps(compare_reports(old, new))
        self.assertNotIn('Changed snippet', summary)
        self.assertNotIn('https://', summary)
        self.assertNotIn('2026-02-03', summary)

    def test_legacy_packet_hashes_cannot_infer_unchanged_evidence(self):
        old = report(qids=('q1',), configs=('a',))
        new = deepcopy(old)
        for r in (old, new):
            r['outcomes'][0].pop('returned_url')
            r['outcomes'][0].pop('snippet')
            r['outcomes'][0]['packet_sha256'] = 'same-broad-packet-hash'
        result = compare_reports(old, new)
        self.assertEqual(table(result)['a']['score_delta'], 0)
        self.assertNotIn('same_projection', json.dumps(result))
        self.assertTrue(any('equivalence' in limit for limit in result['limits']))

    def test_coverage_empty_failed_and_changed_number_of_results(self):
        old = report(changes={('a', 'q2'): dict(grades=[], status='http_failure')})
        new = report(changes={('a', 'q1'): dict(grades=[]), ('a', 'q2'): dict(grades=[None, 1])})
        result = compare_reports(old, new)
        own = result['providers']['a']
        self.assertEqual(own['archived']['coverage'], .5)
        self.assertEqual(own['archived']['failure_or_uncollected_statuses'], {'http_failure': 1})
        self.assertEqual(own['current']['coverage'], 1)
        self.assertEqual(own['current']['empty_cells'], 1)
        self.assertEqual(own['removed_rank_positions'], 1)
        self.assertEqual(own['new_rank_positions'], 2)
        self.assertEqual(own['same_cell_rank_grade_transitions'], {})
        self.assertEqual(result['scope']['matched_answered_question_ids'], ['q1'])
        self.assertEqual(result['rankings']['delivered_utility']['answer@3']['included_questions'], 1)
        self.assertEqual(table(result)['a']['score_delta'], -1 / 3)

    def test_existing_need_and_family_weighting_changes_ranks(self):
        qids = ('q1', 'q2', 'q3', 'q4')
        groups = dict(q1=('F', 'N1'), q2=('F', 'N1'), q3=('F', 'N2'), q4=('G', 'N3'))
        changes = {('a', 'q3'): dict(grades=[0]), ('a', 'q4'): dict(grades=[0]),
                   ('b', 'q1'): dict(grades=[0]), ('b', 'q2'): dict(grades=[0]), ('b', 'q3'): dict(grades=[0])}
        old = report(qids=qids, groups=groups, changes=changes)
        changes = dict(changes)
        changes['a', 'q4'] = dict(grades=[2])
        new = report(qids=qids, groups=groups, changes=changes)
        result = compare_reports(old, new)
        own = table(result)
        self.assertAlmostEqual(own['a']['archived_score'], 1 / 12)
        self.assertAlmostEqual(own['a']['current_score'], 1 / 4)
        self.assertAlmostEqual(own['b']['archived_score'], 1 / 6)
        self.assertEqual((own['a']['archived_rank'], own['a']['current_rank'], own['a']['rank_delta']), (2, 1, -1))
        self.assertEqual((own['b']['archived_rank'], own['b']['current_rank']), (1, 2))
        self.assertEqual(result['rankings']['delivered_utility']['answer@3']['status'], 'determined')

    def test_unknown_families_use_identical_denominators_with_explicit_partial_ranks(self):
        old = report()
        new = report(changes={('a', 'q2'): dict(grades=[None])})
        result = compare_reports(old, new)
        metric = result['rankings']['delivered_utility']['answer@3']
        self.assertEqual(metric['status'], 'pending')
        self.assertEqual(metric['included_family_ids'], ['q1'])
        self.assertEqual(metric['excluded_family_ids'], ['q2'])
        for row in table(result).values():
            self.assertEqual(row['archived_rank'], 1)
            self.assertEqual(row['current_rank'], 1)
            self.assertEqual(row['current_tie_size'], 2)
            self.assertIsNone(row['whole_scope_current_rank'])
        # No determined common family means no rank at all.
        none = compare_reports(report(qids=('q1',)), report(qids=('q1',), changes={('a', 'q1'): dict(grades=[None])}))
        self.assertIsNone(table(none)['b']['current_rank'])

    def test_competition_ties_and_released_vs_matched_failure_views(self):
        tied = report(configs=('a', 'b', 'c'), changes={('c', 'q1'): dict(grades=[0]), ('c', 'q2'): dict(grades=[0])})
        rows = table(compare_reports(tied, tied))
        self.assertEqual([rows[c]['current_rank'] for c in ('a', 'b', 'c')], [1, 1, 3])
        old = report(changes={('a', 'q2'): dict(grades=[], status='http_failure')})
        new = report()
        result = compare_reports(old, new)
        released = table(result)['a']
        matched = table(result, view='matched_answered')['a']
        self.assertEqual(released['archived_score'], 1 / 6)
        self.assertEqual(released['current_score'], 1 / 3)
        self.assertEqual(matched['archived_score'], 1 / 3)
        self.assertEqual(matched['current_score'], 1 / 3)
        self.assertEqual(result['rankings']['matched_answered']['answer@3']['excluded_family_ids'], ['q2'])
        self.assertIsNone(matched['whole_scope_current_rank'])

    def test_ndcg_same_pool_subset_questions_json_roundtrip_and_compact_rejection(self):
        old = report()
        new = report(qids=('q1',))
        own = table(compare_reports(old, new), metric='ndcg@3')['a']
        self.assertEqual(own['score_delta'], 0)
        self.assertEqual(own['archived_rank'], 1)
        self.assertEqual(compare_reports(json.loads(json.dumps(old)), json.loads(json.dumps(new)))['scope']['cells'], 2)
        with self.assertRaises(ValueError):
            compare_reports(old, dict(questions=['q1'], final_unknowns=0))


if __name__ == '__main__':
    unittest.main()
