"""Prepared additive checkpoint: collect, judge, review, audit and pool reports."""
from collections import Counter
from copy import deepcopy
from decimal import Decimal
from itertools import combinations
from pathlib import Path
import tempfile

from . import caching, judging
from .common import asset_bindings,code_bindings,digest,encoded,load,now,private_directory,records,write_private
from .preparation import prepare
from .resilience import POLICY
from .request_profiles import BRAVE_UNDECORATED, apply as request_profiles
from .runner import run
from .review_pipeline import prepare_review,run_review,verify
from .phase_review import completed_bundle,replay_completed_review
from .completion_audit import inspect_ledger
from .accounting import report_costs
from .reporting import write_reports
from ._method import scoring

PROFILE='additive-checkpoint-v1'
COST_KEYS=('published_completed_pro_usd','conservative_completed_pro_usd','other_conservative_holds_and_allowances_usd','conservative_total_usd','published_pro_plus_conservative_residual_usd')


def completed_review(path):
    path=Path(path);p=load(path);root=path.parent;run=Path(p['run_directory'])
    r=load(run/'report.json');closure=load(run/'closure.json');approval=load(root/'approval.json')
    if (r['package_sha256']!=digest(p) or closure['package_sha256']!=digest(p)
            or closure['report_sha256']!=digest((run/'report.json').read_bytes()) or r['execution']['state']!='completed'
            or approval.get('approved')is not True or approval.get('package_sha256')!=digest(p)):
        raise ValueError('completed review approval/report binding differs')
    cases=verify(p,root,historical=True)
    from .settlement import fixture_method, _fixture_responses
    if fixture_method(p):
        responses,ledger_sha=_fixture_responses(p)
        response_hashes={task:digest(raw) for task,raw in responses.items()}
        if closure.get('fixture_ledger_sha256')!=ledger_sha or closure.get('fixture_response_hashes')!=response_hashes:
            raise ValueError('ordinary fixture evidence changed after closure')
        completion_binding=dict(native_ledger_sha256=None,native_audit_sha256=None,
            fixture_ledger_sha256=ledger_sha,fixture_response_hashes=response_hashes)
    else:
        audit=inspect_ledger(run,allow_empty=not p['jobs'])
        jobs={j['task_id']:j for j in p['jobs']}
        if audit['unclassified'] or {e['task_id']for e in audit['records']}!=set(jobs):
            raise ValueError('review tasks lack complete native audits')
        responses={};ends={e['task_id']:e for e in records(run/'attempts.jsonl')if e['event']=='finished'}if (run/'attempts.jsonl').exists()else{}
        for entry in audit['records']:
            if entry['request_sha256']!=digest(jobs[entry['task_id']]):
                raise ValueError('review native request differs')
        latest={entry['task_id']:entry for entry in audit['records']}
        for task,entry in latest.items():
            if entry['classification']not in {'normal','recovered','excluded_intervention'}:raise ValueError('terminal review attempt is incomplete')
            result_path=run/ends[task]['result_path'];raw=load(result_path)
            from .subscription_health import excluded_intervention
            exclusion=excluded_intervention(jobs[task],raw,result_path.parent)
            responses[task]=dict(raw,native_completion_verified=False,native_intervention_excluded=exclusion)if exclusion else raw
        completion_binding=dict(native_ledger_sha256=audit['source_ledger_sha256'],native_audit_sha256=digest(audit['records']))
    replay_completed_review(p,cases,responses,r)
    binding=dict(review_package_path=str(path.absolute()),review_package_sha256=digest(p),report_sha256=digest((run/'report.json').read_bytes()),
                 question_ids=[q['question_id']for q in p['questions']],adjudication_profile=p['adjudication_profile'],
                 source_report_sha256=p['source_report_sha256'],**completion_binding)
    return binding,p,r,cases


