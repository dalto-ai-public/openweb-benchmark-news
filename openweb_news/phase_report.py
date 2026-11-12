"""Readable, private report views built from canonical scores and explicit scopes."""
from collections import Counter
import csv
import html
import io
import json

from .common import digest, write_private
from ._method import scoring
from . import returned_fields


TERM_GUIDE = (
    ('0 — Not helpful', 'The result does not help answer this question under its rules. It may be irrelevant, outside the required dates, or fail another requirement.'),
    ('1 — Lead', 'A promising result worth following, but the returned information is not enough to give a complete answer or confirm every requirement.'),
    ('2 — Good answer', 'The returned information supports an answer and meets the question’s requirements, including dates and source rules when relevant.'),
    ('Total', 'The number of search results actually returned, including repeated results and results we could not grade. This is not the number of questions.'),
    ('Resolved', 'A result we could grade as 0, 1 or 2. A resolved result can still be unhelpful.'),
    ('Unresolved / unknown', 'The same thing here: we could not decide the result’s grade. It is not automatically a 0.'),
    ('Expected results', 'The number of questions multiplied by the selected Top 3, 5 or 10. For example, 70 questions at Top 5 means 350 expected results per provider. This total stays the same if a provider returns fewer results or a search fails.'),
    ('Observed', 'What we can count so far across all questions. Percentages use expected results as their total, including missing and unknown results. Unknown results do not count as answers yet.'),
    ('Useful', 'A lead or a good answer: grades 1 and 2 together.'),
)

COLUMN_GUIDE = (
    ('Order', 'Position in this list, sorted by nDCG on answered searches. Providers can use different resolved families, so this is a partial comparison. Providers excluded for availability are shown separately without a rank.'),
    ('Provider', 'The search service and settings we tested.'),
    ('nDCG · resolved', 'How well a provider puts good answers and leads near the top. Earlier results matter more. A score of 1 matches the best possible order of the collected results; 0 gives no useful credit. We use only questions whose scores can be calculated. This does not measure every possible answer on the web.'),
    ('Questions used', 'Questions in the resolved families supporting the nDCG score. Each provider uses only its answered questions; providers can use different resolved families. Failed searches never count as quality evidence. Related questions are grouped and given equal group weight.'),
    ('Searches completed', 'Successful searches, including successful empty responses, out of all questions. Failed and deferred searches are excluded from this count.'),
    ('Answer · observed', 'The percentage of expected results graded 2. This counts search results, not questions with at least one good answer.'),
    ('Useful · observed', 'The percentage of expected results graded 1 or 2: leads plus good answers.'),
    ('Unknown', 'The percentage of expected results whose grade is still unknown. A repeated link is counted only at its first appearance.'),
)


