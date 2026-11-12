"""Versioned model projection with private source and structural alias bindings.

Only known structured identity/noise fields are omitted. Evidence text, URLs,
contracts and policy wording are never sanitized. Unsupported metadata shapes
fail preparation, leaving the source untouched. Private bindings must stay out of
model context. This helper does not establish policy authority or validate grades.
"""
from copy import deepcopy
import json

from .common import digest
from ._method.text_dates import exact_span
from .prompt import native_date_display, model_display, policy_sections
from .schema import validate


PROFILE = "blind-own-evidence-projection-candidate-v1"
LIVE_PROFILE = "blind-own-evidence-projection-v1"
MODEL_CASE = "own-result"
_NOISE = {"global_rank", "score", "position", "rank", "price"}
_SOURCE = {"source", "type", "meta_url", "news", "properties"}
_INPUT_FIELDS = {
    "case_id", "query", "requirements", "output_predicates", "allowed_text",
    "returned_url", "native_metadata", "native_field_states", "native_record_state",
    "text_dates", "date_observations", "date_checks", "semantic_checks",
    "native_date_ids", "date_comparison_policy", "input_revision",
    "native_date_estimates", "complete_contract", "complete_question", "captured_at",
    "captured_at_role", "native_date_display", "publication_estimate_applicability",
    "existing_policy", "existing_policies", "eligible_native_date_ids",
    "review_checks", "native_mapping_proof",
}
_QUESTION_FIELDS = {"provider_query", "geography", "contract"}
_QUESTION_PRIVATE = {
    "question_id", "revision", "family_id", "need_id", "provenance", "history",
}
_DATE_IDENTITY = {"field_path", "relative_path", "mapping_id", "mapping_version"}
_DATE_SEMANTICS = {
    "raw_value", "state", "date_kind", "derivation", "interpretation_state",
    "precision", "timezone", "timezone_state", "interval",
    "calendar_comparison_interval",
}
_URL_DISPLAY = {
    "scheme": "source_scheme_display", "hostname": "source_hostname_display",
    "netloc": "source_netloc_display", "path": "source_navigation_display",
}


def _unsupported(where):
    raise ValueError("unsupported-source-shape: " + where)


def _known_keys(value, allowed, where):
    if not isinstance(value, dict) or set(value) - allowed:
        _unsupported(where)


def _metadata_text(row, dates):
    """Keep exact serialized evidence, or fail; never drop unknown source leaves."""
    _known_keys(row, {"field", "text", "role"}, "native metadata row")
    if (set(row) != {"field", "text", "role"} or not isinstance(row["field"], str)
            or not isinstance(row["text"], str) or row["role"] != "eligibility_only"):
        _unsupported("native metadata field/text/role")
    field = row.get("field", "")
    if field.startswith("date:"):
        if field not in {d["field"] for d in dates}:
            _unsupported("metadata date reference")
        return True
    if not field.startswith("native:"):
        _unsupported("metadata field " + field)
    name = field[len("native:"):]
    try:
        value = json.loads(row["text"])
    except (ValueError, TypeError):
        _unsupported("metadata JSON " + field)
    if name in _NOISE:
        if isinstance(value, (dict, list)):
            _unsupported("nested noise metadata " + field)
        return False
    if name not in _SOURCE and name not in {d["relative_path"] for d in dates}:
        _unsupported("unmapped metadata field " + field)
    if name == "properties" and isinstance(value, dict) and value and set(value) <= {"fetched_at", "published_date"}:
        # Omit only a duplicate container. Every exact leaf must already have
        # its own retained native observation; this grants no date role/trust.
        for key, leaf in value.items():
            matches = [d for d in dates if d["relative_path"] == "properties." + key]
            if ((leaf is not None and not isinstance(leaf, str)) or len(matches) != 1
                    or type(matches[0]["raw_value"]) is not type(leaf)
                    or matches[0]["raw_value"] != leaf):
                _unsupported("properties date leaf not retained exactly: " + key)
        return False
    if isinstance(value, (dict, list)):
        if name == "meta_url" and isinstance(value, dict):
            if set(value) != set(_URL_DISPLAY) | {"favicon"} or any(type(v) is not str for v in value.values()):
                _unsupported("nested metadata " + field)
            return True
        # The native Seltz title is also mapped as own title text. Preserve this
        # exact source object only when every leaf has this reviewed meaning.
        if not (name == "properties" and isinstance(value, dict)
                and set(value) == {"title"}
                and (value["title"] is None or isinstance(value["title"], str))):
            _unsupported("nested metadata " + field)
    return True


