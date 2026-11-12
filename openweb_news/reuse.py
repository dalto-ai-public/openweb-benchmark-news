"""Explicit, private and self-contained snapshots for the same frozen work."""
from collections import Counter
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
import fcntl
import json
import os
from pathlib import Path
import re

from .common import digest, encoded, load, private_directory, records, write_private


def safe_file(root, relative):
    root = Path(root).expanduser().resolve()
    path = Path(relative)
    if path.is_absolute() or not path.parts or any(part in {".", ".."} for part in path.parts) or path.as_posix() != relative:
        raise ValueError("unsafe reuse artifact path")
    result = root / path
    component = root
    for part in path.parts:
        component = component / part
        if component.is_symlink():
            raise ValueError("reuse artifacts cannot contain symlinks")
    return result


@contextmanager
def source_lock(directory):
    path = safe_file(directory, ".lock")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def history(root, events, *, interrupted_attempts=()):
    """Validate links and derive holds from attempts, never from report totals."""
    starts, finished, counts, kinds = {}, {}, Counter(), {}
    active, uncertain = set(), set()
    latest, end_events = {}, {}
    authorized = {r["prior_attempt_id"]: r for r in interrupted_attempts}
    total = Decimal(0)
    for event in events:
        task, attempt = event["task_id"], event["attempt_id"]
        if not re.fullmatch(r"(?:search|pro-occurrence|codex-occurrence|claude-occurrence|cache-create|cache-delete)-[0-9a-f]{64}", task):
            raise ValueError("unrecognized reused task identity")
        if not re.fullmatch(r"[0-9a-f]{64}", attempt) or datetime.fromisoformat(event["recorded_at"]).tzinfo is None:
            raise ValueError("invalid reused attempt identity or timestamp")
        if event["event"] == "started":
            if task in uncertain:
                previous = latest[task]
                proof = authorized.get(previous)
                old_end = end_events[previous]
                if proof != dict(task_id=task, prior_attempt_id=previous,
                                 request_sha256=event["request_sha256"], prior_result_sha256=old_end["result_sha256"]) or starts[previous]["request_sha256"] != event["request_sha256"]:
                    raise ValueError("uncertain reused attempt has no exact named recovery")
                uncertain.remove(task)
            if attempt in starts or task in active or task in uncertain:
                raise ValueError("duplicate or uncertain reused attempt was resent")
            counts[task] += 1
            if attempt != digest(dict(task_id=task, attempt=counts[task])):
                raise ValueError("reused attempt sequence differs")
            raw = safe_file(root, f"attempts/{attempt}/request.json").read_bytes()
            request = json.loads(raw)
            if digest(raw) != event["request_sha256"] or digest(request) != event["request_sha256"]:
                raise ValueError("reused request hash differs")
            kind = request["kind"]
            if kind not in {"search", "model", "cache"}:
                raise ValueError("unsupported reused task kind")
            if kind == "search":
                expected = "search-" + digest(dict(configuration=request["configuration_id"], question=request["question_id"]))
                if task != expected:
                    raise ValueError("reused search task binding differs")
            elif kind == "cache":
                if request.get("role") != "pro" or not task.startswith(("cache-create-", "cache-delete-")):
                    raise ValueError("invalid cache lifecycle identity")
            elif request["role"] not in {"pro", "codex", "claude"} or not task.startswith(request["role"] + "-occurrence-"):
                raise ValueError("reused reviewer role differs")
            amount = Decimal(request["reservation_usd"])
            if not amount.is_finite() or amount < 0:
                raise ValueError("invalid reused spending reservation")
            total += amount
            kinds[task] = kind
            starts[attempt] = event
            latest[task] = attempt
            active.add(task)
        elif event["event"] == "finished":
            start = starts.get(attempt)
            if not start or attempt in finished or start["task_id"] != task or start["request_sha256"] != event["request_sha256"]:
                raise ValueError("reused completion has no matching start")
            if event["result_path"] != f"attempts/{attempt}/result.json":
                raise ValueError("reused result path differs")
            path = safe_file(root, event["result_path"])
            if digest(path.read_bytes()) != event["result_sha256"]:
                raise ValueError("reused result hash differs")
            result = load(path)
            if not isinstance(result, dict):
                raise ValueError("reused result is not a task response")
            finished[attempt] = result
            end_events[attempt] = event
            active.remove(task)
            if result.get("dispatch_state") == "uncertain":
                uncertain.add(task)
        else:
            raise ValueError("unknown reused ledger event")
    retries = {kind: sum(count - 1 for task, count in counts.items() if kinds[task] == kind) for kind in ("search", "model")}
    for path in (root / "attempts").glob("*/*"):
        if path.is_file() and path.parent.name not in starts and path.name not in {"request.json"} and not path.name.startswith(".writing-"):
            raise ValueError("orphan response has no started event; inspect retained evidence")
    return dict(started_attempts=len(starts), completed_attempts=len(finished),
                uncertain_attempts=sum(key not in finished or finished[key].get("dispatch_state") == "uncertain" for key in starts),
                carried_reservation_usd=str(total), retry_counts=retries,
                maximum_attempts_per_task=max(counts.values(), default=0))


