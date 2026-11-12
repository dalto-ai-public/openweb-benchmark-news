from copy import deepcopy
from datetime import datetime, timezone
import unittest

from openweb_news import date_reading as reading, judging, publication
from openweb_news._method import text_dates, evidence
from . import test_publication


class DateReadingTests(unittest.TestCase):
    def test_complete_dates_in_quoted_context_and_legacy_identity(self):
        samples={
            'Published 15-06-2026, 06:52 pm':'2026-06-15',
            '13. Oct 2026':'2026-10-13',
            'February 12, 2025 | 07:09 pm PT':'2025-02-12',
            '23-Jul-2026 | 20:00 EDT':'2026-07-23',
            '30-Sep-2025':'2025-09-30', 'Sept. 2 , 2024':'2024-09-02',
            '2026/7/22 16:38':'2026-07-22',
            '27.08.2026 / 18:36 CET/CEST':'2026-08-27',
            r'24\.9.2026 06:30:00 CEST':'2026-09-24',
            '24 September 2026at 09:08':'2026-09-24',
            'July 31, 20262:31 PM UTC':'2026-07-31',
            '10/10/2026October 10, 2026':'2026-10-10'}
        for quote,date in samples.items():
            with self.subTest(quote=quote):
                result=reading.parse(quote);self.assertTrue(result['interval']);self.assertTrue(result['normalized'].startswith(date))
        for quote in ['2026','2026-04','2026-04-03','2026-04-03T10:20:00Z','April 3, 2026','today','Q2 2026','April–May 2026']:
            self.assertEqual(reading.parse(quote),text_dates.parse(quote))
        for quote in ['Published 3 May 2026 and updated 4 May 2026','Published 31/02/2026','no date','Omar 12, 2026 said','Grammar 5, 2026','Hajun 3, 2026','Ihmar 1 2026','15-06/2026','3-1/2026','10/10/2026October 11, 2026','Published 15-06-2026 11.59 PM UTC','Published 15-06-2026 11 PM UTC','Published 15-06-2026T11.59 PM UTC','Published 15-06-2026 11:59 +00:99','Published 15-06-2026 06:52 p.m. UTC','Published 15-06-2026 23:00 GMT+0200','Published 15-06-2026 23:59:999','Published 15-06-2026 23:59:','Published 15-06-2026 23:59:30.1234567']:
            self.assertIsNone(reading.parse(quote)['interval'])

    def compare(self, quote, start, end, *, expected='first_publication', capture=None, others=(), item_natives=(), profile=reading.LATEST_PROFILE, context_support=()):
        row=dict(reading.parse(quote),field='snippet',quote=quote,_native=False)
        reader=reading.Reader(captured_at=capture,profile=profile)
        result=reader.compare(row,dict(start_inclusive=start,end_exclusive=end),True,
                              key=('C','publication:window'),rows=[row,*others],expected=expected,item_natives=item_natives,context_support=context_support)
        return result,reader.comparisons

    def test_ambiguous_comparison_capture_and_conflicting_hints(self):
        quote='9/3/2026'
        self.assertEqual(len(reading.parse(quote)['alternatives']),2)
        self.assertEqual(self.compare(quote,'2026-01-01T00:00:00Z','2027-01-01T00:00:00Z')[0],'pass')
        self.assertEqual(self.compare(quote,'2025-01-01T00:00:00Z','2026-01-01T00:00:00Z')[0],'fail')
        self.assertEqual(self.compare(quote,'2026-03-01T00:00:00Z','2026-04-01T00:00:00Z')[0],'unknown')
        args=('12/10/2026','2026-10-01T00:00:00Z','2026-11-01T00:00:00Z')
        self.assertEqual(self.compare(*args,capture='2026-11-02T00:00:00Z')[0],'pass')
        self.assertEqual(self.compare(*args,capture='2026-11-02T00:00:00Z',expected='event')[0],'unknown')
        contrary=dict(evidence.date_interval('2026-12-10'),_native=True,_reviewed_or_first_publication=True)
        self.assertEqual(self.compare(*args,capture='2026-11-02T00:00:00Z',others=[contrary])[0],'unknown')
        instant=dict(evidence.date_interval('2026-10-12T00:00:00.000Z'),_native=True,_reviewed_or_first_publication=True)
        self.assertEqual(self.compare(*args,others=[instant])[0],'pass')
        unreviewed=dict(contrary,_reviewed_or_first_publication=False)
        self.assertEqual(self.compare(*args,others=[unreviewed])[0],'unknown')
        self.assertEqual(self.compare(*args,others=[contrary])[0],'fail')

    def test_same_day_witnesses_do_not_promote_roles_or_override_conflicts(self):
        args=('01.07.2026 / 20:40 CET/CEST','2026-07-01T00:00:00Z','2026-08-01T00:00:00Z')
        instant=dict(evidence.date_interval('2026-07-01T00:00:00.000Z'),date_kind='first_publication',field='native-date')
        status,provenance=self.compare(*args,item_natives=[instant])
        self.assertEqual(status,'pass')
        self.assertTrue(any(r.get('witnesses')for r in provenance['C','publication:window']))
        original=deepcopy(instant)
        self.compare(*args,item_natives=[instant])
        self.assertEqual(instant,original)
        self.assertEqual(self.compare(*args,item_natives=[instant],profile=reading.PROFILE)[0],'unknown')
        own=dict(evidence.date_interval('2026-07-01'),_native=False,field='snippet',quote='1 July 2026')
        self.assertEqual(self.compare(*args,others=[own])[0],'pass')
        self.assertEqual(self.compare(*args,others=[own],expected='event')[0],'pass')
        self.assertEqual(self.compare(*args,others=[own],expected='event',profile=reading.PROFILE)[0],'unknown')
        shifted=dict(evidence.date_interval('2026-06-30'),date_kind='unclear',field='native-date')
        self.assertEqual(self.compare(*args,item_natives=[shifted])[0],'pass')
        for kind in ['update','index','event']:
            native=dict(instant,date_kind=kind)
            self.assertEqual(self.compare(*args,item_natives=[native])[0],'unknown',kind)
        coarse=dict(evidence.date_interval('2026-07'),date_kind='publication_unspecified')
        self.assertEqual(self.compare(*args,item_natives=[coarse])[0],'unknown')
        other=dict(evidence.date_interval('2026-01-07'),date_kind='publication_unspecified')
        self.assertEqual(self.compare(*args,item_natives=[instant,other])[0],'unknown')
        self.assertEqual(self.compare(*args,item_natives=[instant],expected='event')[0],'unknown')
        # A native hint for the future reading must not erase a conflicting capture bound.
        future=dict(evidence.date_interval('2026-12-10'),date_kind='unclear')
        self.assertEqual(self.compare('12/10/2026','2026-10-01T00:00:00Z','2026-11-01T00:00:00Z',capture='2026-11-02T00:00:00Z',item_natives=[future])[0],'unknown')
        self.assertEqual(self.compare(*args)[0],'unknown')

    def test_context_dates_are_hints_only_with_conflicts_and_profile_compatibility(self):
        context=dict(field='snippet',quote='Stock quoted on October 9, 2026.',start=0,end=32)
        args=('10/09/2026','2026-08-20T00:00:00Z','2026-09-20T00:00:00Z')
        status,proof=self.compare(*args,context_support=[context])
        self.assertEqual(status,'fail')
        self.assertIn('same_link_context_day_match',proof['C','publication:window'][0]['hints'])
        for profile in (reading.PROFILE,reading.SAME_DAY_PROFILE):
            self.assertEqual(self.compare(*args,context_support=[context],profile=profile)[0],'unknown')
        contrary=dict(context,quote='Other date September 10, 2026.')
        self.assertEqual(self.compare(*args,context_support=[context,contrary])[0],'unknown')
        self.assertEqual(self.compare(*args,context_support=[dict(context,quote='2026')])[0],'unknown')
        self.assertEqual(self.compare(*args,context_support=[dict(context,quote='October 9, 2026 and September 10, 2026')])[0],'unknown')

    def test_timestamp_boundary_preserves_clock_and_offset(self):
        # January 5, 21:23 ET is after January 6, 00:00 UTC.
        status,_=self.compare('Published: 1/5/2026 9:23 PM ET','2026-01-01T00:00:00Z','2026-01-06T00:00:00Z')
        self.assertEqual(self.compare('Published 1/5/2026 00:30 +14:00','2026-01-05T00:00:00Z','2026-01-06T00:00:00Z',capture='2026-02-01T00:00:00Z')[0],'fail')
        self.assertEqual(status,'fail') # Other reading is May 1, also outside.
        self.assertEqual(self.compare('Published 15-06-2026 23:59 UTC','2026-06-15T00:00:00Z','2026-06-16T00:00:00Z')[0],'pass')
        self.assertEqual(self.compare('Published 15-06-2026 23:59','2026-06-15T00:00:00Z','2026-06-16T00:00:00Z')[0],'unknown')

    def test_historical_cutoff_preserves_new_clock_precision(self):
        row=reading.parse('Published 15-06-2026 23:30 -12:00')
        reader=reading.Reader()
        cutoff=datetime(2026,6,16,tzinfo=timezone.utc)
        self.assertEqual(reader.cutoff(row,cutoff,True),'fail')
        from openweb_news._method import two_step
        f=test_publication.PublicationTests();f.setUp();packet=deepcopy(f.compiled['packet'])
        quote='Published 15-06-2026 23:30 -12:00'
        field=packet['allowed_text'][0]['field'];packet['allowed_text'][0]['text']+=' '+quote
        packet['as_of_interpretation']=dict(cutoff='2026-06-16T00:00:00Z',boundary='exclusive',precision='day')
        link=deepcopy(f.raw['dates'][0]);link.update(predicate_id='as_of_knowledge',role='fact_availability',link_status='established',date_ids=[],additional_dates=[dict(field=field,quote=quote,occurrence=1)],context_support=[dict(field=field,quote=quote,occurrence=1)])
        status,_=two_step.date_condition(packet,dict(predicate_id='as_of_knowledge'),link,calendar_days=True,date_reader=reader)
        self.assertEqual(status['status'],'fail')

    def test_saved_input_and_default_decode_stay_identical(self):
        fixture=test_publication.PublicationTests();fixture.setUp();c=fixture.compiled;before=deepcopy(c)
        old=judging.decode_position(fixture.raw,c)
        judging.decode_position(fixture.raw,c,date_profile=reading.PROFILE)
        latest=judging.decode_position(fixture.raw,c,date_profile=reading.LATEST_PROFILE)
        self.assertIn('date_comparisons',latest)
        self.assertNotIn('date_comparisons',old)
        self.assertEqual(c,before);self.assertEqual(judging.decode_position(fixture.raw,c),old)


