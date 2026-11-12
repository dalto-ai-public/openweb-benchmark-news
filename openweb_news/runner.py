"""One unattended path from frozen questions to reports."""
from collections import Counter
from contextlib import ExitStack
from pathlib import Path
import time

from . import backends, fixtures, judging
from .common import digest, encoded, load, now, private_directory, records, write_private
from .ledger import Ledger, UncertainAttempt
from .preparation import require_approval, verify_package
from .reporting import compact_outcome, write_reports
from .review import classify as classify_position, needs_third, settle
from .scheduling import Admission, AdmissionBlocked, Pipeline, RetryBudgetExhausted, retry_after_deadline, task_ready_at, timestamp
from .transport import Credentials, https
from ._method import providers, scoring


from .accounting import reservation


def cached_tokens(position):
    usage = (position.get("backend_observation") or {}).get("usage")
    value = usage.get("cachedContentTokenCount") if isinstance(usage, dict) else None
    return value if type(value) is int and value >= 0 else 0


def decode_native(role,raw,compiled,profiles,*,request=None,folder=None):
    """Keep native transport validation, then apply the explicitly bound reader."""
    from .review_pipeline import decode_value
    if request is not None and folder is not None:
        from .subscription_health import excluded_intervention,excluded_position
        excluded=excluded_intervention(request,raw,folder)
        if excluded:return excluded_position(excluded)
    if role=='pro':
        position=judging.response_result(raw.get('raw_text','').encode(),compiled,
            http_status=raw.get('http_status')or 0,complete=raw.get('complete',False))
        if profiles and 'raw_verdict'in position:
            retained={k:v for k,v in position.items()if k in ('raw_sha256','http_status','complete','metadata')}
            return dict(retained,**decode_value(position['raw_verdict'],compiled,profiles['text_profile'],date_profile=profiles['date_profile']))
        return position
    if not raw.get('transport_success'):
        return dict(state='unjudged',grade=None,errors=[raw.get('error','reviewer failed')])
    if profiles:
        return decode_value(raw.get('verdict'),compiled,profiles['text_profile'],date_profile=profiles['date_profile'])
    return judging.decode_position(raw.get('verdict'),compiled)


def run(package, *, approval=None, env_file=None, retry_failed=False):
    """Automatically resume a declared transient epoch only while work progresses."""
    from .resilience import enabled
    if not enabled(package["configuration"]):
        return _run(package,approval=approval,env_file=env_file,retry_failed=retry_failed)
    from .codex_runtime import RuntimeStopped
    path=Path(package["configuration"]["run_directory"])/"attempts.jsonl"
    def started_count():
        return sum(e["event"]=="started"for e in records(path))if path.exists()else 0
    while True:
        before=started_count()
        try:
            result=_run(package,approval=approval,env_file=env_file,retry_failed=True)
        except RuntimeStopped:
            if started_count()<=before:
                raise
            continue
        if result["execution"]["state"]!="completed_with_holds"or started_count()<=before:
            return result
        # Completed/uncertain tasks stay protected by the ledger's per-task
        # attempts and one-resend allowance; this loop does not reset them.


