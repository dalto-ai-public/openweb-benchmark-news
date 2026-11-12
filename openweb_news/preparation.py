"""Freeze explicit inputs; approval is a separate owner-supplied receipt."""
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
import math

from .common import asset_bindings, asset_path, code_bindings, digest, load, records, write_private
from ._method.providers import build_request, configuration_catalog
from .judging import METHOD, build_pro_request, compile_packet, packet_for
from .accounting import reservation
from .scheduling import account_bindings, validate_scheduling


def validate_geography(question):
    geography = question["geography"]
    if not isinstance(geography, dict) or not isinstance(geography.get("applies_to"), str) or not geography["applies_to"].strip():
        raise ValueError("geography must state what the regions apply to")
    regions = geography.get("regions")
    if (not isinstance(regions, list) or not regions or
            any(not isinstance(region, str) or not region.strip() or region.strip().lower() == "unspecified" for region in regions)):
        raise ValueError("draft geography is not admitted; state explicit regions or canonical no_geographic_restriction/global")
    if geography["applies_to"] == "no_geographic_restriction" and regions != ["global"]:
        raise ValueError("unrestricted geography requires exactly the canonical global region")


def unresolved_window_paths(value, path="$"):
    """Locate the known unexpanded draft marker without rewriting source data."""
    if isinstance(value, str):
        return [path] if "{window}" in value else []
    if isinstance(value, dict):
        return [p for key, child in value.items() for p in unresolved_window_paths(child, path + "." + key)]
    if isinstance(value, list):
        return [p for index, child in enumerate(value) for p in unresolved_window_paths(child, f"{path}[{index}]")]
    return []


def selected_providers(config, selected):
    """One owner for explicit catalog selection and transport profile edits."""
    by_id = {row["configuration_id"]: row for row in configuration_catalog(include_optional=True)}
    if not selected or len(set(selected)) != len(selected):
        raise ValueError("providers must be nonempty and distinct")
    providers = [by_id[key] for key in selected]
    from .projection import apply_profiles
    providers = apply_profiles(providers, config.get("provider_projection_profiles", {}))
    from .request_profiles import apply as apply_request_profiles
    providers = apply_request_profiles(providers, config.get("provider_request_profiles", {}))
    for provider in providers:
        overrides = config.get("provider_overrides", {}).get(provider["configuration_id"], {})
        if set(overrides) - {"secret_ref", "body_secret_refs"}:
            raise ValueError("provider changes require a new adapter configuration")
        if "secret_ref" in overrides:
            provider["auth"]["secret_ref"] = overrides["secret_ref"]
        if "body_secret_refs" in overrides:
            provider["body_secret_refs"] = overrides["body_secret_refs"]
    return providers


def validate_questions(questions):
    """Shared structural admission checks; semantic question review stays explicit."""
    ids = [q["question_id"] for q in questions]
    if not ids or len(set(ids)) != len(ids):
        raise ValueError("questions must have distinct nonempty IDs")
    for q in questions:
        for key in ("revision", "family_id", "need_id", "provider_query", "geography", "contract"):
            if key not in q:
                raise ValueError(f"question lacks {key}")
        validate_geography(q)
        if unresolved_window_paths({key: q[key] for key in ("provider_query", "geography", "contract")}):
            raise ValueError("question contains an unresolved {window} draft marker")
        for key in ("source_rule", "temporal_requirements", "as_of_requirement", "allowed_snippet_languages",
                    "background_context_allowed", "qualifying_predicates", "requested_facets"):
            if key not in q["contract"]:
                raise ValueError(f"question contract lacks {key}")


