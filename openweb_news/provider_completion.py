"""Bounded provider-cell completion; historical evidence is immutable.

Prepare/inspect offline, then execute only under exact method, independent-plan
review and owner receipts. Normal native/ordinary/accepted owners process only
new cells. The old report is decoded only during final integration.
"""
from collections import Counter
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
import argparse
import hashlib
import json
import mmap
import re

from . import accepted_review, batch, checkpoint, clean_run, profile_authority, review_pipeline, runner, settlement
from .common import asset_bindings, code_bindings, digest, file_digest, file_manifest, load, now, records, write_private
from .accounting import report_costs
from .ledger import Ledger
from .reporting import write_reports

PROFILE = 'provider-cell-completion-v1'
SUMMARY = 'provider-cell-completion-source-summary-v1'
SUCCESS = {'success', 'success_empty'}
FAILURES = {'http_error', 'not_collected'}
_TOKEN = re.compile(rb'"[^"\\]*(?:\\.[^"\\]*)*"|[{}\[\]]')
_SPACE = re.compile(rb'\s*')
_SCALAR = re.compile(rb'[^,\]}\s]+')


def bound(path):
    path = Path(path).resolve()
    return dict(path=str(path), sha256=file_digest(path))


def read(ref):
    if file_digest(ref['path']) != ref['sha256']:
        raise ValueError('bound provider-completion evidence changed')
    return load(ref['path'])


def save_same(path, value):
    path = Path(path)
    if path.exists():
        if load(path) != value:raise ValueError('immutable provider-completion artifact differs: '+path.name)
    else:write_private(path, value)


def _end(data, start):
    start = _SPACE.match(data, start).end()
    if data[start:start+1] not in (b'{', b'['):
        return (_TOKEN.match(data,start) if data[start:start+1] == b'"' else _SCALAR.match(data,start)).end()
    depth = 0
    for match in _TOKEN.finditer(data,start):
        if match[0] in (b'{', b'['):depth += 1
        elif match[0] in (b'}', b']'):
            depth -= 1
            if depth == 0:return match.end()
    raise ValueError('unterminated source JSON')


def _fields(data, start=0):
    pos = _SPACE.match(data,start).end()
    if data[pos:pos+1] != b'{':raise ValueError('expected source JSON object')
    pos += 1
    while True:
        pos = _SPACE.match(data,pos).end()
        if data[pos:pos+1] == b'}':return
        end = _end(data,pos);key = json.loads(data[pos:end]);pos = _SPACE.match(data,end).end()
        if data[pos:pos+1] != b':':raise ValueError('source JSON colon differs')
        pos = _SPACE.match(data,pos+1).end();end = _end(data,pos)
        yield key,(pos,end)
        pos = _SPACE.match(data,end).end()
        if data[pos:pos+1] == b',':pos += 1
        elif data[pos:pos+1] != b'}':raise ValueError('source JSON delimiter differs')


def _prefix(reference):
    p = read(reference);gate = read(p['gate']);review = read(p['review']);snap = read(p['snapshot'])
    clean_run._validate_review(review,gate)
    if (p.get('profile') not in {'verified-clean-prefix-v1','verified-provider-completion-prefix-v1'} or review['decision'] != 'continue'
            or gate['report'] != snap['report'] or p['snapshot'] != gate['snapshot']
            or p['question_ids'] != gate['question_ids'] or p['question_ids'] != snap['question_ids']
            or p['costs'] != gate['costs'] or p['costs'] != snap['costs']
            or p['closure'] != gate['evidence_closure'] or gate['unclassified_native_completions'] != 0
            or gate['undeclared_duplicate_tasks'] != 0):
        raise ValueError('source requires an exact reviewed closed prefix')
    if review.get('source_review'):read(review['source_review']) if Path(review['source_review']['path']).suffix == '.json' else _verify_file(review['source_review'])
    _verify_file(p['closure'])  # Hash one saved index, never walk/re-hash historical files.
    authority = snap['native']['configuration']['live_profile_authority']
    method = load(authority['package_path']);receipt = load(Path(authority['package_path']).parent/'approval.json')
    if (digest(method) != authority['package_sha256'] or digest(receipt) != authority['approval_sha256']
            or receipt.get('approved') is not True or receipt.get('accept_method') is not True
            or receipt.get('package_sha256') != digest(method) or method['profile_bindings'] != authority['profile_bindings']
            or digest(authority) != gate['accepted_profile_sha256'] or snap['code'] != authority['profile_bindings']['semantic_code']):
        raise ValueError('source historical method/owner receipt differs')
    return p,gate,snap


