"""Complete already-approved saved stages after a native proof classification stop."""
from collections import Counter
from pathlib import Path
import tempfile

from . import batch,checkpoint,review_pipeline
from .common import asset_bindings,code_bindings,digest,encoded,load,now,records,write_private,private_directory
from .completion_audit import inspect_ledger

PROFILE='saved-native-checkpoint-recovery-v1'
# These controller/transport/report modules cannot change grading inputs/rules.
CONTROLLERS={'quota.py','subscription_health.py','phase_report.py','checkpoint_recovery.py',
    'cli.py'}
# Exact reviewed routing-only migration. Other settlement/scoring bytes remain
# frozen; future edits require another reviewed migration, not a module waiver.
ROUTING_MIGRATIONS={'settlement.py':{
    ('971c4a802446c49cc1a0c8038c659bf113a45e7e60339c8c0b2b37de773b8820',
     '7ce552f90f8ad53dbb02e1f7114fd1e573de11c396058533f92632145d4a96dc')}}


def source(path,*,seen=()):
    path=Path(path).resolve();original=load(path);code=code_bindings()
    if(original['profile']!=batch.PROFILE or original['assets']!=asset_bindings()
            or any(original['code'].get(k)!=code.get(k)and(original['code'].get(k),code.get(k))not in ROUTING_MIGRATIONS.get(k,set())
                   for k in set(original['code'])|set(code)if k not in CONTROLLERS)):
        raise ValueError('recovery changed grading, method assets or unapproved runtime code')
    p,native,components=batch.verify(path,historical=True,seen=seen)
    approval=load(path.parent/'approval.json');run=Path(native['configuration']['run_directory']);report=load(run/'report.json')
    if(approval.get('decision')!='approved'or approval.get('package_sha256')!=digest(p)
            or not approval.get('approved_by')or not approval.get('approved_at')or native['mode']!='live'
            or report['package_sha256']!=digest(native)or report['execution']['state']!='completed'):
        raise ValueError('recovery requires the approved completed native stage')
    if report['source_versions']!=dict(code=native['code'],assets=native['assets']):
        raise ValueError('native report version differs')
    events=list(records(run/'attempts.jsonl'));starts={e['attempt_id']:e for e in events if e['event']=='started'}
    ends={e['attempt_id']:e for e in events if e['event']=='finished'}
    if set(starts)!=set(ends):raise ValueError('recovery cannot dispatch or repair unfinished native work')
    successes=Counter()
    for aid,event in starts.items():
        end=ends[aid];response_path=run/end['result_path'];request=load(run/'attempts'/aid/'request.json')
        if(digest(request)!=event['request_sha256']or digest(response_path.read_bytes())!=end['result_sha256']
                or event['task_id']!=end['task_id']):raise ValueError('native ledger binding changed')
        response=load(response_path)
        if response.get('transport_success')is True and response.get('dispatch_state')!='uncertain':successes[event['task_id']]+=1
    if any(n>1 for n in successes.values()):raise ValueError('native task has multiple successful finishes')
    audit=inspect_ledger(run)
    if audit['unclassified']:raise ValueError('unclassified native completion remains')
    stopped=load(path.parent/'native-completion-audit.json')
    if(not stopped['unclassified']or stopped['source_ledger_sha256']!=audit['source_ledger_sha256']
            or stopped['code']!=original['code']or stopped['assets']!=original['assets']):
        raise ValueError('recovery requires the retained original native-audit stop')
    return p,native,components,approval,report,audit


def _audit_fields(audit):
    return {k:audit[k]for k in('source_ledger_sha256','counts','unclassified','records')}


