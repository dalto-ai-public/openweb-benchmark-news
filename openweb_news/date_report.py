"""Approved, replayable date-profile report rebuild with bounded saved-scope thirds."""
from collections import Counter
from copy import deepcopy
from pathlib import Path
from itertools import combinations
import tempfile
import json

from . import date_replay,judging,publication,review_pipeline,representation
from .common import asset_bindings,code_bindings,digest,load,now,private_directory,write_private
from .date_reading import LATEST_PROFILE
from .ledger import Ledger
from .reporting import write_reports
from .review import needs_third
from ._method import scoring

PROFILE='date-profile-report-v1'
RECOLLECTION_SOURCE='accepted-repair-recollection-source-v1'


def recollection_reference(report, disposition, review_packages, publication_packages, native_packages):
    """Bind the already accepted repair as evidence, never as new-call authority."""
    def ref(path):
        path=Path(path).resolve()
        return dict(path=str(path),sha256=digest(path.read_bytes()))
    return dict(profile=RECOLLECTION_SOURCE,report=ref(report),disposition=ref(disposition),
        receipt=ref(Path(disposition).parent/'approval.json'),
        review_packages=[ref(p)for p in review_packages],publication_packages=[ref(p)for p in publication_packages],
        native_packages=[ref(p)for p in native_packages])


def recollection_source(reference, authority):
    """Authenticate and replay a repaired source without executing its old method.

    This is a narrowly selected source for replacing the newly filtered cells.
    It is not a general historical method acceptance or a dispatch permission.
    """
    from . import profile_authority
    if profile_authority.read(authority['package_path'],authority['profile_bindings']['reviewers'])!=authority:
        raise ValueError('recollection requires the exact accepted target method')
    return _recollection_evidence(reference,authority)


