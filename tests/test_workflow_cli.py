from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest

from openweb_news import batch, fixtures
from openweb_news.cli import main
from openweb_news.common import digest, encoded, load, write_private
from openweb_news.preparation import prepare


class WorkflowCliTests(unittest.TestCase):
    def invoke(self, *args):
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            code = main(list(args))
        return code, output.getvalue(), error.getvalue()

    def test_repaired_prefix_cli_passes_remaining_scope_without_dispatch(self):
        with TemporaryDirectory()as temp:
            root=Path(temp);archive=root/'archive.json';write_private(archive,dict(synthetic=True))
            with patch('openweb_news.clean_run.prepare',return_value=dict(profile='synthetic',stages=[dict(count=n)for n in (20,30,40,100)]))as prepared:
                code,_,error=self.invoke('prepare-clean-run','--template-package',str(root/'template.json'),
                    '--selection',str(root/'selection.json'),'--accepted-method',str(root/'method.json'),
                    '--archive-reference',str(archive),'--ceiling','2200','--output',str(root/'plan'),
                    '--seed-report',str(root/'repaired.json'),'--seed-review',str(root/'review.json'),
                    '--stage-counts','20','30','40','100')
            self.assertEqual(code,0,error)
            self.assertEqual(prepared.call_args.kwargs['stage_counts'],[20,30,40,100])
            self.assertEqual(prepared.call_args.kwargs['seed_report'],root/'repaired.json')
            self.assertEqual(prepared.call_args.kwargs['seed_review'],root/'review.json')

    def test_bank_commands_preserve_source_and_freeze_complete_order(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            questions = fixtures.config(root / 'run')['questions']
            questions[0]['retained_extra'] = {'note': 'Synthetic source data'}
            raw = b'\n' + b''.join(encoded(q) for q in questions)
            source = root / 'source.jsonl'
            source.write_bytes(raw)
            code, output, _ = self.invoke('validate-bank', '--questions', str(source))
            self.assertEqual(code, 0)
            self.assertIn('not_verified', output)
            code, _, _ = self.invoke('freeze-bank', '--questions', str(source), '--phase-counts', '1', '3', '--output', str(root / 'bank'))
            self.assertEqual(code, 0)
            frozen, _ = batch.frozen_order(root / 'bank/selection.json')
            self.assertEqual(frozen, questions)
            self.assertEqual((root / 'bank/bank.jsonl').read_bytes(), raw)
            self.assertEqual(source.read_bytes(), raw)

    def test_saved_date_diagnostics_command_is_read_only_and_rejects_source_mismatch(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            package = prepare(fixtures.config(root / 'run'), mode='fixture')
            write_private(root / 'package.json', package)
            from openweb_news.runner import run
            run(package)
            source_files = [root / 'package.json', *(root / 'run').rglob('*')]
            before = {p: p.read_bytes() for p in source_files if p.is_file()}
            args = ('date-diagnostics', '--compiled-directory', str(root / 'run/compiled'),
                '--source-package', str(root / 'package.json'), '--source-report', str(root / 'run/report.json'))
            with patch('openweb_news.runner.run', side_effect=AssertionError('no execution')), \
                    patch('socket.socket.connect', side_effect=AssertionError('no network')), \
                    patch('subprocess.Popen', side_effect=AssertionError('no process')):
                code, output, error = self.invoke(*args, '--output', str(root / 'diagnostic'))
            self.assertEqual(code, 0, error)
            self.assertEqual(__import__('json').loads(output)['calls'], 0)
            self.assertTrue((root / 'diagnostic/date-diagnostics.json').is_file())
            write_private(root / 'thresholds.json', dict(minimum_unique_urls=30, minimum_url_agreement=0.90,
                minimum_dateline_agreement=0.90, maximum_native_later_rate=0.10))
            from openweb_news.date_diagnostics import CALENDAR_SCREENING_PROFILE
            code, _, error = self.invoke(*args, '--thresholds', str(root / 'thresholds.json'),
                '--screening-profile', CALENDAR_SCREENING_PROFILE, '--output', str(root / 'calendar-diagnostic'))
            self.assertEqual(code, 0, error)
            self.assertEqual(load(root / 'calendar-diagnostic/date-diagnostics.json')['screening_profile'], CALENDAR_SCREENING_PROFILE)
            self.assertEqual(before, {p: p.read_bytes() for p in before})
            code, _, _ = self.invoke(*args, '--output', str(root / 'run/compiled/diagnostic'))
            self.assertEqual(code, 1)
            self.assertFalse((root / 'run/compiled/diagnostic').exists())
            self.assertEqual(before, {p: p.read_bytes() for p in before})
            report = load(root / 'run/report.json')
            report['package_sha256'] = 'wrong-source-package'
            write_private(root / 'mismatched-report.json', report)
            bad_args = list(args)
            bad_args[-1] = str(root / 'mismatched-report.json')
            code, _, _ = self.invoke(*bad_args, '--output', str(root / 'bad-diagnostic'))
            self.assertEqual(code, 1)
            self.assertFalse((root / 'bad-diagnostic/date-diagnostics.json').exists())

    def test_offline_date_preparation_is_undecided_and_method_template_unapproved(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            cfg = fixtures.config(root / 'run')
            write_private(root / 'config.json', cfg)
            write_private(root / 'diagnostic.json', {'synthetic': True})
            write_private(root / 'source.txt', b'Synthetic documented source.\n')
            code, _, _ = self.invoke('prepare-date-capabilities', '--config', str(root / 'config.json'), '--output', str(root / 'capabilities'))
            self.assertEqual(code, 0)
            self.assertFalse(load(root / 'capabilities/date-capabilities.json')['live_execution_authorized'])
            code, _, _ = self.invoke('prepare-date-trust', '--config', str(root / 'config.json'),
                '--diagnostic', str(root / 'diagnostic.json'), '--source', str(root / 'source.txt'), '--output', str(root / 'trust'))
            self.assertEqual(code, 0)
            approval = load(root / 'trust/approval-template.json')
            self.assertFalse(approval['approved'])
            self.assertTrue(all(choice is None for choice in approval['selections'].values()))
            # Only synthetic test-owner choices are recorded in this temporary fixture.
            approval.update(approved=True, approved_by='synthetic owner', approved_at='fixture',
                selections={cid: dict(eligible_fields=[], reason='Synthetic explicit exclusion') for cid in cfg['provider_ids']})
            write_private(root / 'trust/approval.json', approval)
            from openweb_news.blind_input import LIVE_PROFILE
            cfg.update(judge_input_profile=LIVE_PROFILE, date_trust_package=str(root / 'trust/package.json'))
            write_private(root / 'method-config.json', cfg)
            code, _, _ = self.invoke('prepare-method', '--config', str(root / 'method-config.json'), '--output', str(root / 'method'))
            self.assertEqual(code, 0)
            self.assertFalse(load(root / 'method/approval-template.json')['approved'])
            self.assertNotIn('source_report_path', load(root / 'method/package.json'))

    def test_status_and_presence_do_not_run_and_verification_failures_exit_nonzero(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            package = prepare(fixtures.config(root / 'run'), mode='fixture')
            write_private(root / 'package.json', package)
            with patch('openweb_news.runner.run', side_effect=AssertionError('no dispatch')):
                code, output, _ = self.invoke('status', '--package', str(root / 'package.json'))
                self.assertEqual(code, 0)
                self.assertIn('prepared', output)
                secret = 'synthetic-value-must-not-be-printed'
                with patch.dict('os.environ', {'OPENWEB_NEWS_EXA_API_KEY': secret}, clear=True):
                    code, output, error = self.invoke('check-credentials', '--package', str(root / 'package.json'))
                self.assertEqual(code, 1)
                self.assertNotIn(secret, output + error)
            with patch('openweb_news.operator.verify', return_value=dict(verified=False, package_verified=True, ledger_classification={'state': 'unclassified'})):
                code, _, _ = self.invoke('verify', '--package', str(root / 'package.json'))
                self.assertEqual(code, 1)
            self.assertFalse((root / 'run').exists())

    def test_record_existing_exact_approval_has_no_dispatch_or_new_permission_step(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            package = prepare(fixtures.config(root / 'run'))
            write_private(root / 'package.json', package)
            receipt = dict(decision='approved', package_sha256=digest(package), approved_by='synthetic owner', approved_at='fixture')
            write_private(root / 'supplied-receipt.json', receipt)
            with patch('openweb_news.runner.run', side_effect=AssertionError('no dispatch')):
                for _ in range(2):
                    code, _, _ = self.invoke('record-approval', '--package', str(root / 'package.json'), '--receipt', str(root / 'supplied-receipt.json'))
                    self.assertEqual(code, 0)
            self.assertEqual(load(root / 'approval.json'), receipt)
            self.assertFalse((root / 'run').exists())


if __name__ == '__main__':
    unittest.main()
