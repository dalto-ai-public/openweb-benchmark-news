"""Standalone saved-evidence decode, blind adjudication, correction and reporting."""
from collections import Counter
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from pathlib import Path
from itertools import combinations
import json
import time

from . import adjudication, backends, judging, representation
from .common import asset_bindings, code_bindings, digest, load, now, private_directory, records, write_private
from .ledger import Ledger
from .review import needs_third, settle
from .schema import validate
from .reporting import write_reports
from .scheduling import Pipeline
from .subscription_health import terminal_bind as bind_completion, terminal_verified as verified_completion
from .subscription_health import PROFILE as NORMAL_COMPLETION_PROFILE
from ._method import scoring

PROFILE = "saved-evidence-review-pipeline-v1"


def decode(case, profile, *, date_profile=None):
    positions = {}
    roles = set(case["verdicts"]) | {"pro", "codex"}
    roles |= {r for r,p in case["original"]["positions"].items() if r == "claude" and p.get("state") in {"invalid","unjudged"}}
    for role in sorted(roles, key=lambda r:("pro","codex","claude").index(r)):
        if role not in case["verdicts"]:
            pos = deepcopy(case["original"]["positions"].get(role, {}))
            if pos.get("state") not in {"invalid", "unjudged"}:
                raise ValueError("missing saved verdict for an available position")
            positions[role] = pos
            continue
        value = case["verdicts"][role]
        pos = decode_value(value, case["compiled"], profile, date_profile=date_profile)
        pos.update(independent=True, prior_verdict_exposure=False,
                   backend_observation=case["original"]["positions"].get(role, {}).get("backend_observation"))
        positions[role] = pos
    third = positions.get("claude") if needs_third(positions["pro"], positions["codex"]) else None
    return positions, settle(positions["pro"], positions["codex"], third)


def decode_value(value, compiled, text_profile, *, date_profile=None):
    """Use the same ordered own-text repairs in native and retained decoding."""
    with representation.selected('raw-v1'):
        position=judging.decode_position(value,compiled,date_profile=date_profile)
    profiles=(representation.PROFILE,text_profile)if text_profile==representation.SPACE_PROFILE else(text_profile,)
    for profile in profiles:
        if position['state']!='invalid'or profile=='raw-v1':break
        with representation.selected(profile):
            position=judging.decode_position(value,compiled,date_profile=date_profile)
    return position


def _verdict(role, raw):
    if role != "pro":
        return raw.get("verdict")
    native = json.loads(raw["raw_text"])
    candidate = native["candidates"][0]
    if candidate.get("finishReason") != "STOP":
        raise ValueError("saved primary did not complete")
    return json.loads("".join(p.get("text", "") for p in candidate["content"]["parts"] if not p.get("thought")))