def _verify_file(ref):
    if file_digest(ref['path']) != ref['sha256']:raise ValueError('bound source file changed')
    return Path(ref['path'])


def prepare_source_summary(source_prefix, directory):
    """One offline structural scan; no outcome/judgment-tree decoding or calls."""
    prefix,gate,snapshot = _prefix(source_prefix)
    source = _verify_file(gate['report']);projected = {}
    with source.open('rb') as handle,mmap.mmap(handle.fileno(),0,access=mmap.ACCESS_READ) as data:
        for key,(a,b) in _fields(data):
            if key in {'questions','collection','deferred_collection','phase_costs','method_version','active_review_profile','outcome_counts'}:
                projected[key] = json.loads(data[a:b])
    if ([q['question_id'] for q in projected['questions']] != prefix['question_ids']
            or projected['phase_costs'] != prefix['costs'] or projected['active_review_profile'] != snapshot['native']['configuration']['live_profile_authority']):
        raise ValueError('source projected scope/method/cost differs from closed prefix')
    summary = dict(profile=SUMMARY,source_prefix=deepcopy(source_prefix),source_report=gate['report'],
                   snapshot=prefix['snapshot'],projected=projected,source_report_verified=True)
    path = Path(directory).resolve()/'source-summary.json';save_same(path,summary)
    return bound(path)


def _cells(summary, cells, providers):
    qs = {q['question_id']:q for q in summary['questions']}
    roster = {p['configuration_id'] for p in providers};grid = {}
    for row in summary['collection']:
        key = row['configuration_id'],row['question_id']
        if key in grid:raise ValueError('duplicate source collection cell')
        grid[key] = row
    deferred = {(r['configuration_id'],r['question_id']) for r in summary.get('deferred_collection',[])}
    expected = {(p,q) for p in roster for q in qs}
    if not set(grid) <= expected or not deferred <= expected:raise ValueError('source grid outside frozen roster/questions')
    if expected-set(grid) != deferred-set(grid):raise ValueError('absent source cells lack explicit deferred declarations')
    for key in deferred:
        if key in grid and grid[key]['status'] != 'not_collected':raise ValueError('deferred/source status differs')
        grid.setdefault(key,dict(configuration_id=key[0],question_id=key[1],status='not_collected',deferred=True))
    declared = {};checked = []
    if not isinstance(cells,list) or not cells:raise ValueError('explicit nonempty completion cell manifest required')
    for row in cells:
        if set(row) != {'configuration_id','question_id','source_status'}:raise ValueError('completion manifest fields differ')
        key = row['configuration_id'],row['question_id']
        if key in declared or key not in grid:raise ValueError('duplicate/out-of-scope completion cell')
        source = grid[key]
        if (row['source_status'] not in FAILURES or source['status'] != row['source_status']
                or source.get('dispatch_state') in {'uncertain','started','in_flight'} or source.get('uncertain') is True):
            raise ValueError('completion requires a definite failed or explicitly deferred source cell')
        if source['status'] == 'http_error' and (source.get('dispatch_state') != 'completed' or source.get('transport_stage') not in {'http_request_or_response',None}):
            raise ValueError('failed source send lacks a completed HTTP receipt')
        declared[key] = row;checked.append(deepcopy(row))
    ids = {r['configuration_id'] for r in cells};qids = {r['question_id'] for r in cells}
    if len(ids) != 1 or set(declared) != {(cid,q) for cid in ids for q in qids}:
        raise ValueError('one provider and its exact explicit missing-cell questions required')
    return grid,checked,[q for q in summary['questions'] if q['question_id'] in qids]


