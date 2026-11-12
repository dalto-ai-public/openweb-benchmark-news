"""Blind saved-evidence negative controls; never changes benchmark grades."""
from copy import deepcopy
from pathlib import Path
import time

from . import publication
from .common import asset_bindings, code_bindings, digest, load, now, private_directory, records, write_private
from .completion_audit import inspect_ledger
from .review_pipeline import review_execution
from .settlement import load_parent, jobs_for, _read_case

PROFILE = 'publication-negative-control-calibration-v2'
LEGACY_PROFILE = 'publication-negative-control-calibration-v1'


def _derive(source, selection, root, cases, native, *, created_at, mode='live', build_identity=None, profile=PROFILE):
    rows = selection['cases']
    ids = [r['case_id'] for r in rows]
    if not ids or len(set(ids)) != len(ids)or any(not isinstance(r.get('category'),str)or not r['category'].strip()for r in rows):
        raise ValueError('control bank needs distinct cases and explicit reviewed categories')
    if profile not in {PROFILE,LEGACY_PROFILE}:raise ValueError('unknown calibration profile')
    for row in rows:
        c = cases[row['case_id']]
        texts = {r['field']: r['text'] for r in c['compiled']['packet']['allowed_text']}
        if not row['reason'].strip() or not row['support']:
            raise ValueError('control needs evidence-bound selection rationale')
        for quote in row['support']:
            publication._span(quote, texts)
    cfg = native['configuration']; policy = cfg['execution_policy']
    reviewers = cfg['reviewers']
    if (reviewers['codex']['backend'] != 'codex-subscription'
            or reviewers['codex'].get('transport') != 'app-server-stdio-v1'
            or reviewers['claude']['backend'] != 'claude-subscription'):
        raise ValueError('isolated subscription reviewers required')
    jobs = jobs_for([dict(case_id=cid) for cid in ids], cases, reviewers)
    for job in jobs:
        job.update(task_id=job['task_id'].replace('publication-', 'calibration-', 1), stage='calibration')
    return dict(profile=profile, created_at=created_at, mode=mode, code=code_bindings(), assets=asset_bindings(),
        checker_profile=publication.CHECKER_PROFILE, source_report_path=str(source),
        source_report_sha256=digest(source.read_bytes()), selection_sha256=digest(selection),
        cases=[dict(case_id=cid, path='inputs/'+cid+'.json', sha256=digest(cases[cid])) for cid in ids],
        reviewers=deepcopy(reviewers), jobs=jobs, run_directory=str(root/'run'), build_identity=build_identity,
        scheduling={r+'-subscription':deepcopy(policy['scheduling']['accounts'][r+'-subscription']) for r in ('codex','claude')},
        recovery={r+'-subscription':deepcopy(policy['scheduling']['recovery'][r+'-subscription']) for r in ('codex','claude')},
        automatic_recovery=deepcopy(cfg.get('automatic_recovery')),
        max_attempts_per_task=policy['max_attempts_per_task'], retry_policy=deepcopy(policy['retry_policy']),
        scope=('Five saved controls, ten blind subscription tasks; diagnostic only, including any known-grade cases; no searches, Pro calls, new questions or benchmark grade changes.'if profile==LEGACY_PROFILE else
            f'{len(ids)} reviewed saved controls, {len(jobs)} blind subscription tasks; diagnostic only, including any known-grade cases; no searches, Pro calls, new questions or benchmark grade changes.'),
        incremental_api_usd='0', maximum_new_subscription_tasks=len(jobs))


def prepare(source_report, selection_path, directory, *, build_identity=None):
    source=Path(source_report).resolve();root=Path(directory).resolve()
    if root.is_relative_to(source.parent.parent) or source.is_relative_to(root):
        raise ValueError('calibration must preserve source run')
    _,cases,native=load_parent(source)
    selection=load(selection_path)
    p=_derive(source,selection,root,cases,native,created_at=now(),build_identity=build_identity)
    private_directory(root)
    for ref in p['cases']:write_private(root/ref['path'],cases[ref['case_id']])
    write_private(root/'selection.json',selection);write_private(root/'package.json',p)
    return p


