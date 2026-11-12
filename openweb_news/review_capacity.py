"""Conservative capacity checks and lossless complete-evidence review partitions."""
from copy import deepcopy
from collections import deque
from collections import defaultdict

from .common import asset_path, digest, encoded, load


def rules():
    return load(asset_path('review-capacity-v1.json'))


class CapacityError(ValueError):
    pass


def validate(value, reviewers, mode):
    """Declarations are explicit owner inputs, never guessed model capabilities."""
    if (not isinstance(value, dict) or value.get('profile') != rules()['profile']
            or value.get('kind') != ('fixture' if mode == 'fixture' else 'declared-live')
            or set(value.get('reviewers', {})) != set(reviewers)
            or type(value.get('max_checkpoint_calls')) is not int or value['max_checkpoint_calls'] < 1):
        raise CapacityError('explicit mode-bound reviewer capacities and checkpoint call bound required')
    for role, reviewer in reviewers.items():
        row = value['reviewers'][role]
        fields = ('context_window_tokens', 'max_output_tokens', 'scaffolding_reserve_tokens')
        if (not isinstance(row, dict) or row.get('model') != reviewer['model']
                or not isinstance(row.get('source'), str) or not row['source'].strip()
                or any(type(row.get(k)) is not int or row[k] <= 0 for k in fields)
                or row['context_window_tokens'] <= row['max_output_tokens'] + row['scaffolding_reserve_tokens']):
            raise CapacityError('capacity must bind the exact model, source, window and positive reserves')
    return value


def measure(packet, declaration):
    size = len(encoded(packet))
    budget = (declaration['context_window_tokens'] - declaration['max_output_tokens']
              - declaration['scaffolding_reserve_tokens'])
    minimum_response = 0
    if 'unit_ids' in packet['input']:
        minimum_response = len(encoded(dict(reviewed_unit_ids=packet['input']['unit_ids'],
            decision='continue', material_method_change=False, unexplained_material_differences=[],
            invariant_failures=[], inspection={k:'' for k in packet['input']['required_inspection']})))
    return dict(profile=rules()['measurement'], packet_sha256=digest(packet),
        serialized_utf8_bytes=size, input_budget_units=budget,
        minimum_response_utf8_bytes=minimum_response,
        fits=size <= budget and minimum_response <= declaration['max_output_tokens'],
        declaration=deepcopy(declaration), token_count=None,
        limit='Conservative byte-based planning, not an exact tokenizer or account-access proof.')


def _case_id(case):
    return case['compiled']['packet']['case_id']


def units(source):
    """Group semantic cases; retain every other source path without shortening it."""
    result, used = [], set()
    outcomes, reviews = defaultdict(list), defaultdict(list)
    for j, outcome in enumerate(source.get('report', {}).get('outcomes', [])):
        outcomes[outcome['case_id']].append((j, outcome))
    for j, attempt in enumerate(source.get('review_attempts', [])):
        reviews[attempt['request'].get('case_id')].append((j, attempt))
    for i, case in enumerate(source.get('cases', [])):
        cid = _case_id(case)
        fragments = [dict(path=['cases', i], value=case)]
        fragments.extend(dict(path=['report', 'outcomes', j], value=value) for j, value in outcomes[cid])
        fragments.extend(dict(path=['review_attempts', j], value=value) for j, value in reviews[cid])
        used.update(tuple(f['path']) for f in fragments)
        result.append(dict(unit_id='case:' + cid, kind='case', fragments=fragments))

    prefixes = {p[:i] for p in used for i in range(len(p) + 1)}
    def walk(value, path):
        if tuple(path) in used or path == ['shared_documents']:
            return
        has_used = tuple(path) in prefixes
        if not has_used:
            yield dict(path=path, value=value)
        elif isinstance(value, dict) and value:
            for key in sorted(value):
                yield from walk(value[key], [*path, key])
        elif isinstance(value, list) and value:
            for i, child in enumerate(value):
                own = [*path, i]
                if tuple(own) in used:
                    continue
                yield from walk(child, own)
        else:
            yield dict(path=path, value=value)

    for fragment in walk(source, []):
        result.append(dict(unit_id='field:' + digest(fragment['path']), kind='report', fragments=[fragment]))
    ids = [u['unit_id'] for u in result]
    if len(ids) != len(set(ids)):
        raise CapacityError('checkpoint contains duplicate evidence unit identities')
    for unit in result:
        # Index changes cannot invalidate an otherwise byte-identical old case.
        unit['sha256'] = digest([f['value'] for f in unit['fragments']])
    return result