def _recollection_evidence(reference, authority):
    from .request_profiles import MIXED_START_IDS,MIXED_START_PROFILE
    if reference.get('profile')!=RECOLLECTION_SOURCE:raise ValueError('unknown recollection source')
    def bound(ref):
        path=Path(ref['path'])
        if path.is_symlink()or digest(path.read_bytes())!=ref['sha256']:
            raise ValueError('recollection source bytes changed')
        return load(path)
    view=bound(reference['report']);d=bound(reference['disposition']);receipt=bound(reference['receipt'])
    if (d.get('profile')!='native-literal-repair-owner-disposition-v1'
            or receipt.get('approved')is not True or receipt.get('package_sha256')!=digest(d)
            or receipt.get('accept_corrected_method')is not True or receipt.get('accept_saved_response_repair')is not True
            or not receipt.get('approved_by')or not receipt.get('approved_at')):
        raise ValueError('recollection source repair was not accepted')
    method=bound(d['method']);candidate=bound(d['candidate_report']);source=bound(d['source_report'])
    method_receipt=load(Path(d['method']['path']).parent/'approval.json')
    if (method_receipt.get('approved')is not True or method_receipt.get('accept_method')is not True
            or method_receipt.get('package_sha256')!=digest(method)
            or not method_receipt.get('approved_by')or not method_receipt.get('approved_at')):
        raise ValueError('recollection source method receipt differs')
    old=deepcopy(method['profile_bindings']);new=deepcopy(authority['profile_bindings'])
    expected={cid:MIXED_START_PROFILE for cid in old['provider_ids']if cid in MIXED_START_IDS}
    if not expected or new.get('method_options',{}).get('provider_date_profiles')!=expected:
        raise ValueError('recollection must select exactly the reviewed start-control providers')
    for binding in (old,new):
        binding.pop('semantic_code',None)
        binding.get('method_options',{}).pop('provider_date_profiles',None)
    if old!=new:raise ValueError('recollection changed another selected method choice')
    for key in ('questions','collection','phase_costs'):
        if view[key]!=source[key]:raise ValueError('recollection source scope or cost differs')
    if view['outcomes']!=candidate['outcomes']:raise ValueError('recollection view differs from owner-accepted outcomes')
    paths={key:[r['path']for r in reference[key]]for key in ('review_packages','publication_packages','native_packages')}
    for key in paths:
        for ref in reference[key]:bound(ref)
    if not paths['native_packages']:raise ValueError('recollection lacks original native evidence')
    from .source_audit import audit
    audit(d['source_report']['path'],paths['review_packages'],paths['native_packages'])
    with tempfile.TemporaryDirectory()as temp:
        diagnostic=Path(temp)/'replay'
        summary=date_replay.replay(d['source_report']['path'],paths['review_packages'],paths['publication_packages'],diagnostic,
            expected_source_sha256=d['source_report']['sha256'],date_profile=new['date_profile'],
            baseline_date_profile=source['active_review_profile']['profile_bindings']['date_profile'],text_profile=new['text_profile'])
        effective=load(diagnostic/'effective-outcomes.json')
        if effective!=candidate['outcomes']or summary['unresolved_review_requirements']:
            raise ValueError('accepted source outcomes do not reproduce under target reading')
        native=load(paths['native_packages'][-1]);cfg=native['configuration']
        p=dict(source_report_path=d['source_report']['path'],source_report_sha256=d['source_report']['sha256'],
            created_at=receipt['approved_at'],review_packages=paths['review_packages'],jobs=[],
            accepted_profile=authority,repair_disposition=reference['disposition'],source_costs=source['phase_costs'],
            reviewers=cfg['reviewers'],coverage_target=cfg['coverage_target'],tie_order=cfg['tie_order'],
            date_profile=new['date_profile'],text_profile=new['text_profile'])
        rebuilt=_build(p,Path(reference['receipt']['path']).parent,diagnostic=diagnostic)
    # Preserve the accepted outcomes byte-for-byte. Rebinding retained terminal
    # reviews changes only their provenance/identity, not a vote or result grade.
    if rebuilt['outcomes']!=view['outcomes']:raise ValueError('recollection source outcome changed')
    cases={};packets={o['case_id']:o['packet_sha256']for o in rebuilt['outcomes']}
    for path in map(Path,paths['review_packages']):
        for ref in load(path)['cases']:
            case=load(path.parent/ref['path']);cid=ref['case_id']
            if packets.get(cid)==case['compiled']['packet_sha256']:
                cases[cid]=case
    if set(cases)!={o['case_id']for o in rebuilt['outcomes']}:raise ValueError('recollection source cases missing')
    for case in cases.values():
        from .profile_authority import verify_case_method
        verify_case_method(authority,case['compiled'],case['provider'])
    _verify_retained_reviews(rebuilt,cases,authority)
    return rebuilt,cases,native


def _verify_retained_reviews(report, cases, authority):
    """Fail before collection if unchanged cells would need new review calls."""
    from .settlement import selected
    replaced=set(authority['method_options'].get('provider_date_profiles',{}))
    retained=deepcopy(report)
    retained['outcomes']=[o for o in report['outcomes']if o['configuration_id']not in replaced]
    ids={o['case_id']for o in retained['outcomes']}
    retained['publication_review']['records']=[r for r in report['publication_review']['records']if r['case_id']in ids]
    if selected(retained,{cid:c for cid,c in cases.items()if cid in ids},
            authority['profile_bindings']['reviewers'],authority=authority):
        raise ValueError('unchanged-provider evidence would require new review calls')


def _diagnostic_args(p,output):
    args=dict(source_report=p['source_report_path'],review_packages=p['review_packages'],
        publication_packages=p['publication_packages'],directory=output,
        expected_source_sha256=p['source_report_sha256'],date_profile=p['date_profile'])
    if p.get('text_profile'):args['text_profile']=p['text_profile']
    if p.get('baseline_date_profile'):args['baseline_date_profile']=p['baseline_date_profile']
    if p.get('reused_third_package_path'):args['third_package']=p['reused_third_package_path']
    return args


