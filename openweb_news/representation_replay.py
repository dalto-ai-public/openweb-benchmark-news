"""Offline, complete-scope quote-repair comparison and generic review census."""
from pathlib import Path
import json

from . import date_replay,date_report,publication,representation
from .common import code_bindings,digest,load,write_private
from .phase_report import build_summary
from .review import classify

PROFILE='quote-repair-comparison-v1'


def _coverage(report,cases):
    terminal={r['case_id']for r in report.get('publication_review',{}).get('records',[])if r.get('terminal')}
    rows=[]
    for outcome in report['outcomes']:
        if outcome['grade']is not None:continue
        cid=outcome['case_id'];case=cases[cid]
        route=publication.eligibility(case['compiled'],outcome['positions'],case['provider'],selection_profile=publication.SELECTION_PROFILE)
        if route:
            rows.append(dict(case_id=cid,provider=outcome['configuration_id'],route=route,
                terminal_review_reused=cid in terminal,
                blind_packet_sha256=digest(publication.packet_for(case['compiled']))))
    return rows


def replay(rebuild_package,source_report,directory,*,expected_source_sha256):
    """No dispatcher, parent acceptance or historical code compatibility bypass."""
    path=Path(rebuild_package).resolve();source=Path(source_report).resolve();root=Path(directory).resolve()
    if root.exists():raise ValueError('use a fresh diagnostic directory')
    if root.is_relative_to(path.parent)or path.is_relative_to(root):raise ValueError('diagnostic must preserve its source root')
    p=load(path);report_sha=digest(source.read_bytes());report=load(source)
    closure=load(path.parent/'closure.json');approval=load(path.parent/'approval.json')
    if (p['profile']!=date_report.PROFILE or source!=path.parent/'report/report.json'
        or report_sha!=expected_source_sha256 or closure['report_sha256']!=report_sha
        or closure['package_sha256']!=digest(p)or report['package_sha256']!=digest(p)
        or approval.get('approved')is not True or approval.get('package_sha256')!=digest(p)
        or closure['final_summary_sha256']!=digest((path.parent/'final/summary.json').read_bytes())):
        raise ValueError('accepted source report binding differs')
    code=code_bindings();package_sha=digest(path.read_bytes())
    common=dict(source_report=p['source_report_path'],review_packages=p['review_packages'],
        publication_packages=p['publication_packages'],expected_source_sha256=p['source_report_sha256'],
        date_profile=p['date_profile'],third_package=path)
    base_summary=date_replay.replay(**common,directory=root/'baseline')
    baseline=date_report._build(p,path.parent,root/'baseline')
    for key in ('outcomes','scores','phase_costs','questions','collection','publication_review'):
        if json.loads(json.dumps(baseline[key]))!=report[key]:raise ValueError('accepted baseline does not reproduce: '+key)
    new_summary=date_replay.replay(**common,directory=root/'candidate',text_profile=representation.SPACE_PROFILE)
    if base_summary['source_bindings']!=new_summary['source_bindings']:raise ValueError('retained source bindings changed between variants')
    candidate=date_report._build(p,path.parent,root/'candidate')
    old_by={o['case_id']:o for o in baseline['outcomes']};new_by={o['case_id']:o for o in candidate['outcomes']}
    changes=[];positions=[];cases={}
    for package_path in map(Path,p['review_packages']):
        for ref in load(package_path)['cases']:
            if ref['case_id']not in old_by:continue
            case=load(package_path.parent/ref['path'])
            if case['compiled']['packet_sha256']==old_by[ref['case_id']]['packet_sha256']:cases[ref['case_id']]=case
    for cid,before in old_by.items():
        after=new_by[cid]
        if before['grade']!=after['grade']:
            changes.append(dict(case_id=cid,provider=before['configuration_id'],question_id=before['question_id'],
                before=before['grade'],after=after['grade'],reason=after['reason']))
        for role,pos in after['positions'].items():
            old=before['positions'].get(role,{})
            if old!=pos:
                positions.append(dict(case_id=cid,provider=before['configuration_id'],role=role,
                    before_state=old.get('state'),after_state=pos.get('state'),
                    before_class=classify(old),after_class=classify(pos),
                    before_computed_grade=old.get('computed_grade'),after_computed_grade=pos.get('computed_grade')))
    base_view=build_summary(baseline);new_view=build_summary(candidate)
    views={x['configuration_id']:x for x in base_view['providers']};providers={}
    base_coverage=_coverage(baseline,cases);new_coverage=_coverage(candidate,cases)
    base_reviews={r['case_id']:r for r in load(root/'baseline/publication-audits.json')if r['variant']=='combined'}
    new_reviews={r['case_id']:r for r in load(root/'candidate/publication-audits.json')if r['variant']=='combined'}
    quote_provenance=set()
    def count_quotes(value,cid,role):
        if isinstance(value,dict):
            if value.get('mode')==representation.SPACE_PROFILE+'_to_raw_span':
                quote_provenance.add((cid,role,value.get('field'),value['start'],value['end'],value.get('model_quote')))
            for child in value.values():count_quotes(child,cid,role)
        elif isinstance(value,list):
            for child in value:count_quotes(child,cid,role)
    for cid,outcome in new_by.items():
        for role,pos in outcome['positions'].items():count_quotes(pos,cid,role)
    for cid,row in new_reviews.items():
        for role,audit in row['audits'].items():count_quotes(audit,cid,'publication-review-'+role)
    for provider in new_view['providers']:
        cid=provider['configuration_id'];old=views[cid]
        own=[x for x in new_coverage if x['provider']==cid]
        providers[cid]=dict(before_grades=old['grades'],after_grades=provider['grades'],
            newly_valid_positions=sum(x['provider']==cid and x['before_state']=='invalid'and x['after_state']=='valid'for x in positions),
            newly_available_numeric_positions=sum(x['provider']==cid and x['before_class']!='conforming_numeric'and x['after_class']=='conforming_numeric'for x in positions),
            recovered_unknowns=sum(x['provider']==cid and x['before']is None and x['after']is not None for x in changes),
            known_changes=sum(x['provider']==cid and x['before']is not None for x in changes),
            space_equivalent_quotes=sum(new_by[q[0]]['configuration_id']==cid for q in quote_provenance),
            newly_valid_review_outputs=sum(row['provider']==cid and not base_reviews[key]['audits'][role]['valid']and audit['valid']
                for key,row in new_reviews.items()for role,audit in row['audits'].items()),
            broader_review_cases_before=sum(x['provider']==cid and not x['terminal_review_reused']for x in base_coverage),
            broader_review_cases_after=sum(not x['terminal_review_reused']for x in own),
            terminal_reviews_reused=sum(x['terminal_review_reused']for x in own),
            ndcg={str(k):dict(before=old['coverage'][f'ndcg@{k}']['resolved_family_mean'],
                after=provider['coverage'][f'ndcg@{k}']['resolved_family_mean'],
                questions_before=len(old['coverage'][f'ndcg@{k}']['mean_question_ids']),
                questions_after=len(provider['coverage'][f'ndcg@{k}']['mean_question_ids']),
                families_before=len(old['coverage'][f'ndcg@{k}']['included_families']),
                families_after=len(provider['coverage'][f'ndcg@{k}']['included_families']))for k in (3,5,10)})
    if code_bindings()!=code or digest(path.read_bytes())!=package_sha or digest(source.read_bytes())!=report_sha:raise ValueError('source or candidate changed during comparison')
    result=dict(profile=PROFILE,text_profile=representation.SPACE_PROFILE,selection_profile=publication.SELECTION_PROFILE,
        source_report_sha256=report_sha,rebuild_package_sha256=package_sha,code=code,
        cases=len(old_by),model_calls=0,adopted=False,baseline_reproduced=True,
        before_counts=baseline['outcome_counts'],after_counts=candidate['outcome_counts'],
        known_changes=[x for x in changes if x['before']is not None],by_provider=providers,
        proposed_review_cases=sum(not x['terminal_review_reused']for x in new_coverage),
        proposed_subscription_tasks=2*sum(not x['terminal_review_reused']for x in new_coverage),
        limits='Offline method comparison, not report adoption, historical parent acceptance, live-review approval or human accuracy. New review yield is unmeasured. Unknowns and all original evidence remain preserved.')
    for name,value in [('summary',result),('outcome-changes',changes),('position-changes',positions),('review-coverage',new_coverage)]:write_private(root/(name+'.json'),value)
    lines=['# Quote-repair comparison','',f"{len(old_by):,} saved results. No new calls; accepted report preserved.",'',
        '| Provider | Unknown before | Unknown after | Newly valid positions | New review cases |','|---|---:|---:|---:|---:|']
    for cid,row in sorted(providers.items()):lines.append(f"| {cid} | {row['before_grades'].get('unknown',0)} | {row['after_grades'].get('unknown',0)} | {row['newly_valid_positions']} | {row['broader_review_cases_after']} |")
    lines += ['',f"Changed previously known outcomes: {len(result['known_changes'])}.",f"Proposed future review: {result['proposed_review_cases']} cases, {result['proposed_subscription_tasks']} subscription tasks; not dispatched or approved.",'',result['limits']]
    write_private(root/'summary.md','\n'.join(lines).encode())
    return result
