"""Source-bound continuation after a cache lease expired during a rejected send."""
from copy import deepcopy
from pathlib import Path
import json

from .common import code_bindings,asset_bindings,digest,load,records,write_private,now

PROFILE='cache-expiry-continuation-v1'
KEY='cache_expiry_recovery'
MARGIN=120
# No prompt, decoder, scoring, adapter, transport or price changes are allowed.
CONTROLLERS={'cache_recovery.py','caching.py','runner.py','reuse.py','scheduling.py',
    'preparation.py','subscription_health.py','completion_audit.py','review_pipeline.py',
    'checkpoint.py','phase_review.py','settlement.py','batch.py','cli.py','profile_authority.py'}


def incident(source,root,events):
    """Derive the exact rejection and prior health epoch from retained native bytes."""
    from .scheduling import timestamp
    from .recovery import Recovery
    from .caching import cache_wire,payload
    starts={e['attempt_id']:e for e in events if e['event']=='started'}
    ends={e['attempt_id']:e for e in events if e['event']=='finished'}
    if starts.keys()!=ends.keys():raise ValueError('cache recovery requires drained attempts')
    resources={};archived=[];rejections=[];held=[]
    recovery=Recovery(source['configuration']['execution_policy']['scheduling']['recovery'])
    account=source['account_bindings']['pro']
    for e in events:
        aid=e['attempt_id'];request=load(root/'attempts'/aid/'request.json')
        if request.get('role')!='pro':continue
        recovery.capacity(account,'pro',timestamp(e['recorded_at']))
        if e['event']!='finished':continue
        response=load(root/e['result_path']);archived.append(aid)
        recovery.observe(aid,account,'pro',response,timestamp(e['recorded_at']),response.get('recovery_validation_state'))
        if request['kind']=='cache'and e['task_id'].startswith('cache-create-')and response.get('http_status')in {200,201}:
            value=json.loads(response['raw_text']);resources[value['name']]=(aid,value,request)
        if request['kind']!='model':continue
        if response.get('dispatch_state')=='uncertain':held.append(e['task_id'])
        if response.get('http_status')not in {401,402,403}:continue
        if (response.get('http_status')!=403 or response.get('complete')is not True
                or response.get('dispatch_state')!='completed' or response.get('transport_success')is not False):
            raise ValueError('cache recovery cannot retire other auth/billing failures')
        error=json.loads(response.get('raw_text','{}')).get('error',{})
        if error.get('code')!=403 or error.get('status')!='PERMISSION_DENIED' or error.get('message')!='CachedContent not found (or permission denied)':
            raise ValueError('cache recovery requires a typed completed cache rejection')
        wire=load(root/'attempts'/aid/'effective-wire.json');name=wire['body'].get('cachedContent')
        if name not in resources:raise ValueError('cache rejection lacks its native lease')
        create_id,value,create_request=resources[name];expiry=timestamp(value['expireTime'])
        if not timestamp(starts[aid]['recorded_at'])<expiry<=timestamp(e['recorded_at']):
            raise ValueError('cache rejection did not cross verified native expiry')
        group=payload(request['wire']);resource=dict(group_id=digest(group),name=name)
        if (wire!=cache_wire(request['wire'],resource,group)or create_request['wire']['body'].get('model')!=group['model']
                or create_request['wire']['body'].get('systemInstruction')!=group['systemInstruction']):
            raise ValueError('cache rejection changed effective model input')
        rejections.append(dict(attempt_id=aid,create_attempt_id=create_id,request_sha256=starts[aid]['request_sha256'],
            result_sha256=e['result_sha256'],effective_wire_sha256=digest(wire),expires_at=expiry))
    if not rejections:raise ValueError('no proved expired-cache rejection')
    scopes=recovery.describe()
    if not any(s['account']==account and s['configuration']is None and s['mode']=='stopped'for s in scopes):
        raise ValueError('cache recovery requires the retained stopped account')
    return dict(rejections=rejections,archived_attempt_ids=sorted(archived),archived_recovery_sha256=digest(scopes),
        terminal_uncertain_tasks=sorted(held))