def prepare(source_prefix, source_summary, template_package, cells, directory, *, accepted_profile, shared_accounting, mode='live'):
    root = Path(directory).resolve();summary = read(source_summary)
    if summary.get('profile') != SUMMARY or summary['source_prefix'] != source_prefix or summary.get('source_report_verified') is not True:
        raise ValueError('verified source summary differs')
    prefix,gate,snapshot = _prefix(source_prefix)
    if summary['source_report'] != gate['report'] or summary['snapshot'] != prefix['snapshot']:raise ValueError('source-summary binding differs')
    source_path = Path(gate['report']['path']).resolve()
    if root.is_relative_to(source_path.parent.parent) or source_path.is_relative_to(root):raise ValueError('completion must preserve its source directory')
    template = load(template_package)
    reviewers = template['configuration']['reviewers']
    if any(reviewers[r]['backend'] != r+'-subscription' for r in ('codex','claude')) or reviewers['codex'].get('transport') != 'app-server-stdio-v1':
        raise ValueError('completion requires the frozen isolated subscription reviewer route')
    authority = profile_authority.read(accepted_profile,template['configuration']['reviewers'])
    clean_run._same_choices(summary['projected']['active_review_profile'],authority,allowed={'preparation.py','provider_completion.py'})
    if (template['providers'] != snapshot['native']['providers'] or template['primary'] != snapshot['native']['primary']
            or template['configuration']['reviewers'] != snapshot['native']['configuration']['reviewers']):
        raise ValueError('completion changed provider/judge configuration')
    grid,manifest,questions = _cells(summary['projected'],cells,template['providers'])
    accounting = read(shared_accounting)
    if (accounting['gate'] != prefix['gate'] or accounting['verdict'] != prefix['review']
            or Decimal(accounting['benchmark_conservative_usd']) != Decimal(prefix['costs']['conservative_total_usd'])
            or Decimal(accounting['combined_conservative_usd']) != Decimal(accounting['benchmark_conservative_usd'])+Decimal(accounting['separate_diagnostics_usd'])
            or Decimal(accounting['remaining_usd']) != Decimal(accounting['shared_ceiling_usd'])-Decimal(accounting['combined_conservative_usd'])):
        raise ValueError('shared accounting source/diagnostics/ceiling differs')
    search_ids = [p['configuration_id'] for p in template['providers'] if p['configuration_id'] == manifest[0]['configuration_id']]
    config,native = batch.native_plan(template,questions,root,accounting['shared_ceiling_usd'],accounting['combined_conservative_usd'],
                                     authority=authority,mode=mode,search_provider_ids=search_ids)
    if any(k in config for k in ('reuse_snapshot','fresh_judging','prior_work')):raise ValueError('completion child must be fresh native work')
    package = dict(profile=PROFILE,created_at=now(),mode=mode,output_directory=str(root),code=code_bindings(),assets=asset_bindings(),
                   source_prefix=deepcopy(source_prefix),source_summary=deepcopy(source_summary),source_report=gate['report'],source_snapshot=prefix['snapshot'],
                   template=bound(template_package),accepted_profile=authority,cells=manifest,questions=questions,
                   source_question_count=len(summary['projected']['questions']),source_cell_count=len(grid),source_outcomes_sha256=snapshot['outcomes_sha256'],
                   shared_accounting=deepcopy(shared_accounting),shared_ceiling_usd=accounting['shared_ceiling_usd'],
                   prior_committed_and_held_usd=accounting['combined_conservative_usd'],native_package_sha256=digest(native),
                   ordinary_review_task_bound=3*native['scope']['result_slots'],accepted_review_task_bound=2*native['scope']['result_slots'],
                   disclosure='Later provider capture/index window; all old outcomes/reviews retained. New pooled ideals may move other scores without grade changes.',
                   partial_stop='Preserve all child successes, failures, uncertainty and held costs; resume existing ledger only. No false full-grid-success claim or automatic extra budget.')
    save_same(root/'native/config.json',config);save_same(root/'native/package.json',native);save_same(root/'completion.json',package)
    save_same(root/'approval-template.json',dict(decision='NOT_APPROVED',package_sha256=digest(package),approved_by='',approved_at=''))
    return package


