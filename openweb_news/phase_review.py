"""Finish a phase using disjoint completed review bundles, then report offline."""
from collections import Counter
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from itertools import combinations

from . import adjudication, judging, representation, review_bundle
from .common import asset_bindings, code_bindings, digest, load, now, records, write_private, private_directory
from .review_pipeline import decode_value, verify, decode
from .review import settle, needs_third
from .subscription_health import verified, recovered_completion
from ._method import scoring

PROFILE = 'phase-review-continuation-v1'


def completed_bundle(path, *, legacy_count=False):
    """Audit an immutable completed component without authorizing historical code."""
    path = Path(path).absolute(); root = path.parent; bundle = load(path)
    approval = load(root/'approval.json')
    if (bundle['profile'] != review_bundle.PROFILE or approval.get('decision') != 'approved'
            or approval.get('package_sha256') != digest(bundle)):
        raise ValueError('completed component needs its original exact approval')
    brave = load(root/'brave/package.json'); package = load(root/'review/package.json')
    native = load(root/'brave/run/report.json'); report = load(root/'review/run/report.json')
    closure = load(root/'review/run/closure.json'); receipt = load(root/'review/approval.json')
    if (digest(brave) != bundle['brave_package_sha256'] or native['package_sha256'] != digest(brave)
            or package['replacement_package_sha256'] != digest(brave)
            or package['source_package_sha256'] != bundle['source_package_sha256']
            or package['source_report_sha256'] != bundle['source_report_sha256']
            or package['questions'] != brave['questions']
            or bundle['question_ids'] != [q['question_id'] for q in package['questions']]
            or report['package_sha256'] != digest(package) or closure['package_sha256'] != digest(package)
            or closure['report_sha256'] != digest((root/'review/run/report.json').read_bytes())
            or receipt.get('approved') is not True or receipt.get('package_sha256') != digest(package)
            or receipt.get('parent_bundle_sha256') != digest(bundle)
            or native['execution']['state'] != 'completed' or report['execution']['state'] != 'completed'):
        raise ValueError('component lineage or completion differs')
    cases = verify(package, root/'review', historical=True)
    responses = {}; proofs = 0; legacy_proofs = 0; starts_count = 0; recovered = []; excluded=[]
    for stage in ('brave', 'review'):
        directory = root/stage/'run'; events = list(records(directory/'attempts.jsonl'))
        starts = {e['attempt_id']:e for e in events if e['event']=='started'}
        ends = {e['attempt_id']:e for e in events if e['event']=='finished'}
        if starts.keys() != ends.keys():
            raise ValueError('component has unfinished attempts')
        starts_count += len(starts)
        jobs = {j['task_id']:j for j in package['jobs']} if stage=='review' else None
        for aid,event in starts.items():
            end = ends[aid]; folder = directory/'attempts'/aid
            request = load(folder/'request.json'); raw = load(directory/end['result_path'])
            if (digest(request) != event['request_sha256'] or event['task_id'] != end['task_id']
                    or digest((directory/end['result_path']).read_bytes()) != end['result_sha256']):
                raise ValueError('component native ledger binding differs')
            if jobs is not None and jobs.get(event['task_id']) != request:
                raise ValueError('component request is outside its bound review tasks')
            # Failed/uncertain attempts remain audited history, not completed
            # judgments. A declared retry may have supplied the final proof.
            if (request.get('kind') == 'model' and request.get('backend','').endswith('-subscription')
                    and raw.get('transport_success') is True and raw.get('complete') is not False
                    and raw.get('dispatch_state') == 'completed'):
                if not verified(request, raw, folder):
                    # Review decisions still require an audited terminal result;
                    # a native retry is not itself a failed final completion.
                    original = dict(raw)
                    attached = original.get('subscription_completion_evidence') or {}
                    if attached.get('normal_health') is False:
                        from .subscription_health import terminal_verified
                        if not terminal_verified(request, raw, folder):
                            raise ValueError('recovered review completion binding differs')
                        original.pop('subscription_completion_evidence', None)
                        original.pop('native_completion_verified', None)
                    proof = recovered_completion(request, original, folder)
                    from .subscription_health import excluded_intervention
                    exclusion=excluded_intervention(request,raw,folder)
                    if not proof and not exclusion:
                        raise ValueError('component native subscription proof differs')
                    if exclusion:excluded.append(dict(attempt_id=aid,task_id=event['task_id'],**exclusion))
                    else:recovered.append(dict(attempt_id=aid, task_id=event['task_id'], **proof))
                else:
                    proofs += 1
                    legacy_proofs += raw.get('complete') is True
            if stage=='review':
                from .subscription_health import excluded_intervention
                exclusion=excluded_intervention(request,raw,folder)
                responses[event['task_id']] = dict(raw,native_completion_verified=False,native_intervention_excluded=exclusion)if exclusion else raw
    replay_completed_review(package,cases,responses,report)
    from .accounting import report_costs
    cost = report_costs(brave,Path(brave['configuration']['run_directory']))
    binding = dict(bundle_path=str(path),bundle_sha256=digest(bundle),review_package_sha256=digest(package),
                   report_sha256=digest((root/'review/run/report.json').read_bytes()),
                   brave_report_sha256=digest((root/'brave/run/report.json').read_bytes()),
                   question_ids=bundle['question_ids'],adjudication_profile=package.get('adjudication_profile',adjudication.PROFILE),
                   native_subscription_proofs=legacy_proofs if legacy_count else proofs,native_attempts=starts_count,costs=cost)
    if recovered:
        binding['recovered_terminal_completions'] = recovered
    if excluded:binding['excluded_terminal_completions']=excluded
    return binding, package, report, cases