def build_summary(report):
    scores = report['scores']; avail = scores['availability']
    qids = sorted(avail['table'][next(iter(avail['table']))])
    questions = report.get('questions') or [dict(question_id=q,family_id=q,need_id=q) for q in qids]
    # For old reports without contracts, per-question coverage is still exact;
    # do not manufacture family-weighted provider means from missing identities.
    providers = []
    for cid,cutoffs in sorted(scores['accounting'].items()):
        own = [o for o in report['outcomes'] if o['configuration_id']==cid]
        calls = [r for r in report.get('collection',[]) if r['configuration_id']==cid]
        coverage = {}
        for k in (3,5,10):
            for name in ('answer','useful','ndcg'):
                rows = [r for r in scores['question_rows'] if r['configuration_id']==cid and r['k']==k]
                answered = set(avail['answered_question_ids'][cid])
                values = {(r['question_id'],cid):r['metrics'][name] if r['call_status'] in scoring.ANSWERED
                    else scoring.not_applicable('failed or uncollected call: '+r['call_status']) for r in rows}
                included = sorted(q for q in qids if values[q,cid]['status']=='determined')
                excluded = {q:values[q,cid].get('reason',values[q,cid]['status']) for q in qids if q not in included}
                metric = scoring.family_mean(questions,[cid],answered,values) if report.get('questions') else None
                coverage[f'{name}@{k}'] = dict(included_questions=included,excluded_questions=excluded,
                    resolved_family_mean=metric['per_configuration'][cid]['resolved_family_mean'] if metric else None,
                    included_families=sorted(metric['family_values']) if metric else None,
                    mean_question_ids=sorted(q['question_id'] for q in questions if q['family_id'] in metric['family_values']) if metric else [],
                    excluded_families=metric['excluded_families'] if metric else None)
        positions = {role:dict(Counter(o.get('states',{}).get(role,'unjudged') for o in own)) for role in ('pro','codex','claude')}
        providers.append(dict(configuration_id=cid,returned=len(own),grades=dict(Counter('unknown' if o['grade']is None else str(o['grade'])for o in own)),
            call_states=dict(Counter(avail['table'][cid].values())),
            answered_question_count=len(avail['answered_question_ids'][cid]),
            comparison_included=cid in avail['comparison_configurations'],
            cutoffs={str(k):v for k,v in cutoffs.items()},coverage=coverage,position_states=positions))
    inventory = []
    by_id = {q['question_id']:q for q in questions}
    for qid in qids:
        own = [o for o in report['outcomes']if o['question_id']==qid];q=by_id[qid]
        inventory.append(dict(question_id=qid,query=q.get('provider_query',''),family_id=q['family_id'],
            grades=dict(Counter('unknown'if o['grade']is None else str(o['grade'])for o in own)),
            unresolved_providers=sorted({o['configuration_id']for o in own if o['grade']is None})))
    strict = scores['aggregates']['delivered_utility']['metrics']
    return dict(profile='readable-phase-report-v1',source_report_sha256=digest(report),question_count=len(qids),
        provider_count=len(providers),comparison_configurations=avail['comparison_configurations'],returned=len(report['outcomes']),outcome_counts=report.get('outcome_counts'),
        execution=report.get('execution'),providers=providers,questions=inventory,
        strict_common={name:dict(label=strict[name]['label'],included_families=sorted(strict[name]['family_values']),excluded_families=strict[name]['excluded_families'])
                       for name in ('answer@5','useful@5','ndcg@5')},
        unresolved_dependencies=scores['dependencies'],native_date_accounting=scores['native_date_accounting'],
        costs=report.get('phase_costs'),components=report.get('phase_components',[]),
        review_records=report.get('review_pipeline',{}).get('records',[]),evidence_inventory=report.get('evidence_inventory',{}),
        limits=report.get('report_limits',[]),publication_review=report.get('publication_review'),
        returned_fields_analysis=returned_fields.analyze(report))


