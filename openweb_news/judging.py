"""Own-evidence packets and the recovered date-schema/decoder composition."""
from __future__ import annotations

from copy import deepcopy
import json

from .common import asset_path, digest, encoded, load
from .schema import validate as validate_schema
from ._method import evidence, relative_dates, text_dates, two_step
from ._method import judgment_support
from .prompt import prompt_for, native_date_display

METHOD = load(asset_path("method.json"))


def native_policy_fields(config):
    """Selected owner admission replaces the legacy named-field allowlist."""
    selected = None
    if "date_trust" in config:
        from .date_trust import eligible_fields
        selected = eligible_fields(config)
    if config.get("judge_input_profile") and selected is None:
        raise ValueError("selected judge input profile requires explicit date trust admission")
    if selected is not None:
        return selected
    field = METHOD["publication_estimate_named_fields"].get(config["configuration_id"])
    return [field] if field else []


def applicability_fields(app):
    return app.get("field_paths", [app.get("field_path")]) if app else []


def native_semantics(config, field):
    baseline = METHOD["native_date_semantics"].get(config["configuration_id"], {}).get(field, ["unclear", "unspecified"])
    if config.get("judge_input_profile"):
        from .date_trust import field_semantics
        return field_semantics(config, field) or baseline
    return baseline


def packet_for(question, config, position, case_id):
    """Allowlist fields from this occurrence; never include provider/rank or prior grades."""
    allowed, states = [], {}
    for name, field in [("title", position.get("title", {})),
                        ("snippet", position.get("content", {})),
                        *[(f"additional_snippet_{i}", value)
                          for i, value in enumerate(position.get("additional_content", {}).values())]]:
        states[name] = field.get("state", "missing")
        value = field.get("value")
        if isinstance(value, str):
            allowed.append(dict(field=name, text=value))
        elif isinstance(value, list):
            allowed.extend(dict(field=f"{name}[{i}]", text=text)
                           for i, text in enumerate(value) if isinstance(text, str))
    observations = []
    for row in position.get("native_dates", {}).get("observations", []):
        kind, derivation = native_semantics(config, row["relative_path"])
        observations.append(dict(field_path=row["path"], relative_path=row["relative_path"],
                                 raw_value=deepcopy(row["value"]), state=row["state"],
                                 date_kind=kind, derivation=derivation,
                                 mapping_version=METHOD["version"],
                                 mapping_id=digest(dict(configuration=config, field=row["relative_path"], kind=kind, derivation=derivation)),
                                 **evidence.date_interval(row["value"])))
    item = dict(question_id=question["question_id"], question_revision=question["revision"],
                evidence_id=case_id, allowed_text=allowed, native_field_states=states,
                native_record_state=position.get("status", "non_object_result"))
    metadata = dict(evidence_id=case_id, observed_url=position.get("url", {"state": "missing"}),
                    observed_metadata=deepcopy(position.get("metadata", {})))
    return evidence.build_input(question, item, metadata, observations)


def replay_mentions(packet, supplied):
    """Recovered decoder: only an exact full canonical table can resolve display mismatch."""
    try:
        return text_dates.replay_mentions(packet, supplied)
    except ValueError as error:
        if str(error) != "saved text date display differs":
            raise
        canonical = text_dates.mentions(packet)
        calendar = deepcopy(canonical)
        for row in calendar:
            row["calendar_comparison_interval"] = evidence.comparison_interval(row, True)
        if supplied != canonical and supplied != calendar:
            raise ValueError("saved display mismatch is not exact canonical full-field table") from error
        return deepcopy(canonical if supplied == canonical else calendar)


def native_publication_fields(compiled):
    """Private source IDs admitted by the compiled receipt, never model assertions."""
    if not compiled.get('judge_input_profile'):
        return None
    app=compiled['input'].get('publication_estimate_applicability')
    if app is None:
        return ()
    from .date_trust import PROFILE as TRUST_PROFILE
    trust=app.get('date_trust') or {}
    fields=trust.get('eligible_fields')
    if (trust.get('profile') != TRUST_PROFILE or trust.get('state') != 'selected'
            or not isinstance(fields,list) or fields != applicability_fields(app)):
        raise ValueError('compiled native publication admission differs from private receipt')
    return tuple(row['field'] for row in compiled['packet']['date_observations'] if row['relative_path'] in fields)


def _decoded(packet, verdict, data, *, date_profile=None, native_publication_fields=None):
    if date_profile is not None:
        from .date_reading import Reader, PROFILES, COMPARISON_PROFILES
        if date_profile not in PROFILES:raise ValueError('unknown date-reading profile')
        reader=Reader(captured_at=data.get('captured_at'),profile=date_profile)
        result=two_step.decode(packet, verdict, calendar_days=True, text_dates=data['text_dates'], replay_fn=reader.replay, date_reader=reader,
                               native_publication_fields=native_publication_fields)
        result.update(date_replay_version=date_profile,date_reading_profile=date_profile,date_readings=reader.readings)
        if date_profile in COMPARISON_PROFILES:
            result['date_comparisons']=[dict(candidate_id=cid,predicate_id=predicate,rows=rows)for (cid,predicate),rows in reader.comparisons.items()]
        return result
    return two_step.decode(packet, verdict, calendar_days=True, text_dates=data["text_dates"], replay_fn=replay_mentions,
                           native_publication_fields=native_publication_fields)


