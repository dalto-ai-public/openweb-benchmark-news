"""Cheap descriptive title/metadata/snippet measurements for every pipeline report."""
from collections import Counter, defaultdict
import csv
import html
import io
import json
import math
from pathlib import Path
import re
import statistics

from .common import digest, write_private

CONFIG = json.loads((Path(__file__).with_name('assets')/'returned-fields-v1.json').read_bytes())
SNIPPET = re.compile(r'(?:snippet|additional_snippet_\d+)(?:\[\d+\])?\Z')
PRIMARY = re.compile(r'snippet(?:\[\d+\])?\Z')


def nonempty(value):
    return value is not None and value != [] and value != {} and (not isinstance(value, str) or bool(value.strip()))


def _measure(texts, metadata, dates, snippet_state=None):
    title = [text for field,text in texts if re.fullmatch(r'title(?:\[\d+\])?',field)]
    primary = [text for field,text in texts if PRIMARY.fullmatch(field)]
    additional = [text for field,text in texts if SNIPPET.fullmatch(field) and not PRIMARY.fullmatch(field)]
    snippets = primary + additional
    return dict(title_chars=sum(map(len,title)),title_words=sum(len(s.split()) for s in title),
                title_present=any(s.strip() for s in title),
                snippet_chars=sum(map(len,snippets)),snippet_words=sum(len(s.split()) for s in snippets),
                primary_snippet_chars=sum(map(len,primary)),additional_snippet_chars=sum(map(len,additional)),
                snippet_fields=len(snippets),
                snippet_state=('present' if any(s.strip() for s in snippets) else 'empty' if snippets or snippet_state in {'present','empty'} else snippet_state or 'missing'),
                metadata_entries=sum(nonempty(v) for k,v in metadata if k.split('.')[-1] not in CONFIG['mechanical_metadata_fields']),
                date_metadata_present=any(nonempty(v) for v in dates))


def measure_position(position):
    """Native projection only: no question, grade, model or article fetch needed."""
    texts=[]
    fields=[('title',position.get('title',{})),('snippet',position.get('content',{}))]
    fields += [(f'additional_snippet_{i}',v) for i,v in enumerate(position.get('additional_content',{}).values())]
    for field,item in fields:
        value=item.get('value')
        if isinstance(value,str):texts.append((field,value))
        elif isinstance(value,list):texts.extend((f'{field}[{i}]',s) for i,s in enumerate(value) if isinstance(s,str))
    metadata=[(k,v.get('value')) for k,v in position.get('metadata',{}).items() if v.get('present')]
    dates=[r.get('value') for r in position.get('native_dates',{}).get('observations',[])]
    return _measure(texts,metadata,dates,position.get('content',{}).get('state'))


def measure_packet(packet):
    """Same measurements for replaying already verified own-evidence packets."""
    metadata=[]
    for row in packet.get('native_metadata',[]):
        if row['field'].startswith('native:'):
            metadata.append((row['field'][7:],json.loads(row['text'])))
    return _measure([(r['field'],r['text']) for r in packet.get('allowed_text',[])],metadata,
                    [r.get('raw_value') for r in packet.get('date_observations',[])],
                    packet.get('native_field_states',{}).get('snippet'))


def distribution(values):
    if not values:return dict(count=0,mean=None,median=None,p90=None,p95=None,maximum=None)
    values=sorted(values);n=len(values)
    return dict(count=n,mean=statistics.fmean(values),median=values[math.ceil(n*.5)-1],
                p90=values[math.ceil(n*.9)-1],p95=values[math.ceil(n*.95)-1],maximum=values[-1])


def band(row):
    if row['snippet_state']=='missing':return 'missing'
    if row['snippet_state'] not in {'present','empty'}:return 'unusable'
    if row['snippet_state']=='empty' or not row['snippet_chars']:return 'empty'
    bounds=CONFIG['length_band_lower_bounds']
    lower=max(v for v in bounds if v<=row['snippet_chars'])
    i=bounds.index(lower)
    return f'{lower}–{bounds[i+1]-1}' if i+1<len(bounds) else f'{lower}+'


def quality(rows):
    counts=Counter(r['grade'] for r in rows)
    resolved=sum(counts[g] for g in (0,1,2))
    return dict(results=len(rows),resolved=resolved,good_answers=counts[2],leads=counts[1],not_helpful=counts[0],
                unknown=counts[None],ungraded=counts['ungraded'],
                answer_rate_resolved=counts[2]/resolved if resolved else None,
                useful_rate_resolved=(counts[2]+counts[1])/resolved if resolved else None)


