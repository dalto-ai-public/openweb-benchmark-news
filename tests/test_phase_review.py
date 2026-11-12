from copy import deepcopy
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from openweb_news import adjudication, fixtures, phase_review
from openweb_news.common import asset_bindings,code_bindings,digest,load,write_private
from openweb_news.preparation import prepare
from openweb_news.runner import run
from openweb_news.phase_report import write_readable
from . import test_review_pipeline


class MappingProofTests(unittest.TestCase):
    def setUp(self):
        fixture=test_review_pipeline.AdjudicationTests();fixture.setUp();self.f=fixture

    def test_distinct_scopes_verified_without_weakening_policy_checks(self):
        f=self.f;data=f.packet['input'];proof=data['native_mapping_proof']
        self.assertNotEqual(proof['applicability_mapping_id'],proof['observations'][0]['observation_mapping_id'])
        self.assertEqual(proof['field_path'],proof['observations'][0]['relative_path'])
        self.assertNotIn('configuration_sha256',data['publication_estimate_applicability'])
        adjudication.reject_exposure(data)
        old=adjudication.packet_for(f.compiled,f.ids,profile=adjudication.PROFILE)
        self.assertNotIn('native_mapping_proof',old['input'])
        self.assertNotEqual(f.packet['input_sha256'],old['input_sha256'])
        bad=deepcopy(f.compiled);bad['input']['native_date_ids'][0]['mapping_id']='invented';bad['input_sha256']=digest(bad['input'])
        self.assertFalse(adjudication.eligible(bad,f.positions,f.config))
        altered=deepcopy(f.packet);altered['input']['native_mapping_proof']['field_path']='wrong'
        with self.assertRaises(ValueError):adjudication.corroborate(f.compiled,f.positions,altered,f.responses,f.config)
        response=deepcopy(f.responses);response['claude']['verdict']['checks'][0]['status']='unknown'
        effective,record=adjudication.corroborate(f.compiled,f.positions,f.packet,response,f.config)
        self.assertFalse(record['accepted'])


