"""Exact pilot-to-full-scope migration; no changed judge input or vote alias."""
from copy import deepcopy
from pathlib import Path
from .common import digest, load, records

PROFILE = "completed-pilot-expansion-v1"


def compatible(source, target):
    config = target["configuration"]
    proof = config.get("scale_expansion")
    adopted = source["configuration"].get("scale_expansion")
    if adopted:
        if proof != adopted:
            raise ValueError("continuation changed adopted scale promotion")
        old, new = source["configuration"], config
        mutable = {"run_directory", "reuse_snapshot", "execution_policy"}
        if {k:v for k,v in old.items() if k not in mutable} != {k:v for k,v in new.items() if k not in mutable}:
            raise ValueError("adopted scale continuation changed frozen configuration")
        if {k:v for k,v in old["execution_policy"].items() if k != "spending_ceiling_usd"} != {k:v for k,v in new["execution_policy"].items() if k != "spending_ceiling_usd"}:
            raise ValueError("adopted scale continuation changed recovery/retry/accounting policy")
        return False  # Exact reuse owns later budgets, never a reset of actual stops.
    if proof is None:
        return False
    keys = {"profile", "source_package_sha256", "source_code_sha256", "target_code_sha256",
            "source_assets_sha256", "target_assets_sha256", "completed_trial_sha256", "codex_concurrency", "cache_epoch"}
    if (not isinstance(proof, dict) or set(proof) != keys or proof["profile"] != PROFILE
            or config.get("reuse_scope") != "exact-method-expansion-v1"
            or source["configuration"].get("execution_stage") is not None
            or config.get("execution_stage") is not None
            or proof["source_package_sha256"] != digest(source)
            or proof["source_code_sha256"] != digest(source["code"])
            or proof["target_code_sha256"] != digest(target["code"])
            or proof["source_assets_sha256"] != digest(source["assets"])
            or proof["target_assets_sha256"] != digest(target["assets"])):
        raise ValueError("scale expansion lacks exact source/target bindings")
    if any(source[k] != target[k] for k in ("mode", "primary", "assets", "providers", "schema_plan")):
        raise ValueError("scale expansion changed method, providers or model accounting")
    allowed_code = {"scale.py", "quota.py", "reuse.py", "preparation.py", "runner.py", "scheduling.py", "caching.py", "codex_runtime.py"}
    changed = {k for k in source["code"].keys() | target["code"].keys() if source["code"].get(k) != target["code"].get(k)}
    if not changed <= allowed_code:
        raise ValueError("scale expansion changed code outside orchestration")
    for key, identity in (("questions", "question_id"), ("question_bindings", "question_id"),
                          ("prompt_profiles", "question_id")):
        rows = {q[identity]: q for q in target[key]}
        if any(rows.get(q[identity]) != q for q in source[key]):
            raise ValueError("scale expansion changed a paid question/prompt binding")
    requests = {(r["question_id"], r["configuration_id"]): r for r in target["search_requests"]}
    if any(requests.get((r["question_id"], r["configuration_id"])) != r for r in source["search_requests"]):
        raise ValueError("scale expansion changed a paid search")
    old, new = source["configuration"], config
    mutable = {"run_directory", "reuse_snapshot", "reuse_scope", "scale_expansion", "codex_concurrency_trial", "system_cache", "execution_policy", "subscription_quota"}
    if {k:v for k,v in old.items() if k not in mutable} != {k:v for k,v in new.items() if k not in mutable}:
        raise ValueError("scale expansion changed adopted provenance or reviewer settings")
    a, b = old["execution_policy"], new["execution_policy"]
    if {k:v for k,v in a.items() if k not in {"spending_ceiling_usd", "scheduling"}} != {k:v for k,v in b.items() if k not in {"spending_ceiling_usd", "scheduling"}}:
        raise ValueError("scale expansion changed retry/accounting policy")
    expected = deepcopy(a["scheduling"])
    capacity = proof["codex_concurrency"]
    expected["accounts"]["codex-subscription"]["concurrency"] = capacity
    expected["recovery"]["codex-subscription"]["initial_concurrency"] = capacity
    # Preserve the tested reduced capacity and all real error/stop thresholds.
    if expected != b["scheduling"]:
        raise ValueError("scale expansion changed scheduling beyond tested pool promotion")
    cache = dict(old["system_cache"], renewal=dict(profile="expiry-leases-v1", epoch=proof["cache_epoch"]))
    if cache != new.get("system_cache"):
        raise ValueError("scale expansion changed frozen cache rates or payload estimates")
    if "codex_concurrency_trial" in new:
        raise ValueError("completed trial cannot remain an active full-run barrier")
    trial = old.get("codex_concurrency_trial") or {}
    if capacity != trial.get("stages", [{}])[-1].get("concurrency"):
        raise ValueError("pool exceeds the completed pilot stage")
    from .subscription_health import verified
    root = Path(config["reuse_snapshot"]["manifest_path"]).parent
    latest = {e["task_id"]:e for e in records(root / "events.jsonl") if e["event"] == "finished"}
    bindings = []
    for case, stage in trial.get("remaining_case_cohorts", {}).items():
        event = latest.get("codex-" + case)
        if not event:
            raise ValueError("pilot pool promotion lacks a completed case")
        folder = root / "attempts" / event["attempt_id"]
        raw, request = load(root / event["result_path"]), load(folder / "request.json")
        measurement = raw.get("concurrency_measurement") or {}
        if (measurement.get("stage") != stage or measurement.get("configured_concurrency") != trial["stages"][stage]["concurrency"]
                or raw.get("native_quota_evidence") or not verified(request, raw, folder)):
            raise ValueError("pilot pool promotion lacks clean native completion evidence")
        bindings.append(dict(task_id=event["task_id"], result_sha256=event["result_sha256"]))
    if (not bindings or set(trial["remaining_case_cohorts"].values()) != set(range(len(trial["stages"])))
            or digest(sorted(bindings, key=lambda r:r["task_id"])) != proof["completed_trial_sha256"]):
        raise ValueError("pool promotion differs from the frozen completed trial")
    return True


