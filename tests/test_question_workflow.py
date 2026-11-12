"""Plain-question cold start and refusal/recovery of real pipeline boundaries."""
from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from openweb_news import clean_run, fixtures, question_workflow, question_bank, review_capacity
from openweb_news.cli import main
from openweb_news.common import digest, load, records, write_private


class QuestionWorkflowTests(unittest.TestCase):
    def cli(self, *args, success=True):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main([str(a) for a in args])
        self.assertEqual(code, 0 if success else 1, err.getvalue() + out.getvalue())
        return json.loads(out.getvalue()) if code == 0 else err.getvalue()

    def prepare(self, root, text=None, *, publication_control=False, maximum_calls=None):
        inputs = root / 'inputs'
        self.cli('starter', '--simple', '--output', inputs)
        self.assertFalse((inputs / 'questions.jsonl').exists())
        if text is not None:
            write_private(inputs / 'simple-questions.txt', text.encode(), replace=True)
        if maximum_calls is not None:
            cfg = load(inputs / 'config.json')
            cfg['review_capacity']['max_checkpoint_calls'] = maximum_calls
            write_private(inputs / 'config.json', cfg, replace=True)
        if publication_control:
            from openweb_news.recovery import sibling_rule
            cfg = load(inputs / 'config.json')
            cfg.update(provider_ids=['valyu-search', 'exa-highlights'],
                tie_order=['valyu-search', 'exa-highlights'], provider_date_profiles={})
            scheduling = cfg['execution_policy']['scheduling']
            for name in ('accounts', 'recovery'):
                scheduling[name].pop('provider/OPENWEB_NEWS_BRAVE_API_KEY')
            scheduling['accounts']['provider/OPENWEB_NEWS_VALYU_API_KEY'] = dict(concurrency=2, minimum_start_interval_seconds=0)
            scheduling['recovery']['provider/OPENWEB_NEWS_VALYU_API_KEY'] = sibling_rule('provider', 2)
            write_private(inputs / 'config.json', cfg, replace=True)
        self.cli('prepare-date-trust', '--config', inputs / 'config.json', '--no-date-diagnostics',
            '--source', inputs / 'source-notice.txt', '--output', inputs / 'trust')
        trust = load(inputs / 'trust/approval-template.json')
        trust.update(approved=True, approved_by='SYNTHETIC OWNER', approved_at='fixture',
            selections={cid:dict(eligible_fields=['publication_date'] if publication_control and cid == 'valyu-search' else [],
                                reason='Synthetic explicit field decision.')
                        for cid in trust['selections']})
        write_private(inputs / 'trust/approval.json', trust)
        self.cli('prepare-method', '--mode', 'fixture', '--config', inputs / 'config.json', '--output', inputs / 'method')
        method = load(inputs / 'method/approval-template.json')
        method.update(approved=True, accept_method=True, approved_by='SYNTHETIC OWNER', approved_at='fixture')
        write_private(inputs / 'method/approval.json', method)
        count = len([s for s in (inputs / 'simple-questions.txt').read_text().splitlines() if s.strip()])
        counts = [2, 2] if count == 4 else [count]
        self.cli('prepare-question-workflow', '--questions', inputs / 'simple-questions.txt',
            '--config', inputs / 'config.json', '--mode', 'fixture', '--origin', 'expert',
            '--stage-counts', *counts, '--output', root / 'workflow')
        path = root / 'workflow/workflow.json'
        receipt = load(path.parent / 'approval-template.json')
        receipt.update(decision='approved', accept_derived_execution=True,
            approved_by='SYNTHETIC OWNER', approved_at='fixture')
        write_private(root / 'owner.json', receipt)
        return path, receipt

    def test_plain_text_to_closed_report_via_public_cli_and_no_send_resume(self):
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            path, approval = self.prepare(root, publication_control=True)
            original = load(path)['source_questions'].encode()
            result = self.cli('question-workflow', '--package', path, '--approval', root / 'owner.json')
            self.assertEqual(result['state'], 'completed')
            self.assertEqual(result['questions'], 4)
            self.assertEqual((path.parent / 'source-questions.txt').read_bytes(), original)
            self.assertTrue(Path(result['report_html']).is_file())
            report = load(result['report']['path'])
            self.assertEqual(len(report['questions']), 4)
            self.assertEqual(report['archive_comparison'], clean_run.FIRST_RUN)
            self.assertEqual([q['original_need'] for q in report['questions']], original.decode().splitlines())
            self.assertTrue(all(q['review']['state'] == 'reviewed' for q in report['questions']))
            checks = clean_run.verify_stage_gates(path.parent / 'clean/clean-run.json')
            self.assertEqual([r['review_decision'] for r in checks], ['continue', 'continue'])
            plan = load(path.parent / 'agents/checkpoint-2/plan.json')
            payload = load(path.parent / 'agents/checkpoint-2/source.json')
            prior_source = load(path.parent / 'agents/checkpoint-1/source.json')
            self.assertEqual(review_capacity.restore(plan, prior_source), payload)
            self.assertGreater(len(plan['parts']), 1)
            self.assertGreater(len(plan['reused']), 0)
            for index in (1, 2):
                current_plan = load(path.parent / f'agents/checkpoint-{index}/plan.json')
                for part_index, part in enumerate(current_plan['parts'], 1):
                    job = load(next((path.parent / f'agents/checkpoint-{index}-part-{part_index:04d}/attempts').glob('*/request.json')))
                    self.assertEqual(job['blind_packet'], part['packet'])
                    self.assertTrue(part['capacity']['fits'])
            def expand(value):
                if isinstance(value, dict):
                    if set(value) == {'shared_document_sha256'}:
                        return payload['shared_documents'][value['shared_document_sha256']]
                    return {k:expand(v) for k,v in value.items()}
                return [expand(v) for v in value] if isinstance(value, list) else value
            report_cases = clean_run._case_refs([path.parent / f'clean/stage-{i}/review/package.json'
                for i in (1, 2)], report['outcomes'])
            self.assertEqual(expand(payload['cases']), [load(report_cases[cid]['path']) for cid in sorted(report_cases)])
            self.assertTrue(all(r['kind'] == 'search' for r in payload['collection_requests']))
            expected_reviews = []
            for i in (1, 2):
                for relative in (f'clean/stage-{i}/review/run', f'clean/stage-{i}-accepted-review/publication/run'):
                    directory = path.parent / relative
                    events = list(records(directory / 'attempts.jsonl')) if (directory / 'attempts.jsonl').exists() else []
                    finishes = {e['attempt_id']:e for e in events if e['event'] == 'finished'}
                    for event in events:
                        if event['event'] == 'started':
                            expected_reviews.append(dict(request=load(directory / 'attempts' / event['attempt_id'] / 'request.json'),
                                response=load(directory / finishes[event['attempt_id']]['result_path'])))
            self.assertEqual(expand(payload['review_attempts']), expected_reviews)
            self.assertGreater(len(load(path.parent / 'clean/stage-1/review/package.json')['jobs']), 0)
            self.assertGreater(len(load(path.parent / 'clean/stage-1-accepted-review/publication/package.json')['jobs']), 0)
            self.assertGreater(sum(o['grade'] is None for o in report['outcomes']), 0)
            ledgers = sorted(path.parent.rglob('attempts.jsonl'))
            before = {str(file):file.read_bytes() for file in ledgers}
            with patch.object(fixtures, 'workflow', side_effect=AssertionError('completed task repeated')), \
                    patch.object(fixtures, 'search', side_effect=AssertionError('completed search repeated')), \
                    patch.object(fixtures, 'model', side_effect=AssertionError('completed model repeated')):
                self.assertEqual(self.cli('question-workflow', '--package', path,
                    '--approval', root / 'owner.json'), result)
            self.assertEqual(before, {str(file):file.read_bytes() for file in ledgers})
            agents = list((path.parent / 'agents').glob('*/attempts.jsonl'))
            expected_calls = 2 + sum(len(load(path.parent / f'agents/checkpoint-{i}/plan.json')['parts']) for i in (1, 2))
            self.assertEqual(sum(sum(e['event'] == 'started' for e in records(file)) for file in agents), expected_calls)

    def test_missing_approval_and_fixture_injection_into_live_are_refused_before_dispatch(self):
        with TemporaryDirectory() as temp:
            path, approval = self.prepare(Path(temp).resolve())
            for change in (dict(decision='NOT_APPROVED'), dict(package_sha256='changed'),
                           dict(accept_derived_execution=False)):
                with self.assertRaisesRegex(ValueError, 'exact workflow approval'):
                    question_workflow.run(path, dict(approval, **change))
            self.assertFalse((path.parent / 'agents').exists())
            p = load(path)
            p['mode'] = 'live'
            # A mode change cannot reuse even the independently accepted fixture method.
            write_private(path, p, replace=True)
            with self.assertRaises(ValueError):
                question_workflow.run(path, approval, fixture_call=fixtures.workflow)
            # Reach the transport-injection guard independently of the mode check.
            with patch.object(question_workflow, 'verify', return_value=p), \
                    self.assertRaisesRegex(ValueError, 'live workflow cannot inject'):
                question_workflow.run(path, dict(approval, package_sha256=digest(p)), fixture_call=fixtures.workflow)

    def test_unresolved_question_preserves_source_and_blocks_all_collection(self):
        with TemporaryDirectory() as temp:
            path, approval = self.prepare(Path(temp).resolve(), 'Find the latest ambiguous release.\n')
            result = question_workflow.run(path, approval)
            self.assertEqual(result['state'], 'blocked')
            self.assertIn('unresolved', result['reason'])
            self.assertFalse((path.parent / 'bank').exists())
            self.assertFalse((path.parent / 'clean').exists())
            self.assertEqual((path.parent / 'source-questions.txt').read_text(), load(path)['source_questions'])

    def test_bad_membership_and_negative_semantic_review_cannot_become_admission(self):
        for kind in ('omitted', 'original_changed', 'rejected', 'invalid_schema'):
            with self.subTest(kind=kind), TemporaryDirectory() as temp:
                path, approval = self.prepare(Path(temp).resolve(), 'Find editorial reports about a fictional opening.\n')
                def call(job, folder):
                    raw = fixtures.workflow(job, folder)
                    if kind == 'omitted' and job['stage'] == 'author':
                        raw['verdict']['questions'] = []
                    if kind == 'original_changed' and job['stage'] == 'author':
                        raw['verdict']['questions'][0]['original_need'] = 'Rewritten original'
                    if kind == 'rejected' and job['stage'] == 'admission':
                        raw['verdict']['decisions'][0]['decision'] = 'reject'
                    if kind == 'invalid_schema' and job['stage'] == 'admission':
                        raw['verdict'] = {}
                    return raw
                result = question_workflow.run(path, approval, fixture_call=call)
                self.assertEqual(result['state'], 'blocked')
                self.assertFalse((path.parent / 'clean').exists())

    def test_negative_checkpoint_stops_before_next_stage_and_is_retained(self):
        with TemporaryDirectory() as temp:
            path, approval = self.prepare(Path(temp).resolve())
            def call(job, folder):
                raw = fixtures.workflow(job, folder)
                if job['stage'].startswith('checkpoint-1-part-'):
                    raw['verdict'].update(decision='stop', invariant_failures=['Synthetic review stop.'])
                return raw
            result = question_workflow.run(path, approval, fixture_call=call)
            self.assertEqual(result['state'], 'stopped_after_review')
            self.assertFalse((path.parent / 'clean/stage-2/native').exists())
            review = load(path.parent / 'clean/stage-1/stage-review.json')
            self.assertEqual(review['decision'], 'stop')
            self.assertEqual(review['invariant_failures'], ['Synthetic review stop.'])
            review.update(decision='continue', invariant_failures=[])
            write_private(path.parent / 'clean/stage-1/stage-review.json', review, replace=True)
            with self.assertRaisesRegex(ValueError, 'workflow derived bytes changed'):
                question_workflow.run(path, approval)
            self.assertFalse((path.parent / 'clean/stage-2/native').exists())

    def test_interrupted_agent_is_held_without_resend(self):
        with TemporaryDirectory() as temp:
            path, approval = self.prepare(Path(temp).resolve(), 'Find fictional editorial opening reports.\n')
            def interrupted(job, folder):
                raise KeyboardInterrupt('synthetic interruption after durable start')
            with self.assertRaises(KeyboardInterrupt):
                question_workflow.run(path, approval, fixture_call=interrupted)
            with patch.object(fixtures, 'workflow', side_effect=AssertionError('uncertain task repeated')):
                result = question_workflow.run(path, approval)
            self.assertEqual(result['state'], 'blocked')
            self.assertIn('uncertain attempt', result['reason'])
            self.assertFalse((path.parent / 'clean').exists())

    def test_partial_bank_freeze_finishes_without_changing_existing_bytes(self):
        with TemporaryDirectory() as temp:
            path, approval = self.prepare(Path(temp).resolve(), 'Find fictional editorial opening reports.\n')
            def interrupted(source, directory, *args, **kwargs):
                write_private(Path(directory) / 'bank.jsonl', Path(source).read_bytes())
                raise KeyboardInterrupt('synthetic partial freeze')
            with patch.object(question_bank, 'freeze', side_effect=interrupted), self.assertRaises(KeyboardInterrupt):
                question_workflow.run(path, approval)
            before = (path.parent / 'bank/bank.jsonl').read_bytes()
            result = question_workflow.run(path, approval)
            self.assertEqual(result['state'], 'completed')
            self.assertEqual(before, (path.parent / 'bank/bank.jsonl').read_bytes())
            write_private(path.parent / 'bank/bank.jsonl', b'changed', replace=True)
            with self.assertRaisesRegex(ValueError, 'frozen bank bytes changed'):
                question_workflow.run(path, approval)

    def test_capacity_refuses_oversized_admission_before_auth_or_collection(self):
        with TemporaryDirectory() as temp:
            path, approval = self.prepare(Path(temp).resolve(), 'Find fictional editorial opening reports.\n')
            calls = []
            def large_author(job, folder):
                calls.append(job['stage'])
                raw = fixtures.workflow(job, folder)
                self.assertEqual(job['stage'], 'author')
                raw['verdict']['questions'][0]['contract']['qualifying_predicates'] = ['Synthetic original. ' * 50000]
                return raw
            with patch('openweb_news.review_pipeline.backends.subscription', side_effect=AssertionError('auth/transport called')):
                result = question_workflow.run(path, approval, fixture_call=large_author)
            self.assertEqual(result['state'], 'blocked')
            self.assertIn('capacity budget exceeded', result['reason'])
            self.assertEqual(calls, ['author'])
            self.assertFalse((path.parent / 'agents/admission/attempts.jsonl').exists())
            self.assertTrue((path.parent / 'agents/admission/blocked-packet.json').exists())
            self.assertFalse((path.parent / 'clean').exists())

    def test_call_bound_blocks_complete_checkpoint_plan_without_any_part_send(self):
        with TemporaryDirectory() as temp:
            path, approval = self.prepare(Path(temp).resolve(), publication_control=True, maximum_calls=1)
            calls = []
            def call(job, folder):
                calls.append(job['stage'])
                return fixtures.workflow(job, folder)
            result = question_workflow.run(path, approval, fixture_call=call)
            self.assertEqual(result['state'], 'blocked')
            self.assertIn('call bound', result['reason'])
            self.assertFalse(any(s.startswith('checkpoint-') for s in calls))
            self.assertFalse((path.parent / 'clean/stage-2/native').exists())
            self.assertTrue((path.parent / 'agents/checkpoint-1/source.json').exists())

    def test_missing_part_coverage_cannot_become_a_passing_checkpoint(self):
        with TemporaryDirectory() as temp:
            path, approval = self.prepare(Path(temp).resolve())
            def call(job, folder):
                raw = fixtures.workflow(job, folder)
                if job['stage'].startswith('checkpoint-'):
                    raw['verdict']['reviewed_unit_ids'] = []
                return raw
            result = question_workflow.run(path, approval, fixture_call=call)
            self.assertEqual(result['state'], 'blocked')
            self.assertIn('incomplete evidence coverage', result['reason'])
            self.assertFalse((path.parent / 'clean/stage-1/stage-review.json').exists())
            self.assertFalse((path.parent / 'clean/stage-2/native').exists())

    def test_interrupted_later_part_preserves_completed_parts_without_resend(self):
        with TemporaryDirectory() as temp:
            path, approval = self.prepare(Path(temp).resolve(), publication_control=True)
            def interrupted(job, folder):
                if job['stage'].startswith('checkpoint-') and job['stage'].endswith('part-0002'):
                    raise KeyboardInterrupt('Synthetic later-part interruption after durable start')
                return fixtures.workflow(job, folder)
            with self.assertRaises(KeyboardInterrupt):
                question_workflow.run(path, approval, fixture_call=interrupted)
            before = {str(f):f.read_bytes() for f in (path.parent / 'agents').glob('*/attempts.jsonl')}
            with patch.object(fixtures, 'workflow', side_effect=AssertionError('prior/uncertain part repeated')):
                result = question_workflow.run(path, approval)
            self.assertEqual(result['state'], 'blocked')
            self.assertIn('uncertain attempt', result['reason'])
            self.assertEqual(before, {str(f):f.read_bytes() for f in (path.parent / 'agents').glob('*/attempts.jsonl')})


if __name__ == '__main__':
    unittest.main()