class PhaseReportTests(unittest.TestCase):
    def test_real_scores_unknown_denominators_coverage_and_escaping(self):
        with tempfile.TemporaryDirectory()as temp:
            root=Path(temp);config=fixtures.config(root/'run');p=prepare(config,mode='fixture');report=run(p)
            report['questions']=deepcopy(config['questions'])
            report['questions'][0]['provider_query']='<script>unsafe</script> | query'
            before=deepcopy(report['scores']);summary=write_readable(root/'readable',report)
            for row in summary['providers']:
                expected=before['accounting'][row['configuration_id']][5]
                self.assertEqual(row['cutoffs']['5'],expected)
                included=set(row['coverage']['answer@5']['included_questions'])
                excluded=set(row['coverage']['answer@5']['excluded_questions'])
                self.assertFalse(included & excluded)
                self.assertEqual(len(included|excluded),len(config['questions']))
            self.assertEqual(report['scores'],before)
            page=(root/'readable/report.html').read_text()
            self.assertNotIn('<script>unsafe</script>',page)
            self.assertIn('Unknown',page)
            saved=load(root/'readable/phase-summary.json')
            self.assertEqual(saved['returned'],len(report['outcomes']))
            self.assertTrue((root/'readable/providers.csv').exists())

    def test_cutoff_selector_uses_answered_canonical_ndcg_and_pending_sorts_last(self):
        from openweb_news.phase_report import ordered_providers
        with tempfile.TemporaryDirectory()as temp:
            root=Path(temp);cfg=fixtures.config(root/'run');report=run(prepare(cfg,mode='fixture'));report['questions']=cfg['questions']
            summary=write_readable(root/'readable',report)
            for k in (3,5,10):
                rows=ordered_providers(summary,k)
                numeric=[p['coverage'][f'ndcg@{k}']['resolved_family_mean']for p in rows]
                known=[v for v in numeric if v is not None]
                self.assertEqual(known,sorted(known,reverse=True))
                self.assertEqual(numeric,known+[None]*(len(numeric)-len(known)))
                for p in rows:
                    cid=p['configuration_id']
                    from openweb_news._method import scoring
                    values={(r['question_id'],cid):r['metrics']['ndcg'] if r['call_status'] in scoring.ANSWERED else scoring.not_applicable('failed') for r in report['scores']['question_rows'] if r['k']==k and r['configuration_id']==cid}
                    expected=scoring.family_mean(cfg['questions'],[cid],set(report['scores']['availability']['answered_question_ids'][cid]),values)
                    self.assertEqual(p['coverage'][f'ndcg@{k}']['resolved_family_mean'],expected['per_configuration'][cid]['resolved_family_mean'])
                    self.assertEqual(len(p['coverage'][f'ndcg@{k}']['mean_question_ids']),expected['resolved_question_count'])
            page=(root/'readable/report.html').read_text()
            for k in (3,5,10):
                self.assertIn(f'id="cutoff-{k}"',page);self.assertIn(f'panel-{k}',page);self.assertIn(f'nDCG@{k}',page)
            self.assertIn('id="cutoff-5" checked',page)
            self.assertNotIn('<script',page)
            self.assertIn('<details>',page)
            self.assertIn('How to read the columns',page)
            self.assertIn('not questions with at least one good answer',page)
            self.assertIn('100 expected results',page)
            self.assertIn('scope="col"',page)
            self.assertIn('Unknown results / expected results',page)
            self.assertIn('percentages may increase later',page)
        synthetic={'providers':[{'configuration_id':c,'coverage':{'ndcg@5':{'resolved_family_mean':v}}}for c,v in [('pending',None),('zero',0),('best',.8)]]}
        self.assertEqual([p['configuration_id']for p in ordered_providers(synthetic,5)],['best','zero','pending'])

    def test_failed_and_deferred_searches_never_enter_quality_coverage(self):
        from openweb_news._method import scoring
        from openweb_news.phase_report import build_summary,ordered_providers,key_points
        cfg=fixtures.config(Path('/unused'));questions=cfg['questions'];configs=['working','working-two','interrupted']
        cells={};outcomes=[];labels={};collection=[]
        for index,q in enumerate(questions):
            for cid in configs:
                status='success' if cid!='interrupted' else ['success','success_empty','failed','not_collected'][index]
                evidence=cid+q['question_id'];ranked=[]
                if status=='success':
                    ranked=[dict(rank=1,evidence_id=evidence,url='https://example.com/'+evidence)]
                    labels[evidence]=2;outcomes.append(dict(case_id=evidence,configuration_id=cid,question_id=q['question_id'],grade=2))
                cells[cid,q['question_id']]=dict(status=status,ranked=ranked)
                collection.append(dict(configuration_id=cid,question_id=q['question_id'],status=status))
        scores=scoring.score(questions,configs,cells,labels,{},.9,configs)
        before=deepcopy(scores)
        report=dict(scores=scores,questions=questions,outcomes=outcomes,collection=collection,outcome_counts={'2':len(outcomes)})
        summary=build_summary(report);p=next(p for p in summary['providers'] if p['configuration_id']=='interrupted')
        self.assertEqual(p['answered_question_count'],2)
        self.assertEqual(set(p['coverage']['answer@5']['included_questions']),{q['question_id'] for q in questions[:2]})
        self.assertIn('failed',p['coverage']['answer@5']['excluded_questions'][questions[2]['question_id']])
        self.assertIn('not_collected',p['coverage']['answer@5']['excluded_questions'][questions[3]['question_id']])
        self.assertEqual(p['coverage']['answer@5']['resolved_family_mean'],.1) # one answer / two answered Top5 lists
        self.assertEqual([p['configuration_id'] for p in ordered_providers(summary,5)],['working','working-two'])
        self.assertFalse(any('interrupted' in line for line in key_points(summary)[:3]))
        self.assertEqual(scores,before) # delivered zeros and all canonical scores remain intact
        with tempfile.TemporaryDirectory() as temp:
            report['execution']={'state':'completed'}
            write_readable(Path(temp),report);page=(Path(temp)/'report.html').read_text()
            self.assertIn('Partial coverage — outside the main comparison',page)
            self.assertIn('Searches completed',page)
            self.assertIn('2 / 4',page)

    def test_combined_phase_rescores_shared_pool_and_rejects_overlap(self):
        with tempfile.TemporaryDirectory()as temp:
            root=Path(temp);cfg=fixtures.config(root/'original');source=prepare(cfg,mode='fixture');base=run(source)
            qids=[q['question_id']for q in source['questions']];split=set(qids[:2])
            components=[]
            for name,scope in [('done',split),('remaining',set(qids)-split)]:
                own=[o for o in base['outcomes']if o['question_id']in scope]
                cases={o['case_id']:dict(original=o,compiled=load(root/'original/compiled'/(o['case_id']+'.json')))for o in own}
                cost=dict(published_completed_pro_usd='0',conservative_completed_pro_usd='0',other_conservative_holds_and_allowances_usd='0',conservative_total_usd='0',published_pro_plus_conservative_residual_usd='0',billing='fixture')
                binding=dict(bundle_path=str(root/name/'bundle.json'),question_ids=sorted(scope),adjudication_profile=adjudication.PROFILE,report_sha256='fixture',costs=cost)
                child=dict(source_method=base['method_version'],source_package_sha256=digest(source),jobs=[],source_collection=[r for r in base['collection']if r['question_id']in scope])
                report=dict(outcomes=own,review_pipeline=dict(records=[]))
                components.append((binding,child,report,cases))
            package=dict(source_package=str(root/'source/package.json'),question_ids=qids,completed_components=[components[0][0]])
            def completed(path):return components[1]if Path(path).parent.name=='remaining'else components[0]
            with patch.object(phase_review,'verify_phase',return_value=(package,source)),patch.object(phase_review,'completed_bundle',side_effect=completed),patch('openweb_news.accounting.report_costs',return_value=cost):
                result=phase_review.report_phase(root/'phase.json',root/'result')
                self.assertEqual(result['questions'],len(qids))
                final=load(root/'result/report.json')
                self.assertEqual(final['scores'],json.loads(json.dumps(base['scores'])))
                self.assertEqual(final['outcome_counts'],base['outcome_counts'])
                self.assertTrue((root/'result/report.md').exists())
                self.assertTrue((root/'result/phase-summary.json').exists())
                with self.assertRaises(ValueError):phase_review.report_phase(root/'phase.json',root/'source/run')
                self.assertFalse((root/'source/run/report.json').exists())
                original_bytes=(root/'original/report.json').read_bytes()
                with self.assertRaises(ValueError):phase_review.report_phase(root/'phase.json',root/'original')
                self.assertEqual((root/'original/report.json').read_bytes(),original_bytes)
                (root/'alias').symlink_to(root/'original',target_is_directory=True)
                with self.assertRaises(ValueError):phase_review.report_phase(root/'phase.json',root/'alias')
                self.assertEqual((root/'original/report.json').read_bytes(),original_bytes)
                components[1][0]['question_ids'].append(qids[0])
                with self.assertRaises(ValueError):phase_review.report_phase(root/'phase.json',root/'bad')

    def test_legacy_inventory_count_is_compatible_only_offline_with_same_proof_binding(self):
        with tempfile.TemporaryDirectory()as temp:
            root=Path(temp);source=dict(configuration=dict(run_directory=str(root/'source/run')))
            write_private(root/'source/package.json',source);write_private(root/'source/run/report.json',dict(fixture=True))
            child=dict(question_ids=['q'],source_package_sha256=digest(source),source_report_sha256=digest((root/'source/run/report.json').read_bytes()))
            write_private(root/'remaining/bundle.json',child)
            expected=dict(bundle_path='fixture',bundle_sha256='same',native_subscription_proofs=96)
            code=code_bindings();code['phase_review.py']='historical-reader'
            package=dict(profile=phase_review.PROFILE,code=code,assets=asset_bindings(),source_package=str(root/'source/package.json'),
                source_package_sha256=digest(source),completed_components=[expected],remaining_bundle_sha256=digest(child),remaining_question_ids=['q'])
            write_private(root/'phase.json',package);write_private(root/'approval.json',dict(decision='approved',package_sha256=digest(package)))
            changed=[False]
            def component(path,legacy_count=False):
                binding=dict(expected,native_subscription_proofs=96 if legacy_count else 145)
                if changed[0]:binding['bundle_sha256']='different'
                return binding,None,None,None
            with patch.object(phase_review,'completed_bundle',side_effect=component):
                phase_review.verify_phase(root/'phase.json',reporting_only=True)
                with self.assertRaises(ValueError):phase_review.verify_phase(root/'phase.json')
                changed[0]=True
                with self.assertRaises(ValueError):phase_review.verify_phase(root/'phase.json',reporting_only=True)

    def test_phase_binding_rejects_changed_code_and_completed_component(self):
        with tempfile.TemporaryDirectory()as temp:
            root=Path(temp);source=dict(questions=[]);write_private(root/'source.json',source)
            child=dict(question_ids=['q']);write_private(root/'remaining/bundle.json',child)
            component=dict(bundle_path='fixture',bundle_sha256='original')
            p=dict(profile=phase_review.PROFILE,code=code_bindings(),assets=asset_bindings(),source_package=str(root/'source.json'),source_package_sha256=digest(source),completed_components=[component],remaining_bundle_sha256=digest(child),remaining_question_ids=['q'])
            write_private(root/'phase.json',p)
            with patch.object(phase_review,'completed_bundle',return_value=(dict(component,bundle_sha256='changed'),None,None,None)):
                with self.assertRaises(ValueError):phase_review.verify_phase(root/'phase.json')
            p['code']['phase_review.py']='changed';write_private(root/'phase.json',p,replace=True)
            with self.assertRaises(ValueError):phase_review.verify_phase(root/'phase.json')
            p['code']=code_bindings();p['code']['adjudication.py']='changed';write_private(root/'phase.json',p,replace=True)
            with self.assertRaises(ValueError):phase_review.verify_phase(root/'phase.json',reporting_only=True)


