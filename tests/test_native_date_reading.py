"""Versioned native literal reading across actual decoder and review boundaries."""
from copy import deepcopy
import unittest

from openweb_news import adjudication, date_reading, judging, publication, phase_review, review_pipeline
from openweb_news.review import settle
from openweb_news.common import digest
from .test_blind_input import compiled_for
from . import test_blind_integration, test_publication
from .helpers import verdict


class NativeDateReadingTests(unittest.TestCase):
    def setUp(self):
        self.helper = test_blind_integration.BlindIntegrationTests(); self.helper.setUp()
        self.profile = date_reading.NATIVE_LITERAL_PROFILE

    def case(self, provider='brave-web', date='Oct 12, 2026'):
        config = self.helper.selected(next(c for c in self.helper.catalog if c['configuration_id'] == provider))
        compiled = compiled_for(config, date=date)
        native = next(r for r in compiled['input']['native_date_ids']
                      if r['relative_path'] in config['date_trust']['eligible_fields'])
        value = verdict(compiled)
        quote = dict(field=compiled['packet']['allowed_text'][0]['field'],
                     quote=compiled['packet']['allowed_text'][0]['text'], occurrence=1)
        def remap(node):
            if isinstance(node,dict):
                if node.get('field')=='title':node['field']=quote['field']
                for child in node.values():remap(child)
            elif isinstance(node,list):
                for child in node:remap(child)
        remap(value)
        value['dates'] = [dict(candidate_id=value['candidates'][0]['candidate_id'],
            predicate_id='publication:window', link_status='established', role=native['date_kind'],
            date_ids=[native['date_id']], anchor_date_id='', additional_dates=[],
            context_support=[quote], reason='Own item publication date.')]
        review = dict(deepcopy(value), complete_occurrence_reviewed=True,
            contrary_reading='A conflicting own publication date would defeat this inference.',
            policy_applications=[dict(candidate_ids=[value['candidates'][0]['candidate_id']],
                predicate_id='publication:window', policy=publication.NATIVE_POLICY, status='pass',
                checks=[dict(check=k,status='pass',reason='Own fixed item and publication context checked.',
                             support=[deepcopy(quote)]) for k in publication.CHECKS])])
        return config, compiled, native, value, review

    def test_all_seven_fields_reach_decoder_triage_review_and_final_validation(self):
        for provider in ('brave-web','exa-highlights','parallel-advanced','perplexity-web',
                         'valyu-search','keenable-pro','seltz-web-pro'):
            with self.subTest(provider=provider):
                config,compiled,native,value,review=self.case(provider)
                self.assertIsNone(native['interval'])
                before=deepcopy(compiled); wire=digest(judging.blind_packet(compiled))
                raw=self.helper.model_value(compiled,value)
                pos=judging.decode_position(raw,compiled,date_profile=self.profile)
                self.assertEqual(pos['structural_base_grade'],2 if native['date_kind']=='first_publication' else 1,pos)
                positions={r:deepcopy(pos)for r in ('pro','codex')}
                selected=adjudication.eligible(compiled,positions,config,date_profile=self.profile)
                if native['date_kind']=='first_publication':self.assertEqual(selected,[])
                else:self.assertIn(native['date_id'],selected)
                self.assertEqual(adjudication.eligible(compiled,positions,config,date_profile=date_reading.LATEST_PROFILE),[])
                packet=publication.packet_for(compiled)
                model_review=self.helper.model_value(compiled,review,publication.packet_for(compiled,_source=True))
                old=publication.validate_review(model_review,compiled,packet,
                    checker_profile=publication.READING_PROFILE,date_profile=date_reading.LATEST_PROFILE)
                self.assertFalse(old['valid'])
                new=publication.validate_review(model_review,compiled,packet,
                    checker_profile=publication.READING_PROFILE,date_profile=self.profile)
                self.assertTrue(new['valid'],new);self.assertEqual(new['grade'],2)
                self.assertEqual(compiled,before);self.assertEqual(digest(judging.blind_packet(compiled)),wire)

    def test_completed_ordinary_review_replays_with_v4_eligibility(self):
        config,compiled,_,value,_=self.case()
        raw=self.helper.model_value(compiled,value)
        cid=compiled['packet']['case_id']
        case=dict(compiled=compiled,provider=config,verdicts={r:deepcopy(raw) for r in ('pro','codex')},
                  original=dict(positions={r:{} for r in ('pro','codex')}))
        positions,_=review_pipeline.decode(case,'unicode-spaces-v1',date_profile=self.profile)
        ids=adjudication.eligible(compiled,positions,config,date_profile=self.profile)
        source=adjudication.packet_for(compiled,ids,_source=True)
        quote=deepcopy(value['dates'][0]['context_support'])
        disposition=dict(disposition='confirm',selected_native_date_ids=ids,
            checks=[dict(check=k,status='pass',reason='Own item checked.',support=quote) for k in adjudication.CHECKS],
            contrary_reading='No own conflict found.',reason='All scoped checks supported.')
        wire=self.helper.model_value(compiled,disposition,source)
        responses={r:dict(verdict=deepcopy(wire),transport_success=True,native_completion_verified=True)
                   for r in ('codex','claude')}
        effective,record=adjudication.corroborate(compiled,positions,adjudication.packet_for(compiled,ids),
            responses,config,date_profile=self.profile)
        self.assertTrue(record['accepted'])
        outcome=settle(effective['pro'],effective['codex']);self.assertEqual(outcome['grade'],2)
        record['case_id']=cid
        report=dict(outcomes=[dict(case_id=cid,**outcome)],review_pipeline=dict(records=[record]))
        phase_review.replay_completed_review(dict(text_profile='unicode-spaces-v1',date_profile=self.profile,
            adjudication_profile=adjudication.PROOF_PROFILE),
            {cid:case},{'adjudication-'+r+'-'+cid:response for r,response in responses.items()},report)

    def test_historical_cutoff_requires_same_reviewed_publication_witness(self):
        for cutoff,valid in (('2026-10-11T00:00:00Z',False),
                             ('2026-10-12T12:00:00Z',False),
                             ('2026-10-13T00:00:00Z',True)):
            with self.subTest(cutoff=cutoff):
                f=test_publication.PublicationTests();f.setUp()
                f.provider=self.helper.selected(f.provider)
                f.recompile(lambda q:q['contract'].update(as_of_requirement=dict(
                    meaning='historical_knowledge',cutoff=cutoff,boundary='exclusive',precision='day',
                    timezone='UTC',question_phrase='Facts available before cutoff.')),
                    extra_dates={'publication_date':'Oct 12, 2026'})
                value=f.output(2)
                knowledge=next(r for r in value['dates']if r['predicate_id']=='as_of_knowledge')
                knowledge.update(link_status='established',role='fact_availability',date_ids=['N1'],
                                 context_support=deepcopy(value['candidates'][0]['support']))
                def check(v):
                    raw=self.helper.model_value(f.compiled,v,publication.packet_for(f.compiled,_source=True))
                    return publication.validate_review(raw,f.compiled,f.packet,
                        checker_profile=publication.READING_PROFILE,date_profile=self.profile)
                self.assertFalse(check(value)['valid'])
                app=deepcopy(value['policy_applications'][0]);app['predicate_id']='as_of_knowledge'
                value['policy_applications'].append(app)
                self.assertEqual(check(value)['valid'],valid)
                # Availability cannot use an unreviewed second native witness.
                value['dates'][0]['date_ids']=[]
                self.assertFalse(check(value)['valid'])

    def test_unresolved_and_outside_window_cannot_be_forced_to_pass(self):
        for raw in ('Oct 12','10/12/2026','yesterday','reported Oct 12, 2026',
                    'Oct 12, 2026 23:59:999','Oct 32, 2026','Dec 12, 2026'):
            with self.subTest(raw=raw):
                _,compiled,_,_,review=self.case(date=raw)
                value=self.helper.model_value(compiled,review,publication.packet_for(compiled,_source=True))
                self.assertFalse(publication.validate_review(value,compiled,publication.packet_for(compiled),
                    checker_profile=publication.READING_PROFILE,date_profile=self.profile)['valid'])

    def test_excluded_fields_roles_and_legacy_profiles_are_unchanged(self):
        _,compiled,native,_,_=self.case()
        rows=deepcopy(compiled['packet']['date_observations']);before=deepcopy(rows)
        allowed=judging.native_publication_fields(compiled)
        for profile in (None,*date_reading.PROFILES):
            view=date_reading.native_reading_rows(rows,allowed,profile=profile)
            self.assertEqual(rows,before)
            for prior,current in zip(rows,view):
                if profile!=self.profile or prior['field'] not in allowed:
                    self.assertEqual(prior,current)
                else:
                    self.assertTrue(current['interval']);self.assertEqual(current['raw_value'],prior['raw_value'])
                    self.assertEqual(current['date_kind'],prior['date_kind'])
        for kind in ('unclear','update','index','event'):
            row=dict(native,date_kind=kind)
            self.assertEqual(date_reading.native_reading_rows([row],allowed,profile=self.profile),[row])
        self.assertEqual(date_reading.native_reading_rows(rows,None,profile=self.profile),rows)
