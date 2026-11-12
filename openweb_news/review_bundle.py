"""One prepared pipeline for a request-profile change plus saved-evidence review."""
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
import tempfile
from collections import Counter

from . import caching, judging
from .common import code_bindings,asset_bindings,digest,load,now,write_private,private_directory
from .preparation import prepare
from .request_profiles import BRAVE_UNDECORATED
from .resilience import POLICY
from .scheduling import account_bindings
from .review_pipeline import prepare_review,run_review
from .runner import run

PROFILE="brave-and-policy-review-bundle-v1"


def prepare_bundle(source_package,question_ids,directory):
    source_path=Path(source_package).absolute();source=load(source_path)
    root=private_directory(directory)
    questions=[q for q in source['questions']if q['question_id']in question_ids]
    if len(questions)!=len(question_ids)or len(set(question_ids))!=len(question_ids):
        raise ValueError('distinct source questions required')
    cfg=deepcopy(source['configuration'])
    for key in ('interrupted_run_recovery','reuse_snapshot','prior_work','fresh_judging','benchmark_continuation_reuse','subscription_health_transition','codex_concurrency_trial','scale_expansion','reuse_scope'):
        cfg.pop(key,None)
    cfg.update(questions=questions,provider_ids=['brave-web'],tie_order=['brave-web'],
               run_directory=str(root/'brave/run'),automatic_recovery=deepcopy(POLICY),
               provider_request_profiles={'brave-web':BRAVE_UNDECORATED})
    cfg['provider_projection_profiles']={k:v for k,v in cfg.get('provider_projection_profiles',{}).items()if k=='brave-web'}
    provider=next(p for p in source['providers']if p['configuration_id']=='brave-web')
    selected_accounts=set(account_bindings([provider],cfg['reviewers'],source['primary']).values())
    schedule=cfg['execution_policy']['scheduling']
    for name in ('accounts','recovery'):
        schedule[name]={k:v for k,v in schedule[name].items()if k in selected_accounts}
    report=load(Path(source['configuration']['run_directory'])/'report.json')
    if report['package_sha256']!=digest(source):
        raise ValueError('source report does not belong to the selected package')
    carried=Decimal(report['execution']['committed_and_held_usd'])
    # Preserve the parent's total envelope rather than resetting its spent amount.
    total_ceiling=Decimal(str(cfg['execution_policy']['spending_ceiling_usd']))
    cfg['execution_policy']['spending_ceiling_usd']=str(total_ceiling-carried)
    cache=cfg.pop('system_cache',None)
    cfg['execution_policy']['budget_accounting']='source-native-usage-v1'
    base=prepare(cfg)
    if cache:
        empty=dict(status='native_result',title=dict(state='missing'),content=dict(state='missing'),url=dict(state='missing'),metadata={},additional_content={},native_dates=dict(observations=[]))
        groups={}
        for q in questions:
            wire=judging.build_pro_request(judging.compile_packet(judging.packet_for(q,base['providers'][0],empty,'cache-plan'),'2000-01-01T00:00:00Z',config=base['providers'][0]))
            payload=caching.payload(wire);key=digest(payload)
            groups[key]=cache['estimated_system_tokens'].get(key,(len(str(payload).encode())+3)//4)
        cache['estimated_system_tokens']=groups
        if cache.get('renewal'):
            cache['renewal']['epoch']=digest(dict(bundle=PROFILE,questions=question_ids,source=digest(source)))
        cfg['system_cache']=cache
        cfg['execution_policy']['budget_accounting']='source-native-cache-usage-v1'
    brave=prepare(cfg)
    write_private(root/'brave/config.json',cfg)
    write_private(root/'brave/package.json',brave)
    baseline=prepare_review(source_path,question_ids,root/'baseline')
    bundle=dict(profile=PROFILE,created_at=now(),source_package=str(source_path),source_package_sha256=digest(source),
                source_report_sha256=digest((Path(source['configuration']['run_directory'])/'report.json').read_bytes()),
                question_ids=[q['question_id']for q in questions],question_hashes={q['question_id']:digest(q)for q in questions},
                code=code_bindings(),assets=asset_bindings(),brave_package_sha256=digest(brave),
                baseline_review_package_sha256=digest(baseline),
                request_profile=BRAVE_UNDECORATED,representation='markdown-escapes-v1',automatic_recovery=deepcopy(POLICY),
                maximum_initial_searches=len(questions),maximum_new_pro_positions=len(questions)*10,
                prior_committed_and_held_usd=str(carried),shared_ceiling_usd=str(total_ceiling),
                review_stage='deterministic child derived from this package, exact own source and completed Brave stage',
                continuation='Stop at the selected-question report; no remaining phase1 Brave recollection or phase2 execution')
    write_private(root/'bundle.json',bundle)
    return bundle


def run_bundle(bundle_path,approval,*,env_file=None):
    path=Path(bundle_path);root=path.parent;bundle=load(path)
    if (bundle['profile']!=PROFILE or approval.get('decision')!='approved' or approval.get('package_sha256')!=digest(bundle)
            or not approval.get('approved_by')or not approval.get('approved_at')
            or bundle['code']!=code_bindings()or bundle['assets']!=asset_bindings()):
        raise ValueError('exact approved bundle and unchanged code/assets required')
    source=load(bundle['source_package'])
    if digest(source)!=bundle['source_package_sha256']or digest((Path(source['configuration']['run_directory'])/'report.json').read_bytes())!=bundle['source_report_sha256']:
        raise ValueError('bundle source changed')
    brave=load(root/'brave/package.json')
    if digest(brave)!=bundle['brave_package_sha256']:
        raise ValueError('Brave stage binding changed')
    baseline=load(root/'baseline/package.json')
    if digest(baseline)!=bundle['baseline_review_package_sha256']:
        raise ValueError('approved source replay snapshot changed')
    from .review_pipeline import verify as verify_review
    verify_review(baseline,root/'baseline')
    child_approval=dict(approval,package_sha256=digest(brave),parent_bundle_sha256=digest(bundle),approval_kind='derived_from_approved_bundle')
    write_private(root/'brave/approval.json',child_approval,replace=True)
    native=run(brave,approval=child_approval,env_file=env_file)
    if native['execution']['state']!='completed':
        return dict(stage='brave',execution=native['execution'],continuation_required=True)
    child_path=root/'review/package.json'
    if not child_path.exists():
        prepare_review(bundle['source_package'],bundle['question_ids'],root/'review',replacement_package=root/'brave/package.json')
    child=load(child_path)
    # A claimed parent hash is not a derivation proof. Recompute the entire
    # child and its copied evidence before granting any review dispatch.
    with tempfile.TemporaryDirectory(prefix='openweb-review-derivation-')as temp:
        expected=prepare_review(bundle['source_package'],bundle['question_ids'],temp,replacement_package=root/'brave/package.json')
        normalized=lambda p:{k:v for k,v in p.items()if k not in {'created_at','run_directory'}}
        if normalized(child)!=normalized(expected)or Path(child['run_directory']).absolute()!=root.absolute()/'review/run':
            raise ValueError('review child does not reproduce the approved derivation')
    frozen_cases={c['case_id']:c['sha256']for c in baseline['cases']}
    for ref in child['cases']:
        own=load(root/'review'/ref['path'])
        if own['original']['configuration_id']!='brave-web'and frozen_cases.get(ref['case_id'])!=ref['sha256']:
            raise ValueError('non-recollected evidence changed from approved snapshot')
    if (child['source_package_sha256']!=bundle['source_package_sha256']or child['replacement_package_sha256']!=digest(brave)
            or [q['question_id']for q in child['questions']]!=bundle['question_ids']
            or any(digest(q)!=bundle['question_hashes'][q['question_id']]for q in child['questions'])
            or child['reviewers']!=brave['configuration']['reviewers']):
        raise ValueError('review stage is outside the approved derivation')
    review_approval=dict(approved=True,package_sha256=digest(child),parent_bundle_sha256=digest(bundle),
                         approved_by=approval['approved_by'],approved_at=approval['approved_at'],approval_kind='derived_from_approved_bundle')
    write_private(root/'review/approval.json',review_approval,replace=True)
    result=run_review(child_path,approval=review_approval)
    return summarize_bundle(root,bundle,brave,baseline,child,native,result)


def provider_unknown_shares(scores):
    out={}
    for cid,table in scores['accounting'].items():
        row=table.get(10,table.get('10'))
        out[cid]=dict(unknown=row['counts'].get('unresolved',0),slots=row['slots'],share=row['unresolved_share'])
    return out


def summarize_bundle(root,bundle,brave,baseline,child,native,result,*,output=None):
    from .accounting import report_costs
    costs=report_costs(brave,Path(brave['configuration']['run_directory']))
    outcomes=result['review_pipeline']['records']
    dispositions=Counter('accepted'if r['accepted']else'rule_question'if'rule_question'in r['dispositions'].values()else'unresolved'for r in outcomes)
    quote_counts={}
    for cid in ('brave-web','seltz-web-pro'):
        before=[load(root/'baseline'/ref['path'])['original']for ref in baseline['cases']if load(root/'baseline'/ref['path'])['original']['configuration_id']==cid]
        after=[o for o in result['outcomes']if o['configuration_id']==cid]
        failed=lambda rows:sum(any('quote absent or repeated occurrence unresolved'in str(e)for e in o['positions']['pro'].get('errors',[]))for o in rows)
        quote_counts[cid]=dict(before_pro_span_failures=failed(before),after_pro_span_failures=failed(after))
    summary=dict(profile=PROFILE,bundle_sha256=digest(bundle),questions=bundle['question_ids'],
                 source_report_sha256=bundle['source_report_sha256'],brave_report_sha256=digest((root/'brave/run/report.json').read_bytes()),
                 review_report_sha256=digest((root/'review/run/report.json').read_bytes()),
                 baseline_unknowns=sum(o['grade']is None for o in child['reference_outcomes']),
                 replacement_unknowns=result['review_pipeline']['original_unknowns'],
                 final_unknowns=result['review_pipeline']['final_unknowns'],changes=result['review_pipeline']['changes'],
                 settlements=dict(dispositions),pro_span_failures=quote_counts,
                 comparison_summary=dict(count=len(result['resolved_pairwise']),with_resolved_families=sum(bool(r['family_differences'])for r in result['resolved_pairwise']),details='review/run/pairwise.csv'),
                 new_brave_searches=len(native['collection']),new_pro_positions=len(native['outcomes']),
                 costs=costs,prior_committed_and_held_usd=bundle['prior_committed_and_held_usd'],
                 shared_ceiling_usd=bundle['shared_ceiling_usd'],execution=result['execution'],
                 provider_unknown_shares=provider_unknown_shares(result['scores']),
                 limits=['Enriched feasibility sample, not population accuracy or an unbiased recovery rate.',
                         'Brave was recollected later; changed results are not a controlled markup-only effect.',
                         'Scoped corroborated policy reviews are not human accuracy labels.',
                         'Original results remain immutable; all providers are rescored on this derived roster.',
                         'No URL schema, date parser, primary judging prompt change or provider removal.'])
    write_private((Path(output)if output else root)/'BUNDLE-REPORT.json',summary,replace=True)
    return summary


def finalize_bundle_report(bundle_path,output):
    """Explicit offline reporter repair over completed hash-bound stages only."""
    from .common import records
    from .review_pipeline import verify,decode
    from .subscription_health import verified
    from .adjudication import eligible,packet_for,corroborate
    from .review import settle
    from . import representation
    root=Path(bundle_path).parent;bundle=load(bundle_path);approval=load(root/'approval.json')
    current=code_bindings()
    changed={k for k in set(bundle['code'])|set(current)if bundle['code'].get(k)!=current.get(k)}
    if (bundle['profile']!=PROFILE or approval.get('decision')!='approved'or approval.get('package_sha256')!=digest(bundle)
            or bundle['assets']!=asset_bindings()or not changed<={'review_bundle.py','cli.py'}):
        raise ValueError('offline repair is limited to bundle reporting/CLI code')
    brave=load(root/'brave/package.json');baseline=load(root/'baseline/package.json');child=load(root/'review/package.json')
    if digest(brave)!=bundle['brave_package_sha256']or digest(baseline)!=bundle['baseline_review_package_sha256']:
        raise ValueError('frozen stage package changed')
    for name,package in (('brave',brave),('review',child)):
        receipt=load(root/name/'approval.json')
        accepted=receipt.get('decision')=='approved'if name=='brave'else receipt.get('approved')is True
        if (not accepted or receipt.get('package_sha256')!=digest(package)
                or receipt.get('parent_bundle_sha256')!=digest(bundle)
                or receipt.get('approved_by')!=approval.get('approved_by')):
            raise ValueError('stage approval is outside the frozen parent')
    native=load(root/'brave/run/report.json');result=load(root/'review/run/report.json');closure=load(root/'review/run/closure.json')
    if (native['package_sha256']!=digest(brave)or result['package_sha256']!=digest(child)
            or native['execution']['state']!='completed'or result['execution']['state']!='completed'
            or closure['report_sha256']!=digest((root/'review/run/report.json').read_bytes())
            or child['source_package_sha256']!=bundle['source_package_sha256']or child['replacement_package_sha256']!=digest(brave)):
        raise ValueError('only completed, bound reports can be finalized offline')
    # The original decision modules stay byte-identical. Only their reporting
    # caller changed; no patched metadata is used to authorize live execution.
    cases=verify(dict(child,code=current),root/'review')
    verify(dict(baseline,code=current),root/'baseline')
    frozen={ref['case_id']:ref['sha256']for ref in baseline['cases']}
    for ref in child['cases']:
        if cases[ref['case_id']]['original']['configuration_id']!='brave-web'and frozen.get(ref['case_id'])!=ref['sha256']:
            raise ValueError('retained provider evidence differs from the approved baseline')
    responses={};audited=0
    for stage,package in (('brave',brave),('review',child)):
        directory=root/stage/'run';events=list(records(directory/'attempts.jsonl'))
        starts={e['attempt_id']:e for e in events if e['event']=='started'};ends={e['attempt_id']:e for e in events if e['event']=='finished'}
        if starts.keys()!=ends.keys():raise ValueError('stage has unfinished native work')
        for aid,event in starts.items():
            end=ends[aid];folder=directory/'attempts'/aid;request=load(folder/'request.json');raw=load(directory/end['result_path'])
            if digest(request)!=event['request_sha256']or digest((directory/end['result_path']).read_bytes())!=end['result_sha256']:
                raise ValueError('native ledger request/result changed')
            if request.get('kind')=='model'and request.get('backend','').endswith('-subscription'):
                if not verified(request,raw,folder):raise ValueError('native subscription proof changed')
                audited+=1
            if stage=='review':responses[event['task_id']]=raw
    stored={r['case_id']:r for r in result['outcomes']}
    for cid,case in cases.items():
        positions,replayed=decode(case,child['text_profile'])
        own={role:responses['adjudication-'+role+'-'+cid]for role in ('codex','claude')if'adjudication-'+role+'-'+cid in responses}
        if own:
            ids=eligible(case['compiled'],positions,case['provider'])
            with representation.selected(child['text_profile']):
                effective,record=corroborate(case['compiled'],positions,packet_for(case['compiled'],ids),own,case['provider'])
            if record['accepted']:replayed=settle(effective['pro'],effective['codex'])
        extra=responses.get('third-claude-'+cid)
        if extra:raise ValueError('this reporting repair requires an already replayable base route')
        if replayed['grade']!=stored[cid]['grade']:raise ValueError('offline verification changed a grade')
    output=Path(output).absolute()
    if output==root.absolute()or output.is_relative_to(root.absolute()):raise ValueError('repair output must preserve original bundle')
    summary=summarize_bundle(root,bundle,brave,baseline,child,native,result,output=output)
    write_private(output/'REPAIR-RECEIPT.json',dict(profile='offline-bundle-report-repair-v1',source_bundle_sha256=digest(bundle),
                  repaired_code_files=sorted(changed),current_code=current,original_stage_reports_unchanged=True,
                  verified_native_subscription_completions=audited,replayed_outcomes=len(cases),new_model_calls=0,new_search_calls=0,
                  source_report_sha256=digest((root/'review/run/report.json').read_bytes()),final_report_sha256=digest((output/'BUNDLE-REPORT.json').read_bytes()),completed_at=now()))
    return summary
