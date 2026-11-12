from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest

from openweb_news import date_report,representation_replay,review_pipeline,representation,judging
from openweb_news.common import digest,load
from . import test_date_report,test_publication
from copy import deepcopy


class QuoteReplayTests(unittest.TestCase):
    def test_full_verdict_repair_uses_original_raw_support(self):
        fixture=test_publication.PublicationTests();fixture.setUp()
        packet=deepcopy(fixture.compiled['packet']);packet['allowed_text'][0]['text']=packet['allowed_text'][0]['text'].replace(' ', '\u00a0',1)
        compiled=judging.compile_packet(packet,fixture.compiled['input']['captured_at'],config=fixture.provider)
        case=dict(compiled=compiled,verdicts={r:deepcopy(fixture.raw)for r in ('pro','codex')},original=dict(positions={}))
        old,_=review_pipeline.decode(case,representation.PROFILE)
        new,_=review_pipeline.decode(case,representation.SPACE_PROFILE)
        self.assertEqual(old['codex']['state'],'invalid');self.assertEqual(new['codex']['state'],'valid')
        audit=new['codex']['quote_reconciliations']
        self.assertTrue(any(q['mode']==representation.SPACE_PROFILE+'_to_raw_span'for q in audit))
        for quote in audit:
            text=next(x['text']for x in packet['allowed_text']if x['field']==quote['field'])
            self.assertEqual(quote['quote'],text[quote['start']:quote['end']])
        # Validity alone cannot turn the unsupported policy/base proposal into a vote.
        self.assertNotEqual(new['codex'].get('policy_conformance'),'same_as_structural_base')

    def test_complete_replay_is_offline_reproduces_source_and_never_adopts(self):
        with TemporaryDirectory()as temp:
            root=Path(temp);args=test_date_report.DateReportTests().source(root)
            p=date_report.prepare(**args,output=root/'rebuild',maximum_new_third=0,mode='fixture')
            approval=dict(approved=True,package_sha256=digest(p),approved_by='synthetic',approved_at='2000-01-01T00:00:00Z')
            date_report.run(root/'rebuild/package.json',approval)
            source=root/'rebuild/report/report.json';before=source.read_bytes()
            with patch('openweb_news.backends.subscription',side_effect=AssertionError('no dispatch')),patch('socket.socket.connect',side_effect=AssertionError('offline')),patch('subprocess.Popen',side_effect=AssertionError('offline')):
                result=representation_replay.replay(root/'rebuild/package.json',source,root/'diagnostic',expected_source_sha256=digest(before))
            self.assertEqual(result['cases'],1);self.assertEqual(result['model_calls'],0)
            self.assertTrue(result['baseline_reproduced']);self.assertFalse(result['adopted'])
            self.assertEqual(result['known_changes'],[]);self.assertEqual(source.read_bytes(),before)
            self.assertFalse((root/'diagnostic/report/report.html').exists())
            with self.assertRaises(ValueError):representation_replay.replay(root/'rebuild/package.json',source,root/'bad',expected_source_sha256='0'*64)
            self.assertFalse((root/'bad').exists())
