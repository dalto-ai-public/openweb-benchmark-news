
from __future__ import annotations
from copy import deepcopy
STATUSES = {'pass', 'fail', 'not_demonstrated', 'unknown', 'not_required'}
OUTCOMES = {'judged', 'unjudged', 'refusal', 'error'}

def required_conditions(question: dict) -> list[dict]:
    """Describe checks from the exact contract; no per-result breadth/facet quota.

    A facet list describes requested information across retrieval, not a demand that
    every result establish every facet. Findings name whichever facets they support.
    """
    c = question['contract']
    conditions = []

    def add(ident: str, domain: str, required: bool, pointer: str) -> None:
        conditions.append({'predicate_id': ident, 'domain': domain, 'required': required, 'contract_pointer': pointer})
    add('question_relevance', 'content', True, '/provider_query')
    geo = question['geography']
    restricted = geo['applies_to'] != 'no_geographic_restriction' and any((r.lower() != 'global' for r in geo['regions']))
    add('geography', 'content', restricted, '/geography')
    add('language', 'content', '*' not in c['allowed_snippet_languages'], '/contract/allowed_snippet_languages')
    add('source_rule', 'metadata', True, '/contract/source_rule/qualifying_material')
    add('independence', 'metadata', c['source_rule']['independence_required'], '/contract/source_rule/independence_required')
    publication = False
    for index, req in enumerate(c['temporal_requirements']):
        pointer = f'/contract/temporal_requirements/{index}'
        if req['basis'] == 'publication':
            publication = True
            add(f"publication:{req['id']}", 'metadata', True, pointer)
        else:
            for part in ('kind', 'stage', 'window'):
                add(f"event:{req['id']}:{part}", 'content', True, pointer)
    if not publication:
        add('publication_window', 'metadata', False, '/contract/temporal_requirements')
    add('as_of_availability', 'metadata', c['as_of_requirement'] is not None, '/contract/as_of_requirement')
    return conditions

def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())

def _resolve_support(support: object, texts: dict[str, str], label: str, errors: list[str]) -> list[dict]:
    if not isinstance(support, list):
        errors.append(f'{label}: support must be a list')
        return []
    resolved = []
    for index, item in enumerate(support):
        loc = f'{label}[{index}]'
        if not isinstance(item, dict) or set(item) - {'field', 'quote', 'start', 'end'}:
            errors.append(f'{loc}: expected field/quote and optional offsets')
            continue
        field, quote = (item.get('field'), item.get('quote'))
        if not isinstance(field, str) or field not in texts or (not _text(quote)):
            errors.append(f'{loc}: quote requires an allowed field and nonempty exact text')
            continue
        text = texts[field]
        if 'start' in item or 'end' in item:
            start, end = (item.get('start'), item.get('end'))
            if type(start) is not int or type(end) is not int or (not 0 <= start < end <= len(text)) or (text[start:end] != quote):
                errors.append(f'{loc}: offsets do not identify the exact quote')
                continue
        else:
            start = text.find(quote)
            if start < 0:
                errors.append(f'{loc}: quote does not exactly match allowed text')
                continue
            if text.find(quote, start + 1) >= 0:
                errors.append(f'{loc}: repeated quote needs explicit offsets')
                continue
            end = start + len(quote)
        resolved.append({'field': field, 'quote': quote, 'start': start, 'end': end})
    return resolved

def wire_schema(schema: dict) -> dict:
    """Derive the basic native structured-output schema from the local owner.

    Native model APIs do not enforce our quote checks or contract-dependent rules.
    The full local schema/validator remain authoritative after native output. This
    projection prepares schema bytes; it does not certify provider acceptance.
    """
    omitted = {'$schema', '$id', 'minLength', 'minimum', 'uniqueItems', 'minItems', 'allOf'}

    def project(value: object) -> object:
        if isinstance(value, list):
            return [project(item) for item in value]
        if not isinstance(value, dict):
            return value
        result = {key: project(item) for key, item in value.items() if key not in omitted}
        if 'const' in result:
            result['enum'] = [result.pop('const')]
        if 'enum' in result:
            members = result['enum']
            kinds = {'null' if item is None else 'boolean' if type(item) is bool else 'integer' if type(item) is int else 'number' if type(item) is float else 'string' if isinstance(item, str) else 'object' if isinstance(item, dict) else 'array' for item in members}
            if 'null' in kinds and len(kinds) > 1:
                nonnull = {'enum': [item for item in members if item is not None]}
                result.pop('enum')
                result.pop('type', None)
                result['anyOf'] = [project(nonnull), {'type': 'null'}]
            elif len(kinds) == 1:
                result['type'] = next(iter(kinds))
        return result
    return project(schema)
