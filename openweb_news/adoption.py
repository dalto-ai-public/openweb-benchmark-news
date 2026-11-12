"""Explicit owner acceptance of authenticated offline checker candidates; no calls."""
from copy import deepcopy
from pathlib import Path

from . import calibration, publication, settlement
from .common import asset_bindings, code_bindings, digest, load, now, private_directory, write_private
from .reporting import write_reports

PROFILE = 'publication-checker-adoption-v1'
DECODERS = ('publication.py','judging.py','representation.py','adjudication.py','schema.py','_method/evidence.py',
    '_method/two_step.py','_method/text_dates.py','_method/judgment_support.py','_method/relative_dates.py')


def _inputs(diagnostic, calibration_package, *, seen=()):
    diagnostic=Path(diagnostic).resolve();cp=Path(calibration_package).resolve()
    summary=load(diagnostic/'summary.json')
    rows=load(diagnostic/'case-audits.json')
    if (summary['profile']!='publication-checker-replay-diagnostic-v1'
            or summary['checker_profile']!=publication.CHECKER_PROFILE or not summary['candidate_not_adopted']
            or summary['case_audits_sha256']!=digest((diagnostic/'case-audits.json').read_bytes())):
        raise ValueError('candidate diagnostic binding differs')
    if any(summary['code'].get(name)!=code_bindings()[name] for name in DECODERS):
        raise ValueError('candidate semantic decoder changed')
    source=Path(summary['source_report_path']).resolve()
    if digest(source.read_bytes())!=summary['source_report_sha256']:raise ValueError('candidate parent changed')
    parent,cases,native=settlement.load_parent(source,seen=seen)
    source_package=load(source.parent.parent/'package.json')
    if digest(source_package)!=summary['source_package_sha256']:raise ValueError('review package changed')
    responses=settlement._completed_responses(source_package)
    by_id={row['case_id']:row for row in rows}
    if len(by_id)!=len(rows)or set(by_id)!={row['case_id']for row in source_package['roster']}:
        raise ValueError('candidate roster differs')
    outcomes={o['case_id']:o for o in parent['outcomes']}
    gains=[]
    for row in source_package['roster']:
        cid=row['case_id'];saved=by_id[cid]
        own={role:responses['publication-'+role+'-'+cid]for role in ('codex','claude')}
        result=publication.corroborate(cases[cid]['compiled'],publication.packet_for(cases[cid]['compiled']),
            own,checker_profile=publication.CHECKER_PROFILE)
        gained=result['accepted']and outcomes[cid]['grade']is None
        if (result['audits']!=saved['audits'] or result['accepted']!=saved['candidate_accepted']
                or result['grade']!=saved['candidate_grade'] or gained!=saved['would_change_unknown']
                or outcomes[cid]['grade']!=saved['current_grade']
                or any(digest(raw)!=saved['response_sha256'][role]for role,raw in own.items())):
            raise ValueError('candidate does not replay from native proof')
        if gained:gains.append(cid)
    p=load(cp);root=cp.parent;r=load(root/'run/calibration.json');closure=load(root/'run/closure.json');approval=load(root/'approval.json')
    if (p['mode']!='live'or Path(p['source_report_path']).resolve()!=source
            or p['source_report_sha256']!=summary['source_report_sha256']
            or approval.get('approved')is not True or approval.get('diagnostic_known_grade_review')is not True
            or approval.get('package_sha256')!=digest(p)or closure['package_sha256']!=digest(p)
            or closure['calibration_sha256']!=digest((root/'run/calibration.json').read_bytes())
            or r['execution']['state']!='completed'or not r['diagnostic_only']or r['benchmark_grades_changed']):
        raise ValueError('calibration source or approval differs')
    calibration.verify(p,root,historical=True,source_evidence=(parent,cases,native))
    if any(p['code'].get(name)!=code_bindings()[name]for name in DECODERS):
        raise ValueError('calibration semantic decoder changed')
    own=settlement._completed_responses(p);replay=[]
    for job in p['jobs']:
        raw=own[job['task_id']]
        audit=publication.validate_review(raw.get('verdict'),cases[job['case_id']]['compiled'],
            job['blind_packet'],checker_profile=p['checker_profile'])
        replay.append(dict(case_id=job['case_id'],role=job['role'],task_id=job['task_id'],
            response_sha256=digest(raw),audit=audit,control_pass=bool(raw.get('native_completion_verified')
                and raw.get('transport_success')and audit['valid']and audit['grade']in(0,1,None))))
    if replay!=r['audits']or r['calibration_pass']!=all(row['control_pass']for row in replay):
        raise ValueError('calibration result does not replay')
    bindings=dict(diagnostic_path=str(diagnostic),diagnostic_sha256=digest((diagnostic/'summary.json').read_bytes()),
        calibration_package_path=str(cp),calibration_package_sha256=digest(p),
        calibration_sha256=closure['calibration_sha256'],source_report_path=str(source),
        source_report_sha256=summary['source_report_sha256'],calibration_pass=r['calibration_pass'],
        promoted_case_ids=gains,candidate_outcome_counts=summary['candidate_outcome_counts'],
        calibration_valid_reviews=sum(row['audit']['valid']for row in replay),calibration_total_reviews=len(replay),
        profile_bindings=dict(checker_profile=publication.CHECKER_PROFILE,
            semantic_code={name:code_bindings()[name]for name in DECODERS},
            prompt_sha256=asset_bindings()['publication-review-v1.md'],
            policies_sha256=asset_bindings()['policies.json'],reviewers=source_package['reviewers']))
    return bindings,parent,cases,native,source_package,responses