def verify(path):
    path = Path(path).resolve();p = load(path)
    if (p.get('profile') != PROFILE or p['code'] != code_bindings() or p['assets'] != asset_bindings() or p['output_directory'] != str(path.parent)):
        raise ValueError('provider-completion code/assets/output differs')
    summary = read(p['source_summary']);_,gate,snapshot = _prefix(p['source_prefix']);template = read(p['template']);accounting = read(p['shared_accounting'])
    if p['source_report'] != gate['report'] or p['source_snapshot'] != bound(p['source_snapshot']['path']) or summary['source_report'] != gate['report']:
        raise ValueError('provider-completion source binding differs')
    authority = profile_authority.read(p['accepted_profile']['package_path'],template['configuration']['reviewers'])
    if authority != p['accepted_profile']:raise ValueError('provider-completion authority changed')
    clean_run._same_choices(summary['projected']['active_review_profile'],authority,allowed={'preparation.py','provider_completion.py'})
    grid,manifest,questions = _cells(summary['projected'],p['cells'],template['providers'])
    native = load(path.parent/'native/package.json')
    if (manifest != p['cells'] or questions != p['questions'] or native['questions'] != questions
            or digest(native) != p['native_package_sha256'] or native['code'] != p['code'] or native['assets'] != p['assets']
            or native['providers'] != template['providers'] or native['configuration']['live_profile_authority'] != authority
            or {(r['configuration_id'],r['question_id']) for r in native['search_requests']} != {(r['configuration_id'],r['question_id']) for r in manifest}
            or native['configuration'].get('search_provider_ids') != [manifest[0]['configuration_id']]
            or p['prior_committed_and_held_usd'] != accounting['combined_conservative_usd']
            or Decimal(native['configuration']['execution_policy']['spending_ceiling_usd']) != Decimal(p['shared_ceiling_usd'])-Decimal(p['prior_committed_and_held_usd'])):
        raise ValueError('provider-completion native/cell/accounting scope differs')
    from .preparation import verify_package
    verify_package(native)
    return p,native,summary,snapshot


def record_plan_review(path, receipt):
    p,_,_,_ = verify(path)
    if (receipt.get('decision') not in {'continue','stop'} or receipt.get('package_sha256') != digest(p)
            or receipt.get('source_prefix') != p['source_prefix'] or receipt.get('source_summary') != p['source_summary']
            or not receipt.get('reviewed_by') or not receipt.get('reviewed_at')
            or type(receipt.get('material_method_change')) is not bool or not isinstance(receipt.get('invariant_failures'),list)
            or receipt['decision'] == 'continue' and (receipt['material_method_change'] or receipt['invariant_failures'])):
        raise ValueError('exact external recovery-plan review receipt required')
    save_same(Path(path).parent/'plan-review.json',receipt);return receipt


def _child_reviews(root,p,native,approval,fixture_call):
    review_path = root/'review/package.json'
    if not review_path.exists():review_pipeline.prepare_review(root/'native/package.json',[q['question_id'] for q in p['questions']],root/'review')
    child = load(review_path)
    if (child['source_package_sha256'] != digest(native) or child['questions'] != p['questions']
            or len(child['jobs']) > p['ordinary_review_task_bound'] or child.get('live_profile_authority') != p['accepted_profile']):
        raise ValueError('ordinary child review escaped completion scope')
    receipt = dict(approved=True,package_sha256=digest(child),parent_provider_completion_sha256=digest(p),
                   approved_by=approval['approved_by'],approved_at=approval['approved_at'])
    save_same(root/'review/approval.json',receipt)
    closed = root/'review/run/closure.json'
    result = load(root/'review/run/report.json') if closed.exists() and load(closed)['execution']['state'] == 'completed' else review_pipeline.run_review(review_path,approval=receipt,call=fixture_call)
    if result['execution']['state'] != 'completed':return None,dict(state=result['execution']['state'],stage='ordinary_review',execution=result['execution'])
    component = checkpoint.completed_review(review_path)
    cases = component[3]
    case_refs = {r['case_id']:dict(path=str((review_path.parent/r['path']).resolve()),sha256=r['sha256']) for r in child['cases']}
    child_report = root/'child-assembled/report/report.json'
    if not child_report.exists():
        result = dict(result,questions=deepcopy(p['questions']),phase_costs=report_costs(native,Path(native['configuration']['run_directory'])),active_review_profile=p['accepted_profile'])
        write_reports(child_report.parent,result)
    snapshot_path = root/'child-assembled/verified-parent.json'
    if not snapshot_path.exists():
        trusted = settlement.VerifiedCases({cid:settlement.scoring_fact(c) for cid,c in cases.items()}, {}, p['accepted_profile'])
        settlement.snapshot_parent(child_report,trusted,native,snapshot_path.parent,case_refs=case_refs,
                                   basis=[bound(root/'review/run/closure.json'),bound(root/'native-completion-audit.json')])
    snapshot = bound(snapshot_path)
    accepted_path = root/'child-accepted/review-checkpoint.json'
    if not accepted_path.exists():
        accepted_review.prepare(child_report,p['accepted_profile']['package_path'],accepted_path.parent,
                                allow_inference=True,maximum_tasks=p['accepted_review_task_bound'],mode=p['mode'],verified_parent=snapshot)
    accepted = load(accepted_path)
    if (accepted['parent_report_path'] != str(child_report.resolve()) or accepted['accepted_profile'] != p['accepted_profile']
            or accepted['maximum_review_tasks'] != p['accepted_review_task_bound'] or accepted['mode'] != p['mode']):
        raise ValueError('accepted child review escaped completion scope')
    receipt = dict(approved=True,package_sha256=digest(accepted),parent_provider_completion_sha256=digest(p),
                   approved_by=approval['approved_by'],approved_at=approval['approved_at'])
    result = accepted_review.run(accepted_path,receipt,call=fixture_call)
    execution = load(root/'child-accepted/closure.json')['execution']
    if execution['state'] != 'completed':return None,dict(state=execution['state'],stage='accepted_review',execution=execution)
    return (result,cases,case_refs),None