def analyze(report):
    """Always recompute from collection measurements and the current final grades."""
    outcomes=report.get('outcomes',[])
    joinable=[o for o in outcomes if type(o.get('rank')) is int]
    grades={(o['configuration_id'],o['question_id'],o['rank']):o['grade'] for o in joinable}
    if len(grades)!=len(joinable):raise ValueError('duplicate returned-field grade join')
    measured={};seen=set();providers=set((report.get('scores') or {}).get('accounting',{}))|{o['configuration_id'] for o in outcomes};cells=Counter();covered=Counter()
    for cell in report.get('collection',[]):
        cid,qid=cell['configuration_id'],cell['question_id'];providers.add(cid);cells[cid]+=1
        if (cid,qid) in seen:raise ValueError('duplicate returned-field collection cell')
        seen.add((cid,qid))
        if 'returned_field_observations' not in cell:continue
        if cell.get('returned_field_profile')!=CONFIG['profile']:raise ValueError('unknown returned-field observation profile')
        covered[cid]+=1
        for item in cell['returned_field_observations']:
            key=(cid,qid,item['rank'])
            if key in measured:raise ValueError('duplicate returned-field measurement')
            measured[key]=dict(item,grade=grades.get(key,'ungraded'))
    provider_rows=[];bands=[]
    # Band labels derive from the versioned boundaries, never from this dataset.
    b=CONFIG['length_band_lower_bounds'];labels=['missing','unusable','empty']+[f'{v}–{b[i+1]-1}' if i+1<len(b) else f'{v}+' for i,v in enumerate(b)]
    groups=defaultdict(list)
    for (cid,qid,rank),r in measured.items():groups[cid].append(r)
    for cid in sorted(providers):
        rows=groups[cid];n=len(rows);own=[o for o in outcomes if o['configuration_id']==cid]
        missing=len(own)-sum(key in measured for key in grades if key[0]==cid)
        p=dict(configuration_id=cid,measured_results=n,outcome_results=len(own),unmeasured_outcomes=missing,
               collection_cells=cells[cid],measured_collection_cells=covered[cid],
               snippet_states=dict(Counter(r['snippet_state'] for r in rows)),
               title_present=sum(r['title_present'] for r in rows),metadata_present=sum(r['metadata_entries']>0 for r in rows),
               date_metadata_present=sum(r['date_metadata_present'] for r in rows),quality=quality(rows))
        for metric in ('snippet_chars','snippet_words','primary_snippet_chars','additional_snippet_chars','title_chars','title_words','metadata_entries'):
            p[metric]=distribution([r[metric] for r in rows])
        provider_rows.append(p)
        for label in labels:bands.append(dict(configuration_id=cid,length_band=label,**quality([r for r in rows if band(r)==label])))
    provider_rows.sort(key=lambda p:(p['snippet_chars']['median'] is None,-(p['snippet_chars']['median'] or 0),p['configuration_id']))
    return dict(profile=CONFIG['profile'],rules=CONFIG,rules_sha256=digest(CONFIG),providers=provider_rows,
                length_bands=bands,measured_results=len(measured),outcome_results=len(outcomes),
                unmeasured_outcomes=len(outcomes)-sum(key in measured for key in grades),
                status='complete' if all(covered[c]==cells[c] for c in providers) and len(outcomes)==sum(key in measured for key in grades) else 'partial_or_unavailable')


def write(directory, analysis):
    write_private(directory/'returned-fields.json',analysis,replace=True)
    rows=[]
    for p in analysis['providers']:
        row={k:p[k] for k in ('configuration_id','measured_results','outcome_results','unmeasured_outcomes','collection_cells','measured_collection_cells','title_present','metadata_present','date_metadata_present')}
        for metric in ('snippet_chars','snippet_words','primary_snippet_chars','additional_snippet_chars','title_chars','title_words','metadata_entries'):
            row.update({metric+'_'+k:v for k,v in p[metric].items()})
        row.update({'snippet_'+s:p['snippet_states'].get(s,0) for s in ('present','empty','missing')})
        rows.append(row)
    for name,records in [('returned-fields.csv',rows),('snippet-length-quality.csv',analysis['length_bands'])]:
        stream=io.StringIO(newline='');writer=csv.DictWriter(stream,fieldnames=list(records[0]) if records else ['configuration_id'])
        writer.writeheader();writer.writerows(records);write_private(directory/name,stream.getvalue().encode(),replace=True)


