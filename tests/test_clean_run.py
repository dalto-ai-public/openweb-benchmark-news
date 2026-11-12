from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest

from openweb_news import clean_run, fixtures, profile_authority
from openweb_news.common import digest, encoded, load, records, write_private
from openweb_news.preparation import prepare
from openweb_news.runner import run
from . import test_checkpoint
from .test_independent_method import accepted_fixture


class CleanRunTests(unittest.TestCase):
    def test_recollect_first_provider_cells_retains_other_outcomes_costs_and_full_next_stage(self):
        from openweb_news import settlement
        from openweb_news.accounting import report_costs
        from openweb_news.request_profiles import MIXED_START_PROFILE
        with TemporaryDirectory()as temp:
            root=Path(temp).resolve()
            cfg,selection,reference,_,order=self.setup_run(root,counts=(1,1),provider_ids=['brave-web','exa-highlights'])
            cfg['date_reading_profile']='quoted-date-reading-v4'
            cfg,_=accepted_fixture(root/'old-authority',cfg)
            write_private(root/'old-template.json',prepare(cfg,mode='fixture'))
            old=clean_run.prepare(root/'old-template.json',selection,root/'old',ceiling='100',
                accepted_profile=cfg['method_acceptance_package'],archived_reference=reference,stage_counts=(1,),mode='fixture')
            old_approval=dict(decision='approved',package_sha256=digest(old),approved_by='synthetic owner',approved_at='fixture')
            first=clean_run.run(root/'old/clean-run.json',old_approval)
            source_path=Path(first['evidence']['report']['path'])
            source,cases,source_native=settlement.load_parent(source_path)
            source.setdefault('publication_review',dict(profile='synthetic-publication',records=[]))
            cfg['provider_date_profiles']={'brave-web':MIXED_START_PROFILE}
            from openweb_news.accounting import FIXTURE_PROFILE
            cfg['fixture_accounting'] = FIXTURE_PROFILE
            target=profile_authority.prepare_method(cfg,root/'target-method',mode='fixture')
            write_private(root/'target-method/approval.json',dict(approved=True,accept_method=True,
                package_sha256=digest(target),approved_by='synthetic owner',approved_at='fixture'))
            cfg['method_acceptance_package']=str(root/'target-method/package.json')
            write_private(root/'target-template.json',prepare(cfg,mode='fixture'))
            authority=profile_authority.read(cfg['method_acceptance_package'],cfg['reviewers'])
            source['active_review_profile']=deepcopy(authority)
            retained=next(o for o in source['outcomes']if o['configuration_id']=='exa-highlights')
            retained['grade']=None
            from openweb_news import publication
            own=cases[retained['case_id']]['compiled']
            packet=publication.packet_for(own)
            source['publication_review']['records']=[dict(case_id=retained['case_id'],configuration_id='exa-highlights',
                terminal=True,accepted=False,grade=None,audits={},checker_profile=authority['profile_bindings']['checker_profile'],
                date_profile=authority['profile_bindings']['date_profile'],text_profile=authority['profile_bindings']['text_profile'],
                terminal_key=settlement.identity(own,cfg['reviewers'],authority=authority),
                provenance={role:dict(native_completion_verified=True,input_sha256=packet['input_sha256'])for role in ('codex','claude')})]
            source_ref=dict(profile='synthetic-reviewed-source',report=clean_run._bound_file(source_path))
            def recollection(ref,selected):
                if ref is None:return None
                self.assertEqual(ref,source_ref);self.assertEqual(selected,authority)
                return deepcopy(source),deepcopy(cases),deepcopy(source_native)
            # Source authentication/replay is tested separately. This control
            # runs actual collection, review, pooling, closures, operator
            # verification and two-stage resume with an isolated source loader.
            with patch('openweb_news.clean_run._recollection',side_effect=recollection),\
                    patch('openweb_news.publication.eligibility',return_value='synthetic-publication-route'):
                from openweb_news import date_report
                date_report._verify_retained_reviews(source,cases,authority)
                missing=deepcopy(source);missing['publication_review']['records'][0]['terminal']=False
                with self.assertRaisesRegex(ValueError,'require new review calls'):
                    date_report._verify_retained_reviews(missing,cases,authority)
                p=clean_run.prepare(root/'target-template.json',selection,root/'recollect',ceiling='100',
                    accepted_profile=cfg['method_acceptance_package'],archived_reference=reference,
                    stage_counts=(1,1),mode='fixture',recollection_source=source_ref)
                path=root/'recollect/clean-run.json'
                self.assertEqual(p['stages'][0]['native_prototype']['scope']['search_cells'],1)
                self.assertEqual(p['stages'][1]['native_prototype']['scope']['search_cells'],2)
                approval=dict(decision='approved',package_sha256=digest(p),approved_by='synthetic owner',approved_at='fixture')
                result=clean_run.run(path,approval)
                self.assertEqual(result['state'],'awaiting_stage_review')
                final=load(result['evidence']['report']['path'])
                self.assertIn('Fixture-only conservative-rate simulation', Path(result['report']).read_text())
                for component in ('native/run/report.html','review/run/report.html'):
                    rendered=(root/'recollect/stage-1'/component).read_text()
                    self.assertIn('Partial provider-cell component',rendered)
                    self.assertNotIn('Delivered utility',rendered)
                    self.assertNotIn('Provider comparison',rendered)
                    self.assertFalse((root/'recollect/stage-1'/component).with_name('pairwise.csv').exists())
                    partial=(root/'recollect/stage-1'/component).with_suffix('.json')
                    from openweb_news import comparison
                    with self.assertRaisesRegex(ValueError,'configuration roster differs'):
                        comparison.compare_reports(load(reference['report_path']),dict(load(partial),questions=[order[0]]))
                    with self.assertRaises(ValueError):
                        clean_run.seed_evidence(partial,reference,authority)
                self.assertEqual(load(root/'recollect/stage-1-accepted-review/review-checkpoint.json')['review_tasks'],0)
                kept=[o for o in source['outcomes']if o['configuration_id']=='exa-highlights']
                self.assertEqual([o for o in final['outcomes']if o['configuration_id']=='exa-highlights'],kept)
                self.assertEqual(len(final['collection']),2)
                stage_native=load(root/'recollect/stage-1/native/package.json')
                new_costs=report_costs(stage_native,Path(stage_native['configuration']['run_directory']))
                for key in clean_run.checkpoint.COST_KEYS:
                    self.assertEqual(Decimal(final['phase_costs'][key]),Decimal(source['phase_costs'][key])+Decimal(new_costs[key]))
                _,_,planning=settlement.load_parent(result['evidence']['report']['path'])
                self.assertNotIn('search_provider_ids',planning['configuration'])
                original_loader=settlement.load_parent
                final_path=Path(result['evidence']['report']['path'])
                def changed_retained(path,**kwargs):
                    evidence=original_loader(path,**kwargs)
                    if Path(path)==final_path:
                        changed=deepcopy(evidence[0])
                        next(o for o in changed['outcomes']if o['configuration_id']=='exa-highlights')['grade']=2
                        return changed,evidence[1],evidence[2]
                    return evidence
                with patch('openweb_news.settlement.load_parent',side_effect=changed_retained):
                    with self.assertRaisesRegex(ValueError,'changed an unchanged-provider outcome'):
                        clean_run._stage_evidence(root/'recollect/stage-1',p,load(root/'recollect/stage-1/checkpoint.json'),write=False)
                from openweb_news import operator
                self.assertTrue(operator.verify(path)['verified'])
                status=operator.status(path)
                self.assertEqual(Decimal(status['costs']['shared_committed_and_held_usd']),Decimal(final['phase_costs']['conservative_total_usd']))
                with patch('openweb_news.fixtures.search',side_effect=AssertionError('completed search repeated')):
                    self.assertEqual(clean_run.run(path,approval),result)
                self.review(path,result)
                second=clean_run.run(path,approval)
                self.assertEqual(second['stage_index'],2)
                second_native=load(root/'recollect/stage-2/native/package.json')
                self.assertEqual(len(second_native['search_requests']),2)
                self.assertNotIn('search_provider_ids',second_native['configuration'])

    def setup_run(self, root, counts=(1, 1), first='answer', retry=False, question_template=None, provider_ids=None, archived_provider_ids=None):
        base = question_template or fixtures.config(root / 'unused')['questions'][0]
        order = [dict(deepcopy(base), question_id='synthetic-clean-' + str(i)) for i in range(sum(counts))]
        if first == 'empty':
            order[0]['question_id'] = 'synthetic-empty'
        cfg = test_checkpoint.CheckpointTests().cfg(root / 'template-unused')
        cfg['questions'] = order
        if provider_ids is not None:
            from openweb_news.preparation import selected_providers
            from openweb_news.recovery import sibling_rule
            cfg['provider_ids'] = list(provider_ids)
            cfg['tie_order'] = list(provider_ids)
            scheduling = cfg['execution_policy']['scheduling']
            scheduling['accounts'] = {k: v for k, v in scheduling['accounts'].items() if not k.startswith('provider/')}
            scheduling['recovery'] = {k: v for k, v in scheduling['recovery'].items() if not k.startswith('provider/')}
            for provider in selected_providers(cfg, provider_ids):
                account = 'provider/' + provider['auth']['secret_ref']
                scheduling['accounts'][account] = dict(concurrency=2, minimum_start_interval_seconds=0)
                scheduling['recovery'][account] = sibling_rule('provider', 2)
        if retry:
            from openweb_news.resilience import POLICY
            cfg['automatic_recovery'] = deepcopy(POLICY)
            cfg['execution_policy']['max_attempts_per_task'] = 3
            cfg['execution_policy']['retry_policy'] = dict(max_total=None, max_search=None, max_model=None,
                http_statuses=[503], backoff_seconds=[0, 0])
        template = prepare(cfg, mode='fixture')
        write_private(root / 'template.json', template)
        selection = root / 'admitted/selection.json'
        bank = selection.parent / 'bank.jsonl'
        phase = selection.parent / 'phase-1-questions.jsonl'
        write_private(bank, b''.join(encoded(q) for q in order))
        write_private(phase, b''.join(encoded(q) for q in order))
        write_private(selection, dict(bank_sha256=digest(bank.read_bytes()), question_order=[q['question_id'] for q in order],
            phases=[dict(phase=1, question_ids=[q['question_id'] for q in order], questions_sha256=digest(order))]))
        write_private(selection.parent / 'VERIFICATION-MANIFEST.json', dict(files=[dict(path=p.name, sha256=digest(p.read_bytes())) for p in (bank, phase, selection)]))
        # Synthetic diagnostic only; it never becomes execution/method authority.
        archived_cfg = deepcopy(cfg)
        archived_cfg['run_directory'] = str(root / 'archived')
        if archived_provider_ids is not None:
            archived_cfg['provider_ids'] = list(archived_provider_ids)
            archived_cfg['tie_order'] = list(archived_provider_ids)
        archived = prepare(archived_cfg, mode='fixture')
        report = run(archived)
        report['questions'] = order
        # Distinct archived evidence identities must never block same-question ranks.
        identities = {o['case_id']: 'archived-' + o['case_id'] for o in report['outcomes']}
        for outcome in report['outcomes']:
            outcome['case_id'] = identities[outcome['case_id']]
            outcome['packet_sha256'] = 'synthetic-different-archived-packet'
        for row in report['scores']['question_rows']:
            row['evidence_ids'] = [identities[c] for c in row.get('evidence_ids', [])]
        write_private(root / 'archive/report.json', report)
        write_private(root / 'archive/closure.json', dict(package_sha256=report['package_sha256'], report_sha256=digest((root / 'archive/report.json').read_bytes()), execution='completed'))
        reference = dict(report_path=str(root / 'archive/report.json'), report_sha256=digest((root / 'archive/report.json').read_bytes()),
            closure_path=str(root / 'archive/closure.json'), closure_sha256=digest((root / 'archive/closure.json').read_bytes()))
        authority = dict(profile=profile_authority.METHOD_PROFILE, mode='fixture', package_path=str(root / 'method/package.json'), approval_sha256='synthetic-owner-method',
            profile_bindings=profile_authority.bindings(cfg['reviewers']))
        return cfg, selection, reference, authority, order

    def test_general_tavily_lifecycle_discloses_changed_mode_without_importing_votes(self):
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            cfg, selection, reference, _, _ = self.setup_run(root, counts=(1,),
                provider_ids=['tavily-general'], archived_provider_ids=['tavily-news'])
            cfg, _ = accepted_fixture(root / 'new-authority', cfg)
            write_private(root / 'template.json', prepare(cfg, mode='fixture'), replace=True)
            kwargs = dict(ceiling='100', accepted_profile=cfg['method_acceptance_package'],
                archived_reference=reference, stage_counts=(1,), mode='fixture')
            with self.assertRaises(ValueError):
                clean_run.prepare(root / 'template.json', selection, root / 'missing-pair', **kwargs)
            reference['configuration_replacements'] = {'tavily-general': 'tavily-news'}
            p = clean_run.prepare(root / 'template.json', selection, root / 'clean', **kwargs)
            path = root / 'clean/clean-run.json'
            self.assertEqual(clean_run.verify(path)[0], p)
            self.assertIn('tavily-general', clean_run.approval_review(p))
            approval = dict(decision='approved', package_sha256=digest(p), approved_by='synthetic owner', approved_at='fixture')
            result = clean_run.run(path, approval)
            diagnostic = load(result['evidence']['comparison']['path'])
            self.assertEqual(diagnostic['configuration_replacements'], reference['configuration_replacements'])
            self.assertEqual(diagnostic['providers']['tavily-general']['archived_configuration_id'], 'tavily-news')
            self.assertEqual(result['state'], 'awaiting_stage_review')
            changed = deepcopy(p)
            changed['archived_reference']['configuration_replacements'] = {'tavily-general': 'exa-highlights'}
            write_private(path, changed, replace=True)
            with self.assertRaises(ValueError):
                clean_run.verify(path)

    def prepared(self, root, counts=(1, 1), first='answer', retry=False):
        cfg, selection, reference, _, order = self.setup_run(root, counts, first, retry)
        cfg, _ = accepted_fixture(root / 'new-authority', cfg)
        template = prepare(cfg, mode='fixture')
        authority = profile_authority.read(root / 'new-authority/method/package.json', cfg['reviewers'])
        write_private(root / 'template.json', template, replace=True)
        package = clean_run.prepare(root / 'template.json', selection, root / 'clean', ceiling='100', accepted_profile=authority['package_path'],
            archived_reference=reference, stage_counts=counts, mode='fixture')
        approval = dict(decision='approved', package_sha256=digest(package), approved_by='owner', approved_at='fixture')
        return root / 'clean/clean-run.json', package, approval, order

    def review(self, path, result, decision='continue', **changes):
        receipt = dict(stage_index=result['stage_index'], decision=decision, evidence=result['evidence'],
            reviewed_by='independent-reviewer', reviewed_at='fixture', material_method_change=False,
            unexplained_material_differences=[], invariant_failures=[], inspection=dict(
                model_inputs='synthetic retained input reviewed', date_source_and_quotes='synthetic source reviewed',
                changed_and_unchanged_cases='both retained cases checked', ranking_movements='diagnostic ranks inspected'))
        receipt.update(changes)
        return clean_run.record_stage_review(path, receipt)

    def test_repaired_prefix_carries_costs_once_and_resumes_without_prefix_calls(self):
        from openweb_news import settlement
        with TemporaryDirectory()as temp:
            root=Path(temp).resolve();path,original,approval,order=self.prepared(root)
            first=clean_run.run(path,approval)
            source_path=Path(first['evidence']['report']['path'])
            source,cases,native=settlement.load_parent(source_path)
            authority=original['accepted_profile']
            seed=deepcopy(source);seed.update(mode='date-profile-report-v1',active_review_profile=authority)
            seed['phase_costs']={k:'0'for k in seed['phase_costs']if k!='components'}
            seed['phase_costs'].update(other_conservative_holds_and_allowances_usd='7.50',
                conservative_total_usd='7.50',published_pro_plus_conservative_residual_usd='7.50')
            seed['phase_costs']['components']=[dict(name='synthetic-original-first',conservative_total_usd='7.50')]
            seed_path=root/'repaired/report/report.json';write_private(seed_path,seed)
            write_private(root/'repaired/package.json',dict(repair_disposition={'synthetic':True},accepted_profile=authority,jobs=[]))
            original_loader=settlement.load_parent
            def parent(file,**kwargs):
                return (deepcopy(seed),deepcopy(cases),deepcopy(native))if Path(file).resolve()==seed_path else original_loader(file,**kwargs)
            with patch('openweb_news.settlement.load_parent',side_effect=parent):
                evidence=clean_run.seed_evidence(seed_path,original['archived_reference'],authority)
                review=dict(stage_index=0,decision='continue',evidence=evidence,reviewed_by='independent fixture',
                    reviewed_at='fixture',material_method_change=False,unexplained_material_differences=[],invariant_failures=[],
                    inspection={k:'fixture inspected'for k in ('model_inputs','date_source_and_quotes','changed_and_unchanged_cases','ranking_movements')})
                review_path=root/'repaired/review.json';write_private(review_path,review)
                plan=clean_run.prepare(root/'template.json',original['selection_path'],root/'remaining',ceiling='100',
                    accepted_profile=authority['package_path'],archived_reference=original['archived_reference'],stage_counts=(1,),
                    mode='fixture',seed_report=seed_path,seed_review=review_path)
                self.assertEqual(plan['stages'][0]['questions'],[order[1]])
                self.assertEqual(plan['stages'][0]['cumulative'],2)
                actual=clean_run.run(root/'remaining/clean-run.json',dict(approval,package_sha256=digest(plan)))
                self.assertEqual(actual['state'],'awaiting_stage_review')
                self.assertEqual(actual['evidence']['question_ids'],[q['question_id']for q in order])
                report=load(actual['evidence']['report']['path'])
                self.assertEqual(report['phase_costs']['conservative_total_usd'],'7.50')
                self.assertEqual(report['phase_costs']['other_conservative_holds_and_allowances_usd'],'7.50')
                checkpoint=load(root/'remaining/stage-1/checkpoint.json')
                self.assertEqual(checkpoint['carried_usd'],'7.50')
                new_native=load(root/'remaining/stage-1/native/package.json')
                self.assertEqual(Decimal(new_native['configuration']['execution_policy']['spending_ceiling_usd']),Decimal('92.50'))
                self.assertEqual(new_native['questions'],[order[1]])
                from openweb_news import operator
                verified=operator.verify(root/'remaining/clean-run.json')
                self.assertTrue(verified['verified'])
                status=operator.status(root/'remaining/clean-run.json')
                self.assertEqual(status['costs']['shared_committed_and_held_usd'],'7.50')
                self.assertEqual(status['costs']['remaining_allowance_usd'],'92.50')
                new_reviews=load(root/'remaining/stage-1/review/package.json')['jobs']
                self.assertTrue(all(job['case_id']not in cases for job in new_reviews))
                before=source_path.read_bytes()
                with patch('openweb_news.fixtures.search',side_effect=AssertionError('no repeat collection')),                     patch('openweb_news.fixtures.model',side_effect=AssertionError('no repeat judgment')):
                    resumed=clean_run.run(root/'remaining/clean-run.json',dict(approval,package_sha256=digest(plan)))
                self.assertEqual(resumed,actual);self.assertEqual(source_path.read_bytes(),before)
                stopped=deepcopy(review);stopped['decision']='stop'
                stop_path=root/'repaired/stop.json';write_private(stop_path,stopped)
                stop_plan=clean_run.prepare(root/'template.json',original['selection_path'],root/'stopped',ceiling='100',
                    accepted_profile=authority['package_path'],archived_reference=original['archived_reference'],stage_counts=(1,),
                    mode='fixture',seed_report=seed_path,seed_review=stop_path)
                with patch('openweb_news.fixtures.search',side_effect=AssertionError('stop must block collection')):
                    result=clean_run.run(root/'stopped/clean-run.json',dict(approval,package_sha256=digest(stop_plan)))
                self.assertEqual(result['state'],'stopped_after_seed_review')

    def test_clean_first_and_second_close_without_old_parent_and_resume_without_calls(self):
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            path, p, approval, order = self.prepared(root)
            first = clean_run.run(path, approval)
            self.assertEqual(first['state'], 'awaiting_stage_review')
            self.assertFalse((root / 'clean/stage-2').exists())
            self.assertEqual(load(root / 'clean/stage-1/checkpoint.json')['parent_report'], None)
            self.assertEqual(first['evidence']['question_ids'], [order[0]['question_id']])
            self.assertEqual(first['evidence']['unclassified_native_completions'], 0)
            diagnostic = load(first['evidence']['comparison']['path'])
            self.assertTrue(diagnostic['diagnostic_only'])
            self.assertEqual(first['evidence']['expected_differences'], p['stages'][0]['expected_differences'])
            self.assertEqual(diagnostic['expected_differences'], p['stages'][0]['expected_differences'])
            with self.assertRaises(ValueError):
                self.review(path, first, unexplained_material_differences=['requires diagnosis'])
            with self.assertRaises(ValueError):
                self.review(path, first, inspection={})
            before = (root / 'clean/stage-1/native/run/attempts.jsonl').read_bytes()
            with patch('openweb_news.fixtures.search', side_effect=AssertionError('no repeated search')), patch('openweb_news.fixtures.model', side_effect=AssertionError('no repeated vote')):
                self.assertEqual(clean_run.run(path, approval), first)
            self.review(path, first)
            second = clean_run.run(path, approval)
            self.assertEqual(second['evidence']['question_ids'], [q['question_id'] for q in order])
            self.assertEqual(second['evidence']['expected_differences'], p['stages'][1]['expected_differences'])
            self.assertEqual(load(second['evidence']['comparison']['path'])['expected_differences'], p['stages'][1]['expected_differences'])
            self.assertEqual((root / 'clean/stage-1/native/run/attempts.jsonl').read_bytes(), before)
            self.review(path, second)
            completed = clean_run.run(path, approval)
            self.assertEqual(completed['state'], 'completed')
            self.assertEqual(completed['questions'], 2)
            self.assertEqual(load(completed['report']['path'])['phase_costs']['conservative_total_usd'], '0')

    def test_success_empty_and_failed_first_are_classified_cells_and_compare_without_same_items(self):
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            path, _, approval, _ = self.prepared(root, counts=(1,), first='empty')
            result = clean_run.run(path, approval)
            report = load(result['evidence']['report']['path'])
            self.assertEqual({c['status'] for c in report['collection']}, {'success_empty', 'http_error'})
            self.assertEqual(report['outcomes'], [])
            diagnostic = load(result['evidence']['comparison']['path'])
            self.assertEqual(diagnostic['rankings']['delivered_utility']['answer@3']['status'], 'determined')
            self.assertTrue(diagnostic['diagnostic_only'])

    def test_failed_first_preserves_error_and_does_not_self_approve(self):
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            path, _, approval, _ = self.prepared(root, counts=(1,))
            def failed(*args):
                return dict(transport_success=False, http_status=503, complete=True, raw_text='synthetic failed first')
            with patch('openweb_news.fixtures.search', side_effect=failed):
                result = clean_run.run(path, approval)
            report = load(result['evidence']['report']['path'])
            self.assertTrue(all(c['status'] == 'http_error' for c in report['collection']))
            self.assertFalse((root / 'clean/stage-1/stage-review.json').exists())
            self.assertTrue(list((root / 'clean/stage-1/native/run/attempts').rglob('result.json')))
            with self.assertRaises(ValueError):
                self.review(path, result, material_method_change=True)
            self.review(path, result, decision='stop', material_method_change=True)
            self.assertEqual(clean_run.run(path, approval)['state'], 'stopped_after_review')

    def test_transient_retry_and_uncertain_hold_preserve_exact_native_task(self):
        for uncertain in (False, True):
            with self.subTest(uncertain=uncertain), TemporaryDirectory() as temp:
                root = Path(temp).resolve()
                path, _, approval, _ = self.prepared(root, counts=(1,), retry=not uncertain)
                original = fixtures.model
                calls, chosen = [], []
                def model(role, compiled, folder):
                    if role == 'pro':
                        cid = compiled['packet']['case_id']
                        if not chosen:
                            chosen.append(cid)
                        if cid == chosen[0]:
                            calls.append(cid)
                            if uncertain or len(calls) == 1:
                                return dict(transport_success=False, complete=not uncertain, http_status=None if uncertain else 503,
                                    dispatch_state='uncertain' if uncertain else 'completed', raw_text='synthetic retained failure')
                    return original(role, compiled, folder)
                with patch('openweb_news.fixtures.model', side_effect=model):
                    result = clean_run.run(path, approval)
                    resumed = clean_run.run(path, approval)
                self.assertEqual(len(calls), 1 if uncertain else 2)
                self.assertEqual(result['state'], 'completed_with_holds' if uncertain else 'awaiting_stage_review')
                self.assertEqual(resumed['state'], result['state'])
                if uncertain:
                    self.assertFalse((root / 'clean/stage-1/review').exists())
                events = list(records(root / 'clean/stage-1/native/run/attempts.jsonl'))
                starts = [e for e in events if e['event'] == 'started' and e['task_id'] == 'pro-' + chosen[0]]
                self.assertEqual(len({e['request_sha256'] for e in starts}), 1)

    def test_unclassified_completion_and_duplicate_or_missing_cell_are_rejected(self):
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            path, _, approval, _ = self.prepared(root, counts=(1,))
            original = clean_run.inspect_ledger
            def bad(directory, **kwargs):
                value = original(directory, **kwargs)
                value['unclassified'] = ['synthetic-unclassified']
                return value
            with patch.object(clean_run, 'inspect_ledger', side_effect=bad), self.assertRaises(ValueError):
                clean_run.run(path, approval)
            self.assertFalse((root / 'clean/stage-1/report').exists())
            real_combine = clean_run._combine
            for missing in (False, True):
                def corrupt(*args):
                    report = real_combine(*args)
                    if missing:
                        report['collection'].pop()
                    else:
                        report['collection'].append(deepcopy(report['collection'][0]))
                    return report
                with patch.object(clean_run, '_combine', side_effect=corrupt), self.assertRaises(ValueError):
                    clean_run.run(path, approval)

    def test_cumulative_ceiling_carries_only_new_retained_charge_holds(self):
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            path, p, approval, _ = self.prepared(root)
            slots = p['stages'][0]['native_prototype']['scope']['result_slots']
            self.assertEqual(p['stages'][0]['ordinary_review_task_bound'], 3 * slots)
            self.assertEqual(p['stages'][1]['accepted_review_task_bound'], 4 * slots)
            # Synthetic prices are persisted in real runner request/ledger records.
            with patch('openweb_news.runner.reservation', side_effect=lambda request, package:
                    Decimal('0.10') if request['kind'] == 'search' else Decimal('0')):
                first = clean_run.run(path, approval)
                self.assertEqual(Decimal(first['evidence']['costs']['conservative_total_usd']), Decimal('0.20'))
                self.assertEqual(Decimal(first['evidence']['costs']['other_conservative_holds_and_allowances_usd']), Decimal('0.20'))
                self.review(path, first)
                second = clean_run.run(path, approval)
            native = load(root / 'clean/stage-2/native/package.json')
            self.assertEqual(Decimal(native['configuration']['execution_policy']['spending_ceiling_usd']), Decimal('99.80'))
            self.assertEqual(Decimal(second['evidence']['costs']['conservative_total_usd']), Decimal('0.40'))
            self.assertEqual(Decimal(load(root / 'clean/stage-2/checkpoint.json')['carried_usd']), Decimal('0.20'))

    def test_expected_differences_are_bound_before_calls_and_rebuilt_on_verify(self):
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            path, p, _, order = self.prepared(root)
            first, second = p['stages']
            declarations = {row['category']: row for row in first['expected_differences']}
            self.assertEqual(declarations['new_collection_and_votes']['bindings']['new_question_ids'], [order[0]['question_id']])
            self.assertEqual(declarations['accepted_model_inputs']['bindings']['judge_input_profile'], p['accepted_profile']['profile_bindings']['judge_input_profile'])
            self.assertEqual(declarations['owner_selected_date_trust']['bindings']['date_trust_receipt_sha256'], p['accepted_profile']['profile_bindings']['date_trust']['receipt_sha256'])
            self.assertEqual(declarations['provider_request_choices']['bindings']['search_requests_sha256'], digest(first['native_prototype']['search_requests']))
            self.assertNotEqual(first['expected_differences'], second['expected_differences'])
            view = (root / 'clean/APPROVAL-REVIEW.md').read_text()
            for stage in p['stages']:
                self.assertIn(json.dumps(stage['expected_differences'], ensure_ascii=False, sort_keys=True, indent=2), view)
            with patch.object(clean_run, 'run_native', side_effect=AssertionError('verification must not dispatch')):
                clean_run.verify(path)
                for change in ('missing', 'edited', 'reordered', 'wrong_stage'):
                    altered = deepcopy(p)
                    if change == 'missing':
                        altered['stages'][0].pop('expected_differences')
                    elif change == 'edited':
                        altered['stages'][0]['expected_differences'][0]['expected'] = 'Unreviewed explanation'
                    elif change == 'reordered':
                        altered['stages'][0]['expected_differences'].reverse()
                    else:
                        altered['stages'][0]['expected_differences'] = deepcopy(second['expected_differences'])
                    write_private(path, altered, replace=True)
                    with self.assertRaisesRegex(ValueError, 'manifest/order/bounds changed'):
                        clean_run.verify(path)
            write_private(path, p, replace=True)

    def test_operator_verifies_final_gate_comparison_and_verdict_without_writes(self):
        from openweb_news import operator
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            path, _, approval, _ = self.prepared(root, counts=(1,))
            first = clean_run.run(path, approval)
            self.review(path, first)
            self.assertEqual(clean_run.run(path, approval)['state'], 'completed')
            def snapshot():
                return dict(entries=sorted(p.relative_to(root).as_posix() for p in root.rglob('*')),
                    files={p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*') if p.is_file()})
            before = snapshot()
            with patch.object(clean_run, 'run_native', side_effect=AssertionError('verification cannot dispatch')):
                self.assertTrue(operator.verify(path)['verified'])
            self.assertEqual(snapshot(), before)
            stage = path.parent / 'stage-1'
            targets = [
                (stage / 'gate-evidence.json', lambda value: value.update(expected_differences=[])),
                (stage / 'comparison.json', lambda value: value.update(expected_differences=[])),
                (stage / 'stage-review.json', lambda value: value['evidence'].update(expected_differences=[])),
                (stage / 'stage-review.json', lambda value: value.update(stage_index=2)),
                (stage / 'stage-review.json', lambda value: value.update(decision='stop')),
                (path.parent / 'closure.json', lambda value: value.update(questions=999)),
                (path.parent / 'closure.json', lambda value: value['report'].update(path=str(stage / 'report/report.json'))),
            ]
            for target, change in targets:
                with self.subTest(target=target.name, change=change):
                    original = target.read_bytes()
                    value = load(target)
                    change(value)
                    write_private(target, value, replace=True)
                    modified = snapshot()
                    try:
                        with self.assertRaises(ValueError):
                            operator.verify(path)
                        self.assertEqual(snapshot(), modified)
                    finally:
                        write_private(target, original, replace=True)
            review = stage / 'stage-review.json'
            original = review.read_bytes()
            review.unlink()
            missing = snapshot()
            try:
                with self.assertRaisesRegex(ValueError, 'every bound checkpoint continuation'):
                    operator.verify(path)
                self.assertEqual(snapshot(), missing)
            finally:
                write_private(review, original)
            self.assertEqual(snapshot(), before)

    def test_operator_checks_comparison_before_gate_write_without_creating_gate(self):
        from openweb_news import operator
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            path, _, approval, _ = self.prepared(root, counts=(1,))
            first = clean_run.run(path, approval)
            stage = path.parent / 'stage-1'
            gate = stage / 'gate-evidence.json'
            gate.unlink()  # Interruption after comparison, before atomic gate write.
            def snapshot():
                return dict(entries=sorted(p.relative_to(root).as_posix() for p in root.rglob('*')),
                    files={p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*') if p.is_file()})
            before = snapshot()
            result = operator.verify(path)
            self.assertTrue(result['verified'])
            self.assertFalse(result['checks'][-1]['existing_clean_stage_gates'][0]['gate_present'])
            self.assertEqual(snapshot(), before)
            self.review(path, first)  # External review without its persisted gate is an orphan.
            orphan = snapshot()
            with self.assertRaisesRegex(ValueError, 'no bound gate evidence'):
                operator.verify(path)
            self.assertEqual(snapshot(), orphan)

    def test_approval_view_binds_package_and_separates_capacity_from_api_estimates(self):
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            _, p, _, _ = self.prepared(root, counts=(2, 1))
            view = (root / 'clean/APPROVAL-REVIEW.md').read_text()
            self.assertIn(digest(p), view)
            self.assertIn(p['accepted_profile']['package_sha256'], view)
            self.assertIn(p['accepted_profile']['profile_bindings']['date_trust']['receipt_sha256'], view)
            self.assertIn('NOT APPROVED', view)
            self.assertIn('not a worst-case bound', view)
            self.assertIn('no available capacity or completion date is promised', view)
            self.assertIn('no automatic API fallback', view)
            first, second = p['capacity']['stages']
            self.assertEqual(first['native_tasks'], dict(pro_api=40, sol_subscription=40, conditional_claude_subscription=40))
            self.assertEqual(first['ordinary_review'], dict(total=120, codex=40, claude=80))
            self.assertEqual(second['accepted_review'], dict(total=120, codex=60, claude=60))
            self.assertEqual(second['cumulative_questions'], 3)
            self.assertEqual(second['ordinary_review']['total'], p['stages'][1]['ordinary_review_task_bound'])
            self.assertEqual(first['native_empty_evidence_baseline'], p['stages'][0]['native_prototype']['reservation_forecast'])
            self.assertFalse((root / 'clean/approval.json').exists())

    def test_nonempty_ordinary_and_accepted_reviews_close_two_stages_without_repeat(self):
        from openweb_news import adjudication, blind_input, date_trust, judging, publication
        from openweb_news.preparation import selected_providers
        from .helpers import native_response, question, verdict
        from .test_blind_input import model_verdict
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            q = question('synthetic-publication-gap')
            q['contract']['temporal_requirements'] = [dict(id='window', basis='publication',
                start_inclusive='2026-10-01T00:00:00Z', end_exclusive='2026-11-01T00:00:00Z')]
            cfg, selection, reference, _, order = self.setup_run(root, counts=(1, 1), question_template=q, provider_ids=['valyu-search'])
            providers = selected_providers(cfg, cfg['provider_ids'])
            write_private(root / 'diagnostic.json', dict(synthetic=True, negative_controls=True))
            write_private(root / 'source.txt', b'Synthetic exact native field admission.\n')
            date_trust.prepare(providers, root / 'trust', diagnostic_paths=[root / 'diagnostic.json'], source_bindings={'synthetic': root / 'source.txt'})
            receipt = load(root / 'trust/approval-template.json')
            receipt.update(approved=True, approved_by='synthetic fixture owner', approved_at='fixture',
                selections={'valyu-search': dict(eligible_fields=['publication_date'], reason='Synthetic exact owner field choice')})
            write_private(root / 'trust/approval.json', receipt)
            cfg.update(judge_input_profile=blind_input.LIVE_PROFILE, date_trust_package=str(root / 'trust/package.json'))
            method = profile_authority.prepare_method(cfg, root / 'method', mode='fixture')
            write_private(root / 'method/approval.json', dict(approved=True, accept_method=True, package_sha256=digest(method),
                approved_by='synthetic fixture owner', approved_at='fixture'))
            cfg['method_acceptance_package'] = str(root / 'method/package.json')
            write_private(root / 'template.json', prepare(cfg, mode='fixture'), replace=True)
            p = clean_run.prepare(root / 'template.json', selection, root / 'clean', ceiling='100',
                accepted_profile=root / 'method/package.json', archived_reference=reference, stage_counts=(1, 1), mode='fixture')
            approval = dict(decision='approved', package_sha256=digest(p), approved_by='synthetic fixture owner', approved_at='fixture')
            compiled_cases, calls = {}, []
            def native_value(compiled):
                value = verdict(compiled)
                native = next(r['date_id'] for r in compiled['input']['native_date_ids'] if r['relative_path'] == 'publication_date')
                value['dates'] = [dict(candidate_id='finding-1', predicate_id='publication:window',
                    link_status='not_demonstrated', role='first_publication', date_ids=[native], anchor_date_id='',
                    additional_dates=[], context_support=[], reason='Ordinary vote does not upgrade native unspecified publication.')]
                return value
            def search(request, provider, folder):
                response = native_response(provider, text='Editorial report. Published October 12, 2026: the fictional Aster observatory opened.')
                response['results'][0]['publication_date'] = '2026-10-12'
                raw = encoded(response)
                write_private(folder / 'response.bin', raw)
                calls.append(('search', request['question_id']))
                return dict(transport_success=True, complete=True, http_status=200, raw_text=raw.decode(), captured_at='2026-10-28T00:00:00Z', fixture=True)
            def model(role, compiled, folder):
                compiled_cases[compiled['packet']['case_id']] = deepcopy(compiled)
                value = model_verdict(native_value(compiled), compiled['model_projection'])
                calls.append((role, compiled['packet']['question']['question_id']))
                write_private(folder / 'fixture-verdict.json', value)
                if role == 'pro':
                    response = dict(modelVersion=judging.METHOD['primary']['model'], candidates=[dict(finishReason='STOP',
                        content=dict(parts=[dict(text=encoded(value).decode())]))], usageMetadata=dict(promptTokenCount=0, candidatesTokenCount=0))
                    return dict(transport_success=True, complete=True, http_status=200, raw_text=encoded(response).decode(), fixture=True)
                return dict(transport_success=True, verdict=value, served_model='synthetic-model', prior_verdict_exposure=False, fixture=True)
            def review(job, folder):
                calls.append((job['stage'], job['task_id']))
                if job['stage'] == 'adjudication':
                    field = job['blind_packet']['input']['allowed_text'][0]
                    quote = dict(field=field['field'], quote=field['text'], occurrence=1)
                    value = dict(disposition='unknown', selected_native_date_ids=job['blind_packet']['input']['eligible_native_date_ids'],
                        checks=[dict(check=k, status='unknown', reason='Synthetic unresolved ordinary review.', support=[deepcopy(quote)]) for k in adjudication.CHECKS],
                        contrary_reading='Publication date requires whole-occurrence review.', reason='Synthetic ordinary review stays unknown.')
                else:
                    self.assertEqual(job['stage'], 'publication')
                    compiled = compiled_cases[job['case_id']]
                    value = native_value(compiled)
                    field = compiled['packet']['allowed_text'][0]
                    quote = dict(field=field['field'], quote=field['text'], occurrence=1)
                    value.update(overall_grade=2, contrary_reading='A changing page or conflicting date would prevent this inference.',
                        complete_occurrence_reviewed=True, policy_applications=[dict(candidate_ids=['finding-1'], predicate_id='publication:window',
                            policy=publication.NATIVE_POLICY, status='pass', checks=[dict(check=k,status='pass',reason='Synthetic fixed own report supports this check.',
                                support=[deepcopy(quote)]) for k in publication.CHECKS])])
                    value['dates'][0].update(link_status='established', context_support=[deepcopy(quote)])
                    source_packet = publication.packet_for(compiled, _source=True)
                    projection = blind_input.project(source_packet, source_packet=compiled['packet'], profile=blind_input.LIVE_PROFILE)
                    value = model_verdict(value, projection)
                return dict(transport_success=True, verdict=value, native_completion_verified=True, fixture=True)
            path = root / 'clean/clean-run.json'
            with patch('openweb_news.fixtures.search', side_effect=search), patch('openweb_news.fixtures.model', side_effect=model):
                first = clean_run.run(path, approval, fixture_call=review)
                self.assertEqual(first['state'], 'awaiting_stage_review')
                self.assertEqual(load(root / 'clean/stage-1/report/report.json')['outcome_counts'], {'unknown': 1})
                self.assertEqual(load(root / 'clean/stage-1-accepted-review/review-checkpoint.json')['review_tasks'], 2)
                self.assertEqual(load(first['evidence']['report']['path'])['outcome_counts'], {'2': 1})
                self.assertEqual(len(load(root / 'clean/stage-1/review/package.json')['jobs']), 2)
                from openweb_news import checkpoint
                ordinary_path = root / 'clean/stage-1/review/package.json'
                completed_binding = checkpoint.completed_review(ordinary_path)[0]
                self.assertIsNone(completed_binding['native_ledger_sha256'])
                self.assertTrue(completed_binding['fixture_ledger_sha256'])
                ordinary_closure = load(root / 'clean/stage-1/review/run/closure.json')
                self.assertEqual(len(ordinary_closure['fixture_response_hashes']), 2)
                ordinary_events = list(records(root / 'clean/stage-1/review/run/attempts.jsonl'))
                retained = root / 'clean/stage-1/review/run' / next(e['result_path'] for e in ordinary_events if e['event'] == 'finished')
                original_bytes = retained.read_bytes()
                try:
                    write_private(retained, dict(load(retained), synthetic_tamper=True), replace=True)
                    with self.assertRaises(ValueError):
                        checkpoint.completed_review(ordinary_path)
                finally:
                    write_private(retained, original_bytes, replace=True)
                first_calls = list(calls)
                self.assertEqual(clean_run.run(path, approval, fixture_call=lambda *_: self.fail('review repeated')), first)
                self.assertEqual(calls, first_calls)
                self.review(path, first)
                second = clean_run.run(path, approval, fixture_call=review)
                self.assertEqual(second['state'], 'awaiting_stage_review')
                self.assertEqual(load(root / 'clean/stage-2-accepted-review/review-checkpoint.json')['review_tasks'], 2)
                final = load(second['evidence']['report']['path'])
                self.assertEqual(final['outcome_counts'], {'2': 2})
                self.assertEqual(final['questions'], order)
                self.review(path, second)
                closed = clean_run.run(path, approval, fixture_call=lambda *_: self.fail('closed reviews repeated'))
                self.assertEqual(closed['state'], 'completed')
            for qid in (q['question_id'] for q in order):
                self.assertEqual(sum(kind == 'search' and key == qid for kind, key in calls), 1)
            self.assertEqual(sum(kind == 'publication' for kind, _ in calls), 4)
            self.assertEqual(sum(kind == 'adjudication' for kind, _ in calls), 4)

    def test_real_independent_method_receipts_need_no_old_report_authority(self):
        from openweb_news import blind_input, date_trust
        from openweb_news.preparation import selected_providers
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            cfg, selection, reference, _, order = self.setup_run(root, counts=(1,))
            diagnostic = root / 'synthetic-diagnostic.json'
            source = root / 'synthetic-method-source.txt'
            write_private(diagnostic, dict(synthetic=True, negative_controls=True, not_accuracy=True))
            write_private(source, b'Synthetic selected source evidence. No publication truth claim.\n')
            providers = selected_providers(cfg, cfg['provider_ids'])
            trust = date_trust.prepare(providers, root / 'date-trust', diagnostic_paths=[diagnostic], source_bindings={'synthetic': source})
            receipt = load(root / 'date-trust/approval-template.json')
            receipt.update(approved=True, approved_by='synthetic owner', approved_at='fixture', selections={
                c['configuration_id']: dict(eligible_fields=[], reason='Synthetic explicit no field admission') for c in providers})
            write_private(root / 'date-trust/approval.json', receipt)
            cfg.update(judge_input_profile=blind_input.LIVE_PROFILE, date_trust_package=str(root / 'date-trust/package.json'))
            method = profile_authority.prepare_method(cfg, root / 'new-method', mode='fixture')
            self.assertNotIn('source_report_path', method)
            self.assertNotIn('calibration_package_path', method)
            write_private(root / 'new-method/approval.json', dict(approved=True, accept_method=True,
                package_sha256=digest(method), approved_by='synthetic owner', approved_at='fixture'))
            authority = profile_authority.read(root / 'new-method/package.json', cfg['reviewers'])
            cfg['live_profile_authority'] = authority
            template = prepare(cfg, mode='fixture')
            write_private(root / 'template.json', template, replace=True)
            package = clean_run.prepare(root / 'template.json', selection, root / 'clean', ceiling='100',
                accepted_profile=root / 'new-method/package.json', archived_reference=reference, stage_counts=(1,), mode='fixture')
            approval = dict(decision='approved', package_sha256=digest(package), approved_by='synthetic owner', approved_at='fixture')
            result = clean_run.run(root / 'clean/clean-run.json', approval)
            self.assertEqual(result['state'], 'awaiting_stage_review')
            self.assertEqual(result['evidence']['question_ids'], [order[0]['question_id']])
            self.assertEqual(load(root / 'clean/stage-1/checkpoint.json')['parent_report'], None)
            with self.assertRaises(ValueError):
                clean_run.prepare(root / 'template.json', selection, root / 'must-not-live', ceiling='100',
                    accepted_profile=root / 'new-method/package.json', archived_reference=reference, stage_counts=(1,), mode='live')

    def test_archived_accepted_wrapper_requires_its_bound_package_and_owner_receipt(self):
        from openweb_news import accepted_review
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            _, _, flat, _, _ = self.setup_run(root, counts=(1,))
            report = load(flat['report_path'])
            wrapper = dict(profile=accepted_review.PROFILE, output_directory=str(root / 'wrapper'),
                parent_report_sha256='synthetic-parent-report', accepted_profile={'synthetic': True})
            write_private(root / 'wrapper/review-checkpoint.json', wrapper)
            write_private(root / 'wrapper/approval.json', dict(approved=True, package_sha256=digest(wrapper), approved_by='owner', approved_at='fixture'))
            write_private(root / 'wrapper/closure.json', dict(package_sha256=digest(wrapper), execution={'state': 'completed'},
                source_report_sha256=wrapper['parent_report_sha256'], accepted_profile_sha256=digest(wrapper['accepted_profile']),
                report_files={'report.json': flat['report_sha256']}))
            reference = dict(report=clean_run._bound_file(flat['report_path']),
                closure=clean_run._bound_file(root / 'wrapper/closure.json'),
                package=clean_run._bound_file(root / 'wrapper/review-checkpoint.json'),
                approval=clean_run._bound_file(root / 'wrapper/approval.json'))
            self.assertEqual(clean_run.archived_report(reference), report)
            missing = dict(reference)
            missing.pop('package')
            with self.assertRaises(ValueError):
                clean_run.archived_report(missing)
            wrong = deepcopy(wrapper)
            wrong['parent_report_sha256'] = 'wrong-parent'
            write_private(root / 'wrapper/wrong.json', wrong)
            with self.assertRaises(ValueError):
                clean_run.archived_report(dict(reference, package=clean_run._bound_file(root / 'wrapper/wrong.json')))

    def test_unsupported_review_backends_are_rejected_before_any_native_calls(self):
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            _, p, _, _ = self.prepared(root, counts=(1,))
            template = load(root / 'template.json')
            template['configuration']['reviewers']['codex']['backend'] = 'openai-api'
            write_private(root / 'unsupported-template.json', template)
            with patch.object(clean_run, 'run_native', side_effect=AssertionError('must never dispatch')), self.assertRaisesRegex(ValueError, 'subscription reviews'):
                clean_run.prepare(root / 'unsupported-template.json', p['selection_path'], root / 'unsupported', ceiling='100',
                    accepted_profile=p['accepted_profile']['package_path'], archived_reference=p['archived_reference'], stage_counts=(1,), mode='fixture')
            self.assertFalse((root / 'unsupported').exists())

    def test_frozen_stage_order_and_owner_approval_are_required(self):
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            path, p, approval, _ = self.prepared(root, counts=(2, 1))
            self.assertEqual([s['count'] for s in p['stages']], [2, 1])
            self.assertEqual(clean_run.STAGE_COUNTS, (10, 20, 30, 40, 100))
            old_authority = dict(p['accepted_profile'], profile=profile_authority.LIVE_PROFILE)
            with patch.object(profile_authority, 'read', return_value=old_authority), self.assertRaises(ValueError):
                clean_run.prepare(root / 'template.json', p['selection_path'], root / 'reject-old-authority', ceiling='100',
                    accepted_profile=old_authority['package_path'], archived_reference=p['archived_reference'], stage_counts=(1,), mode='fixture')
            with self.assertRaises(ValueError):
                clean_run.run(path, dict(approval, package_sha256='wrong'))
            p['stages'][0]['questions'].reverse()
            write_private(path, p, replace=True)
            with self.assertRaises(ValueError):
                clean_run.verify(path)


if __name__ == '__main__':
    unittest.main()