def prepare_review(source_package, question_ids, directory, *, text_profile=None, date_profile=None, replacement_package=None, authority_rebind=None):
    """Copy and bind selected own evidence. Preparation makes no model calls."""
    source = load(source_package)
    from .profile_authority import METHOD_PROFILE, verify_native, verify_case_method
    authority=source['configuration'].get('live_profile_authority')
    source_config = source['configuration']
    if authority_rebind is not None:
        if replacement_package is not None:
            raise ValueError('native authority rebind cannot combine with provider replacement')
        from .clean_run import verify_native_authority_rebind
        authority = verify_native_authority_rebind(source, authority_rebind)
        source_config = dict(source_config, live_profile_authority=authority)
    bound=verify_native(source_config,mode=source['mode'])
    if bound:
        if ((text_profile is not None and text_profile!=bound['text_profile'])
                or(date_profile is not None and date_profile!=bound['date_profile'])):
            raise ValueError('review profiles differ from native authority')
        text_profile=bound['text_profile'];date_profile=bound['date_profile']
    elif date_profile is not None:
        raise ValueError('date review profile requires native authority')
    if text_profile is None:text_profile=representation.PROFILE
    run = Path(source["configuration"]["run_directory"])
    report_bytes = (run / "report.json").read_bytes()
    report = json.loads(report_bytes)
    if report["package_sha256"] != digest(source):
        raise ValueError("source report/package binding differs")
    replacement = load(replacement_package) if replacement_package else None
    replacement_report = None
    if replacement:
        replacement_authority=replacement['configuration'].get('live_profile_authority')
        new_method=(authority or {}).get('profile') == METHOD_PROFILE or source['configuration'].get('judge_input_profile')
        replacement_method=((replacement_authority or {}).get('profile') == METHOD_PROFILE
            or replacement['configuration'].get('judge_input_profile')
            or any(p.get('judge_input_profile') or p.get('date_trust') for p in replacement['providers']))
        if new_method or replacement_method:
            if ((authority or {}).get('profile') != METHOD_PROFILE or authority != replacement_authority or replacement['mode'] != source['mode']
                    or replacement['configuration'].get('reviewers') != source['configuration']['reviewers']):
                raise ValueError('replacement method authority or mode differs from source')
            verify_native(replacement['configuration'],mode=replacement['mode'])
        replacement_run = Path(replacement["configuration"]["run_directory"])
        replacement_report = load(replacement_run / "report.json")
        if (replacement_report["package_sha256"] != digest(replacement)
                or replacement["questions"] != [q for q in source["questions"] if q["question_id"] in question_ids]
                or [p["configuration_id"] for p in replacement["providers"]] != ["brave-web"]
                or replacement["providers"][0].get("request_profile") != "brave-undecorated-v1"
                or replacement_report["execution"]["state"] != "completed"):
            raise ValueError("replacement must be the complete bound Brave subset")
    for name, sha in source["assets"].items():
        if asset_bindings().get(name) != sha:
            raise ValueError("source method assets changed")
    if not question_ids or len(set(question_ids)) != len(question_ids):
        raise ValueError("distinct explicit question ids required")
    questions = [q for q in source["questions"] if q["question_id"] in question_ids]
    if len(questions) != len(question_ids):
        raise ValueError("question outside source run")
    destination = Path(directory).absolute()
    if destination == run or destination.is_relative_to(run):
        raise ValueError("review destination must be separate from source")
    private_directory(destination)
    latest = {e["task_id"]: e for e in records(run / "attempts.jsonl") if e["event"] == "finished"}
    cases, jobs, native_usage = [], [], Counter()
    source_outcomes = [o for o in report["outcomes"] if not replacement or o["configuration_id"] != "brave-web"]
    if replacement:
        source_outcomes += replacement_report["outcomes"]
    source_latest = latest
    for old in source_outcomes:
        if old["question_id"] not in question_ids:
            continue
        cid = old["case_id"]
        selected_source = replacement if replacement and old["configuration_id"] == "brave-web" else source
        run = Path(selected_source["configuration"]["run_directory"])
        latest = ({e["task_id"]:e for e in records(run/"attempts.jsonl") if e["event"]=="finished"}
                  if selected_source is replacement else source_latest)
        compiled = load(run / "compiled" / (cid + ".json"))
        provider = next(p for p in selected_source["providers"] if p["configuration_id"] == old["configuration_id"])
        verify_case_method(authority,compiled,provider)
        judging.verify_compiled(compiled)
        if compiled != judging.compile_packet(compiled["packet"],compiled["input"]["captured_at"],config=provider):
            raise ValueError("source compiler or native applicability binding differs")
        if compiled["packet_sha256"] != old["packet_sha256"]:
            raise ValueError("source compiled packet differs")
        verdicts, provenance = {}, {}
        for role in ("pro", "codex", "claude"):
            event = latest.get(role + "-" + cid)
            if event is None:
                continue
            path = run / event["result_path"]
            raw_bytes = path.read_bytes()
            if digest(raw_bytes) != event["result_sha256"]:
                raise ValueError("source response hash differs")
            request_bytes = (path.parent / "request.json").read_bytes()
            request = json.loads(request_bytes)
            if digest(request) != event["request_sha256"]:
                raise ValueError("source request hash differs")
            if role == "pro":
                if request["wire"] != judging.build_pro_request(compiled):
                    raise ValueError("source primary input differs from retained request")
            elif request["blind_packet"] != judging.blind_packet(compiled):
                raise ValueError("source reviewer input differs from retained request")
            raw = json.loads(raw_bytes)
            from .subscription_health import excluded_intervention
            excluded=excluded_intervention(request,raw,path.parent)
            if raw.get("transport_success") and not excluded:
                try:
                    if role == "pro":
                        retained = judging.response_result(raw.get("raw_text", "").encode(), compiled,
                            http_status=raw.get("http_status") or 0, complete=raw.get("complete", False))
                        value = retained.get("raw_verdict")
                    else:
                        value = _verdict(role, raw)
                except (ValueError, KeyError, IndexError, TypeError):
                    if old["positions"].get(role, {}).get("state") not in {"invalid", "unjudged"}:
                        raise ValueError("saved numeric position has no reproducible native verdict")
                    value = None
                if value is not None:
                    verdicts[role] = value
            provenance[role] = dict(request_sha256=event["request_sha256"], result_sha256=event["result_sha256"],
                                    attempt_id=event["attempt_id"])
            if excluded:provenance[role]['excluded_intervention']=excluded
            if role == "pro" and raw.get("complete") and raw.get("http_status") == 200:
                try:
                    usage = json.loads(raw["raw_text"]).get("usageMetadata", {})
                    if not isinstance(usage, dict):usage = {}
                except (ValueError, TypeError, AttributeError):
                    usage = {}
                for key, val in usage.items():
                    if type(val) is int:
                        native_usage[key] += val
        case = dict(compiled=compiled, verdicts=verdicts, provenance=provenance, original=old, provider=provider)
        positions, baseline = decode(case, text_profile if bound else "raw-v1", date_profile=date_profile)
        if baseline["grade"] != old["grade"] or baseline["states"] != old["states"]:
            raise ValueError("saved source baseline does not reproduce")
        positions, replay = decode(case, text_profile, date_profile=date_profile)
        case["replayed_grade"] = replay["grade"]
        if old["grade"] is not None and replay["grade"] != old["grade"]:
            raise ValueError("representation repair changed a determined grade")
        if replay["third_required"] and "claude" not in positions:
            jobs.append(dict(task_id="third-claude-" + cid, stage="third", role="claude", case_id=cid,
                             blind_packet=judging.blind_packet(compiled)))
        ids = adjudication.eligible(compiled, positions, provider, date_profile=date_profile) if replay["grade"] is None else []
        if ids:
            packet = adjudication.packet_for(compiled, ids)
            for role in ("codex", "claude"):
                jobs.append(dict(task_id="adjudication-" + role + "-" + cid, stage="adjudication", role=role,
                                 case_id=cid, blind_packet=packet))
        name = "inputs/" + cid + ".json"
        write_private(destination / name, case)
        cases.append(dict(case_id=cid, path=name, sha256=digest((destination / name).read_bytes())))
    reviewers = source["configuration"]["reviewers"]
    if (reviewers["codex"].get("backend") != "codex-subscription" or reviewers["codex"].get("transport") != "app-server-stdio-v1"
            or reviewers["claude"].get("backend") != "claude-subscription"):
        raise ValueError("this review profile requires both isolated subscriptions")
    for job in jobs:
        job.update(kind="model", backend=reviewers[job["role"]]["backend"], backend_config=deepcopy(reviewers[job["role"]]),
                   reservation_usd="0", reservation_basis="configured subscription; quota and time separate")
    collection = [r for r in report["collection"] if r["question_id"] in question_ids and (not replacement or r["configuration_id"]!="brave-web")]
    if replacement:
        collection += replacement_report["collection"]
    providers = [replacement["providers"][0] if replacement and p["configuration_id"]=="brave-web" else p for p in source["providers"]]
    package = dict(profile=PROFILE, mode="live", source_package_sha256=digest(source), source_report_sha256=digest(report_bytes),
                   source_method=report["method_version"], created_at=now(), run_directory=str(destination / "run"),
                   text_profile=text_profile, adjudication_profile=adjudication.PROOF_PROFILE,
                   questions=questions, providers=providers, reviewers=reviewers,
                   cases=cases, jobs=jobs, code=code_bindings(), assets=asset_bindings(),
                   scheduling={role + "-subscription":source["configuration"]["execution_policy"]["scheduling"]["accounts"][role + "-subscription"] for role in ("codex", "claude")},
                   source_collection=collection,
                   reference_outcomes=[{k:o[k]for k in ("case_id","configuration_id","question_id","rank","grade","states")}for o in report["outcomes"]if o["question_id"]in question_ids],
                   replacement_package_sha256=digest(replacement)if replacement else None,
                   automatic_recovery=deepcopy((replacement or source)["configuration"].get("automatic_recovery")),
                   recovery={role+"-subscription":deepcopy((replacement or source)["configuration"]["execution_policy"]["scheduling"]["recovery"][role+"-subscription"])for role in ("codex","claude")},
                   retry_policy=deepcopy((replacement or source)["configuration"]["execution_policy"]["retry_policy"]),
                   max_attempts_per_task=(replacement or source)["configuration"]["execution_policy"]["max_attempts_per_task"],
                   coverage_target=source["configuration"]["coverage_target"], tie_order=source["configuration"]["tie_order"],
                   retained_pro_native_usage=dict(native_usage), incremental_api_calls=0,
                   maximum_new_subscription_tasks=len(jobs), retention=deepcopy(source["retention"]))
    if bound:
        package.update(date_profile=date_profile,live_profile_authority=authority)
        from .profile_authority import METHOD_PROFILE, method_options
        if package['live_profile_authority'].get('profile') == METHOD_PROFILE:
            package['mode'] = source['mode']
            package['method_options'] = method_options(source['configuration'])
    if authority_rebind is not None:
        package['native_authority_rebind'] = deepcopy(authority_rebind)
    write_private(destination / "package.json", package)
    return package