def snippet_chart(analysis):
    """Two plain, offline views; separate medians are never stacked or added."""
    from ._method.providers import configuration_catalog
    names={p['configuration_id']:p['provider'] for p in configuration_catalog(include_optional=True)}
    esc=lambda v:html.escape(str(v),quote=True)
    number=lambda v:'unavailable' if v is None else f'{v:,.0f}'
    providers=analysis['providers']
    maximum=max((p['snippet_chars']['median'] or 0 for p in providers),default=0)
    unit=10**math.floor(math.log10(maximum)) if maximum>0 else 1
    scale=max(1,math.ceil(maximum/unit)*unit)
    tick=lambda v:f'{v:,.0f}' if v==int(v) else f'{v:,.1f}'
    css='''#returned-fields .snippet-chart-ui{margin:20px 0}
#returned-fields .snippet-chart-ui>label{display:inline-block;padding:7px 12px;margin:0 12px 12px 0;cursor:pointer}
#returned-fields .snippet-chart-ui>input:checked+label{font-weight:700;background:rgba(49,93,213,.10);border-radius:6px}
#returned-fields .snippet-view{display:none}
#returned-fields #returned-snippet-all:checked~.snippet-all,#returned-fields #returned-snippet-main:checked~.snippet-main{display:block}
#returned-fields .snippet-row,#returned-fields .snippet-axis{display:grid;grid-template-columns:140px minmax(0,1fr) 85px;gap:12px;align-items:center}
#returned-fields .snippet-row{margin:12px 0;font-size:14px}
#returned-fields .snippet-provider{overflow-wrap:anywhere}
#returned-fields .snippet-track{height:18px;background:rgba(100,115,140,.10);border-radius:3px;overflow:hidden}
#returned-fields .snippet-fill{height:100%;background:#315dd5}
#returned-fields .snippet-value{text-align:right;font-variant-numeric:tabular-nums}
#returned-fields .snippet-ticks{display:flex;justify-content:space-between;color:#526174;font-size:12px}
#returned-fields .snippet-axis{margin-bottom:16px;font-size:12px;color:#526174}
@media(max-width:600px){#returned-fields .snippet-row,#returned-fields .snippet-axis{grid-template-columns:95px minmax(0,1fr) 75px;gap:8px}}'''
    parts=['<style>'+css+'</style><div class="snippet-chart-ui">',
           '<input type="radio" name="returned-snippet-mode" id="returned-snippet-all" checked><label for="returned-snippet-all">All snippets</label>',
           '<input type="radio" name="returned-snippet-mode" id="returned-snippet-main"><label for="returned-snippet-main">Main snippet only</label>']
    for view,metric in [('all','snippet_chars'),('main','primary_snippet_chars')]:
        label='All snippet text' if view=='all' else 'Main snippet only'
        parts.append(f'<div class="snippet-view snippet-{view}" data-snippet-view="{view}"><div class="snippet-axis"><span>Provider</span><div class="snippet-ticks"><span>0</span><span>{tick(scale/2)}</span><span>{tick(scale)}</span></div><span>Characters</span></div><div role="list" aria-label="{label}: median characters per returned result">')
        ordered=sorted(providers,key=lambda p:(p[metric]['median'] is None,-(p[metric]['median'] or 0),p['configuration_id']))
        for p in ordered:
            cid=p['configuration_id'];name=names.get(cid,cid);value=p[metric]['median']
            width=100*value/scale if value is not None else 0
            accessible=f'{name}: {label.lower()}, median {number(value)} characters'
            parts.append(f'<div class="snippet-row" role="listitem" data-provider="{esc(cid)}" data-length-chars="{value if value is not None else "unavailable"}" aria-label="{esc(accessible)}"><span class="snippet-provider" title="{esc(cid)}">{esc(name)}</span><div class="snippet-track" aria-hidden="true"><div class="snippet-fill" style="width:{width:.4f}%"></div></div><span class="snippet-value">{number(value)}</span></div>')
        parts.append('</div></div>')
    parts.append('</div>')
    return ''.join(parts)


