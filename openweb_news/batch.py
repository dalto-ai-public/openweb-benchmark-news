"""Add the next declared questions to a verified, immutable benchmark report."""
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

from . import caching, checkpoint, judging, profile_authority, settlement
from .common import asset_bindings,code_bindings,digest,encoded,load,now,records,write_private
from .preparation import prepare as prepare_native

PROFILE='additive-batch-v1'


def frozen_order(selection_path):
    path=Path(selection_path).resolve();manifest=path.parent/'VERIFICATION-MANIFEST.json'
    selection=load(path);bound={r['path']:r['sha256']for r in load(manifest)['files']}
    bank=path.parent/'bank.jsonl'
    if (bound.get(path.name)!=digest(path.read_bytes())or bound.get(bank.name)!=digest(bank.read_bytes())
            or selection['bank_sha256']!=digest(bank.read_bytes())):
        raise ValueError('frozen question admission changed')
    rows=list(records(bank));by_id={q['question_id']:q for q in rows};ids=selection['question_order']
    if len(by_id)!=len(rows)or len(set(ids))!=len(ids)or set(ids)!=set(by_id):
        raise ValueError('frozen order must contain the complete unique bank')
    phase_ids=[]
    for phase in selection['phases']:
        file=path.parent/('phase-'+str(phase['phase'])+'-questions.jsonl');questions=list(records(file))
        if (bound.get(file.name)!=digest(file.read_bytes())or digest(questions)!=phase['questions_sha256']
                or [q['question_id']for q in questions]!=phase['question_ids']
                or any(q!=by_id[q['question_id']]for q in questions)):
            raise ValueError('phase differs from frozen bank/order')
        phase_ids+=phase['question_ids']
    if phase_ids!=ids:raise ValueError('phase order differs from frozen order')
    return [by_id[q]for q in ids],dict(selection_path=str(path),selection_sha256=digest(path.read_bytes()),
        admission_manifest_path=str(manifest),admission_manifest_sha256=digest(manifest.read_bytes()))


def next_questions(prior,order,count):
    if type(count)is not int or count<=0:raise ValueError('batch size must be positive')
    old=prior['questions'];n=len(old);ids=[q['question_id']for q in old]
    if len(set(ids))!=n or set(ids)!={q['question_id']for q in order[:n]}:
        raise ValueError('parent must contain exactly the completed frozen prefix')
    by_id={q['question_id']:q for q in order[:n]}
    if any(q!=by_id[q['question_id']]for q in old):raise ValueError('parent question bytes differ')
    new=order[n:n+count]
    if len(new)!=count:raise ValueError('batch exceeds remaining frozen questions')
    return new


def parent_component(path,evidence):
    report,cases,_=evidence
    binding=dict(parent_report_path=str(Path(path).resolve()),parent_report_sha256=digest(Path(path).read_bytes()),
        question_ids=[q['question_id']for q in report['questions']])
    p=dict(questions=report['questions'],source_method=report['method_version'],source_collection=report['collection'])
    return binding,p,report,cases


