"""Versioned official-document date capabilities, separate from field admission.

These receipts describe retrieval controls, never verified first-publication truth.
No network, credential access, endpoint switching or query rewriting occurs here.
"""
from copy import deepcopy
from datetime import timedelta
import re

from .common import digest

PROFILE = "documented-date-controls-v1"
LINKUP_DATE_PROFILE = "linkup-considered-date-widened-v1"
PARALLEL_DATE_PROFILE = "parallel-publication-lower-v1"
CHECKED_AT = "2026-11-05"

# Exact products reviewed. Unlisted products must receive an onboarding review.
# (fields, meaning, precision/bounds, official documentation, limitations)
_ROWS = {
    "exa-highlights": (["startPublishedDate", "endPublishedDate"], "publication", "datetime; legacy mapping widens by one second at each end", "https://exa.ai/docs/reference/search", []),
    "perplexity-web": (["search_after_date_filter", "search_before_date_filter"], "publication", "MM/DD/YYYY; legacy conservative calendar widening", "https://docs.perplexity.ai/api-reference/search-post", []),
    "valyu-search": (["start_date", "end_date"], "publication", "inclusive calendar dates; legacy upper boundary conservatively retained", "https://docs.valyu.ai/api-reference/endpoint/search", []),
    "keenable-pro": (["published_after", "published_before"], "publication", "inclusive datetime bounds; UTC date interpretation", "https://docs.keenable.ai/api-reference/search", ["Missing publication dates are excluded when the publication filter is set; acquired bounds are index time, not publication."]),
    "seltz-web-pro": (["from_date", "to_date"], "publication", "inclusive datetime bounds; UTC", "https://docs.seltz.ai/api-reference/search", ["OpenAPI 1.4.1 documents top-level published_date; properties.published_date equivalence is not established."]),
    "linkup-fast": (["fromDate", "toDate"], "considered_result_date_unspecified", "YYYY-MM-DD; fromDate < toDate; dates later than 1970-01-01; timezone/inclusivity unspecified", "https://docs.linkup.so/pages/documentation/endpoints/search/reference", ["First-publication meaning and exact inclusivity are not established.", "A one-day exact interval cannot be claimed from this schema; proposed mapping widens both calendar boundaries."]),
    "firecrawl-web": (["tbs"], "search_engine_time_unspecified", "MM/DD/YYYY custom range; timezone/inclusivity unspecified", "https://docs.firecrawl.dev/features/search", ["tbs applies only to web, not news/images.", "Search-engine time filtering does not establish first publication."]),
    "you-com-web": (["freshness"], "freshness_unspecified", "YYYY-MM-DDtoYYYY-MM-DD; timezone/inclusivity unspecified", "https://you.com/docs/api-reference/search/v1-search", ["Temporal query terms and freshness use the broader timeframe; a strict requested interval is not assured."]),
    "tavily-news": (["start_date", "end_date"], "publication_or_update", "YYYY-MM-DD after/before; exact inclusivity/timezone unspecified", "https://docs.tavily.com/documentation/api-reference/endpoint/search", ["The date can be publication or last update; an in-window original updated later can be excluded.", "filter_by_published_date is a separate strict-removal choice, including undated results; no new mapping is admitted here."]),
    "brave-web": (["freshness"], "publication_or_update", "YYYY-MM-DDtoYYYY-MM-DD; timezone/inclusivity unspecified", "https://api-dashboard.search.brave.com/api-reference/web/search/get", ["Page age uses relevant publication or modification date; original in-window articles updated later can be excluded."]),
    "parallel-advanced": (["advanced_settings.source_policy.after_date"], "publication", "earliest publication date YYYY-MM-DD; no documented upper bound; timezone unspecified", "https://docs.parallel.ai/search/advanced-search-settings", ["Lower bound only, not an interval equivalent.", "SourcePolicy meaning: https://docs.parallel.ai/resources/source-policy"]),
    "searlo-web": (["dateRestrict"], "rolling_recent_result_date_unspecified", "dN/wN/mN/yN relative to actual request time; timezone/inclusivity unspecified", "https://searlo.tech/docs/search-web", ["No historical upper bound; no rolling approximation is admitted for a fixed publication window.", "Cannot combine with query operators site:, inurl:, intitle:, filetype:, before:, after:, email addresses or long OR chains; query is never rewritten.", "Long OR chain length is undocumented; unsupported combinations need review before any future relative-recency mapping."]),
    "oxylabs-search": ([], "no_documented_control", "none documented for Instant", "https://pypi.org/project/oxylabs-ai-studio/", ["Vendor SDK Instant parameters are query, limit and geo_location; other Oxylabs products do not establish this product's capability."]),
}

# Same endpoint, independently selected general mode. Date inclusion is opt-in
# outside news and is left at its documented default; no native field is trusted.
_ROWS["tavily-general"] = _ROWS["tavily-news"]


def _catalog_identity(config):
    # New opt-in fields do not change the endpoint/mode being reviewed.
    from ._method.providers import configuration_catalog
    for row in configuration_catalog(include_optional=True):
        if row["configuration_id"] == config["configuration_id"]:
            keys = ("provider", "method", "endpoint", "query_fields", "date_mapping")
            mode_keys = ("mode", "depth", "outputType", "sources", "scope", "tier", "topic", "search_depth", "type", "search_type")
            return (all(config.get(k) == row.get(k) for k in keys)
                    and all(config["base_body"].get(k) == row["base_body"].get(k) for k in mode_keys))
    return False