def _combined_costs(p,summary,native):
    accounting = read(p['shared_accounting']);old = summary['projected']['phase_costs'];new = report_costs(native,Path(native['configuration']['run_directory']))
    diagnostics = Decimal(accounting['separate_diagnostics_usd'])
    costs = dict(old)
    for key in checkpoint.COST_KEYS:
        extra = diagnostics if key in {'other_conservative_holds_and_allowances_usd','conservative_total_usd','published_pro_plus_conservative_residual_usd'} else Decimal(0)
        costs[key] = str(Decimal(old[key])+Decimal(new[key])+extra)
    if Decimal(costs['conservative_total_usd']) > Decimal(p['shared_ceiling_usd']):raise ValueError('completion exceeded shared ceiling')
    costs['components'] = old.get('components',[])+[dict(name='retained-separate-diagnostics',conservative_total_usd=str(diagnostics),source=p['shared_accounting']),dict(name='new-provider-cell-completion',**new)]
    return costs


def merge_reports(parent,child,p,native,snapshot,child_cases,costs):
    """Typed replacement only; never the old overlapping checkpoint/recollection flags."""
    summary = read(p['source_summary'])['projected']
    if (parent['questions'] != summary['questions'] or parent['collection'] != summary['collection']
            or digest(parent['outcomes']) != p['source_outcomes_sha256']):raise ValueError('merge source report changed')
    old_cells,_,questions = _cells(summary,p['cells'],native['providers'])
    declared = {(r['configuration_id'],r['question_id']) for r in p['cells']}
    new_cells = {(r['configuration_id'],r['question_id']):r for r in child['collection']}
    if (len(new_cells) != len(child['collection']) or set(new_cells) != declared or child['questions'] != questions
            or any(r.get('dispatch_state') == 'uncertain' for r in new_cells.values())):
        raise ValueError('child completion cell/question scope differs')
    old_ids = {o['case_id'] for o in parent['outcomes']};new_ids = {o['case_id'] for o in child['outcomes']}
    if (old_ids & new_ids or len(new_ids) != len(child['outcomes'])
            or any((o['configuration_id'],o['question_id']) in declared for o in parent['outcomes'])
            or any((o['configuration_id'],o['question_id']) not in declared for o in child['outcomes'])):
        raise ValueError('completion would replace existing outcome or inject unrelated case')
    report = dict(parent)
    report.update(mode=PROFILE,package_sha256=digest(p),generated_at=now(),outcomes=parent['outcomes']+deepcopy(child['outcomes']),
                  collection=[deepcopy(new_cells.get(key,row)) for key,row in old_cells.items()],phase_costs=costs,
                  active_review_profile=p['accepted_profile'],execution=dict(state='completed',held_tasks=[],reused_tasks=0))
    remaining = [r for r in parent.get('deferred_collection',[]) if (r['configuration_id'],r['question_id']) not in declared]
    if remaining:report['deferred_collection'] = remaining
    else:report.pop('deferred_collection',None)
    report['provider_completion'] = dict(profile=PROFILE,source_report=p['source_report'],source_prefix=p['source_prefix'],cells=p['cells'],
        previous_collection=[deepcopy(old_cells[key]) for key in new_cells],source_outcomes_sha256=p['source_outcomes_sha256'],
        preserved_outcomes=len(snapshot['facts']),new_outcomes=len(child['outcomes']),later_capture=True,
        source_method=summary['active_review_profile'],new_method=p['accepted_profile'],remaining_unanswered_completed_cells=sum(r['status'] not in SUCCESS for r in new_cells.values()))
    report['provider_completion']['full_provider_grid_success'] = all(r['status'] in SUCCESS for r in report['collection'])
    component_root = Path(p['output_directory'])
    report['phase_components'] = parent.get('phase_components',[])+[dict(profile=PROFILE,package_sha256=digest(p),native_package_sha256=digest(native),
        question_ids=[q['question_id'] for q in p['questions']],adjudication_profile=load(component_root/'review/package.json')['adjudication_profile'],
        report_sha256=file_digest(component_root/'review/run/report.json'),
        ordinary_review=bound(component_root/'review/run/closure.json'),accepted_review=bound(component_root/'child-accepted/closure.json'))]
    report['review_pipeline'] = dict(parent['review_pipeline'],records=parent['review_pipeline']['records']+deepcopy(child['review_pipeline']['records']),
                                    final_unknowns=sum(o['grade'] is None for o in report['outcomes']))
    old_publication = parent.get('publication_review',{});new_publication = child.get('publication_review',{})
    if old_publication or new_publication:
        report['publication_review'] = dict(old_publication or new_publication,records=old_publication.get('records',[])+deepcopy(new_publication.get('records',[])))
    report['outcome_counts'] = dict(Counter('unknown' if o['grade'] is None else str(o['grade']) for o in report['outcomes']))
    report['report_limits'] = parent.get('report_limits',[])+[p['disclosure'],
        'Only declared failed/deferred provider cells were newly collected. Old unknown grades did not trigger new calls. Original closed reports are immutable; this later report does not replace them.']
    if len(report['collection']) != p['source_cell_count'] or len(report['questions']) != p['source_question_count']:
        raise ValueError('completed collection grid differs')
    facts = dict(snapshot['facts']);facts.update({cid:settlement.scoring_fact(c) for cid,c in child_cases.items()})
    trusted = settlement.VerifiedCases(facts,{},p['accepted_profile'])
    settlement._rescore(report,trusted,native)
    inventory = {}
    for provider in native['providers']:
        cid = provider['configuration_id'];own = [v['compiled']['input'] for v in facts.values() if v['original']['configuration_id'] == cid]
        times = sorted(v['captured_at'] for v in own)
        inventory[cid] = dict(earliest_capture=times[0] if times else None,latest_capture=times[-1] if times else None,
                             field_states={field:dict(Counter(v['native_field_states'].get(field,'missing') for v in own)) for field in ('title','snippet')})
    report['evidence_inventory'] = inventory
    if report['outcomes'][:len(parent['outcomes'])] != parent['outcomes']:raise ValueError('retained outcomes changed')
    return report,trusted


