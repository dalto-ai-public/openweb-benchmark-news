"""One fixed independent review route. Model positions are never averaged."""
from collections import Counter
from copy import deepcopy


def classify(position):
    """Classify source validity separately from supported numeric conformance."""
    state = position.get("state")
    grade = position.get("grade")
    if state == "invalid":
        return "invalid"
    if state != "valid":
        return "unjudged"
    if grade is None:
        return "valid_unknown"
    if type(grade) is not int or grade not in {0, 1, 2}:
        return "invalid"
    if position.get("policy_conformance") not in {"same_as_structural_base", "supported_scoped_policy"}:
        return "nonconforming_numeric"
    return "conforming_numeric"


def numeric(position):
    return position.get("grade") if classify(position) == "conforming_numeric" else None


def needs_third(primary, second):
    a, b = numeric(primary), numeric(second)
    return (a is not None or b is not None) and (a is None or b is None or a != b)


def settle(primary, second, third=None):
    positions = dict(pro=deepcopy(primary), codex=deepcopy(second))
    required = needs_third(primary, second)
    if third is not None:
        if not required:
            raise ValueError("third position is outside the fixed route")
        if third.get("prior_verdict_exposure") is not False or third.get("independent") is not True:
            raise ValueError("third position must be independent and blind")
        positions["claude"] = deepcopy(third)
    for role, position in positions.items():
        if position.get("prior_verdict_exposure") is not False or position.get("independent") is not True:
            raise ValueError(f"{role} position is not independent and blind")
    counts = Counter(numeric(p) for p in positions.values() if numeric(p) is not None)
    matching = [grade for grade, count in counts.items() if count >= 2]
    selected = matching[0] if len(matching) == 1 else None
    roles = [role for role, p in positions.items() if selected is not None and numeric(p) == selected]
    return dict(grade=selected, status="determined" if selected is not None else "unknown",
                reason="two matching valid conforming independent positions" if selected is not None else
                "no two matching valid conforming independent numeric positions",
                selected_roles=roles, states={role: classify(p) for role, p in positions.items()},
                third_required=required, third_obtained=third is not None, positions=positions)