def capability(config):
    cid = config["configuration_id"]
    known = cid in _ROWS and _catalog_identity(config)
    fields, meaning, bounds, url, limitations = _ROWS[cid] if known else ([], "unreviewed_exact_product", "unknown", None, ["Official-document onboarding review required for this exact endpoint/mode."])
    fixed = cid in {"exa-highlights", "perplexity-web", "valyu-search", "keenable-pro", "seltz-web-pro", "linkup-fast", "parallel-advanced"} and known
    return dict(profile=PROFILE, configuration_id=cid, method=config["method"], endpoint=config["endpoint"],
                mode={k: deepcopy(config["base_body"][k]) for k in ("mode", "depth", "outputType", "sources", "scope", "tier", "topic", "search_depth", "type", "search_type") if k in config["base_body"]},
                fields=deepcopy(fields), date_meaning=meaning, bounds=bounds,
                docs=[dict(url=url, checked_at=CHECKED_AT, revision="current official reference; no historical server proof")] if url else [],
                limitations=deepcopy(limitations), reviewed=known, fixed_publication_window_applicable=fixed,
                eligibility_proof=False)


def apply_profile(config, profile):
    result = deepcopy(config)
    cap = capability(config)
    expected = {"linkup-fast": LINKUP_DATE_PROFILE, "parallel-advanced": PARALLEL_DATE_PROFILE}
    if not cap["reviewed"] or expected.get(config["configuration_id"]) != profile:
        raise ValueError("no such explicit date profile for this exact product")
    result["date_request_profile"] = profile
    return result


def map_control(config, start, end, query):
    """Return proposed body additions and a receipt, only for the opt-in profile.

    Day controls with unsettled inclusivity are widened on both sides. This is
    disclosed approximation, not proof against undocumented server behavior.
    """
    expected = {"linkup-fast": LINKUP_DATE_PROFILE, "parallel-advanced": PARALLEL_DATE_PROFILE}
    if config.get("date_request_profile") != expected.get(config["configuration_id"]):
        raise ValueError("unsupported explicit date request profile")
    cap = capability(config)
    if not cap["reviewed"] or not cap["fields"]:
        raise ValueError("date capability differs from reviewed endpoint/mode")
    cid = config["configuration_id"]
    lo, hi = start.date() - timedelta(days=1), end.date() + timedelta(days=1)
    fields, state = {}, "mapped_conservative_calendar_bounds"
    if cid == "linkup-fast":
        if lo.isoformat() <= "1970-01-01":
            state = "owner_decision_required_documented_1970_boundary"
        else:
            fields.update(fromDate=lo.isoformat(), toDate=hi.isoformat())
    elif cid == "parallel-advanced":
        advanced = deepcopy(config["base_body"].get("advanced_settings", {}))
        advanced.setdefault("source_policy", {})["after_date"] = lo.isoformat()
        fields["advanced_settings"] = advanced
        state = "mapped_publication_lower_bound_only"
    else:
        raise ValueError("unsupported explicit date profile")
    return fields, dict(profile=config["date_request_profile"], capability_sha256=digest(cap), state=state,
                        requested_start_inclusive=start.isoformat(), requested_end_exclusive=end.isoformat(),
                        sent_date_fields=deepcopy(fields), timezone="input normalized to UTC; provider timezone as capability receipt",
                        widening=("Previous start day and next end day; exact inclusivity and first-publication meaning remain unresolved."
                                  if cid == "linkup-fast" else "Previous UTC start calendar day conservatively covers unresolved provider timezone edges; no upper bound is sent; provider timezone remains unspecified."),
                        eligibility_proof=False)


def prepare(providers, questions, *, prepared_at=None):
    """Offline preflight: expose supported omissions and unresolved combinations."""
    from ._method.providers import build_request
    from ._method.native import field_value
    rows = []
    for config in providers:
        cap = capability(config)
        requests = []
        for question in questions:
            request = build_request(config, question)
            publication = any(r["basis"] == "publication" for r in question["contract"]["temporal_requirements"])
            sent = {field: deepcopy(field_value(request["body"], field)[1]) for field in cap["fields"] if field_value(request["body"], field)[0]}
            receipt = request.get("date_capability_receipt") or dict(profile="existing-request-date-mapping",
                capability_sha256=digest(cap), state=request["date_mapping_state"], sent_date_fields=sent,
                requested_publication_windows=deepcopy([r for r in question["contract"]["temporal_requirements"] if r["basis"] == "publication"]),
                eligibility_proof=False)
            query_state = "no_documented_query_incompatibility_reviewed"
            if config["configuration_id"] == "searlo-web":
                query = question["provider_query"]
                query_state = ("documented_operator_or_email_incompatibility" if re.search(r"\b(?:site|inurl|intitle|filetype|before|after):|[^\s@]+@[^\s@]+", query, re.I)
                               else "OR_chain_length_undocumented_requires_review" if re.search(r"\bOR\b", query)
                               else "no_documented_operator_conflict_detected")
            omitted = publication and cap["fixed_publication_window_applicable"] and config["date_mapping"] == "unmapped" and not request.get("date_capability_receipt", {}).get("sent_date_fields")
            requests.append(dict(question_id=question["question_id"], revision=question["revision"],
                state=request["date_mapping_state"], supported_filter_omitted=bool(omitted),
                receipt=receipt, publication_requirement=publication, query_compatibility=query_state,
                unsupported_for_fixed_publication=publication and bool(cap["fields"]) and not cap["fixed_publication_window_applicable"]))
        rows.append(dict(capability=cap, requests=requests))
    return dict(profile=PROFILE, prepared_at=prepared_at, providers=rows,
                supported_omissions=[dict(configuration_id=r["capability"]["configuration_id"], question_id=q["question_id"], revision=q["revision"]) for r in rows for q in r["requests"] if q["supported_filter_omitted"]],
                live_execution_authorized=False)