def write_readable(directory, report):
    summary = build_summary(report)
    returned_fields.write(directory, summary['returned_fields_analysis'])
    def cell(v):return str(v).replace('&','&amp;').replace('<','&lt;').replace('>','&gt;').replace('|','&#124;').replace('\n',' ')
    def counts(values):
        labels={'2':'answers','1':'leads','0':'not helpful','unknown':'unknown','present':'present','missing':'missing',
                'conforming_numeric':'supported','nonconforming_numeric':'unsupported','valid_unknown':'unknown','unjudged':'not used'}
        return ', '.join(f"{n} {labels.get(k,k.replace('_',' '))}" for k,n in values.items()) or 'none'
    def pct(v):return f'{v:.1%}'
    lines = ['# News benchmark phase report','',
        f"{summary['question_count']} questions · {summary['provider_count']} providers · {summary['returned']:,} search results returned.",'',
        f"Execution: {summary['execution'].get('state','not supplied')}. Grades: {counts(summary['outcome_counts'])}.",'',
        'These are model-reviewed benchmark results, not human accuracy labels. The ranked table uses answered-search quality on each provider’s resolved families, which may differ; providers excluded for availability are unranked. Check how many questions support each score. Unknown results keep their unknown grade.','',
        '## Provider outcomes','',
        'Percentages count good answers or leads out of the number of expected results. Missing results and unknown grades remain in that total. Useful means a lead or a good answer. Unknown grades may change these percentages later.','',
        '| Provider | Searches: results / no results / other | Returned | Results: good answer / lead / not helpful / unknown | Answer@5 observed | Useful@5 observed | Answer@10 observed | Useful@10 observed | Unknown@5 | Unknown@10 | Answer@5 resolved questions |',
        '|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|']
    guide_lines=['## Terms used in the table','']
    for label,explanation in TERM_GUIDE:guide_lines.append(f'- {label}: {explanation}')
    guide_lines.extend(['', '## Column guide',''])
    for label,explanation in COLUMN_GUIDE:guide_lines.extend([f'- {label}: {explanation}'])
    guide_lines.extend(['', 'The cutoff selects the first 3, 5 or 10 ranked results per question. Percentages use the number of expected results. If a provider returns fewer results or a search fails, we still use that expected total. Repeated links count only once. Unknown grades may increase the answer or useful percentage later.', '', 'Example: 20 questions at Top 5 means 100 expected results per provider. If 40 are good answers, 20 are leads and 10 are unknown, Answer is 40%, Useful is 60% and Unknown is 10%. Even if only 80 results were returned, we still divide by 100.', ''])
    lines[8:8]=['## Key points at Top5','']+['- '+cell(p)for p in key_points(summary)]+['']+guide_lines
    flat = []
    for p in summary['providers']:
        a=p['cutoffs']['5'];b=p['cutoffs']['10'];c=p['call_states'];g=p['grades'];n=a['counts']
        calls=f"{c.get('success',0)} / {c.get('success_empty',0)} / {sum(v for k,v in c.items()if k not in ('success','success_empty'))}"
        grades=' / '.join(str(g.get(k,0))for k in ('2','1','0','unknown'))
        useful=(n.get('answer',0)+n.get('lead',0))/a['slots']
        useful10=(b['counts'].get('answer',0)+b['counts'].get('lead',0))/b['slots']
        covered=len(p['coverage']['answer@5']['included_questions'])
        lines.append('| '+' | '.join([cell(p['configuration_id']),calls,str(p['returned']),grades,pct(a['observed_answer_rate']),pct(useful),pct(b['observed_answer_rate']),pct(useful10),pct(a['unresolved_share']),pct(b['unresolved_share']),f"{covered}/{summary['question_count']}"])+' |')
        flat.append(dict(configuration=p['configuration_id'],comparison_included=p['comparison_included'],searches_completed=p['answered_question_count'],returned=p['returned'],answer5_observed=a['observed_answer_rate'],useful5_observed=useful,answer10_observed=b['observed_answer_rate'],useful10_observed=useful10,
            unknown5_share=a['unresolved_share'],unknown10_share=b['unresolved_share'],scheduled5=a['slots'],scheduled10=b['slots'],
            answer5_included_questions=json.dumps(p['coverage']['answer@5']['included_questions']),answer5_excluded_questions=json.dumps(p['coverage']['answer@5']['excluded_questions'],sort_keys=True)))
    lines += ['', '## Resolved comparisons','',
        'The delivered-utility comparison uses the same complete resolved families for every provider and counts failed searches as zero delivery, not quality evidence. It may remain pending even when the run is finished. Pairwise comparisons are in pairwise.csv; different pairs can use different families.','']
    for name,entry in summary['strict_common'].items():lines += [f"- {name}: {entry['label']}."]
    lines += ['', '| Provider | Scope | Answer@5 on questions we could score | Questions used / left out | Included / excluded question groups |', '|---|---|---:|---:|---:|']
    for p in summary['providers']:
        c=p['coverage']['answer@5'];mean='pending'if c['resolved_family_mean']is None else pct(c['resolved_family_mean'])
        families='not supplied'if c['included_families']is None else f"{len(c['included_families'])} / {len(c['excluded_families'])}"
        lines += [f"| {cell(p['configuration_id'])} | {'compared provider; own answered scope' if p['comparison_included'] else 'partial coverage; not ranked'} | {mean} | {len(c['included_questions'])} / {len(c['excluded_questions'])} | {families} |"]
    lines += ['', 'Each own-provider quality mean can use a different answered subset. It is a partial diagnostic, not a common-scope ranking. Providers excluded for availability have no main rank. Exact included/excluded question and family IDs are in phase-summary.json.','',
        '## Question coverage','', '| Question | Grades 2 / 1 / 0 / unknown | Providers with unresolved results | Query |','|---|---:|---|---|']
    for q in summary['questions']:
        g=q['grades'];lines += ['| '+' | '.join([cell(q['question_id']),' / '.join(str(g.get(k,0))for k in ('2','1','0','unknown')),cell(', '.join(q['unresolved_providers'])),cell(q['query'])])+' |']
    if summary['evidence_inventory']:
        lines += ['', '## Returned fields and collection times', '', '| Provider | Title states | Snippet states | Earliest / latest capture |', '|---|---|---|---|']
        for cid, row in sorted(summary['evidence_inventory'].items()):
            lines += ['| '+' | '.join([cell(cid),cell(counts(row['field_states']['title'])),cell(counts(row['field_states']['snippet'])),cell(str(row['earliest_capture'])+' / '+str(row['latest_capture']))])+' |']
        lines += ['', 'Missing titles and short lists reflect the saved native projections. Available snippets are still graded. Capture time is collection provenance, not publication proof.']
    lines += ['', '## Judgment gaps','',
        f"{len(summary['unresolved_dependencies'])} unresolved occurrences have recorded metric dependencies. Unknown is not a failed retrieval and is not proof of irrelevant content.",'',
        'Distinct and unique stories are not measured yet; no complete reference answer key or calibrated accuracy claim is produced. Native dates establish only their declared mapped meaning, not automatic article applicability or historical availability.','',
        '| Provider | Pro states | Sol states | Conditional Claude states |','|---|---|---|---|']
    for p in summary['providers']:
        lines += ['| '+ ' | '.join([cell(p['configuration_id']),*[cell(counts(p['position_states'][r]))for r in ('pro','codex','claude')]])+' |']
    unresolved = [r for r in summary['review_records']if not r['accepted']]
    lines += ['', f"Publication-policy review: {len(summary['review_records'])-len(unresolved)} corroborated applications; {len(unresolved)} retained unresolved. Sol and Pro also supplied the original proposals. Blind replication does not establish independent errors or accuracy.",'']
    for r in unresolved:lines += [f"- {cell(r['case_id'])}: {cell('; '.join(role+': '+str(reason) for role,reason in r['reasons'].items()))}"]
    if summary['costs']:
        c=summary['costs'];lines += ['', '## Cost estimates','',
            f"Estimated Pro cost from recorded usage: ${float(c['published_completed_pro_usd']):,.2f} (published token prices).",'',
            f"Conservative Pro budget estimate: ${c['conservative_completed_pro_usd']}.",'',
            f"Other conservative allowances and holds: ${c['other_conservative_holds_and_allowances_usd']}. Published-Pro plus conservative residual: ${c['published_pro_plus_conservative_residual_usd']}. Fully conservative cumulative total: ${c['conservative_total_usd']}.",'',
            'These totals include the original phase and additional diagnostic/replacement calls once. Subscriptions are separate. Estimates and planning holds are not invoices.','']
    if summary.get('publication_review'):
        rows=summary['publication_review']['records']
        lines += ['', '## Publication review', '']
        for cid in sorted({r['configuration_id'] for r in rows}):
            settled=[r for r in rows if r['configuration_id']==cid and r['accepted']]
            g=Counter(r['grade'] for r in settled)
            lines.append(f"- {cell(cid)}: {len(settled)} unknown results settled by review ({g[2]} answers, {g[1]} leads, {g[0]} not helpful).")
        lines += ['', 'Review version: '+cell(summary['publication_review']['profile'])+'.', '', 'Earlier judgments and original reports are preserved. Unsettled reviews remain unknown and are reused without repeated calls.']
    lines += ['## Titles, metadata and snippet length','', 'The automatic analysis is in report.html, returned-fields.json, returned-fields.csv and snippet-length-quality.csv. Provider tables sort by median snippet characters, not quality. Unknown grades and unavailable measurements remain separate. Length associations do not establish causation.', '']
    lines += ['## Versions and limits','']
    for c in summary['components']:
        if 'parent_report_path'in c:
            lines += [f"- {len(c['question_ids'])} retained questions; verified parent report SHA-256 {c['parent_report_sha256']}."]
        else:
            lines += [f"- {len(c['question_ids'])} questions: {c['adjudication_profile']}; report SHA-256 {c['report_sha256']}."]
        recovered = c.get('recovered_terminal_completions', [])
        if recovered:
            sol=[p for p in recovered if p['profile']=='codex-reconnected-terminal-completion-v1']
            sol_v2=[p for p in recovered if p['profile']=='codex-retried-terminal-completion-v2']
            claude=[p for p in recovered if p['profile']=='claude-retried-terminal-completion-v1']
            lines += [f"- Native completion audit: {c['native_subscription_proofs']} normal proofs. Recovered completions remain separate from normal account health."]
            if sol:
                lines += [f"- {len(sol)} completed Sol turns followed {sum(p['error_count']for p in sol)} native websocket reconnect events (HTTP403). Context, request, echoes, final verdicts and successful terminal events were checked separately."]
            if claude:
                lines += [f"- {len(claude)} completed Claude calls followed {sum(p['error_count']for p in claude)} internal retry notices with an unspecified error. The retained request, session, model/isolation settings and matching successful final output were checked. Claude does not independently echo the full prompt in its native log."]
            if sol_v2:
                lines += [f"- {len(sol_v2)} completed Sol turns followed {sum(p['error_count']for p in sol_v2)} native retry events. Same-model request, schema, input echo, verdict and successful terminal bindings were verified; these are recovered completions, not normal account health."]
            lines += ['- Native internal retries do not establish the underlying model-request count. Original errors and missing normal-health proofs remain preserved.']
    for limit in summary['limits']:lines += ['- '+cell(limit.replace('scheduled slots','expected results').replace('resolved-question/family comparisons','comparisons on questions we could score'))]
    text='\n'.join(lines)+'\n'
    write_private(directory/'report.md',text.encode(),replace=True)
    write_private(directory/'phase-summary.json',summary,replace=True)
    out=io.StringIO(newline='');writer=csv.DictWriter(out,fieldnames=list(flat[0]));writer.writeheader();writer.writerows(flat)
    write_private(directory/'providers.csv',out.getvalue().encode(),replace=True)
    # Keep the report entirely offline, escaped and navigable. Render our small
    # Markdown subset directly, with no package/browser/network dependency.
    body=[];table=False;items=False
    for line in lines:
        if not line.startswith('|')and table:body.append('</tbody></table></div>');table=False
        if not line.startswith('- ')and items:body.append('</ul>');items=False
        if line.startswith('|---'):continue
        if line.startswith('|'):
            values=line.strip('|').split('|');tag='th'if not table else'td'
            if not table:body.append('<div class="table"><table><tbody>');table=True
            body.append('<tr>'+''.join('<'+tag+'>'+html.escape(v.strip())+'</'+tag+'>'for v in values)+'</tr>')
        elif line.startswith('## '):body.append('<h2>'+html.escape(line[3:])+'</h2>')
        elif line.startswith('# '):body.append('<h1>'+html.escape(line[2:])+'</h1>')
        elif line.startswith('- '):
            if not items:body.append('<ul>');items=True
            body.append('<li>'+html.escape(line[2:])+'</li>')
        elif line:body.append('<p>'+html.escape(line)+'</p>')
    if table:body.append('</tbody></table></div>')
    if items:body.append('</ul>')
    document = interactive_html(summary, body)
    write_private(directory/'report.html',document.encode(),replace=True)
    return summary