def prepare(source_report,review_packages,publication_packages,output,*,expected_source_sha256,maximum_new_third=1,mode="live",native_packages=(),text_profile=None,reused_third_package=None,comparison_report=None,expected_comparison_sha256=None,date_profile=LATEST_PROFILE,baseline_date_profile=None,accepted_profile=None,repair_disposition=None):
    root=Path(output).resolve();source=Path(source_report).resolve()
    if mode not in {'live','fixture'}:raise ValueError('unknown rebuild mode')
    if mode=='live'and not native_packages:raise ValueError('bound native template required')
    if root.exists():raise ValueError('use a fresh rebuild directory')
    if type(maximum_new_third)is not int or maximum_new_third<0:raise ValueError('explicit third task limit required')
    initial=dict(source_report_path=str(source),source_report_sha256=expected_source_sha256,
        review_packages=[str(Path(x).resolve())for x in review_packages],publication_packages=[str(Path(x).resolve())for x in publication_packages],date_profile=date_profile)
    if baseline_date_profile is not None:initial['baseline_date_profile']=baseline_date_profile
    if text_profile is not None:
        if text_profile not in representation.PROFILES:raise ValueError('unknown text profile')
        initial['text_profile']=text_profile
    if reused_third_package:
        third=Path(reused_third_package).resolve()
        if root.is_relative_to(third.parent)or third.is_relative_to(root):raise ValueError('rebuild must preserve reused evidence root')
        initial.update(reused_third_package_path=str(third),reused_third_package_sha256=digest(third.read_bytes()))
    if comparison_report:
        comparison=Path(comparison_report).resolve()
        if root.is_relative_to(comparison.parent.parent)or comparison.is_relative_to(root):raise ValueError('rebuild must preserve comparison root')
        if digest(comparison.read_bytes())!=expected_comparison_sha256:raise ValueError('comparison report hash differs')
        initial.update(comparison_report_path=str(comparison),comparison_report_sha256=expected_comparison_sha256)
    if mode=='live':
        from .source_audit import audit
        source_audit=audit(source,review_packages,native_packages)
    else:source_audit=dict(fixture=True)
    summary=date_replay.replay(**_diagnostic_args(initial,root/'initial'))
    write_private(root/'source-audit.json',source_audit)
    report=load(source);prior={o['case_id']:o for o in report['outcomes']};effective=load(root/'initial/effective-outcomes.json')
    if comparison_report:_compare(initial,effective)
    pending={o['case_id']for o in effective if prior[o['case_id']]['grade']is not None and o['grade']is None
             and needs_third(o['positions']['pro'],o['positions']['codex'])and 'claude'not in o['positions']}
    if len(pending)>maximum_new_third:raise ValueError('required new third judgments exceed declared scope')
    if pending and reused_third_package:raise ValueError('reused thirds cannot be combined with new third tasks')
    jobs=[];refs=[];template=None;native_binding={}
    if native_packages:
        native_path=Path(native_packages[-1]).resolve();native_binding=dict(native_package_path=str(native_path),native_package_sha256=digest(native_path.read_bytes()),native_packages=[str(Path(x).resolve())for x in native_packages])
    for path in map(Path,review_packages):
        package=load(path);template=template or package
        for ref in package['cases']:
            if ref['case_id']not in pending:continue
            case=load(path.parent/ref['path']);cid=ref['case_id']
            if case['compiled']['packet_sha256']!=prior[cid]['packet_sha256']:continue
            if any(j['case_id']==cid for j in jobs):raise ValueError('third case duplicated')
            target='inputs/'+cid+'.json';write_private(root/target,case)
            refs.append(dict(case_id=cid,path=target,sha256=digest((root/target).read_bytes())))
            reviewer=package['reviewers']['claude']
            jobs.append(dict(task_id='third-claude-'+cid,case_id=cid,stage='third',kind='model',role='claude',
                backend=reviewer['backend'],backend_config=deepcopy(reviewer),blind_packet=judging.blind_packet(case['compiled']),
                reservation_usd='0',reservation_basis='approved subscription third; no API charge'))
    if len(jobs)!=len(pending)or not template:raise ValueError('third input scope incomplete')
    p=dict(initial,**native_binding,profile=PROFILE,created_at=now(),mode=mode,code=code_bindings(),assets=asset_bindings(),
        output_directory=str(root),run_directory=str(root/'run'),maximum_new_third=maximum_new_third,jobs=jobs,cases=refs,
        initial_summary_sha256=digest((root/'initial/summary.json').read_bytes()),initial_effective_sha256=digest((root/'initial/effective-outcomes.json').read_bytes()),
        source_audit_sha256=digest((root/'source-audit.json').read_bytes()),source_costs=deepcopy(report['phase_costs']),coverage_target=template['coverage_target'],tie_order=template['tie_order'],
        **{k:deepcopy(template[k])for k in ('reviewers','scheduling','recovery','retry_policy','max_attempts_per_task','automatic_recovery')if k in template},
        scope='Adopt the approved saved date/publication profile over this report, plus only declared blind Claude third judgments. No searches, Pro, new questions or known numeric grade fallback.')
    if accepted_profile is not None or repair_disposition is not None:
        from . import profile_authority
        if accepted_profile is None or repair_disposition is None or maximum_new_third != 0 or jobs:
            raise ValueError('checker-only repair requires a target method, owner disposition and zero new jobs')
        authority=profile_authority.read(accepted_profile,p['reviewers'])
        profiles=profile_authority.profiles(authority)
        if (profiles.get('date_profile')!=date_profile or profiles.get('text_profile')!=(text_profile or template['text_profile'])
                or profiles.get('checker_profile')!=publication.READING_PROFILE
                or summary.get('unresolved_review_requirements')):
            raise ValueError('repair profiles or missing review requirements differ')
        disposition=Path(repair_disposition).resolve()
        receipt=disposition.parent/'approval.json'
        method=Path(accepted_profile).resolve()
        p.update(accepted_profile=authority,repair_disposition=dict(path=str(disposition),sha256=digest(disposition.read_bytes())),
                 repair_receipt=dict(path=str(receipt),sha256=digest(receipt.read_bytes())),
                 target_method=dict(path=str(method),sha256=digest(method.read_bytes())),
                 target_method_receipt=dict(path=str(method.parent/'approval.json'),sha256=digest((method.parent/'approval.json').read_bytes())))
        _verify_repair(p)
    write_private(root/'package.json',p)
    return p