def documents_for(value, documents):
    selected = {}
    def visit(node):
        if isinstance(node, dict):
            if set(node) == {'shared_document_sha256'}:
                ident = node['shared_document_sha256']
                if ident not in documents or digest(documents[ident]) != ident:
                    raise CapacityError('shared checkpoint document is missing or changed')
                selected[ident] = documents[ident]
            else:
                for child in node.values():
                    visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)
    visit(value)
    return selected


def plan(source, context, declaration, maximum_calls, make_packet, *, previous=None):
    """Plan complete coverage before the first send; never split a semantic unit."""
    documents = source.get('shared_documents', {})
    if any(digest(value) != key for key, value in documents.items()):
        raise CapacityError('shared checkpoint document binding changed')
    current = units(source)
    previous = previous or {}
    old_cases = previous.get('case_fingerprints', {})
    reused = [dict(unit_id=u['unit_id'], sha256=u['sha256'], prior=previous['authority'])
              for u in current if u['kind'] == 'case' and old_cases.get(u['unit_id']) == u['sha256']]
    reuse_ids = {u['unit_id'] for u in reused}
    pending = deque(u for u in current if u['unit_id'] not in reuse_ids)
    def packet_for(batch):
        data = dict(context=context, units=batch, unit_ids=[u['unit_id'] for u in batch],
                    required_inspection=source['required_inspection'])
        data['shared_documents'] = documents_for(data, documents)
        return make_packet(data)
    parts, batch, refined = [], [], []
    while pending:
        unit = pending.popleft()
        packet = packet_for([*batch, unit])
        if not measure(packet, declaration)['fits']:
            if batch:
                parts.append(packet_for(batch))
                batch = []
            packet = packet_for([unit])
            if not measure(packet, declaration)['fits']:
                fragment = unit['fragments'][0]
                value, path = fragment['value'], fragment['path']
                # Reports can be partitioned by complete children. Cases and
                # array rows remain indivisible; strings are never shortened.
                if (unit['kind'] == 'case' or len(unit['fragments']) != 1
                        or path and isinstance(path[-1], int)
                        or not isinstance(value, (dict, list)) or not value):
                    raise CapacityError('one complete checkpoint unit plus context exceeds declared reviewer capacity; no review sent')
                children = value.items() if isinstance(value, dict) else enumerate(value)
                smaller = []
                for key, child in children:
                    f = dict(path=[*path, key], value=child)
                    smaller.append(dict(unit_id='field:' + digest(f['path']), kind='report', fragments=[f],
                                        sha256=digest([child])))
                pending.extendleft(reversed(smaller))
                continue
        batch.append(unit)
        refined.append(unit)
    if batch:
        parts.append(packet_for(batch))
    if not parts or len(parts) > maximum_calls:
        raise CapacityError('complete checkpoint partition exceeds the approved call bound; no review sent')
    result = dict(profile=rules()['profile'], source_sha256=digest(source), context_sha256=digest(context),
        maximum_calls=maximum_calls, declaration=declaration,
        units=[dict(unit_id=u['unit_id'], kind=u['kind'], sha256=u['sha256'], paths=[f['path'] for f in u['fragments']])
               for u in [*refined, *[u for u in current if u['unit_id'] in reuse_ids]]], reused=reused,
        case_fingerprints={u['unit_id']:u['sha256'] for u in current if u['kind'] == 'case'},
        parts=[dict(packet=p, capacity=measure(p, declaration)) for p in parts])
    if digest(restore(result, previous.get('source'))) != digest(source):
        raise CapacityError('checkpoint partition does not preserve its complete source')
    return result