def native_plan(template,questions,root,ceiling,carried,*,authority=None,mode='live',search_provider_ids=None):
    cfg=deepcopy(template['configuration'])
    cfg.pop('live_profile_authority',None)
    if authority is not None and profile_authority.profiles(authority):
        cfg['live_profile_authority']=deepcopy(authority)
        if authority.get('profile')==profile_authority.METHOD_PROFILE:
            for key in profile_authority.method_options(cfg):cfg.pop(key,None)
            cfg.update(deepcopy(authority['method_options']))
    for key in ('interrupted_run_recovery','reuse_snapshot','prior_work','fresh_judging','benchmark_continuation_reuse',
            'subscription_health_transition','codex_concurrency_trial','scale_expansion','reuse_scope','cache_expiry_recovery'):
        cfg.pop(key,None)
    cfg.update(questions=questions,provider_ids=[p['configuration_id']for p in template['providers']],run_directory=str(root/'native/run'))
    if search_provider_ids is not None:cfg['search_provider_ids']=list(search_provider_ids)
    remaining=Decimal(ceiling)-Decimal(carried)
    if not remaining.is_finite()or remaining<=0:raise ValueError('ceiling must exceed carried costs')
    cfg['execution_policy']['spending_ceiling_usd']=str(remaining)
    cache=cfg.pop('system_cache',None)
    if cache:cfg['execution_policy']['budget_accounting']='source-native-usage-v1'
    native=prepare_native(cfg,mode=mode)
    if cache:
        empty=dict(status='native_result',title=dict(state='missing'),content=dict(state='missing'),url=dict(state='missing'),metadata={},additional_content={},native_dates=dict(observations=[]))
        groups={}
        for q in questions:
            compiled=judging.compile_packet(judging.packet_for(q,native['providers'][0],empty,'cache-plan'),'2000-01-01T00:00:00Z',config=native['providers'][0])
            payload=caching.payload(judging.build_pro_request(compiled));groups[digest(payload)]=(len(encoded(payload))+3)//4
        cache['estimated_system_tokens']=groups
        cache['renewal']=dict(cache.get('renewal')or dict(profile='expiry-leases-v1'),
            epoch=digest(dict(profile=PROFILE,questions=questions,template=digest(template))))
        cfg['system_cache']=cache;cfg['execution_policy']['budget_accounting']='source-native-cache-usage-v1';native=prepare_native(cfg,mode=mode)
    if (native['providers']!=template['providers']or native['primary']!=template['primary']
            or native['configuration']['reviewers']!=template['configuration']['reviewers']):
        raise ValueError('batch changed provider or grader settings')
    return cfg,native


def prepare(parent_report,selection_path,count,directory,*,ceiling,accepted_profile,build_identity=None):
    source=Path(parent_report).resolve();root=Path(directory).resolve()
    if root.is_relative_to(source.parent.parent)or source.is_relative_to(root):raise ValueError('batch must preserve parent')
    evidence=settlement.load_parent(source);prior,_,template=evidence
    order,admission=frozen_order(selection_path);questions=next_questions(prior,order,count)
    authority=profile_authority.read(accepted_profile,template['configuration']['reviewers'])
    if not parent_authority(prior,digest(source.read_bytes()),authority):
        raise ValueError('parent must carry the selected accepted review authority')
    carried=prior['phase_costs']['conservative_total_usd']
    cfg,native=native_plan(template,questions,root,ceiling,carried,authority=authority)
    forecast=cost_forecast(template,count,prior['phase_costs'])
    p=dict(profile=PROFILE,created_at=now(),code=code_bindings(),assets=asset_bindings(),build_identity=build_identity,
        parent_report_path=str(source),parent_report_sha256=digest(source.read_bytes()),template_sha256=digest(template),
        output_directory=str(root),old_questions=prior['questions'],new_questions=questions,
        old_costs=prior['phase_costs'],prior_committed_and_held_usd=carried,shared_ceiling_usd=str(Decimal(ceiling)),
        native_package_sha256=digest(native),accepted_profile=authority,
        cost_forecast=forecast,
        accepted_review_directory=str(root.parent/(root.name+'-accepted-review')),
        accepted_review_task_bound=2*(sum(o['grade']is None for o in prior['outcomes'])+native['scope']['result_slots']),
        acceptance=dict(unclassified_native_completions=0,undeclared_duplicate_tasks=0,
            unknown_share='Observation, not an acceptance gate',one_launch='Collection, ordinary judging, saved-evidence review, audit, accepted-profile review and pooled report'),
        stop=f'Report all {len(prior["questions"])+count} questions and stop; no further batch authorized.',**admission)
    if profile_authority.profiles(authority):p['live_profiles']=profile_authority.profiles(authority)
    write_private(root/'native/config.json',cfg);write_private(root/'native/package.json',native)
    write_private(root/'checkpoint.json',p)
    write_private(root/'approval-template.json',dict(decision='NOT_APPROVED',package_sha256=digest(p),
        approved_by='',approved_at='',scope=p['stop'],shared_ceiling_usd=p['shared_ceiling_usd']))
    write_private(root/'APPROVAL-REVIEW.md',approval_review(p,native).encode())
    return p