def prepare(checkpoint_path,directory):
    path=Path(checkpoint_path).resolve();root=Path(directory).resolve()
    if root.is_relative_to(path.parent)or path.parent.is_relative_to(root):raise ValueError('recovery must preserve original checkpoint')
    marker=path.parent/'completion-recovery.json'
    if marker.exists():
        bound=load(marker)
        if bound['output_directory']!=str(root):raise ValueError('checkpoint already has a bound continuation; resume it')
        verify(root/'package.json')
        return load(root/'package.json')
    original,native,_,approval,_,audit=source(path)
    downstream=Path(original['accepted_review_directory'])
    if(any((path.parent/name).exists()for name in('closure.json','pipeline-closure.json','review/package.json'))
            or downstream.exists()):
        raise ValueError('checkpoint has downstream work; it must be reused, not redispatched')
    prior=Path(original['parent_report_path']).parent.parent
    if root.is_relative_to(prior)or prior.is_relative_to(root):raise ValueError('recovery must preserve prior report')
    child=review_pipeline.prepare_review(path.parent/'native/package.json',[q['question_id']for q in original['new_questions']],root/'review')
    maximum=2*native['scope']['result_slots']
    if len(child['jobs'])>maximum:raise ValueError('recovery review exceeds original declared bound')
    p=dict(profile=PROFILE,created_at=now(),code=code_bindings(),assets=asset_bindings(),output_directory=str(root),
        source_checkpoint_path=str(path),source_checkpoint_sha256=digest(original),source_approval_sha256=digest(approval),
        native_package_sha256=digest(native),native_report_sha256=digest((Path(native['configuration']['run_directory'])/'report.json').read_bytes()),
        native_audit_sha256=digest(_audit_fields(audit)),review_package_sha256=digest(child),review_tasks=len(child['jobs']),
        maximum_review_tasks=maximum,accepted_profile=original['accepted_profile'],
        accepted_review_directory=str(root.parent/(root.name+'-accepted-review')),
        accepted_review_task_bound=original['accepted_review_task_bound'],shared_ceiling_usd=original['shared_ceiling_usd'],
        scope='Only originally approved saved-evidence reviews, audit, pooling and report; no native dispatch, searches, Pro calls or ordinary rejudging.')
    if original.get('live_profiles'):p['live_profiles']=original['live_profiles']
    write_private(root/'native-completion-audit.json',audit)
    write_private(root/'package.json',p)
    # Atomic no-overwrite link ensures only one output can acquire authority.
    write_private(marker,dict(source_checkpoint_sha256=digest(original),package_sha256=digest(p),output_directory=str(root)))
    return p


def verify(path,*,historical=False,seen=()):
    path=Path(path).resolve();root=path.parent;p=load(path)
    if(p['profile']!=PROFILE or p['output_directory']!=str(root)
            or(not historical and(p['code']!=code_bindings()or p['assets']!=asset_bindings()))
            or p['assets']!=asset_bindings()):raise ValueError('recovery code/assets/output changed')
    original,native,components,approval,report,audit=source(p['source_checkpoint_path'],seen=seen)
    marker=load(Path(p['source_checkpoint_path']).parent/'completion-recovery.json')
    if marker!=dict(source_checkpoint_sha256=digest(original),package_sha256=digest(p),output_directory=str(root)):
        raise ValueError('checkpoint continuation identity changed')
    run=Path(native['configuration']['run_directory'])
    if (p['source_checkpoint_sha256']!=digest(original)or p['source_approval_sha256']!=digest(approval)
            or p['native_package_sha256']!=digest(native)or p['native_report_sha256']!=digest((run/'report.json').read_bytes())
            or p['native_audit_sha256']!=digest(_audit_fields(audit))
            or _audit_fields(load(root/'native-completion-audit.json'))!=_audit_fields(audit)
            or p['accepted_profile']!=original['accepted_profile']or p['shared_ceiling_usd']!=original['shared_ceiling_usd']
            or p.get('live_profiles')!=original.get('live_profiles')
            or p['accepted_review_task_bound']!=original['accepted_review_task_bound']
            or p['accepted_review_directory']!=str(root.parent/(root.name+'-accepted-review'))
            or p['maximum_review_tasks']!=2*native['scope']['result_slots']):
        raise ValueError('recovery source/approval/scope changed')
    child=load(root/'review/package.json')
    if (digest(child)!=p['review_package_sha256']or len(child['jobs'])!=p['review_tasks']or p['review_tasks']>p['maximum_review_tasks']):
        raise ValueError('recovery declared review jobs changed')
    review_pipeline.verify(child,root/'review',historical=historical)
    with tempfile.TemporaryDirectory(prefix='openweb-recovery-derivation-')as tmp:
        expected=review_pipeline.prepare_review(Path(p['source_checkpoint_path']).parent/'native/package.json',
            [q['question_id']for q in original['new_questions']],tmp)
        omit={'created_at','run_directory','code'}if historical else{'created_at','run_directory'}
        normalize=lambda value:{k:v for k,v in value.items()if k not in omit}
        if(normalize(expected)!=normalize(child)or Path(child['run_directory']).resolve()!=(root/'review/run').resolve()):
            raise ValueError('recovery child does not derive from approved saved inputs')
    return p,original,native,components,approval,audit