def compatible(source, target, statistics):
    from .cache_recovery import compatible as cache_compatible
    cache_recovery=cache_compatible(source,target)
    from .interruption import compatible as interruption_compatible
    interrupted = interruption_compatible(source, target)
    from .scale import compatible as scale_compatible
    scale = scale_compatible(source, target)
    collection_test = compatible_collection_test(source, target)
    benchmark = compatible_benchmark_continuation(source, target)
    adopted = source["configuration"].get("benchmark_continuation_reuse")
    if adopted and adopted != target["configuration"].get("benchmark_continuation_reuse"):
        raise ValueError("continuation changed adopted benchmark migration")
    if (benchmark or adopted) and not scale and source["configuration"].get("system_cache") != target["configuration"].get("system_cache"):
        raise ValueError("benchmark continuation changed frozen cache accounting/lifecycle")
    if source["configuration"].get("execution_stage") != target["configuration"].get("execution_stage") and not (collection_test or benchmark):
        raise ValueError("continuation changed execution stage without an exact migration")
    expanded = target["configuration"].get("reuse_scope") == "exact-method-expansion-v1"
    for key in (("mode", "code", "assets", "primary") if expanded else ("mode", "code", "assets", "question_bindings", "providers", "primary", "scope")):
        if source[key] != target[key]:
            if collection_test and key in {"code", "assets", "providers", "scope"}:
                continue
            if (benchmark or scale) and key in {"code", "scope"}:
                continue
            if key == "scope" and adopted:
                derived = {"new_search_tasks", "exact_saved_occurrences", "dispatchable_primary_tasks", "dispatchable_second_tasks", "maximum_dispatchable_third_tasks"}
                if {k: v for k, v in source[key].items() if k not in derived} == {k: v for k, v in target[key].items() if k not in derived}:
                    continue
            if key == "code" and compatible_accounting_fix(source, target):
                continue
            if key == "code" and interrupted:
                continue
            if key=='code'and cache_recovery:
                continue
            if key == "question_bindings" and compatible_geography_projection(source, target):
                continue
            if key == "providers" and compatible_provider_projection(source, target):
                continue
            raise ValueError("continuation changed frozen work: " + key)
    if expanded:
        for key, identity in (("question_bindings", "question_id"), ("providers", "configuration_id")):
            rows = {row[identity]: row for row in target[key]}
            if any(rows.get(row[identity]) != row for row in source[key]) and not (key == "providers" and compatible_provider_projection(source, target)):
                raise ValueError("expansion changed or dropped frozen work: " + key)
        if source["configuration"].get("fresh_judging"):
            raise ValueError("fresh epochs require explicit prior-work cost provenance before expansion")
        for key in ("prior_work",):
            if source["configuration"].get(key) != target["configuration"].get(key):
                raise ValueError("expansion changed inherited obligations")
        old_cache, new_cache = source["configuration"].get("system_cache"), target["configuration"].get("system_cache")
        if bool(old_cache) != bool(new_cache):
            raise ValueError("expansion changed cache accounting")
        if old_cache:
            if not scale and (any(old_cache[k] != new_cache.get(k) for k in old_cache if k != "estimated_system_tokens") or any(new_cache["estimated_system_tokens"].get(k) != v for k, v in old_cache["estimated_system_tokens"].items())):
                raise ValueError("expansion changed retained cache lifecycle/rates")
    if source["configuration"]["reviewers"] != target["configuration"]["reviewers"]:
        raise ValueError("continuation changed reviewer roles/settings")
    old_transition = source["configuration"].get("subscription_health_transition")
    new_transition = target["configuration"].get("subscription_health_transition")
    if (old_transition != new_transition and (old_transition is not None or not new_transition
            or new_transition.get("source_package_sha256") != digest(source)
            or new_transition.get("source_snapshot") != target["configuration"].get("reuse_snapshot"))):
        raise ValueError("continuation changed adopted native health epoch")
    if source["configuration"].get("subscription_completion_health") != target["configuration"].get("subscription_completion_health"):
        if not new_transition or old_transition or new_transition.get("source_package_sha256") != digest(source):
            raise ValueError("continuation changed subscription completion health; an explicit health transition is required")
    if source["configuration"].get("prior_work") != target["configuration"].get("prior_work"):
        raise ValueError("continuation changed inherited prior work")
    old_health = source["configuration"].get("semantic_health_retirement")
    new_health = target["configuration"].get("semantic_health_retirement")
    if old_health != new_health and (old_health is not None or not compatible_geography_projection(source, target)):
        raise ValueError("continuation changed adopted semantic-health history")
    if source["configuration"]["execution_policy"].get("budget_accounting") != target["configuration"]["execution_policy"].get("budget_accounting"):
        raise ValueError("continuation changed budget accounting profile")
    if Path(source["configuration"]["run_directory"]).expanduser().resolve() == Path(target["configuration"]["run_directory"]).expanduser().resolve():
        raise ValueError("continuation must use a new run directory; preserve the source")
    policy = target["configuration"]["execution_policy"]
    if Decimal(statistics["carried_reservation_usd"]) > Decimal(str(policy["spending_ceiling_usd"])):
        raise ValueError("continuation ceiling is below already reserved spending")
    if statistics["maximum_attempts_per_task"] > policy["max_attempts_per_task"]:
        raise ValueError("continuation attempt bound is below retained history")
    retries = statistics["retry_counts"]
    bounds = policy["retry_policy"]
    if (bounds["max_total"] is not None and sum(retries.values()) > bounds["max_total"]) or any(bounds["max_"+kind] is not None and retries[kind] > bounds["max_"+kind] for kind in retries):
        raise ValueError("continuation retry bounds are below retained history")