def verify(path,*,historical=False,seen=()):
    path=Path(path).resolve();root=path.parent;p=load(path);source=Path(p['parent_report_path']).resolve()
    if (p['profile']!=PROFILE or p['output_directory']!=str(root)
            or root.is_relative_to(source.parent.parent)or source.is_relative_to(root)
            or digest(source.read_bytes())!=p['parent_report_sha256']
            or(not historical and(p['code']!=code_bindings()or p['assets']!=asset_bindings()))
            or not all(asset_bindings().get(k)==v for k,v in p['assets'].items())):
        raise ValueError('batch bindings changed')
    evidence=settlement.load_parent(source,seen=seen);prior,_,template=evidence
    order,admission=frozen_order(p['selection_path'])
    if (any(p[k]!=v for k,v in admission.items())or digest(template)!=p['template_sha256']
            or prior['questions']!=p['old_questions']or prior['phase_costs']!=p['old_costs']
            or p['new_questions']!=next_questions(prior,order,len(p['new_questions']))
            or p['prior_committed_and_held_usd']!=prior['phase_costs']['conservative_total_usd']):
        raise ValueError('batch parent/order/accounting changed')
    authority=profile_authority.read(p['accepted_profile']['package_path'],template['configuration']['reviewers'])
    if (authority!=p['accepted_profile']or not parent_authority(prior,p['parent_report_sha256'],authority)
            or p.get('live_profiles',{})!=profile_authority.profiles(authority)
            or p['accepted_review_directory']!=str(root.parent/(root.name+'-accepted-review'))):
        raise ValueError('batch accepted profile changed')
    if p.get('native_recovery'):
        from .cache_recovery import expected_native
        expected=expected_native(p,root,evidence,authority)
    else:
        _,expected=native_plan(template,p['new_questions'],root,p['shared_ceiling_usd'],p['prior_committed_and_held_usd'],authority=authority)
    native=load(root/'native/package.json')
    if historical:expected.update(code=native['code'],assets=native['assets'])
    if (native!=expected or digest(native)!=p['native_package_sha256']
            or p['cost_forecast']!=cost_forecast(template,len(p['new_questions']),prior['phase_costs'])
            or p['accepted_review_task_bound']!=2*(sum(o['grade']is None for o in prior['outcomes'])+native['scope']['result_slots'])):
        raise ValueError('batch native plan changed')
    return p,native,[parent_component(source,evidence)]


def load_completed(path,*,seen=()):
    path=Path(path).resolve();root=path.parent.parent
    p,native,components=verify(root/'checkpoint.json',historical=True,seen=(*seen,path))
    closure=load(root/'closure.json');approval=load(root/'approval.json');report=load(path)
    if (approval.get('decision')!='approved'or approval.get('package_sha256')!=digest(p)
            or not approval.get('approved_by')or not approval.get('approved_at')
            or closure['checkpoint_sha256']!=digest(p)or closure['report_sha256']!=digest(path.read_bytes())
            or closure['execution']!='completed'or report['execution']['state']!='completed'):
        raise ValueError('batch completion proof differs')
    extra=checkpoint.completed_review(root/'review/package.json');components.append(extra)
    if(extra[1]['source_package_sha256']!=digest(native)or extra[1]['questions']!=p['new_questions']):
        raise ValueError('batch review differs from native question scope')
    costs=combined_costs(p,native)
    replay=checkpoint.combine(components,p,native,costs)
    for key in ('method_version','mode','package_sha256','questions','outcomes','collection','scores','resolved_pairwise',
            'outcome_counts','phase_costs','phase_components','evidence_inventory','review_pipeline','report_limits',
            'checker_acceptance','publication_review','active_review_profile','live_profiles'):
        if encoded(loads(replay.get(key)))!=encoded(report.get(key)):raise ValueError('batch report does not replay: '+key)
    if closure['costs']!=costs or closure['question_count']!=len(report['questions']):raise ValueError('batch closure cost/scope differs')
    cases={cid:value for _,_,_,own in components for cid,value in own.items()}
    return report,cases,native


def loads(value):
    # JSON report keys (notably metric depths) are strings after serialization.
    import json
    return json.loads(encoded(value))


def combined_costs(p,native):
    from .accounting import report_costs
    costs=report_costs(native,Path(native['configuration']['run_directory']))
    current=deepcopy(p['old_costs'])
    current.update({k:str(Decimal(p['old_costs'][k])+Decimal(costs[k]))for k in checkpoint.COST_KEYS})
    current['components']=p['old_costs']['components']+[dict(name='batch-'+str(len(p['new_questions'])),**costs)]
    return current