def prepare_checkpoint(source_package,old_bundles,old_report,question_file,count,directory,*,ceiling='350',build_identity=None,accepted_profile=None):
    root=private_directory(directory);source=load(source_package);prior=load(old_report)
    components=[completed_bundle(p)for p in old_bundles]
    old_qs=[q for _,p,_,_ in components for q in p['questions']];old_ids={q['question_id']for q in old_qs}
    if len(old_ids)!=len(old_qs) or len(old_ids)!=20:raise ValueError('checkpoint requires disjoint completed20')
    saved={o['case_id']:o for o in prior['outcomes']};validated={o['case_id']:o for _,_,r,_ in components for o in r['outcomes']}
    if len(saved)!=len(prior['outcomes'])or saved.keys()!=validated.keys()or any((saved[c]['grade'],saved[c]['rank'])!=(v['grade'],v['rank'])for c,v in validated.items()):
        raise ValueError('prior aggregate differs from validated completed components')
    question_file=Path(question_file)
    selection_path=question_file.parent/'selection.json';manifest_path=question_file.parent/'VERIFICATION-MANIFEST.json'
    selection=load(selection_path);manifest=load(manifest_path);bound={r['path']:r['sha256']for r in manifest['files']}
    if bound.get(selection_path.name)!=digest(selection_path.read_bytes())or bound.get(question_file.name)!=digest(question_file.read_bytes()):raise ValueError('frozen question selection binding changed')
    order=list(records(question_file));questions=order[:count]
    phase=next((r for r in selection['phases']if r['phase']==2),None)
    if not phase or phase['question_ids']!=[q['question_id']for q in order]or phase['questions_sha256']!=digest(order):raise ValueError('question file differs from frozen phase2')
    if {q['question_id']for q in source['questions']}!=old_ids or set(selection['phases'][0]['question_ids'])!=old_ids:raise ValueError('source20 differs from frozen selection')
    if count!=10 or len(questions)!=count or old_ids&{q['question_id']for q in questions}:raise ValueError('exact next10 new questions required')
    if len({q['question_id']for q in questions})!=count:raise ValueError('duplicate new questions')
    cfg=deepcopy(source['configuration'])
    for k in ('interrupted_run_recovery','reuse_snapshot','prior_work','fresh_judging','benchmark_continuation_reuse','subscription_health_transition','codex_concurrency_trial','scale_expansion','reuse_scope'):
        cfg.pop(k,None)
    prior_cost=prior['phase_costs']
    baseline_cost=report_costs(source,Path(source['configuration']['run_directory']))
    ledger_costs=[baseline_cost]+[b['costs']for b,_,_,_ in components]
    expected_costs={k:str(sum((Decimal(c[k])for c in ledger_costs),Decimal(0)))for k in COST_KEYS}
    if any(Decimal(prior_cost[k])!=Decimal(v)for k,v in expected_costs.items()):raise ValueError('prior costs differ from retained accounting')
    if any(p['source_package_sha256']!=digest(source)for _,p,_,_ in components):raise ValueError('prior components use another source')
    if Decimal(ceiling)!=Decimal(source['configuration']['execution_policy']['spending_ceiling_usd']):raise ValueError('checkpoint cannot reset the shared ceiling')
    carried=Decimal(expected_costs['conservative_total_usd'])
    cfg.update(questions=questions,provider_ids=[p['configuration_id']for p in source['providers']],run_directory=str(root/'native/run'),
               automatic_recovery=deepcopy(POLICY),provider_request_profiles={'brave-web':BRAVE_UNDECORATED})
    cfg['execution_policy']['spending_ceiling_usd']=str(Decimal(ceiling)-carried)
    cache=cfg.pop('system_cache',None);cfg['execution_policy']['budget_accounting']='source-native-usage-v1'
    native=prepare(cfg)
    if cache:
        empty=dict(status='native_result',title=dict(state='missing'),content=dict(state='missing'),url=dict(state='missing'),metadata={},additional_content={},native_dates=dict(observations=[]))
        groups={}
        for q in questions:
            compiled=judging.compile_packet(judging.packet_for(q,native['providers'][0],empty,'cache-plan'),'2000-01-01T00:00:00Z',config=native['providers'][0])
            payload=caching.payload(judging.build_pro_request(compiled));groups[digest(payload)]=(len(encoded(payload))+3)//4
        cache['estimated_system_tokens']=groups;cache['renewal']=dict(profile='expiry-leases-v1',epoch=digest(dict(profile=PROFILE,questions=questions,source=digest(source))))
        cfg['system_cache']=cache;cfg['execution_policy']['budget_accounting']='source-native-cache-usage-v1';native=prepare(cfg)
    if native['primary']!=source['primary']or cfg['reviewers']!=source['configuration']['reviewers']:raise ValueError('grader settings changed')
    for expected in request_profiles(source['providers'],{'brave-web':BRAVE_UNDECORATED}):
        actual=next(n for n in native['providers']if n['configuration_id']==expected['configuration_id'])
        if actual!=expected:raise ValueError('provider settings differ from the agreed baseline')
    write_private(root/'native/config.json',cfg);write_private(root/'native/package.json',native)
    package=dict(profile=PROFILE,created_at=now(),build_identity=build_identity,acceptance=dict(one_launch='Collection, grading, review, audit and combined30 report without a manual finalizer',unclassified_native_completions=0,undeclared_duplicate_tasks=0,conservative_shared_ceiling_usd=str(ceiling),unknown_share='Observation, not an acceptance gate'),code=code_bindings(),assets=asset_bindings(),source_package_path=str(Path(source_package).absolute()),
                 source_package_sha256=digest(source),completed_components=[b for b,_,_,_ in components],
                 old_report_path=str(Path(old_report).absolute()),old_report_sha256=digest(Path(old_report).read_bytes()),old_costs=prior_cost,
                 old_questions=old_qs,new_questions=questions,selection_path=str(selection_path.absolute()),selection_sha256=digest(selection_path.read_bytes()),admission_manifest_path=str(manifest_path.absolute()),admission_manifest_sha256=digest(manifest_path.read_bytes()),question_order_path=str(Path(question_file).absolute()),question_order_sha256=digest(Path(question_file).read_bytes()),
                 native_package_sha256=digest(native),shared_ceiling_usd=str(ceiling),prior_committed_and_held_usd=str(carried),
                 review_derivation='Complete native evidence, current decoder and blind publication-policy-v2 reviews; no original20 dispatch',
                 stop='Report combined30 and stop. No other questions authorized.')
    if accepted_profile is not None:
        from .profile_authority import read
        package.update(accepted_profile=read(accepted_profile,cfg['reviewers']),
            accepted_review_directory=str(root.resolve().parent/(root.name+'-accepted-review')),
            accepted_review_task_bound=2*(sum(o['grade']is None for o in prior['outcomes'])+native['scope']['result_slots']))
    write_private(root/'checkpoint.json',package);return package


