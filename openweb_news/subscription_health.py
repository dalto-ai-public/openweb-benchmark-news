"""Opt-in native completion audits. These checks never accept a judgment vote."""
import json
from pathlib import Path

from .common import digest, load, records


PROFILE = "native-subscription-completion-v1"
RECOVERED_CODEX_V2 = "codex-retried-terminal-completion-v2"


def _denied_retry(error):
    """Typed native auth/quota/spend failures never gain completion authority."""
    def codes(value):
        if isinstance(value,str):return [value]
        if isinstance(value,dict):
            if value.get('httpStatusCode')in {401,402,429}:return ['auth_or_quota']
            return list(value)+[c for v in value.values()if isinstance(v,(dict,list))for c in codes(v)]
        if isinstance(value,list):return [c for v in value for c in codes(v)]
        return []
    values=codes(error.get('codexErrorInfo'))+codes(error.get('code'))+codes(error.get('type'))
    return any(word in code.lower()for code in values for word in ('auth','quota','ratelimit','usagelimit','credit','billing','spend','context','sandbox','permission'))


def _model_matches(params,model):
    # Inspect protocol provenance, never model-like text in user/answer content.
    for obj in (params,params.get('thread')or{},params.get('turn')or{}):
        for key,value in obj.items():
            if key.lower().endswith(('model','modelslug'))and value is not None and value!=model:
                return False
    return True


def validate(value, reviewers, scheduling):
    if value is None:
        return
    if (not isinstance(value, dict) or set(value) != {"profile", "roles"}
            or value["profile"] != PROFILE or not isinstance(value["roles"], list)
            or not value["roles"] or len(set(value["roles"])) != len(value["roles"])):
        raise ValueError("explicit native subscription health profile and distinct roles required")
    for role in value["roles"]:
        reviewer = reviewers.get(role, {})
        if (role not in {"codex", "claude"} or reviewer.get("backend") != role + "-subscription"
                or role == "codex" and reviewer.get("transport") != "app-server-stdio-v1"
                or role + "-subscription" not in scheduling.get("recovery", {})):
            raise ValueError("native health requires the selected isolated subscription transport and recovery")


def audit(request, response, folder):
    """Verify retained native evidence, not a trusted response flag or valid grade.

    The caller binds request/result and evidence hashes. Missing or abnormal native
    evidence returns false, leaving ordinary source recovery in force.
    """
    return _audit(request, response, folder)


def _audit(request, response, folder, *, recovered=False, intervention=False):
    try:
        role, config, packet = request["role"], request["backend_config"], request["blind_packet"]
        if (role not in {"codex", "claude"} or request["backend"] != role + "-subscription"
                or config["backend"] != request["backend"]
                or response.get("selected_backend") != request["backend"]
                or response.get("transport_success") is not True or response.get("complete") is False
                or response.get("dispatch_state") != "completed" or response.get("http_status") not in {None, 200}
                or response.get("account_mode") != "subscription" or response.get("fatal_configuration")
                or response.get("error") or response.get("errors") or response.get("error_type")
                or response.get("native_quota_evidence") or response.get("credential_echo_redacted")
                or response.get("verdict") is None
                or response.get("requested_model") != config["model"]
                or response.get("requested_settings") != dict(reasoning_effort=config["reasoning_effort"])):
            return False
        return _codex(config, packet, response, folder, recovered=recovered) if role == "codex" else _claude(config, response, folder, recovered=recovered, intervention=intervention)
    except (OSError, KeyError, ValueError, TypeError, AttributeError):
        return False


