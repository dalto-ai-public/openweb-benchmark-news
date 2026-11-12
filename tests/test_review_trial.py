from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest

from openweb_news import settlement,publication
from openweb_news.common import write_private,load
from . import test_publication


class ReviewTrialTests(unittest.TestCase):
    def test_equal_frequency_category_ties_have_a_fixed_sample(self):
        rows=[('a','B',False),('b','A',False),('c','A',True),('d','C',True)]
        numeric=dict(state='valid',grade=1,policy_conformance='same_as_structural_base')
        invalid=dict(state='invalid')
        parent=dict(outcomes=[dict(case_id=cid,configuration_id=provider,positions=dict(pro=numeric if available else invalid,codex=invalid))for cid,provider,available in rows])
        roster=[dict(case_id=cid)for cid,_,_ in rows]
        self.assertEqual([r['case_id']for r in settlement.representative_roster(roster,parent)],['a','b','d'])

    def test_representative_roster_covers_providers_and_primary_categories(self):
        numeric=lambda n:dict(state='valid',grade=n,policy_conformance='same_as_structural_base')
        invalid=dict(state='invalid')
        outcomes=[];roster=[]
        for cid,provider,pro,sol in [('a','provider-a',invalid,invalid),('b','provider-b',numeric(1),invalid),('c','provider-c',numeric(1),numeric(2)),('d','provider-a',numeric(2),invalid)]:
            outcomes.append(dict(case_id=cid,configuration_id=provider,positions=dict(pro=pro,codex=sol)))
            roster.append(dict(case_id=cid))
        chosen=settlement.representative_roster(roster,dict(outcomes=outcomes))
        selected={r['case_id']for r in chosen};own=[o for o in outcomes if o['case_id']in selected]
        self.assertEqual(len(own),3);self.assertEqual(len({o['configuration_id']for o in own}),3)
        self.assertEqual({settlement.primary_category(o)for o in own},{'no_primary_numeric','one_primary_numeric','conflicting_primaries'})
        self.assertEqual(selected,{r['case_id']for r in settlement.representative_roster(list(reversed(roster)),dict(outcomes=outcomes))})

    def test_trial_is_bounded_blind_and_terminal_null_is_not_retried(self):
        with TemporaryDirectory()as temp:
            root=Path(temp);f=test_publication.PublicationTests();f.setUp();parent,cases,native=f.parent(root)
            parent['outcomes'][0]['positions']['pro']['state']='invalid'
            parent['outcomes'][0]['states']['pro']='invalid'
            parent['active_review_profile']={k:settlement.TRIAL_PROFILES[k]for k in ('checker_profile','date_profile','text_profile')}
            source=root/'parent/run/report.json';write_private(source,parent)
            with patch.object(settlement,'load_parent',return_value=(parent,cases,native)):
                with self.assertRaisesRegex(ValueError,'maximum'):settlement.prepare_settlement(source,root/'missing',experimental=True)
                with self.assertRaises(ValueError):settlement.prepare_settlement(source,root/'small',experimental=True,maximum_tasks=0)
                self.assertFalse((root/'small').exists())
                p=settlement.prepare_settlement(source,root/'trial',experimental=True,maximum_tasks=2)
                self.assertEqual(len(p['jobs']),2);self.assertEqual(p['sample_categories'],{'no_primary_numeric':1})
                self.assertNotIn('positions',p['jobs'][0]['blind_packet']['input'])
                for changed in ('profiles','roster','bound'):
                    bad=deepcopy(p)
                    if changed=='profiles':bad['experimental_review_profile']['text_profile']='raw-v1'
                    if changed=='roster':bad['roster']=[]
                    if changed=='bound':bad['declared_maximum_tasks']=0
                    with self.assertRaises(ValueError):settlement.verify(bad,root/'trial')
                p['mode']='fixture';write_private(root/'trial/package.json',p,replace=True)
                calls=[]
                def call(job,folder):
                    calls.append(job['task_id']);return dict(verdict=f.output(None),transport_success=True,native_completion_verified=True)
                result=settlement.run_settlement(root/'trial/package.json',{},call=call)
                self.assertIsNone(result['outcomes'][0]['grade']);self.assertFalse(result['experimental_review']['future_batch_authority'])
                settlement.run_settlement(root/'trial/package.json',{},call=lambda *_:self.fail('resume dispatched'))
                self.assertEqual(len(calls),2)
                self.assertEqual(settlement.selected(result,cases,p['reviewers'],trial_profiles=settlement.TRIAL_PROFILES),[])
