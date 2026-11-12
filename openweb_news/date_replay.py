"""No-call date/profile comparison over explicitly bound saved pipeline evidence."""
from collections import Counter
from copy import deepcopy
from pathlib import Path

from . import adjudication, judging, publication, representation, review_pipeline
from .common import code_bindings,digest,load,records,private_directory,write_private
from .date_reading import LATEST_PROFILE as DATE_PROFILE, PROFILES as DATE_PROFILES
from .review import settle, needs_third

PROFILE='date-reading-replay-v1'


def _responses(package, root):
    run=root/'run';jobs={j['task_id']:j for j in package['jobs']};responses={}
    ledger=run/'attempts.jsonl'
    if not ledger.exists():return responses
    for e in records(ledger):
        if e['event']!='finished' or e['task_id']not in jobs:continue
        response_path=run/e['result_path'];request_path=run/'attempts'/e['attempt_id']/'request.json'
        raw=load(response_path);request=load(request_path)
        if digest(response_path.read_bytes())!=e['result_sha256']or digest(request_path.read_bytes())!=e['request_sha256']:
            raise ValueError('saved response/request ledger binding differs')
        job=jobs[e['task_id']]
        if (request!=job or job['backend_config']!=package['reviewers'][job['role']]
                or job['backend']!=job['backend_config']['backend']):
            raise ValueError('saved blind request differs from declared job')
        if not raw.get('transport_success'):continue
        from .subscription_health import terminal_verified
        if not terminal_verified(job,raw,response_path.parent):raise ValueError('saved native completion differs')
        if e['task_id']in responses:raise ValueError('multiple successful review responses')
        responses[e['task_id']]=dict(raw,native_completion_verified=True)
    return responses


def _case_grade(case,p,raw,*,date_profile=None):
    positions,replay=review_pipeline.decode(case,p['text_profile'],date_profile=date_profile)
    cid=case['compiled']['packet']['case_id'];needs=[]
    extra=raw.get('third-claude-'+cid)
    if extra:
        declared=next(j for j in p['jobs']if j['task_id']=='third-claude-'+cid)
        if declared['blind_packet']!=judging.blind_packet(case['compiled']):raise ValueError('third review input differs from canonical case')
        with representation.selected(p['text_profile']):
            if p['text_profile']==representation.SPACE_PROFILE:
                with representation.selected(representation.PROFILE):
                    pos=judging.decode_position(extra.get('verdict'),case['compiled'],date_profile=date_profile)
                if pos['state']=='invalid':pos=judging.decode_position(extra.get('verdict'),case['compiled'],date_profile=date_profile)
            else:pos=judging.decode_position(extra.get('verdict'),case['compiled'],date_profile=date_profile)
        pos.update(independent=True,prior_verdict_exposure=False);positions['claude']=pos
        replay=settle(positions['pro'],positions['codex'],pos)if needs_third(positions['pro'],positions['codex'])else settle(positions['pro'],positions['codex'])
    own={role:raw['adjudication-'+role+'-'+cid]for role in ('codex','claude')if 'adjudication-'+role+'-'+cid in raw}
    if replay['grade']is None:
        ids=adjudication.eligible(case['compiled'],positions,case['provider'],date_profile=date_profile)
        if ids and not own:
            needs.append('newly eligible adjudication has no saved responses: '+','.join(ids))
        if ids and own:
            packet=adjudication.packet_for(case['compiled'],ids,profile=p.get('adjudication_profile',adjudication.PROFILE))
            jobs=[j for j in p['jobs']if j['case_id']==cid and j['stage']=='adjudication']
            if all(j['blind_packet']==packet for j in jobs):
                with representation.selected(p['text_profile']):
                    effective,audit=adjudication.corroborate(case['compiled'],positions,packet,own,case['provider'],date_profile=date_profile)
                if audit['accepted']:replay=settle(effective['pro'],effective['codex']);positions=effective
            else:needs.append('changed adjudication input; saved review not reused')
    if replay['third_required']and not replay['third_obtained']:needs.append('third judgment not available')
    return replay['grade'],positions,needs


def _position_view(p):
    return {k:p.get(k)for k in ('state','grade','computed_grade','structural_base_grade','proposal_grade','policy_conformance','errors')}