def _display_leaves(row):
    """The one supported source-display object, with exact canonical JSON spans.

    Its path is a display string (often breadcrumbs), not a parsed URL path.
    Favicon is an icon hint; it stays private. No component equivalence is assumed.
    """
    value = json.loads(row["text"])
    if row["field"] != "native:meta_url" or not isinstance(value, dict):
        return [dict(text=row["text"], role=row["role"])]
    if row["text"] != json.dumps(value, ensure_ascii=False, sort_keys=True):
        _unsupported("noncanonical source-display JSON")
    leaves, cursor = [], 1  # Opening brace, then the sorted key/value pairs.
    for key in sorted(value):
        token = json.dumps(value[key], ensure_ascii=False)
        start = cursor + len(json.dumps(key, ensure_ascii=False) + ": ")
        end = start + len(token)
        if row["text"][start:end] != token:
            _unsupported("misaligned source-display leaf")
        if key != "favicon":
            leaves.append(dict(text=token, role=row["role"], value_type="string",
                               source_display_kind=_URL_DISPLAY[key],
                               _source_span=dict(start=start, end=end)))
        cursor = end + 2  # Comma and space before the next canonical pair.
    return leaves


def _date(row, aliases):
    _known_keys(row, _DATE_IDENTITY | _DATE_SEMANTICS | {"field", "date_id"},
                "native date observation")
    if isinstance(row.get("raw_value"), (dict, list)):
        _unsupported("non-scalar native date value")
    if row.get("field") not in aliases:
        _unsupported("native date field reference")
    return {key: (aliases[value] if key == "field" else deepcopy(value))
            for key, value in row.items() if key in _DATE_SEMANTICS | {"field", "date_id"}}


def _project_schema(schema, aliases, case_id):
    """Rewrite only schema quote-field enums and the root occurrence enum."""
    result = deepcopy(schema)
    seen = set()

    def visit(node):
        if not isinstance(node, dict) or id(node) in seen:
            return
        seen.add(id(node))
        props = node.get("properties", {})
        if "field" in props and "quote" in props:
            field = props["field"]
            if "enum" in field:
                field["enum"] = [alias for f in field["enum"] for alias in aliases.get(f, [])]
            else:
                # The existing builders omit the enum when no own-text field
                # exists. Do not turn that absence into metadata quote authority.
                field["enum"] = []
        for child in props.values():
            visit(child)
        visit(node.get("items"))
        for key in ("anyOf", "oneOf", "allOf"):
            for child in node.get(key, []):
                visit(child)

    visit(result)
    case_schema = result.get("properties", {}).get("case_id")
    if case_schema is not None:
        if case_schema.get("enum") != [case_id]:
            _unsupported("schema occurrence binding")
        case_schema["enum"] = [MODEL_CASE]
    return result