def proof_for(source,reference):
    from .reuse import read_snapshot
    manifest=read_snapshot(reference);root=Path(reference['manifest_path']).parent
    if manifest['source_package_sha256']!=digest(source):raise ValueError('cache snapshot source differs')
    return dict(profile=PROFILE,source_snapshot=reference,source_package_sha256=digest(source),
        source_code_sha256=digest(source['code']),target_code_sha256=digest(code_bindings()),
        **incident(source,root,list(records(root/'events.jsonl'))))


def compatible(source,target):
    proof=target['configuration'].get(KEY)
    old=source['configuration'].get(KEY)
    if old and proof==old:return False
    if proof is None:
        if old:raise ValueError('cache continuation dropped its recovery provenance')
        return False
    if old:raise ValueError('another cache health epoch requires a separately reviewed migration')
    if (proof!=proof_for(source,target['configuration'].get('reuse_snapshot'))
            or proof['target_code_sha256']!=digest(target['code'])):
        raise ValueError('cache recovery proof differs from exact source evidence')
    changed={k for k in source['code'].keys()|target['code'].keys()if source['code'].get(k)!=target['code'].get(k)}
    if not changed<=CONTROLLERS:raise ValueError('cache recovery changed frozen method code')
    for key in ('mode','assets','questions','question_bindings','prompt_profiles','primary','providers','search_requests','schema_plan'):
        if source[key]!=target[key]:raise ValueError('cache recovery changed frozen work: '+key)
    a,b=source['configuration'],target['configuration'];mutable={'run_directory','reuse_snapshot',KEY,'system_cache','live_profile_authority'}
    if {k:v for k,v in a.items()if k not in mutable}!={k:v for k,v in b.items()if k not in mutable}:
        raise ValueError('cache recovery changed settings or spending ceiling')
    expected=deepcopy(a['system_cache']);expected['renewal']=renewal(source,proof['source_snapshot'])
    if b.get('system_cache')!=expected:raise ValueError('cache recovery changed more than the declared lease policy')
    from .profile_authority import profiles,live_bindings
    old_authority=a.get('live_profile_authority');new_authority=b.get('live_profile_authority')
    if bool(old_authority)!=bool(new_authority):raise ValueError('cache recovery changed profile authority presence')
    if old_authority:
        if (profiles(old_authority)!=profiles(new_authority)
                or new_authority.get('source_report_sha256')!=old_authority.get('source_report_sha256')
                or new_authority['profile_bindings']!=live_bindings(b['reviewers'],selection_profile=profiles(old_authority)['selection_profile'])):
            raise ValueError('cache recovery changed accepted grading profiles')
    return True


def renewal(source,reference):
    return dict(profile='expiry-leases-v2',renew_before_seconds=MARGIN,
        epoch=digest(dict(profile=PROFILE,source=digest(source),snapshot=reference)))


def archived_attempts(package):
    proof=package['configuration'].get(KEY)
    if not proof:return set()
    from .reuse import read_snapshot
    ref=proof['source_snapshot'];read_snapshot(ref)
    source=load(Path(ref['manifest_path']).parent/'source-package.json')
    if not compatible(source,package):
        # Later exact continuations retain the already accepted epoch, never
        # retire any attempt outside this explicit original source set.
        if source['configuration'].get(KEY)!=proof:raise ValueError('cache health epoch differs')
    return set(proof['archived_attempt_ids'])


def terminal_uncertain_tasks(package):
    proof=package['configuration'].get(KEY)
    return set(proof['terminal_uncertain_tasks'])if proof else set()


def native_plan(source,root,reference,authority):
    from .preparation import prepare
    cfg=deepcopy(source['configuration']);cfg.update(run_directory=str(root/'native/run'),reuse_snapshot=reference,
        questions=source['questions'],provider_ids=[p['configuration_id']for p in source['providers']])
    cfg['system_cache']['renewal']=renewal(source,reference)
    cfg[KEY]=proof_for(source,reference)
    if authority is not None:cfg['live_profile_authority']=authority
    return cfg,prepare(cfg,mode=source['mode'])


