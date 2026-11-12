from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest

from openweb_news import adoption,settlement,publication
from openweb_news.common import digest,write_private,load
from openweb_news.reporting import write_reports
from . import test_publication as pubtests


class AdoptionTests(unittest.TestCase):
    def test_exact_exception_preserved_gate_offline_report_and_replay(self):
        with TemporaryDirectory()as temp:
            root=Path(temp);f=pubtests.PublicationTests();f.setUp();parent,cases,native=f.parent(root)
            source=root/'source/report/report.json';parent['outcome_counts']={'unknown':1};parent['execution']={'state':'completed'}
            write_reports(source.parent,parent)
            with patch.object(settlement,'load_parent',return_value=(parent,cases,native)):
                original=settlement.prepare_settlement(source,root/'original')
            v=f.output(2);v['dates'][0]['role']='publication_unspecified'
            responses={j['task_id']:dict(verdict=deepcopy(v),transport_success=True,native_completion_verified=True)for j in original['jobs']}
            cid=parent['outcomes'][0]['case_id']
            counts={'2':1}
            bindings=dict(diagnostic_path=str(root/'diagnostic'),diagnostic_sha256='diagnostic',
                calibration_package_path=str(root/'calibration/package.json'),calibration_package_sha256='calibration',
                calibration_sha256='failed-control',source_report_path=str(source),source_report_sha256=digest(source.read_bytes()),
                calibration_pass=False,promoted_case_ids=[cid],candidate_outcome_counts=counts,
                calibration_valid_reviews=9,calibration_total_reviews=10)
            inputs=(bindings,parent,cases,native,original,responses)
            with patch.object(adoption,'_inputs',return_value=inputs):
                p=adoption.prepare(root/'diagnostic',root/'calibration/package.json',root/'adopted')
                approval=dict(approved=True,package_sha256=digest(p),approved_by='owner',approved_at='synthetic')
                with self.assertRaises(ValueError):adoption.run(root/'adopted/package.json',approval)
                approval['accept_failed_calibration']=True
                before=source.read_bytes()
                with patch('openweb_news.review_pipeline.backends.subscription',side_effect=AssertionError('adoption inference')):
                    result=adoption.run(root/'adopted/package.json',approval)
                    replay,_,_=settlement.load_parent(root/'adopted/report/report.json')
                self.assertEqual(digest(result),digest((root/'adopted/report/report.json').read_bytes()))
                self.assertEqual(result['outcomes'],replay['outcomes'])
                self.assertEqual(result['outcomes'][0]['grade'],2)
                self.assertFalse(result['checker_acceptance']['calibration_pass'])
                self.assertTrue(result['checker_acceptance']['one_time_owner_exception'])
                self.assertEqual(result['phase_costs'],parent['phase_costs'])
                self.assertEqual(source.read_bytes(),before)
                saved=(root/'adopted/report/report.json').read_bytes()
                adoption.run(root/'adopted/package.json',approval)
                self.assertEqual((root/'adopted/report/report.json').read_bytes(),saved)
                bad_approval=dict(approval,approved_at='changed')
                with self.assertRaises(ValueError):adoption.run(root/'adopted/package.json',bad_approval)
                bad=deepcopy(bindings);bad['promoted_case_ids']=[]
                with patch.object(adoption,'_inputs',return_value=(bad,*inputs[1:])):
                    with self.assertRaises(ValueError):adoption.load_completed(root/'adopted/report/report.json')
                report_path=root/'adopted/report/report.json';r=load(report_path);r['outcomes'][0]['grade']=0
                write_private(report_path,r,replace=True)
                with self.assertRaises(ValueError):adoption.load_completed(report_path)


if __name__=='__main__':unittest.main()