def verify_checkpoint(path):
    path=Path(path);p=load(path)
    if p.get('profile')=='additive-batch-v1':
        from .batch import verify as verify_batch
        return verify_batch(path)
    if p['profile']!=PROFILE or p['code']!=code_bindings()or p['assets']!=asset_bindings():raise ValueError('checkpoint code/assets changed')
    if digest(load(p['source_package_path']))!=p['source_package_sha256']or digest(Path(p['old_report_path']).read_bytes())!=p['old_report_sha256']or digest(Path(p['question_order_path']).read_bytes())!=p['question_order_sha256']:
        raise ValueError('checkpoint source changed')
    if digest(Path(p['selection_path']).read_bytes())!=p['selection_sha256']or digest(Path(p['admission_manifest_path']).read_bytes())!=p['admission_manifest_sha256']:raise ValueError('question admission binding changed')
    components=[]
    for expected in p['completed_components']:
        own=completed_bundle(expected['bundle_path'])
        if own[0]!=expected:raise ValueError('completed20 component changed')
        components.append(own)
    native=load(path.parent/'native/package.json')
    if digest(native)!=p['native_package_sha256']or native['questions']!=p['new_questions']:raise ValueError('native checkpoint changed')
    old_ids=[q['question_id']for q in p['old_questions']];new_ids=[q['question_id']for q in p['new_questions']]
    if len(old_ids)!=20 or len(new_ids)!=10 or len(set(old_ids+new_ids))!=30:raise ValueError('checkpoint question overlap/scope differs')
    if new_ids!=[q['question_id']for q in list(records(p['question_order_path']))[:10]]:raise ValueError('checkpoint no longer uses first10')
    if p.get('accepted_profile'):
        from .profile_authority import read
        if(read(p['accepted_profile']['package_path'],native['configuration']['reviewers'])!=p['accepted_profile']
                or p['accepted_review_directory']!=str(path.resolve().parent.parent/(path.parent.name+'-accepted-review'))
                or p['accepted_review_task_bound']!=2*(sum(o['grade']is None for o in load(p['old_report_path'])['outcomes'])+native['scope']['result_slots'])):
            raise ValueError('checkpoint accepted review scope changed')
    return p,native,components