def replay_completed_review(package,cases,responses,report):
    saved = {o['case_id']:o for o in report['outcomes']}
    if len(saved) != len(report['outcomes']) or saved.keys() != cases.keys():
        raise ValueError('component outcome roster differs')
    checked_records = []
    for cid,case in cases.items():
        pos, outcome = decode(case, package['text_profile'], date_profile=package.get('date_profile'))
        extra = responses.get('third-claude-'+cid)
        if extra:
            excluded=extra.get('native_intervention_excluded')
            if (not extra.get('native_completion_verified')and not excluded) or not needs_third(pos['pro'],pos['codex']):
                raise ValueError('invalid completed third review')
            with representation.selected(package['text_profile']):
                third = (decode_value(extra.get('verdict'),case['compiled'],package['text_profile'],date_profile=package['date_profile'])
                         if package.get('date_profile') else judging.decode_position(extra.get('verdict'),case['compiled']))
            if excluded:
                from .subscription_health import excluded_position
                third=excluded_position(excluded)
            third.update(independent=True,prior_verdict_exposure=False)
            pos['claude'] = third; outcome = settle(pos['pro'],pos['codex'],third)
        own = {r:responses['adjudication-'+r+'-'+cid] for r in ('codex','claude') if 'adjudication-'+r+'-'+cid in responses}
        if own:
            ids = adjudication.eligible(case['compiled'],pos,case['provider'],date_profile=package.get('date_profile'))
            packet = adjudication.packet_for(case['compiled'],ids,profile=package.get('adjudication_profile',adjudication.PROFILE))
            with representation.selected(package['text_profile']):
                effective, record = adjudication.corroborate(case['compiled'],pos,packet,own,case['provider'],date_profile=package.get('date_profile'))
            record['case_id'] = cid; checked_records.append(record)
            if record['accepted']: outcome = settle(effective['pro'],effective['codex'])
        if outcome['grade'] != saved[cid]['grade'] or outcome['selected_roles'] != saved[cid]['selected_roles']:
            raise ValueError('component judgment does not replay')
    if checked_records != report['review_pipeline']['records']:
        raise ValueError('component settlement record does not replay')