def _codex(config, packet, response, folder, *, recovered=False):
    from .codex_runtime import THREAD_PASSIVE_NOTIFICATIONS, HANDLED_THREAD_NOTIFICATIONS
    from .concurrency import quota_signal
    if (config.get("transport") != "app-server-stdio-v1" or response.get("complete") is not True
            or response.get("selected_transport") != config["transport"] or response.get("auth_observation") != "chatgpt"):
        return False
    thread = load(folder / "runtime-thread.json")
    native_request = load(folder / "runtime-request.json")
    start, turn = native_request["thread_start"], native_request["turn_start"]
    tid, uid = response["thread_id"], response["turn_id"]
    if (not tid or not uid or thread["thread"].get("id") != tid or thread.get("instructionSources") != []
            or thread.get("model") != config["model"] or thread.get("modelProvider") != "openai"
            or thread.get("reasoningEffort") != config["reasoning_effort"] or thread.get("approvalPolicy") != "never"
            or thread.get("sandbox") != dict(type="readOnly", networkAccess=False)
            or thread["thread"].get("ephemeral") is not True or thread["thread"].get("turns")
            or thread["thread"].get("forkedFromId") or thread["thread"].get("parentThreadId")
            or start.get("model") != config["model"] or start.get("baseInstructions") != packet["prompt"]
            or start.get("ephemeral") is not True or start.get("approvalPolicy") != "never"
            or start.get("sandbox") != "read-only" or start.get("developerInstructions") != ""
            or start.get("dynamicTools") != [] or start.get("allowProviderModelFallback") is not False
            or turn.get("threadId") != tid or turn.get("model") != config["model"]
            or turn.get("effort") != config["reasoning_effort"] or turn.get("outputSchema") != packet["schema"]
            or len(turn["input"]) != 1 or turn["input"][0].get("type") != "text"
            or json.loads(turn["input"][0]["text"]) != packet["input"]):
        return False
    echoes, answers, completions = [], [], []
    active_turn=False
    for event in records(folder / "runtime-events.jsonl"):
        method, params = event.get("method"), event.get("params", {})
        retry_event = (recovered and method == "error" and not completions
                       and params.get("threadId") == tid and params.get("turnId") == uid
                       and params.get("willRetry") is True
                       and not _denied_retry(params.get('error')or{})
                       and (params.get("error", {}).get("codexErrorInfo") == {"responseStreamDisconnected": {"httpStatusCode": 403}}
                            or recovered=='v2'and active_turn))
        if (method not in THREAD_PASSIVE_NOTIFICATIONS | HANDLED_THREAD_NOTIFICATIONS or (method == "error" and not retry_event)
                or event.get("runtime_failure") or quota_signal(event)
                or params.get("threadId") not in {None, tid} or params.get("turnId") not in {None, uid}):
            return False
        if recovered=='v2'and(not _model_matches(params,config['model'])
                or method.startswith('modelProvider/authRecovery')
                or completions and(method in {'warning','turn/started'}or method.startswith('item/'))):
            return False
        if recovered=='v2':
            if method=='turn/started':
                if active_turn or params.get('threadId')!=tid or params.get('turn',{}).get('id')!=uid:return False
                active_turn=True
            if method=='error'and not active_turn:return False
            if method=='warning'and params.get('threadId')!=tid:return False
            # Native thread-scoped warnings omit turnId. The fresh ephemeral
            # thread and its single bound turn supply that scope; conflicting
            # explicit IDs were rejected above.
        if method.startswith("modelProvider/authRecovery") and params.get("provider") != "openai":
            return False
        if method == "model/safetyBuffering/updated" and (params.get("model") != config["model"] or params.get("fasterModel") not in {None, config["model"]}):
            return False
        if method in {"item/started", "item/completed"}:
            item = params["item"]
            if item.get("type") not in {"userMessage", "agentMessage", "reasoning"} or item.get("memoryCitation"):
                return False
            if method == "item/completed":
                if item["type"] == "userMessage":
                    echoes.append(json.loads("".join(part.get("text", "") for part in item["content"])))
                if item["type"] == "agentMessage" and item.get("phase") != "commentary":
                    answers.append(json.loads(item["text"]))
        if method == "turn/completed":
            if params["turn"].get("error"):
                return False
            completions.append(params)
    return (echoes == [packet["input"]] and answers == [response.get("verdict")]
            and len(completions) == 1 and completions[0].get("threadId") == tid
            and completions[0]["turn"].get("id") == uid and completions[0]["turn"].get("status") == "completed")