def render_html(analysis):
    esc=lambda x:html.escape(str(x),quote=True)
    num=lambda x:'unavailable' if x is None else f'{x:,.0f}'
    pct=lambda x:'N/A' if x is None else f'{x:.1%}'
    table=lambda headers,rows:'<div class="table"><table><thead><tr>'+''.join('<th>'+esc(h)+'</th>' for h in headers)+'</tr></thead><tbody>'+''.join('<tr>'+''.join('<td>'+esc(v)+'</td>' for v in row)+'</tr>' for row in rows)+'</tbody></table></div>'
    parts=['<section id="returned-fields"><h2>Titles, metadata and snippet length</h2>',
           '<h3>How much snippet text does a typical result contain?</h3>',
           '<p>Bars show the median length: at least half the results are this length or shorter. Providers are sorted longest first in each view. Longer bars mean more text, not better answers.</p>',
           snippet_chart(analysis),
           '<p><strong>All snippets</strong> includes the main snippet plus any additional snippets returned for the same result. <strong>Main snippet only</strong> excludes those extra fields. Characters include spaces; titles and metadata are excluded.</p>',
           f'<p class="note">Based on {analysis["measured_results"]:,} measured original Top 10 returned positions, including repeated URLs. Failed calls and unreturned slots are excluded.</p>']
    if analysis['status']!='complete':parts.append('<p class="note">Measurement coverage is partial or unavailable. Older captures can lack these numeric observations; compare coverage before interpreting providers.</p>')
    parts.append('<details><summary>Exact lengths, percentiles and missing fields</summary>')
    parts.append(f'<p>{analysis["unmeasured_outcomes"]:,} graded/unknown positions lack saved measurements; unavailable measurements are not counted as empty text. Words are whitespace-separated chunks, not model tokens or language-independent linguistic words. This table sorts by combined median snippet length.</p>')
    rows=[]
    for p in analysis['providers']:
        n=p['measured_results'];dist=p['snippet_chars'];states=p['snippet_states']
        rows.append([p['configuration_id'],f'{n:,}',f'{p["measured_collection_cells"]}/{p["collection_cells"]}',num(dist['median']),num(dist['mean']),num(dist['p90']),num(dist['p95']),num(dist['maximum']),num(p['snippet_words']['median']),num(p['primary_snippet_chars']['median']),num(p['additional_snippet_chars']['median']),pct(states.get('missing',0)/n) if n else 'N/A',pct(states.get('empty',0)/n) if n else 'N/A'])
    parts.append(table(['Provider','Measured results','Searches measured / total','Median chars','Mean chars','p90 chars','p95 chars','Max chars','Median words','Primary median chars','Additional median chars','Missing snippet','Empty snippet'],rows))
    parts.append('<p>Median, p90 and p95 use the nearest-rank percentile over measured positions, including zero characters for absent/empty strings (whitespace-only strings retain their raw length). p90 means at least 90% of results are that length or shorter; p95 means at least 95%. Primary and additional medians are computed separately and need not add up to the combined median. Missing text was not supplied; empty text was supplied without usable characters.</p></details>')
    rows=[]
    for p in analysis['providers']:
        n=p['measured_results'];rate=lambda count:pct(count/n) if n else 'N/A'
        rows.append([p['configuration_id'],rate(p['title_present']),num(p['title_chars']['median']),num(p['title_words']['median']),rate(p['metadata_present']),num(p['metadata_entries']['median']),rate(p['date_metadata_present'])])
    parts.append('<details><summary>Title and metadata availability</summary>'+table(['Provider','Nonblank title','Median title chars','Median title words','Native metadata present','Median metadata entries','Date metadata supplied'],rows))
    parts.append('<p>Metadata excludes mechanical rank/position/index and duplicate date aliases. Date metadata supplied is not proof of a valid publication/event date or satisfaction of question requirements.</p></details><details><summary>Snippet length versus own result grade — by provider</summary><p>Length bands are characters. Answer/useful percentages divide by resolved grades only; unknown and ungraded results stay visible. These raw position counts are not deduplicated or family-weighted nDCG. Provider, query and result-rank differences can confound the association. This does not prove that length improves quality.</p>')
    rows=[]
    for p in analysis['providers']:
        for r in analysis['length_bands']:
            if r['configuration_id']!=p['configuration_id']:continue
            rows.append([r['configuration_id'],r['length_band'],r['results'],r['resolved'],r['unknown'],r['ungraded'],pct(r['answer_rate_resolved']),pct(r['useful_rate_resolved'])])
    parts.append(table(['Provider','Snippet characters','Results','Resolved','Unknown','Ungraded','Good answer / resolved','Useful / resolved'],rows))
    parts.append('</details><p>Numeric details: returned-fields.json, returned-fields.csv and snippet-length-quality.csv. No raw title, snippet, metadata values or article text is copied into these aggregate exports.</p></section>')
    return ''.join(parts)