def run_checkpoint(path,approval,*,env_file=None):
    from .ledger import Ledger
    path=Path(path)
    p=load(path)
    with Ledger(path.parent/'coordinator',dict(package_sha256=digest(p),mode='live')).locked():
        return _run_checkpoint(path,approval,env_file=env_file)


def _run_checkpoint(path,approval,*,env_file=None):
    path=Path(path);root=path.parent;p,native,components=verify_checkpoint(path)
    if approval.get('decision')!='approved'or approval.get('package_sha256')!=digest(p)or not approval.get('approved_by')or not approval.get('approved_at'):
        raise ValueError('exact checkpoint approval required')
    if p.get('profile')=='additive-batch-v1':
        if(root/'approval.json').exists():
            if load(root/'approval.json')!=approval:raise ValueError('batch approval changed')
        else:write_private(root/'approval.json',approval)
    if p.get('accepted_profile')and(root/'closure.json').exists():
        from .settlement import load_parent
        load_parent(root/'report/report.json')
        return _finish_accepted(root,p,approval)
    ordinary=dict(approval,package_sha256=digest(native),parent_checkpoint_sha256=digest(p))
    write_private(root/'native/approval.json',ordinary,replace=True)
    native_report=root/'native/run/report.json'
    cached=load(native_report)if native_report.exists()else None
    if cached and cached.get('execution',{}).get('state')=='completed':
        if cached['package_sha256']!=digest(native):raise ValueError('completed native package binding changed')
        result=cached
    else:
        if (root/'review/package.json').exists():raise ValueError('bound review exists without completed native source')
        result=run(native,approval=ordinary,env_file=env_file)
    if result['execution']['state']!='completed':return dict(stage='native',execution=result['execution'])
    native_audit=inspect_ledger(Path(native['configuration']['run_directory']))
    write_private(root/'native-completion-audit.json',native_audit,replace=True)
    if native_audit['unclassified']:raise ValueError('ordinary subscription completion has no audited terminal proof')
    review_path=root/'review/package.json'
    if not review_path.exists():prepare_review(root/'native/package.json',[q['question_id']for q in p['new_questions']],root/'review')
    child=load(review_path)
    with tempfile.TemporaryDirectory(prefix='openweb-checkpoint-derivation-')as temp:
        expected=prepare_review(root/'native/package.json',[q['question_id']for q in p['new_questions']],temp)
        normalize=lambda v:{k:x for k,x in v.items()if k not in {'created_at','run_directory'}}
        if normalize(child)!=normalize(expected)or Path(child['run_directory']).resolve()!=(root/'review/run').resolve():raise ValueError('review child does not derive from approved native inputs')
    receipt=dict(approved=True,package_sha256=digest(child),parent_checkpoint_sha256=digest(p),approved_by=approval['approved_by'],approved_at=approval['approved_at'])
    write_private(root/'review/approval.json',receipt,replace=True)
    review=run_review(review_path,approval=receipt)
    if review['execution']['state']!='completed':return dict(stage='review',execution=review['execution'])
    extra=completed_review(review_path);components.append(extra)
    generic=p['profile']=='additive-batch-v1'
    if generic:
        from .batch import combined_costs
        current=combined_costs(p,native)
    else:
        costs=report_costs(native,Path(native['configuration']['run_directory']))
        current=deepcopy(p['old_costs']);current.update({k:str(Decimal(p['old_costs'][k])+Decimal(costs[k]))for k in COST_KEYS})
        current['components']=p['old_costs']['components']+[dict(name='new10',**costs)]
    combined=combine(components,p,native,current)
    write_reports(private_directory(root/'report'),combined)
    # Internal cohort views are separate from the single human report.
    for name,items in ((('parent',components[:-1]),('batch-'+str(len(p['new_questions'])),components[-1:]))if generic else(('original20',components[:-1]),('new10',components[-1:]))):
        report=combine(items,p,native,current)
        write_reports(private_directory(root/'diagnostics'/name),report)
    write_private(root/'closure.json',dict(profile=p['profile'],checkpoint_sha256=digest(p),report_sha256=digest((root/'report/report.json').read_bytes()),
                  execution='completed',question_count=len(combined['questions']),original_results_unchanged=True,components=[b for b,_,_,_ in components],costs=current),replace=True)
    if p.get('accepted_profile'):return _finish_accepted(root,p,approval)
    return dict(state='completed',questions=30,outcome_counts=combined['outcome_counts'],costs=current,report=str(root/'report/report.html'))