def replay(source_report,review_packages,publication_packages,directory,*,expected_source_sha256,date_profile=DATE_PROFILE,third_package=None,text_profile=None,baseline_date_profile=None):
    """Compare candidates only; never activate them or emit a benchmark report."""
    if baseline_date_profile is not None and baseline_date_profile not in DATE_PROFILES:raise ValueError('unknown baseline date-reading profile')
    if date_profile not in DATE_PROFILES:raise ValueError("unknown date-reading profile")
    if text_profile is not None and text_profile not in representation.PROFILES:raise ValueError('unknown text representation profile')
    source=Path(source_report).resolve();root=Path(directory).resolve()
    paths=[Path(p).resolve()for p in [*review_packages,*publication_packages]]
    if root.exists():raise ValueError('use a fresh diagnostic directory')
    if any(root.is_relative_to(p.parent)or p.is_relative_to(root)for p in [source,*paths]):
        raise ValueError('diagnostic must preserve all source roots')
    if digest(source.read_bytes())!=expected_source_sha256:raise ValueError('source report hash differs')
    code=code_bindings()
    report=load(source);outcomes={o['case_id']:o for o in report['outcomes']};cases={};bindings=[]
    for path in map(Path,review_packages):
        path=path.resolve();p=load(path)
        if p['profile']!=review_pipeline.PROFILE:raise ValueError('saved pipeline package required')
        raw=_responses(p,path.parent);bindings.append(dict(path=str(path),sha256=digest(path.read_bytes()),ledger_sha256=digest((path.parent/'run/attempts.jsonl').read_bytes())))
        for ref in p['cases']:
            cid=ref['case_id']
            if cid not in outcomes:continue
            file=path.parent/ref['path']
            if file.is_symlink()or not file.resolve().is_relative_to(path.parent)or digest(file.read_bytes())!=ref['sha256']:
                raise ValueError('case input binding differs')
            case=load(file)
            if case['compiled']['packet_sha256']!=outcomes[cid]['packet_sha256']:continue
            judging.verify_compiled(case['compiled'])
            if cid in cases:
                if digest(cases[cid][0])!=digest(case):raise ValueError('overlapping source cases differ')
                continue
            cases[cid]=(case,p,raw)
    if set(cases)!=set(outcomes):raise ValueError(f'canonical case coverage differs: {len(cases)} / {len(outcomes)}')
    latest={x['case_id']:x for x in report.get('publication_review',{}).get('records',[])}
    pubs={}
    for path in map(Path,publication_packages):
        path=path.resolve();p=load(path);raw=_responses(p,path.parent)
        bindings.append(dict(path=str(path),sha256=digest(path.read_bytes()),ledger_sha256=digest((path.parent/'run/attempts.jsonl').read_bytes())))
        for ref in p['cases']:
            cid=ref['case_id']
            if cid not in latest:continue
            file=path.parent/ref['path']
            if file.is_symlink()or not file.resolve().is_relative_to(path.parent)or digest(file.read_bytes())!=ref['sha256']:raise ValueError('publication input binding differs')
            compiled=load(file)['compiled']
            canonical=publication.packet_for(compiled)
            for role in ('codex','claude'):
                declared=next(j for j in p['jobs']if j['task_id']=='publication-'+role+'-'+cid)
                if declared['blind_packet']!=canonical:raise ValueError('publication review input differs from canonical case')
            own={role:raw['publication-'+role+'-'+cid]for role in ('codex','claude')}
            if any(digest(r)!=latest[cid]['provenance'][role]['response_sha256']for role,r in own.items()):
                raise ValueError('publication response provenance differs')
            if compiled!=cases[cid][0]['compiled']:raise ValueError('publication and grading input differ')
            pubs[cid]=(compiled,own)
    if set(pubs)!=set(latest):raise ValueError('publication replay coverage differs')
    extra_jobs={};extra_raw={}
    if third_package is not None:
        third_path=Path(third_package).resolve();third=load(third_path)
        from .date_report import PROFILE as REPORT_PROFILE
        if third['profile']!=REPORT_PROFILE or third['source_report_sha256']!=expected_source_sha256 or third['date_profile']!=date_profile:raise ValueError('targeted third scope differs')
        extra_raw=_responses(third,third_path.parent)
        extra_jobs={j['case_id']:j for j in third['jobs']}
        if len(extra_jobs)!=len(third['jobs'])or len(extra_jobs)>third['maximum_new_third']:raise ValueError('targeted third roster differs')
        bindings.append(dict(path=str(third_path),sha256=digest(third_path.read_bytes()),ledger_sha256=digest((third_path.parent/'run/attempts.jsonl').read_bytes())))
    effective_rows=[];comparison_rows=[];reading_rows=[];provider_counts={};positions=[];changes=[];review_rows=[];counts={name:Counter()for name in ('baseline','date_only','review_only','combined')}
    parse_changes={};totals=Counter();native_changes=[];required_reviews=[]
    for cid,(case,p,raw)in cases.items():
        from .date_reading import native_reading_rows
        admitted=judging.native_publication_fields(case['compiled'])
        source_rows=case['compiled']['packet']['date_observations']
        baseline_rows=native_reading_rows(source_rows,admitted,profile=baseline_date_profile)
        candidate_rows=native_reading_rows(source_rows,admitted,profile=date_profile)
        for before,after in zip(baseline_rows,candidate_rows):
            if before!=after:native_changes.append(dict(case_id=cid,provider=outcomes[cid]['configuration_id'],before=before,after=after))
        old,oldpos,oldneeds=_case_grade(case,p,raw,date_profile=baseline_date_profile)
        candidate_p=p;candidate_raw=raw
        if cid in extra_jobs:
            job=extra_jobs[cid]
            if job['task_id']in raw or job['role']!='claude' or job['stage']!='third':raise ValueError('targeted third would replace an existing position')
            candidate_p=dict(p,jobs=[*p['jobs'],job]);candidate_raw=dict(raw,**{job['task_id']:extra_raw[job['task_id']]})
        if text_profile is not None:candidate_p=dict(candidate_p,text_profile=text_profile)
        new,newpos,newneeds=_case_grade(case,candidate_p,candidate_raw,date_profile=date_profile)
        for role in sorted(set(oldpos)|set(newpos)):
            before=_position_view(oldpos.get(role,{}));after=_position_view(newpos.get(role,{}));totals[role]+=1
            for comparison in newpos.get(role,{}).get('date_comparisons',[]):comparison_rows.append(dict(case_id=cid,provider=outcomes[cid]['configuration_id'],role=role,**comparison))
            for reading in newpos.get(role,{}).get('date_readings',[]):reading_rows.append(dict(case_id=cid,provider=outcomes[cid]['configuration_id'],role=role,**reading))
            if before!=after:positions.append(dict(case_id=cid,provider=outcomes[cid]['configuration_id'],role=role,before=before,after=after))
        # Verify every saved wire mention that was already resolved reads identically.
        from .date_reading import parse
        from ._method import text_dates
        for mention in case['compiled']['input']['text_dates']:
            prior=text_dates.parse(mention['display_text']);candidate=parse(mention['display_text'])
            if prior.get('interval')and prior!=candidate:raise ValueError('resolved reading changed')
            if prior!=candidate:parse_changes[digest(mention['display_text'])]=dict(before=prior,after=candidate)
        grades=dict(baseline=old,date_only=new,review_only=old,combined=new);combined_publication=None
        if cid in pubs:
            compiled,own=pubs[cid];packet=publication.packet_for(compiled);record=latest[cid]
            for name,checker,date in [('baseline',record.get('checker_profile',publication.PROFILE),baseline_date_profile),('date_only',record.get('checker_profile',publication.PROFILE),date_profile),('review_only',publication.READING_PROFILE,baseline_date_profile),('combined',publication.READING_PROFILE,date_profile)]:
                result=publication.corroborate(compiled,packet,own,checker_profile=checker,date_profile=date,
                    **(dict(text_profile=text_profile)if text_profile is not None and name in {'date_only','combined'}else {}))
                if name=='baseline' and any(result[k]!=record[k]for k in ('accepted','grade','audits','terminal')):raise ValueError('frozen publication audit differs')
                if result['accepted']:grades[name]=result['grade']
                if name=='combined':combined_publication=result
                review_rows.append(dict(case_id=cid,provider=outcomes[cid]['configuration_id'],variant=name,audits=result['audits'],accepted=result['accepted'],grade=result['grade'],terminal=result['terminal'],validity={r:a['valid']for r,a in result['audits'].items()},fills={r:a.get('reviewed_date_fills',[])for r,a in result['audits'].items()},readings={r:a.get('date_readings',[])for r,a in result['audits'].items()},comparisons={r:a.get('date_comparisons',[])for r,a in result['audits'].items()}))
        from .reporting import compact_outcome
        effective=deepcopy(outcomes[cid]);effective.update(grade=grades['combined'],positions=deepcopy(newpos),original_grade=outcomes[cid]['grade'])
        third_pos=newpos.get('claude')if needs_third(newpos['pro'],newpos['codex'])else None
        effective.update(settle(newpos['pro'],newpos['codex'],third_pos))
        if combined_publication and combined_publication['accepted']:
            for role,audit in combined_publication['audits'].items():
                effective['positions']['publication-review-'+role]=dict(state='valid',grade=audit['grade'],judgment=audit['judgment'],policy_conformance='supported_scoped_policy',independent=True,prior_verdict_exposure=False)
            effective.update(grade=combined_publication['grade'],selected_roles=['publication-review-codex','publication-review-claude'],reason='two matching valid scoped publication reviews',status='determined')

        if grades['combined'] is None and newneeds:
            required_reviews.append(dict(case_id=cid,provider=outcomes[cid]['configuration_id'],needs=newneeds))
        if effective['grade']!=grades['combined']:raise ValueError('effective positions do not reproduce grade')
        effective_rows.append(compact_outcome(effective))
        if grades['baseline']!=outcomes[cid]['grade']:raise ValueError('frozen final outcome does not replay: '+cid)
        provider=outcomes[cid]['configuration_id']
        provider_counts.setdefault(provider,{name:Counter()for name in counts})
        for name,grade in grades.items():
            label='unknown'if grade is None else str(grade)
            counts[name][label]+=1;provider_counts[provider][name][label]+=1
        if any(g!=grades['baseline']for g in grades.values()):changes.append(dict(case_id=cid,question_id=outcomes[cid]['question_id'],provider=outcomes[cid]['configuration_id'],grades=grades,needs_saved_scope_review=newneeds))
    if digest(source.read_bytes())!=expected_source_sha256:raise ValueError('source report changed')
    for b in bindings:
        if digest(Path(b['path']).read_bytes())!=b['sha256']:raise ValueError('source package changed')
        if digest((Path(b['path']).parent/'run/attempts.jsonl').read_bytes())!=b['ledger_sha256']:raise ValueError('source ledger changed')
    if code_bindings()!=code:raise ValueError('candidate code changed during replay')
    summary=dict(profile=PROFILE,baseline_date_profile=baseline_date_profile,date_profile=date_profile,checker_profile=publication.READING_PROFILE,code=code,source_report_path=str(source),source_report_sha256=expected_source_sha256,source_bindings=bindings,model_calls=0,adopted=False,native_reading_changes=len(native_changes),unresolved_review_requirements=len(required_reviews),cases=len(cases),position_totals=dict(totals),changed_positions=len(positions),outcome_counts={k:dict(v)for k,v in counts.items()},known_outcome_changes=[r for r in changes if r['grades']['baseline']is not None],by_provider={p:{name:dict(v)for name,v in own.items()}for p,own in provider_counts.items()},limits='Diagnostic over retained canonical evidence and saved review tasks only. Changed or newly required review inputs do not authorize new calls. No report adoption, calibration quality or semantic accuracy claim.')
    if text_profile is not None:summary['text_profile']=text_profile
    root=private_directory(root)
    for name,value in [('summary',summary),('native-reading-changes',native_changes),('unresolved-review-requirements',required_reviews),('position-changes',positions),('outcome-changes',changes),('publication-audits',review_rows),('reading-changes',parse_changes),('position-readings',reading_rows),('position-comparisons',comparison_rows),('effective-outcomes',effective_rows)]:write_private(root/(name+'.json'),value)
    lines=['# Offline date-reading comparison','',f"Retained results: {len(cases):,}. No model calls. Candidate results are not adopted.",'',
        '| Variant | Good answers | Leads | Not helpful | Unknown |','|---|---:|---:|---:|---:|']
    for name,own in counts.items():lines.append('| '+name+' | '+' | '.join(str(own.get(k,0))for k in ('2','1','0','unknown'))+' |')
    lines+=['','## Changes by provider','', '| Provider | Date-only unknowns resolved | Combined unknowns resolved | Known outcomes changed by combined |','|---|---:|---:|---:|']
    for provider in sorted(provider_counts):
        own=[r for r in changes if r['provider']==provider]
        values=[sum(r['grades']['baseline']is None and r['grades'][variant]is not None for r in own)for variant in ('date_only','combined')]
        values.append(sum(r['grades']['baseline']is not None and r['grades']['combined']!=r['grades']['baseline']for r in own))
        lines.append('| '+provider+' | '+' | '.join(map(str,values))+' |')
    lines+=['','Changed known grades and reasons requiring saved-scope review are listed in outcome-changes.json. A changed route with no saved third judgment remains unknown; no new task is run.','',summary['limits'],'']
    write_private(root/'summary.md','\n'.join(lines).encode())
    return summary