def _receipt(p,approval):
    return dict(approved=True,package_sha256=digest(p),parent_checkpoint_sha256=p['source_checkpoint_sha256'],
        parent_approval_sha256=p['source_approval_sha256'],approved_by=approval['approved_by'],approved_at=approval['approved_at'],
        authority='Continuation of already-approved saved-evidence review/report scope only; no native dispatch.')


def _report(p,original,native,components,audit):
    result=checkpoint.combine(components,dict(p,profile=batch.PROFILE),native,batch.combined_costs(original,native))
    result.update(mode=PROFILE,package_sha256=digest(p))
    profiles=Counter(row['proof']['profile']for row in audit['records']if row['classification']=='recovered')
    result['report_limits']+=[
        'The original run stopped at native completion audit. This report completed through a separately versioned saved-evidence continuation.',
        'Native recovered completion profiles: '+str(dict(profiles))+'. Original errors remain preserved; recovered proofs supply no normal-health credit or judgment grade.']
    return result


def run(path):
    from .ledger import Ledger
    path=Path(path).resolve();root=path.parent;initial=load(path)
    with Ledger(root/'coordinator',dict(package_sha256=digest(initial),mode='live')).locked():
        p,original,native,components,approval,audit=verify(path)
        receipt=_receipt(p,approval)
        if(root/'approval.json').exists():
            if load(root/'approval.json')!=receipt:raise ValueError('recovery approval changed')
        else:write_private(root/'approval.json',receipt)
        if(root/'closure.json').exists():
            load_completed(root/'report/report.json')
            return checkpoint._finish_accepted(root,p,dict(approved_by=approval['approved_by'],approved_at=approval['approved_at']))
        child=load(root/'review/package.json');child_receipt=dict(receipt,package_sha256=digest(child),parent_recovery_sha256=digest(p))
        if(root/'review/approval.json').exists():
            if load(root/'review/approval.json')!=child_receipt:raise ValueError('recovery child approval changed')
        else:write_private(root/'review/approval.json',child_receipt)
        reviewed=review_pipeline.run_review(root/'review/package.json',approval=child_receipt)
        if reviewed['execution']['state']!='completed':return dict(stage='review',execution=reviewed['execution'])
        components.append(checkpoint.completed_review(root/'review/package.json'))
        report=_report(p,original,native,components,audit);write_reports=checkpoint.write_reports
        write_reports(private_directory(root/'report'),report)
        write_private(root/'closure.json',dict(package_sha256=digest(p),source_checkpoint_sha256=p['source_checkpoint_sha256'],
            native_report_sha256=p['native_report_sha256'],report_sha256=digest((root/'report/report.json').read_bytes()),
            execution='completed',costs=report['phase_costs'],new_search_calls=0,new_pro_calls=0),replace=True)
        return checkpoint._finish_accepted(root,p,dict(approved_by=approval['approved_by'],approved_at=approval['approved_at']))


def load_completed(path,*,seen=()):
    path=Path(path).resolve();root=path.parent.parent
    if path in seen:raise ValueError('cyclic recovery report')
    p,original,native,components,approval,audit=verify(root/'package.json',historical=True,seen=(*seen,path))
    receipt=load(root/'approval.json');closure=load(root/'closure.json');report=load(path)
    if(receipt!=_receipt(p,approval)or closure['package_sha256']!=digest(p)or closure['execution']!='completed'
            or closure['source_checkpoint_sha256']!=p['source_checkpoint_sha256']
            or closure['native_report_sha256']!=p['native_report_sha256']or closure['report_sha256']!=digest(path.read_bytes())):
        raise ValueError('recovery closure changed')
    components.append(checkpoint.completed_review(root/'review/package.json'))
    expected=_report(p,original,native,components,audit)
    for key,value in expected.items():
        if key=='generated_at':continue
        if encoded(batch.loads(value))!=encoded(report.get(key)):raise ValueError('recovery report does not replay: '+key)
    if closure['costs']!=expected['phase_costs']:raise ValueError('recovery costs changed')
    cases={cid:value for _,_,_,own in components for cid,value in own.items()}
    return report,cases,native