def verify(package, root, *, historical=False):
    # Historical compatibility is read-only replay, never dispatch permission.
    assets = asset_bindings()
    compatible_assets = (all(assets.get(k) == v for k,v in package["assets"].items()) if historical
                         else package["assets"] == assets)
    if (package["profile"] != PROFILE or (not historical and package["code"] != code_bindings()) or not compatible_assets
            or package["maximum_new_subscription_tasks"] != len(package["jobs"]) or package["incremental_api_calls"] != 0):
        raise ValueError("review package code/assets/tasks changed")
    from .profile_authority import verify_native, verify_case_method
    authority=package.get('live_profile_authority')
    if package.get('native_authority_rebind') is not None:
        from .clean_run import verify_native_authority_rebind, _read_bound
        record = package['native_authority_rebind']
        completed = _read_bound(record['completion'])
        source = _read_bound(completed['native_package'])
        if (verify_native_authority_rebind(source, record) != authority
                or digest(source) != package['source_package_sha256']
                or completed['report']['sha256'] != package['source_report_sha256']):
            raise ValueError('review native authority/source binding differs')
    bound=verify_native(dict(package.get('method_options',{}),live_profile_authority=authority,reviewers=package['reviewers']),mode=package['mode'])
    if ((bound and (package.get('date_profile')!=bound['date_profile']or package['text_profile']!=bound['text_profile']))
            or(not bound and 'date_profile'in package)):
        raise ValueError('review profile authority differs')
    cases = {}
    expected = []
    for ref in package["cases"]:
        path = Path(root) / ref["path"]
        if path.is_symlink() or not path.resolve().is_relative_to(Path(root).resolve()) or digest(path.read_bytes()) != ref["sha256"]:
            raise ValueError("review input binding differs")
        case = load(path)
        if case["compiled"]["packet"]["case_id"] != ref["case_id"]:
            raise ValueError("review occurrence identity differs")
        cases[ref["case_id"]] = case
        compiled = case["compiled"]
        verify_case_method(authority,compiled,case['provider'])
        if compiled != judging.compile_packet(compiled["packet"], compiled["input"]["captured_at"], config=case["provider"]):
            raise ValueError("review compiler/mapping proof differs")
        positions, replay = decode(case, package["text_profile"], date_profile=package.get("date_profile"))
        cid = ref["case_id"]
        if replay["third_required"] and "claude" not in positions:
            expected.append(dict(task_id="third-claude-" + cid, stage="third", role="claude", case_id=cid,
                                 blind_packet=judging.blind_packet(case["compiled"])))
        ids = adjudication.eligible(case["compiled"], positions, case["provider"], date_profile=package.get("date_profile")) if replay["grade"] is None else []
        if ids:
            for role in ("codex", "claude"):
                expected.append(dict(task_id="adjudication-" + role + "-" + cid, stage="adjudication", role=role,
                                     case_id=cid, blind_packet=adjudication.packet_for(case["compiled"], ids,
                                         profile=package.get("adjudication_profile", adjudication.PROFILE))))
    for job in expected:
        config = package["reviewers"][job["role"]]
        job.update(kind="model",backend=config["backend"],backend_config=deepcopy(config),reservation_usd="0",
                   reservation_basis="configured subscription; quota and time separate")
    if expected != package["jobs"]:
        raise ValueError("review tasks differ from the eligible blind route")
    return cases


