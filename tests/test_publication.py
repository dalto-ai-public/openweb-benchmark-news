from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import json
import unittest

from openweb_news import publication, settlement, judging, fixtures
from openweb_news import checker_replay
from openweb_news.common import digest, load, write_private
from openweb_news.recovery import sibling_rule
from openweb_news.review import settle
from openweb_news.reporting import compact_outcome
from . import test_execution
from . import helpers
from openweb_news._method import providers, two_step, evidence
from . import test_subscription_health
from types import SimpleNamespace


class PublicationTests(unittest.TestCase):
    def test_space_fallback_repairs_only_invalid_scoped_review_and_keeps_provenance(self):
        from openweb_news import representation
        packet=deepcopy(self.compiled['packet']);before=packet['allowed_text'][0]['text']
        after=before.replace(' ', '\u00a0',1);packet['allowed_text'][0]['text']=after
        compiled=judging.compile_packet(packet,self.compiled['input']['captured_at'],config=self.provider)
        blind=publication.packet_for(compiled);plain=self.output(2);exact=deepcopy(plain)
        def fix(value):
            if isinstance(value,dict):
                if value.get('field')=='title'and value.get('quote')==before:value['quote']=after
                for child in value.values():fix(child)
            elif isinstance(value,list):
                for child in value:fix(child)
        fix(exact)
        responses={r:dict(verdict=value,transport_success=True,native_completion_verified=True)for r,value in [('codex',exact),('claude',plain)]}
        old=publication.corroborate(compiled,blind,responses)
        self.assertTrue(old['audits']['codex']['valid']);self.assertFalse(old['audits']['claude']['valid'])
        new=publication.corroborate(compiled,blind,responses,text_profile=representation.SPACE_PROFILE)
        self.assertTrue(new['accepted']);self.assertEqual(new['grade'],2)
        self.assertEqual(new['audits']['codex'],old['audits']['codex'])
        proof=new['audits']['claude']['policy_applications'][0]['support'][0]
        self.assertEqual(proof['mode'],representation.SPACE_PROFILE+'_to_raw_span')
        self.assertEqual(proof['quote'],after[proof['start']:proof['end']])
        bad=deepcopy(responses);bad['claude']['verdict']['policy_applications'][0]['checks'][0]['support'][0]['quote']='unsupported wording'
        self.assertFalse(publication.corroborate(compiled,blind,bad,text_profile=representation.SPACE_PROFILE)['accepted'])

    def test_broader_review_eligibility_and_no_native_mapping_are_provider_neutral(self):
        provider=next(p for p in providers.configuration_catalog(include_optional=True)if p['configuration_id']=='brave-web')
        compiled=judging.compile_packet(self.compiled['packet'],self.compiled['input']['captured_at'],config=provider)
        positions={'pro':dict(state='invalid'),'codex':dict(state='unjudged')}
        self.assertIsNone(publication.eligibility(compiled,positions,provider))
        route=publication.eligibility(compiled,positions,provider,selection_profile=publication.SELECTION_PROFILE)
        self.assertEqual(route['invalid_roles'],['codex','pro']);self.assertTrue(route['source_only'])
        packet=publication.packet_for(compiled)
        self.assertEqual(packet['input']['eligible_native_date_ids'],[])
        self.assertIsNone(packet['input']['publication_estimate_applicability'])
        self.assertNotIn('positions',packet['input'])
        self.assertFalse(publication.validate_review(self.output(2),compiled,packet)['valid'])
        value=self.output(2);value['dates'][0].update(date_ids=['D1'],role='first_publication')
        value['policy_applications'][0]['policy']=publication.TEXT_POLICY
        self.assertTrue(publication.validate_review(value,compiled,packet)['valid'])
        responses={r:dict(verdict=deepcopy(value),transport_success=True,native_completion_verified=True)for r in ('codex','claude')}
        self.assertTrue(publication.corroborate(compiled,packet,responses)['accepted'])
        responses['claude']['verdict']=self.output(0)
        self.assertFalse(publication.corroborate(compiled,packet,responses)['accepted'])
        responses['claude']['verdict']=deepcopy(value);responses['claude']['native_completion_verified']=False
        self.assertFalse(publication.corroborate(compiled,packet,responses)['accepted'])
        known={r:dict(state='valid',grade=0,policy_conformance='same_as_structural_base')for r in ('pro','codex')}
        known['claude']=dict(state='invalid')
        self.assertIsNone(publication.eligibility(compiled,known,provider,selection_profile=publication.SELECTION_PROFILE))

    def setUp(self):
        fixture = test_execution.PolicyTests(); fixture.setUp()
        self.provider, self.raw = fixture.config, fixture.raw
        packet = deepcopy(fixture.compiled['packet'])
        packet['question'].update(family_id='synthetic-family', need_id='synthetic-need')
        self.compiled = judging.compile_packet(packet, fixture.compiled['input']['captured_at'], config=self.provider)
        self.packet = publication.packet_for(self.compiled)

    def output(self, grade=1):
        source = self.compiled['packet']
        quote = dict(field=source['allowed_text'][0]['field'], quote=source['allowed_text'][0]['text'], occurrence=1)
        value = deepcopy(self.raw)
        value.update(overall_grade=grade, reason='Complete synthetic own-source disposition.',
            contrary_reading='A container or conflicting date would prevent this publication inference.',
            complete_occurrence_reviewed=True, policy_applications=[])
        if grade is None:
            value['dates'][0]['link_status'] = 'unknown'
        if grade == 0:
            value['candidates'][0]['conditions'][0].update(status='fail')
        if grade == 2:
            value['dates'][0].update(link_status='established', context_support=[deepcopy(quote)])
            value['policy_applications'] = [dict(candidate_ids=[value['candidates'][0]['candidate_id']],
                predicate_id='publication:window', policy=publication.NATIVE_POLICY, status='pass',
                checks=[dict(check=k, status='pass', reason='Synthetic applicable fixed item, no contrary own date.',
                             support=[deepcopy(quote)]) for k in publication.CHECKS])]
        return value

    def test_exact_dispositions_policy_context_and_immutable_native_meaning(self):
        before = deepcopy(self.compiled)
        for grade in (0, 1, 2, None):
            with self.subTest(grade=grade):
                result = publication.validate_review(self.output(grade), self.compiled, self.packet)
                self.assertTrue(result['valid'], result)
                self.assertEqual(result['grade'], grade)
        text = self.output(2)
        text['policy_applications'][0]['policy'] = publication.TEXT_POLICY
        text['dates'][0]['date_ids'] = ['D1']
        self.assertTrue(publication.validate_review(text, self.compiled, self.packet)['valid'])
        self.assertEqual(self.compiled, before)

    def test_mixed_and_single_selection_and_no_proposer_date_leak(self):
        high = judging.decode_position(self.raw, self.compiled)
        lead = deepcopy(high); lead.update(proposal_grade=1, grade=1, policy_conformance='same_as_structural_base')
        for positions, pattern in [({'pro': high, 'codex': high}, 'both-primary'),
                                   ({'pro': high, 'codex': lead, 'claude': high}, 'mixed'),
                                   ({'pro': high, 'codex': lead}, 'single')]:
            before = publication.packet_for(self.compiled)
            self.assertEqual(publication.eligibility(self.compiled, positions, self.provider)['pattern'], pattern)
            self.assertEqual(publication.packet_for(self.compiled), before)
        self.assertIsNone(publication.eligibility(self.compiled, {'pro': lead, 'codex': lead}, self.provider))
        self.assertEqual(self.packet['input']['eligible_native_date_ids'], ['N1'])
        self.assertNotIn('configuration_sha256', self.packet['input']['publication_estimate_applicability'])

    def test_negative_proofs_and_schema_cannot_force_numeric_grade(self):
        for kind in ('bare', 'field', 'policy', 'date', 'check', 'span', 'predicate', 'scope', 'false_grade', 'incomplete'):
            with self.subTest(kind=kind):
                value = self.output(2)
                app = value['policy_applications'][0]
                if kind == 'bare': value['policy_applications'] = []
                if kind == 'field': value['dates'][0]['date_ids'] = ['N999']
                if kind == 'policy': app['policy'] = 'invented-policy'
                if kind == 'date': app['status'] = 'fail'
                if kind == 'check': app['checks'][0]['status'] = 'unknown'
                if kind == 'span': app['checks'][0]['support'][0]['quote'] = 'Not in this evidence'
                if kind == 'predicate': value['candidates'][0]['conditions'].pop()
                if kind == 'scope': app['candidate_ids'] = ['not-reviewed']
                if kind == 'false_grade': value['overall_grade'] = 1
                if kind == 'incomplete': value['complete_occurrence_reviewed'] = False
                self.assertFalse(publication.validate_review(value, self.compiled, self.packet)['valid'])
        for bad in (None, [], 42, 'bad', {'grade': 2}):
            self.assertFalse(publication.validate_review(bad, self.compiled, self.packet)['valid'])

    def test_candidate_and_record_precedence_disagreement_and_terminal_null(self):
        value = self.output(2)
        failed = deepcopy(self.output(0)['candidates'][0]); failed['candidate_id'] = 'failed'
        value['candidates'].append(failed)
        link = deepcopy(self.output(0)['dates'][0]); link['candidate_id'] = 'failed'; value['dates'].append(link)
        self.assertTrue(publication.validate_review(value, self.compiled, self.packet)['valid'])
        value['overall_grade'] = 0
        self.assertFalse(publication.validate_review(value, self.compiled, self.packet)['valid'])
        value = self.output(1)
        unknown = deepcopy(self.output(None)['candidates'][0]); unknown['candidate_id'] = 'unknown'
        value['candidates'].append(unknown)
        link = deepcopy(self.output(None)['dates'][0]); link['candidate_id'] = 'unknown'; value['dates'].append(link)
        self.assertFalse(publication.validate_review(value, self.compiled, self.packet)['valid'])
        value['overall_grade'] = None
        self.assertTrue(publication.validate_review(value, self.compiled, self.packet)['valid'])
        def response(g):
            return dict(verdict=self.output(g), transport_success=True, native_completion_verified=True)
        both = publication.corroborate(self.compiled, self.packet, dict(codex=response(1), claude=response(2)))
        self.assertFalse(both['accepted']); self.assertIsNone(both['grade']); self.assertTrue(both['terminal'])
        both = publication.corroborate(self.compiled, self.packet, dict(codex=response(None), claude=response(None)))
        self.assertFalse(both['accepted']); self.assertTrue(both['terminal'])

    def recompile(self, change=None, text=None, extra_dates=None):
        question = deepcopy(self.compiled['packet']['question'])
        if change:
            change(question)
        text = text or self.compiled['packet']['allowed_text'][0]['text']
        response = helpers.native_response(self.provider, text=text)
        response['results'][0]['publication_date'] = '2026-10-12'
        response['results'][0].update(extra_dates or {})
        projected = providers.project_response(self.provider, response, '2026-10-28T00:00:00Z')
        packet = judging.packet_for(question, self.provider, projected['positions'][0], 'policy-case')
        self.compiled = judging.compile_packet(packet, '2026-10-28T00:00:00Z', config=self.provider)
        self.packet = publication.packet_for(self.compiled)
        self.raw = helpers.verdict(self.compiled)
        for spec in packet['required_conditions']:
            if two_step.date_predicate(spec):
                role = 'first_publication' if spec['predicate_id'].startswith('publication:') else 'fact_availability' if spec['predicate_id']=='as_of_knowledge' else 'event'
                self.raw['dates'].append(dict(candidate_id='finding-1', predicate_id=spec['predicate_id'],
                    link_status='not_demonstrated', role=role, date_ids=['N1'], anchor_date_id='',
                    additional_dates=[], context_support=[], reason='Explicit synthetic source link.'))

    def test_cutoff_arithmetic_and_scoped_historical_availability(self):
        for cutoff, valid in [('2000-01-01T00:00:00Z',False), ('2026-10-12T00:00:00Z',False),
                              ('2026-10-13T00:00:00Z',True)]:
            with self.subTest(cutoff=cutoff):
                self.recompile(lambda q:q['contract'].update(as_of_requirement=dict(
                    meaning='historical_knowledge', cutoff=cutoff, boundary='exclusive', precision='day',
                    timezone='UTC', question_phrase='Facts available before the cutoff.')))
                value = self.output(2)
                knowledge = next(r for r in value['dates'] if r['predicate_id']=='as_of_knowledge')
                knowledge.update(link_status='established', role='fact_availability', date_ids=['D1'],
                                 context_support=deepcopy(value['candidates'][0]['support']))
                result = publication.validate_review(value,self.compiled,self.packet)
                self.assertEqual(result['valid'],valid,result)
                knowledge['date_ids']=['N1']
                # A raw native publication field cannot become fact availability.
                self.assertFalse(publication.validate_review(value,self.compiled,self.packet)['valid'])
                app=deepcopy(value['policy_applications'][0]);app['predicate_id']='as_of_knowledge'
                value['policy_applications'].append(app)
                result=publication.validate_review(value,self.compiled,self.packet)
                self.assertEqual(result['valid'],valid,result)

    def test_event_window_cannot_be_a_span_only_pass_or_native_publication(self):
        self.recompile(lambda q:q['contract']['temporal_requirements'].append(dict(
            id='event',basis='event',start_inclusive='2000-01-01T00:00:00Z',end_exclusive='2000-02-01T00:00:00Z')))
        value=self.output(2)
        link=next(r for r in value['dates'] if r['predicate_id']=='event:event:window')
        link.update(link_status='established',role='event',date_ids=['D1'],context_support=deepcopy(value['candidates'][0]['support']))
        self.assertFalse(publication.validate_review(value,self.compiled,self.packet)['valid'])
        link['date_ids']=['N1']
        self.assertFalse(publication.validate_review(value,self.compiled,self.packet)['valid'])
        value['dates'].remove(link)
        self.assertFalse(publication.validate_review(value,self.compiled,self.packet)['valid'])

    def test_source_additional_dates_relative_anchor_and_linked_year(self):
        value=self.output(2)
        date=self.compiled['input']['text_dates'][0]
        value['policy_applications'][0]['policy']=publication.TEXT_POLICY
        value['dates'][0].update(date_ids=[],additional_dates=[dict(field=date['field'],quote='**October** 12, 2026',occurrence=1)])
        self.assertTrue(publication.validate_review(value,self.compiled,self.packet)['valid'])
        for text, kind in [
            ('Editorial report. Published yesterday. Reference date October 12, 2026. The fictional Aster observatory opened.','relative'),
            ('2026. Editorial report. Published October 12: the fictional Aster observatory opened.','year')]:
            with self.subTest(kind=kind):
                self.recompile(text=text)
                dates=[r for r in self.compiled['input']['text_dates'] if r['field']=='title']
                if kind=='relative':
                    selected=next(r for r in dates if r['quote'].lower()=='yesterday')
                    anchor=next(r for r in dates if r.get('interval'))
                else:
                    selected=next(r for r in dates if r['quote'].startswith('October') and not r.get('interval'))
                    anchor=next(r for r in dates if r.get('normalized')=='2026')
                value=self.output(2);value['policy_applications'][0]['policy']=publication.TEXT_POLICY
                value['dates'][0].update(date_ids=[selected['date_id']],anchor_date_id=anchor['date_id'])
                result=publication.validate_review(value,self.compiled,self.packet)
                self.assertTrue(result['valid'],result)
                if kind=='year':
                    self.assertTrue(any(r['kind']=='linked_year_composition' for r in result['date_refinements']))

    def test_missing_ineligible_and_out_of_window_dates(self):
        self.recompile(extra_dates={'crawl_date':'2026-10-12'})
        value=self.output(2)
        wrong=next(r['date_id']for r in self.compiled['input']['native_date_ids']if r['relative_path']=='crawl_date')
        value['dates'][0]['date_ids']=[wrong]
        self.assertFalse(publication.validate_review(value,self.compiled,self.packet)['valid'])
        self.recompile(extra_dates={'publication_date':None})
        self.assertFalse(publication.validate_review(self.output(2),self.compiled,self.packet)['valid'])
        self.assertTrue(publication.validate_review(self.output(1),self.compiled,self.packet)['valid'])
        value=self.output(2);value['dates'][0]['date_ids']=['D1'];value['policy_applications'][0]['policy']=publication.TEXT_POLICY
        self.assertTrue(publication.validate_review(value,self.compiled,self.packet)['valid'])
        self.recompile(lambda q:q['contract']['temporal_requirements'][0].update(
            start_inclusive='2000-01-01T00:00:00Z',end_exclusive='2000-02-01T00:00:00Z'))
        self.assertFalse(publication.validate_review(self.output(2),self.compiled,self.packet)['valid'])
        value=self.output(2);value['overall_grade']=0;value['policy_applications'][0]['status']='fail'
        self.assertTrue(publication.validate_review(value,self.compiled,self.packet)['valid'])

    def test_scoped_review_cannot_change_default_source_decoding(self):
        before=deepcopy(self.raw)
        self.assertTrue(publication.validate_review(self.output(2),self.compiled,self.packet)['valid'])
        decoded=judging.decode_position(self.raw,self.compiled)
        self.assertEqual(decoded['structural_base_grade'],1)
        self.assertEqual(decoded['proposal_grade'],2)
        self.assertEqual(decoded['policy_conformance'],'requires_own_source_review')
        self.assertEqual(self.raw,before)

    def test_review_capability_cannot_be_injected_by_model_json(self):
        value = deepcopy(self.raw)
        value['reviewed_publications'] = {'finding-1': ['N1']}
        self.assertEqual(judging.decode_position(value, self.compiled)['state'], 'invalid')
        value = self.output(2)
        value['reviewed_publications'] = {'finding-1': ['N1']}
        self.assertFalse(publication.validate_review(value, self.compiled, self.packet)['valid'])

    def test_opt_in_native_role_bridge_preserves_frozen_default(self):
        value = self.output(2)
        value['dates'][0]['role'] = 'publication_unspecified'
        before = deepcopy((value, self.compiled, self.packet))
        self.assertFalse(publication.validate_review(value, self.compiled, self.packet)['valid'])
        result = publication.validate_review(value, self.compiled, self.packet,
            checker_profile=publication.CHECKER_PROFILE)
        self.assertTrue(result['valid'], result)
        self.assertEqual(result['grade'], 2)
        self.assertEqual(result['role_bridges'][0]['original_role'], 'publication_unspecified')
        self.assertEqual(result['role_bridges'][0]['date_ids'], ['N1'])
        self.assertEqual((value, self.compiled, self.packet), before)
        self.assertFalse(publication.validate_review(value, self.compiled, self.packet)['valid'])

    def test_native_role_bridge_cannot_supply_missing_policy_or_timing_proof(self):
        for kind in ('missing', 'scope', 'field', 'check', 'quote', 'update', 'text', 'window'):
            with self.subTest(kind=kind):
                value = self.output(2); value['dates'][0]['role']='publication_unspecified'
                app = value['policy_applications'][0]
                compiled = deepcopy(self.compiled)
                if kind=='missing': value['policy_applications']=[]
                if kind=='scope': app['candidate_ids']=['another-candidate']
                if kind=='field': value['dates'][0]['date_ids']=['N999']
                if kind=='check': app['checks'][0]['status']='unknown'
                if kind=='quote': app['checks'][0]['support'][0]['quote']='Absent quotation'
                if kind=='update': value['dates'][0]['role']='update'
                if kind=='text':
                    app['policy']=publication.TEXT_POLICY; value['dates'][0]['date_ids']=['D1']
                if kind=='window':
                    self.recompile(lambda q:q['contract']['temporal_requirements'][0].update(
                        start_inclusive='2000-01-01T00:00:00Z', end_exclusive='2000-02-01T00:00:00Z'))
                    compiled=self.compiled
                packet=publication.packet_for(compiled)
                self.assertFalse(publication.validate_review(value,compiled,packet,
                    checker_profile=publication.CHECKER_PROFILE)['valid'])

    def test_native_role_bridge_does_not_apply_to_event_or_cutoff(self):
        self.recompile(lambda q:q['contract'].update(as_of_requirement=dict(
            meaning='historical_knowledge',cutoff='2026-10-13T00:00:00Z',boundary='exclusive',
            precision='day',timezone='UTC',question_phrase='Facts available before the cutoff.')))
        value=self.output(2)
        for link in value['dates']:
            link.update(role='publication_unspecified',link_status='established',
                context_support=deepcopy(value['candidates'][0]['support']))
        app=deepcopy(value['policy_applications'][0]);app['predicate_id']='as_of_knowledge'
        value['policy_applications'].append(app)
        self.assertFalse(publication.validate_review(value,self.compiled,self.packet,
            checker_profile=publication.CHECKER_PROFILE)['valid'])

        self.recompile(lambda q:q['contract']['temporal_requirements'].append(dict(
            id='event',basis='event',start_inclusive='2026-10-01T00:00:00Z',end_exclusive='2026-11-01T00:00:00Z')))
        value=self.output(2)
        for link in value['dates']:
            link.update(role='publication_unspecified',link_status='established',
                context_support=deepcopy(value['candidates'][0]['support']))
        self.assertFalse(publication.validate_review(value,self.compiled,self.packet,
            checker_profile=publication.CHECKER_PROFILE)['valid'])

    def test_offline_checker_replay_is_candidate_only_and_preserves_source(self):
        with TemporaryDirectory() as temp:
            root=Path(temp); parent,cases,native=self.parent(root)
            with patch.object(settlement,'load_parent',return_value=(parent,cases,native)):
                write_private(root/'parent/run/report.json',parent)
                package=settlement.prepare_settlement(root/'parent/run/report.json',root/'derived')
            value=self.output(2);value['dates'][0]['role']='publication_unspecified'
            responses={j['task_id']:dict(verdict=deepcopy(value),transport_success=True,
                native_completion_verified=True) for j in package['jobs']}
            report=settlement.overlay(parent,cases,native,package,responses)
            report['execution']=dict(state='completed')
            source=root/'derived/run/report.json';write_private(source,report)
            before=source.read_bytes()
            with patch.object(checker_replay,'load_parent',return_value=(report,cases,native)),\
                 patch.object(checker_replay,'_completed_responses',return_value=responses),\
                 patch('openweb_news.review_pipeline.backends.subscription',side_effect=AssertionError('offline dispatch')):
                summary=checker_replay.replay(source,root/'diagnostic')
                self.assertTrue(summary['candidate_not_adopted'])
                self.assertEqual(summary['model_calls'],0)
                self.assertEqual(summary['additional_candidate_grades'],{'2':1})
                self.assertEqual(summary['candidate_pair_dispositions'],{'2':1})
                self.assertEqual(source.read_bytes(),before)
                self.assertFalse((root/'diagnostic/report.json').exists())
                self.assertEqual(load(root/'diagnostic/summary.json'),summary)
                with self.assertRaises(ValueError):checker_replay.replay(source,root/'derived/diagnostic')
                with self.assertRaises(ValueError):
                    checker_replay.replay(source,root/'elsewhere/../derived/diagnostic')
                (root/'source-alias').symlink_to(root/'derived',target_is_directory=True)
                with self.assertRaises(ValueError):checker_replay.replay(source,root/'source-alias/diagnostic')
                bad=deepcopy(responses);next(iter(bad.values()))['verdict']['reason']='Changed source'
                with patch.object(checker_replay,'_completed_responses',return_value=bad):
                    with self.assertRaises(ValueError):checker_replay.replay(source,root/'tampered')

    def test_metadata_availability_permission_is_candidate_scoped(self):
        self.recompile(lambda q:q['contract'].update(as_of_requirement=dict(
            meaning='historical_knowledge', cutoff='2026-10-13T00:00:00Z', boundary='exclusive',
            precision='day', timezone='UTC', question_phrase='Facts available before the cutoff.')))
        value = self.output(2)
        link = next(r for r in value['dates'] if r['predicate_id']=='as_of_knowledge')
        link.update(link_status='established', role='fact_availability', date_ids=['N1'],
                    context_support=deepcopy(value['candidates'][0]['support']))
        app = deepcopy(value['policy_applications'][0]); app['predicate_id']='as_of_knowledge'
        value['policy_applications'].append(app)
        result = publication.validate_review(value,self.compiled,self.packet)
        self.assertTrue(result['valid'], result)
        judgment = deepcopy(result['judgment'])
        cid = judgment['candidates'][0]['candidate_id']
        scopes = {(cid,'publication:window'), (cid,'as_of_knowledge')}
        self.assertFalse(evidence.validate_judgment(self.compiled['packet'], judgment,
            calendar_days=True, reviewed_publications=scopes)[0])
        # A second candidate has the same metadata evidence but no availability permission.
        other = deepcopy(judgment['candidates'][0]); other['candidate_id']='unreviewed'
        judgment['candidates'].append(other)
        scopes.add(('unreviewed','publication:window'))
        errors, _ = evidence.validate_judgment(self.compiled['packet'], judgment,
            calendar_days=True, reviewed_publications=scopes)
        self.assertTrue(errors)
        scopes.add(('unreviewed','as_of_knowledge'))
        self.assertFalse(evidence.validate_judgment(self.compiled['packet'], judgment,
            calendar_days=True, reviewed_publications=scopes)[0])

    def parent(self, root):
        cfg = fixtures.config(root / 'unused')
        cfg['tie_order'] = [self.provider['configuration_id']]
        cfg['execution_policy']['scheduling']['recovery'] = {
            role + '-subscription': sibling_rule('judge', cfg['execution_policy']['scheduling']['accounts'][role + '-subscription']['concurrency'])
            for role in ('codex', 'claude')}
        native = dict(configuration=cfg, providers=[self.provider])
        pos = judging.decode_position(self.raw, self.compiled)
        pos.update(independent=True, prior_verdict_exposure=False)
        outcome = dict(case_id=self.compiled['packet']['case_id'], packet_sha256=self.compiled['packet_sha256'],
            configuration_id=self.provider['configuration_id'], question_id=self.compiled['packet']['question']['question_id'], rank=1,
            **settle(pos, pos))
        parent = dict(method_version='synthetic', mode='synthetic-parent', package_sha256='synthetic',
            questions=[self.compiled['packet']['question']], collection=[dict(configuration_id=self.provider['configuration_id'],
            question_id=outcome['question_id'], status='success')], outcomes=[outcome],
            phase_costs={k:'0' for k in ('published_completed_pro_usd','conservative_completed_pro_usd',
                'other_conservative_holds_and_allowances_usd','conservative_total_usd','published_pro_plus_conservative_residual_usd')},
            phase_components=[], review_pipeline=dict(records=[], final_unknowns=1), report_limits=[])
        cases = {outcome['case_id']:dict(compiled=self.compiled, provider=self.provider)}
        for item in parent['outcomes']:
            compact_outcome(item)
        settlement._rescore(parent, cases, native)
        return parent, cases, native

    def test_executor_overlay_and_no_send_resume_including_null(self):
        for grade in (1, None):
            with self.subTest(grade=grade), TemporaryDirectory() as temp:
                root = Path(temp)
                parent, cases, native = self.parent(root)
                # A valid known result in the same question must remain literal.
                known_packet=deepcopy(self.compiled['packet']);known_packet['case_id']='known-case'
                known_compiled=judging.compile_packet(known_packet,self.compiled['input']['captured_at'],config=self.provider)
                known_raw=deepcopy(self.raw);known_raw.update(case_id='known-case',overall_grade=1)
                known_pos=judging.decode_position(known_raw,known_compiled)
                known_pos.update(independent=True,prior_verdict_exposure=False)
                known=dict(case_id='known-case',packet_sha256=known_compiled['packet_sha256'],
                    configuration_id=self.provider['configuration_id'],question_id=known_packet['question']['question_id'],
                    rank=2,**settle(known_pos,known_pos))
                compact_outcome(known);parent['outcomes'].append(known)
                cases['known-case']=dict(compiled=known_compiled,provider=self.provider)
                settlement._rescore(parent,cases,native)
                write_private(root / 'parent/run/report.json', parent)
                before = (root / 'parent/run/report.json').read_bytes()
                with patch.object(settlement, 'load_parent', return_value=(parent, cases, native)):
                    package = settlement.prepare_settlement(root / 'parent/run/report.json', root / 'derived')
                    package['mode'] = 'fixture'
                    write_private(root / 'derived/package.json', package, replace=True)
                    def call(job, folder):
                        return dict(verdict=self.output(grade), transport_success=True, native_completion_verified=True)
                    report = settlement.run_settlement(root / 'derived/package.json', {}, call=call)
                    self.assertEqual(report['execution']['state'], 'completed')
                    self.assertEqual(report['outcomes'][0]['grade'], grade)
                    self.assertEqual(report['outcomes'][1],known)
                    replay = settlement.run_settlement(root / 'derived/package.json', {}, call=lambda *_:self.fail('resume dispatched'))
                    self.assertEqual(replay['execution']['reused_tasks'], 2)
                    self.assertEqual(report['outcomes'], replay['outcomes'])
                    self.assertEqual(settlement.selected(report, cases, package['reviewers']), [])
                    self.assertEqual((root / 'parent/run/report.json').read_bytes(), before)
                    html = (root / 'derived/run/report.html').read_text()
                    self.assertIn('Publication review', html)
                    self.assertIn('id="cutoff-5" checked', html)
                    tampered = deepcopy(package); tampered['jobs'][0]['blind_packet']['input']['prior_labels'] = [2]
                    with self.assertRaises(ValueError):settlement.verify(tampered, root / 'derived')
                    tampered=deepcopy(package);tampered['max_attempts_per_task']+=1
                    with self.assertRaises(ValueError):settlement.verify(tampered,root/'derived')
                    tampered=deepcopy(package);tampered['run_directory']=str(root/'parent/run')
                    with self.assertRaises(ValueError):settlement.verify(tampered,root/'derived')

    def test_exact_live_approval_rejected_before_preflight(self):
        with TemporaryDirectory()as temp:
            root=Path(temp);parent,cases,native=self.parent(root)
            write_private(root/'parent/run/report.json',parent)
            with patch.object(settlement,'load_parent',return_value=(parent,cases,native)):
                package=settlement.prepare_settlement(root/'parent/run/report.json',root/'derived')
                with patch('openweb_news.review_pipeline.backends.subscription',side_effect=AssertionError('unapproved preflight')):
                    with self.assertRaises(ValueError):
                        settlement.run_settlement(root/'derived/package.json',dict(approved=True,package_sha256='wrong'))

    def test_historical_live_closure_replays_real_native_proof_checks_without_external_calls(self):
        """Synthetic native logs exercise production proof checks, not real model quality."""
        with TemporaryDirectory()as temp:
            root=Path(temp);parent,cases,native=self.parent(root)
            parent_path=root/'parent/run/report.json';write_private(parent_path,parent)
            original_load=settlement.load_parent
            def parent_loader(path,**kwargs):
                return (parent,cases,native)if Path(path).resolve()==parent_path.resolve() else original_load(path,**kwargs)
            with patch.object(settlement,'load_parent',side_effect=parent_loader):
                package=settlement.prepare_settlement(parent_path,root/'derived')
                approval=dict(approved=True,package_sha256=digest(package))
                write_private(root/'derived/approval.json',approval)
                def original(role,compiled,folder):
                    return dict(verdict=self.output(1),transport_success=True,complete=True,served_model='synthetic-model')
                emitters={'codex':test_subscription_health.native_model(original),
                          'claude':test_subscription_health.native_claude(original)}
                def emit(role,packet,folder):
                    proxy=dict(self.compiled,input=packet['input'],prompt=packet['prompt'],schema=packet['schema'],
                               input_sha256=digest(packet['input']),schema_sha256=digest(packet['schema']),
                               prompt_sha256=digest(packet['prompt'].encode()))
                    return emitters[role](role,proxy,folder)
                def subscription(config,packet,folder,**kwargs):
                    return dict(transport_success=True)if kwargs.get('preflight_only')else emit('claude',packet,folder)
                class Runtime:
                    def __init__(runtime,*args,**kwargs):runtime.client=SimpleNamespace(failure=None)
                    def __enter__(runtime):return runtime
                    def __exit__(runtime,*args):pass
                    def review(runtime,packet,folder):return emit('codex',packet,folder)
                with patch('openweb_news.review_pipeline.backends.subscription',side_effect=subscription),\
                     patch('openweb_news.codex_runtime.CodexRuntime',Runtime),\
                     patch('openweb_news.quota.QuotaGate',return_value=None):
                    result=settlement.run_settlement(root/'derived/package.json',approval)
                self.assertEqual(result['execution']['state'],'completed')
                self.assertEqual(result['outcomes'][0]['grade'],None)
                replay,_,_=settlement.load_parent(root/'derived/run/report.json')
                self.assertEqual(result['outcomes'],replay['outcomes'])
                self.assertTrue(replay['publication_review']['records'][0]['terminal'])
                self.assertEqual(settlement.selected(replay,cases,package['reviewers']),[])
                ledger=(root/'derived/run/attempts.jsonl').read_bytes()
                with patch('openweb_news.review_pipeline.backends.subscription',side_effect=AssertionError('resume preflight')):
                    resumed=settlement.run_settlement(root/'derived/package.json',approval)
                self.assertEqual(resumed['execution']['reused_tasks'],2)
                self.assertEqual((root/'derived/run/attempts.jsonl').read_bytes(),ledger)
                path=next((root/'derived/run/attempts').glob('*/runtime-events.jsonl'))
                path.write_bytes(path.read_bytes()+b' \n')
                with self.assertRaises(ValueError):settlement.load_parent(root/'derived/run/report.json')


if __name__ == '__main__':
    unittest.main()
