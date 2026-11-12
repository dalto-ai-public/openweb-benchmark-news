"""Public CLI cold start. Only transport faults and external fixture decisions are supplied."""
from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest

from openweb_news import fixtures, clean_run, request_profiles
from openweb_news.cli import main
from openweb_news.common import digest, load, records, write_private


class FirstRunCliTests(unittest.TestCase):
    def command(self, *args, success=True):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main([str(a) for a in args])
        self.assertEqual(code, 0 if success else 1, err.getvalue()+out.getvalue())
        return json.loads(out.getvalue()) if code == 0 else err.getvalue()

    def prepare(self, root, *, automatic=False):
        inputs = root/'inputs'
        self.command('starter', '--output', inputs)
        if automatic:
            from openweb_news.resilience import POLICY
            cfg = load(inputs/'config.json')
            cfg['automatic_recovery'] = deepcopy(POLICY)
            policy = cfg['execution_policy']
            policy['max_attempts_per_task'] = 3
            policy['retry_policy'].update(max_total=None, max_search=None, max_model=None)
            for account in policy['scheduling']['accounts'].values():
                account['concurrency'] = 1
            from openweb_news.recovery import sibling_rule
            policy['scheduling']['recovery'] = {k:sibling_rule('provider' if k.startswith('provider/') else 'judge',v['concurrency']) for k,v in policy['scheduling']['accounts'].items()}
            for rule in policy['scheduling']['recovery'].values():
                rule['cooldown_seconds'] = 0
            write_private(inputs/'config.json', cfg, replace=True)
        source = (inputs/'questions.jsonl').read_bytes()
        self.command('validate-bank', '--questions', inputs/'questions.jsonl')
        self.command('freeze-bank', '--questions', inputs/'questions.jsonl', '--phase-counts', 2, 2, '--output', root/'bank')
        self.assertEqual((root/'bank/bank.jsonl').read_bytes(), source)
        self.command('prepare-date-capabilities', '--config', inputs/'config.json', '--output', inputs/'capabilities')
        self.command('prepare-date-trust', '--config', inputs/'config.json', '--no-date-diagnostics',
            '--source', inputs/'source-notice.txt', '--output', inputs/'trust')
        trust = load(inputs/'trust/approval-template.json')
        self.assertFalse(trust['approved'])
        trust.update(approved=True, approved_by='SYNTHETIC TEST OWNER ONLY', approved_at='fixture',
            selections={cid: dict(eligible_fields=[], reason='Fixture-only explicit exclusion; no diagnostics available')
                        for cid in trust['selections']})
        write_private(inputs/'trust/approval.json', trust)
        self.command('prepare-method', '--mode', 'fixture', '--config', inputs/'config.json', '--output', inputs/'method')
        method = load(inputs/'method/approval-template.json')
        self.assertFalse(method['approved'])
        method.update(approved=True, accept_method=True, approved_by='SYNTHETIC TEST OWNER ONLY', approved_at='fixture')
        write_private(inputs/'method/approval.json', method)
        self.command('prepare', '--mode', 'fixture', '--config', inputs/'config.json', '--output', root/'template.json')
        self.command('prepare-clean-run', '--mode', 'fixture', '--template-package', root/'template.json',
            '--selection', root/'bank/selection.json', '--accepted-method', inputs/'method/package.json',
            '--ceiling', 100, '--stage-counts', 2, 2, '--output', root/'clean')
        package = root/'clean/clean-run.json'
        self.assertIsNone(load(package)['archived_reference'])
        self.assertEqual(load(package)['archive_comparison'], clean_run.FIRST_RUN)
        owner = load(root/'clean/approval-template.json')
        write_private(root/'not-approved.json', owner)
        self.command('clean-run', '--package', package, '--approval', root/'not-approved.json', success=False)
        owner.update(decision='approved', approved_by='SYNTHETIC TEST OWNER ONLY', approved_at='fixture')
        write_private(root/'owner.json', owner)
        self.command('record-approval', '--package', package, '--receipt', root/'owner.json')
        return package, inputs/'fixture-review-response.json'

    def execute(self, root, package, response):
        return self.command('clean-run', '--package', package, '--approval', root/'owner.json', '--fixture-response', response)

    def review(self, root, package, result, **changes):
        receipt = dict(stage_index=result['stage_index'], decision='continue', evidence=result['evidence'],
            reviewed_by='SYNTHETIC INDEPENDENT REVIEWER ONLY', reviewed_at='fixture', material_method_change=False,
            unexplained_material_differences=[], invariant_failures=[],
            inspection={k:'Fixture evidence inspected; no live validation' for k in
                ('model_inputs','date_source_and_quotes','changed_and_unchanged_cases','ranking_movements')})
        receipt.update(changes)
        write_private(root/'review-input.json', receipt, replace=(root/'review-input.json').exists())
        return self.command('record-stage-review', '--package', package, '--receipt', root/'review-input.json',
            success=not bool(changes))

    def test_public_first_run_to_report_keeps_absent_archive_and_external_gates(self):
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            package, response = self.prepare(root)
            self.assertFalse(any(root.glob('**/archive*')))
            result = self.execute(root, package, response)
            self.assertEqual(result['state'], 'awaiting_stage_review')
            self.assertEqual(result['stage_index'], 1)
            diagnostic = load(result['evidence']['comparison']['path'])
            self.assertEqual(diagnostic['state'], 'unavailable')
            self.assertIsNone(diagnostic['sources']['archived'])
            report = load(result['evidence']['report']['path'])
            self.assertIn('Fixture-only conservative-rate simulation', Path(result['report']).read_text())
            self.assertEqual(report['archive_comparison'], clean_run.FIRST_RUN)
            self.assertIn('Archive comparison unavailable', Path(result['report']).read_text())
            status = self.command('status', '--package', package)
            self.assertEqual(status['archive_comparison'], clean_run.FIRST_RUN)
            self.assertTrue(self.command('verify', '--package', package)['verified'])
            before = {p:p.read_bytes() for p in (root/'clean/stage-1').rglob('attempts.jsonl')}
            self.assertEqual(self.execute(root, package, response), result)
            self.assertFalse((root/'clean/stage-2/native/package.json').exists())
            self.review(root, package, result, evidence={})
            self.review(root, package, result, invariant_failures=['synthetic failure'])
            self.review(root, package, result)
            result = self.execute(root, package, response)
            self.assertEqual(result['stage_index'], 2)
            final = load(result['evidence']['report']['path'])
            self.assertEqual(len(final['questions']), 4)
            self.assertEqual(len({q['family_id'] for q in final['questions']}), 1)
            self.assertTrue(any(o['grade'] is None for o in final['outcomes']))
            self.assertEqual(final['archive_comparison'], clean_run.FIRST_RUN)
            self.assertEqual(before, {p:p.read_bytes() for p in before})
            native = load(root/'clean/stage-1/native/package.json')
            self.assertEqual(request_profiles.audit_dispatch(native, root/'clean/stage-1/native/run')['verified_attempts'], 1)
            self.review(root, package, result)
            completed = self.execute(root, package, response)
            self.assertEqual(completed['state'], 'completed')
            self.assertTrue(self.command('verify', '--package', package)['verified'])
            status = self.command('status', '--package', package)
            self.assertEqual(status['state'], 'completed')
            self.assertEqual(status['costs']['shared_committed_and_held_usd'], final['phase_costs']['conservative_total_usd'])
            from decimal import Decimal
            self.assertGreater(Decimal(final['phase_costs']['conservative_total_usd']), 0)
            with patch.object(fixtures, 'search', side_effect=AssertionError('completed search repeated')), \
                    patch.object(fixtures, 'model', side_effect=AssertionError('completed judge repeated')):
                self.assertEqual(self.execute(root, package, response), completed)

    def test_fixture_receipt_cannot_prepare_live_template_and_no_archive_declaration_is_bound(self):
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            package, response = self.prepare(root)
            self.command('prepare', '--config', root/'inputs/config.json', '--output', root/'live.json', success=False)
            cfg = load(root/'inputs/config.json'); cfg.pop('fixture_accounting')
            write_private(root/'without-fixture-accounting.json', cfg)
            self.command('prepare', '--config', root/'without-fixture-accounting.json', '--output', root/'live.json', success=False)
            value = load(package); value.pop('archive_comparison')
            write_private(package, value, replace=True)
            self.command('verify', '--package', package, success=False)

    def test_process_interruption_resumes_without_repeating_successes_and_accounts_once(self):
        from decimal import Decimal
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            baseline = root/'baseline'; resumed = root/'resumed'
            p, response = self.prepare(baseline, automatic=True)
            result = self.execute(baseline, p, response)
            self.review(baseline, p, result)
            result = self.execute(baseline, p, response)
            normal = load(result['evidence']['report']['path'])
            p, response = self.prepare(resumed, automatic=True)
            original = fixtures.model
            interrupted = []
            def stop(role, compiled, folder):
                if role == 'pro' and not interrupted:
                    interrupted.append(role+'-'+compiled['packet']['case_id'])
                    raise KeyboardInterrupt('synthetic process reset after durable start')
                return original(role, compiled, folder)
            with patch.object(fixtures, 'model', side_effect=stop), self.assertRaises(KeyboardInterrupt):
                self.execute(resumed, p, response)
            ledger = resumed/'clean/stage-1/native/run/attempts.jsonl'
            success = {e['task_id'] for e in records(ledger) if e['event']=='finished'
                       and load(ledger.parent/e['result_path']).get('transport_success')}
            def model(role, compiled, folder):
                self.assertNotIn(role+'-'+compiled['packet']['case_id'], success)
                return original(role, compiled, folder)
            original_search = fixtures.search
            def search(request, provider, folder):
                task = 'search-'+digest(dict(configuration=request['configuration_id'], question=request['question_id']))
                self.assertNotIn(task, success)
                return original_search(request, provider, folder)
            with patch.object(fixtures, 'model', side_effect=model), patch.object(fixtures, 'search', side_effect=search):
                result = self.execute(resumed, p, response)
            self.assertEqual(result['state'], 'awaiting_stage_review')
            events = list(records(ledger))
            uncertain = [e for e in events if e['event']=='finished' and
                         load(ledger.parent/e['result_path']).get('dispatch_state')=='uncertain']
            self.assertEqual(len(uncertain), 1)
            task = interrupted[0]
            self.assertEqual(sum(e['event']=='started' and e['task_id']==task for e in events), 2)
            self.review(resumed, p, result)
            result = self.execute(resumed, p, response)
            recovered = load(result['evidence']['report']['path'])
            # Exact method/trust receipt paths differ between independent test
            # roots, so occurrence IDs differ. Compare every score value after
            # replacing only those provenance IDs with scientific coordinates.
            def normalized(value, report):
                ids = {o['case_id']:str((o['configuration_id'],o['question_id'],o['rank'])) for o in report['outcomes']}
                def convert(v):
                    if isinstance(v, str): return ids.get(v, v)
                    if isinstance(v, list): return [convert(x) for x in v]
                    if isinstance(v, dict): return {k:convert(x) for k,x in v.items()}
                    return v
                return convert(value)
            self.assertEqual(normalized(recovered['scores'], recovered), normalized(normal['scores'], normal))
            if 'resolved_pairwise' in normal:
                self.assertEqual(normalized(recovered['resolved_pairwise'], recovered), normalized(normal['resolved_pairwise'], normal))
            scientific = lambda r: sorted((o['question_id'],o['configuration_id'],o['rank'],o['grade'],o['reason']) for o in r['outcomes'])
            self.assertEqual(scientific(recovered), scientific(normal))
            hold = load(ledger.parent/'attempts'/uncertain[0]['attempt_id']/'request.json')['reservation_usd']
            self.assertGreater(Decimal(hold), 0)
            self.assertEqual(Decimal(recovered['phase_costs']['conservative_total_usd'])-
                Decimal(normal['phase_costs']['conservative_total_usd']), Decimal(hold))
            before = ledger.read_bytes()
            self.assertEqual(self.execute(resumed, p, response), result)
            self.assertEqual(ledger.read_bytes(), before)
            status = self.command('status', '--package', p)
            self.assertEqual(status['costs']['shared_committed_and_held_usd'], recovered['phase_costs']['conservative_total_usd'])

    def test_no_archive_lean_continuation_keeps_report_disclosure(self):
        # A separate continuation regression, not part of cold-start proof.
        from .test_lean_checkpoints import LeanCheckpointTests
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            helper, source, _, _ = LeanCheckpointTests().setup_prefix(root)
            target = clean_run.prepare(root/'template.json', source['selection_path'], root/'new',
                ceiling=source['shared_ceiling_usd'], accepted_profile=source['accepted_profile']['package_path'],
                stage_counts=(1,), mode='fixture', verified_prefix=source['verified_seed'])
            owner = dict(decision='approved', package_sha256=digest(target), approved_by='SYNTHETIC OWNER', approved_at='fixture')
            result = clean_run.run(root/'new/clean-run.json', owner)
            report = load(result['evidence']['report']['path'])
            self.assertEqual(report['archive_comparison'], clean_run.FIRST_RUN)
            self.assertIn('Archive comparison unavailable', Path(result['report']).read_text())
            self.assertEqual(load(result['evidence']['comparison']['path'])['state'], 'unavailable')
            self.assertEqual(clean_run.verify(root/'new/clean-run.json')[0], target)