def run(path, approval, *, env_file=None, fixture_call=None):
    path = Path(path).resolve();root = path.parent;p = load(path)
    with Ledger(root/'coordinator',dict(package_sha256=digest(p),mode=p['mode'])).locked():
        p,native,summary,snapshot = verify(path)
        if (approval.get('decision') != 'approved' or approval.get('package_sha256') != digest(p)
                or not approval.get('approved_by') or not approval.get('approved_at') or p['mode'] == 'live' and fixture_call is not None):
            raise ValueError('exact provider-completion owner approval and frozen transport required')
        if p['mode'] == 'live':
            plan = load(root/'plan-review.json');record_plan_review(path,plan)
            if plan['decision'] != 'continue':raise ValueError('independent completion plan did not pass')
        save_same(root/'approval.json',approval)
        _verify_file(p['source_report'])  # No historical tree decode or repeat sends.
        validation = root/'source-validation.json'
        expected_validation = dict(package_sha256=digest(p),source_summary=p['source_summary'],source_report=p['source_report'])
        if validation.exists():
            if load(validation) != expected_validation:raise ValueError('saved source validation differs')
        else:
            derived = prepare_source_summary(p['source_prefix'],root/'validated-source')
            if read(derived) != summary:raise ValueError('source summary does not derive from actual source questions/collection')
            save_same(validation,expected_validation)
        if (root/'gate-evidence.json').exists():
            gate = load(root/'gate-evidence.json')
            if gate['completion_package_sha256'] != digest(p) or gate['source_prefix'] != p['source_prefix']:
                raise ValueError('saved completion gate differs')
            _verify_file(gate['report'])
            result = dict(state='awaiting_stage_review',stage_index=1,evidence=gate,report=str(root/'report/report.html'))
            if (root/'stage-review.json').exists():
                review = load(root/'stage-review.json');clean_run._validate_review(review,gate)
                result['state'] = 'completed' if review['decision'] == 'continue' else 'stopped'
            write_private(root/'state.json',result,replace=True);return result
        ordinary = dict(approval,package_sha256=digest(native),parent_provider_completion_sha256=digest(p))
        save_same(root/'native/approval.json',ordinary)
        native_report = Path(native['configuration']['run_directory'])/'report.json'
        result = load(native_report) if native_report.exists() else None
        if not result or result['execution']['state'] != 'completed':result = runner.run(native,approval=ordinary,env_file=env_file)
        if result['package_sha256'] != digest(native):raise ValueError('native completion package differs')
        if result['execution']['state'] != 'completed':
            state = dict(state=result['execution']['state'],stage='native',execution=result['execution']);write_private(root/'state.json',state,replace=True);return state
        audit_path = root/'native-completion-audit.json'
        if not audit_path.exists():save_same(audit_path,clean_run._audit(native))
        audit = load(audit_path)
        if audit['unclassified']:raise ValueError('cached native audit is unclassified')
        _verify_file(dict(path=audit['source_ledger'],sha256=audit['source_ledger_sha256']))
        child,held = _child_reviews(root,p,native,approval,fixture_call)
        if held:write_private(root/'state.json',held,replace=True);return held
        final_child,child_cases,child_refs = child
        closure_path = root/'closure.json'
        if closure_path.exists():
            closure = load(closure_path)
            if (closure['package_sha256'] != digest(p) or closure['source_report'] != p['source_report']
                    or closure['source_outcomes_sha256'] != p['source_outcomes_sha256']):
                raise ValueError('saved completion closure differs')
            report = load(_verify_file(closure['report']))
            retained = [o for o in report['outcomes'] if o['case_id'] in snapshot['facts']]
            if digest(retained) != p['source_outcomes_sha256']:
                raise ValueError('saved completion changed retained outcomes')
            costs = _combined_costs(p,summary,native)
            if costs != closure['costs'] or report['phase_costs'] != costs:
                raise ValueError('saved completion accounting differs')
            facts = dict(snapshot['facts']);facts.update({cid:settlement.scoring_fact(c) for cid,c in child_cases.items()})
            trusted = settlement.VerifiedCases(facts,{},p['accepted_profile'])
        else:
            parent = load(p['source_report']['path'])  # Single deep historical decode, at integration only.
            costs = _combined_costs(p,summary,native)
            report,trusted = merge_reports(parent,final_child,p,native,snapshot,child_cases,costs)
            output = root/'report';write_reports(output,report)
            closure = dict(profile=PROFILE,package_sha256=digest(p),report=bound(output/'report.json'),source_report=p['source_report'],
                           source_outcomes_sha256=p['source_outcomes_sha256'],new_outcomes=len(final_child['outcomes']),costs=costs,
                           native_audit=bound(root/'native-completion-audit.json'),ordinary_closure=bound(root/'review/run/closure.json'),accepted_closure=bound(root/'child-accepted/closure.json'))
            save_same(closure_path,closure)
        refs = dict(snapshot['case_refs']);refs.update(child_refs)
        final_snapshot_path = root/'accepted/verified-parent.json'
        if final_snapshot_path.exists():
            final_snapshot = bound(final_snapshot_path)
            settlement.verify_snapshot(final_snapshot,root/'report/report.json',authority=p['accepted_profile'])
        else:
            final_snapshot = settlement.snapshot_parent(root/'report/report.json',trusted,native,root/'accepted',case_refs=refs,basis=[bound(closure_path)])
        clean_run._closed_index_checks([root/'native',root/'review',root/'child-accepted'])
        files = clean_run._evidence_files([root])
        files -= {str(root/name) for name in ('evidence-closure.json','gate-evidence.json','stage-review.json','prefix.json','state.json')}
        evidence_closure = dict(profile='provider-completion-evidence-closure-v1',parent_closure=read(p['source_prefix'])['closure'],files=file_manifest(files))
        save_same(root/'evidence-closure.json',evidence_closure)
        gate = dict(profile=PROFILE,stage_index=1,completion_package_sha256=digest(p),report=closure['report'],snapshot=final_snapshot,
                    source_prefix=p['source_prefix'],source_summary=p['source_summary'],evidence_closure=bound(root/'evidence-closure.json'),
                    native_audit=closure['native_audit'],ordinary_closure=closure['ordinary_closure'],accepted_closure=closure['accepted_closure'],
                    question_ids=[q['question_id'] for q in report['questions']],costs=costs,accepted_profile_sha256=digest(p['accepted_profile']),
                    unclassified_native_completions=0,undeclared_duplicate_tasks=0,preserved_outcomes=len(snapshot['facts']),
                    remaining_unanswered_completed_cells=report['provider_completion']['remaining_unanswered_completed_cells'],
                    full_provider_grid_success=report['provider_completion']['full_provider_grid_success'])
        save_same(root/'gate-evidence.json',gate)
        state = dict(state='awaiting_stage_review',stage_index=1,evidence=gate,report=str(root/'report/report.html'))
        write_private(root/'state.json',state,replace=True);return state