def compatible_collection_test(source, target):
    """Reviewed roster selection for provider-only work; never vote migration.

    All source attempts and costs remain. Exact selected request identity is
    checked again by the ledger before reuse; no changed request is aliased.
    """
    config = target["configuration"]
    proof = config.get("provider_test_reuse")
    keys = {"profile", "source_package_sha256", "source_code_sha256", "target_code_sha256",
            "source_assets_sha256", "target_assets_sha256", "retained_configuration_ids", "new_configuration_ids"}
    if (config.get("execution_stage") != "collection-only-v1" or config.get("reuse_scope")
            or not isinstance(proof, dict) or set(proof) != keys or proof["profile"] != "provider-integration-collection-v1"
            or source["mode"] != target["mode"] or source["primary"] != target["primary"]
            or source["question_bindings"] != target["question_bindings"]
            or proof["source_package_sha256"] != digest(source)
            or proof["source_code_sha256"] != digest(source["code"]) or proof["target_code_sha256"] != digest(target["code"])
            or proof["source_assets_sha256"] != digest(source["assets"]) or proof["target_assets_sha256"] != digest(target["assets"])):
        return False
    assets = {k for k in source["assets"].keys() | target["assets"].keys() if source["assets"].get(k) != target["assets"].get(k)}
    code = {k for k in source["code"].keys() | target["code"].keys() if source["code"].get(k) != target["code"].get(k)}
    allowed = {"preparation.py", "runner.py", "reporting.py", "reuse.py", "fixtures.py", "_method/native.py", "_method/providers.py",
               "subscription_health.py", "projection.py", "recovery.py", "scheduling.py", "concurrency.py"}
    if assets - {"providers.json"} or code - allowed or any(target["scope"][key] for key in ("primary_tasks", "second_tasks", "conditional_third_tasks", "all_model_tasks")):
        return False
    old = {p["configuration_id"]: p for p in source["providers"]}
    new = {p["configuration_id"]: p for p in target["providers"]}
    overlap = sorted(old.keys() & new.keys())
    if proof["retained_configuration_ids"] != overlap or proof["new_configuration_ids"] != sorted(new.keys() - old.keys()):
        return False
    from .projection import apply_profiles
    for cid in overlap:
        expected = old[cid]
        if cid in config.get("provider_projection_profiles", {}) and expected != new[cid]:
            try:
                expected = apply_profiles([expected], {cid: config["provider_projection_profiles"][cid]})[0]
            except ValueError:
                return False
        if expected != new[cid]:
            return False
    requests = {(r["configuration_id"], r["question_id"]): r for r in source["search_requests"]}
    for request in target["search_requests"]:
        if request["configuration_id"] in old and requests.get((request["configuration_id"], request["question_id"])) != request:
            return False
    return bool(overlap)