def _verify_repair(p, *, historical=False):
    """Verify the accepted zero-call transition; never grants dispatch authority."""
    if 'accepted_profile' not in p:return
    from . import profile_authority
    def bound(key):
        ref=p[key];path=Path(ref['path'])
        if path.is_symlink() or digest(path.read_bytes())!=ref['sha256']:
            raise ValueError('repair source/target receipt changed')
        return load(path)
    d=bound('repair_disposition');receipt=bound('repair_receipt')
    method=bound('target_method');method_receipt=bound('target_method_receipt')
    authority=p['accepted_profile']
    if (d.get('profile')!='native-literal-repair-owner-disposition-v1'
            or receipt.get('approved')is not True or receipt.get('accept_corrected_method')is not True
            or receipt.get('accept_saved_response_repair')is not True or receipt.get('package_sha256')!=digest(d)
            or not receipt.get('approved_by') or not receipt.get('approved_at')
            or d['source_report']!=dict(path=p['source_report_path'],sha256=p['source_report_sha256'])
            or p['jobs'] or p['maximum_new_third']!=0
            or method_receipt.get('approved')is not True or method_receipt.get('accept_method')is not True
            or method_receipt.get('package_sha256')!=digest(method)
            or not method_receipt.get('approved_by') or not method_receipt.get('approved_at')
            or authority['package_sha256']!=digest(method) or authority['approval_sha256']!=digest(method_receipt)
            or authority['profile_bindings']!=method['profile_bindings']
            or authority['method_options']!=method['method_options']):
        raise ValueError('checker-only repair acceptance differs')
    selected_path=Path(d['method']['path']);selected=load(selected_path)
    selected_receipt=load(selected_path.parent/'approval.json')
    if (digest(selected_path.read_bytes())!=d['method']['sha256']
            or selected_receipt.get('approved')is not True or selected_receipt.get('accept_method')is not True
            or selected_receipt.get('package_sha256')!=digest(selected)):
        raise ValueError('owner-selected corrected method changed')
    # Integration code is separately frozen/accepted. It cannot change chosen
    # providers, settings, field admissions, prompts, assets or review profiles.
    old={k:v for k,v in selected['profile_bindings'].items()if k!='semantic_code'}
    new={k:v for k,v in method['profile_bindings'].items()if k!='semantic_code'}
    if old!=new:raise ValueError('repair changed owner-selected method choices')
    source=load(p['source_report_path']);effective=load(Path(p['output_directory'])/'initial/effective-outcomes.json')
    before={r['case_id']:r for r in source['outcomes']};after={r['case_id']:r for r in effective}
    declared={r['case_id']:r for r in d['repaired_cases']}
    if len(declared)!=len(d['repaired_cases']) or before.keys()!=after.keys():
        raise ValueError('repair occurrence scope differs')
    changed={cid for cid in before if before[cid]['grade']!=after[cid]['grade']}
    if changed!=declared.keys():raise ValueError('repair changed undeclared grades')
    records={r['case_id']:r for r in source['publication_review']['records']}
    compiled={}
    for file in map(Path,p['review_packages']):
        for ref in load(file)['cases']:
            if ref['case_id']in declared:
                path=file.parent/ref['path']
                if path.is_symlink() or digest(path.read_bytes())!=ref['sha256']:
                    raise ValueError('repair compiled source binding differs')
                compiled[ref['case_id']]=load(path)['compiled']
    if compiled.keys()!=declared.keys():raise ValueError('repair compiled source scope differs')
    for cid,row in declared.items():
        record=records[cid]
        own=compiled[cid]
        if (own['input_sha256']!=row['compiled_input_sha256']
                or digest(publication.packet_for(own))!=row['publication_packet_sha256']
                or before[cid]['grade']!=row['before'] or after[cid]['grade']!=row['after']
                or before[cid]['packet_sha256']!=row['packet_sha256']
                or after[cid]['packet_sha256']!=row['packet_sha256']
                or set(record['provenance'])!={'codex','claude'}
                or {role:proof['response_sha256']for role,proof in record['provenance'].items()}!=row['response_sha256']):
            raise ValueError('repair occurrence/input/response binding differs')
    if not historical and profile_authority.read(p['target_method']['path'],p['reviewers'])!=authority:
        raise ValueError('repair target method authority differs')
    return d