def _finish_accepted(root,p,approval):
    from . import accepted_review
    root=Path(root).resolve()
    destination=Path(p['accepted_review_directory']);path=destination/'review-checkpoint.json'
    if not path.exists():
        accepted_review.prepare(root/'report/report.json',p['accepted_profile']['package_path'],destination,
            allow_inference=True,maximum_tasks=p['accepted_review_task_bound'])
    child=load(path)
    if(child['parent_report_path']!=str((root/'report/report.json').resolve())
            or child['accepted_profile']!=p['accepted_profile']or not child['allow_inference']
            or child['maximum_review_tasks']!=p['accepted_review_task_bound']or child['mode']!='live'):
        raise ValueError('accepted review child differs from checkpoint scope')
    receipt=dict(approved=True,package_sha256=digest(child),parent_checkpoint_sha256=digest(p),
        approved_by=approval['approved_by'],approved_at=approval['approved_at'])
    report=accepted_review.run(path,receipt)
    execution=load(destination/'closure.json')['execution']
    result=dict(state=execution['state'],stage='accepted_review',questions=len(report['questions']),
        outcome_counts=report['outcome_counts'],costs=report['phase_costs'],report=str(destination/'report/report.html'))
    write_private(root/'pipeline-closure.json',dict(checkpoint_sha256=digest(p),
        accepted_review_package_sha256=digest(child),accepted_review_closure_sha256=digest(load(destination/'closure.json')),
        execution=execution,report=result['report']),replace=True)
    return result