def source_checkpoint(path,evidence):
    """Authenticate old authority as history, without allowing its code to run."""
    from . import batch
    path=Path(path).resolve();p=load(path);native=load(path.parent/'native/package.json');approval=load(path.parent/'approval.json')
    prior,_,template=evidence
    if (p.get('profile')!=batch.PROFILE or p.get('native_recovery')or p['output_directory']!=str(path.parent)
            or approval.get('decision')!='approved'or approval.get('package_sha256')!=digest(p)
            or not approval.get('approved_by')or not approval.get('approved_at')
            or p['assets']!=asset_bindings()or p['native_package_sha256']!=digest(native)
            or p['code']!=native['code']or p['assets']!=native['assets']
            or p['parent_report_sha256']!=digest(Path(p['parent_report_path']).read_bytes())
            or p['old_questions']!=prior['questions']or p['old_costs']!=prior['phase_costs']):
        raise ValueError('original approved checkpoint binding differs')
    order,admission=batch.frozen_order(p['selection_path'])
    if any(p[k]!=v for k,v in admission.items())or p['new_questions']!=batch.next_questions(prior,order,len(p['new_questions'])):
        raise ValueError('original checkpoint admission differs')
    _,expected=batch.native_plan(template,p['new_questions'],path.parent,p['shared_ceiling_usd'],p['prior_committed_and_held_usd'],authority=p['accepted_profile'])
    expected.update(code=native['code'],assets=native['assets'])
    if native!=expected:raise ValueError('original native package does not derive from approved settings')
    authority=p['accepted_profile'];apath=Path(authority['package_path']);ap=load(apath);receipt=load(apath.parent/'approval.json')
    if (digest(ap)!=authority['package_sha256']or digest(receipt)!=authority['approval_sha256']
            or receipt.get('approved')is not True or receipt.get('accept_live_profiles')is not True
            or receipt.get('package_sha256')!=digest(ap)or ap['profile_bindings']!=authority['profile_bindings']
            or authority['source_report_sha256']!=p['parent_report_sha256']):
        raise ValueError('original accepted authority receipt differs')
    return p,native


def prepare(checkpoint_path,reference,accepted_profile,directory,*,build_identity=None):
    from . import batch,settlement,profile_authority
    source_path=Path(checkpoint_path).resolve();root=Path(directory).resolve()
    if root.is_relative_to(source_path.parent)or source_path.parent.is_relative_to(root):raise ValueError('recovery must preserve original checkpoint')
    original=load(source_path);evidence=settlement.load_parent(original['parent_report_path'])
    original,source=source_checkpoint(source_path,evidence)
    authority=profile_authority.read(accepted_profile,source['configuration']['reviewers'])
    cfg,native=native_plan(source,root,reference,authority)
    p=deepcopy(original);p.update(created_at=now(),code=code_bindings(),assets=asset_bindings(),build_identity=build_identity,
        output_directory=str(root),native_package_sha256=digest(native),accepted_profile=authority,
        accepted_review_directory=str(root.parent/(root.name+'-accepted-review')),
        live_profiles=profile_authority.profiles(authority),native_recovery=dict(profile=PROFILE,
            source_checkpoint_path=str(source_path),source_checkpoint_sha256=digest(original),source_snapshot=reference))
    write_private(root/'native/config.json',cfg);write_private(root/'native/package.json',native);write_private(root/'checkpoint.json',p)
    write_private(root/'approval-template.json',dict(decision='NOT_APPROVED',package_sha256=digest(p),approved_by='',approved_at='',shared_ceiling_usd=p['shared_ceiling_usd'],scope=p['stop']))
    write_private(root/'APPROVAL-REVIEW.md',(batch.approval_review(p,native)+'\nCache-expiry recovery: exact-task reuse; pre-expiry renewal120s; saved403terminal; unsafe uncertain sends held; provider interventions excluded; no known successful call repeated.\n').encode())
    return p


def expected_native(p,root,evidence,authority):
    proof=p['native_recovery']
    if set(proof)!={'profile','source_checkpoint_path','source_checkpoint_sha256','source_snapshot'}:
        raise ValueError('incomplete cache checkpoint recovery binding')
    path=Path(proof['source_checkpoint_path'])
    source_p,source=source_checkpoint(path,evidence)
    if (proof.get('profile')!=PROFILE or proof['source_checkpoint_sha256']!=digest(source_p)
            or p['parent_report_path']!=source_p['parent_report_path']
            or p['shared_ceiling_usd']!=source_p['shared_ceiling_usd']
            or p['old_questions']!=source_p['old_questions']or p['new_questions']!=source_p['new_questions']):
        raise ValueError('checkpoint recovery changed source scope or ceiling')
    _,native=native_plan(source,root,proof['source_snapshot'],authority)
    return native
