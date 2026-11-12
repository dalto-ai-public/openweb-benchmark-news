"""Explicit versioned request changes; prior packages retain their exact bytes."""
from copy import deepcopy
from .provider_dates import LINKUP_DATE_PROFILE, PARALLEL_DATE_PROFILE, apply_profile

BRAVE_UNDECORATED = "brave-undecorated-v1"
PERPLEXITY_DEFAULT_CONTEXT = "perplexity-documented-context-default-v1"


def apply(providers, profiles):
    providers = deepcopy(providers)
    by_id = {p["configuration_id"]: p for p in providers}
    for cid, profile in profiles.items():
        if cid == "perplexity-web" and cid in by_id and profile == PERPLEXITY_DEFAULT_CONTEXT:
            # Keep historical packages/catalog bytes reproducible. The new
            # opt-in omits only the no-longer-documented context-size flag.
            by_id[cid]["base_body"].pop("search_context_size", None)
            by_id[cid]["request_profile"] = profile
            continue
        if cid in by_id and profile in (LINKUP_DATE_PROFILE, PARALLEL_DATE_PROFILE):
            selected = apply_profile(by_id[cid], profile)
            by_id[cid].update(selected)
            continue
        if cid != "brave-web" or profile != BRAVE_UNDECORATED or cid not in by_id:
            raise ValueError("unsupported explicit provider request profile")
        # GET query values are strings; urlencode(False) would emit capital F.
        by_id[cid]["base_body"]["text_decorations"] = "false"
        by_id[cid]["request_profile"] = profile
    return providers


# Retrieval starts are request policy, separate from the frozen native-date
# capability/admission registry. Keep that registry and its source bindings intact.
from datetime import datetime, timedelta
from pathlib import Path
from .common import digest
from .provider_dates import capability

MIXED_START_PROFILE = "mixed-date-retrieval-lower-v1"
MIXED_START_IDS = frozenset({"brave-web", "you-com-web", "tavily-general"})
MIXED_START_LIMITATIONS = [
    "Mixed/unspecified retrieval dates can predate valid entries on changing pages; useful results may be lost. This is not a publication interval or eligibility proof.",
    "The provider's internal filtering date, timezone and boundary semantics are not verified. Tavily news observations do not establish general-mode filtering behavior.",
    "Brave/You use a closed range as a lower-bound approximation, ending two UTC calendar days after first dispatch. Provider acceptance of the future bound is not live-verified; errors do not authorize fallback or tuning.",
    "The first-dispatch range stays fixed across retries and resume, including later days. Collection timing remains retrieval provenance, not publication evidence.",
]


def validate_start_profiles(providers, profiles):
    """Select retrieval controls without changing native/provider identities."""
    by_id = {p["configuration_id"]: p for p in providers}
    if not isinstance(profiles, dict):
        raise ValueError("date profiles must map exact provider IDs to profiles")
    for cid, profile in profiles.items():
        if (cid not in MIXED_START_IDS or cid not in by_id or profile != MIXED_START_PROFILE
                or not capability(by_id[cid])["reviewed"]
                or by_id[cid].get("date_request_profile")):
            raise ValueError("unsupported or conflicting retrieval start profile")


def map_start_control(config, start, end, profile):
    """A mixed-date lower proxy, never an eligibility or losslessness claim."""
    validate_start_profiles([config], {config["configuration_id"]: profile})
    lo = (start.date() - timedelta(days=1)).isoformat()
    receipt = dict(profile=profile, capability_sha256=digest(capability(config)),
        requested_start_inclusive=start.isoformat(), requested_end_exclusive=end.isoformat(),
        lower_date=lo, eligibility_proof=False,
        limitations=deepcopy(MIXED_START_LIMITATIONS))
    if config["configuration_id"] == "tavily-general":
        return {"start_date": lo}, dict(receipt, state="mapped_mixed_retrieval_lower_only",
            sent_date_fields={"start_date": lo}), None
    # Closed ranges are the documented shape. Two UTC calendar days keep the
    # current UTC day inside even an exclusive local midnight at UTC+14.
    # This is a declared approximation, not proof of server interpretation.
    binding = dict(profile=profile, field="freshness", lower_date=lo,
        upper_rule="first-dispatch-utc-date-plus-two-days", eligibility_proof=False)
    return {}, dict(receipt, state="pending_first_dispatch_closed_retrieval_range",
        sent_date_fields={}, dispatch_binding=binding), binding


def _dispatch_request(request, anchor):
    """Resolve the declared range without modifying the frozen search request."""
    from ._method.providers import _instant
    binding = request["date_dispatch"]
    cid = request["configuration_id"]
    if (cid not in {"brave-web", "you-com-web"} or binding != dict(
            profile=MIXED_START_PROFILE, field="freshness", lower_date=binding.get("lower_date"),
            upper_rule="first-dispatch-utc-date-plus-two-days", eligibility_proof=False)
            or request.get("date_capability_receipt", {}).get("dispatch_binding") != binding
            or "freshness" in request["body"]):
        raise ValueError("dispatch date declaration differs")
    lower = datetime.strptime(binding["lower_date"], "%Y-%m-%d").date()
    if lower.isoformat() != binding["lower_date"]:
        raise ValueError("dispatch lower date must be canonical")
    at = _instant(anchor)
    upper = at.date() + timedelta(days=2)
    if lower >= upper:
        raise ValueError("retrieval start is not before dispatch range end")
    effective = deepcopy(request)
    effective.pop("date_dispatch")
    effective["body"]["freshness"] = lower.isoformat() + "to" + upper.isoformat()
    effective["date_mapping_state"] = "mapped_mixed_retrieval_closed_range_as_lower_proxy"
    effective["date_capability_receipt"].update(state=effective["date_mapping_state"],
        sent_date_fields={"freshness": effective["body"]["freshness"]}, first_dispatch_at=at.isoformat())
    return effective