def combine(components,identity,native,costs):
    cases={};outcomes=[];collection=[];questions=[];bindings=[];policies=[];seen=set()
    for binding,p,r,own in components:
        qids={q['question_id']for q in p['questions']}
        overlap=qids&seen
        known={q['question_id']:q for q in questions}
        if (cases.keys()&own.keys() or overlap and (not identity.get('recollection_source')
                or any(known[q['question_id']]!=q for q in p['questions']if q['question_id']in overlap))):
            raise ValueError('overlapping report components')
        questions += [q for q in p['questions']if q['question_id']not in seen]
        seen|=qids;cases.update(own);outcomes+=deepcopy(r['outcomes']);collection+=p['source_collection'];bindings.append(binding);policies+=deepcopy(r['review_pipeline']['records'])
    configs=[p['configuration_id']for p in native['providers']];cells={}
    for row in collection:
        key=row['configuration_id'],row['question_id']
        if key in cells:raise ValueError('duplicate collection cell')
        ranked=[dict(rank=o['rank'],evidence_id=o['case_id'],url=cases[o['case_id']]['compiled']['packet']['returned_url'])for o in sorted(outcomes,key=lambda v:v['rank'])if(o['configuration_id'],o['question_id'])==key]
        cells[key]=dict(status=row['status'],ranked=ranked)
    expected={(c,q) for c in configs for q in seen}
    search_ids=set(native['configuration'].get('search_provider_ids',configs))
    deferred={(c,q['question_id']) for c in configs if c not in search_ids for q in native['questions']} if not identity.get('recollection_source') else set()
    if set(cells) != expected-deferred:
        raise ValueError('combined collection scope differs from declared deferred cells')
    for cid,qid in sorted(deferred):
        cells[cid,qid]=dict(status='not_collected',ranked=[])
        collection.append(dict(configuration_id=cid,question_id=qid,status='not_collected',deferred=True))
    labels={o['case_id']:o['grade']for o in outcomes};reasons={o['case_id']:o['reason']for o in outcomes};findings={}
    for o in outcomes:
        pos=o['positions'].get(o['selected_roles'][0],{})if o['selected_roles']else{}
        j=pos.get('conformance_judgment')or pos.get('judgment')or{}
        findings[o['case_id']]=[dict(report_id=None,event_id=None,facet_ids=c['supported_facet_ids'])for c in j.get('candidates',[])if c['answer_supported']]if o['grade']==2 else[]
    cfg=native['configuration'];scores=scoring.score(questions,configs,cells,labels,reasons,cfg['coverage_target'],cfg['tie_order'],findings=findings)
    pairs=[]
    for k in(3,5,10):
        for metric in('answer','useful'):
            values={(r['question_id'],r['configuration_id']):r['metrics'][metric]for r in scores['question_rows']if r['k']==k and r['metrics']is not None}
            for a,b in combinations(configs,2):pairs.append(dict(metric=f'{metric}@{k}',**scoring.paired(questions,a,b,scores['availability']['answered_question_ids'],values)))
    inventory={}
    for c in configs:
        own=[v['compiled']['input']for v in cases.values()if v['original']['configuration_id']==c];times=sorted(v['captured_at']for v in own)
        inventory[c]=dict(field_states={f:dict(Counter(v['native_field_states'].get(f,'missing')for v in own))for f in('title','snippet')},earliest_capture=times[0]if times else None,latest_capture=times[-1]if times else None)
    result=dict(method_version=components[0][1]['source_method'],mode=identity.get('profile',PROFILE),package_sha256=digest(identity),generated_at=now(),questions=questions,outcomes=outcomes,collection=collection,scores=scores,resolved_pairwise=pairs,
                outcome_counts=dict(Counter('unknown'if o['grade']is None else str(o['grade'])for o in outcomes)),execution=dict(state='completed',held_tasks=[],reused_tasks=0),phase_costs=costs,phase_components=bindings,evidence_inventory=inventory,
                review_pipeline=dict(records=policies,final_unknowns=sum(o['grade']is None for o in outcomes)),
                report_limits=['Grades are model-reviewed, not human accuracy labels. Unknowns remain unknown.',
                               'Original review versions and actual collection times are preserved. Later captures are not a controlled quality comparison.',
                               'Provider nDCG uses its own resolved questions; coverage differs. Distinct and unique stories are not measured yet.',
                               'The original20 are retained without repeated calls. The checkpoint stops at30.'])
    if deferred:
        result['deferred_collection']=[dict(configuration_id=c,question_id=q) for c,q in sorted(deferred)]
        result['report_limits'].append('Declared deferred searches were not collected; they provide no provider-quality evidence. Delivered utility retains normal missing-delivery accounting.')
    if identity.get('profile')=='additive-batch-v1':
        parent=components[0][2]
        for key in ('checker_acceptance','publication_review'):
            if key in parent:result[key]=deepcopy(parent[key])
        result['active_review_profile']=identity['accepted_profile']
        result['report_limits'][-1]=f'The parent results are retained without repeated calls. This batch stops at {len(questions)} questions.'
    if identity.get('live_profiles'):result['live_profiles']=deepcopy(identity['live_profiles'])
    return result