def status(path):
    """Cheap observation only; this does not grant approval or authenticate history."""
    root = Path(path).resolve().parent
    return load(root/'state.json') if (root/'state.json').exists() else dict(state='prepared',stage='not_started')


def record_stage_review(path,receipt):
    p,_,_,_ = verify(path);root = Path(path).resolve().parent;gate = load(root/'gate-evidence.json')
    clean_run._validate_review(receipt,gate);save_same(root/'stage-review.json',receipt)
    if receipt['decision'] == 'continue':
        prefix = dict(profile='verified-provider-completion-prefix-v1',snapshot=gate['snapshot'],gate=bound(root/'gate-evidence.json'),
                      review=bound(root/'stage-review.json'),closure=gate['evidence_closure'],question_ids=gate['question_ids'],costs=gate['costs'])
        save_same(root/'prefix.json',prefix)
    return receipt


def load_completed(path):
    report_path = Path(path).resolve();root = report_path.parent.parent;p,native,_,_ = verify(root/'completion.json')
    gate = load(root/'gate-evidence.json');review = load(root/'stage-review.json');clean_run._validate_review(review,gate)
    if review['decision'] != 'continue' or gate['report'] != bound(report_path):raise ValueError('completed provider report lacks external continue review')
    read(gate['evidence_closure'])
    return settlement.read_snapshot(gate['snapshot'],report_path,authority=p['accepted_profile'])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__);sub = parser.add_subparsers(dest='action',required=True)
    summary = sub.add_parser('source-summary');summary.add_argument('--prefix',type=Path,required=True);summary.add_argument('--output',type=Path,required=True)
    prep = sub.add_parser('prepare');prep.add_argument('--inputs',type=Path,required=True)
    for name in ('status','run','record-plan-review','record-stage-review'):
        command = sub.add_parser(name);command.add_argument('--package',type=Path,required=True)
        if name != 'status':command.add_argument('--receipt',type=Path,required=True)
        if name == 'run':command.add_argument('--env-file',type=Path)
    args = parser.parse_args(argv)
    if args.action == 'source-summary':result = prepare_source_summary(bound(args.prefix),args.output)
    elif args.action == 'prepare':result = prepare(**load(args.inputs))
    elif args.action == 'status':result = status(args.package)
    elif args.action == 'run':result = run(args.package,load(args.receipt),env_file=args.env_file)
    elif args.action == 'record-plan-review':result = record_plan_review(args.package,load(args.receipt))
    else:result = record_stage_review(args.package,load(args.receipt))
    print(json.dumps(result,sort_keys=True));return 0


if __name__ == '__main__':raise SystemExit(main())