def _compare(p,outcomes):
    """An approved adoption preserves every already-known comparison outcome."""
    path=Path(p['comparison_report_path'])
    if digest(path.read_bytes())!=p['comparison_report_sha256']:raise ValueError('comparison report changed')
    source=load(p['source_report_path']);comparison=load(path);rows={o['case_id']:o for o in outcomes}
    if any(source[k]!=comparison[k]for k in ('questions','collection','phase_costs')):raise ValueError('comparison scope or costs differ')
    if set(rows)!={o['case_id']for o in comparison['outcomes']}:raise ValueError('comparison outcome scope differs')
    for old in comparison['outcomes']:
        new=rows[old['case_id']]
        if new['packet_sha256']!=old['packet_sha256']or old['grade']is not None and old['grade']!=new['grade']:raise ValueError('adoption changes a known comparison grade')


def verify(path,*,historical=False):
    path=Path(path).resolve();root=path.parent;p=load(path)
    if(p['profile']!=PROFILE or p['mode']not in {'live','fixture'}or (not historical and p['code']!=code_bindings())
       or not all(asset_bindings().get(k)==v for k,v in p['assets'].items())or(not historical and p['assets']!=asset_bindings())
       or Path(p['output_directory'])!=root or Path(p['run_directory'])!=root/'run'
       or digest(Path(p['source_report_path']).read_bytes())!=p['source_report_sha256']
       or digest((root/'initial/summary.json').read_bytes())!=p['initial_summary_sha256']
       or digest((root/'initial/effective-outcomes.json').read_bytes())!=p['initial_effective_sha256']
       or len(p['jobs'])>p['maximum_new_third']):raise ValueError('rebuild binding changed')
    if digest((root/'source-audit.json').read_bytes())!=p['source_audit_sha256']:raise ValueError('source audit changed')
    if p['mode']=='live' and digest(Path(p['native_package_path']).read_bytes())!=p['native_package_sha256']:raise ValueError('native template changed')
    if p.get('reused_third_package_path'):
        if p['jobs']or digest(Path(p['reused_third_package_path']).read_bytes())!=p['reused_third_package_sha256']:raise ValueError('reused third binding changed')
    if p.get('comparison_report_path'):_compare(p,load(root/'initial/effective-outcomes.json'))
    for ref,job in zip(p['cases'],p['jobs'],strict=True):
        file=root/ref['path']
        if file.is_symlink()or not file.resolve().is_relative_to(root)or digest(file.read_bytes())!=ref['sha256']:raise ValueError('third input changed')
        case=load(file)
        if(job['case_id']!=ref['case_id']or job['task_id']!='third-claude-'+ref['case_id']or job['role']!='claude'or job['stage']!='third'
           or job['backend']!='claude-subscription'or job['backend_config']!=p['reviewers']['claude']
           or job['blind_packet']!=judging.blind_packet(case['compiled'])):raise ValueError('declared third differs')
    _verify_repair(p,historical=historical)
    return p


