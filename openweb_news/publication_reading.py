"""Evidence-bound publication review authority, separate from first-pass decoding."""
from copy import deepcopy
from ._method import evidence

PROFILE = 'scoped-publication-witness-v3'


def fill(decoded, value, source, applications, proofs, comparisons, reviewed_publications, native_publication_fields=None):
    """Fill unresolved publication comparisons only; preserve exact source checks."""
    if decoded['state'] != 'valid':return decoded, []
    result=deepcopy(decoded);judgment=result['judgment'];fills=[]
    for candidate in judgment['candidates']:
        for condition in candidate['conditions']:
            key=(candidate['candidate_id'],condition['predicate_id']);app=applications.get(key)
            if not app or not key[1].startswith('publication:') or condition['status']not in {'unknown','not_demonstrated'}:continue
            rows=comparisons.get(key,[])
            # Unknown coarse periods alone are not affirmative date witnesses.
            witnesses=[r for r in rows if r['status']==app['status'] or
                       (r['status']=='not_demonstrated' and r['precision']=='unresolved' and not r['native']) or
                       app['status']in r.get('alternatives',[])]
            if not witnesses:continue
            proof=next(p for p in proofs if p['application']==app)
            prior=condition['status'];condition['status']=app['status'];condition['support']=deepcopy(proof['support'])
            opposite=any(r['status']in {'pass','fail'} and r['status']!=app['status']for r in rows)
            unparsed=any(r['precision']=='unresolved'for r in witnesses)
            ambiguous=any(r.get('alternatives')for r in witnesses)
            category='semantic_override'if opposite else 'ambiguous_witness'if ambiguous else 'unparsed_witness'if unparsed else 'compatible_coarser'
            fills.append(dict(candidate_id=key[0],predicate_id=key[1],parser_status=prior,
                              reviewed_status=app['status'],kind=category,linked_comparisons=deepcopy(rows)))
        candidate['missing_predicates']=[r['predicate_id']for r in candidate['conditions']if r['status']=='not_demonstrated']
    specs={r['predicate_id']:r for r in source['required_conditions']}
    grades=[evidence.candidate_grade(c,specs)for c in judgment['candidates']]
    grade=2 if 2 in grades else None if None in grades else max(grades,default=0)
    if value['status']!='judged':grade=None
    judgment.update(grade=grade,evidence_class={0:'neither',1:'lead',2:'answer',None:'unknown'}[grade])
    errors,normalized=evidence.validate_judgment(source,judgment,calendar_days=True,reviewed_publications=reviewed_publications,
                                               native_publication_fields=native_publication_fields)
    if errors:return dict(result,state='unjudged',errors=errors,judgment=normalized),fills
    result.update(judgment=normalized,computed_grade=grade,grade_disagreement=value['overall_grade']!=grade)
    return result,fills