def verify(package, root, *, historical=False, source_evidence=None):
    root=Path(root).resolve();source=Path(package['source_report_path']).resolve()
    if root.is_relative_to(source.parent.parent) or source.is_relative_to(root):
        raise ValueError('calibration overlaps source run')
    if digest(source.read_bytes())!=package['source_report_sha256']:
        raise ValueError('calibration source changed')
    _,cases,native=load_parent(source) if source_evidence is None else source_evidence
    expected=_derive(source,load(root/'selection.json'),root,cases,native,
        created_at=package['created_at'],mode=package['mode'],build_identity=package['build_identity'],profile=package['profile'])
    if historical:
        if not all(asset_bindings().get(k)==v for k,v in package['assets'].items()):
            raise ValueError('historical calibration assets differ')
        expected.update(code=package['code'],assets=package['assets'])
    if package!=expected:raise ValueError('calibration code, scope or runtime policy changed')
    for ref in package['cases']:
        if _read_case(root,ref)!=cases[ref['case_id']]:raise ValueError('control differs from saved evidence')
    return cases


def _run(path, p, approval, *, call=None):
    cases=verify(p,path.parent)
    live=p['mode']=='live'
    if live and (approval.get('approved')is not True or approval.get('package_sha256')!=digest(p)
            or approval.get('diagnostic_known_grade_review')is not True
            or not approval.get('approved_by') or not approval.get('approved_at')):
        raise ValueError('exact diagnostic-only calibration approval required')
    if live and call is not None:raise ValueError('live calibration transport is frozen')
    if not live and (p['mode']!='fixture' or call is None):raise ValueError('fixture transport required')
    with review_execution(p,call=call) as (responses,stopped,pending,started,directory):
        audits=[];missing=[]
        for job in p['jobs']:
            raw=responses[job['task_id']]
            if not raw.get('native_completion_verified'):missing.append(job['task_id'])
            result=publication.validate_review(raw.get('verdict'),cases[job['case_id']]['compiled'],
                job['blind_packet'],checker_profile=p['checker_profile'])
            audits.append(dict(case_id=job['case_id'],role=job['role'],task_id=job['task_id'],
                response_sha256=digest(raw),audit=result,
                control_pass=bool(raw.get('native_completion_verified') and raw.get('transport_success')
                    and result['valid'] and result['grade'] in (0,1,None))))
        audit=inspect_ledger(directory) if live and not missing else None
        if audit and audit['unclassified']:raise ValueError('unclassified calibration completions')
        result=dict(profile=p['profile'],package_sha256=digest(p),source_report_sha256=p['source_report_sha256'],
            generated_at=now(),diagnostic_only=True,benchmark_grades_changed=False,
            calibration_pass=not missing and all(r['control_pass']for r in audits),audits=audits,
            execution=dict(state='completed_with_holds'if missing else'completed',held_tasks=missing,
                permanent_stop_roles=sorted(stopped),reused_tasks=len(p['jobs'])-len(pending),elapsed_seconds=time.monotonic()-started),
            new_search_calls=0,new_pro_calls=0,incremental_api_usd='0',
            limits='Targeted judgments are a diagnostic check, not a calibrated accuracy estimate or automatic permission to promote candidates.')
        write_private(directory/'calibration.json',result,replace=True)
        if audit:write_private(directory/'completion-audit.json',audit,replace=True)
        write_private(directory/'closure.json',dict(package_sha256=digest(p),
            calibration_sha256=digest((directory/'calibration.json').read_bytes()),source_report_sha256=p['source_report_sha256'],
            execution=result['execution'],diagnostic_only=True,
            native_ledger_sha256=audit['source_ledger_sha256']if audit else None,
            native_audit_records_sha256=digest(audit['records'])if audit else None),replace=True)
        return result


def run(path, approval, *, call=None):
    from .resilience import enabled
    from .codex_runtime import RuntimeStopped
    path=Path(path);p=load(path);ledger=Path(p['run_directory'])/'attempts.jsonl'
    def count():return sum(e['event']=='started'for e in records(ledger))if ledger.exists()else 0
    while True:
        before=count()
        try:result=_run(path,p,approval,call=call)
        except RuntimeStopped:
            if not enabled(p)or count()<=before:raise
            continue
        if (not enabled(p)or result['execution']['state']=='completed'
                or result['execution']['permanent_stop_roles']or count()<=before):return result