def bind_dispatch(request, path, *, dispatched_at=None):
    """Persist once before send; every retry/resume uses the same wire fields.

    Called within the ledger's per-task lock, only for an actual attempt. The
    declaration stays the ledger request identity. No credential values enter
    this artifact; credentials are injected later by the existing transport.
    """
    if "date_dispatch" not in request:
        return request
    from .common import now, write_private
    path = Path(path)
    if path.is_symlink():
        raise ValueError("dispatch receipt must not be a symlink")
    if path.exists():
        return read_dispatch(request, path)
    anchor = dispatched_at or now()
    effective = _dispatch_request(request, anchor)
    write_private(path, dict(profile=MIXED_START_PROFILE, declared_request_sha256=digest(request),
        first_dispatch_at=anchor, effective_request=effective, effective_request_sha256=digest(effective)))
    return effective


def read_dispatch(request, path):
    """Read-only replay; a missing or changed dispatch receipt fails closed."""
    from .common import load
    path = Path(path)
    if path.is_symlink():
        raise ValueError("dispatch receipt must not be a symlink")
    saved = load(path)
    anchor = saved["first_dispatch_at"]
    effective = _dispatch_request(request, anchor)
    expected = dict(profile=MIXED_START_PROFILE, declared_request_sha256=digest(request),
        first_dispatch_at=anchor, effective_request=effective, effective_request_sha256=digest(effective))
    if saved != expected:
        raise ValueError("retained dispatch request differs from frozen declaration")
    return effective


def audit_dispatch(package, run, *, ledger_name="attempts.jsonl"):
    """Verify sent range fields from the declaration through each saved attempt."""
    from .common import load, records
    run = Path(run)
    requests = {"search-" + digest(dict(configuration=r["configuration_id"], question=r["question_id"])): r
                for r in package.get("search_requests", []) if "date_dispatch" in r}
    verified = 0
    uncertain = []
    if not requests:
        return dict(profile=MIXED_START_PROFILE, verified_attempts=0)
    for event in records(run / ledger_name):
        task = event.get("task_id")
        if event["event"] != "finished" or task not in requests:
            continue
        path = run / event["result_path"]
        result = load(path)
        declared_attempt = load(path.parent / "request.json")
        receipt = run / "search-dispatch" / (task + ".json")
        attempt = path.parent / "effective-search-request.json"
        if (digest(declared_attempt) != event["request_sha256"]
                or any(declared_attempt.get(k) != v for k,v in requests[task].items())
                or digest(path.read_bytes()) != event["result_sha256"]):
            raise ValueError("search attempt differs from declared ledger identity")
        # The existing ledger seals exceptions/interruption as uncertain, not
        # as normal transport returns. Never require a completed-send receipt
        # from a process that may have stopped before even writing that file.
        ledger_exception = (set(result) == {"transport_success", "error_type", "dispatch_state", "error"}
            and result.get("error") == "task failed; inspect private transport records"
            and isinstance(result.get("error_type"), str))
        interrupted = (event.get("reconciled_after_interruption") is True
            and result == dict(transport_success=False, complete=False, dispatch_state="uncertain",
                error="prior process ended after the durable start boundary; automatic recovery retains possible duplicate billing"))
        if ((ledger_exception or interrupted) and result.get("transport_success") is False
                and result.get("dispatch_state") == "uncertain"):
            if receipt.exists():
                effective = read_dispatch(requests[task], receipt)
                if attempt.exists() and load(attempt) != effective:
                    raise ValueError("interrupted effective search request differs")
            elif attempt.exists():
                raise ValueError("interrupted effective request lacks its dispatch anchor")
            uncertain.append(event["attempt_id"])
            continue
        effective = read_dispatch(requests[task], receipt)
        if (load(attempt) != effective
                or result.get("effective_search_request_sha256") != digest(effective)
                or result.get("search_dispatch_receipt_sha256") != digest(receipt.read_bytes())):
            raise ValueError("actual search dispatch differs from retained date declaration")
        verified += 1
    return dict(profile=MIXED_START_PROFILE, verified_attempts=verified,
        **({"uncertain_attempt_ids":uncertain} if uncertain else {}))


def start_disclosures(package):
    """Public-safe method limits; no response text, credentials or scores."""
    profiles = package["configuration"].get("provider_date_profiles", {})
    return dict(profile=MIXED_START_PROFILE, selected_profiles=deepcopy(profiles),
        lower_rule="publication-window-start-minus-one-utc-calendar-day",
        closed_range_upper_rule="first-dispatch-utc-date-plus-two-days; retained unchanged on retry/resume",
        planned_profile_search_cells=sum(r["configuration_id"] in profiles for r in package["search_requests"]),
        limitations=deepcopy(MIXED_START_LIMITATIONS), eligibility_proof=False,
        dispatch_receipt_location="search-dispatch/; effective-search-request.json within each native attempt")


def prepare_dates(providers, questions, *, profiles):
    """Overlay explicit retrieval receipts on the existing capability report."""
    from .provider_dates import prepare
    from ._method.providers import build_request
    validate_start_profiles(providers, profiles)
    report = prepare(providers, questions)
    by_id = {p['configuration_id']:p for p in providers}
    for row in report['providers']:
        cid = row['capability']['configuration_id']
        if cid not in profiles:
            continue
        row['selected_retrieval_profile'] = profiles[cid]
        for observed, question in zip(row['requests'], questions):
            request = build_request(by_id[cid], question, date_profile=profiles[cid])
            observed['state'] = request['date_mapping_state']
            if 'date_capability_receipt' in request:
                observed['receipt'] = deepcopy(request['date_capability_receipt'])
    return report
