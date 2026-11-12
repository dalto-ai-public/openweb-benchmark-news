"""Typed corroboration of existing publication policy; no new grading rule."""
from copy import deepcopy

from .common import asset_path, digest, load
from .judging import verify_compiled, METHOD, model_packet, restore_model_verdict, native_policy_fields, applicability_fields, native_semantics
from .review import classify
from .schema import validate
from ._method import evidence, text_dates

PROFILE = "publication-policy-adjudication-v1"
PROOF_PROFILE = "publication-policy-adjudication-v2"
POLICY = "publication-estimate-policy-v2.json"
CHECKS = ("fixed_article", "native_applicability", "no_material_conflict", "window_and_cutoff", "other_requirements")
FORBIDDEN = {"prior_labels", "prior_reviews", "current_review", "native_verdict", "positions",
             "proposal_grade", "structural_base_grade", "original_rank", "configuration_id",
             "configuration_sha256", "aggregate_rankings"}


def reject_exposure(value):
    if isinstance(value, dict):
        if FORBIDDEN & value.keys():
            raise ValueError("prior verdict or configuration identity in blind review")
        for item in value.values():
            reject_exposure(item)
    elif isinstance(value, list):
        for item in value:
            reject_exposure(item)


def eligible(compiled, positions, config, *, date_profile=None):
    """A narrow triage predicate, never an approval of the policy application."""
    verify_compiled(compiled)
    app = compiled["input"].get("publication_estimate_applicability")
    if not app or app.get("policy") != POLICY:
        return []
    fields = native_policy_fields(config)
    expected_bindings = [dict(asset=name, sha256=digest(asset_path(name).read_bytes())) for name in ("method.json", "policies.json")]
    mapping = dict(configuration=config, fields=fields) if config.get("date_trust") is not None else dict(configuration=config, field=fields[0] if fields else None)
    if (not fields or applicability_fields(app) != fields or app.get("configuration_sha256") != digest(config)
            or app.get("mapping_id") != digest(mapping)
            or app.get("source_bindings") != expected_bindings):
        return []
    requirements = compiled["packet"]["question"]["contract"]["temporal_requirements"]
    native = {r["date_id"]: r for r in compiled["input"]["native_date_ids"]}
    for row in native.values():
        kind, derivation = native_semantics(config, row["relative_path"])
        if (row.get("mapping_id") != digest(dict(configuration=config, field=row["relative_path"], kind=kind, derivation=derivation))
                or row.get("date_kind") != kind or row.get("derivation") != derivation
                or row.get("mapping_version") != METHOD["version"]):
            return []
    from .date_reading import native_reading_rows
    from .judging import native_publication_fields
    native = {r["date_id"]: r for r in native_reading_rows(
        list(native.values()), native_publication_fields(compiled), profile=date_profile)}
    selected_by_role = []
    for role in ("pro", "codex"):
        pos = positions.get(role, {})
        if (classify(pos) != "nonconforming_numeric" or pos.get("proposal_grade") != 2
                or pos.get("structural_base_grade") != 1):
            return []
        gaps = [r for c in (pos.get("judgment") or {}).get("candidates", []) for r in c["conditions"]
                if r["status"] not in {"pass", "not_required"}]
        if not gaps or any(not r["predicate_id"].startswith("publication:") or r["status"] != "not_demonstrated" for r in gaps):
            return []
        selected = set()
        for link in pos.get("decoder_verdict", pos["raw_verdict"])["dates"]:
            req = next((r for r in requirements if r["basis"] == "publication" and "publication:" + r["id"] == link["predicate_id"]), None)
            if req:
                for ident in link["date_ids"]:
                    row = native.get(ident)
                    if (row and row.get("relative_path") in fields
                            and evidence.compare_interval(dict(row, date_kind="first_publication"), req, True) == "pass"):
                        selected.add(ident)
        selected_by_role.append(selected)
    return sorted(set.intersection(*selected_by_role))


def mapping_proof(compiled, eligible_ids):
    """Project already checked mapping bindings without the provider identity.

    The compiler's two hashes have different scopes. Neither hash is an article
    date, and equality between the two is not an applicability requirement.
    """
    app = compiled["input"]["publication_estimate_applicability"]
    native = {r["date_id"]: r for r in compiled["input"]["native_date_ids"]}
    bindings = [dict(asset=name, sha256=digest(asset_path(name).read_bytes()))
                for name in ("method.json", "policies.json")]
    if app["source_bindings"] != bindings or not eligible_ids:
        raise ValueError("mapping proof requires bound portable policy assets")
    rows = []
    for ident in eligible_ids:
        row = native[ident]
        if row["relative_path"] not in applicability_fields(app):
            raise ValueError("native observation is outside the applicable field")
        rows.append(dict(date_id=ident, relative_path=row["relative_path"],
                         observation_mapping_id=row["mapping_id"],
                         date_kind=row["date_kind"], derivation=row["derivation"],
                         mapping_version=row["mapping_version"]))
    return dict(profile="portable-native-mapping-proof-v1", verified_by="deterministic eligibility and canonical compiler replay",
                applicability_mapping_id=app["mapping_id"], field_path=app.get("field_path"),
                applicability_id_scope="configuration plus named policy-eligible field",
                observation_id_scope="configuration plus field plus native date kind and derivation",
                relationship="Different scopes intentionally have different hashes; each observation below binds the same named field.",
                portable_authority=bindings,
                source_authority="Packaged method.json and policies.json own this standalone mapping. Upstream filenames and hashes are not runtime authority.",
                observations=rows,
                limit="Field eligibility only. Fixed-item applicability, conflicts, credibility and every answer requirement still need own-evidence review.")


