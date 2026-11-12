from copy import deepcopy
from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest

from openweb_news import clean_run, fixtures, settlement, publication, profile_authority
from openweb_news.common import digest, encoded, file_digest, load, records, write_private
from . import test_lean_checkpoints


def rejected():
    usage = dict(input_tokens=0, output_tokens=0, cache_creation_input_tokens=0,
                 cache_read_input_tokens=0, iterations=[])
    raw = dict(transport_success=False, dispatch_state='completed', exit_code=1,
        served_model='<synthetic>', usage=usage, cli_cost_usd_estimate=0, verdict=None, fixture=True)
    events = [dict(type='rate_limit_event', rate_limit_info=dict(status='rejected')),
        dict(type='result', is_error=True, api_error_status=429, structured_output=None,
             modelUsage={}, total_cost_usd=0, usage=deepcopy(usage))]
    return raw, events


class QuotaRepairTests(unittest.TestCase):
    def test_only_proven_no_inference_is_eligible(self):
        raw, events = rejected()
        self.assertTrue(settlement.quota_rejected_without_inference(raw, events))
        for change in ('answered', 'tokens', 'missing_tokens', 'timeout', 'no_rejection', 'missing_final', 'cost', 'native_model'):
            with self.subTest(change=change):
                r, es = deepcopy(raw), deepcopy(events)
                if change == 'answered': r['verdict'] = {'invalid': True}
                if change == 'tokens': r['usage']['output_tokens'] = 1; es[-1]['usage'] = deepcopy(r['usage'])
                if change == 'missing_tokens': del r['usage']['input_tokens']; es[-1]['usage'] = deepcopy(r['usage'])
                if change == 'timeout': r.update(dispatch_state='uncertain', complete=False)
                if change == 'no_rejection': es[0]['rate_limit_info']['status'] = 'allowed'
                if change == 'missing_final': es.pop()
                if change == 'cost': es[-1]['total_cost_usd'] = .01
                if change == 'native_model': r['served_model'] = 'real-model'
                self.assertFalse(settlement.quota_rejected_without_inference(r, es))

    def source(self, root):
        original = fixtures.model
        def model(role, compiled, folder):
            if compiled['packet']['question']['question_id'].endswith('-1'):
                if role == 'claude':
                    raw, events = rejected()
                    write_private(folder/'stdout.jsonl', b''.join(encoded(e) for e in events))
                    return raw
                value = original(role, compiled, folder)
                if role == 'codex':
                    v = value['verdict']; v['overall_grade'] = 0
                    v['candidates'][0].update(answer_supported=False, clear_lead=False)
                    v['candidates'][0]['conditions'][0]['status'] = 'fail'
                return value
            return original(role, compiled, folder)
        with patch('openweb_news.fixtures.model', side_effect=model), patch('openweb_news.publication.eligibility', return_value=None):
            helper, p, approval, _ = test_lean_checkpoints.LeanCheckpointTests().setup_prefix(root)
            path = root/'lean/clean-run.json'
            result = clean_run.run(path, approval)
        gate = root/'lean/stage-1/gate-evidence.json'
        checklist = root/'checklist.json'
        write_private(checklist, dict(gate_sha256=file_digest(gate), reviewed_by='synthetic independent reviewer',
            reviewed_at='fixture', inspection={k:'checked' for k in
                ('model_inputs', 'date_source_and_quotes', 'changed_and_unchanged_cases', 'ranking_movements')}))
        return p, gate, checklist, result

    def test_complete_repair_preserves_other_outcomes_and_costs_and_reuses_votes(self):
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve(); source, gate, checklist, old = self.source(root)
            parent = load(old['evidence']['report']['path'])
            p = clean_run.prepare_quota_repair(gate, checklist, root/'repair',
                accepted_profile=source['accepted_profile']['package_path'], maximum_tasks=10)
            child = load(p['thirds']['path'])
            from openweb_news import operator
            self.assertEqual(operator.status(root/'repair/quota-repair.json')['state'], 'prepared')
            self.assertTrue(operator.verify(root/'repair/quota-repair.json')['verified'])
            self.assertEqual(len(child['jobs']), 2)  # One occurrence for each fixture provider.
            self.assertEqual(p['maximum_followup_tasks'], 4)
            selected = {r['case_id'] for r in child['roster']}
            for row, job in zip(child['roster'], child['jobs']):
                request = load(row['rejection'][0]['path'])
                self.assertEqual(job['blind_packet'], request['blind_packet'])
            called = []
            def call(job, folder):
                called.append(job['task_id'])
                case = load(root/'repair/thirds/inputs'/(job['case_id']+'.json'))
                raw = fixtures.model('codex', case['compiled'], folder)
                return dict(raw, native_completion_verified=True, complete=True, dispatch_state='completed')
            approval = dict(decision='approved', package_sha256=digest(p), approved_by='synthetic owner', approved_at='fixture')
            result = clean_run.run_quota_repair(root/'repair/quota-repair.json', approval, call=call)
            self.assertEqual(result['state'], 'awaiting_stage_review')
            self.assertEqual(len(called), 2)
            final = load(result['evidence']['report']['path'])
            self.assertEqual(final['phase_costs'], parent['phase_costs'])
            previous = {o['case_id']:o for o in parent['outcomes']}
            for outcome in final['outcomes']:
                if outcome['case_id'] in selected: self.assertEqual(outcome['grade'], 2)
                else: self.assertEqual(outcome, previous[outcome['case_id']])
            again = clean_run.run_quota_repair(root/'repair/quota-repair.json', approval,
                call=lambda *_: self.fail('completed vote resent'))
            self.assertEqual(again, result)
            review = dict(decision='continue', stage_index=result['evidence']['stage_index'], evidence=result['evidence'],
                gate_sha256=result['gate_sha256'], reviewed_by='synthetic independent reviewer', reviewed_at='fixture',
                material_method_change=False, unexplained_material_differences=[], invariant_failures=[],
                inspection={k:'checked' for k in ('model_inputs','date_source_and_quotes','changed_and_unchanged_cases','ranking_movements')})
            clean_run.record_quota_repair_review(root/'repair/quota-repair.json', review)
            prefix = clean_run._bound_file(root/'repair/prefix.json')
            clean_run._verified_prefix(prefix, p['accepted_profile'])
            self.assertTrue(operator.verify(root/'repair/quota-repair.json')['verified'])
            self.assertEqual(operator.status(root/'repair/quota-repair.json')['state'], 'completed')
            self.assertTrue(Path(result['report']).exists())
            review_path=root/'repair/stage-review.json'
            review_path.write_bytes(review_path.read_bytes()+b' ')
            with self.assertRaises(ValueError):operator.verify(root/'repair/quota-repair.json')

    def test_changed_source_evidence_and_changed_method_are_rejected(self):
        with TemporaryDirectory() as temp:
            root=Path(temp).resolve(); source, gate, checklist, old = self.source(root)
            closed=load(load(gate)['evidence_closure']['path'])
            raw=next(Path(r['path']) for r in closed['files'] if r['path'].endswith('/stdout.jsonl'))
            before=raw.read_bytes();raw.write_bytes(before+b' ')
            with self.assertRaises(ValueError):
                clean_run.prepare_quota_repair(gate,checklist,root/'bad',accepted_profile=source['accepted_profile']['package_path'],maximum_tasks=10)
            raw.write_bytes(before)
            old_auth=source['accepted_profile'];changed=deepcopy(old_auth)
            changed['profile_bindings']['semantic_code']['judging.py']='changed'
            with self.assertRaises(ValueError):clean_run._same_choices(old_auth,changed)

    def test_a_rejected_repair_vote_stays_held_without_retries(self):
        with TemporaryDirectory() as temp:
            root=Path(temp).resolve();source,gate,checklist,_=self.source(root)
            p=clean_run.prepare_quota_repair(gate,checklist,root/'repair',accepted_profile=source['accepted_profile']['package_path'],maximum_tasks=10)
            approval=dict(decision='approved',package_sha256=digest(p),approved_by='synthetic owner',approved_at='fixture')
            calls=[]
            def call(job,folder):
                calls.append(job['task_id']);return rejected()[0]
            result=clean_run.run_quota_repair(root/'repair/quota-repair.json',approval,call=call)
            self.assertEqual(result['state'],'completed_with_holds')
            count=len(calls)
            clean_run.run_quota_repair(root/'repair/quota-repair.json',approval,call=lambda *_:self.fail('failed quota vote silently resent'))
            self.assertEqual(count,len(calls))
            self.assertFalse((root/'repair/gate-evidence.json').exists())

    def test_new_third_can_trigger_only_bounded_normal_followup(self):
        with TemporaryDirectory() as temp:
            root=Path(temp).resolve();source,gate,checklist,old=self.source(root)
            parent=load(old['evidence']['report']['path'])
            p=clean_run.prepare_quota_repair(gate,checklist,root/'repair',accepted_profile=source['accepted_profile']['package_path'],maximum_tasks=10)
            chosen={r['case_id'] for r in load(p['thirds']['path'])['roster']};calls=[]
            def call(job,folder):
                calls.append((job['stage'],job['case_id']))
                if job['stage']=='publication':
                    return dict(transport_success=True,verdict={},native_completion_verified=True,complete=True,dispatch_state='completed')
                case=load(root/'repair/thirds/inputs'/(job['case_id']+'.json'))
                raw=fixtures.model('codex',case['compiled'],folder);v=raw['verdict']
                v['overall_grade']=1;v['candidates'][0].update(answer_supported=False,clear_lead=True)
                v['candidates'][0]['conditions'][0].update(status='not_demonstrated',support=[])
                return dict(raw,native_completion_verified=True,complete=True,dispatch_state='completed')
            def eligible(compiled,positions,*args,**kwargs):
                return 'single' if positions.get('claude',{}).get('grade')==1 else None
            approval=dict(decision='approved',package_sha256=digest(p),approved_by='synthetic owner',approved_at='fixture')
            with patch('openweb_news.publication.eligibility',side_effect=eligible):
                result=clean_run.run_quota_repair(root/'repair/quota-repair.json',approval,call=call)
            self.assertEqual(result['state'],'awaiting_stage_review')
            self.assertEqual(Counter(stage for stage,_ in calls),{'third':2,'publication':4})
            self.assertTrue(all(cid in chosen for _,cid in calls))
            before={o['case_id']:o for o in parent['outcomes']}
            for row in load(result['evidence']['report']['path'])['outcomes']:
                if row['case_id']not in chosen:self.assertEqual(row,before[row['case_id']])

    def test_rebound_terminal_unknown_does_not_request_another_review(self):
        with TemporaryDirectory() as temp:
            root=Path(temp).resolve();source,gate,checklist,_=self.source(root)
            p=clean_run.prepare_quota_repair(gate,checklist,root/'repair',accepted_profile=source['accepted_profile']['package_path'],maximum_tasks=10)
            meta=load(p['source_snapshot']['path']);parent=load(meta['report']['path'])
            cases=settlement.VerifiedCases(meta['facts'],meta['case_refs'],p['accepted_profile'])
            old=next(o for o in parent['outcomes'] if o['grade']is not None);old['grade']=None
            packet=publication.packet_for(cases[old['case_id']]['compiled'])
            b=p['accepted_profile']['profile_bindings']
            parent.setdefault('publication_review',{})['records']=[dict(case_id=old['case_id'],terminal=True,
                terminal_key='old-code-terminal-key',checker_profile=b['checker_profile'],date_profile=b['date_profile'],text_profile=b['text_profile'],
                provenance={r:dict(native_completion_verified=True,input_sha256=packet['input_sha256'])for r in ('codex','claude')})]
            with patch('openweb_news.publication.eligibility',return_value='single'):
                rows=settlement.selected(parent,cases,meta['native']['configuration']['reviewers'],authority=p['accepted_profile'])
            self.assertNotIn(old['case_id'],{r['case_id']for r in rows})

    def test_held_gate_rebinds_only_the_allowed_orchestration_code(self):
        with TemporaryDirectory() as temp:
            root=Path(temp).resolve();source,gate,checklist,_=self.source(root)
            original=source['code'];changed=dict(original,**{'clean_run.py':'synthetic-new-orchestration'})
            cfg=load(root/'lean/stage-1/native/config.json')
            with patch('openweb_news.profile_authority.code_bindings',return_value=changed), \
                    patch('openweb_news.settlement.code_bindings',return_value=changed), \
                    patch('openweb_news.clean_run.code_bindings',return_value=changed):
                target=profile_authority.prepare_method(cfg,root/'target-method',mode='fixture')
                write_private(root/'target-method/approval.json',dict(approved=True,accept_method=True,
                    package_sha256=digest(target),approved_by='synthetic owner',approved_at='fixture'))
                snapshot=clean_run.rebind_held_checkpoint(gate,checklist,root/'rebound',accepted_profile=root/'target-method/package.json')
                meta=load(snapshot['path'])
                self.assertEqual(meta['code'],changed)
                self.assertEqual(meta['case_refs'],load(load(gate)['snapshot']['path'])['case_refs'])
                settlement.read_snapshot(snapshot,load(gate)['report']['path'])

    def test_recorded_source_verdict_and_empty_repair_are_refused(self):
        with TemporaryDirectory() as temp:
            root=Path(temp).resolve();source,gate,checklist,_=self.source(root)
            for decision in ('continue','stop'):
                review=gate.parent/'stage-review.json';write_private(review,dict(decision=decision),replace=True)
                with self.assertRaisesRegex(ValueError,'held gate'):
                    clean_run.rebind_held_checkpoint(gate,checklist,root/'closed',accepted_profile=source['accepted_profile']['package_path'])
            review.unlink()
            with patch('openweb_news.settlement.quota_rejected_without_inference',return_value=False):
                with self.assertRaisesRegex(ValueError,'nonempty'):
                    clean_run.prepare_quota_repair(gate,checklist,root/'empty',accepted_profile=source['accepted_profile']['package_path'],maximum_tasks=10)

    def test_unselected_unjudged_cases_are_accounted_and_missing_evidence_refuses(self):
        with TemporaryDirectory() as temp:
            root=Path(temp).resolve();source,gate,checklist,_=self.source(root)
            snapshot=clean_run.rebind_held_checkpoint(gate,checklist,root/'rebound',accepted_profile=source['accepted_profile']['package_path'])
            parent,cases,native=settlement.read_snapshot(snapshot,load(gate)['report']['path'])
            excluded=[]
            with patch('openweb_news.settlement.quota_rejected_without_inference',return_value=False):
                self.assertEqual(settlement.selected_quota_thirds(parent,cases,native,exclusions=excluded),[])
            self.assertEqual(len(excluded),2)
            self.assertTrue(all(r['reason']=='not_proven_no_inference_quota_rejection'for r in excluded))
            closure=load(native['quota_repair_source']['closure']['path'])
            removed=next(r for r in closure['files']if r['path'].endswith('/stdout.jsonl'))
            closure['files'].remove(removed);write_private(root/'incomplete.json',closure)
            native['quota_repair_source']['closure']=clean_run._bound_file(root/'incomplete.json')
            with self.assertRaisesRegex(ValueError,'closed source evidence'):
                settlement.selected_quota_thirds(parent,cases,native)

    def test_new_result_tamper_cannot_be_reblessed_at_sealing(self):
        with TemporaryDirectory() as temp:
            root=Path(temp).resolve();source,gate,checklist,_=self.source(root)
            p=clean_run.prepare_quota_repair(gate,checklist,root/'repair',accepted_profile=source['accepted_profile']['package_path'],maximum_tasks=10)
            approval=dict(decision='approved',package_sha256=digest(p),approved_by='synthetic owner',approved_at='fixture')
            def call(job,folder):
                case=load(root/'repair/thirds/inputs'/(job['case_id']+'.json'))
                return dict(fixtures.model('codex',case['compiled'],folder),native_completion_verified=True,complete=True,dispatch_state='completed')
            original=clean_run.accepted_review.run
            def tamper(*args,**kwargs):
                result=original(*args,**kwargs)
                run=root/'repair/thirds/run';event=next(e for e in records(run/'attempts.jsonl')if e['event']=='finished')
                path=run/event['result_path'];path.write_bytes(path.read_bytes()+b' ')
                return result
            with patch('openweb_news.clean_run.accepted_review.run',side_effect=tamper):
                with self.assertRaisesRegex(ValueError,'changed'):
                    clean_run.run_quota_repair(root/'repair/quota-repair.json',approval,call=call)
            self.assertFalse((root/'repair/evidence-closure.json').exists())
            self.assertFalse((root/'repair/gate-evidence.json').exists())