def compatible_benchmark_continuation(source, target):
    """Only activate judging over an unchanged, fully saved collection.

    This proof permits no prompt, adapter, accounting or transport change. The
    separate native-health epoch still owns any historical judge-stop transition.
    A live target needs its own exact owner approval, never the collection receipt.
    """
    config = target["configuration"]
    proof = config.get("benchmark_continuation_reuse")
    keys = {"profile", "source_package_sha256", "source_code_sha256", "target_code_sha256",
            "source_assets_sha256", "target_assets_sha256"}
    if (source["configuration"].get("execution_stage") != "collection-only-v1"
            or config.get("execution_stage") is not None or config.get("reuse_scope")
            or not isinstance(proof, dict) or set(proof) != keys
            or proof["profile"] != "provider-integration-benchmark-v1"
            or proof["source_package_sha256"] != digest(source)
            or proof["source_code_sha256"] != digest(source["code"])
            or proof["target_code_sha256"] != digest(target["code"])
            or proof["source_assets_sha256"] != digest(source["assets"])
            or proof["target_assets_sha256"] != digest(target["assets"])):
        return False
    if any(source[k] != target[k] for k in ("mode", "primary", "assets", "question_bindings", "providers", "search_requests", "prompt_profiles", "schema_plan")):
        return False
    changed = {k for k in source["code"].keys() | target["code"].keys() if source["code"].get(k) != target["code"].get(k)}
    return changed <= {"reuse.py", "preparation.py"}


def read_snapshot(reference, target=None):
    if set(reference) != {"manifest_path", "sha256"}:
        raise ValueError("reuse snapshot requires an explicit manifest path and hash")
    path = Path(reference["manifest_path"]).expanduser().absolute()
    path = safe_file(path.parent, path.name)
    raw = path.read_bytes()
    if digest(raw) != reference["sha256"]:
        raise ValueError("reuse snapshot manifest hash differs")
    manifest = json.loads(raw)
    if manifest["version"] != 1:
        raise ValueError("unsupported reuse snapshot version")
    root = path.parent
    if target is not None and Path(target["configuration"]["run_directory"]).expanduser().resolve().is_relative_to(root.resolve()):
        raise ValueError("continuation run must not modify its reuse snapshot")
    names = set()
    for item in manifest["files"]:
        name = item["path"]
        if name in names or not (name in {"source-package.json", "source-binding.json", "events.jsonl", "offline-repair-claim.json", "codex-runtime/quota-events.jsonl"} or re.fullmatch(r"attempts/[0-9a-f]{64}/[^/]+", name) or re.fullmatch(r"search-dispatch/search-[0-9a-f]{64}\.json", name)):
            raise ValueError("duplicate or unrecognized reuse artifact")
        names.add(name)
        data = safe_file(root, name).read_bytes()
        if len(data) != item["bytes"] or digest(data) != item["sha256"]:
            raise ValueError("reuse artifact hash differs")
    if not {"source-package.json", "source-binding.json", "events.jsonl"} <= names:
        raise ValueError("reuse snapshot lacks source bindings")
    source = load(root / "source-package.json")
    if digest(source) != manifest["source_package_sha256"] or load(root / "source-binding.json") != dict(package_sha256=digest(source), mode=source["mode"]):
        raise ValueError("reuse source package binding differs")
    events = list(records(root / "events.jsonl"))
    # Every referenced request/result must itself be in the hash-bound file list.
    required = {f"attempts/{e['attempt_id']}/request.json" for e in events}
    required.update(e["result_path"] for e in events if e["event"] == "finished")
    if not required <= names:
        raise ValueError("reused ledger references unbound artifacts")
    from .interruption import attempts as interrupted_attempts
    statistics = history(root, events, interrupted_attempts=interrupted_attempts(source))
    receipts = _dispatch_receipts(source, root, events)
    effective = {f"attempts/{e['attempt_id']}/effective-search-request.json" for e in events if e['event'] == 'started'
                 and safe_file(root, f"attempts/{e['attempt_id']}/effective-search-request.json").exists()}
    if set(receipts) != {n for n in names if n.startswith('search-dispatch/')} or not effective <= names:
        raise ValueError("reuse dispatch audit references unbound receipts")
    from .request_profiles import audit_dispatch
    audit_dispatch(source, root, ledger_name="events.jsonl")
    if statistics != manifest["statistics"]:
        raise ValueError("reuse accounting summary differs from attempts")
    if target is not None:
        effective = dict(statistics, carried_reservation_usd=str(committed(root, source, events)))
        compatible(source, target, effective)
        if compatible_geography_projection(source, target) and statistics["uncertain_attempts"]:
            raise ValueError("geography correction cannot redispatch source uncertainty")
        if source["code"] != target["code"] and not compatible_benchmark_continuation(source, target):
            # The only permitted changed-code profile fixes recursive admission
            # metadata sizing. Every saved price must still reproduce exactly.
            from .accounting import reservation
            for event in events:
                if event["event"] != "started":
                    continue
                request = load(safe_file(root, f"attempts/{event['attempt_id']}/request.json"))
                if request["kind"] == "search" and target["configuration"].get("scale_expansion"):
                    # Scale permits no adapter/asset/accounting change. Earlier
                    # roster members still own their frozen holds, but never new
                    # sends. Some are intentionally absent from both rosters.
                    continue
                pricing = dict(target, providers=source["providers"]) if request["kind"] == "search" and compatible_collection_test(source, target) else target
                if request["kind"] != "cache" and reservation(request, pricing) != Decimal(request["reservation_usd"]):
                    raise ValueError("accounting fix changed an inherited reservation")
    return manifest