def _run(package, *, approval=None, env_file=None, retry_failed=False):
    started = time.monotonic()
    live = package["mode"] == "live"
    if live:
        require_approval(package, approval or {})
    else:
        verify_package(package)
    config = package["configuration"]
    from .interruption import subscription_only
    subscriptions_only = subscription_only(package)
    from .profile_authority import verify_native
    profiles=verify_native(config,mode=package['mode'])
    from .resilience import enabled as automatic_recovery
    automatic = automatic_recovery(config)
    retry_failed = retry_failed or automatic
    collection_only = config.get("execution_stage") == "collection-only-v1"
    directory = private_directory(config["run_directory"])
    package_hash = digest(package)
    from .interruption import attempts as interrupted_attempts
    ledger = Ledger(directory, dict(package_sha256=package_hash, mode=package["mode"]),
                    interrupted_attempts=interrupted_attempts(package),
                    retry_preconnect=automatic or bool(package["configuration"].get("interrupted_run_recovery")),
                    uncertain_resends=1 if automatic else 0)
    if (directory / "attempts.jsonl").exists():
        from .request_profiles import audit_dispatch
        audit_dispatch(package, directory)
    credentials = Credentials(env_file) if live else None
    policy = config["execution_policy"]
    cells, labels, reasons, findings, outcomes, provenance = {}, {}, {}, {}, [], []
    execution_counts = Counter()
    held = []
    system_cache = None
    trial = None

    def execute(task_id, request, call):
        if request.get("role") == "codex" and codex_runtime is not None and getattr(codex_runtime.client,"failure",None):
            return dict(transport_success=False,dispatch_state="not_sent",complete=False,
                        held_reason="Codex runtime stopped before this planned turn; no ledger attempt or resend")
        selected_call = call
        if system_cache is not None and request.get("backend") == "gemini-api":
            try:
                wire = system_cache.wire(request["wire"])
                selected_call = lambda folder: call(folder, wire)
            except AdmissionBlocked as error:
                return dict(transport_success=False, dispatch_state="not_sent", held_reason=str(error), complete=False)
        def retained_call(folder):
            raw = selected_call(folder)
            if automatic and raw.get("recoverable_runtime_failure"):
                raw=dict(raw,fatal_configuration=False)
            if request.get("role") in config.get("subscription_completion_health", {}).get("roles", []):
                from .subscription_health import bind
                proof = bind(request, raw, folder)
                if proof:
                    raw = dict(raw, subscription_completion_evidence=proof)
            return dict(raw, recovery_validation_state=classify(task_id, raw,request=request,folder=folder)) if policy["scheduling"].get("recovery") else raw
        try:
            return ledger.execute(task_id, request, retained_call, max_attempts=policy["max_attempts_per_task"],
                                  retry_failed=retry_failed, retry_http_statuses=policy["retry_policy"]["http_statuses"],
                                  before_start=admission.reserve)
        except RetryBudgetExhausted:
            # Keep the original complete failure when a shared retry allowance
            # is exhausted; do not invent a new attempt or abandon other work.
            return dict(ledger.inspect(task_id, request, max_attempts=policy["max_attempts_per_task"],
                                  retry_failed=False, retry_http_statuses=())["result"], retry_allowance_exhausted=True)
        except AdmissionBlocked as error:
            return dict(transport_success=False, dispatch_state="not_sent", held_reason=str(error), complete=False)

    def submit(stage, task_id, request, call, complete, request_factory=None):
        account = package["account_bindings"][stage]
        request = dict(request, reservation_usd=str(reservation(request, package)),
                       reservation_basis="conservative estimate; not invoice or guaranteed upper bound")
        def received(raw):
            from .recovery import known_not_sent_connect
            from .cache_recovery import terminal_uncertain_tasks
            retained_unknown=task_id in terminal_uncertain_tasks(package)
            if raw.get("held_reason") or (raw.get("dispatch_state") == "uncertain" and not retained_unknown) or known_not_sent_connect(raw):
                held.append(dict(task_id=task_id, stage=stage, account=account,
                                 dispatch_state=raw.get("dispatch_state"),
                                 transport_stage=raw.get("transport_stage"),
                                 reason=raw.get("held_reason", "pre-HTTP connection failed; no request sent" if known_not_sent_connect(raw)
                                                else "uncertain transport; retained evidence requires inspection")))
            if raw.get("runtime_cleanup_error"):
                held.append(dict(task_id=task_id,stage=stage,account=account,dispatch_state=raw.get("dispatch_state"),
                    reason="known completed judgment retained; runtime cleanup failed and further runtime dispatch stopped"))
            complete(raw)
        try:
            from .cache_recovery import terminal_uncertain_tasks
            preserve_uncertain=task_id in terminal_uncertain_tasks(package)
            state = ledger.inspect(task_id, request, max_attempts=policy["max_attempts_per_task"],
                                   retry_failed=retry_failed and not preserve_uncertain, retry_http_statuses=policy["retry_policy"]["http_statuses"])
        except UncertainAttempt:
            received(dict(transport_success=False, complete=False, dispatch_state="uncertain",
                          held_reason="uncertain saved attempt; no resend; inspect retained evidence"))
            return
        if not state["dispatch"]:
            # Cache reads need neither a worker slot nor account pacing.
            execution_counts["reused_tasks"] += 1
            retained = state["result"]
            if retained.get("fatal_configuration"):
                # Keep a saved failed judgment unavailable without re-running it
                # or letting its historical configuration abort unrelated work.
                retained = dict(retained, fatal_configuration=False, transport_success=False,
                                held_reason="saved reviewer configuration failure; output excluded; no resend",
                                error="saved reviewer configuration failure; output excluded; no resend")
            received(retained)
            return
        if subscriptions_only and stage not in {'codex', 'claude'}:
            received(dict(transport_success=False, complete=False, dispatch_state='not_sent',
                held_reason='subscription-only recovery; search and Pro dispatch deferred'))
            return
        execution_counts["scheduled_tasks"] += 1
        request_hash = digest(request)
        def bound_request():
            if request_factory is None:
                return request
            rebuilt = request_factory()
            rebuilt.update(reservation_usd=str(reservation(rebuilt, package)),
                           reservation_basis="conservative estimate; not invoice or guaranteed upper bound")
            if digest(rebuilt) != request_hash:
                raise ValueError("queued request differs from retained compiled evidence")
            return rebuilt
        def finished(raw):
            # Retained deadlines, including old cached responses, never restart a delay.
            pipeline.sync()
            from .recovery import known_not_sent_connect
            eligible_failure = (raw.get("complete") is True and raw.get("transport_success") is False
                                and raw.get("http_status") in policy["retry_policy"]["http_statuses"])
            eligible_connect = bool((automatic or config.get("interrupted_run_recovery")) and known_not_sent_connect(raw))
            eligible_uncertain = bool(automatic and raw.get("dispatch_state") == "uncertain")
            if retry_failed and not raw.get("retry_allowance_exhausted") and (eligible_failure or eligible_connect or eligible_uncertain):
                retry_request = bound_request()
                next_attempt = ledger.inspect(task_id, retry_request, max_attempts=policy["max_attempts_per_task"], retry_failed=True, retry_http_statuses=policy["retry_policy"]["http_statuses"])
                if next_attempt["dispatch"]:
                    submit(stage, task_id, retry_request, call, complete, request_factory)
                    return
            received(raw)
        if request_factory is not None:
            request = None  # Full inputs stay on disk; queued jobs retain only a binding/loader.
        pipeline.submit(account, lambda: execute(task_id, bound_request(), call), finished,
                        ready_at=task_ready_at(state, policy, account), scope=stage,
                        case=task_id[len("codex-"):] if stage == "codex" else None)

    with ledger.locked(), ExitStack() as runtimes:
        from .reuse import materialize
        from .fresh import begin
        fresh = begin(package, ledger)
        reuse_receipt = materialize(package, ledger)
        prior = None
        if "prior_work" in config:
            from .prior_work import read
            prior = read(config["prior_work"], package)
        admission = Admission(ledger, policy, package=package,
                              initial_committed_usd=fresh["initial_committed_usd"] if fresh else prior["committed_and_held_usd"] if prior else "0")
        if live and not collection_only and "system_cache" in config and not subscriptions_only:
            from .caching import SystemCache
            auth = dict(header="x-goog-api-key", prefix="", secret_ref=package["primary"]["secret_ref"])
            system_cache = SystemCache(package, ledger, admission,
                lambda wire, folder: https(wire, auth, credentials, folder, timeout_seconds=policy["timeout_seconds"]))
        def classify(task_id, raw, *, request=None, folder=None):
            if request is None:
                event=next((e for e in reversed(ledger.task_events(task_id))if e['event']=='finished'),None)
                if event:
                    folder=directory/'attempts'/event['attempt_id'];request=load(folder/'request.json')
            if request is not None and folder is not None:
                from .subscription_health import excluded_intervention
                if excluded_intervention(request,raw,folder):return 'unjudged'
            if "recovery_validation_state" in raw:
                return raw["recovery_validation_state"]
            if not policy["scheduling"].get("recovery"):
                return None
            for role in ("pro", "codex", "claude"):
                if task_id.startswith(role+"-"):
                    compiled = load(directory / "compiled" / (task_id[len(role)+1:]+".json"))
                    position = decode_native(role,raw,compiled,profiles,request=request,folder=folder)
                    return position.get("state")
            return None
        pipeline = Pipeline(policy["scheduling"]["accounts"], recovery=policy["scheduling"].get("recovery"), classify=classify)
        if (config.get('system_cache',{}).get('renewal')or{}).get('profile')=='expiry-leases-v2':
            def health_changed(event):
                path=directory/'status-events.jsonl'
                raw=(path.read_bytes()if path.exists()else b'')+encoded(event)
                write_private(path,raw,replace=path.exists())
            pipeline.on_health_change=health_changed
        codex_runtime = None
        quota_path = directory / "codex-runtime" / "quota-events.jsonl"
        retained_quota = list(records(quota_path)) if quota_path.exists() else []
        def synchronize():
            quota = codex_runtime.client.quota_evidence if codex_runtime else retained_quota
            admission.restore_pacing(ledger, pipeline, package["account_bindings"], quota)
            if trial:
                if codex_runtime:
                    trial.observe_quota(codex_runtime.client.quota_evidence)
                trial.advance(pipeline.recovery)
        pipeline.sync = synchronize
        preflight = {}
        if "codex_concurrency_trial" in config:
            from .concurrency import Trial
            trial = Trial(config["codex_concurrency_trial"], len(package["search_requests"]))
            pipeline.trial = trial
            trial.observe_quota(retained_quota)
        synchronize()
        if live:
            for reference in package["required_credential_references"]:
                credentials.get(reference)
        if live and not collection_only:
            for role, reviewer in config["reviewers"].items():
                if reviewer["backend"].endswith("-subscription"):
                    try:
                        check = backends.subscription(reviewer, None, directory, preflight_only=True)
                    except (OSError, ValueError, RuntimeError) as error:
                        check = dict(transport_success=False, error_type=type(error).__name__, inference_performed=False)
                    preflight[role] = check
            write_private(directory / "preflight.json", preflight, replace=True)
            if any(not check["transport_success"] for check in preflight.values()):
                raise ValueError("reviewer preflight failed before search/Pro dispatch; inspect private preflight.json")
            codex_config = config["reviewers"]["codex"]
            if codex_config["backend"] == "codex-subscription" and codex_config.get("transport") == "app-server-stdio-v1":
                from .codex_runtime import CodexRuntime
                codex_runtime = runtimes.enter_context(CodexRuntime(codex_config, directory / "codex-runtime", preflight=preflight["codex"]))
        if codex_runtime and config.get("subscription_quota"):
            from .quota import QuotaGate
            pipeline.quota_gate = QuotaGate(codex_runtime.client, directory / "codex-runtime")
        def retain_outcome(case_id, packet_sha256, primary, second, third):
            metadata = occurrences[case_id]
            result = dict(case_id=case_id, **metadata, packet_sha256=packet_sha256,
                          **settle(primary, second, third))
            compact_outcome(result)
            outcomes.append(result)
            labels[case_id], reasons[case_id] = result["grade"], result["reason"]
            selected = result["positions"].get(result["selected_roles"][0]) if result["selected_roles"] else None
            candidates = ((selected or {}).get("conformance_judgment") or (selected or {}).get("judgment") or {}).get("candidates", [])
            findings[case_id] = [dict(report_id=None, event_id=None, facet_ids=c["supported_facet_ids"])
                                 for c in candidates if c["answer_supported"]] if result["grade"] == 2 else []

        def enqueue_occurrence(compiled):
            case_id = compiled["packet"]["case_id"]
            packet_sha256 = compiled["packet_sha256"]
            compiled_hash = digest(compiled)
            def read_compiled():
                path = directory / "compiled" / (case_id + ".json")
                raw = path.read_bytes()
                if digest(raw) != compiled_hash:
                    raise ValueError("queued compiled evidence changed")
                import json
                return json.loads(raw)
            compiled = None
            positions = {}
            def received(role, position):
                positions[role] = position
                if role == "claude":
                    retain_outcome(case_id, packet_sha256, positions["pro"], positions["codex"], position)
                elif "pro" in positions and "codex" in positions:
                    if any(p.get('pending_dispatch')for p in positions.values()):
                        retain_outcome(case_id,packet_sha256,positions['pro'],positions['codex'],None)
                    elif needs_third(positions["pro"], positions["codex"]):
                        enqueue_model("claude")
                    else:
                        retain_outcome(case_id, packet_sha256, positions["pro"], positions["codex"], None)

            def enqueue_model(role):
                coordinate = (role, occurrences[case_id]["configuration_id"],
                              occurrences[case_id]["question_id"], occurrences[case_id]["rank"])
                if fresh and (role + "-" + case_id in fresh["blocked_tasks"] or
                              coordinate in fresh["blocked_occurrences"]):
                    held.append(dict(task_id=role + "-" + case_id, stage=role,
                                     account=package["account_bindings"][role],
                                     dispatch_state="not_sent",
                                     reason="uncertain source role/occurrence; no fresh dispatch"))
                    received(role, dict(state="unjudged", grade=None,
                                        errors=["uncertain source role/occurrence retained without resend"],
                                        independent=True, prior_verdict_exposure=False))
                    return
                if role == "pro":
                    def request_factory():
                        return dict(kind="model", role=role, backend="gemini-api", wire=judging.build_pro_request(read_compiled()))
                    auth = dict(header="x-goog-api-key", prefix="", secret_ref=package["primary"]["secret_ref"])
                    def call(folder, wire_override=None):
                        actual = wire_override or request_factory()["wire"]
                        write_private(folder / "effective-wire.json", actual)
                        return https(actual, auth, credentials, folder, timeout_seconds=policy["timeout_seconds"]) if live else fixtures.model(role, read_compiled(), folder)
                else:
                    backend_config = config["reviewers"][role]
                    def request_factory():
                        return dict(kind="model", role=role, backend=backend_config["backend"],
                                    backend_config=backend_config, blind_packet=judging.blind_packet(read_compiled()))
                    def backend_call(folder):
                        if not live:
                            return fixtures.model(role, read_compiled(), folder)
                        blind = judging.blind_packet(read_compiled())
                        if role == "codex" and codex_runtime is not None:
                            return codex_runtime.review(blind, folder)
                        if backend_config["backend"].endswith("-subscription"):
                            try:
                                return backends.subscription(backend_config, blind, folder, preflight=preflight.get(role))
                            except (OSError, ValueError, RuntimeError) as error:
                                return dict(transport_success=False, dispatch_state="uncertain", fatal_configuration=True,
                                            error_type=type(error).__name__, error="reviewer isolation/auth configuration failed")
                        return backends.api(backend_config, blind, folder, credentials)

                    def call(folder):
                        if role != "codex" or trial is None:
                            return backend_call(folder)
                        measurement = trial.started(case_id)
                        raw = backend_call(folder)
                        return dict(raw, concurrency_measurement=trial.finished(measurement))

                def decoded(raw):
                    compiled = read_compiled()
                    event=next((e for e in reversed(ledger.task_events(role+'-'+case_id))if e['event']=='finished'),None)
                    folder=directory/'attempts'/event['attempt_id']if event else None
                    retained_request=load(folder/'request.json')if folder else None
                    if role == "pro":
                        position = decode_native(role,raw,compiled,profiles,request=retained_request,folder=folder)
                        raw = dict(raw, selected_backend="gemini-api", requested_model=package["primary"]["model"], account_mode="api",
                                   served_model=position.get("metadata", {}).get("modelVersion"),
                                   usage=position.get("metadata", {}).get("usageMetadata"))
                    else:
                        if raw.get("fatal_configuration"):
                            raise RuntimeError("reviewer configuration/isolation failed; stop and inspect private attempt records")
                        position = decode_native(role,raw,compiled,profiles,request=retained_request,folder=folder)
                    if (raw.get('dispatch_state')=='not_sent' and raw.get('held_reason')
                            and (subscriptions_only or (config.get('system_cache',{}).get('renewal')or{}).get('profile')=='expiry-leases-v2')):
                        position['pending_dispatch']=True
                    position.update(independent=True, prior_verdict_exposure=False,
                                    backend_observation={key: raw.get(key) for key in
                                                         ("selected_backend", "requested_model", "served_model", "account_mode",
                                                          "auth_observation", "served_model_observation", "requested_settings", "cli_version",
                                                          "usage", "elapsed_seconds", "cli_cost_usd_estimate", "selected_transport", "thread_id", "turn_id",
                                                          "dispatch_state", "transport_stage")})
                    if role in config.get("subscription_completion_health", {}).get("roles", []):
                        from .subscription_health import verified
                        event = next((e for e in reversed(ledger.task_events(role + "-" + case_id))
                                      if e["event"] == "finished"), None)
                        normal = bool(event and verified(request_factory(), raw,
                                      directory / "attempts" / event["attempt_id"]))
                        position["backend_observation"]["native_completion_health"] = (
                            "normal_proof" if normal else "completed_without_normal_health_proof"
                            if raw.get("transport_success") else "incomplete_transport")
                    if role == "codex" and trial:
                        trial.observe(case_id, raw, position.get("state", "unjudged"))
                    received(role, position)
                submit(role, role + "-" + case_id, request_factory(), call, decoded, request_factory)
            enqueue_model("pro")
            enqueue_model("codex")

        def enqueue_search(search_request):
            cid, qid = search_request["configuration_id"], search_request["question_id"]
            provider = next(p for p in package["providers"] if p["configuration_id"] == cid)
            question = next(q for q in package["questions"] if q["question_id"] == qid)
            task_id = "search-" + digest(dict(configuration=cid, question=qid))
            request = dict(search_request, kind="search", auth_reference=provider["auth"])
            def collected(response):
                if trial:
                    trial.collected(task_id)
                if response.get("held_reason"):
                    # Not collected stays separate from a completed provider failure.
                    provenance.append(dict(task_id=task_id, configuration_id=cid, question_id=qid,
                                           status="held", reason=response["held_reason"], dispatch_state=response.get("dispatch_state"),
                                           transport_stage=response.get("transport_stage")))
                    return
                projected = providers.project_response(provider, response.get("raw_text", "").encode(),
                    response.get("captured_at", "2026-01-01T00:00:00Z"),
                    http_status=response.get("http_status") or 0, complete=response.get("complete", False))
                if response.get("credential_echo_redacted"):
                    projected.update(status="credential_echo_requires_private_review", positions=[])
                write_private(directory / "projections" / (task_id + ".json"), projected, replace=True)
                cell = dict(status=projected["status"], ranked=[])
                cells[cid, qid] = cell
                provenance.append(dict(task_id=task_id, configuration_id=cid, question_id=qid,
                                       status=projected["status"], response_metadata=projected.get("response_metadata"),
                                       dispatch_state=response.get("dispatch_state"), transport_stage=response.get("transport_stage"),
                                       **({"failure_detail": projected["failure_detail"]} if "failure_detail" in projected else {}),
                                       elapsed_seconds=response.get("elapsed_seconds")))
                from .returned_fields import CONFIG as field_rules, measure_position
                provenance[-1]['returned_field_profile'] = field_rules['profile']
                provenance[-1]['returned_field_observations'] = [
                    dict(rank=p['rank'], **measure_position(p))
                    for p in projected['positions'][:judging.METHOD['pool_depth']]]
                if collection_only:
                    provenance[-1].update(native_positions=len(projected["positions"]),
                        capture_time=projected.get("capture_time"), raw_sha256=projected["raw_sha256"],
                        field_states={name: dict(Counter(p.get(name, {}).get("state", "missing") for p in projected["positions"]))
                                      for name in ("title", "content", "url")},
                        native_text_lengths=projected.get("native_text_length_observations"),
                        native_product_issues=projected.get("native_product_issues", []))
                    return
                if projected["status"] not in scoring.ANSWERED:
                    return
                for position in projected["positions"][:judging.METHOD["pool_depth"]]:
                    case_id = "occurrence-" + digest(dict(question=question, configuration=provider, position=position))
                    url = position.get("url", {})
                    cell["ranked"].append(dict(rank=position["rank"], evidence_id=case_id,
                                               url=url.get("value") if url.get("state") == "present" else None))
                    packet = judging.packet_for(question, provider, position, case_id)
                    compiled = judging.compile_packet(packet, response["captured_at"], config=provider)
                    frozen_prompt = next(p for p in package["prompt_profiles"] if p["question_id"] == qid)
                    if judging.blind_packet(compiled)["prompt_sha256"] != frozen_prompt["prompt_sha256"]:
                        raise ValueError("runtime prompt differs from the exact prepared question profile")
                    write_private(directory / "compiled" / (case_id + ".json"), compiled, replace=True)
                    occurrences[case_id] = dict(question_id=qid, configuration_id=cid, rank=position["rank"])
                    if trial:
                        trial.register(case_id, occurrences[case_id])
                    enqueue_occurrence(compiled)
            if fresh:
                response = fresh["captures"].get(task_id)
                if response is None or response.get("dispatch_state") == "uncertain":
                    response = dict(held_reason="source search capture unavailable; no provider dispatch",
                                    dispatch_state="not_sent")
                execution_counts["reused_search_cells"] += 1
                collected(response)
            else:
                def search_call(folder):
                    from .request_profiles import bind_dispatch
                    receipt = directory / "search-dispatch" / (task_id + ".json")
                    effective = bind_dispatch(search_request, receipt)
                    if "date_dispatch" in search_request:
                        write_private(folder / "effective-search-request.json", effective)
                    response = (https(effective, provider["auth"], credentials, folder,
                                      timeout_seconds=policy["timeout_seconds"]) if live
                                else fixtures.search(effective, provider, folder))
                    if "date_dispatch" in search_request:
                        response = dict(response, effective_search_request_sha256=digest(effective),
                            search_dispatch_receipt_sha256=digest(receipt.read_bytes()))
                    return response
                submit("search/" + cid, task_id, request,
                       search_call, collected)

        occurrences = {}
        for search_request in package["search_requests"]:
            enqueue_search(search_request)
        if any(pipeline.queues.values()):
            pipeline.run()
        if system_cache is not None:
            system_cache.cleanup()
        # Completion order must not affect scoring tie behavior or report ordering.
        outcomes.sort(key=lambda row: (row["configuration_id"], row["question_id"], row["rank"], row["case_id"]))
        provenance.sort(key=lambda row: (row["configuration_id"], row["question_id"]))
        scores = None if collection_only else scoring.score(package["questions"], [p["configuration_id"] for p in package["providers"]],
                               cells, labels, reasons, config["coverage_target"], config["tie_order"], findings=findings)
        report = dict(method_version=judging.METHOD["version"], mode=package["mode"], package_sha256=package_hash,
                      execution_stage=config.get("execution_stage", "benchmark"),
                      generated_at=now(), scores=scores, outcomes=outcomes, collection=provenance,
                      source_versions=dict(code=package["code"], assets=package["assets"]),
                      outcome_counts=dict(Counter("unknown" if v is None else str(v) for v in labels.values())),
                      execution=dict(**execution_counts, elapsed_seconds=time.monotonic() - started,
                                     reuse=reuse_receipt,
                                     fresh_judging=fresh["receipt"] if fresh else None,
                                     prior_work=prior,
                                     committed_and_held_usd=str(admission.total),
                                     budget_accounting=policy.get("budget_accounting", "maximum-reservations"),
                                     state="stopped_at_ceiling" if admission.stopped_reason else "completed_with_holds" if held else "completed",
                                     stopped_reason=admission.stopped_reason, held_tasks=sorted(held, key=lambda row: row["task_id"]),
                                     retained_started_attempts=sum(event["event"] == "started" for event in ledger.events()),
                                     scheduling=policy["scheduling"],
                                     recovery=pipeline.recovery.describe() if pipeline.recovery else None,
                                     primary_native_cached_input_tokens=sum(cached_tokens(row["positions"]["pro"]) for row in outcomes),
                                     review_work=dict(primary_states=dict(Counter(classify_position(row["positions"]["pro"]) for row in outcomes)),
                                         primary_denominator=len(outcomes), conditional_third_required=sum(row["third_required"] for row in outcomes),
                                         conditional_third_obtained=sum(row["third_obtained"] for row in outcomes),
                                         role_elapsed_seconds={role: sum((row["positions"].get(role, {}).get("backend_observation") or {}).get("elapsed_seconds") or 0 for row in outcomes) for role in ("pro", "codex", "claude")}),
                                     system_cache=system_cache.report() if system_cache else None,
                                     codex_concurrency_trial=trial.report() if trial else None,
                                     cache_accounting="Native Pro cache tokens across retained positions; local reuse is per invocation; no assumed billing discount"),
                      proof_limits=["Synthetic fixture execution" if not live else "Operational pilot; not statistical accuracy",
                                    "CLI served model is unknown when not emitted by the CLI",
                                    "No reviewed event/report identities are inferred from grades",
                                    "Spending reservations and CLI costs are estimates, not invoices"])
        if profiles:report['live_profiles']=profiles
        if config.get('cache_expiry_recovery'):
            from .cache_recovery import terminal_uncertain_tasks
            report['execution']['retained_terminal_uncertainty_tasks']=sorted(terminal_uncertain_tasks(package))
        if config.get("provider_date_profiles"):
            from .request_profiles import start_disclosures
            report['retrieval_date_controls'] = start_disclosures(package)
            report['report_limits'] = report.get('report_limits', []) + report['retrieval_date_controls']['limitations']
        write_reports(directory, report)
    return report
