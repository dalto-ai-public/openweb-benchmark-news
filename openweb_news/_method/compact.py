
from pathlib import Path
from copy import deepcopy
from . import evidence as judge
ASSETS = Path(__file__).resolve().parents[1] / 'assets'

def compact_input(packet):
    errors = judge.validate_input(packet)
    if errors:
        raise ValueError('; '.join(errors))
    q = packet['question']
    contract = q['contract']
    required = {r['predicate_id'] for r in packet['required_conditions'] if r['required']}
    requirements = dict(source_rule=deepcopy(contract['source_rule']['qualifying_material']), temporal_requirements=deepcopy(contract['temporal_requirements']))
    for key in ('requested_facets', 'requested_report_target', 'background_context_definition'):
        if contract.get(key):
            requirements[key] = deepcopy(contract[key])
    requirements['background_context_allowed'] = contract['background_context_allowed']
    if 'geography' in required:
        requirements['geography'] = deepcopy(q['geography'])
    if 'language' in required:
        requirements['allowed_snippet_languages'] = deepcopy(contract['allowed_snippet_languages'])
    if 'independence' in required:
        requirements['independence_required'] = True
    if packet['as_of_interpretation']:
        requirements['as_of_requirement'] = deepcopy(packet['as_of_interpretation'])
    dates_apply = any((r.startswith('publication:') for r in required)) or 'as_of_knowledge' in required
    native = [r for r in packet['native_metadata'] if dates_apply or not r['field'].startswith('date:')]
    result = dict(case_id=packet['case_id'], query=q['provider_query'], requirements=requirements, output_predicates=[dict(predicate_id=r['predicate_id'], required=r['required'], scope=r['scope']) for r in packet['required_conditions']], allowed_text=deepcopy(packet['allowed_text']), returned_url=packet['returned_url'], native_metadata=deepcopy(native), native_field_states=deepcopy(packet['native_field_states']), native_record_state=packet['native_record_state'])
    if dates_apply:
        result['date_observations'] = deepcopy(packet['date_observations'])
    return result

def prompt_for(packet):
    required = {r['predicate_id'] for r in packet['required_conditions'] if r['required']}
    parts = [(ASSETS / 'prompt-v3.md').read_text().strip(), 'Return the supplied schema. Each candidate lists every output predicate once; missing_predicates lists its not_demonstrated IDs in order. answer_supported needs title/snippet support; clear_lead needs a concrete clue. Facet IDs identify supported requested facets, not a quota. Background scope does not control lead credit. Source quotes may combine URL publisher identity with item evidence; URL identity alone proves no editorial status.']
    if 'independence' in required:
        parts.append((ASSETS / 'independence-v1.md').read_text().strip())
    if any((r.startswith('publication:') for r in required)):
        parts.append('Publication eligibility requires first-publication meaning in own evidence. Generic publication/update/crawl dates do not establish it. Use reviewed date roles/precision/intervals; do not invent midnight, timezone or a capture-time anchor. Different date roles are not automatically conflicts. Own explicit title/snippet first-publication wording can be quoted for semantic extraction; code checks native intervals.')
    as_of = packet['as_of_interpretation']
    if as_of:
        if as_of['meaning'] in {'historical_state', 'both'}:
            parts.append('Historical state tests what applied at the target time. Later retrospective text may establish it; an earlier event/price alone does not establish continued validity.')
        if as_of['meaning'] in {'historical_knowledge', 'both'}:
            parts.append('Historical knowledge tests public availability of this fact by the cutoff. An explicit earlier public announcement may establish it; a generic article timestamp/current snippet alone does not.')
    return '\n\n'.join(parts) + '\n'