def run_review(package_path, *, approval=None, call=None):
    """Resume a declared recoverable runtime epoch while real work progresses."""
    from .resilience import enabled
    p=load(package_path)
    if not enabled(p):
        return _run_review(package_path,approval=approval,call=call)
    from .codex_runtime import RuntimeStopped
    path=Path(p['run_directory'])/'attempts.jsonl'
    def started_count():return sum(e['event']=='started'for e in records(path))if path.exists()else 0
    while True:
        before=started_count()
        try:
            report=_run_review(package_path,approval=approval,call=call)
        except RuntimeStopped:
            if started_count()<=before:raise
            continue
        if report['execution']['state']!='completed_with_holds'or report['execution'].get('permanent_stop_roles')or started_count()<=before:
            return report


def _run_review(package_path, *, approval=None, call=None):
    """One command performs replay, blind reviews, accepted corrections and reports."""
    package_path = Path(package_path)
    package = load(package_path)
    cases = verify(package, package_path.parent)
    live = package["mode"] == "live"
    if live and (not approval or approval.get("approved") is not True or approval.get("package_sha256") != digest(package)):
        raise ValueError("exact approved review package required")
    if live and call is not None:
        raise ValueError("live review cannot replace its frozen transport")
    with review_execution(package, call=call) as (responses, stopped, pending, started, directory):
        outcomes, records_out, labels, reasons, findings = [], [], {}, {}, {}
        delta = Counter()
        for cid, case in cases.items():
            positions, replay = decode(case, package["text_profile"], date_profile=package.get("date_profile"))
            original_grade = case["original"]["grade"]
            replay_grade = replay["grade"]
            extra = responses.get("third-claude-" + cid)
            if extra and (extra.get("native_completion_verified")or extra.get('native_intervention_excluded')):
                with representation.selected(package["text_profile"]):
                    p = (decode_value(extra.get("verdict"), case["compiled"], package["text_profile"], date_profile=package["date_profile"])
                         if package.get("date_profile") else judging.decode_position(extra.get("verdict"), case["compiled"]))
                p.update(independent=True, prior_verdict_exposure=False)
                if extra.get('native_intervention_excluded'):
                    from .subscription_health import excluded_position
                    p=excluded_position(extra['native_intervention_excluded'])
                positions["claude"] = p
                replay = settle(positions["pro"], positions["codex"], p)
            own = {role:responses["adjudication-" + role + "-" + cid] for role in ("codex", "claude")
                   if "adjudication-" + role + "-" + cid in responses}
            if own:
                ids = adjudication.eligible(case["compiled"], positions, case["provider"], date_profile=package.get("date_profile"))
                with representation.selected(package["text_profile"]):
                    effective, record = adjudication.corroborate(case["compiled"], positions, adjudication.packet_for(case["compiled"], ids,
                        profile=package.get("adjudication_profile", adjudication.PROFILE)), own, case["provider"], date_profile=package.get("date_profile"))
                record["case_id"] = cid
                records_out.append(record)
                if record["accepted"]:
                    replay = settle(effective["pro"], effective["codex"])
            if original_grade is not None and replay["grade"] != original_grade:
                raise ValueError("review changed an already determined grade")
            delta["representation_recoveries"] += original_grade is None and replay_grade is not None
            delta["additional_third_recoveries"] += replay_grade is None and extra is not None and replay["grade"] is not None and not own
            delta["policy_recoveries"] += bool(own) and records_out[-1]["accepted"] and replay["grade"] is not None
            old = case["original"]
            result = dict(old, **replay, original_grade=original_grade, source_positions=old["positions"],
                          representation_profile=package["text_profile"])
            outcomes.append(result)
            labels[cid], reasons[cid] = result["grade"], result["reason"]
            selected = result["positions"].get(result["selected_roles"][0], {}) if result["selected_roles"] else {}
            judgment = selected.get("conformance_judgment") or selected.get("judgment") or {}
            findings[cid] = [dict(report_id=None, event_id=None, facet_ids=c["supported_facet_ids"])
                            for c in judgment.get("candidates", []) if c["answer_supported"]] if result["grade"] == 2 else []
        cells = {}
        for row in package["source_collection"]:
            ranked = [dict(rank=o["rank"], evidence_id=o["case_id"], url=cases[o["case_id"]]["compiled"]["packet"]["returned_url"])
                      for o in sorted(outcomes,key=lambda o:o["rank"]) if o["configuration_id"] == row["configuration_id"] and o["question_id"] == row["question_id"]]
            cells[row["configuration_id"],row["question_id"]] = dict(status=row["status"], ranked=ranked)
        scores = scoring.score(package["questions"], [p["configuration_id"] for p in package["providers"]], cells,
                               labels,reasons,package["coverage_target"],package["tie_order"],findings=findings)
        pairs=[]
        for k in (3,5,10):
            for name in ('answer','useful'):
                values={(r['question_id'],r['configuration_id']):r['metrics'][name]for r in scores['question_rows']if r['k']==k and r['metrics'] is not None}
                for a,b in combinations([p['configuration_id']for p in package['providers']],2):
                    pairs.append(dict(metric=f'{name}@{k}',**scoring.paired(package['questions'],a,b,scores['availability']['answered_question_ids'],values)))
        missing = [j["task_id"] for j in package["jobs"] if not responses[j["task_id"]].get("native_completion_verified")
                   and not responses[j['task_id']].get('native_intervention_excluded')]
        report = dict(method_version=package["source_method"],mode=PROFILE,package_sha256=digest(package),
                      generated_at=now(),outcomes=outcomes,scores=scores,collection=package["source_collection"],resolved_pairwise=pairs,
                      outcome_counts=dict(Counter("unknown" if r["grade"] is None else str(r["grade"])for r in outcomes)),
                      execution=dict(state="completed_with_holds" if missing else "completed",held_tasks=missing,
                                     permanent_stop_roles=sorted(stopped),
                                     reused_tasks=len(package["jobs"])-len(pending),elapsed_seconds=time.monotonic()-started),
                      review_pipeline=dict(profile=PROFILE,original_unknowns=sum(c["original"]["grade"] is None for c in cases.values()),
                                           final_unknowns=sum(r["grade"]is None for r in outcomes),changes=dict(delta),
                                           question_ids=[q["question_id"]for q in package["questions"]],
                                           selection_limit="Enriched five-question feasibility sample; not an unbiased recovery forecast.",
                                           records=records_out,new_search_calls=0,new_pro_calls=0,incremental_api_usd="0",
                                           source_report_sha256=package["source_report_sha256"],
                                           new_subscription_tasks=len(pending),maximum_subscription_tasks=len(package["jobs"])))
        if package.get('live_profile_authority'):
            from .profile_authority import profiles
            report['live_profiles']=profiles(package['live_profile_authority'])
        write_reports(directory, report)
        summary = report["review_pipeline"]
        fixture_evidence={}
        from .settlement import fixture_method, _fixture_responses
        if fixture_method(package) and not missing:
            retained,ledger_sha=_fixture_responses(package)
            fixture_evidence=dict(fixture_ledger_sha256=ledger_sha,
                fixture_response_hashes={task:digest(raw) for task,raw in retained.items()})
        write_private(directory/"closure.json",dict(package_sha256=digest(package),
                      report_sha256=digest((directory/"report.json").read_bytes()),execution=report["execution"],**summary,**fixture_evidence),replace=True)
        return report


