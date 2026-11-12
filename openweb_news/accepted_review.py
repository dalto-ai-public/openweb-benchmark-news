"""One approved post-checkpoint review step, with reusable profile authority."""
from collections import Counter
from pathlib import Path

from . import settlement, profile_authority
from .common import asset_bindings,code_bindings,digest,load,now,private_directory,write_private
from .ledger import Ledger

PROFILE='accepted-review-checkpoint-v1'
REPORT_FILES=('report.json','report.html','report.md','phase-summary.json','providers.csv',
    'summary.csv','metrics.csv','pairwise.csv')


def prepare(parent_report,accepted_profile,directory,*,allow_inference=False,maximum_tasks=None,mode='live',verified_parent=None):
    source=Path(parent_report).resolve();root=Path(directory).resolve()
    if root.is_relative_to(source.parent.parent)or source.is_relative_to(root):
        raise ValueError('review checkpoint must preserve parent run')
    if mode not in {'live','fixture'}or type(allow_inference)is not bool:
        raise ValueError('review checkpoint mode differs')
    evidence=(settlement.read_snapshot(verified_parent,source) if verified_parent
        else settlement.load_parent(source))
    authority=profile_authority.read(accepted_profile,evidence[2]['configuration']['reviewers'])
    source_config=evidence[2]['configuration']
    source_authority=source_config.get('live_profile_authority')
    if (source_config.get('judge_input_profile') or (source_authority or {}).get('profile') == profile_authority.METHOD_PROFILE) and authority != source_authority:
        raise ValueError('review checkpoint source method authority differs')
    if authority.get('profile') == profile_authority.METHOD_PROFILE:
        if (evidence[2]['mode'] != mode or evidence[2]['configuration'].get('live_profile_authority') != authority):
            raise ValueError('review checkpoint source method authority or mode differs')
        profile_authority.verify_native(evidence[2]['configuration'],mode=mode)
    count=2*len(settlement.selected(evidence[0],evidence[1],evidence[2]['configuration']['reviewers'],authority=authority))
    maximum=count if maximum_tasks is None else maximum_tasks
    if type(maximum)is not int or maximum<0 or count>maximum:
        raise ValueError('review tasks exceed declared scope')
    if mode=='live'and count and not allow_inference:
        raise ValueError('report-only checkpoint would dispatch reviews')
    child=settlement.prepare_settlement(source,root/'publication',accepted_profile=accepted_profile,source_evidence=evidence,maximum_tasks=maximum,mode=mode,verified_parent=verified_parent)
    if len(child['jobs'])!=count or child['accepted_profile']!=authority:raise ValueError('review plan changed during preparation')
    p=dict(profile=PROFILE,created_at=now(),mode=mode,code=code_bindings(),assets=asset_bindings(),
        parent_report_path=str(source),parent_report_sha256=digest(source.read_bytes()),
        accepted_profile=child['accepted_profile'],child_package_sha256=digest(child),
        review_tasks=count,maximum_review_tasks=maximum,allow_inference=allow_inference,
        output_directory=str(root),scope='Only declared unknown-case publication reviews; reuse terminal evidence; no searches, Pro calls, calibration or known-grade rejudging.')
    if profile_authority.profiles(authority):p['live_profiles']=profile_authority.profiles(authority)
    write_private(root/'review-checkpoint.json',p)
    return p


def verify(p,root,*,historical=False,seen=()):
    root=Path(root).resolve();source=Path(p['parent_report_path']).resolve()
    if (p['profile']!=PROFILE or Path(p['output_directory']).resolve()!=root
            or p['mode']not in {'live','fixture'}or type(p['allow_inference'])is not bool
            or type(p['maximum_review_tasks'])is not int or p['maximum_review_tasks']<0
            or root.is_relative_to(source.parent.parent)or source.is_relative_to(root)
            or(not historical and(p['code']!=code_bindings()or p['assets']!=asset_bindings()))
            or not all(asset_bindings().get(k)==v for k,v in p['assets'].items())
            or digest(source.read_bytes())!=p['parent_report_sha256']):
        raise ValueError('review checkpoint binding changed')
    child=load(root/'publication/package.json')
    if (digest(child)!=p['child_package_sha256']or child['mode']!=p['mode']
            or child['accepted_profile']!=p['accepted_profile']or child['parent_report_path']!=str(source)
            or child['run_directory']!=str(root/'publication/run')
            or p['review_tasks']!=len(child['jobs'])or len(child['jobs'])>p['maximum_review_tasks']
            or(p['mode']=='live'and child['jobs']and not p['allow_inference'])):
        raise ValueError('declared review child differs')
    if p.get('live_profiles',{})!=profile_authority.profiles(p['accepted_profile']):
        raise ValueError('review checkpoint profiles differ')
    evidence=settlement.verify(child,root/'publication',historical=historical,seen=seen)
    return child,evidence