def prepare_phase(source_package, completed_bundles, directory):
    """Bind completed partitions and prepare only the still-uncollected Brave subset."""
    root = private_directory(directory); source_path = Path(source_package).absolute(); source = load(source_path)
    selected = []; covered = set(); questions = {q['question_id']:q for q in source['questions']}
    if not completed_bundles: raise ValueError('explicit completed components required')
    for path in completed_bundles:
        binding, package, _, _ = completed_bundle(path)
        if (package['source_package_sha256'] != digest(source) or covered.intersection(binding['question_ids'])
                or any(questions.get(q['question_id']) != q for q in package['questions'])):
            raise ValueError('components must be disjoint subsets of this source phase')
        covered.update(binding['question_ids']); selected.append(binding)
    remaining = [q for q in questions if q not in covered]
    if not remaining: raise ValueError('phase already covered; use phase-report to regenerate its views')
    child = review_bundle.prepare_bundle(source_path,remaining,root/'remaining')
    # The source report contains the original phase spend. Carry the completed
    # diagnostics too; separate ledgers must never reset the owner's ceiling.
    additional = sum((Decimal(b['costs']['conservative_total_usd']) for b in selected),Decimal(0))
    brave = load(root/'remaining/brave/package.json'); cfg = load(root/'remaining/brave/config.json')
    cfg['execution_policy']['spending_ceiling_usd'] = str(Decimal(cfg['execution_policy']['spending_ceiling_usd'])-additional)
    if Decimal(cfg['execution_policy']['spending_ceiling_usd']) <= 0: raise ValueError('phase ceiling exhausted')
    from .preparation import prepare
    brave = prepare(cfg)
    write_private(root/'remaining/brave/config.json',cfg,replace=True)
    write_private(root/'remaining/brave/package.json',brave,replace=True)
    child.update(brave_package_sha256=digest(brave),prior_committed_and_held_usd=str(Decimal(child['prior_committed_and_held_usd'])+additional))
    write_private(root/'remaining/bundle.json',child,replace=True)
    package = dict(profile=PROFILE,created_at=now(),source_package=str(source_path),source_package_sha256=digest(source),
                   code=code_bindings(),assets=asset_bindings(),completed_components=selected,
                   remaining_bundle_sha256=digest(child),question_ids=list(questions),remaining_question_ids=remaining,
                   shared_ceiling_usd=child['shared_ceiling_usd'],prior_committed_and_held_usd=child['prior_committed_and_held_usd'],
                   continuation='Finish this phase and report; no next-phase dispatch')
    write_private(root/'phase.json',package)
    return package


def verify_phase(path, *, reporting_only=False):
    path = Path(path); package = load(path)
    current = code_bindings()
    changed = {k for k in set(current)|set(package['code']) if current.get(k) != package['code'].get(k)}
    allowed = {'phase_review.py','phase_report.py','subscription_health.py','cli.py'} if reporting_only else set()
    if package['profile'] != PROFILE or not changed <= allowed or package['assets'] != asset_bindings():
        raise ValueError('phase code/assets changed')
    if reporting_only:
        approval = load(path.parent/'approval.json')
        if approval.get('decision') != 'approved' or approval.get('package_sha256') != digest(package):
            raise ValueError('offline repair requires the original phase approval')
    source = load(package['source_package'])
    if digest(source) != package['source_package_sha256']: raise ValueError('phase source changed')
    for expected in package['completed_components']:
        actual, _, _, _ = completed_bundle(expected['bundle_path'])
        if actual != expected:
            # v1's inventory counted only explicit complete=True, omitting the
            # Claude terminal proofs. Reproduce that exact counter offline,
            # after auditing every current proof; never ignore other differences.
            legacy = completed_bundle(expected['bundle_path'],legacy_count=True)[0] if reporting_only else None
            if legacy != expected:
                raise ValueError('completed phase component changed')
    child = load(path.parent/'remaining/bundle.json')
    if (digest(child) != package['remaining_bundle_sha256'] or child['question_ids'] != package['remaining_question_ids']
            or child['source_package_sha256'] != digest(source)
            or child['source_report_sha256'] != digest((Path(source['configuration']['run_directory'])/'report.json').read_bytes())):
        raise ValueError('remaining work binding changed')
    return package, source