def _build(p,root,diagnostic=None):
    final=Path(diagnostic)if diagnostic is not None else root/'final'
    source=load(p['source_report_path']);summary=load(final/'summary.json');outcomes=load(final/'effective-outcomes.json')
    report=deepcopy(source);by_id={o['case_id']:o for o in outcomes}
    if set(by_id)!={o['case_id']for o in source['outcomes']}or len(by_id)!=len(outcomes):raise ValueError('rebuilt outcome scope differs')
    report.update(mode=PROFILE,package_sha256=digest(p),generated_at=p['created_at'],outcomes=[by_id[o['case_id']]for o in source['outcomes']])
    cases={}
    for path in map(Path,p['review_packages']):
        package=load(path)
        for ref in package['cases']:
            cid=ref['case_id']
            if cid not in by_id:continue
            case=load(path.parent/ref['path'])
            if case['compiled']['packet_sha256']==by_id[cid]['packet_sha256']:cases[cid]=case
    if set(cases)!=set(by_id):raise ValueError('scoring inputs incomplete')
    configs=list(source['scores']['accounting']);cells={}
    for row in source['collection']:
        cid,qid=row['configuration_id'],row['question_id']
        own=sorted((o for o in outcomes if o['configuration_id']==cid and o['question_id']==qid),key=lambda o:o['rank'])
        cells[cid,qid]=dict(status=row['status'],ranked=[dict(rank=o['rank'],evidence_id=o['case_id'],url=cases[o['case_id']]['compiled']['packet']['returned_url'])for o in own])
    labels={o['case_id']:o['grade']for o in outcomes};reasons={o['case_id']:o['reason']for o in outcomes};findings={}
    for o in outcomes:
        selected=o['positions'].get(o['selected_roles'][0],{})if o.get('selected_roles')else {}
        judgment=selected.get('conformance_judgment')or selected.get('judgment')or {}
        findings[o['case_id']]=[dict(report_id=None,event_id=None,facet_ids=c['supported_facet_ids'])for c in judgment.get('candidates',[])if c['answer_supported']]if o['grade']==2 else []
    report['scores']=scoring.score(source['questions'],configs,cells,labels,reasons,p['coverage_target'],p['tie_order'],findings=findings)
    pairs=[]
    for k in (3,5,10):
        for name in ('answer','useful'):
            values={(r['question_id'],r['configuration_id']):(r.get('metrics')or r['delivered_metrics'])[name]for r in report['scores']['question_rows']if r['k']==k}
            pairs += [dict(metric=f'{name}@{k}',**scoring.paired(source['questions'],a,b,report['scores']['availability']['answered_question_ids'],values))for a,b in combinations(configs,2)]
    report['resolved_pairwise']=pairs;report['outcome_counts']=dict(Counter('unknown'if o['grade']is None else str(o['grade'])for o in outcomes))
    publication_rows=[x for x in load(final/'publication-audits.json')if x['variant']=='combined']
    prior_publication={r['case_id']:r for r in source['publication_review']['records']}
    publication_records=[]
    for row in publication_rows:
        record=deepcopy(prior_publication[row['case_id']])
        record.update(accepted=row['accepted'],grade=row['grade'],terminal=row['terminal'],audits=row['audits'],checker_profile=publication.READING_PROFILE,date_profile=p['date_profile'],date_fills=row['fills'])
        if p.get('accepted_profile'):
            from .settlement import identity
            record['source_terminal_key']=record['terminal_key']
            record['terminal_key']=identity(cases[row['case_id']]['compiled'],p['reviewers'],authority=p['accepted_profile'])
            record['checker_only_reuse']=dict(source_record_sha256=digest(prior_publication[row['case_id']]),
                disposition_sha256=p['repair_disposition']['sha256'],
                unchanged_input_sha256=publication.packet_for(cases[row['case_id']]['compiled'])['input_sha256'],
                response_hashes={role:proof['response_sha256']for role,proof in record['provenance'].items()})
        publication_records.append(record)
    report['publication_review']=dict(profile=publication.READING_PROFILE,records=publication_records,subscription_tasks=0,reused_subscription_tasks=2*len(publication_records))
    if 'review_pipeline'in report:report['review_pipeline']['final_unknowns']=sum(o['grade']is None for o in outcomes)
    report['source_checker_acceptance']=report.pop('checker_acceptance',None)
    report['active_review_profile']=deepcopy(p['accepted_profile'])if p.get('accepted_profile')else dict(checker_profile=publication.READING_PROFILE,date_profile=p['date_profile'])
    report['execution']=dict(state='completed',new_third_tasks=len(p['jobs']),new_search_calls=0,new_pro_calls=0)
    recorded_summary=root/'final/summary.json'
    report['date_profile_rebuild']=dict(profile=PROFILE,source_report_path=p['source_report_path'],source_report_sha256=p['source_report_sha256'],diagnostic_summary_sha256=digest((recorded_summary if recorded_summary.exists()else final/'summary.json').read_bytes()),date_profile=p['date_profile'],checker_profile=publication.READING_PROFILE,new_third_tasks=len(p['jobs']),approval_sha256=digest(load(root/'approval.json')),scope='This derived report only; future question dispatch remains separately approved.')
    if p.get('text_profile'):
        if not p.get('accepted_profile'):report['active_review_profile']['text_profile']=p['text_profile']
        report['date_profile_rebuild']['text_profile']=p['text_profile']
        report['report_limits'].append('Own-text quote reconciliation uses '+p['text_profile']+'; original text and raw spans are preserved.')
    if p.get('reused_third_package_path'):
        report['date_profile_rebuild']['reused_third_package_sha256']=p['reused_third_package_sha256']
        report['report_limits'].append('The earlier approved third judgment is reused from its native evidence; it was not sent again.')
    if p.get('comparison_report_sha256'):
        report['date_profile_rebuild']['comparison_report_sha256']=p['comparison_report_sha256']
    report['report_limits'] += ['This report replays the full saved evidence under '+p['date_profile']+' and '+publication.READING_PROFILE+'. Original reports and all failures remain preserved.',f"{len(p['jobs'])} newly approved blind Claude third judgment(s); no searches or Pro calls. Subscription usage is separate.",'Unclear native-date hints and semantic publication reviews remain model-assisted assumptions, not human accuracy labels.']
    if report['phase_costs']!=p['source_costs']:raise ValueError('source costs changed')
    if report['outcome_counts']!=summary['outcome_counts']['combined']:raise ValueError('rebuilt grades differ from replay')
    return report