def _result_source(p,root):
    return (Path(p['parent_report_path'])if not p['review_tasks']else Path(root)/'publication/run/report.json')


def _summary(p,report):
    records=report.get('publication_review',{}).get('records',[])
    latest={r['case_id']:r for r in records if r.get('checker_profile')==p['accepted_profile']['profile_bindings']['checker_profile']}
    return dict(profile=PROFILE,accepted_profile=p['accepted_profile'],declared_review_tasks=p['review_tasks'],
        validity_counts=dict(Counter(role+':'+('valid'if audit['valid']else'invalid')for r in latest.values()for role,audit in r['audits'].items())),
        unresolved_results=sum(o['grade']is None for o in report['outcomes']),
        calibration_repeated=False,known_grade_reviews=0,
        limits='Per-review validity is separate from execution and owner profile acceptance; invalid reviews cannot supply a vote.')


def run(path,approval,*,call=None):
    path=Path(path).resolve();p=load(path);root=path.parent
    with Ledger(root/'coordinator',dict(package_sha256=digest(p),mode=p['mode'])).locked():
        child,_=verify(p,root)
        del _  # Do not retain a decoded cumulative report during child review.
        if p['mode']=='live'and(approval.get('approved')is not True or approval.get('package_sha256')!=digest(p)
                or not approval.get('approved_by')or not approval.get('approved_at')or call is not None):
            raise ValueError('exact review checkpoint approval and frozen transport required')
        if(root/'approval.json').exists():
            if digest(load(root/'approval.json'))!=digest(approval):raise ValueError('review checkpoint approval changed')
            if(root/'closure.json').exists()and load(root/'closure.json')['execution']['state']=='completed':
                if p['mode']=='fixture':return load(root/'report/report.json')
                return load_completed(root/'report/report.json')[0]
        else:write_private(root/'approval.json',approval)
        if child['jobs']:
            receipt=dict(approved=True,package_sha256=digest(child),parent_review_checkpoint_sha256=digest(p),
                approved_by=approval.get('approved_by','fixture'),approved_at=approval.get('approved_at','fixture'))
            child_receipt=root/'publication/approval.json'
            if child_receipt.exists():
                if load(child_receipt)!=receipt:raise ValueError('derived review approval changed')
            else:write_private(child_receipt,receipt)
            report=settlement.run_settlement(root/'publication/package.json',receipt,call=call)
            execution=report['execution']
        else:
            report=load(p['parent_report_path']);execution=dict(state='completed',review_tasks=0)
        source=_result_source(p,root)
        output=private_directory(root/'report');copied={}
        for name in REPORT_FILES:
            file=source.parent/name
            if file.exists():
                data=file.read_bytes();write_private(output/name,data,replace=True);copied[name]=digest(data)
        summary=_summary(p,report);write_private(root/'review-summary.json',summary,replace=True)
        write_private(root/'closure.json',dict(package_sha256=digest(p),result_source_path=str(source),
            execution=execution,report_files=copied,summary_sha256=digest(summary),
            source_report_sha256=p['parent_report_sha256'],accepted_profile_sha256=digest(p['accepted_profile']),
            new_search_calls=0,new_pro_calls=0,calibration_repeated=False),replace=True)
        return report


def load_completed(path,*,seen=()):
    path=Path(path).resolve();root=path.parent.parent;p=load(root/'review-checkpoint.json')
    if path in seen:raise ValueError('cyclic review checkpoint')
    child,evidence=verify(p,root,historical=True,seen=(*seen,path))
    approval=load(root/'approval.json');closure=load(root/'closure.json')
    if p['mode']!='live'and child['jobs'] and not settlement.fixture_method(child):
        raise ValueError('fixture review is not completed live evidence')
    if p['mode']=='live'and(approval.get('approved')is not True or approval.get('package_sha256')!=digest(p)):
        raise ValueError('review checkpoint receipt differs')
    source=_result_source(p,root)
    if (closure['package_sha256']!=digest(p)or closure['execution']['state']!='completed'
            or closure['result_source_path']!=str(source)
            or closure['source_report_sha256']!=p['parent_report_sha256']
            or closure['accepted_profile_sha256']!=digest(p['accepted_profile'])
            or closure['summary_sha256']!=digest(load(root/'review-summary.json'))
            or 'report.json'not in closure['report_files']):
        raise ValueError('review checkpoint closure differs')
    for name,sha in closure['report_files'].items():
        if name not in REPORT_FILES or digest((root/'report'/name).read_bytes())!=sha or digest((source.parent/name).read_bytes())!=sha:
            raise ValueError('review checkpoint report changed')
    report,cases,native=(settlement.load_parent(source,seen=(*seen,path))if child['jobs']else evidence)
    if _summary(p,report)!=load(root/'review-summary.json'):raise ValueError('review summary does not replay')
    return report,cases,native