def cost_forecast(template,count,parent_costs):
    """Observed-cohort projection, with Pro and lost-cache scenarios explicit."""
    from collections import Counter
    from .accounting import report_costs,usage_cost
    run=Path(template['configuration']['run_directory']);events=list(records(run/'attempts.jsonl'))
    ends={e['attempt_id']:e for e in events if e['event']=='finished'}
    costs=report_costs(template,run);factor=Decimal(count)/len(template['questions'])
    uncached=deepcopy(template);uncached['configuration']['execution_policy']['budget_accounting']='source-native-usage-v1'
    uncached_pro=Decimal(0);residual=Counter();calls=Counter()
    for event in events:
        if event['event']!='started':continue
        request=load(run/'attempts'/event['attempt_id']/'request.json')
        response=load(run/ends[event['attempt_id']]['result_path'])if event['attempt_id']in ends else{}
        calls[request['role']if request['kind']=='model'else request['kind']]+=1
        value=usage_cost(request,response,uncached)
        if value is not None:uncached_pro+=value
        else:residual[request['kind']]+=Decimal(request['reservation_usd'])
    scale=lambda value:str(Decimal(value)*factor)
    observed=Decimal(costs['conservative_total_usd'])*factor
    no_cache=(uncached_pro+Decimal(costs['other_conservative_holds_and_allowances_usd']))*factor
    carry=Decimal(parent_costs['conservative_total_usd'])
    return dict(basis='Linear projection of the latest completed native cohort, not a quote, accuracy estimate or guaranteed upper bound. Frozen source accounting rates; no current-price lookup.',
        source_native_ledger_sha256=digest((run/'attempts.jsonl').read_bytes()),source_questions=len(template['questions']),
        source_costs=costs,new_questions=count,
        completed_pro_published_projection_usd=scale(costs['published_completed_pro_usd']),
        completed_pro_conservative_projection_usd=scale(costs['conservative_completed_pro_usd']),
        completed_pro_no_cache_conservative_projection_usd=str(uncached_pro*factor),
        residual_projection_usd={kind:scale(amount)for kind,amount in sorted(residual.items())},
        observed_cache_total_projection_usd=str(observed),no_cache_total_projection_usd=str(no_cache),
        cumulative_observed_projection_usd=str(carry+observed),cumulative_no_cache_projection_usd=str(carry+no_cache),
        source_attempt_counts=dict(calls),ordinary_attempt_projection={role:scale(n)for role,n in sorted(calls.items())},
        subscriptions='Outside API total; ordinary Sol/Claude projections omit later policy/publication reviews and quota waits. Bound task scope, not dollars, controls these stages.',
        warning='Pro is included once in each total. Search/cache/unknown-usage reservations remain in residuals. More results, tokens, retries, cache leases or provider charges can exceed both projections; the approved ceiling stops further admission.')