def run(path,approval,*,call=None):
    path=Path(path).resolve();root=path.parent;p=verify(path)
    if approval.get('approved')is not True or approval.get('package_sha256')!=digest(p)or not approval.get('approved_by')or not approval.get('approved_at'):raise ValueError('exact rebuild approval required')
    with Ledger(root/'coordinator',dict(package_sha256=digest(p),mode='live')).locked():
        if(root/'approval.json').exists():
            if load(root/'approval.json')!=approval:raise ValueError('rebuild approval changed')
        else:write_private(root/'approval.json',approval)
        if(root/'closure.json').exists():
            if p['mode']=='fixture':return load(root/'report/report.json')
            return load_completed(root/'report/report.json')[0]
        if p['jobs']:
            if call is not None:raise ValueError('live rebuild requires frozen subscription transport')
            with review_pipeline.review_execution(p)as(responses,stopped,pending,started,directory):
                if any(not r.get('native_completion_verified')for r in responses.values())or len(responses)!=len(p['jobs']):raise ValueError('third judgment execution incomplete')
        else:
            private_directory(root/'run')
            if not(root/'run/attempts.jsonl').exists():write_private(root/'run/attempts.jsonl',b'')
        if not(root/'final/summary.json').exists():
            args=_diagnostic_args(p,root/'final')
            args.setdefault('third_package',path)
            date_replay.replay(**args)
        report=_build(p,root);write_reports(private_directory(root/'report'),report)
        write_private(root/'closure.json',dict(package_sha256=digest(p),report_sha256=digest((root/'report/report.json').read_bytes()),final_summary_sha256=digest((root/'final/summary.json').read_bytes()),execution='completed',new_third_tasks=len(p['jobs']),new_pro_calls=0,new_search_calls=0))
        return report