class PublicationReadingTests(unittest.TestCase):
    def setUp(self):
        self.fixture=test_publication.PublicationTests();self.fixture.setUp()

    def test_scoped_coarse_refinement_and_negative_guards(self):
        f=self.fixture;c=deepcopy(f.compiled);packet=c['packet'];field=packet['allowed_text'][0]['field']
        packet['allowed_text'][0]['text']+=' 2026'
        # Retain the fixture's native date and policy mapping, compile a fresh synthetic packet.
        c=judging.compile_packet(packet,c['input']['captured_at'],config=f.provider);p=publication.packet_for(c)
        value=f.output(2);value['dates'][0]['additional_dates']=[dict(field=field,quote='2026',occurrence=1)]
        value['dates'][0]['role']='first_publication'
        # Fixture has a different year: use an in-window broad witness from its own date.
        year=c['input']['native_date_ids'][0]['raw_value'][:4]
        if year!='2026':
            packet['allowed_text'][0]['text']=packet['allowed_text'][0]['text'].removesuffix(' 2026')+' '+year
            c=judging.compile_packet(packet,c['input']['captured_at'],config=f.provider);p=publication.packet_for(c)
            value['dates'][0]['additional_dates'][0]['quote']=year
        before=deepcopy(value)
        result=publication.validate_review(value,c,p,checker_profile=publication.READING_PROFILE)
        self.assertTrue(result['valid'],result);self.assertEqual(result['grade'],2)
        self.assertEqual(result['reviewed_date_fills'][0]['kind'],'compatible_coarser')
        self.assertEqual(value,before)
        for mutation in ['no_application','year_only','bad_span','nonpublication_fail','all_dates_fail']:
            v=deepcopy(value)
            if mutation=='no_application':v['policy_applications']=[]
            if mutation=='year_only':
                v['policy_applications'][0]['policy']=publication.TEXT_POLICY
                v['dates'][0]['date_ids']=[]
            if mutation=='bad_span':v['policy_applications'][0]['checks'][0]['support'][0]['quote']='absent quote'
            if mutation=='nonpublication_fail':v['candidates'][0]['conditions'][0]['status']='fail'
            if mutation=='all_dates_fail':v['policy_applications'][0]['status']='fail'
            self.assertFalse(publication.validate_review(v,c,p,checker_profile=publication.READING_PROFILE)['valid'],mutation)