def prepare_forecast(package):
    """Exact saved cells plus additive unknown-evidence scope, without sends."""
    from decimal import Decimal
    from .preparation import prepare_judging_continuation
    from .accounting import reservation
    from .judging import METHOD, packet_for, compile_packet, build_pro_request
    root = Path(package["configuration"]["reuse_snapshot"]["manifest_path"]).parent
    source = load(root / "source-package.json")
    events = list(records(root / "events.jsonl"))
    starts = {e["task_id"]:e for e in events if e["event"] == "started"}
    latest = {e["task_id"]:e for e in events if e["event"] == "finished"}
    saved, pending = [], []
    for row in package["search_requests"]:
        task = "search-" + digest(dict(configuration=row["configuration_id"], question=row["question_id"]))
        event = latest.get(task)
        raw = load(root / event["result_path"]) if event else {}
        if event and starts[task]["attempt_id"] == event["attempt_id"] and raw.get("complete") is True and raw.get("transport_success") is True and raw.get("dispatch_state") != "uncertain" and not raw.get("credential_echo_redacted"):
            saved.append(row)
        else:
            pending.append(row)
    known = dict(package, search_requests=saved, scope=dict(package["scope"]))
    prepare_judging_continuation(known)
    policy = package["configuration"]["execution_policy"]
    forecast = dict(known["reservation_forecast"])
    empty = dict(status="native_result", title=dict(state="missing"), content=dict(state="missing"), url=dict(state="missing"), metadata={}, additional_content={}, native_dates=dict(observations=[]))
    model = Decimal(0)
    search = Decimal(0)
    for row in pending:
        provider = next(p for p in package["providers"] if p["configuration_id"] == row["configuration_id"])
        question = next(q for q in package["questions"] if q["question_id"] == row["question_id"])
        example = compile_packet(packet_for(question, provider, empty, "occurrence-" + "0"*64), "2000-01-01T00:00:00Z", config=provider)
        model += reservation(dict(kind="model", role="pro", backend="gemini-api", wire=build_pro_request(example)), package) * METHOD["pool_depth"]
        search += reservation(dict(kind="search", configuration_id=provider["configuration_id"]), package)
    forecast["base_model_usd"] = str(Decimal(forecast["base_model_usd"]) + model)
    forecast["base_search_usd"] = str(search)
    # Existing retry policy allows up to two completed-HTTP retries per task.
    retries = Decimal(forecast["covered_retry_estimate_usd"]) + (model+search)*(policy["max_attempts_per_task"]-1)
    carried = Decimal(forecast["carried_committed_and_held_usd"])
    base = carried + Decimal(forecast["base_model_usd"]) + search
    ceiling = Decimal(str(policy["spending_ceiling_usd"]))
    forecast.update(basis="Exact retained native cells and paid request identities plus empty-evidence surrogate reservations for additive cells; no invoice or finite unknown-input worst-case claim.",
        covered_retry_estimate_usd=str(retries), baseline_total_usd=str(base+retries), baseline_fits=base+retries<=ceiling,
        base_without_retries_usd=str(base), base_without_retries_fits=base<=ceiling,
        unknown_evidence_search_cells=len(pending), renewed_cache_lifecycle="Per-lease native create/storage holds admitted separately under the same ceiling; each lease has the unchanged one-hour TTL. Scenario forecast owns expected lease count.")
    plan = known["continuation_plan"]
    package["expansion_plan"] = dict(profile=PROFILE, source_package_sha256=digest(source), retained_started_attempts=forecast["reused_started_attempts"],
        saved_search_cells=len(saved), additive_or_unavailable_search_cells=len(pending), saved_occurrences=plan["current_occurrences"], saved_judgments=plan["counts"], saved_request_bindings=plan["request_bindings"])
    package["scope"].update(new_search_tasks=len(pending), exact_saved_occurrences=plan["current_occurrences"],
        maximum_new_primary_tasks=len(pending)*METHOD["pool_depth"] + known["scope"]["dispatchable_primary_tasks"],
        maximum_new_second_tasks=len(pending)*METHOD["pool_depth"] + known["scope"]["dispatchable_second_tasks"],
        maximum_new_third_tasks=len(pending)*METHOD["pool_depth"] + known["scope"]["maximum_dispatchable_third_tasks"])
    package["reservation_forecast"] = forecast
