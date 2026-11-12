from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest

from openweb_news import provider_completion as completion, fixtures, checkpoint, settlement, review_pipeline
from openweb_news.common import digest,load,write_private
from openweb_news.preparation import prepare,selected_providers
from openweb_news.runner import run
from openweb_news.recovery import sibling_rule
from .test_independent_method import accepted_fixture


class ProviderCompletionTests(unittest.TestCase):
    def fixture_source(self,root):
        cfg=fixtures.config(root/'source-native/run')
        cfg['provider_ids']=['brave-web','exa-highlights'];cfg['tie_order']=cfg['provider_ids'][:]
        base=cfg['questions'][0]
        cfg['questions']=[dict(deepcopy(base),question_id='fixture-completion-'+str(i))for i in range(3)]
        cfg['execution_policy']['spending_ceiling_usd']='100'
        cfg['execution_policy']['scheduling']['recovery']={k:sibling_rule('provider'if k.startswith('provider/')else'judge',v['concurrency'])for k,v in cfg['execution_policy']['scheduling']['accounts'].items()}
        cfg,_=accepted_fixture(root/'method-source',cfg)
        native=prepare(cfg,mode='fixture');write_private(root/'source-native/package.json',native);run(native)
        child=review_pipeline.prepare_review(root/'source-native/package.json',[q['question_id']for q in native['questions']],root/'source-review')
        approval=dict(approved=True,package_sha256=digest(child),approved_by='synthetic owner',approved_at='fixture')
        write_private(root/'source-review/approval.json',approval);review_pipeline.run_review(root/'source-review/package.json',approval=approval)
        component=checkpoint.completed_review(root/'source-review/package.json')
        costs={k:'0'for k in checkpoint.COST_KEYS};costs['components']=[]
        source=checkpoint.combine([component],{},native,costs)
        # Construct a closed synthetic source with one success and two gaps.
        # This is software-fixture evidence, never a rewritten live report.
        gaps={('brave-web',native['questions'][i]['question_id'])for i in(1,2)}
        source['outcomes']=[o for o in source['outcomes']if(o['configuration_id'],o['question_id'])not in gaps]
        source['collection']=[dict(r,status='http_error',dispatch_state='completed',transport_stage='http_request_or_response')
                              if(r['configuration_id'],r['question_id'])in gaps else r for r in source['collection']]
        deferred=source['collection'].pop(next(i for i,r in enumerate(source['collection'])if r['configuration_id']=='brave-web'and r['question_id']==native['questions'][2]['question_id']))
        source['deferred_collection']=[{k:deferred[k]for k in('configuration_id','question_id')}]
        retained=source['outcomes'][0];retained['grade']=None;retained['status']='unresolved'
        source['active_review_profile']=native['configuration']['live_profile_authority']
        source['outcome_counts']={};source['phase_costs']=costs
        ids={o['case_id']for o in source['outcomes']}
        cases={cid:case for cid,case in component[3].items()if cid in ids}
        settlement._rescore(source,cases,native)
        report=root/'source/report/report.json';write_private(report,source)
        basis=root/'source/basis.json';write_private(basis,dict(synthetic=True,closed=True))
        refs={r['case_id']:dict(path=str((root/'source-review'/r['path']).resolve()),sha256=r['sha256'])for r in child['cases']if r['case_id']in ids}
        trusted=settlement.VerifiedCases({cid:settlement.scoring_fact(c)for cid,c in cases.items()},{},native['configuration']['live_profile_authority'])
        snap=settlement.snapshot_parent(report,trusted,native,root/'source/accepted',case_refs=refs,basis=[completion.bound(basis)])
        closure=root/'source/evidence-closure.json';write_private(closure,dict(profile='verified-evidence-closure-v1',files=[completion.bound(basis)]))
        gate=dict(profile='synthetic-reviewed-source',stage_index=1,report=completion.bound(report),snapshot=snap,evidence_closure=completion.bound(closure),
                  question_ids=[q['question_id']for q in source['questions']],costs=costs,accepted_profile_sha256=digest(source['active_review_profile']),
                  unclassified_native_completions=0,undeclared_duplicate_tasks=0)
        write_private(root/'source/gate.json',gate)
        review=dict(decision='continue',stage_index=1,evidence=gate,reviewed_by='synthetic independent reviewer',reviewed_at='fixture',material_method_change=False,
                    unexplained_material_differences=[],invariant_failures=[],inspection={k:True for k in('model_inputs','date_source_and_quotes','changed_and_unchanged_cases','ranking_movements')})
        write_private(root/'source/review.json',review)
        prefix=dict(profile='verified-clean-prefix-v1',gate=completion.bound(root/'source/gate.json'),review=completion.bound(root/'source/review.json'),
                    snapshot=snap,closure=completion.bound(closure),question_ids=gate['question_ids'],costs=costs)
        write_private(root/'source/prefix.json',prefix)
        prefix_ref=completion.bound(root/'source/prefix.json')
        summary=completion.prepare_source_summary(prefix_ref,root/'summary')
        accounting=dict(gate=prefix['gate'],verdict=prefix['review'],benchmark_conservative_usd='0',separate_diagnostics_usd='0.025',
                        combined_conservative_usd='0.025',shared_ceiling_usd='100',remaining_usd='99.975')
        write_private(root/'accounting.json',accounting)
        cells=[dict(configuration_id='brave-web',question_id=native['questions'][i]['question_id'],source_status='http_error'if i==1 else'not_collected')for i in(1,2)]
        package=completion.prepare(prefix_ref,summary,root/'source-native/package.json',cells,root/'completion',
                                   accepted_profile=cfg['method_acceptance_package'],shared_accounting=completion.bound(root/'accounting.json'),mode='fixture')
        return package,source,report,summary,cfg

    def test_end_to_end_only_missing_cells_reviews_preserved_unknown_and_zero_call_resume(self):
        with TemporaryDirectory()as tmp:
            root=Path(tmp).resolve();p,source,source_path,_,_=self.fixture_source(root);before=source_path.read_bytes()
            path=root/'completion/completion.json'
            self.assertEqual(load(root/'completion/native/package.json')['scope']['search_cells'],2)
            approval=dict(decision='approved',package_sha256=digest(p),approved_by='synthetic owner',approved_at='fixture')
            with patch('openweb_news.fixtures.search',wraps=fixtures.search)as calls:
                result=completion.run(path,approval)
            self.assertEqual(calls.call_count,2);self.assertEqual({c.args[0]['configuration_id']for c in calls.call_args_list},{'brave-web'})
            self.assertEqual(result['state'],'awaiting_stage_review')
            final=load(result['evidence']['report']['path'])
            self.assertEqual(final['outcomes'][:len(source['outcomes'])],source['outcomes'])
            self.assertIsNone(final['outcomes'][0]['grade']);self.assertEqual(len(final['collection']),6)
            self.assertEqual(final['scores']['availability']['all_configuration_common_question_count'],3)
            self.assertEqual(source_path.read_bytes(),before)
            self.assertEqual(Decimal(final['phase_costs']['conservative_total_usd']),Decimal('0.025'))
            new_cases=set(load(root/'completion/review/package.json')['reference_outcomes'][i]['case_id']for i in range(2))
            self.assertFalse(new_cases&{o['case_id']for o in source['outcomes']})
            with patch('openweb_news.fixtures.search',side_effect=AssertionError('repeat search')),patch('openweb_news.fixtures.model',side_effect=AssertionError('repeat model')):
                self.assertEqual(completion.run(path,approval),result)
            review=dict(decision='continue',stage_index=1,evidence=result['evidence'],reviewed_by='synthetic independent',reviewed_at='fixture',material_method_change=False,
                        unexplained_material_differences=[],invariant_failures=[],inspection={k:True for k in('model_inputs','date_source_and_quotes','changed_and_unchanged_cases','ranking_movements')})
            completion.record_stage_review(path,review)
            self.assertEqual(completion.load_completed(result['evidence']['report']['path'])[0]['outcomes'],final['outcomes'])

    def test_source_cell_manifest_receipt_tamper_and_hold_guards(self):
        with TemporaryDirectory()as tmp:
            root=Path(tmp).resolve();p,source,_,summary,cfg=self.fixture_source(root);path=root/'completion/completion.json'
            projected=load(summary['path'])['projected'];providers=load(root/'completion/native/package.json')['providers']
            for cells in ([p['cells'][0]]*2,[dict(p['cells'][0],question_id=source['questions'][0]['question_id'],source_status='success')],
                          [dict(p['cells'][0],question_id='outside')]):
                with self.assertRaises(ValueError):completion._cells(projected,cells,providers)
            changed=deepcopy(projected);next(r for r in changed['collection']if r['configuration_id']=='brave-web'and r['status']=='http_error')['dispatch_state']='uncertain'
            with self.assertRaises(ValueError):completion._cells(changed,p['cells'],providers)
            with self.assertRaises(ValueError):completion.run(path,{})
            approval=dict(decision='approved',package_sha256=digest(p),approved_by='synthetic',approved_at='fixture')
            native=load(root/'completion/native/package.json');held=dict(package_sha256=digest(native),execution=dict(state='completed_with_holds',held_tasks=['uncertain']))
            with patch.object(completion.runner,'run',return_value=held):
                self.assertEqual(completion.run(path,approval)['state'],'completed_with_holds')
            self.assertFalse((root/'completion/gate-evidence.json').exists())
            write_private(summary['path'],dict(tampered=True),replace=True)
            with self.assertRaises(ValueError):completion.verify(path)

    def test_finalization_resumes_after_closure_and_snapshot_without_calls(self):
        for point in ('closure','snapshot','gate'):
            with self.subTest(point=point),TemporaryDirectory()as tmp:
                root=Path(tmp).resolve();p,source,source_path,_,_=self.fixture_source(root)
                path=root/'completion/completion.json'
                approval=dict(decision='approved',package_sha256=digest(p),approved_by='synthetic owner',approved_at='fixture')
                original_save=completion.save_same;original_snapshot=settlement.snapshot_parent
                def save(path,value):
                    result=original_save(path,value)
                    if point=='closure'and Path(path)==root/'completion/closure.json':raise RuntimeError('synthetic interruption after closure')
                    if point=='gate'and Path(path)==root/'completion/gate-evidence.json':raise RuntimeError('synthetic interruption after gate')
                    return result
                def snapshot(*args,**kwargs):
                    result=original_snapshot(*args,**kwargs)
                    if point=='snapshot'and Path(args[3])==root/'completion/accepted':raise RuntimeError('synthetic interruption after snapshot')
                    return result
                with patch.object(completion,'save_same',side_effect=save),patch.object(settlement,'snapshot_parent',side_effect=snapshot):
                    with self.assertRaisesRegex(RuntimeError,'synthetic interruption'):completion.run(path,approval)
                saved=load(root/'completion/closure.json');report_hash=saved['report']['sha256']
                with patch.object(completion.runner,'run',side_effect=AssertionError('repeated native')),patch.object(review_pipeline,'run_review',side_effect=AssertionError('repeated ordinary review')),\
                        patch.object(settlement,'run_settlement',side_effect=AssertionError('repeated accepted review')):
                    result=completion.run(path,approval)
                self.assertEqual(result['state'],'awaiting_stage_review')
                self.assertEqual(result['evidence']['report']['sha256'],report_hash)
                self.assertEqual(Decimal(result['evidence']['costs']['conservative_total_usd']),Decimal('0.025'))
                self.assertEqual(load(source_path)['outcomes'],source['outcomes'])

    def test_empty_or_failed_first_collection_is_honest_and_no_extra_recollection(self):
        for empty in (True,False):
            with self.subTest(empty=empty),TemporaryDirectory()as tmp:
                root=Path(tmp).resolve();p,source,_,_,_=self.fixture_source(root)
                path=root/'completion/completion.json';approval=dict(decision='approved',package_sha256=digest(p),approved_by='synthetic owner',approved_at='fixture')
                original=fixtures.search
                def search(request,provider,folder):
                    request=dict(request,question_id='synthetic-empty')
                    if not empty:provider=dict(provider,configuration_id='exa-highlights')
                    return original(request,provider,folder)
                with patch('openweb_news.fixtures.search',side_effect=search)as calls:
                    result=completion.run(path,approval)
                self.assertEqual(calls.call_count,2)
                final=load(result['evidence']['report']['path'])
                self.assertEqual(final['outcomes'],source['outcomes'])
                self.assertEqual(result['evidence']['full_provider_grid_success'],empty)
                self.assertEqual(result['evidence']['remaining_unanswered_completed_cells'],0 if empty else 2)
                self.assertEqual(len(final['provider_completion']['previous_collection']),2)
                with patch('openweb_news.fixtures.search',side_effect=AssertionError('unapproved recollection')):
                    self.assertEqual(completion.run(path,approval)['evidence'],result['evidence'])

    def test_forecast_and_credentials_follow_search_subset_but_judge_accounts_stay_exact(self):
        root = Path(self.enterContext(TemporaryDirectory())).resolve()
        cfg=fixtures.config(root/'provider-completion-no-execution')
        cfg['questions']=cfg['questions'][:1]
        cfg['provider_ids']=['exa-highlights','perplexity-web','brave-web','firecrawl-web','searlo-web','parallel-advanced','tavily-general','linkup-fast','you-com-web','valyu-search','oxylabs-search','keenable-pro','seltz-web-pro']
        cfg['tie_order']=cfg['provider_ids'][:]
        for provider in selected_providers(cfg,cfg['provider_ids']):cfg['execution_policy']['scheduling']['accounts']['provider/'+provider['auth']['secret_ref']]=dict(concurrency=1,minimum_start_interval_seconds=0)
        cfg['search_provider_ids']=['brave-web']
        import openweb_news.preparation as owner
        original=owner.reservation;requests=[]
        def reserve(request,package):requests.append(request);return original(request,package)
        with patch.object(owner,'reservation',side_effect=reserve):package=prepare(cfg,mode='live')
        self.assertEqual(len(package['providers']),13);self.assertEqual(len(package['search_requests']),1)
        self.assertEqual({r['configuration_id']for r in requests if r['kind']=='search'},{'brave-web'})
        self.assertEqual(len([r for r in requests if r['kind']=='model']),3)
        self.assertEqual(set(package['required_credential_references']),{'OPENWEB_NEWS_BRAVE_API_KEY',package['primary']['secret_ref']})
        bad=deepcopy(cfg);bad['execution_policy']['scheduling']['accounts'].pop('gemini-api/'+package['primary']['secret_ref'])
        with self.assertRaises(ValueError):prepare(bad,mode='live')
        full=deepcopy(cfg);full.pop('search_provider_ids')
        self.assertEqual(len(prepare(full,mode='live')['search_requests']),13)


if __name__=='__main__':unittest.main()