def _claude(config, response, folder, *, recovered=False, intervention=False):
    if response.get("auth_observation") not in {"claude.ai", "oauth_token"} or response.get("exit_code") != 0:
        return False
    events = list(records(folder / "stdout.jsonl"))
    init = [e for e in events if e.get("type") == "system" and e.get("subtype") == "init"]
    final = [e for e in events if e.get("type") == "result"]
    if len(init) != 1 or len(final) != 1 or events[-1] != final[0]:
        return False
    setup, end = init[0], final[0]
    if recovered:
        session = setup.get("session_id")
        if (events[0] != setup or not session or end.get("session_id") != session
                or any(e.get("session_id") not in {None, session} for e in events)):
            return False
    if (setup.get("model") != config["model"] or response.get("served_model") != config["model"]
            or setup.get("analytics_disabled") is not True or setup.get("product_feedback_disabled") is not True
            or setup.get("mcp_servers") or setup.get("skills") or set(setup.get("tools", [])) - {"StructuredOutput"}
            or any(p.get("name") not in {"cc-plugin-agents-md", "cc-plugin-plugin-authoring"}
                   or p.get("path") != "builtin" or not str(p.get("source", "")).endswith("@builtin") for p in setup.get("plugins", []))
            or end.get("subtype") != "success" or end.get("is_error") is not False
            or end.get("api_error_status") or end.get("permission_denials") or (end.get("safety_stops") and not intervention)
            or end.get("structured_output") != response.get("verdict")):
        return False
    for event in events:
        if event.get("type") == "rate_limit_event":
            rate = event.get("rate_limit_info", {})
            if rate.get("status") not in {"allowed", "allowed_warning"}:
                return False
        retry = (recovered and event.get("type") == "system" and event.get("subtype") == "api_retry"
                 and set(event) == {"type", "subtype", "attempt", "max_retries", "retry_delay_ms", "error_status", "error", "session_id", "uuid"}
                 and isinstance(event.get("uuid"), str) and bool(event["uuid"])
                 and event.get("session_id") == session and event.get("error") == "unknown"
                 and "error_status" in event and event["error_status"] is None
                 and type(event.get("attempt")) is int and type(event.get("max_retries")) is int
                 and 1 <= event["attempt"] <= event["max_retries"]
                 and type(event.get("retry_delay_ms")) is int and event["retry_delay_ms"] >= 0)
        if (event.get("type") in {"error", "auth_status"}
                or (event.get("error") and not retry)
                or (recovered and event.get("subtype") == "api_retry" and not retry)):
            return False
        if event.get("type") == "assistant":
            message = event.get("message", {})
            if message.get("model") not in {None, config["model"]} or any(
                    p.get("type") == "tool_use" and p.get("name") != "StructuredOutput" for p in message.get("content", [])):
                return False
    return True


def excluded_intervention(request,response,folder):
    """Classify a completed provider-intervened response, never accept its vote."""
    try:
        response={k:v for k,v in response.items()if k not in
            {'native_intervention_excluded','native_completion_verified','subscription_completion_evidence'}}
        if request.get('role')!='claude' or not _audit(request,response,folder,intervention=True):return None
        final=[e for e in records(folder/'stdout.jsonl')if e.get('type')=='result']
        stops=final[0].get('safety_stops')
        if type(stops)is not int or stops<=0:return None
        return dict(profile='excluded-provider-intervention-v1',vote_eligible=False,normal_health=False,
            reason='native provider safeguard intervention',safety_stops=stops,
            request_sha256=digest(request),response_sha256=digest(response),
            files={name:digest((folder/name).read_bytes())for name in evidence_names('claude')})
    except (OSError,KeyError,ValueError,TypeError,AttributeError,IndexError):return None


def completion_view(request,response,folder):
    """A terminal exclusion is complete evidence, but never a verified vote."""
    excluded=excluded_intervention(request,response,folder)
    if excluded:return dict(response,native_completion_verified=False,native_intervention_excluded=excluded)
    return dict(response,native_completion_verified=terminal_verified(request,response,folder))


def excluded_position(proof):
    return dict(state='unjudged',grade=None,errors=['Native provider intervention; output excluded'],
        voting_exclusion=proof,independent=True,prior_verdict_exposure=False)