def ordered_providers(summary, k):
    """Sort answered-search own-provider quality; excluded providers are separate."""
    def key(row):
        value = row['coverage'][f'ndcg@{k}']['resolved_family_mean']
        return (value is None, -value if value is not None else 0, row['configuration_id'])
    return sorted((p for p in summary['providers'] if p.get('comparison_included', True)), key=key)


def key_points(summary):
    """Plain observations at Top5, with explicit denominators."""
    from ._method.providers import configuration_catalog
    names={p['configuration_id']:p.get('provider',p['configuration_id'])for p in configuration_catalog(include_optional=True)}
    name=lambda p:names.get(p['configuration_id'],p['configuration_id'])+' ('+p['configuration_id']+')'
    answer=lambda p:p['cutoffs']['5']['observed_answer_rate']
    useful=lambda p:sum(p['cutoffs']['5']['counts'].get(k,0)for k in ('answer','lead'))/p['cutoffs']['5']['slots']
    included=[p for p in summary['providers'] if p.get('comparison_included',True)]
    ranked=sorted((p for p in included if answer(p) is not None),key=lambda p:(-answer(p),name(p)))
    useful_ranked=sorted((p for p in included if useful(p) is not None),key=lambda p:(-useful(p),name(p)))
    fmt=lambda rows,metric:', '.join(name(p)+f' {metric(p):.1%}'for p in rows)
    unknown=(summary['outcome_counts']or{}).get('unknown',0)
    points=['Compared providers, Top5 delivered, highest answer percentages: '+fmt(ranked[:3],answer)+'.',
            'Compared providers, Top5 delivered, lowest answer percentages: '+fmt(sorted(ranked,key=lambda p:(answer(p),name(p)))[:2],answer)+'.',
            'Compared providers, Top5 delivered, highest useful percentages (answers and leads): '+fmt(useful_ranked[:3],useful)+'.',
            f'Across all returned results, unknown after review: {unknown} of {summary["returned"]} returned results ({unknown/summary["returned"]:.1%}).'if summary['returned']else'No results returned.']
    returned=[p for p in summary['providers']if p['returned']]
    share=lambda p:p['grades'].get('unknown',0)/p['returned']
    points.append('Across all returned results, largest unknown shares: '+fmt(sorted(returned,key=lambda p:(-share(p),name(p)))[:2],share)+'. These percentages use returned results; the table uses expected results (questions × the selected Top 3, 5 or 10).')
    if summary['questions']:
        q=sorted(summary['questions'],key=lambda q:(-q['grades'].get('unknown',0),q['question_id']))[0]
        points.append(f'Across all returned results, most unknown results for one question: {q["question_id"]}, {q["grades"].get("unknown",0)}.')
    points.append('Grades come from AI model reviews. Distinct and unique stories are not measured yet; different links can cover the same story.')
    return points