def run_phase(path, approval, *, env_file=None):
    path = Path(path); package, _ = verify_phase(path)
    if (approval.get('decision') != 'approved' or approval.get('package_sha256') != digest(package)
            or not approval.get('approved_by') or not approval.get('approved_at')):
        raise ValueError('exact phase approval required')
    child_path = path.parent/'remaining/bundle.json'
    receipt = dict(approval,package_sha256=digest(load(child_path)),parent_phase_sha256=digest(package))
    write_private(child_path.parent/'approval.json',receipt,replace=True)
    result = review_bundle.run_bundle(child_path,receipt,env_file=env_file)
    if result.get('continuation_required'): return result
    return report_phase(path,path.parent/'report')


def report_phase(path, output, *, reporting_only=False):
    """Revalidate completed components, pool original-rank slots and rescore once."""
    path = Path(path); package, source = verify_phase(path, reporting_only=reporting_only)
    output = Path(output).resolve()
    source_root = Path(package.get('source_package', source['configuration']['run_directory'])).resolve().parent
    source_run = Path(source['configuration']['run_directory']).resolve()
    if output.is_relative_to(source_root) or output.is_relative_to(source_run):
        raise ValueError('phase report must preserve the original source evidence tree')
    components = [completed_bundle(b['bundle_path']) for b in package['completed_components']]
    components.append(completed_bundle(path.parent/'remaining/bundle.json'))
    cases = {}; outcomes = []; collection = []; seen = set(); policies = []; bindings = []
    for binding, child, report, own in components:
        qids = set(binding['question_ids'])
        if seen & qids or set(cases)&set(own): raise ValueError('overlapping phase components')
        if child['source_package_sha256'] != digest(source): raise ValueError('mixed source phases')
        seen |= qids; cases.update(own); outcomes += deepcopy(report['outcomes']); collection += child['source_collection']
        policies += deepcopy(report['review_pipeline']['records']); bindings.append(binding)
    if seen != set(package['question_ids']): raise ValueError('incomplete phase scope')
    labels = {o['case_id']:o['grade'] for o in outcomes}; reasons = {o['case_id']:o['reason'] for o in outcomes}; findings = {}
    for o in outcomes:
        pos = o['positions'].get(o['selected_roles'][0],{}) if o['selected_roles'] else {}
        judgment = pos.get('conformance_judgment') or pos.get('judgment') or {}
        findings[o['case_id']] = [dict(report_id=None,event_id=None,facet_ids=c['supported_facet_ids']) for c in judgment.get('candidates',[]) if c['answer_supported']] if o['grade']==2 else []
    cells = {}
    for row in collection:
        key = row['configuration_id'],row['question_id']
        if key in cells: raise ValueError('duplicate phase collection cell')
        ranked = [dict(rank=o['rank'],evidence_id=o['case_id'],url=cases[o['case_id']]['compiled']['packet']['returned_url'])
                  for o in sorted(outcomes,key=lambda r:r['rank']) if (o['configuration_id'],o['question_id'])==key]
        cells[key] = dict(status=row['status'],ranked=ranked)
    configs = [p['configuration_id'] for p in source['providers']]
    if set(cells) != {(c,q) for c in configs for q in seen}: raise ValueError('phase collection coverage differs')
    scores = scoring.score(source['questions'],configs,cells,labels,reasons,source['configuration']['coverage_target'],source['configuration']['tie_order'],findings=findings)
    pairs = []
    for k in (3,5,10):
        for name in ('answer','useful'):
            values = {(r['question_id'],r['configuration_id']):r['metrics'][name] for r in scores['question_rows'] if r['k']==k and r['metrics'] is not None}
            for a,b in combinations(configs,2):
                pairs.append(dict(metric=f'{name}@{k}',**scoring.paired(source['questions'],a,b,scores['availability']['answered_question_ids'],values)))
    from .accounting import report_costs
    base_cost = report_costs(source,Path(source['configuration']['run_directory']))
    costs = [base_cost]+[b['costs'] for b in bindings]
    totals = {key:str(sum((Decimal(c[key]) for c in costs),Decimal(0))) for key in
              ('published_completed_pro_usd','conservative_completed_pro_usd','other_conservative_holds_and_allowances_usd','conservative_total_usd','published_pro_plus_conservative_residual_usd')}
    totals.update(billing=base_cost['billing'],components=[dict(name='original phase',**base_cost)]+[dict(name=','.join(b['question_ids']),**b['costs'])for b in bindings])
    inventory = {}
    for config in configs:
        own = [c['compiled']['input'] for c in cases.values() if c['original']['configuration_id']==config]
        times = sorted(c['captured_at'] for c in own)
        inventory[config] = dict(field_states={f:dict(Counter(c['native_field_states'].get(f,'missing') for c in own)) for f in ('title','snippet')},
                                 earliest_capture=times[0] if times else None,latest_capture=times[-1] if times else None)
    report = dict(method_version=components[0][1]['source_method'],mode=PROFILE,package_sha256=digest(package),generated_at=now(),
                  questions=source['questions'],outcomes=outcomes,collection=collection,scores=scores,resolved_pairwise=pairs,
                  outcome_counts=dict(Counter('unknown' if o['grade'] is None else str(o['grade'])for o in outcomes)),
                  execution=dict(state='completed',held_tasks=[],reused_tasks=sum(len(c[1]['jobs']) for c in components[:-1])),
                  phase_costs=totals,phase_components=bindings,evidence_inventory=inventory,
                  review_pipeline=dict(records=policies,final_unknowns=sum(o['grade']is None for o in outcomes),
                                       original_unknowns=sum(o['grade']is None for o in load(Path(source['configuration']['run_directory'])/'report.json')['outcomes'])),
                  report_limits=['Unknowns remain unknown; no model agreement is human accuracy.',
                                 'Five completed questions retain v1 review evidence; remaining questions use the clarified v2 mapping proof.',
                                 'Brave was collected later; no controlled markup-only effect is claimed.',
                                 'Provider observed rates use scheduled slots; resolved-question/family comparisons have explicit distinct scopes.',
                                 'Original reports and native artifacts are retained. No next-phase work is authorized.'])
    from .reporting import write_reports
    if any(output.is_relative_to(Path(c[0]['bundle_path']).resolve().parent) for c in components):
        raise ValueError('phase report must preserve source components')
    write_reports(private_directory(output),report)
    write_private(output/'closure.json',dict(profile=PROFILE,package_sha256=digest(package),report_sha256=digest((output/'report.json').read_bytes()),
                  question_count=len(seen),outcomes=len(outcomes),original_results_unchanged=True,components=bindings,new_calls_by_reporter=0),replace=True)
    if reporting_only:
        changed = sorted(k for k in set(code_bindings())|set(package['code']) if code_bindings().get(k) != package['code'].get(k))
        write_private(output/'REPAIR-RECEIPT.json',dict(profile='offline-phase-report-repair-v1',source_phase_sha256=digest(package),
                      changed_code_files=changed,current_code=code_bindings(),assets=asset_bindings(),
                      completed_at=now(),new_calls=0,replayed_outcomes=len(outcomes),
                      report_sha256=digest((output/'report.json').read_bytes()),components=bindings,
                      historical_normal_proof_counts=[dict(bundle_sha256=b['bundle_sha256'],frozen_count=e['native_subscription_proofs'],
                          expanded_count=b['native_subscription_proofs'])for e,b in zip(package['completed_components'],bindings)
                          if e['native_subscription_proofs'] != b['native_subscription_proofs']]),replace=True)
    return dict(state='completed',questions=len(seen),outcome_counts=report['outcome_counts'],costs=totals,report=str(output/'report.html'))