def compile_packet(packet, captured_at, *, config=None):
    errors = evidence.validate_input(packet)
    if errors:
        raise ValueError("; ".join(errors))
    data = relative_dates.input_for(packet, captured_at, calendar_days=True)
    data.update(complete_contract=deepcopy(packet["question"]["contract"]),
                complete_question=deepcopy(packet["question"]), captured_at=captured_at,
                captured_at_role="collection_provenance_only",
                native_date_display=native_date_display(packet))
    applicability = None
    if config:
        fields = native_policy_fields(config)
        if fields:
            field = fields[0]
            applicability = dict(configuration_sha256=digest(config), field_path=field,
                                 mapping_id=digest(dict(configuration=config, field=field)),
                                 reviewer="bundled-reviewed-native-field-mapping",
                                 reason="The versioned native mapping and publication-estimate policy bind this exact configuration and field.",
                                 source_bindings=[dict(asset=name, sha256=digest(asset_path(name).read_bytes()))
                                                  for name in ("method.json", "policies.json")],
                                 policy="publication-estimate-policy-v2.json",
                                 state="named_field_eligible_for_own_occurrence_review_not_automatic_pass")
            if config.get("date_trust") is not None:
                applicability.pop("field_path")
                applicability.update(field_paths=deepcopy(fields), date_trust=deepcopy(config["date_trust"]),
                    mapping_id=digest(dict(configuration=config, fields=fields)),
                    reviewer="owner-receipt-bound-native-field-admission",
                    reason="The owner receipt admits only these exact fields to existing occurrence review; native meanings remain unchanged.")
    data["publication_estimate_applicability"] = applicability
    schema = two_step.schema_for(packet)
    fields = list(dict.fromkeys(row["field"] for row in packet["allowed_text"]))
    own = [r["date_id"] for r in replay_mentions(packet, data["text_dates"])]
    native = [f"N{i+1}" for i, _ in enumerate(packet["date_observations"])]
    if [row["date_id"] for row in data["native_date_ids"]] != native:
        raise ValueError("native date ID table differs")
    # Detach fact citations from the shared eligibility/date quote schema.
    # Native metadata stays available in its dedicated condition/date fields.
    candidate = schema["properties"]["candidates"]["items"]["properties"]
    candidate["support"] = deepcopy(candidate["support"])
    candidate["support"]["items"]["properties"]["field"] = dict(type="string", enum=[*fields, "url"])
    # Same detached date restrictions as the recovered v2 refinement.
    links = schema["properties"]["dates"]["items"]["properties"]
    for key in ("context_support", "additional_dates"):
        detached = deepcopy(links[key])
        detached["items"]["properties"]["field"] = dict(type="string", **({"enum": fields} if fields else {}))
        if not fields:
            detached["maxItems"] = 0
        links[key] = detached
    links["anchor_date_id"] = dict(type="string", enum=["", *own])
    ids = own + native
    links["date_ids"] = dict(type="array", items=dict(type="string", **({"enum": ids} if ids else {})),
                             **({} if ids else {"maxItems": 0}))
    prompt = prompt_for(packet)
    compiled = dict(version=METHOD["version"], packet=deepcopy(packet), input=data, prompt=prompt,
                schema=schema, packet_sha256=digest(packet), input_sha256=digest(data),
                schema_sha256=digest(schema), prompt_sha256=digest(prompt.encode()),
                schema_profile=METHOD["schema_profile"], decoder_profile=METHOD["decoder_profile"])
    profile = (config or {}).get("judge_input_profile")
    if profile is not None:
        from .blind_input import LIVE_PROFILE, project
        if profile != LIVE_PROFILE:
            raise ValueError("unsupported judge input profile")
        compiled["judge_input_profile"] = profile
        compiled["model_projection"] = project(_source_model_packet(compiled), source_packet=packet, profile=profile)
    return compiled


def verify_compiled(compiled):
    for key in ("packet", "input", "schema"):
        if digest(compiled[key]) != compiled[key + "_sha256"]:
            raise ValueError(f"compiled {key} binding differs")
    if digest(compiled["prompt"].encode()) != compiled["prompt_sha256"]:
        raise ValueError("compiled prompt binding differs")
    if compiled["decoder_profile"] != METHOD["decoder_profile"] or compiled["schema_profile"] != METHOD["schema_profile"]:
        raise ValueError("compiled method profile differs")
    profile = compiled.get("judge_input_profile")
    if profile is None:
        if "model_projection" in compiled:
            raise ValueError("unselected model projection")
    else:
        from .blind_input import LIVE_PROFILE, project
        if profile != LIVE_PROFILE:
            raise ValueError("unsupported judge input profile")
        if compiled.get("model_projection") != project(_source_model_packet(compiled), source_packet=compiled["packet"], profile=profile):
            raise ValueError("compiled model projection binding differs")