def compatible_accounting_fix(source, target):
    """Narrow, separately approved migration; no arbitrary changed-code reuse."""
    proof = target["configuration"].get("reuse_code_compatibility")
    if not isinstance(proof, dict) or set(proof) != {"profile", "source_package_sha256", "source_code_sha256", "target_code_sha256"}:
        return False
    changed = {key for key in set(source["code"]) | set(target["code"]) if source["code"].get(key) != target["code"].get(key)}
    profiles = {"reservation-metadata-idempotency-v1": {"accounting.py", "reuse.py"},
                "native-health-and-provider-projection-v1": {"subscription_health.py", "projection.py", "recovery.py", "scheduling.py", "preparation.py", "runner.py", "reuse.py", "concurrency.py", "reporting.py", "_method/providers.py"},
                "reservation-and-geography-recovery-v1": {"accounting.py", "reuse.py", "scheduling.py", "preparation.py"}}
    profile = proof["profile"]
    return (profile in profiles and
            (profile != "reservation-and-geography-recovery-v1" or compatible_geography_projection(source, target)) and
            proof["source_package_sha256"] == digest(source) and
            proof["source_code_sha256"] == digest(source["code"]) and
            proof["target_code_sha256"] == digest(target["code"]) and
            changed <= profiles[profile] and bool(changed))


def compatible_provider_projection(source, target):
    """Only explicit Searlo mappings with no previously projected positions.

    The exact original request/result bytes remain reused. No successful vote is
    re-identified by changing an already useful native mapping.
    """
    from .projection import apply_profiles
    old = source["configuration"].get("provider_projection_profiles", {})
    new = target["configuration"].get("provider_projection_profiles", {})
    if not isinstance(new, dict) or any(new.get(k) != v for k, v in old.items()):
        return False
    additions = {k: v for k, v in new.items() if k not in old}
    if not additions:
        return False
    try:
        if apply_profiles(source["providers"], additions) != target["providers"] or source["search_requests"] != target["search_requests"]:
            return False
        root = Path(target["configuration"]["reuse_snapshot"]["manifest_path"]).parent
        seen = set()
        from ._method.providers import project_response
        for event in records(root / "events.jsonl"):
            if event["event"] != "finished" or not event["task_id"].startswith("search-"):
                continue
            request = load(root / "attempts" / event["attempt_id"] / "request.json")
            cid = request["configuration_id"]
            if cid not in additions:
                continue
            response = load(root / event["result_path"])
            provider = next(p for p in source["providers"] if p["configuration_id"] == cid)
            projected = project_response(provider, response.get("raw_text", "").encode(), response["captured_at"],
                http_status=response.get("http_status") or 0, complete=response.get("complete", False))
            if projected["status"] != "missing_results" or projected["positions"]:
                return False
            seen.add((cid, request["question_id"]))
        return seen == {(cid, q["question_id"]) for cid in additions for q in source["questions"]}
    except (KeyError, ValueError, OSError, TypeError):
        return False


