from pathlib import Path
from tempfile import TemporaryDirectory
from copy import deepcopy
from unittest.mock import patch
import unittest

from openweb_news import settlement,profile_authority,publication
from openweb_news.common import digest,write_private
from . import test_publication


class SettlementLiveProfilesTests(unittest.TestCase):
    def test_accepted_profiles_flow_through_review_and_terminal_reuse(self):
        with TemporaryDirectory()as temp:
            root=Path(temp);f=test_publication.PublicationTests();f.setUp();parent,cases,native=f.parent(root)
            profiles=dict(settlement.TRIAL_PROFILES,selection_profile=profile_authority.POLICY_SELECTION)
            parent['active_review_profile']={k:profiles[k]for k in ('checker_profile','date_profile','text_profile')}
            authority=dict(package_path=str(root/'authority/package.json'),package_sha256='synthetic-authority',approval_sha256='owner-accepted',profile_bindings=profiles)
            source=root/'parent/run/report.json';write_private(source,parent)
            with patch.object(settlement,'load_parent',return_value=(parent,cases,native)),patch.object(profile_authority,'read',return_value=authority):
                p=settlement.prepare_settlement(source,root/'review',accepted_profile=authority['package_path'])
                self.assertEqual(p['live_profiles'],profiles);self.assertEqual(len(p['jobs']),2)
                altered=deepcopy(p);altered['live_profiles']['text_profile']='raw-v1'
                with self.assertRaisesRegex(ValueError,'profiles'):settlement.verify(altered,root/'review')
                p['mode']='fixture';write_private(root/'review/package.json',p,replace=True)
                def call(job,folder):return dict(verdict=f.output(None),transport_success=True,native_completion_verified=True)
                result=settlement.run_settlement(root/'review/package.json',{},call=call)
                self.assertEqual(result['active_review_profile'],authority)
                record=result['publication_review']['records'][-1]
                self.assertEqual(record['date_profile'],profiles['date_profile']);self.assertEqual(record['text_profile'],profiles['text_profile'])
                self.assertEqual(settlement.selected(result,cases,p['reviewers'],authority=authority),[])
                self.assertIsNone(result['outcomes'][0]['grade'])
                settlement.run_settlement(root/'review/package.json',{},call=lambda *_:self.fail('resend'))
                bad=deepcopy(result);bad['publication_review']['records'][-1]['text_profile']='raw-v1'
                bad['publication_review']['records'][-1]['terminal_key']='another-profile'
                self.assertEqual(len(settlement.selected(bad,cases,p['reviewers'],authority=authority)),1)