def prepare(diagnostic, calibration_package, directory):
    root=Path(directory).resolve()
    bindings,*_=_inputs(diagnostic,calibration_package)
    sources=[Path(bindings['source_report_path']).parent.parent,Path(diagnostic).resolve(),Path(calibration_package).resolve().parent]
    if any(root.is_relative_to(s)or s.is_relative_to(root)for s in sources):
        raise ValueError('adoption must preserve all source packages')
    p=dict(profile=PROFILE,created_at=now(),code=code_bindings(),assets=asset_bindings(),
        checker_profile=publication.CHECKER_PROFILE,output_directory=str(root),**bindings)
    write_private(root/'package.json',p)
    return p


def _build(p,approval,*,historical=False,seen=()):
    if (p['profile']!=PROFILE or p['checker_profile']!=publication.CHECKER_PROFILE
            or(not historical and(p['code']!=code_bindings()or p['assets']!=asset_bindings()))
            or not all(asset_bindings().get(k)==v for k,v in p['assets'].items())):
        raise ValueError('adoption code or assets changed')
    if (approval.get('approved')is not True or approval.get('package_sha256')!=digest(p)
            or not approval.get('approved_by')or not approval.get('approved_at')
            or(not p['calibration_pass']and approval.get('accept_failed_calibration')is not True)):
        raise ValueError('exact owner acceptance including failed calibration required')
    bindings,parent,cases,native,source_package,responses=_inputs(p['diagnostic_path'],p['calibration_package_path'],seen=seen)
    if any(p[k]!=v for k,v in bindings.items()):raise ValueError('adoption source binding changed')
    source_package=deepcopy(source_package)
    unknown={o['case_id']for o in parent['outcomes']if o['grade']is None}
    source_package['roster']=[r for r in source_package['roster']if r['case_id']in unknown]
    for row in source_package['roster']:
        row['terminal_key']=digest(dict(checker_profile=p['checker_profile'],original_terminal_key=row['terminal_key']))
    source_package['parent_report_sha256']=p['source_report_sha256']
    report=settlement.overlay(parent,cases,native,source_package,responses,checker_profile=p['checker_profile'])
    old_by_id={o['case_id']:o for o in parent['outcomes']}
    changed={o['case_id']for o in report['outcomes']if o!=old_by_id[o['case_id']]}
    if changed!=set(p['promoted_case_ids'])or report['outcome_counts']!=p['candidate_outcome_counts']:
        raise ValueError('adopted changes differ from approved candidates')
    report.update(mode=PROFILE,package_sha256=digest(p),generated_at=p['created_at'],
        execution=dict(state='completed',kind='offline_owner_acceptance',new_model_calls=0))
    report['publication_review'].update(profile=p['checker_profile'],subscription_tasks=0,reused_subscription_tasks=len(responses))
    report['checker_acceptance']=dict(profile=p['checker_profile'],one_time_owner_exception=not p['calibration_pass'],
        calibration_pass=p['calibration_pass'],calibration_sha256=p['calibration_sha256'],approval_sha256=digest(approval),
        promoted_results=len(changed),new_model_calls=0)
    report['report_limits'].append(f"Checker candidates adopted under explicit owner acceptance. The calibration has {p['calibration_valid_reviews']} valid reviews out of {p['calibration_total_reviews']}; its original pass/fail result ({p['calibration_pass']}) is preserved. This acceptance is not a general accuracy claim or permission to accept future failed calibrations.")
    return report,cases,native


def run(package_path,approval):
    path=Path(package_path).resolve();p=load(path)
    if Path(p['output_directory']).resolve()!=path.parent:raise ValueError('adoption output changed')
    if (path.parent/'approval.json').exists():
        if digest(load(path.parent/'approval.json'))!=digest(approval):raise ValueError('adoption approval changed')
        if (path.parent/'closure.json').exists():return load_completed(path.parent/'report/report.json')[0]
    report,_,_=_build(p,approval)
    root=private_directory(path.parent)
    if not(root/'approval.json').exists():write_private(root/'approval.json',approval)
    write_reports(private_directory(root/'report'),report)
    write_private(root/'closure.json',_closure(p,digest((root/'report/report.json').read_bytes())))
    return report


def _closure(p,report_sha):
    return dict(package_sha256=digest(p),report_sha256=report_sha,parent_report_sha256=p['source_report_sha256'],
        calibration_sha256=p['calibration_sha256'],calibration_pass=p['calibration_pass'],
        one_time_owner_exception=not p['calibration_pass'],promoted_results=len(p['promoted_case_ids']),new_model_calls=0)


def load_completed(path,*,seen=()):
    path=Path(path).resolve();root=path.parent.parent;p=load(root/'package.json');report=load(path)
    if path in seen or Path(p['source_report_path']).resolve()in seen:
        raise ValueError('cyclic adoption source')
    closure=load(root/'closure.json');approval=load(root/'approval.json')
    if Path(p['output_directory']).resolve()!=root or closure!=_closure(p,digest(path.read_bytes())):
        raise ValueError('adoption closure differs')
    replay,cases,native=_build(p,approval,historical=True,seen=(*seen,path))
    if digest(replay)!=closure['report_sha256']:raise ValueError('adopted report does not replay')
    return report,cases,native