def prepare(config, *, mode="live"):
    config = deepcopy(config)
    from .resilience import enabled as automatic_recovery
    automatic = automatic_recovery(config)
    if "fixture_accounting" in config:
        from .accounting import FIXTURE_PROFILE
        if mode != "fixture" or config["fixture_accounting"] != FIXTURE_PROFILE:
            raise ValueError("rate simulation requires the explicit fixture-only accounting profile")
    if automatic:
        for rule in config["execution_policy"]["scheduling"].get("recovery", {}).values():
            rule["transient_pause_action"] = "cooldown"
    if config.get("execution_stage") not in (None, "collection-only-v1"):
        raise ValueError("unknown execution stage")
    collection_only = config.get("execution_stage") == "collection-only-v1"
    if collection_only and any(key in config for key in ("fresh_judging", "codex_concurrency_trial", "subscription_health_transition")):
        raise ValueError("provider integration cannot activate a judge epoch or concurrency trial")
    if "benchmark_continuation_reuse" in config and (collection_only or "reuse_snapshot" not in config):
        raise ValueError("benchmark continuation requires an explicit saved collection and benchmark stage")
    if "scale_expansion" in config and (collection_only or "reuse_snapshot" not in config or config.get("reuse_scope") != "exact-method-expansion-v1"):
        raise ValueError("scale expansion requires an explicit exact-method snapshot and benchmark stage")
    questions = config.pop("questions", None)
    if questions is None:
        questions = list(records(config.pop("questions_path")))
    validate_questions(questions)
    selected = config.pop("provider_ids")
    providers = selected_providers(config, selected)
    from .blind_input import LIVE_PROFILE
    judge_profile = config.get("judge_input_profile")
    if judge_profile is not None:
        if judge_profile != LIVE_PROFILE:
            raise ValueError("unsupported explicit judge input profile")
        from .date_trust import read as read_trust, apply as apply_trust
        trust = read_trust(config["date_trust_package"], providers)
        providers = apply_trust(providers, trust)
        for provider in providers:
            provider["judge_input_profile"] = judge_profile
    elif "date_trust_package" in config:
        raise ValueError("owner date trust requires its explicit judge input profile")
    reviewers = config["reviewers"]
    if set(reviewers) != {"codex", "claude"}:
        raise ValueError("both independent reviewer roles must be explicitly configured")
    for role, reviewer in reviewers.items():
        allowed = {f"{role}-subscription", "openai-api", "anthropic-api"}
        if reviewer.get("backend") not in allowed or not reviewer.get("model"):
            raise ValueError("reviewer requires an explicit supported backend/model")
        if reviewer["backend"] == "codex-subscription" and reviewer.get("transport") not in {"app-server-stdio-v1", "serial-cli-v1"}:
            raise ValueError("Codex subscription requires explicit app-server-stdio-v1 or serial-cli-v1 transport")
        if reviewer["timeout_seconds"] <= 0:
            raise ValueError("reviewer timeout must be positive")
        if reviewer["backend"].endswith("-api"):
            for key in ("secret_ref", "max_output_tokens", "price_input_per_million_usd", "price_output_per_million_usd"):
                if key not in reviewer:
                    raise ValueError(f"explicit API reviewer lacks {key}")
            if type(reviewer["max_output_tokens"]) is not int or reviewer["max_output_tokens"] <= 0:
                raise ValueError("API output-token bound must be a positive integer")
            for key in ("price_input_per_million_usd", "price_output_per_million_usd"):
                rate = Decimal(str(reviewer[key]))
                if not rate.is_finite() or rate < 0:
                    raise ValueError("API accounting rates must be finite and nonnegative")
    if judge_profile is not None:
        from .profile_authority import read as read_authority, verify_method_config
        method_path = config.pop("method_acceptance_package", None)
        if method_path is not None:
            authority = read_authority(method_path, reviewers)
            if "live_profile_authority" in config and config["live_profile_authority"] != authority:
                raise ValueError("method acceptance references disagree")
            config["live_profile_authority"] = authority
        verify_method_config(config, selected, mode=mode)
    from .quota import validate as validate_quota
    validate_quota(config.get("subscription_quota"), reviewers)
    policy = config["execution_policy"]
    if policy.get("budget_accounting") not in (None, "source-native-usage-v1", "source-native-cache-usage-v1"):
        raise ValueError("unknown budget accounting profile")
    from .caching import validate as validate_cache
    validate_cache(config.get("system_cache"), policy)
    if policy.get("budget_accounting") == "source-native-cache-usage-v1" and "system_cache" not in config:
        raise ValueError("cache accounting requires a frozen cache policy")
    if "fresh_judging" in config and "reuse_snapshot" in config:
        raise ValueError("choose exact-task reuse or an explicit fresh-judging epoch")
    if 'cache_expiry_recovery'in config and 'reuse_snapshot'not in config:
        raise ValueError('cache-expiry recovery requires its exact saved-work snapshot')
    if config.get("reuse_scope") not in (None, "exact-method-expansion-v1") or "reuse_scope" in config and "reuse_snapshot" not in config:
        raise ValueError("expanded reuse requires an exact history snapshot")
    if "prior_work" in config and "fresh_judging" in config:
        raise ValueError("prior obligations cannot be added to a fresh epoch")
    for key in ("spending_ceiling_usd", "timeout_seconds", "max_attempts_per_task", "retry_policy", "repair_branch", "scheduling"):
        if key not in policy:
            raise ValueError(f"execution policy lacks {key}")
    bindings = account_bindings(providers, reviewers, METHOD["primary"])
    if "reuse_snapshot" in config and (collection_only or "benchmark_continuation_reuse" in config):
        from .reuse import read_snapshot
        reference = config["reuse_snapshot"]
        read_snapshot(reference)
        source = load(Path(reference["manifest_path"]).parent / "source-package.json")
        # Old requests still own pacing/health/accounting, including providers
        # outside the selected roster. None of those bindings authorizes a send.
        bindings = dict(source["account_bindings"], **bindings)
    validate_scheduling(policy["scheduling"], bindings, reviewers)
    from .subscription_health import validate as validate_subscription_health
    validate_subscription_health(config.get("subscription_completion_health"), reviewers, policy["scheduling"])
    from .concurrency import validate as validate_trial
    validate_trial(config.get("codex_concurrency_trial"), policy["scheduling"], reviewers, METHOD["pool_depth"])
    ceiling = Decimal(str(policy["spending_ceiling_usd"]))
    if not ceiling.is_finite() or ceiling <= 0 or type(policy["max_attempts_per_task"]) is not int or policy["max_attempts_per_task"] < 1:
        raise ValueError("spending/attempt bounds must be positive")
    retry = policy["retry_policy"]
    if not isinstance(retry, dict) or set(retry) != {"max_total", "max_search", "max_model", "http_statuses", "backoff_seconds"}:
        raise ValueError("retry policy must explicitly bind total/search/model counts, HTTP statuses and backoff")
    backoffs = retry["backoff_seconds"]
    if any(retry[k] is not None and (type(retry[k]) is not int or retry[k] < 0) for k in ("max_total", "max_search", "max_model")):
        raise ValueError("retry bounds must be nonnegative")
    delays = backoffs if isinstance(backoffs, list) else [backoffs]
    if any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in delays) or isinstance(backoffs, list) and len(backoffs) < policy["max_attempts_per_task"]-1:
        raise ValueError("finite retry backoff coverage required")
    if not isinstance(retry["http_statuses"], list) or any(type(status) is not int or not 400 <= status <= 599 for status in retry["http_statuses"]):
        raise ValueError("retry statuses must be completed HTTP failure codes")
    if not isinstance(policy["repair_branch"], dict):
        raise ValueError("repair branch must be an explicit conditional package")
    if not 0 < config["coverage_target"] <= 1:
        raise ValueError("coverage target must be in (0,1]")
    if sorted(config["tie_order"]) != sorted(selected):
        raise ValueError("availability tie order must match selected configurations")
    from .request_profiles import validate_start_profiles
    date_profiles = config.get("provider_date_profiles", {})
    validate_start_profiles(providers, date_profiles)
    search_ids = config.get("search_provider_ids", selected)
    if (not isinstance(search_ids, list) or not search_ids or len(set(search_ids)) != len(search_ids)
            or not set(search_ids).issubset(selected)
            or search_ids != [cid for cid in selected if cid in search_ids]):
        raise ValueError("search execution scope must be a distinct ordered subset of the method roster")
    requests = [build_request(provider, q, date_profile=date_profiles.get(provider["configuration_id"]))
                for q in questions for provider in providers if provider["configuration_id"] in search_ids]
    for request in requests:
        provider = next(p for p in providers if p["configuration_id"] == request["configuration_id"])
        if provider.get("body_secret_refs"):
            request["body_secret_refs"] = provider["body_secret_refs"]
    if "reuse_snapshot" in config:
        # Collection keeps its original revision metadata. Only the grading
        # projection changes; every native search input must remain identical.
        from .reuse import read_snapshot
        reference = config["reuse_snapshot"]
        read_snapshot(reference)
        source = load(Path(reference["manifest_path"]).parent / "source-package.json")
        def wire_identity(rows):
            return [{k: v for k, v in row.items() if k != "revision"} for row in rows]
        if "question_projection_compatibility" in config and wire_identity(requests) != wire_identity(source["search_requests"]):
            raise ValueError("geography projection changed native search inputs")
        old = {(row["configuration_id"], row["question_id"]): row for row in source["search_requests"]}
        requests = [deepcopy(old[row["configuration_id"], row["question_id"]])
                    if (row["configuration_id"], row["question_id"]) in old and
                    wire_identity([row]) == wire_identity([old[row["configuration_id"], row["question_id"]]])
                    else row for row in requests]
    searched_providers = [p for p in providers if p["configuration_id"] in search_ids]
    required_refs = {p["auth"]["secret_ref"] for p in searched_providers}
    required_refs.update(ref for p in searched_providers for ref in p.get("body_secret_refs", {}).values())
    if not collection_only:
        required_refs.add(METHOD["primary"]["secret_ref"])
        required_refs.update(r["secret_ref"] for r in reviewers.values() if r["backend"].endswith("-api"))
    prompt_profiles = []
    baseline_model_reservations = []
    baseline_search_reservations = []
    accounting_package = dict(mode=mode, providers=providers, primary=METHOD["primary"],
        configuration=config)
    empty_position = dict(status="native_result", title=dict(state="missing"), content=dict(state="missing"),
                          url=dict(state="missing"), metadata={}, additional_content={}, native_dates=dict(observations=[]))
    for question in questions:
        packet = packet_for(question, providers[0], empty_position, "runtime-occurrence-id")
        compiled = compile_packet(packet, "2000-01-01T00:00:00Z", config=providers[0] if judge_profile else None)
        from .judging import blind_packet
        visible = blind_packet(compiled)
        prompt_profiles.append(dict(question_id=question["question_id"], prompt=visible["prompt"],
                                    prompt_sha256=visible["prompt_sha256"]))
        for provider in searched_providers:
            baseline_search_reservations.append(reservation(dict(kind="search", configuration_id=provider["configuration_id"]), accounting_package))
            if collection_only:
                continue
            full_packet = packet_for(question, provider, empty_position, "occurrence-" + "0" * 64)
            example = compile_packet(full_packet, "2000-01-01T00:00:00Z", config=provider)
            per_role = [reservation(dict(kind="model", role="pro", backend="gemini-api", wire=build_pro_request(example)), accounting_package)]
            for role, reviewer in reviewers.items():
                per_role.append(reservation(dict(kind="model", role=role, backend=reviewer["backend"],
                                                 backend_config=reviewer, blind_packet=blind_packet(example) if judge_profile else
                                                 {k: example[k] for k in ("prompt", "input", "schema")}), accounting_package))
            baseline_model_reservations.extend(per_role * METHOD["pool_depth"])
    base_model = sum(baseline_model_reservations, Decimal(0))
    base_search = sum(baseline_search_reservations, Decimal(0))
    def retry_reserve(kind, amounts, base):
        bounds = [v for v in (retry["max_total"], retry["max_"+kind]) if v is not None]
        return max(amounts, default=Decimal(0))*min(bounds) if bounds else base*(policy["max_attempts_per_task"]-1)
    retry_model = retry_reserve("model", baseline_model_reservations, base_model)
    retry_search = retry_reserve("search", baseline_search_reservations, base_search)
    baseline_total = base_model + base_search + retry_model + retry_search
    package = dict(schema_version=1, mode=mode, questions=questions,
                question_bindings=[dict(question_id=q["question_id"], revision=q["revision"],
                                        contract_sha256=digest(q["contract"]), question_sha256=digest(q)) for q in questions],
                providers=providers, search_requests=requests, configuration=config,
                account_bindings=bindings,
                primary=METHOD["primary"], assets=asset_bindings(), code=code_bindings(),
                prompt_profiles=prompt_profiles,
                schema_plan=dict(schema_profile=METHOD["schema_profile"], decoder_profile=METHOD["decoder_profile"],
                                 conformance_schema=METHOD["conformance_schema"],
                                 dynamic_bindings="case ID, permitted field names and own/native date IDs are generated only from each retained native occurrence; generator code and all rules are hash-bound; exact expanded schema is retained before dispatch"),
                reservation_forecast=dict(basis="exact selected contracts/backends with empty-evidence surrogate packets, full output limits and every conditional third position; NOT a worst-case bound on unknown native evidence",
                    base_model_usd=str(base_model), base_search_usd=str(base_search),
                    covered_retry_estimate_usd=str(retry_model + retry_search), baseline_total_usd=str(baseline_total),
                    admission_ceiling_usd=str(ceiling), baseline_fits=baseline_total <= ceiling,
                    base_without_retries_usd=str(base_model+base_search), base_without_retries_fits=base_model+base_search <= ceiling,
                    warning="Actual native evidence/schema size is unknown before collection. No finite worst-case is asserted; no evidence truncation is introduced. A ceiling can stop the run. Subscription usage/CLI internal retries are not an API invoice."),
                required_credential_references=sorted(required_refs),
                scope=dict(search_cells=len(requests), result_slots=len(requests) * METHOD["pool_depth"],
                           primary_tasks=len(requests) * METHOD["pool_depth"],
                           second_tasks=len(requests) * METHOD["pool_depth"],
                           conditional_third_tasks=len(requests) * METHOD["pool_depth"],
                           all_model_tasks=len(requests) * METHOD["pool_depth"] * METHOD["max_positions_per_occurrence"]),
                retention="full permitted private native inputs/outputs/attempts; no automatic deletion; credentials excluded",
                approval_state="requires_separate_owner_receipt" if mode == "live" else "synthetic_offline_only")
    if judge_profile is not None:
        from .request_profiles import prepare_dates as prepare_date_capabilities
        package["date_capabilities"] = prepare_date_capabilities(providers, questions, profiles=date_profiles)
    if config.get("interrupted_run_recovery"):
        from .interruption import validate as validate_interruption
        validate_interruption(package)
    if collection_only:
        package["scope"].update(primary_tasks=0, second_tasks=0, conditional_third_tasks=0, all_model_tasks=0,
                                purpose="provider integration only; no judgments or quality scores")
    if "reuse_snapshot" in config:
        from .reuse import read_snapshot
        snapshot = read_snapshot(config["reuse_snapshot"], package)
        stats = snapshot["statistics"]
        package["reservation_forecast"].update(
            carried_reservation_usd=stats["carried_reservation_usd"],
            remaining_admission_usd=str(ceiling - Decimal(stats["carried_reservation_usd"])),
            reused_started_attempts=stats["started_attempts"],
            continuation_basis="The ceiling covers the entire lineage. Base estimates describe full scope, not additional spend; completed tasks are not dispatched again.")
        if config.get('cache_expiry_recovery'):
            from .reuse import committed
            root=Path(config['reuse_snapshot']['manifest_path']).parent
            carried=committed(root,load(root/'source-package.json'),list(records(root/'events.jsonl')))
            package['reservation_forecast'].update(carried_reservation_usd=str(carried),
                remaining_admission_usd=str(ceiling-carried),
                continuation_basis='Whole-lineage native usage and retained holds; completed successes are reused, not new spend.')
        if collection_only:
            root = Path(config["reuse_snapshot"]["manifest_path"]).parent
            history = list(records(root / "events.jsonl"))
            started = {e["task_id"] for e in history if e["event"] == "started"}
            completed = {e["task_id"]: e for e in history if e["event"] == "finished"}
            needed = set()
            pending_prices = []
            for request in requests:
                task = "search-" + digest(dict(configuration=request["configuration_id"], question=request["question_id"]))
                send = task not in started
                if task in completed:
                    result = load(root / completed[task]["result_path"])
                    send = (result.get("complete") is True and result.get("transport_success") is False
                            and result.get("dispatch_state") != "uncertain"
                            and result.get("http_status") in policy["retry_policy"]["http_statuses"])
                if send:
                    provider = next(p for p in providers if p["configuration_id"] == request["configuration_id"])
                    needed.add(provider["auth"]["secret_ref"])
                    needed.update(provider.get("body_secret_refs", {}).values())
                    pending_prices.append(reservation(dict(kind="search", configuration_id=request["configuration_id"]), accounting_package))
            package["required_credential_references"] = sorted(needed)
            from .reuse import committed
            current = committed(root, source, history)
            new_base = sum(pending_prices, Decimal(0))
            new_retries = retry_reserve("search", pending_prices, new_base)
            package["reservation_forecast"].update(
                basis="Saved attempts settle at their frozen native-usage rules; only remaining provider requests are forecast. No model work or cache lifecycle calls.",
                historical_maximum_reservations_usd=stats["carried_reservation_usd"],
                carried_reservation_usd=str(current), carried_committed_and_held_usd=str(current),
                remaining_admission_usd=str(ceiling-current),
                base_model_usd="0", base_search_usd=str(new_base), covered_retry_estimate_usd=str(new_retries),
                baseline_total_usd=str(current+new_base+new_retries), baseline_fits=current+new_base+new_retries <= ceiling,
                base_without_retries_usd=str(current+new_base), base_without_retries_fits=current+new_base <= ceiling,
                remaining_search_tasks=len(pending_prices))
    from .concurrency import validate_saved_cohorts
    validate_saved_cohorts(package)
    if "subscription_health_transition" in config:
        from .subscription_health import transition_attempts
        transition_attempts(package)
    if "fresh_judging" in config:
        from .fresh import planned_requests, validate
        manifest, captures = validate(config["fresh_judging"], package)
        package["required_credential_references"] = sorted(
            {METHOD["primary"]["secret_ref"]} |
            {r["secret_ref"] for r in reviewers.values() if r["backend"].endswith("-api")})
        package["scope"].update(new_search_tasks=0,
                                purpose=manifest["purpose"])
        expanded = list(planned_requests(package, captures))
        for role, name in (("pro", "dispatchable_primary_tasks"), ("codex", "dispatchable_second_tasks"),
                           ("claude", "maximum_dispatchable_third_tasks")):
            package["scope"][name] = sum(request["role"] == role for _, request in expanded)
        package["scope"]["maximum_new_model_tasks"] = len(expanded)
        amounts = [reservation(request, package) for _, request in expanded]
        base = sum(amounts, Decimal(0))
        retries = retry_reserve("model", amounts, base)
        carried = Decimal(manifest["summary"]["carried_cost_usd"])
        package["reservation_forecast"] = dict(
            basis="Exact saved native evidence, current expanded requests, full output limits and every dispatchable conditional third position; not an invoice guarantee",
            request_bindings=[dict(task_id=task, request_sha256=digest(request), reservation_usd=str(amount))
                              for (task, request), amount in zip(expanded, amounts)],
            base_model_usd=str(base), base_search_usd="0", covered_retry_estimate_usd=str(retries),
            baseline_total_usd=str(carried + base + retries), admission_ceiling_usd=str(ceiling),
            baseline_fits=carried + base + retries <= ceiling,
            base_without_retries_usd=str(carried + base), base_without_retries_fits=carried + base <= ceiling,
            carried_committed_and_held_usd=str(carried),
            remaining_admission_usd=str(ceiling - carried),
            warning="Known completed Pro usage settles at frozen rates without assumed cache credit; unknown/uncertain usage retains the full maximum. Retries still need budget admission; the ceiling can stop a partial run. Subscriptions are outside this API ceiling.",
            fresh_judging="New source-method positions only; no old vote, no provider call; source uncertainty remains no-send")
    if "system_cache" in config and not collection_only:
        from .caching import plan
        package["cache_plan"] = plan(package)
        package["reservation_forecast"]["cache_estimated_lifecycle_usd"] = str(sum(
            (Decimal(row["estimated_lifecycle_usd"]) for row in package["cache_plan"] if row["eligible"]), Decimal(0)))
    if "prior_work" in config:
        from .prior_work import read
        prior = read(config["prior_work"], package)
        package["reservation_forecast"]["prior_committed_and_held_usd"] = prior["committed_and_held_usd"]
        package["reservation_forecast"]["remaining_after_prior_obligations_usd"] = str(ceiling - Decimal(prior["committed_and_held_usd"]))
        if collection_only:
            forecast = package["reservation_forecast"]
            inherited = Decimal(prior["committed_and_held_usd"])
            carried = Decimal(forecast.get("carried_committed_and_held_usd", "0")) + inherited
            baseline = Decimal(forecast["baseline_total_usd"]) + inherited
            base = Decimal(forecast["base_without_retries_usd"]) + inherited
            forecast.update(carried_committed_and_held_usd=str(carried), remaining_admission_usd=str(ceiling-carried),
                remaining_after_prior_obligations_usd=str(ceiling-carried), baseline_total_usd=str(baseline),
                baseline_fits=baseline <= ceiling, base_without_retries_usd=str(base), base_without_retries_fits=base <= ceiling)
    if "scale_expansion" in config:
        from .scale import prepare_forecast
        prepare_forecast(package)
    if "benchmark_continuation_reuse" in config and "scale_expansion" not in config:
        prepare_judging_continuation(package)
    return package