def project(model_packet, *, source_packet, profile=PROFILE):
    """Return {packet, binding, binding_sha256}; callers must pin the latter.

    model_packet is an already-built ordinary/publication/adjudication packet.
    source_packet is its private own-evidence packet (compiled['packet']). The
    binding contains full private snapshots for replay; never send it to a model.
    The retained candidate profile keeps prompt/policy text verbatim. The live
    profile uses only its versioned provider-neutral display of existing rules.
    """
    if profile not in {PROFILE, LIVE_PROFILE}:
        raise ValueError("unsupported blind-input profile")
    data = model_packet["input"]
    _known_keys(data, _INPUT_FIELDS, "model input fields")
    question = data["complete_question"]
    _known_keys(question, _QUESTION_FIELDS | _QUESTION_PRIVATE, "question fields")
    if (data["case_id"] != source_packet["case_id"]
            or data["allowed_text"] != source_packet["allowed_text"]
            or data["returned_url"] != source_packet["returned_url"]
            or data["native_field_states"] != source_packet["native_field_states"]
            or data["native_record_state"] != source_packet["native_record_state"]
            or data["native_metadata"] != source_packet["native_metadata"]
            or data["date_observations"] != source_packet["date_observations"]
            or question != source_packet["question"]
            or data["complete_contract"] != question["contract"]
            or data["query"] != question["provider_query"]):
        raise ValueError("model/source packet binding differs")

    aliases = {row["field"]: row["field"] for row in source_packet["allowed_text"]}
    aliases["url"] = "url"
    quote_aliases = {field: [alias] for field, alias in aliases.items()}
    field_aliases = {alias: field for field, alias in aliases.items()}
    source_leaf_spans = {}
    metadata = []
    for row in data["native_metadata"]:
        if _metadata_text(row, data["date_observations"]):
            if row["field"] in quote_aliases:
                _unsupported("duplicate metadata field")
            quote_aliases[row["field"]] = []
            for leaf in _display_leaves(row):
                alias = "M" + str(len(metadata) + 1)
                if alias in field_aliases:
                    _unsupported("duplicate metadata alias")
                span = leaf.pop("_source_span", None)
                if span is None:
                    aliases[row["field"]] = alias
                else:
                    source_leaf_spans[alias] = dict(source_field=row["field"], **span)
                quote_aliases[row["field"]].append(alias)
                field_aliases[alias] = row["field"]
                metadata.append(dict(field=alias, **leaf))

    result = deepcopy(data)
    result["case_id"] = MODEL_CASE
    result["complete_question"] = {key: deepcopy(question[key]) for key in _QUESTION_FIELDS}
    result["native_metadata"] = metadata
    result["date_observations"] = [_date(row, aliases) for row in data["date_observations"]]
    native = data["native_date_ids"]
    if [r["date_id"] for r in native] != [f"N{i + 1}" for i in range(len(native))]:
        _unsupported("native date IDs")
    if len(native) != len(data["date_observations"]):
        _unsupported("native date table length")
    for row, observation in zip(native, data["date_observations"]):
        if {key: value for key, value in row.items()
                if key not in {"date_id", "calendar_comparison_interval"}} != observation:
            raise ValueError("native date table/source binding differs")
    if data["native_date_display"] != native_date_display(source_packet):
        raise ValueError("native date display/source binding differs")
    result["native_date_ids"] = [_date(row, aliases) for row in native]
    result["native_date_display"] = [dict(
        date_id=row["date_id"], source_field=row["field"],
        raw_value=deepcopy(row["raw_value"]), declared_meaning=row["date_kind"],
        derivation=row["derivation"],
        native_metadata=deepcopy(next(m for m in metadata if m["field"] == row["field"])),
        observation=deepcopy(result["date_observations"][index]),
    ) for index, row in enumerate(result["native_date_ids"])]

    app = data["publication_estimate_applicability"]
    if app is not None:
        _known_keys(app, {"configuration_sha256", "field_path", "field_paths", "mapping_id", "reviewer",
                          "reason", "source_bindings", "policy", "state", "date_trust"},
                    "native policy applicability")
    eligible = [] if app is None else [row["date_id"] for row in native
                                      if row["relative_path"] in app.get("field_paths", [app.get("field_path")])]
    supplied = data.get("eligible_native_date_ids", eligible)
    if len(supplied) != len(set(supplied)) or set(supplied) - set(eligible):
        raise ValueError("model native eligibility differs from source field")
    result["publication_estimate_applicability"] = (None if app is None else dict(
        policy=app["policy"], state=app["state"], eligible_native_date_ids=deepcopy(supplied)))
    if "native_mapping_proof" in data:
        _known_keys(data["native_mapping_proof"], {
            "profile", "verified_by", "applicability_mapping_id", "field_path",
            "applicability_id_scope", "observation_id_scope", "relationship",
            "portable_authority", "source_authority", "observations", "limit",
        }, "native mapping proof")
        result["native_mapping_proof"] = dict(
            profile="private-source-bound-native-eligibility-display-v1",
            observations=[{key: deepcopy(row[key])
                           for key in ("date_id", "field", "date_kind", "derivation")}
                          for row in result["native_date_ids"] if row["date_id"] in supplied],
            limit=data["native_mapping_proof"]["limit"],
        )
    schema = _project_schema(model_packet["schema"], quote_aliases, source_packet["case_id"])
    prompt = model_packet["prompt"]
    if profile == LIVE_PROFILE:
        prompt = model_display(prompt, profile)
        policies = {row["policy"]: row for row in policy_sections(profile)}
        if "existing_policy" in result:
            result["existing_policy"] = deepcopy(policies[result["existing_policy"]["policy"]])
        if "existing_policies" in result:
            result["existing_policies"] = [deepcopy(policies[row["policy"]]) for row in result["existing_policies"]]
    packet = dict(profile=profile, input=result, schema=schema, prompt=prompt)
    packet.update(input_sha256=digest(result), schema_sha256=digest(schema),
                  prompt_sha256=digest(packet["prompt"].encode()))
    binding = dict(profile=profile, source_packet=deepcopy(source_packet),
                   source_model_packet=deepcopy(model_packet), packet=deepcopy(packet),
                   source_packet_sha256=digest(source_packet),
                   source_model_packet_sha256=digest(model_packet),
                   projected_packet_sha256=digest(packet),
                   field_aliases=field_aliases, source_leaf_spans=source_leaf_spans)
    return dict(packet=packet, binding=binding, binding_sha256=digest(binding))