def interactive_html(summary, detail_body):
    esc = lambda value: html.escape(str(value), quote=True)
    panels = []
    for k in (3,5,10):
        rows = []
        for index,p in enumerate(ordered_providers(summary,k),1):
            metric=p['coverage'][f'ndcg@{k}'];a=p['cutoffs'][str(k)];counts=a['counts']
            score=metric['resolved_family_mean'];covered=len(metric.get('mean_question_ids',metric['included_questions']))
            useful=(counts.get('answer',0)+counts.get('lead',0))/a['slots']
            values=[index,p['configuration_id'],'pending' if score is None else f'{score:.3f}',
                    f"{covered} / {summary['question_count']}",f"{p['answered_question_count']} / {summary['question_count']}",f"{a['observed_answer_rate']:.1%}",f'{useful:.1%}',f"{a['unresolved_share']:.1%}"]
            rows.append('<tr>'+''.join('<td>'+esc(v)+'</td>'for v in values)+'</tr>')
        headers=['Order','Provider',f'nDCG@{k} · resolved', 'Questions used','Searches completed',f'Answer@{k} · delivered',f'Useful@{k} · delivered',f'Unknown@{k}']
        panels.append(f'<section class="panel panel-{k}" aria-label="Top {k} results"><p class="note"><strong>Percentage total per provider: {summary["question_count"]} questions × {k} results = {summary["question_count"] * k:,} expected results.</strong> Delivered Answer, Useful and Unknown percentages all use this total, including failed searches. They describe delivery, not the common-family quality scope used by nDCG.</p><div class="table"><table><thead><tr>'+''.join('<th scope="col">'+esc(h)+'<span class="column-hint">'+esc(hint)+'</span></th>'for h,hint in zip(headers,['Sorted by nDCG','Search configuration','Ranking quality · 0–1','Quality questions / total','Successful searches / total','Good answers / expected results','Good answers + leads / expected results','Unknown results / expected results']))+'</tr></thead><tbody>'+''.join(rows)+'</tbody></table></div></section>')
    excluded = [p for p in summary['providers'] if not p.get('comparison_included', True)]
    if excluded:
        for index, k in enumerate((3, 5, 10)):
            rows = []
            for p in excluded:
                metric = p['coverage'][f'ndcg@{k}']
                value = metric['resolved_family_mean']
                failed=sum(v for status,v in p['call_states'].items() if status not in scoring.ANSWERED and status!='not_collected')
                values = [p['configuration_id'], f"{p['answered_question_count']} / {summary['question_count']}",
                    f"{failed} / {p['call_states'].get('not_collected',0)}", f"{len(metric['mean_question_ids'])} / {summary['question_count']}",
                    'pending' if value is None else f'{value:.3f}']
                rows.append('<tr>'+''.join('<td>'+esc(v)+'</td>' for v in values)+'</tr>')
            partial = '<h3>Partial coverage — outside the main comparison</h3><p>No rank: these scores use only each provider’s answered questions and are not directly comparable with the main table.</p><div class="table"><table><thead><tr>'+''.join('<th scope="col">'+esc(v)+'</th>' for v in ['Provider','Searches completed','Failed / deferred searches','Questions used',f'nDCG@{k} · partial'])+'</tr></thead><tbody>'+''.join(rows)+'</tbody></table></div>'
            panels[index] = panels[index].replace('</section>', partial+'</section>')
    terms='<ul class="metric-terms">'+''.join('<li><strong>'+esc(label)+':</strong> '+esc(explanation)+'</li>'for label,explanation in TERM_GUIDE)+'</ul>'
    guide='<details class="column-guide" open><summary>How to read the columns</summary><dl>'+''.join('<dt>'+esc(label)+'</dt><dd>'+esc(explanation)+'</dd>'for label,explanation in COLUMN_GUIDE)+'</dl><p class="note">Example: 20 questions × Top 5 = 100 expected results per provider. With 40 good answers, 20 leads and 10 unknown results: Answer = 40%, Useful = 60%, Unknown = 10%. If only 80 results were returned, we still divide by 100.</p><p class="note">Missing results and failed searches do not reduce the number we divide by. Repeated links count only once. Unknown grades are not counted as good answers or leads yet, so the percentages may increase later. The overview counts all returned results; choosing Top 3, 5 or 10 changes the table below.</p></details>'
    # Native radios work offline and with JavaScript disabled. Only one cutoff
    # table is visible; the label and keyboard controls remain ordinary HTML.
    selectors=''.join(f'<input class="cutoff-input" type="radio" name="cutoff" id="cutoff-{k}"'+(' checked'if k==5 else'')+'><label for="cutoff-'+str(k)+'">Top '+str(k)+'</label>'for k in (3,5,10))
    import re
    details=[]
    parts=re.split(r'<h2>(.*?)</h2>', ''.join(detail_body))
    for i in range(1,len(parts),2):
        if parts[i] in {'Column guide','Terms used in the table'}:continue
        details.append('<details><summary>'+parts[i]+'</summary>'+parts[i+1]+'</details>')
    grades=summary['outcome_counts'] or {}
    cards=''.join('<div class="card"><strong>'+esc(value)+'</strong><span>'+esc(label)+'</span></div>'for label,value in
                  [('Questions',summary['question_count']),('Providers',summary['provider_count']),('Total results returned',f"{summary['returned']:,}"),('Unknown results',grades.get('unknown',0))])
    css="""body{font:16px/1.55 system-ui;margin:40px auto;padding:0 24px;max-width:1280px;color:#172335;background:#f5f7fb}h1{font-size:32px;margin-bottom:8px}h2{font-size:22px}.intro{color:#526174;max-width:960px}.cards{display:flex;flex-wrap:wrap;gap:12px;margin:24px 0}.card{background:white;border:1px solid #dbe3ee;border-radius:12px;padding:16px 24px;min-width:145px}.card strong{display:block;font-size:28px}.card span{color:#59697d;font-size:14px}.table{overflow:auto;background:white;border:1px solid #d4dce6;border-radius:10px;margin:20px 0}table{border-collapse:collapse;width:100%;font-size:14px}th,td{padding:13px 14px;border-bottom:1px solid #e4e9f0;text-align:right;white-space:nowrap}th{background:#eaf0f7;font-size:12px}.column-hint{display:block;font-size:11px;font-weight:400;white-space:normal;max-width:170px;color:#526174;margin-top:5px}.column-guide dl{display:grid;grid-template-columns:175px 1fr;gap:12px 20px;margin:20px 0}.column-guide dt{font-weight:650}.column-guide dd{margin:0;max-width:920px}.column-guide p{max-width:1100px}td:nth-child(2),th:nth-child(2){text-align:left}tr:nth-child(even){background:#f8fafc}tbody tr:hover{background:#edf4ff}.cutoff-input{position:absolute;opacity:0;width:1px;height:1px}.cutoff-input+label{display:inline-block;cursor:pointer;padding:10px 25px;margin-right:8px;background:white;border:1px solid #c7d3e2;border-radius:8px;font-weight:600}.cutoff-input:checked+label{background:#1e4fd0;color:white;border-color:#1e4fd0}.cutoff-input:focus-visible+label{outline:3px solid #75a9ff;outline-offset:3px}.panel{display:none}#cutoff-3:checked~.panel-3,#cutoff-5:checked~.panel-5,#cutoff-10:checked~.panel-10{display:block}.note{font-size:14px;color:#526174;max-width:1100px}details{background:white;border:1px solid #dbe3ee;border-radius:10px;margin:14px 0;padding:16px}summary{font-weight:650;cursor:pointer}details p,details li{overflow-wrap:anywhere}details td{white-space:normal;text-align:left}@media(max-width:650px){.column-guide dl{grid-template-columns:1fr;gap:6px}.column-guide dd{margin-bottom:12px}body{margin:20px auto;padding:0 12px}.card{min-width:100px;padding:12px}.cutoff-input+label{padding:9px 18px}}"""
    return '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'"><title>News benchmark report</title><style>'+css+'</style><h1>News benchmark</h1><p class="intro">Search coverage is shown for every provider; failed or deferred searches are separate from quality evidence. Choose whether to compare their first 3, 5 or 10 search results. Grades come from model reviews and are not a guarantee of factual accuracy.</p><div class="cards">'+cards+'</div><h2>Key points at Top5</h2><ul>'+''.join('<li>'+esc(p)+'</li>'for p in key_points(summary))+'</ul><h2>Provider results</h2><p class="note">Sorted by nDCG on answered searches, highest first. Each provider can use different resolved families; this is a partial comparison. Providers excluded for availability appear separately without a rank. Scores that cannot yet be calculated appear last.</p>'+terms+'<div class="cutoff-selector" role="group" aria-label="Results cutoff">'+selectors+''.join(panels)+'</div><p class="note">Answer, Useful and Unknown are percentages of search results, not percentages of questions answered. “Questions used” shows how much of the test supports the nDCG score. For nDCG, “resolved” means enough information is available to calculate the score; it does not mean every result received grade 2.</p>'+guide+returned_fields.render_html(summary['returned_fields_analysis'])+'<h2>Details</h2>'+''.join(details)+'</html>'