def compatible_geography_projection(source, target):
    """An explicitly approved draft-to-runtime projection, never vote aliasing."""
    proof = target["configuration"].get("question_projection_compatibility")
    keys = {"profile", "source_package_sha256", "target_question_bindings_sha256", "question_ids"}
    if not isinstance(proof, dict) or set(proof) != keys or proof["profile"] != "canonical-unrestricted-geography-v1":
        return False
    if proof["source_package_sha256"] != digest(source) or proof["target_question_bindings_sha256"] != digest(target["question_bindings"]):
        return False
    old = {q["question_id"]: q for q in source["questions"]}
    new = {q["question_id"]: q for q in target["questions"]}
    changed = {key for key in old.keys() | new.keys() if old.get(key) != new.get(key)}
    if not changed or changed != set(proof["question_ids"]) or old.keys() != new.keys() or source["search_requests"] != target["search_requests"]:
        return False
    for key in changed:
        a, b = old[key], new[key]
        if (a["geography"].get("regions") != ["unspecified"] or
                b["geography"] != dict(applies_to="no_geographic_restriction", regions=["global"]) or
                type(a["revision"]) is not int or b["revision"] != a["revision"] + 1 or
                {k: v for k, v in a.items() if k not in {"geography", "revision"}} !=
                {k: v for k, v in b.items() if k not in {"geography", "revision"}}):
            return False
    return True


def superseded_geography_health(package):
    """Retire native-normal Codex health observations on superseded contracts.

    This requires the new exact package's projection approval. Costs, votes,
    account pacing and real transport/quota errors remain in the original ledger.
    """
    config = package["configuration"]
    retirement = config.get("semantic_health_retirement")
    if retirement:
        path = Path(retirement["manifest_path"]).expanduser().absolute()
        path = safe_file(path.parent, path.name)
        raw = path.read_bytes()
        if digest(raw) != retirement["sha256"]:
            raise ValueError("semantic-health retirement proof differs")
        receipt = json.loads(raw)
        if receipt["profile"] != "canonical-geography-health-retirement-v1":
            raise ValueError("unknown semantic-health retirement")
        reference = receipt["source_snapshot"]
        read_snapshot(reference)
        source = load(Path(reference["manifest_path"]).parent / "source-package.json")
        if source["configuration"].get("semantic_health_retirement"):
            raise ValueError("retirement proof must bind the original pre-projection phase")
        projected = dict(source, questions=receipt["projected_questions"],
            question_bindings=[dict(question_id=q["question_id"], revision=q["revision"],
                contract_sha256=digest(q["contract"]), question_sha256=digest(q)) for q in receipt["projected_questions"]],
            configuration=dict(source["configuration"], reuse_snapshot=reference,
                run_directory=config["run_directory"],
                question_projection_compatibility=receipt["projection"]))
        if not compatible_geography_projection(source, projected):
            raise ValueError("retired health lacks canonical source projection")
        derived = superseded_geography_health(projected)
        if sorted(derived) != receipt["attempt_ids"]:
            raise ValueError("retired observations differ from bound native evidence")
        return derived
    if "question_projection_compatibility" not in config:
        return set()
    reference = config["reuse_snapshot"]
    manifest = read_snapshot(reference, package)
    root = Path(reference["manifest_path"]).parent
    source = load(root / "source-package.json")
    if not compatible_geography_projection(source, package):
        raise ValueError("superseded geography health lacks exact projection proof")
    affected = set(config["question_projection_compatibility"]["question_ids"])
    eligible = set()
    files = {item["path"] for item in manifest["files"]}
    for event in records(root / "events.jsonl"):
        if event["event"] != "finished" or not event["task_id"].startswith("codex-"):
            continue
        aid = event["attempt_id"]
        request = load(root / "attempts" / aid / "request.json")
        result = load(root / event["result_path"])
        question = request["blind_packet"]["input"]["complete_question"]
        if (question["question_id"] not in affected or result.get("recovery_validation_state") not in {"valid", "invalid"} or
                result.get("complete") is not True or result.get("dispatch_state") != "completed" or
                result.get("transport_success") is not True or result.get("native_quota_evidence") or
                result.get("fatal_configuration") or result.get("error") or result.get("error_type") or
                result.get("selected_transport") != "app-server-stdio-v1" or
                result.get("account_mode") != "subscription" or result.get("auth_observation") != "chatgpt"):
            continue
        name = f"attempts/{aid}/runtime-events.jsonl"
        if name not in files:
            continue
        events = list(records(root / name))
        if any(e.get("method") == "error" or e.get("runtime_failure") for e in events):
            continue
        completed = [e for e in events if e.get("method") == "turn/completed"]
        if (len(completed) != 1 or completed[0]["params"]["turn"]["status"] != "completed" or
                completed[0]["params"]["turn"]["id"] != result.get("turn_id") or
                completed[0]["params"].get("threadId") != result.get("thread_id")):
            continue
        thread_name = f"attempts/{aid}/runtime-thread.json"
        if thread_name not in files:
            continue
        thread = load(root / thread_name)
        reviewer = source["configuration"]["reviewers"]["codex"]
        if (thread.get("instructionSources") != [] or thread.get("model") != reviewer["model"] or
                thread.get("modelProvider") != "openai" or thread.get("reasoningEffort") != reviewer["reasoning_effort"] or
                thread.get("approvalPolicy") != "never" or thread.get("sandbox") != dict(type="readOnly", networkAccess=False) or
                thread["thread"].get("ephemeral") is not True or thread["thread"].get("turns") or
                thread["thread"].get("id") != result.get("thread_id")):
            continue
        items = [e["params"]["item"] for e in events if e.get("method") == "item/completed"]
        echoes = [item for item in items if item.get("type") == "userMessage"]
        answers = [item for item in items if item.get("type") == "agentMessage" and item.get("phase") != "commentary"]
        if (any(item.get("type") not in {"userMessage", "agentMessage", "reasoning"} or item.get("memoryCitation") for item in items) or
                len(echoes) != 1 or len(answers) != 1):
            continue
        try:
            echoed = json.loads("".join(part.get("text", "") for part in echoes[0]["content"]))
        except (ValueError, KeyError, TypeError):
            continue
        if echoed == request["blind_packet"]["input"]:
            eligible.add(aid)
    return eligible