if __name__=='__main__':unittest.main()

class CompletedAttemptTests(unittest.TestCase):
    def test_failed_attempt_is_preserved_and_only_completions_need_proofs(self):
        from openweb_news.review_pipeline import run_review, PROFILE
        from openweb_news.ledger import Ledger
        from openweb_news import representation, review_bundle
        from openweb_news.review import settle
        f=test_review_pipeline.AdjudicationTests();f.setUp()
        with tempfile.TemporaryDirectory()as temp:
            root=Path(temp);q=deepcopy(f.compiled['packet']['question']);q.update(family_id='family',need_id='need')
            cid=f.compiled['packet']['case_id']
            original=dict(case_id=cid,question_id=q['question_id'],configuration_id=f.config['configuration_id'],rank=1,packet_sha256=f.compiled['packet_sha256'],**settle(f.positions['pro'],f.positions['codex']))
            case=dict(compiled=f.compiled,verdicts={'pro':f.raw,'codex':f.raw},original=original,provenance={},provider=f.config)
            write_private(root/'review/inputs/case.json',case)
            config={r:dict(backend=r+'-subscription',model='fixture',reasoning_effort='low',timeout_seconds=1)for r in ('codex','claude')}
            jobs=[dict(task_id='adjudication-'+r+'-'+cid,stage='adjudication',role=r,case_id=cid,blind_packet=f.packet,kind='model',backend=r+'-subscription',backend_config=config[r],reservation_usd='0',reservation_basis='configured subscription; quota and time separate')for r in config]
            brave=dict(questions=[q],configuration=dict(run_directory=str(root/'brave/run')))
            write_private(root/'brave/package.json',brave)
            package=dict(profile=PROFILE,mode='fixture',code=code_bindings(),assets=asset_bindings(),incremental_api_calls=0,maximum_new_subscription_tasks=2,jobs=jobs,reviewers=config,
                cases=[dict(case_id=cid,path='inputs/case.json',sha256=digest((root/'review/inputs/case.json').read_bytes()))],text_profile=representation.PROFILE,
                adjudication_profile=adjudication.PROOF_PROFILE,run_directory=str(root/'review/run'),scheduling={r+'-subscription':dict(concurrency=1,minimum_start_interval_seconds=0)for r in config},
                source_collection=[dict(question_id=q['question_id'],configuration_id=f.config['configuration_id'],status='success')],providers=[f.config],questions=[q],coverage_target=.8,tie_order=[f.config['configuration_id']],
                source_method=4,source_report_sha256='source-report',source_package_sha256='source-package',replacement_package_sha256=digest(brave))
            write_private(root/'review/package.json',package)
            run_review(root/'review/package.json',call=lambda job,folder:dict(f.responses[job['role']],dispatch_state='completed',**({'complete':True}if job['role']=='codex'else{})))
            bundle=dict(profile=review_bundle.PROFILE,brave_package_sha256=digest(brave),source_package_sha256='source-package',source_report_sha256='source-report',question_ids=[q['question_id']])
            write_private(root/'bundle.json',bundle)
            write_private(root/'approval.json',dict(decision='approved',package_sha256=digest(bundle)))
            write_private(root/'review/approval.json',dict(approved=True,package_sha256=digest(package),parent_bundle_sha256=digest(bundle)))
            write_private(root/'brave/run/report.json',dict(package_sha256=digest(brave),execution=dict(state='completed')))
            request=dict(kind='model',backend='codex-subscription',reservation_usd='0')
            ledger=Ledger(root/'brave/run',dict(package_sha256=digest(brave),mode='fixture'),uncertain_resends=1)
            with ledger.locked():
                ledger.execute('saved-task',request,lambda folder:dict(transport_success=False,complete=False,dispatch_state='uncertain'),max_attempts=3)
                ledger.execute('saved-task',request,lambda folder:dict(transport_success=True,complete=True,dispatch_state='completed'),max_attempts=3,retry_failed=True)
            def proof(request,response,folder):
                self.assertTrue(response['transport_success']);return True
            cost=dict(conservative_total_usd='0')
            with patch.object(phase_review,'verified',side_effect=proof),patch('openweb_news.accounting.report_costs',return_value=cost):
                binding,*_=phase_review.completed_bundle(root/'bundle.json')
            self.assertEqual(binding['native_attempts'],4)
            self.assertEqual(binding['native_subscription_proofs'],3)
            with patch.object(phase_review,'verified',return_value=False):
                with self.assertRaises(ValueError):phase_review.completed_bundle(root/'bundle.json')