def restore(plan, previous_source=None):
    """Replay source bytes from current parts plus bound certified prior values."""
    fragments, documents, covered = [], {}, []
    expected = {u['unit_id']:u for u in plan['units']}
    for part in plan['parts']:
        data = part['packet']['input']
        documents.update(data['shared_documents'])
        for unit in data['units']:
            ident = unit['unit_id']
            if (ident not in expected or digest([f['value'] for f in unit['fragments']]) != expected[ident]['sha256']
                    or [f['path'] for f in unit['fragments']] != expected[ident]['paths']):
                raise CapacityError('checkpoint part evidence changed')
            covered.append(ident)
            fragments.extend(unit['fragments'])
    old = {u['unit_id']:u for u in units(previous_source)} if previous_source is not None else {}
    if previous_source is not None:
        documents.update(previous_source.get('shared_documents', {}))
    for ref in plan['reused']:
        ident = ref['unit_id']
        if ident not in old or old[ident]['sha256'] != ref['sha256'] or expected[ident]['sha256'] != ref['sha256']:
            raise CapacityError('unchanged-case certificate source differs')
        values = [f['value'] for f in old[ident]['fragments']]
        if len(values) != len(expected[ident]['paths']):
            raise CapacityError('unchanged-case certificate paths differ')
        fragments.extend(dict(path=path, value=value) for path, value in zip(expected[ident]['paths'], values))
        covered.append(ident)
    if len(covered) != len(set(covered)) or set(covered) != set(expected):
        raise CapacityError('checkpoint evidence coverage is incomplete or duplicated')
    result = {}
    for fragment in fragments:
        path, value = fragment['path'], fragment['value']
        if not path:
            result = deepcopy(value)
            continue
        node = result
        for i, key in enumerate(path):
            if isinstance(node, list):
                node.extend([None] * (key + 1 - len(node)))
            if i == len(path) - 1:
                node[key] = deepcopy(value)
            else:
                missing = key not in node if isinstance(node, dict) else node[key] is None
                if missing:
                    node[key] = [] if isinstance(path[i+1], int) else {}
                node = node[key]
    result['shared_documents'] = documents_for({k:v for k,v in result.items() if k != 'shared_documents'}, documents)
    if digest(result) != plan['source_sha256']:
        raise CapacityError('checkpoint partition does not preserve its complete source')
    return result


def validate_part(value, packet):
    ids = value.get(rules()['coverage_field'])
    expected = [u['unit_id'] for u in packet['input']['units']]
    if (not isinstance(ids, list) or any(not isinstance(s, str) for s in ids)
            or packet['input']['unit_ids'] != expected or len(expected) != len(set(expected))
            or len(ids) != len(set(ids)) or set(ids) != set(expected)):
        raise CapacityError('checkpoint verdict does not cover its exact evidence units')


def combine(plan, verdicts):
    """Conjunction of actual scoped verdicts; incomplete coverage cannot pass."""
    if not verdicts:
        raise CapacityError('checkpoint has no independent verdict')
    if len(verdicts) > len(plan['parts']):
        raise CapacityError('unexpected checkpoint verdict count')
    for verdict, part in zip(verdicts, plan['parts']):
        validate_part(verdict, part['packet'])
        if verdict['decision'] == 'continue' and (verdict['material_method_change']
                or verdict['unexplained_material_differences'] or verdict['invariant_failures']):
            raise CapacityError('continuing checkpoint part has unresolved findings')
    complete = len(verdicts) == len(plan['parts'])
    stop = not complete or any(v['decision'] == 'stop' for v in verdicts)
    result = dict(decision='stop' if stop else 'continue',
        material_method_change=any(v['material_method_change'] for v in verdicts),
        unexplained_material_differences=[s for v in verdicts for s in v['unexplained_material_differences']],
        invariant_failures=[s for v in verdicts for s in v['invariant_failures']],
        inspection={key:'\n'.join(f'Part {i}: {v["inspection"][key]}' for i, v in enumerate(verdicts, 1))
                    for key in verdicts[0]['inspection']})
    if not complete:
        result['invariant_failures'].append('Review stopped before complete checkpoint coverage.')
    return result