def evidence_names(role):
    return ["runtime-thread.json", "runtime-request.json", "runtime-events.jsonl"] if role == "codex" else ["stdout.jsonl", "stderr.txt"]


def bind(request, response, folder):
    """Seal native files into the ledger result before any health exemption."""
    if not audit(request, response, folder):
        return None
    return dict(profile=PROFILE, files={name: digest((folder / name).read_bytes()) for name in evidence_names(request["role"])})


def verified(request, response, folder):
    proof = response.get("subscription_completion_evidence")
    return bool(proof and proof == bind(request, response, folder))


def recovered_completion(request, response, folder):
    """Read-only proof of a completed turn after retained native reconnects.

    This is NOT normal account health, a retry authorization or a grade. The
    native runtime already completed this same thread/turn. Keep its original
    missing health proof and every native event unchanged. Preserve v1 proofs
    first; Codex v2 then verifies retry/terminal invariants without matching a
    particular transport error message or status. Auth/quota/spend still deny.
    Claude's native log does not echo the full input: its proof binds the retained
    ledger request and native session/output, not an independently echoed prompt.
    """
    try:
        role = request.get("role")
        if role not in {"codex", "claude"}:
            return None
        mode=next((mode for mode in ([True,'v2']if role=='codex'else[True])
                   if _audit(request,response,folder,recovered=mode)),None)
        if mode is None:return None
        path = folder / ("runtime-events.jsonl" if role == "codex" else "stdout.jsonl")
        errors = [e for e in records(path) if e.get("method") == "error" or
                  (role == "claude" and e.get("type") == "system" and e.get("subtype") == "api_retry")]
        if not errors:
            return None
        profile=(RECOVERED_CODEX_V2 if mode=='v2'else 'codex-reconnected-terminal-completion-v1')if role=='codex'else 'claude-retried-terminal-completion-v1'
        return dict(profile=profile, error_count=len(errors),
                    normal_health=False, request_sha256=digest(request), response_sha256=digest(response),
                    files={name:digest((folder/name).read_bytes()) for name in evidence_names(role)})
    except (OSError, KeyError, ValueError, TypeError, AttributeError):
        return None


def terminal_bind(request, response, folder):
    """Completion proof for review/reporting; never a normal-health exemption."""
    return bind(request, response, folder) or recovered_completion(request, response, folder)


def terminal_verified(request, response, folder):
    proof = response.get("subscription_completion_evidence")
    if not proof:
        return False
    # Recovered proofs bind the original response before its proof was attached.
    original = dict(response)
    if proof.get("normal_health") is False:
        original.pop("subscription_completion_evidence", None)
        original.pop("native_completion_verified", None)
    return proof == terminal_bind(request, original, folder)


