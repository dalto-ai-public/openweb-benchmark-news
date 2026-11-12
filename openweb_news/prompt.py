"""Source-aligned domain prompt; bundled assets replace development-only replay."""
from copy import deepcopy
import json
from types import SimpleNamespace

from .common import asset_path, load
from ._method import two_step as two, relative_dates as relative

company = SimpleNamespace(INSTRUCTIONS=asset_path("company-geography-instructions.md").read_text())

def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode()

def policy_sections(profile=None):
    policies = load(asset_path("policies.json"))["policies"]
    if profile is None:
        return policies
    display = load(asset_path("blind-policy-display-v1.json"))
    if display["profile"] != profile:
        raise ValueError("unknown model policy display profile")
    overrides = display["policy_overrides"]
    if set(overrides) - {row["policy"] for row in policies}:
        raise ValueError("model policy display references unknown policy")
    for row in policies:
        for key, value in overrides.get(row["policy"], {}).items():
            if key != "rules" or set(value) - set(row["rules"]):
                raise ValueError("model policy display changes policy structure")
            row["rules"].update(deepcopy(value))
    return policies


def model_display(prompt, profile):
    """Versioned display of existing rules; private source owners stay intact."""
    display = load(asset_path("blind-policy-display-v1.json"))
    if display["profile"] != profile:
        raise ValueError("unknown model policy display profile")
    original = encoded(policy_sections()).decode()
    if original in prompt:
        prompt = prompt.replace(original, encoded(policy_sections(profile)).decode())
    for row in display.get("prompt_replacements", []):
        prompt = prompt.replace(row["source"], row["display"])
    return prompt

def calibrated_format_rules():
    starts = ("Output completeness rules:", "Field separation:", "Final literal-evidence check:")
    rows = [p.strip() for p in asset_path("output-rules.md").read_text().split("\n\n")
            if p.strip().startswith(starts)]
    if len(rows) != len(starts):
        raise ValueError("bundled calibrated output rules differ")
    return rows, None

def prompt_for(packet):
    rules, _ = calibrated_format_rules()
    bridge = (
        'The following source-bound current policies extend the full core above. '
        'Effective-date policy A and own-publication-context policy B take their '
        'explicit stated precedence. Preserve native field roles and values; '
        'record inferred meaning in own-text date links/context, never by rewriting '
        'native dates. Publication-estimate inference applies only when the input '
        'explicitly supplies an independently reviewed exact configuration/field '
        'applicability binding; an old provider name or mapping is insufficient. '
        'Your overall_grade is the current-policy proposal. Code retains the '
        'structural/base grade separately; disagreement requires own-source review '
        'and is never an automatic favorable override. Every candidate must satisfy '
        'all qualifying_predicates in complete_contract; these define '
        'question_relevance even when they are not separate output predicate IDs. '
        'Requested facets describe explicitly requested information and are not '
        'a completeness quota per result. A condition beginning if-present must '
        'not reject an otherwise qualifying finding when it is absent. Each '
        'finding must be established by its own returned evidence. Capture time '
        'is collection provenance, never publication/event proof. Native date '
        'display preserves declared meaning including unclear or mixed roles.'
    )
    baseline = '\n\n'.join([two.prompt_for(packet).strip(), *rules,
                           relative.INSTRUCTIONS, company.INSTRUCTIONS, bridge,
                           'Current policy rule owners:\n' + encoded(policy_sections()).decode()]) + '\n'
    return baseline.rstrip() + '\n\n' + asset_path('lead-flag-clarification-v1.md').read_text().strip() + '\n'

def native_date_display(packet):
    metadata = {r['field']: r for r in packet['native_metadata']}
    return [dict(date_id=f'N{i+1}', source_field=row.get('field'),
                 source_path=row.get('field_path', row.get('relative_path')),
                 raw_value=deepcopy(row.get('raw_value')),
                 declared_meaning=row['date_kind'], derivation=row['derivation'],
                 mapping_version=row['mapping_version'],
                 native_metadata=deepcopy(metadata.get(row.get('field'))),
                 observation=deepcopy(row))
            for i, row in enumerate(packet['date_observations'])]