def committed(root, source, events, *, native_usage=False):
    """Recompute known native cost plus unknown holds from bound attempt bytes."""
    from .accounting import usage_cost
    finished = {e["attempt_id"]: e for e in events if e["event"] == "finished"}
    total = Decimal(0)
    for event in events:
        if event["event"] != "started":
            continue
        request = load(safe_file(root, f"attempts/{event['attempt_id']}/request.json"))
        maximum = Decimal(request["reservation_usd"])
        done = finished.get(event["attempt_id"])
        cost = None
        if done and (native_usage or source["configuration"]["execution_policy"].get("budget_accounting") in ("source-native-usage-v1", "source-native-cache-usage-v1")):
            cost = usage_cost(request, load(safe_file(root, done["result_path"])), source)
        if cost is not None and cost > maximum:
            raise ValueError("source native cost exceeds its reserved maximum")
        total += maximum if cost is None else cost
    return total


def _dispatch_receipts(package, root, events):
    """Retain declared first-send anchors, including starts interrupted before return."""
    from .request_profiles import read_dispatch
    requests = {"search-" + digest(dict(configuration=r["configuration_id"], question=r["question_id"])): r
                for r in package.get("search_requests", []) if "date_dispatch" in r}
    names = set()
    for event in events:
        task = event["task_id"]
        if event["event"] != "started" or task not in requests:
            continue
        name = "search-dispatch/" + task + ".json"
        path = safe_file(root, name)
        attempt = safe_file(root, f"attempts/{event['attempt_id']}/effective-search-request.json")
        if path.exists():
            effective = read_dispatch(requests[task], path)
            if attempt.exists() and load(attempt) != effective:
                raise ValueError("interrupted effective search request differs")
            names.add(name)
        elif attempt.exists():
            raise ValueError("interrupted effective request lacks its dispatch anchor")
    return sorted(names)


