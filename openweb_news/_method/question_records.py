"""Sibling question-record validator; portable authoring checks, not semantic admission.

Selected functions retained from the source recorded in ATTRIBUTION.md. Release
membership is owned by question_bank.freeze; historical bank-size restrictions
and source-local rendering/CLI are intentionally not carried.
"""
from collections import defaultdict
from datetime import datetime
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

REVIEW_STATES = {"draft", "needs_clarification", "needs_evidence", "reviewed", "superseded"}
ORIGINS = {"actual", "expert", "realistic_generated"}
HEX_256 = re.compile(r"^[0-9a-f]{64}$")
FORBIDDEN_ANSWER_FIELDS = {"answer", "answer_key", "reference_answer", "judgments", "provider_results"}


def filled(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def aware_time(value: object, label: str, errors: list[str]) -> datetime | None:
    if not filled(value):
        errors.append(f"{label}: required timestamp")
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        errors.append(f"{label}: invalid ISO timestamp")
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        errors.append(f"{label}: timezone offset required")
        return None
    return parsed


def require_text(record: dict, key: str, label: str, errors: list[str]) -> None:
    if not filled(record.get(key)):
        errors.append(f"{label}.{key}: required nonempty text")


def require_list(record: dict, key: str, label: str, errors: list[str]) -> list:
    value = record.get(key)
    if not isinstance(value, list):
        errors.append(f"{label}.{key}: expected a list")
        return []
    return value


def validate_question(q: dict, errors: list[str]) -> None:
    label = str(q.get("question_id") or "<missing question_id>")
    for key in ("question_id", "need_id", "family_id", "original_need", "provider_query", "primary_subject"):
        require_text(q, key, label, errors)
    if type(q.get("revision")) is not int or q["revision"] < 1:
        errors.append(f"{label}.revision: expected positive integer")
    if q.get("input_surface") not in {"agent_query", "user_question"}:
        errors.append(f"{label}.input_surface: expected agent_query or user_question")
    if q.get("origin") not in ORIGINS:
        errors.append(f"{label}.origin: expected actual, expert, or realistic_generated")
    forbidden = FORBIDDEN_ANSWER_FIELDS.intersection(q)
    if forbidden:
        errors.append(f"{label}: factual answer/provider fields belong outside the question record: {sorted(forbidden)}")
    if "source_pair_id" in q and not filled(q["source_pair_id"]):
        errors.append(f"{label}.source_pair_id: expected nonempty text")
    if "question_form" in q:
        require_text(q, "question_form", label, errors)

    tags = require_list(q, "secondary_tags", label, errors)
    if any(not filled(tag) for tag in tags):
        errors.append(f"{label}.secondary_tags: entries must be nonempty text")
    geo = q.get("geography")
    if not isinstance(geo, dict):
        errors.append(f"{label}.geography: expected an object")
    else:
        regions = require_list(geo, "regions", f"{label}.geography", errors)
        if not regions or any(not filled(region) for region in regions):
            errors.append(f"{label}.geography.regions: at least one nonempty region required")
        require_text(geo, "applies_to", f"{label}.geography", errors)

    provenance = q.get("provenance")
    if not isinstance(provenance, dict):
        errors.append(f"{label}.provenance: expected an object")
    else:
        require_text(provenance, "source_id", f"{label}.provenance", errors)
        if provenance.get("candidate_id") is not None and not filled(provenance.get("candidate_id")):
            errors.append(f"{label}.provenance.candidate_id: expected nonempty text or null")
        if not filled(provenance.get("original_text")) and not filled(provenance.get("original_sha256")):
            errors.append(f"{label}.provenance: original_text or permitted original_sha256 required")
        if filled(provenance.get("original_sha256")) and not HEX_256.fullmatch(provenance["original_sha256"]):
            errors.append(f"{label}.provenance.original_sha256: expected SHA-256 hex")
        if "upstream_need_reference" in provenance:
            upstream = provenance["upstream_need_reference"]
            pointer_label = f"{label}.provenance.upstream_need_reference"
            if not isinstance(upstream, dict):
                errors.append(f"{pointer_label}: expected an object")
            else:
                for key in ("source_id", "source_record_id", "field", "sha256"):
                    require_text(upstream, key, pointer_label, errors)
                if filled(upstream.get("sha256")) and not HEX_256.fullmatch(upstream["sha256"]):
                    errors.append(f"{pointer_label}.sha256: expected SHA-256 hex")

    contract = q.get("contract")
    if not isinstance(contract, dict):
        errors.append(f"{label}.contract: expected an object")
    else:
        validate_contract(contract, q.get("provider_query"), label, errors)
    review = q.get("review")
    if not isinstance(review, dict):
        errors.append(f"{label}.review: expected an object")
    else:
        if review.get("state") not in REVIEW_STATES:
            errors.append(f"{label}.review.state: invalid state")
        issues = require_list(review, "issues", f"{label}.review", errors)
        if any(not filled(issue) for issue in issues):
            errors.append(f"{label}.review.issues: entries must be nonempty text")
        if review.get("state") == "reviewed":
            if isinstance(contract, dict) and contract.get("allowed_snippet_languages") is None:
                errors.append(f"{label}: reviewed contract requires resolved snippet languages")
            require_text(review, "reviewer", f"{label}.review", errors)
            aware_time(review.get("reviewed_at"), f"{label}.review.reviewed_at", errors)
            if review.get("query_contract_consistency") != "pass":
                errors.append(f"{label}.review.query_contract_consistency: reviewed record requires pass")


def validate_contract(contract: dict, query: object, label: str, errors: list[str]) -> None:
    require_text(contract, "query_language", f"{label}.contract", errors)
    if "allowed_snippet_languages" in contract and contract["allowed_snippet_languages"] is None:
        require_text(contract, "allowed_snippet_languages_reason", f"{label}.contract", errors)
    else:
        languages = require_list(contract, "allowed_snippet_languages", f"{label}.contract", errors)
        if not languages or any(not filled(lang) for lang in languages):
            errors.append(f"{label}.contract.allowed_snippet_languages: explicit nonempty languages required")
    source = contract.get("source_rule")
    if not isinstance(source, dict):
        errors.append(f"{label}.contract.source_rule: expected an object")
    else:
        if type(source.get("independence_required")) is not bool:
            errors.append(f"{label}.contract.source_rule.independence_required: expected boolean")
        require_text(source, "qualifying_material", f"{label}.contract.source_rule", errors)
    facets = require_list(contract, "requested_facets", f"{label}.contract", errors)
    for index, facet in enumerate(facets):
        if not isinstance(facet, dict):
            errors.append(f"{label}.contract.requested_facets[{index}]: expected object")
            continue
        require_text(facet, "id", f"{label}.contract.requested_facets[{index}]", errors)
        require_text(facet, "text", f"{label}.contract.requested_facets[{index}]", errors)
    target = contract.get("requested_report_target")
    if target is not None and not filled(target):
        errors.append(f"{label}.contract.requested_report_target: expected text or null")
    if "as_of_requirement" not in contract:
        errors.append(f"{label}.contract.as_of_requirement: explicit null or object required")
    elif contract["as_of_requirement"] is not None:
        as_of = contract["as_of_requirement"]
        if not isinstance(as_of, dict):
            errors.append(f"{label}.contract.as_of_requirement: expected object or null")
        else:
            # The active standalone method also supports state-only targets;
            # those have no information-availability cutoff to invent.
            if as_of.get("meaning") != "historical_state":
                aware_time(as_of.get("cutoff"), f"{label}.contract.as_of_requirement.cutoff", errors)
            require_text(as_of, "question_phrase", f"{label}.contract.as_of_requirement", errors)
            if filled(as_of.get("question_phrase")) and filled(query) and as_of["question_phrase"].casefold() not in query.casefold():
                errors.append(f"{label}.contract.as_of_requirement.question_phrase: phrase absent from provider_query")
    context_allowed = contract.get("background_context_allowed")
    if type(context_allowed) is not bool:
        errors.append(f"{label}.contract.background_context_allowed: expected boolean")
    definition = contract.get("background_context_definition")
    if context_allowed is True and not filled(definition):
        errors.append(f"{label}.contract.background_context_definition: required when context is allowed")
    if context_allowed is False and definition is not None:
        errors.append(f"{label}.contract.background_context_definition: use null when context is disallowed")

    temporal = require_list(contract, "temporal_requirements", f"{label}.contract", errors)
    ids: set[str] = set()
    for index, req in enumerate(temporal):
        item = f"{label}.contract.temporal_requirements[{index}]"
        if not isinstance(req, dict):
            errors.append(f"{item}: expected object")
            continue
        require_text(req, "id", item, errors)
        if filled(req.get("id")):
            if req["id"] in ids:
                errors.append(f"{item}.id: duplicate requirement ID")
            ids.add(req["id"])
        basis = req.get("basis")
        if basis not in {"event", "publication"}:
            errors.append(f"{item}.basis: expected event or publication")
        start = aware_time(req.get("start_inclusive"), f"{item}.start_inclusive", errors)
        end = aware_time(req.get("end_exclusive"), f"{item}.end_exclusive", errors)
        if start is not None and end is not None and start >= end:
            errors.append(f"{item}: start must precede end")
        require_text(req, "timezone", item, errors)
        if filled(req.get("timezone")):
            try:
                ZoneInfo(req["timezone"])
            except ZoneInfoNotFoundError:
                errors.append(f"{item}.timezone: unknown timezone")
        require_text(req, "precision", item, errors)
        require_text(req, "question_phrase", item, errors)
        if filled(req.get("question_phrase")) and filled(query) and req["question_phrase"].casefold() not in query.casefold():
            errors.append(f"{item}.question_phrase: phrase absent from provider_query")
        if basis == "event":
            require_text(req, "event_kind", item, errors)
            require_text(req, "stage", item, errors)
        if basis == "publication" and ("event_kind" in req or "stage" in req):
            errors.append(f"{item}: publication requirement must not impose event kind/stage")
        if "interpretation_at" in req:
            aware_time(req["interpretation_at"], f"{item}.interpretation_at", errors)


def validate_decisions(decisions: list[dict], questions: list[dict], errors: list[str]) -> None:
    question_ids = {q.get("question_id") for q in questions if filled(q.get("question_id"))}
    candidate_to_question = {
        q.get("provenance", {}).get("candidate_id"): q
        for q in questions if isinstance(q.get("provenance"), dict) and filled(q.get("provenance", {}).get("candidate_id"))
    }
    seen: set[str] = set()
    for decision in decisions:
        label = str(decision.get("candidate_id") or "<missing candidate_id>")
        for key in ("candidate_id", "source_id", "source_record_id", "reason"):
            require_text(decision, key, label, errors)
        if label in seen:
            errors.append(f"{label}: duplicate candidate decision")
        seen.add(label)
        if not filled(decision.get("original_text")) and not filled(decision.get("original_sha256")):
            errors.append(f"{label}: original_text or permitted original_sha256 required")
        if filled(decision.get("original_sha256")) and not HEX_256.fullmatch(decision["original_sha256"]):
            errors.append(f"{label}.original_sha256: expected SHA-256 hex")
        disposition = decision.get("disposition")
        if disposition not in {"keep", "revise", "exclude"}:
            errors.append(f"{label}.disposition: expected keep, revise, or exclude")
        qid = decision.get("question_id")
        if disposition == "exclude" and qid is not None:
            errors.append(f"{label}: excluded candidate cannot point to an admitted question")
        linked = candidate_to_question.get(label)
        if disposition == "exclude" and linked is not None:
            errors.append(f"{label}: excluded candidate is used by a question")
        if disposition in {"keep", "revise"}:
            if not filled(qid) or qid not in question_ids:
                errors.append(f"{label}: admitted question_id is absent")
            elif linked is None or linked.get("question_id") != qid:
                errors.append(f"{label}: question provenance does not link back to candidate")
            else:
                provenance = linked["provenance"]
                for key in ("source_id", "source_record_id", "original_text", "original_sha256"):
                    if decision.get(key) != provenance.get(key):
                        errors.append(f"{label}: {key} differs from question provenance")
    for candidate_id in candidate_to_question:
        if candidate_id is not None and candidate_id not in seen:
            errors.append(f"{candidate_id}: question has no candidate decision")


def latest_questions(questions: list[dict]) -> list[dict]:
    latest: dict[str, dict] = {}
    for q in questions:
        qid = q.get("question_id")
        revision = q.get("revision")
        if not filled(qid) or type(revision) is not int:
            continue
        if qid not in latest or revision > latest[qid]["revision"]:
            latest[qid] = q
    return list(latest.values())


def validate_pairs(questions: list[dict], errors: list[str], complete: bool) -> None:
    groups: dict[str, list[dict]] = defaultdict(list)
    for q in questions:
        if filled(q.get("source_pair_id")):
            groups[q["source_pair_id"]].append(q)
    for pair_id, pair in groups.items():
        if len(pair) != 2:
            if complete:
                errors.append(f"source_pair_id {pair_id}: expected both variants, found {len(pair)}")
            continue
        left, right = pair
        if left.get("question_id") == right.get("question_id") or left.get("need_id") == right.get("need_id"):
            errors.append(f"source_pair_id {pair_id}: question and need IDs must differ")
        for key in ("family_id", "primary_subject", "secondary_tags", "geography"):
            if left.get(key) != right.get(key):
                errors.append(f"source_pair_id {pair_id}: {key} differs")
        lcontract = left.get("contract")
        rcontract = right.get("contract")
        if not isinstance(lcontract, dict) or not isinstance(rcontract, dict):
            continue
        lindependent = lcontract.get("source_rule", {}).get("independence_required") if isinstance(lcontract.get("source_rule"), dict) else None
        rindependent = rcontract.get("source_rule", {}).get("independence_required") if isinstance(rcontract.get("source_rule"), dict) else None
        if {lindependent, rindependent} != {True, False}:
            errors.append(f"source_pair_id {pair_id}: independence requirements must be opposite")
        for key in set(lcontract) | set(rcontract):
            if key != "source_rule" and lcontract.get(key) != rcontract.get(key):
                errors.append(f"source_pair_id {pair_id}: contract.{key} differs")


def validate_questions(questions: list[dict], decisions: list[dict] | None = None) -> list[str]:
    errors: list[str] = []
    if not questions:
        errors.append("questions: at least one record required")
    seen: set[tuple[str, int]] = set()
    for q in questions:
        validate_question(q, errors)
        if not filled(q.get("question_id")) or type(q.get("revision")) is not int:
            continue
        identity = (q["question_id"], q["revision"])
        if identity in seen:
            errors.append(f"{identity[0]} revision {identity[1]}: duplicate identity")
        seen.add(identity)
    validate_pairs(latest_questions(questions), errors, complete=False)
    if decisions is not None:
        validate_decisions(decisions, questions, errors)
    return errors
