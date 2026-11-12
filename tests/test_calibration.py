from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest

from openweb_news import calibration, judging
from openweb_news.common import digest, load, write_private
from . import test_publication as publication_tests


class CalibrationTests(unittest.TestCase):
    def setup_package(self, root, count=5):
        f=publication_tests.PublicationTests();f.setUp();parent,_,native=f.parent(root)
        source=root/'source/run/report.json';write_private(source,parent)
        cases={};rows=[]
        for i in range(count):
            packet=deepcopy(f.compiled['packet']);packet['case_id']='control-'+str(i)
            compiled=judging.compile_packet(packet,f.compiled['input']['captured_at'],config=f.provider)
            cases[packet['case_id']]=dict(compiled=compiled,provider=f.provider)
            text=packet['allowed_text'][0]
            rows.append(dict(case_id=packet['case_id'],category=('synthetic-date','synthetic-container','synthetic-window')[i%3],
                reason='Synthetic negative control.',support=[dict(field=text['field'],quote=text['text'],occurrence=1)]))
        selection=root/'selection.json';write_private(selection,dict(cases=rows))
        return f,parent,cases,native,source,selection

    def test_reviewed_bank_sizes_and_categories_are_data_not_runtime_rules(self):
        for count in (2,7,13):
            with self.subTest(count=count),TemporaryDirectory()as temp:
                root=Path(temp);_,parent,cases,native,source,selection=self.setup_package(root,count)
                rows=load(selection);rows['cases'][0]['category']='another-reviewed-evidence-class'
                write_private(selection,rows,replace=True)
                with patch.object(calibration,'load_parent',return_value=(parent,cases,native)):
                    p=calibration.prepare(source,selection,root/'controls')
                    self.assertEqual(len(p['jobs']),2*count)
                    self.assertEqual(p['maximum_new_subscription_tasks'],2*count)
                    calibration.verify(p,root/'controls')
                    rows['cases'].append(deepcopy(rows['cases'][0]));write_private(selection,rows,replace=True)
                    with self.assertRaises(ValueError):calibration.prepare(source,selection,root/'duplicate')
                    self.assertFalse((root/'duplicate').exists())

    def test_diagnostic_gate_no_send_resume_and_no_grade_writeback(self):
        for grade in (0,1,None,2):
            with self.subTest(grade=grade),TemporaryDirectory()as temp:
                root=Path(temp);f,parent,cases,native,source,selection=self.setup_package(root)
                before=source.read_bytes()
                with patch.object(calibration,'load_parent',return_value=(parent,cases,native)):
                    p=calibration.prepare(source,selection,root/'calibration');p['mode']='fixture'
                    write_private(root/'calibration/package.json',p,replace=True)
                    def call(job,folder):
                        v=f.output(grade);v['case_id']=job['case_id']
                        return dict(verdict=v,transport_success=True,native_completion_verified=True)
                    result=calibration.run(root/'calibration/package.json',{},call=call)
                    self.assertEqual(result['calibration_pass'],grade!=2)
                    self.assertEqual(len(result['audits']),10)
                    self.assertTrue(result['diagnostic_only']);self.assertFalse(result['benchmark_grades_changed'])
                    result=calibration.run(root/'calibration/package.json',{},call=lambda *_:self.fail('resend'))
                    self.assertEqual(result['execution']['reused_tasks'],10)
                    self.assertEqual(source.read_bytes(),before)
                    self.assertFalse((root/'calibration/run/report.json').exists())
                    bad=deepcopy(p);bad['jobs'][0]['blind_packet']['input']['expected_grade']=0
                    with self.assertRaises(ValueError):calibration.verify(bad,root/'calibration')

    def test_live_approval_and_blind_input_boundaries(self):
        with TemporaryDirectory()as temp:
            root=Path(temp);_,parent,cases,native,source,selection=self.setup_package(root)
            with patch.object(calibration,'load_parent',return_value=(parent,cases,native)):
                p=calibration.prepare(source,selection,root/'calibration')
                for j in p['jobs']:
                    self.assertNotIn('control',j['blind_packet']['input'])
                    self.assertNotIn('category',j['blind_packet']['input'])
                    self.assertNotIn('expected_grade',j['blind_packet']['input'])
                approval=dict(approved=True,package_sha256=digest(p),approved_by='owner',approved_at='synthetic')
                with patch('openweb_news.review_pipeline.backends.subscription',side_effect=AssertionError('preflight')):
                    with self.assertRaises(ValueError):calibration.run(root/'calibration/package.json',approval)
                    approval['diagnostic_known_grade_review']=True
                    with self.assertRaises(ValueError):calibration.run(root/'calibration/package.json',approval,call=lambda *_:None)
                bad=load(selection);bad['cases'][0]['support'][0]['quote']='not in own evidence'
                write_private(selection,bad,replace=True)
                with self.assertRaises(ValueError):calibration.prepare(source,selection,root/'bad')


if __name__=='__main__':unittest.main()