def prepare_judging_continuation(package):
    """Derive the exact remaining workload without dispatch or source mutation."""
    from collections import Counter
    from . import judging, review
    from .common import encoded
    from .caching import payload
    from ._method.providers import project_response
    from .reuse import committed
    config = package["configuration"]
    root = Path(config["reuse_snapshot"]["manifest_path"]).parent
    source = load(root / "source-package.json")
    events = list(records(root / "events.jsonl"))
    starts = {e["task_id"]: e for e in events if e["event"] == "started"}
    latest = {e["task_id"]: e for e in events if e["event"] == "finished"}
    attempts = Counter(e["task_id"] for e in events if e["event"] == "started")
    used_retries = sum(n-1 for n in attempts.values())
    used_model_retries = sum(n-1 for task, n in attempts.items() if task.startswith(("pro-", "codex-", "claude-")))
    policy = config["execution_policy"]
    retry = policy["retry_policy"]
    retry_bounds = [max(0, retry[key]-used) for key, used in (("max_total", used_retries), ("max_model", used_model_retries)) if retry[key] is not None]
    retry_available = not retry_bounds or min(retry_bounds) > 0
    counts, bindings, prices, retry_prices, needed, cases, new_pro_groups = Counter(), [], [], [], set(), set(), set()
    for search in package["search_requests"]:
        cid, qid = search["configuration_id"], search["question_id"]
        provider = next(p for p in package["providers"] if p["configuration_id"] == cid)
        question = next(q for q in package["questions"] if q["question_id"] == qid)
        task = "search-" + digest(dict(configuration=cid, question=qid))
        event = latest.get(task)
        if event is None or starts[task]["attempt_id"] != event["attempt_id"]:
            raise ValueError("judging continuation requires every completed saved search; no recollection")
        request = dict(search, kind="search", auth_reference=provider["auth"])
        request.update(reservation_usd=str(reservation(request, package)),
                       reservation_basis="conservative estimate; not invoice or guaranteed upper bound")
        if digest(request) != event["request_sha256"]:
            raise ValueError("saved search differs from the exact continuation request")
        raw = load(root / event["result_path"])
        if (raw.get("complete") is not True or raw.get("transport_success") is not True
                or raw.get("dispatch_state") not in {None, "completed"} or raw.get("credential_echo_redacted")):
            raise ValueError("judging continuation requires successful saved collection; inspect failed or uncertain searches")
        projected = project_response(provider, raw.get("raw_text", "").encode(), raw["captured_at"],
                                     http_status=raw.get("http_status") or 0, complete=raw["complete"])
        if projected["status"] not in {"success", "success_empty"}:
            raise ValueError("judging continuation requires an audited native provider response")
        for position in projected["positions"][:METHOD["pool_depth"]]:
            case = "occurrence-" + digest(dict(question=question, configuration=provider, position=position))
            cases.add(case)
            compiled = compile_packet(packet_for(question, provider, position, case), raw["captured_at"], config=provider)
            retained = {}
            for role in ("pro", "codex", "claude"):
                task = role + "-" + case
                reviewer = config["reviewers"].get(role)
                request = (dict(kind="model", role=role, backend="gemini-api", wire=build_pro_request(compiled)) if role == "pro" else
                           dict(kind="model", role=role, backend=reviewer["backend"], backend_config=reviewer, blind_packet=judging.blind_packet(compiled)))
                amount = reservation(request, package)
                request.update(reservation_usd=str(amount), reservation_basis="conservative estimate; not invoice or guaranteed upper bound")
                saved, started = latest.get(task), starts.get(task)
                disposition = "new" if role != "claude" else "conditional-new"
                if started:
                    if digest(request) != started["request_sha256"]:
                        raise ValueError("saved judgment differs from the exact continuation input")
                    disposition = "held-uncertain"
                    if saved and saved["attempt_id"] == started["attempt_id"]:
                        response = load(root / saved["result_path"])
                        if response.get("dispatch_state") != "uncertain":
                            disposition = "retained-no-send"
                            if (retry_available and attempts[task] < policy["max_attempts_per_task"]
                                    and response.get("complete") is True and response.get("transport_success") is False
                                    and response.get("http_status") in retry["http_statuses"]):
                                disposition = "covered-retry"
                            else:
                                retained[role] = (judging.response_result(response.get("raw_text", "").encode(), compiled,
                                    http_status=response.get("http_status") or 0, complete=response.get("complete", False)) if role == "pro" else
                                    judging.decode_position(response.get("verdict"), compiled) if response.get("transport_success") else dict(state="unjudged"))
                if role == "claude" and "pro" in retained and "codex" in retained and not review.needs_third(retained["pro"], retained["codex"]):
                    disposition = "not-in-fixed-route"
                maximum_new_attempts = 0
                if disposition in {"new", "conditional-new", "covered-retry"}:
                    maximum_new_attempts = policy["max_attempts_per_task"] - attempts[task]
                    if retry_bounds:
                        maximum_new_attempts = min(maximum_new_attempts, min(retry_bounds) + (disposition != "covered-retry"))
                    if disposition == "covered-retry":
                        retry_prices.extend([amount] * maximum_new_attempts)
                    else:
                        prices.append(amount)
                        retry_prices.extend([amount] * (maximum_new_attempts-1))
                    if role == "pro":
                        needed.add(METHOD["primary"]["secret_ref"])
                        new_pro_groups.add(digest(payload(request["wire"])))
                    elif reviewer["backend"].endswith("-api"):
                        needed.add(reviewer["secret_ref"])
                counts[role + "/" + disposition] += 1
                bindings.append(dict(task_id=task, case_id=case, role=role, question_id=qid, configuration_id=cid,
                    rank=position["rank"], request_sha256=digest(request), reservation_usd=str(amount), disposition=disposition,
                    maximum_new_attempts=maximum_new_attempts))
    carried = committed(root, source, events)
    if config.get("prior_work"):
        from .prior_work import read
        carried += Decimal(read(config["prior_work"], package)["committed_and_held_usd"])
    base = sum(prices, Decimal(0))
    retries = sum(sorted(retry_prices, reverse=True)[:min(retry_bounds) if retry_bounds else len(retry_prices)], Decimal(0))
    # Inherited expired/deleted resources never recreate. Reserve new lifecycle
    # work only for eligible groups with unsent Pro work and no saved create.
    new_groups = set()
    lifecycle = Decimal(0)
    cache = config.get("system_cache")
    if cache:
        for group in package.get("cache_plan", []):
            if not group["eligible"] or "cache-create-" + group["group_id"] in starts or group["group_id"] not in new_pro_groups:
                continue
            tokens = len(encoded(group["payload"])) + 1024
            rates = package["primary"]["accounting_rates"]
            lifecycle += Decimal(tokens) * (Decimal(rates["input_per_million_usd"]) +
                Decimal(str(cache["storage_per_million_token_hour_usd"])) * cache["ttl_seconds"] / 3600) / 1_000_000
            new_groups.add(group["group_id"])
    ceiling = Decimal(str(config["execution_policy"]["spending_ceiling_usd"]))
    package["continuation_plan"] = dict(profile="provider-integration-benchmark-v1", counts=dict(counts),
        current_occurrences=len(cases), reused_searches=len(package["search_requests"]), new_search_calls=0,
        new_cache_group_ids=sorted(new_groups), request_bindings=bindings)
    package["required_credential_references"] = sorted(needed)
    package["scope"].update(new_search_tasks=0, exact_saved_occurrences=len(cases),
        dispatchable_primary_tasks=counts["pro/new"]+counts["pro/covered-retry"], dispatchable_second_tasks=counts["codex/new"]+counts["codex/covered-retry"],
        maximum_dispatchable_third_tasks=counts["claude/conditional-new"]+counts["claude/covered-retry"])
    package["reservation_forecast"] = dict(basis="Exact saved collection and request identities; unsent judgments, covered completed HTTP retries and every possible fixed-route third position. No search dispatch; successful/invalid/uncertain completed judgments are never resent.",
        carried_committed_and_held_usd=str(carried), historical_maximum_reservations_usd=str(sum((Decimal(load(root / 'attempts' / e['attempt_id'] / 'request.json')["reservation_usd"]) for e in events if e["event"] == "started"), Decimal(0))),
        base_model_usd=str(base), base_search_usd="0", covered_retry_estimate_usd=str(retries), new_cache_maximum_reservations_usd=str(lifecycle),
        baseline_total_usd=str(carried + base + retries + lifecycle), baseline_fits=carried + base + retries + lifecycle <= ceiling,
        base_without_retries_usd=str(carried + base + lifecycle), base_without_retries_fits=carried + base + lifecycle <= ceiling,
        admission_ceiling_usd=str(ceiling), remaining_admission_usd=str(ceiling-carried),
        reused_started_attempts=sum(e["event"] == "started" for e in events),
        warning="Conservative reservations, not an invoice or time forecast. Existing uncertainty and stops remain unless a separate exact health transition is approved. Subscriptions are outside the API ceiling; cache discounts are not assumed.")


def verify_package(package):
    if package["assets"] != asset_bindings() or package["code"] != code_bindings():
        raise ValueError("package code or method assets changed; prepare a new package")
    if package["schema_version"] != 1 or package["mode"] not in {"live", "fixture"}:
        raise ValueError("unsupported package schema or mode")
    rebuilt_config = dict(package["configuration"], questions=package["questions"],
                          provider_ids=[p["configuration_id"] for p in package["providers"]])
    if prepare(rebuilt_config, mode=package["mode"]) != package:
        raise ValueError("prepared package differs from its explicit source inputs")


def require_approval(package, receipt):
    verify_package(package)
    if package["mode"] != "live":
        raise ValueError("fixture package cannot authorize live execution")
    if receipt.get("package_sha256") != digest(package) or receipt.get("decision") != "approved":
        raise ValueError("owner approval does not match the exact execution package")
    if not receipt.get("approved_by") or not receipt.get("approved_at"):
        raise ValueError("owner approval must identify the approver and time")
    return True
