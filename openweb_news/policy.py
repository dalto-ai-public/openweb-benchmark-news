"""Keep source model proposals separate from structural acceptance.

The sibling has no native policy_applications output field. Unattended agreement
cannot manufacture the own-source review required for a base-grade disagreement.
"""
from copy import deepcopy


def conform(decoded):
    result = dict(policy_conformance="requires_own_source_review",
                  policy_errors=[], conformance_judgment=None)
    if decoded.get("state") != "valid":
        return result
    if decoded.get("judge_overall_grade") == decoded.get("computed_grade"):
        result.update(policy_conformance="same_as_structural_base",
                      conformance_judgment=deepcopy(decoded["judgment"]))
    return result