def transition_attempts(package):
    """Validate a separately approved, once-only account epoch from a checkpoint.

    Preparation only derives this set. Execution still requires the exact new
    package's owner receipt. All selected old observations, including successes,
    stay archived and supply no new health/trial credit. Votes and costs remain.
    """
    config = package["configuration"]
    transition = config.get("subscription_health_transition")
    if transition is None:
        return set()
    from .reuse import read_snapshot, superseded_geography_health, committed
    from .recovery import Recovery
    from .scheduling import timestamp
    keys = {"profile", "source_snapshot", "source_package_sha256", "roles", "attempt_ids", "archived_recovery_sha256"}
    if (set(transition) != keys or transition["profile"] != "native-subscription-health-epoch-v1"
            or not transition["roles"] or len(set(transition["roles"])) != len(transition["roles"])
            or not set(transition["roles"]) <= set(config.get("subscription_completion_health", {}).get("roles", []))):
        raise ValueError("explicit native health transition and enabled roles required")
    reference = transition["source_snapshot"]
    current_reference = config.get("reuse_snapshot")
    if not current_reference:
        raise ValueError("native health transition requires retained exact-task reuse")
    manifest = read_snapshot(reference)
    root = Path(reference["manifest_path"]).parent
    source = load(root / "source-package.json")
    current_questions = {q["question_id"]: q for q in package["questions"]}
    if (digest(source) != transition["source_package_sha256"] or any(current_questions.get(q["question_id"]) != q for q in source["questions"])
            or source["configuration"]["reviewers"] != config["reviewers"]
            or source["configuration"].get("subscription_health_transition")
            or manifest["statistics"]["uncertain_attempts"]):
        raise ValueError("native health epoch requires the exact idle source, unchanged judges/questions and no previous epoch")
    events = list(records(root / "events.jsonl"))
    read_snapshot(current_reference)
    current_root = Path(current_reference["manifest_path"]).parent
    current_source = load(current_root / "source-package.json")
    if (current_reference != reference and
            (current_source["configuration"].get("subscription_health_transition") != transition
             or list(records(current_root / "events.jsonl"))[:len(events)] != events)):
        raise ValueError("native health transition requires the exact checkpoint or its preserved continuation prefix")
    if len(events) != 2 * manifest["statistics"]["started_attempts"]:
        raise ValueError("native health epoch cannot retire incomplete source attempts")
    source_policy = source["configuration"]["execution_policy"]
    from decimal import Decimal
    prior = Decimal(0)
    if source["configuration"].get("prior_work"):
        from .prior_work import read
        prior = Decimal(read(source["configuration"]["prior_work"], source)["committed_and_held_usd"])
    elif source["configuration"].get("fresh_judging"):
        from .fresh import validate as validate_fresh
        fresh, _ = validate_fresh(source["configuration"]["fresh_judging"], source)
        prior = Decimal(fresh["summary"]["carried_cost_usd"])
    if prior + committed(root, source, events) >= Decimal(str(source_policy["spending_ceiling_usd"])):
        raise ValueError("native account transition cannot reset global spending admission")
    journal = root / "codex-runtime" / "quota-events.jsonl"
    quota = list(records(journal)) if journal.exists() else []
    if "codex" in transition["roles"] and quota:
        raise ValueError("native quota evidence cannot be retired by this transition")
    retired, expected = superseded_geography_health(source), set()
    recovery = Recovery(source_policy["scheduling"]["recovery"])
    bound_names = {f["path"] for f in manifest["files"]}
    ordered = [(timestamp(event["recorded_at"]), False, event) for event in events]
    ordered.extend((event["observed_at"], True, event) for event in quota)
    for when, native_quota, event in sorted(ordered, key=lambda row: row[:2]):
        if native_quota:
            recovery.observe("native-quota-" + event["event_id"], "codex-subscription", "codex",
                dict(complete=True, dispatch_state="completed", http_status=429, transport_success=False), when)
            continue
        request = load(root / "attempts" / event["attempt_id"] / "request.json")
        stage = "search/" + request["configuration_id"] if request["kind"] == "search" else request["role"]
        account = source["account_bindings"][stage]
        if event["event"] == "started":
            recovery.capacity(account, stage, when)
            continue
        result = load(root / event["result_path"])
        if stage in transition["roles"]:
            names = {f"attempts/{event['attempt_id']}/{name}" for name in evidence_names(stage)}
            if not names <= bound_names or not audit(request, result, root / "attempts" / event["attempt_id"]):
                raise ValueError("transition includes an unaudited native completion, transport/auth/quota/protocol failure")
            expected.add(event["attempt_id"])
        if event["attempt_id"] not in retired:
            native_completion = stage in source["configuration"].get("subscription_completion_health", {}).get("roles", []) and verified(request, result, root / "attempts" / event["attempt_id"])
            recovery.observe(event["attempt_id"], account, stage, result, when, result.get("recovery_validation_state"), native_completion=native_completion)
    if sorted(expected) != transition["attempt_ids"] or digest(recovery.describe()) != transition["archived_recovery_sha256"]:
        raise ValueError("native health transition differs from its exact archived observations")
    for role in transition["roles"]:
        state = recovery.scope(source["account_bindings"][role])
        if state["mode"] != "stopped" or state["pauses"] != source_policy["scheduling"]["recovery"][source["account_bindings"][role]]["repeated_pause_stop"]:
            raise ValueError("native health epoch requires the archived repeated-pause account stop")
    return expected