def load_completed(path,*,seen=()):
    path=Path(path).resolve();root=path.parent.parent;p=verify(root/'package.json',historical=True);closure=load(root/'closure.json');report=load(path)
    if closure['package_sha256']!=digest(p)or closure['report_sha256']!=digest(path.read_bytes())or closure['final_summary_sha256']!=digest((root/'final/summary.json').read_bytes()):raise ValueError('rebuild closure changed')
    if p['mode']!='live':raise ValueError('fixture rebuild is not live evidence')
    approval=load(root/'approval.json')
    if approval.get('approved')is not True or approval.get('package_sha256')!=digest(p):raise ValueError('rebuild acceptance changed')
    from .source_audit import audit
    if audit(p['source_report_path'],p['review_packages'],p['native_packages'])!=load(root/'source-audit.json'):raise ValueError('native source audit does not replay')
    with tempfile.TemporaryDirectory()as temp:
        diagnostic=Path(temp)/'replay'
        args=_diagnostic_args(p,diagnostic);args.setdefault('third_package',root/'package.json')
        fresh=date_replay.replay(**args)
        saved=load(root/'final/summary.json')
        # Historical loading is full source-bound compatibility replay, not
        # permission to dispatch under a changed runtime. Code hashes are the
        # only summary metadata allowed to differ; every result is recomputed.
        if {k:v for k,v in fresh.items()if k!='code'}!={k:v for k,v in saved.items()if k!='code'}:raise ValueError('final diagnostic does not replay')
        expected=_build(p,root,diagnostic)
    for key in ('outcomes','scores','phase_costs','questions','collection','date_profile_rebuild','publication_review',
                'outcome_counts','active_review_profile','execution','resolved_pairwise'):
        if json.loads(json.dumps(expected[key]))!=report[key]:raise ValueError('rebuilt report does not replay: '+key)
    if expected.get('review_pipeline')!=report.get('review_pipeline'):raise ValueError('rebuilt report does not replay: review_pipeline')
    cases={};packet_ids={o['case_id']:o['packet_sha256']for o in report['outcomes']}
    for path in map(Path,p['review_packages']):
        for ref in load(path)['cases']:
            case=load(path.parent/ref['path']);cid=ref['case_id']
            if case['compiled']['packet_sha256']==packet_ids.get(cid):cases[cid]=case
    native=load(p['native_package_path'])
    if p.get('accepted_profile'):
        from .profile_authority import method_options
        cfg=native['configuration']
        for key in method_options(cfg):cfg.pop(key,None)
        cfg.update(deepcopy(p['accepted_profile']['method_options']))
        cfg['live_profile_authority']=deepcopy(p['accepted_profile'])
    return report,cases,native