def restore_verdict(raw_verdict, binding, *, expected_binding_sha256):
    """Restore structural identifiers after verifying an externally pinned hash.

    Raw output is never mutated. This only validates the projected schema and
    alias binding. Source-display leaf quotes also require exact literal spans;
    their occurrence index is restored to that original source span. Existing
    own-span/date/policy/grade validation is still required.
    A hash copied from a modified binding is not an independent integrity check.
    """
    if digest(binding) != expected_binding_sha256:
        raise ValueError("blind-input binding hash differs")
    replay = project(binding["source_model_packet"], source_packet=binding["source_packet"],
                     profile=binding["profile"])
    if replay["binding"] != binding:
        raise ValueError("blind-input source/projection binding differs")
    schema = binding["packet"]["schema"]
    errors = validate(raw_verdict, schema)
    if errors:
        raise ValueError("projected verdict schema differs: " + "; ".join(errors))
    result = deepcopy(raw_verdict)
    aliases = binding["field_aliases"]
    source_texts = {r["field"]: r["text"] for r in binding["source_packet"]["native_metadata"]}
    model_texts = {r["field"]: r["text"] for r in binding["packet"]["input"]["native_metadata"]}
    seen = set()

    def restore_leaf(value):
        leaf = binding["source_leaf_spans"][value["field"]]
        model_span = exact_span(model_texts[value["field"]], value["quote"], value["occurrence"])
        if model_span["mode"] != "literal":
            raise ValueError("source-display quote must match literal serialized text")
        text = source_texts[leaf["source_field"]]
        if text[leaf["start"]:leaf["end"]] != model_texts[value["field"]]:
            raise ValueError("source-display leaf span differs")
        target = (leaf["start"] + model_span["start"], leaf["start"] + model_span["end"])
        # The existing exact-span owner determines occurrence numbering, even
        # when identical strings occur in other values or metadata keys.
        for occurrence in range(1, len(text) + 1):
            span = exact_span(text, value["quote"], occurrence)
            if (span["start"], span["end"]) == target and span["mode"] == "literal":
                value["occurrence"] = occurrence
                return
        raise ValueError("source-display quote/source span differs")

    def restore(value, spec):
        if isinstance(value, (dict, list)):
            if id(value) in seen:
                return
            seen.add(id(value))
        if isinstance(value, list):
            for child in value:
                restore(child, spec.get("items", {}))
        elif isinstance(value, dict):
            props = spec.get("properties", {})
            if "field" in props and "quote" in props:
                if value.get("field") not in aliases:
                    raise ValueError("unknown blind-input field alias")
                if value["field"] in binding["source_leaf_spans"]:
                    restore_leaf(value)
                value["field"] = aliases[value["field"]]
            for key, child in value.items():
                restore(child, props.get(key, {}))

    restore(result, schema)
    if "case_id" in schema.get("properties", {}):
        result["case_id"] = binding["source_packet"]["case_id"]
    return result