def _source_model_packet(compiled):
    return {key: deepcopy(compiled[key]) for key in
            ("version", "input", "prompt", "schema", "packet_sha256", "input_sha256",
             "schema_sha256", "prompt_sha256", "schema_profile", "decoder_profile")}


def model_packet(compiled, packet):
    """Derive a review view from its authentic private packet."""
    if not compiled.get("judge_input_profile"):
        return packet
    from .blind_input import project
    projected = project(packet, source_packet=compiled["packet"], profile=compiled["judge_input_profile"])["packet"]
    # Review contract versions and input-display versions have separate owners.
    projected["profile"] = packet["profile"]
    projected["judge_input_profile"] = compiled["judge_input_profile"]
    return projected


def restore_model_verdict(value, compiled, source_model_packet=None):
    if not compiled.get("judge_input_profile"):
        return deepcopy(value)
    from .blind_input import project, restore_verdict
    projection = (compiled["model_projection"] if source_model_packet is None else
                  project(source_model_packet, source_packet=compiled["packet"], profile=compiled["judge_input_profile"]))
    return restore_verdict(value, projection["binding"], expected_binding_sha256=projection["binding_sha256"])


def blind_packet(compiled):
    verify_compiled(compiled)
    return deepcopy(compiled["model_projection"]["packet"]) if compiled.get("judge_input_profile") else _source_model_packet(compiled)


def build_pro_request(compiled):
    model = blind_packet(compiled)
    body = dict(systemInstruction=dict(parts=[dict(text=model["prompt"])]),
                contents=[dict(role="user", parts=[dict(text=json.dumps(model["input"], ensure_ascii=False,
                                                                      sort_keys=True, separators=(",", ":")))])],
                generationConfig={**deepcopy(METHOD["primary"]["settings"]),
                                  "responseFormat": dict(text=dict(mimeType="APPLICATION_JSON",
                                                                   schema=judgment_support.wire_schema(model["schema"])))})
    return dict(method="POST", endpoint=METHOD["primary"]["endpoint"], body=body,
                model=METHOD["primary"]["model"], body_sha256=digest(body),
                estimated_input_tokens_chars_div4=(len(encoded(body)) + 3) // 4)


def decode_position(value, compiled, *, date_profile=None):
    verify_compiled(compiled)
    raw = deepcopy(value)
    try:
        value = restore_model_verdict(value, compiled)
    except (ValueError, KeyError, TypeError) as error:
        return dict(state="invalid", errors=[str(error)], raw_verdict=raw, grade=None,
                    judge_input_profile=compiled.get("judge_input_profile"))
    errors = validate_schema(value, compiled["schema"])
    if errors:
        return dict(state="invalid", errors=errors, raw_verdict=raw, grade=None)
    decoded = _decoded(compiled["packet"], value, compiled["input"], date_profile=date_profile,
                       native_publication_fields=native_publication_fields(compiled))
    from .policy import conform
    conformance = conform(decoded)
    attribution = {} if not compiled.get("judge_input_profile") else dict(
        judge_input_profile=compiled["judge_input_profile"], decoder_verdict=deepcopy(value),
        model_input_sha256=compiled["model_projection"]["packet"]["input_sha256"],
        model_binding_sha256=compiled["model_projection"]["binding_sha256"])
    return dict(decoded, **conformance, **attribution, state="invalid" if decoded.get("errors") else decoded.get("state"), raw_verdict=raw,
                structural_base_grade=decoded.get("computed_grade"),
                proposal_grade=value.get("overall_grade"),
                grade=value.get("overall_grade") if decoded.get("state") == "valid" else None)


def response_result(raw, compiled, *, http_status=200, complete=True):
    retained = dict(raw_sha256=digest(raw), http_status=http_status, complete=complete)
    native_complete = False
    try:
        if http_status != 200 or not complete:
            raise ValueError("primary transport failed or incomplete")
        value = json.loads(raw)
        retained["metadata"] = {k: value.get(k) for k in ("modelVersion", "responseId", "usageMetadata", "promptFeedback")}
        if value.get("modelVersion") != METHOD["primary"]["model"]:
            raise ValueError("primary model differs from exact selected model")
        candidates = value.get("candidates", [])
        if value.get("promptFeedback", {}).get("blockReason") or len(candidates) != 1:
            raise ValueError("blocked or missing/extra primary candidate")
        row = candidates[0]
        parts = row.get("content", {}).get("parts", [])
        if row.get("finishReason") != "STOP" or not parts:
            raise ValueError("incomplete primary output")
        if any(not isinstance(p, dict) or set(p) - {"text", "thought", "thoughtSignature"} for p in parts):
            raise ValueError("unexpected primary non-text output")
        native_complete = True
        text = "".join(p["text"] for p in parts if isinstance(p.get("text"), str) and not p.get("thought"))
        return dict(retained, **decode_position(json.loads(text), compiled))
    except (ValueError, TypeError, KeyError, AttributeError, UnicodeError) as error:
        return dict(retained, state="invalid" if native_complete else "unjudged", grade=None, errors=[str(error)])