@contextmanager
def review_execution(package, *, call=None):
    """Shared isolated subscription executor; reporting stays under the run lock."""
    live = package["mode"] == "live"
    started = time.monotonic()
    directory = private_directory(package["run_directory"])
    from .resilience import enabled as automatic_recovery
    automatic=automatic_recovery(package)
    ledger = Ledger(directory, dict(package_sha256=digest(package), mode=package["mode"]),uncertain_resends=1 if automatic else 0)
    responses, preflights, stopped = {}, {}, set()
    with ledger.locked(), ExitStack() as stack:
        # Inspect every task first: no login/runtime is needed for a no-send resume.
        maximum=package.get("max_attempts_per_task",1)
        statuses=package.get("retry_policy",{}).get("http_statuses",())
        pending = [j for j in package["jobs"] if ledger.inspect(j["task_id"], j, max_attempts=maximum, retry_failed=automatic, retry_http_statuses=statuses)["dispatch"]]
        runtime = None
        if live and pending:
            for role in sorted({j["role"] for j in pending}):
                preflights[role] = backends.subscription(package["reviewers"][role], {}, directory,
                                                         preflight_only=True)
                if not preflights[role].get("transport_success"):
                    raise ValueError("selected subscription preflight failed")
            if "codex" in preflights:
                from .codex_runtime import CodexRuntime
                runtime = stack.enter_context(CodexRuntime(package["reviewers"]["codex"], directory / "codex-runtime", preflight=preflights["codex"]))
        pipeline = Pipeline(package["scheduling"],recovery=package.get("recovery"))
        if runtime:
            from .quota import QuotaGate
            pipeline.quota_gate = QuotaGate(runtime.client, directory / "codex-runtime")
        def execute(job):
            role = job["role"]
            if role in stopped or runtime and role == "codex" and runtime.client.failure:
                return dict(transport_success=False, complete=False, dispatch_state="not_sent", held_reason="affected subscription stopped")
            def transport(folder):
                if not live:
                    raw = call(job, folder)
                elif role == "codex":
                    raw = runtime.review(job["blind_packet"], folder)
                else:
                    raw = backends.subscription(job["backend_config"], job["blind_packet"], folder, preflight=preflights[role])
                if automatic and raw.get("recoverable_runtime_failure"):
                    raw=dict(raw,fatal_configuration=False)
                proof = bind_completion(job, raw, folder) if live else None
                if raw.get("fatal_configuration") or raw.get("native_quota_evidence"):
                    stopped.add(role)
                return dict(raw, subscription_completion_evidence=proof,
                            native_completion_verified=bool(proof) if live else raw.get("native_completion_verified") is True)
            raw = ledger.execute(job["task_id"], job, transport,max_attempts=maximum,retry_failed=automatic,retry_http_statuses=statuses)
            if live:
                event = next(e for e in reversed(ledger.events()) if e.get("event") == "finished" and e["task_id"] == job["task_id"])
                folder = directory / event["result_path"]
                from .subscription_health import excluded_intervention
                excluded=excluded_intervention(job,raw,folder.parent)
                raw=dict(raw,native_completion_verified=False,native_intervention_excluded=excluded)if excluded else dict(
                    raw,native_completion_verified=verified_completion(job,raw,folder.parent))
            return raw
        def submit(job,ready_at=0):
            account=job["role"]+"-subscription"
            def finished(raw):
                if pipeline.recovery:
                    event=next((e for e in reversed(ledger.events())if e.get('event')=='finished'and e['task_id']==job['task_id']),None)
                    normal_health = (raw.get("native_completion_verified") is True and
                                     (raw.get("subscription_completion_evidence") or {}).get("profile") == NORMAL_COMPLETION_PROFILE)
                    pipeline.observe(event['attempt_id']if event else digest(dict(task=job["task_id"],response=raw)),account,job["stage"],raw,time.time(),job["task_id"],native_completion=normal_health)
                if automatic and not raw.get('fatal_configuration')and not raw.get('native_quota_evidence')and (raw.get("dispatch_state")=="uncertain" or raw.get("http_status")in statuses):
                    state=ledger.inspect(job["task_id"],job,max_attempts=maximum,retry_failed=True,retry_http_statuses=statuses)
                    if state["dispatch"]:
                        delays=package["retry_policy"]["backoff_seconds"]
                        delay=delays[state["attempt"]-2]if isinstance(delays,list)else delays
                        submit(job,time.time()+delay)
                        return
                responses[job["task_id"]]=raw
            pipeline.submit(account,lambda:execute(job),finished,ready_at=ready_at,scope=job["stage"])
        for job in package["jobs"]:
            submit(job)
        pipeline.run()
        yield responses, stopped, pending, started, directory