def packet_for(compiled, eligible_ids, *, profile=PROOF_PROFILE, _source=False):
    """Same general task contract for both backends and every eligible occurrence."""
    data = deepcopy(compiled["input"])
    # Preserve source-bound field applicability, without revealing a provider id/hash.
    data["publication_estimate_applicability"].pop("configuration_sha256", None)
    data["existing_policy"] = next(x for x in load(asset_path("policies.json"))["policies"] if x["policy"] == POLICY)
    data["eligible_native_date_ids"] = eligible_ids
    data["review_checks"] = list(CHECKS)
    if profile == PROOF_PROFILE:
        data["native_mapping_proof"] = mapping_proof(compiled, eligible_ids)
    elif profile != PROFILE:
        raise ValueError("unknown adjudication contract")
    if not compiled.get("judge_input_profile"):
        reject_exposure(data)
    def obj(props):
        return dict(type="object", properties=props, required=list(props), additionalProperties=False)
    string = lambda values=None: dict(type="string", **({"enum": values} if values else {}))
    fields = [r["field"] for r in data["allowed_text"]]
    quote = obj(dict(field=string(fields), quote=string(), occurrence=dict(type="integer", minimum=1)))
    check = obj(dict(check=string(list(CHECKS)), status=string(["pass", "fail", "unknown"]), reason=string(),
                     support=dict(type="array", items=quote)))
    schema = obj(dict(disposition=string(["confirm", "reject", "unknown", "rule_question"]),
                      selected_native_date_ids=dict(type="array", items=string(eligible_ids)),
                      checks=dict(type="array", items=check), contrary_reading=string(), reason=string()))
    prompt = asset_path("adjudication-v2.md" if profile == PROOF_PROFILE else "adjudication-v1.md").read_text()
    packet = dict(input=data, schema=schema, prompt=prompt, profile=profile,
                packet_sha256=compiled["packet_sha256"], input_sha256=digest(data),
                schema_sha256=digest(schema), prompt_sha256=digest(prompt.encode()))
    if _source:
        return packet
    result = model_packet(compiled, packet)
    reject_exposure(result["input"])
    return result


def validate_disposition(value, packet):
    errors = validate(value, packet["schema"])
    spans = []
    if errors:
        return dict(accepted=False, errors=errors, spans=[])
    checks = value["checks"]
    if len(checks) != len(CHECKS) or {r["check"] for r in checks} != set(CHECKS):
        errors.append("every review check must occur exactly once")
    texts = {r["field"]: r["text"] for r in packet["input"]["allowed_text"]}
    for check in checks:
        if not check["reason"].strip():
            errors.append("check reason missing")
        if check["check"] in {"fixed_article", "other_requirements"} and not check["support"]:
            errors.append("affirmative own-text support missing")
        for quote in check["support"]:
            try:
                spans.append(dict(check=check["check"], field=quote["field"], **text_dates.exact_span(
                    texts[quote["field"]], quote["quote"], quote["occurrence"])))
            except (KeyError, ValueError):
                errors.append("invalid own-text span")
    if not value["reason"].strip() or not value["contrary_reading"].strip():
        errors.append("disposition reasoning or contrary reading missing")
    if value["disposition"] == "confirm":
        if not value["selected_native_date_ids"] or any(r["status"] != "pass" for r in checks):
            errors.append("confirmation requires all checks and a selected eligible date")
    return dict(accepted=not errors and value["disposition"] == "confirm", errors=errors, spans=spans)


def corroborate(compiled, original_positions, packet, responses, config, *, date_profile=None):
    """Produce attributable effective positions while retaining source positions."""
    ids = eligible(compiled, original_positions, config, date_profile=date_profile)
    if not ids or packet != packet_for(compiled, ids, profile=packet.get("profile")):
        raise ValueError("policy eligibility or blind input binding changed")
    source_packet = packet_for(compiled, ids, profile=packet.get("profile"), _source=True)
    audits = {}
    decoded_responses = {}
    for role, response in responses.items():
        try:
            value = restore_model_verdict(response.get("verdict"), compiled, source_packet)
            audits[role] = validate_disposition(value, source_packet)
            decoded_responses[role] = value
            if compiled.get("judge_input_profile"):
                audits[role].update(raw_verdict=deepcopy(response.get("verdict")), decoder_verdict=deepcopy(value),
                                    judge_input_profile=compiled["judge_input_profile"])
        except (ValueError, KeyError, TypeError) as error:
            audits[role] = dict(accepted=False, errors=[str(error)], spans=[])
    accepted = (set(responses) == {"codex", "claude"} and all(a["accepted"] for a in audits.values())
                and all(r.get("transport_success") is True and r.get("native_completion_verified") is True for r in responses.values()))
    positions = deepcopy(original_positions)
    record = dict(profile=packet["profile"], policy=POLICY, packet_sha256=compiled["packet_sha256"],
                  input_sha256=packet["input_sha256"], accepted=accepted, audits=audits,
                  dispositions={role:(r or{}).get('disposition')for role,r in decoded_responses.items()},
                  reasons={role:(r or{}).get('reason')for role,r in decoded_responses.items()},
                  original_judgments={role:digest(p) for role,p in original_positions.items()},
                  review_response_hashes={role:digest(r) for role,r in responses.items()})
    if accepted:
        for role in ("pro", "codex"):
            positions[role].update(policy_conformance="supported_scoped_policy",
                                   conformance_judgment=deepcopy(positions[role]["judgment"]),
                                   reviewed_policy_application=digest(record))
    return positions, record