def approval_review(p,native):
    f=p['cost_forecast'];n=len(p['old_questions']);k=len(p['new_questions'])
    money=lambda value:f'{Decimal(value):.2f}'
    lines=[f'# Proposed next batch: {k} new questions; {n+k} total','',
        f'Proposed: run the next {k} questions with the same {len(native["providers"])} providers and accepted grading method. Keep the completed {n}, produce one report covering all {n+k}, then stop for review.',
        '', 'NOT APPROVED. Preparation makes no provider or model calls.','',
        f'- Exact package SHA: `{digest(p)}`',f'- Parent report SHA: `{p["parent_report_sha256"]}`',
        f'- Keep all {len(native["providers"])} provider configurations and the accepted method/model identities.',
        f'- Retain {n} completed questions; collect only the next {k} frozen questions, then stop.',
        '- One launch includes native judging, policy review, native audit, accepted-profile review and the combined report.',
        '- Unknown is a permitted final outcome. Invalid reviews cannot vote. Calibration is not repeated.',
        '', '## Proposed API budget','',
        f'- Parent-report conservative API cost: ${money(p["prior_committed_and_held_usd"])}.',
        f'- Proposed cumulative ceiling: ${money(p["shared_ceiling_usd"])}; native phase allowance including retained work: ${money(native["configuration"]["execution_policy"]["spending_ceiling_usd"])}.',
        f'- Pro alone, observed-cache projection: ${money(f["completed_pro_conservative_projection_usd"])} conservative; ${money(f["completed_pro_published_projection_usd"])} at the frozen published accounting tier.',
        f'- Pro alone, no cache benefit: ${money(f["completed_pro_no_cache_conservative_projection_usd"])} conservative.',
        f'- Search allowances: ${money(f["residual_projection_usd"].get("search",0))}; cache allowances: ${money(f["residual_projection_usd"].get("cache",0))}; other retained allowances: ${money(sum(Decimal(v)for kind,v in f["residual_projection_usd"].items()if kind not in {"search","cache"}))}.',
        f'- New API projection with observed cache behavior: ${money(f["observed_cache_total_projection_usd"])}; without cache benefit: ${money(f["no_cache_total_projection_usd"])}.',
        f'- Cumulative projections: ${money(f["cumulative_observed_projection_usd"])} to ${money(f["cumulative_no_cache_projection_usd"])}. Each total includes Pro once.',
        '- These scenarios are not guaranteed bounds or invoices. The runner stops admission at the approved ceiling.',
        '- Subscription usage is separate. No new model, endpoint, fallback or prompt change is selected.',
        '', '## Declared work and recovery','',
        f'- Native phase scope, including retained work on recovery: {native["scope"]["search_cells"]} search cells; up to {native["scope"]["result_slots"]} returned results.',
        f'- Whole native judging scope, including retained work: up to {native["scope"]["primary_tasks"]} Pro judgments, {native["scope"]["second_tasks"]} Sol judgments and {native["scope"]["conditional_third_tasks"]} conditional Claude judgments. Exact-task reuse prevents repeat successful calls.',
        f'- Policy-review bound: {2*native["scope"]["result_slots"]} subscription tasks; final accepted-review bound: {p["accepted_review_task_bound"]}.',
        f'- Attempts per task: {native["configuration"]["execution_policy"]["max_attempts_per_task"]}; existing bound retry, transient recovery and quota rules retained.',
        '- Full private inputs/outputs, failures, uncertainty and charge holds retained. No automatic semantic resends.',
        '- Original questions keep their collection times and grades; later collection is not a controlled quality comparison.',
        '- The frozen question/date anchor and provider date settings remain unchanged.',
        '', 'Approve only the exact package after reviewing its settings, projections and limitations.',
        'Complete approval-template.json with decision=approved, approved_by and approved_at, then run checkpoint with that receipt.']
    if p.get('live_profiles'):
        lines[8:8]=[f'- Live profiles: date `{p["live_profiles"]["date_profile"]}`; checker `{p["live_profiles"]["checker_profile"]}`; text `{p["live_profiles"]["text_profile"]}`; selection `{p["live_profiles"]["selection_profile"]}`.',
            f'- Profile authority package SHA: `{p["accepted_profile"]["package_sha256"]}`; owner receipt SHA: `{p["accepted_profile"]["approval_sha256"]}`.']
    if (native['configuration'].get('system_cache',{}).get('renewal')or{}).get('profile')=='expiry-leases-v2':
        lines.extend(['','Under the bound v2 lease policy, an unsent/held primary remains pending and does not trigger a third. A ceiling stop therefore preserves a partial tail for resume rather than spending additional subscription reviews on missing primaries. Judge stop/cooldown transitions are written to status-events.jsonl.'])
    if p.get('native_recovery'):
        carried=Decimal(native['reservation_forecast']['carried_reservation_usd'])
        available=Decimal(p['shared_ceiling_usd'])-Decimal(p['prior_committed_and_held_usd'])-carried
        lines.extend(['',f'Recovery already carries ${money(carried)} of native usage/holds in addition to the parent report. Remaining cumulative admission is ${money(available)}. Whole-phase projections above include this retained work; they are not additional recovery charges.'])
    return '\n'.join(lines)+'\n'


def parent_authority(prior,report_sha256,authority):
    active=prior.get('active_review_profile',prior.get('checker_acceptance',{}))
    if profile_authority.profiles(authority):
        return (authority.get('source_report_sha256')==report_sha256 or active==authority)
    return active.get('approval_sha256')==authority['approval_sha256']
