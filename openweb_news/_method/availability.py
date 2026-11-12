

def select(config_ids, question_ids, answered, target, tie_order):
    """Preserve collection-summary semantics: remove earliest frozen tie, stop at two."""
    if not config_ids or len(config_ids) != len(set(config_ids)):
        raise ValueError('availability requires distinct configurations')
    if not question_ids or len(question_ids) != len(set(question_ids)):
        raise ValueError('availability requires distinct questions')
    if not 0 < target <= 1:
        raise ValueError('coverage target must be a fraction of released questions')
    if len(tie_order) != len(set(tie_order)) or not set(config_ids) <= set(tie_order):
        raise ValueError('tie order must contain each compared configuration once')
    scope = set(question_ids)
    success = {c: set(answered[c]) & scope for c in config_ids}
    common = set.intersection(*success.values())
    initial_common = set(common)
    comparison, removed = (list(config_ids), [])
    while len(common) / len(question_ids) < target and len(comparison) > 2:
        worst = min(comparison, key=lambda c: (len(success[c]), tie_order.index(c)))
        comparison.remove(worst)
        removed.append(worst)
        common = set.intersection(*(success[c] for c in comparison))
    return (comparison, removed, common, initial_common)