def freeze(source_package_path, output):
    """Read a quiescent source; freeze an independent copy without a live call."""
    source_path = Path(source_package_path).expanduser().absolute()
    source_path = safe_file(source_path.parent, source_path.name)
    source_bytes = source_path.read_bytes()
    source = json.loads(source_bytes)
    directory = Path(source["configuration"]["run_directory"]).expanduser().resolve()
    output = Path(output).expanduser().resolve()
    if output.exists() or output == directory or output.is_relative_to(directory):
        raise ValueError("choose a new private snapshot directory outside the source run")
    with source_lock(directory):
        binding = safe_file(directory, "binding.json").read_bytes()
        if json.loads(binding) != dict(package_sha256=digest(source), mode=source["mode"]):
            raise ValueError("source run does not belong to the supplied package")
        private_directory(output)
        files = []
        def copy(name, data):
            write_private(safe_file(output, name), data)
            files.append(dict(path=name, bytes=len(data), sha256=digest(data)))
        copy("source-package.json", source_bytes)
        copy("source-binding.json", binding)
        events_path = safe_file(directory, "attempts.jsonl")
        copy("events.jsonl", events_path.read_bytes() if events_path.exists() else b"")
        repair_claim = safe_file(directory, "offline-repair-claim.json")
        if repair_claim.exists():
            copy("offline-repair-claim.json", repair_claim.read_bytes())
        quota = safe_file(directory, "codex-runtime/quota-events.jsonl")
        if quota.exists():
            copy("codex-runtime/quota-events.jsonl", quota.read_bytes())
        attempts = directory / "attempts"
        if attempts.is_symlink():
            raise ValueError("source attempts directory cannot be a symlink")
        if attempts.exists():
            for path in sorted(attempts.rglob("*")):
                relative = path.relative_to(directory).as_posix()
                safe_file(directory, relative)
                if path.is_file():
                    if not re.fullmatch(r"attempts/[0-9a-f]{64}/[^/]+", relative):
                        raise ValueError("unrecognized source attempt artifact")
                    copy(relative, path.read_bytes())
        events = list(records(output / "events.jsonl"))
        for name in _dispatch_receipts(source, directory, events):
            copy(name, safe_file(directory, name).read_bytes())
        from .interruption import attempts as interrupted_attempts
        manifest = dict(version=1, source_package_sha256=digest(source),
                        files=sorted(files, key=lambda item: item["path"]), statistics=history(output, events, interrupted_attempts=interrupted_attempts(source)),
                        purpose="continuation of the same frozen collection; original capture times and cumulative reservations; no expiry or automatic deletion")
        write_private(output / "manifest.json", manifest)
    reference = dict(manifest_path=str(output / "manifest.json"), sha256=digest(encoded(manifest)))
    read_snapshot(reference)
    return reference


def materialize(package, ledger):
    reference = package["configuration"].get("reuse_snapshot")
    if reference is None:
        return None
    manifest = read_snapshot(reference, package)
    root = Path(reference["manifest_path"]).expanduser().absolute().parent
    receipt = dict(snapshot_sha256=reference["sha256"], source_package_sha256=manifest["source_package_sha256"],
                   offline_repair_already_claimed=any(item["path"] == "offline-repair-claim.json" for item in manifest["files"]),
                   **manifest["statistics"])
    marker = safe_file(ledger.directory, "reuse.json")
    safe_file(ledger.directory, ledger.path.name)
    def bound_bytes(item):
        data = safe_file(root, item["path"]).read_bytes()
        if len(data) != item["bytes"] or digest(data) != item["sha256"]:
            raise ValueError("reuse artifact changed during import")
        return data
    if marker.exists() and load(marker) != receipt:
        raise ValueError("run reuse receipt changed")
    # Idempotent after a crash: matching copied files are retained, never replaced.
    for item in manifest["files"]:
        if not item["path"].startswith(("attempts/", "search-dispatch/")) and item["path"] not in {"offline-repair-claim.json", "codex-runtime/quota-events.jsonl"}:
            continue
        path = safe_file(ledger.directory, item["path"])
        if path.exists():
            appended_quota = item["path"] == "codex-runtime/quota-events.jsonl" and marker.exists() and path.read_bytes().startswith(bound_bytes(item))
            if not appended_quota and digest(path.read_bytes()) != item["sha256"]:
                raise ValueError("imported evidence bytes differ")
        else:
            if marker.exists():
                raise ValueError("sealed import lost an artifact")
            write_private(path, bound_bytes(item))
    original = bound_bytes(next(item for item in manifest["files"] if item["path"] == "events.jsonl"))
    if ledger.path.exists():
        saved = ledger.path.read_bytes()
        if marker.exists():
            if not saved.startswith(original):
                raise ValueError("imported event prefix changed")
        elif saved != original:
            raise ValueError("unsealed import contains different attempts")
    else:
        if marker.exists():
            raise ValueError("sealed import lost its ledger")
        write_private(ledger.path, original)
    if not marker.exists():
        write_private(marker, receipt)
    ledger.reload()
    return receipt
