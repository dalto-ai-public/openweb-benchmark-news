"""Small read-only operator views and recording of an existing exact approval.

The package, attempt ledger, reports and clean-stage receipts remain the owners.
Nothing here starts work, tests account access, or discovers credential files.
"""
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

from .common import digest, load, records, write_private


def _read(package_path):
    path = Path(package_path).expanduser().resolve()
    package = load(path)
    if not isinstance(package, dict):
        kind = "unsupported"
    elif package.get("profile") in {"clean-staged-run-v1", "clean-staged-run-v2"}:
        kind = "clean"
    elif package.get('profile') == 'quota-third-repair-checkpoint-v1':
        kind = 'quota_repair'
    elif (package.get("schema_version") == 1 and all(k in package for k in
            ("configuration", "providers", "search_requests", "scope"))):
        kind = "native"
    else:
        kind = "unsupported"
    return path, package, kind


def _ref(path):
    path = Path(path)
    return dict(path=str(path), sha256=digest(path.read_bytes()))


def _optional(path):
    return load(path) if path.exists() else None


def _native_status(package, package_path):
    root = Path(package["configuration"]["run_directory"])
    ledger_path, report_path = root / "attempts.jsonl", root / "report.json"
    events = list(records(ledger_path)) if ledger_path.exists() else []
    starts = {e["attempt_id"]: e for e in events if e["event"] == "started"}
    ends = {e["attempt_id"]: e for e in events if e["event"] == "finished"}
    finished_tasks = {e["task_id"] for e in ends.values()}
    search_tasks = {"search-" + digest(dict(configuration=r["configuration_id"], question=r["question_id"])) for r in package["search_requests"]}
    report = _optional(report_path)
    if report is not None and report.get("package_sha256") != digest(package):
        raise ValueError("operator report differs from native package identity")
    execution = report.get("execution", {}) if report else {}
    source = [_ref(package_path)] + ([_ref(ledger_path)] if ledger_path.exists() else [])
    if report is not None:
        source.append(_ref(report_path))
    stops = []
    if execution.get("stopped_reason"):
        stops.append(dict(source="report", reason=execution["stopped_reason"]))
    recovery = execution.get("recovery") or []
    stops.extend(dict(source="reported_recovery", **row) for row in recovery if row.get("mode") in {"stopped", "cooldown"})
    status_path = root / "status-events.jsonl"
    if status_path.exists():
        latest = {}
        for row in records(status_path):
            latest[row.get("account"), row.get("configuration")] = row
        stops.extend(dict(source="status_events", **row) for row in latest.values() if row.get("mode") in {"stopped", "cooldown"})
        source.append(_ref(status_path))
    costs = None
    if ledger_path.exists():
        from .accounting import report_costs
        costs = report_costs(package, root)
    unfinished = [dict(attempt_id=aid, task_id=e["task_id"], state="in_flight_or_interrupted") for aid, e in starts.items() if aid not in ends]
    return dict(kind="native", package_sha256=digest(package),
        state=execution.get("state", "incomplete_or_running" if starts else "prepared"),
        stage=package["configuration"].get("execution_stage", "benchmark"),
        completed=dict(local_finished_attempts=len(ends), local_finished_tasks=len(finished_tasks),
                       local_finished_search_tasks=len(search_tasks & finished_tasks)),
        pending=dict(search_cells_without_local_finished_task=len(search_tasks - finished_tasks),
                     exact_remaining_dispatches=None, frozen_scope=deepcopy(package["scope"]),
                     limits="Local ledger counts include finished failures; reuse and conditional model work prevent an exact remaining-dispatch count."),
        held=dict(reported_tasks=deepcopy(execution.get("held_tasks", [])), unfinished_attempts=unfinished),
        stops=stops, reports=[str(report_path)] if report else [],
        costs=dict(ledger_estimates_and_holds=costs,
                   reported_committed_and_held_usd=execution.get("committed_and_held_usd"),
                   frozen_reservation_forecast=deepcopy(package.get("reservation_forecast")), invoice_verified=False),
        source_evidence=source, verification="Not replayed by status; use offline verify. Report and ledger may reflect different observation times.")


def _review_paths(root):
    return [("ordinary_review", root / "review/package.json"),
            ("accepted_review", root.parent / (root.name + "-accepted-review") / "publication/package.json")]


def _review_status(path, role):
    package = load(path)
    root = Path(package["run_directory"])
    ledger_path = root / "attempts.jsonl"
    events = list(records(ledger_path)) if ledger_path.exists() else []
    starts = {e["attempt_id"]: e for e in events if e["event"] == "started"}
    ends = {e["attempt_id"]: e for e in events if e["event"] == "finished"}
    report_path, closure_path = root / "report.json", root / "closure.json"
    report, closure = _optional(report_path), _optional(closure_path)
    source = [_ref(path)] + ([_ref(ledger_path)] if ledger_path.exists() else [])
    for file, value in ((report_path, report), (closure_path, closure)):
        if value is not None:
            if value.get("package_sha256") != digest(package):
                raise ValueError("operator review report/closure package identity differs")
            source.append(_ref(file))
    wrapper_execution = None
    if role == "accepted_review":
        wrapper_path = path.parent.parent / "review-checkpoint.json"
        wrapper_closure = path.parent.parent / "closure.json"
        if wrapper_path.exists() and wrapper_closure.exists():
            wrapped = load(wrapper_closure)
            if wrapped.get("package_sha256") != digest(load(wrapper_path)):
                raise ValueError("operator accepted review wrapper closure identity differs")
            wrapper_execution = wrapped.get("execution")
            source.extend([_ref(wrapper_path), _ref(wrapper_closure)])
    execution = (report or closure or {}).get("execution", wrapper_execution or {})
    return dict(review_stage=role, package_path=str(path), package_sha256=digest(package),
        state=execution.get("state", "incomplete_or_running" if starts else "prepared"),
        completed=dict(local_finished_attempts=len(ends), local_finished_jobs=len({e["task_id"] for e in ends.values()})),
        pending=dict(declared_jobs_without_local_finish=len({j["task_id"] for j in package["jobs"]} - {e["task_id"] for e in ends.values()})),
        held=dict(reported_tasks=deepcopy(execution.get("held_tasks", [])), unfinished_attempts=[
            dict(attempt_id=aid, task_id=e["task_id"], state="in_flight_or_interrupted") for aid, e in starts.items() if aid not in ends]),
        stops=[dict(source="review_execution", role=r) for r in execution.get("permanent_stop_roles", [])]
              + ([dict(source="review_execution", reason=execution["stopped_reason"])] if execution.get("stopped_reason") else []),
        reports=[str(report_path)] if report else [], closure_path=str(closure_path) if closure else None,
        wrapper_execution=wrapper_execution, source_evidence=source)


def _clean_status(path, package):
    root, stages, reports, stops = path.parent, [], [], []
    source = [_ref(path)]
    for manifest in package["stages"]:
        index = manifest["index"]
        folder = root / ("stage-" + str(index))
        native_path = folder / "native/package.json"
        native = _native_status(load(native_path), native_path) if native_path.exists() else None
        if native:
            stops.extend(dict(stage_index=index, **row) for row in native["stops"])
        reviews = [_review_status(file, role) for role, file in _review_paths(folder) if file.exists()]
        for review_status in reviews:
            stops.extend(dict(stage_index=index, review_stage=review_status["review_stage"], **row) for row in review_status["stops"])
            reports.extend(review_status["reports"])
            source.extend(review_status["source_evidence"])
        gate_path, review_path = folder / "gate-evidence.json", folder / "stage-review.json"
        gate, review = _optional(gate_path), _optional(review_path)
        ordinary = folder / "review/run/report.json"
        accepted = root / (folder.name + "-accepted-review") / "report/report.json"
        state = "native" if native else "not_started"
        if reviews:
            state = reviews[-1]["review_stage"]
        if ordinary.exists():
            state = "ordinary_review"
        if (folder / "closure.json").exists():
            state = "accepted_review"
        if gate is not None:
            state = "awaiting_stage_review"
            source.append(_ref(gate_path))
        if review is not None:
            state = "reviewed_continue" if review.get("decision") == "continue" else "reviewed_stop"
            source.append(_ref(review_path))
        if state == "reviewed_stop":
            stops.append(dict(stage_index=index, source="stage_review", decision=review.get("decision")))
        for candidate in (ordinary, folder / "report/report.json", accepted):
            if candidate.exists():
                reports.append(str(candidate))
        stages.append(dict(index=index, question_count=manifest["count"], state=state, native=native, model_reviews=reviews,
                           gate_costs=deepcopy(gate.get("costs")) if gate else None))
    closure_path = root / "closure.json"
    closure = _optional(closure_path)
    if closure:
        if closure.get("clean_package_sha256") != digest(package):
            raise ValueError("operator closure differs from clean package identity")
        source.append(_ref(closure_path))
    completed = [s["index"] for s in stages if s["state"] == "reviewed_continue"]
    current = next((s for s in stages if s["state"] != "reviewed_continue"), None)
    reviewed_stop = any(s["state"] == "reviewed_stop" for s in stages)
    state = closure.get("state") if closure else "stopped_after_review" if reviewed_stop else current["state"] if current else "awaiting_closure"
    # Each clean native ledger owns only its new stage. Gate costs are cumulative
    # report views and must never be added again to these observed obligations.
    shared = sum((Decimal(s["native"]["costs"]["ledger_estimates_and_holds"]["conservative_total_usd"])
                  for s in stages if s["native"] and s["native"]["costs"]["ledger_estimates_and_holds"] is not None), Decimal(0))
    seed_costs=None
    if package.get('verified_seed'):
        from .clean_run import _read_bound
        prefix=_read_bound(package['verified_seed'])
        seed_costs=prefix['costs'];shared+=Decimal(seed_costs['conservative_total_usd'])
        source.append(deepcopy(package['verified_seed']))
    if package.get('completed_seed'):
        from .clean_run import _verify_bound
        ref=package['completed_seed']['report'];seed_report=load(_verify_bound(ref))
        seed_costs=seed_report['phase_costs'];shared+=Decimal(seed_costs['conservative_total_usd'])
        source.append(deepcopy(ref))
    if package.get('recollection_source'):
        from .clean_run import _verify_bound
        ref=package['recollection_source']['report'];seed_report=load(_verify_bound(ref))
        seed_costs=seed_report['phase_costs'];shared+=Decimal(seed_costs['conservative_total_usd'])
        source.append(deepcopy(ref))
    return dict(kind="clean", package_sha256=digest(package), state=state,
        archive_comparison=package.get("archive_comparison", "bound-archive"),
        stage=current["index"] if current else None, stages=stages,
        completed=dict(reviewed_stages=completed), pending=dict(stage_indices=[s["index"] for s in stages if s["state"] != "reviewed_continue"]),
        held=([dict(stage_index=s["index"], **s["native"]["held"]) for s in stages if s["native"]]
              + [dict(stage_index=s["index"], review_stage=r["review_stage"], **r["held"]) for s in stages for r in s["model_reviews"]]),
        stops=stops, reports=reports, costs=dict(shared_ceiling_usd=package["shared_ceiling_usd"],
            **(dict(repaired_prefix_costs=seed_costs)if seed_costs is not None else {}),
            shared_committed_and_held_usd=str(shared),
            remaining_allowance_usd=str(Decimal(package["shared_ceiling_usd"]) - shared),
            shared_basis="Observed clean native ledger estimates and holds, once per stage, plus any explicitly bound repaired prefix once; cumulative gate costs are not added. Not invoices or dispatch admission approval.",
            stage_costs=[dict(stage_index=s["index"], costs=s["gate_costs"]) for s in stages if s["gate_costs"] is not None],
            native_estimates_and_holds=[dict(stage_index=s["index"], costs=s["native"]["costs"]) for s in stages if s["native"]], invoice_verified=False),
        source_evidence=source, verification="Not replayed by status; use offline verify. Stage files are the state owners.")


def status(package_path):
    """Read current evidence without opening locks, creating files or running work."""
    path, package, kind = _read(package_path)
    if kind == "native":
        return _native_status(package, path)
    if kind == "clean":
        return _clean_status(path, package)
    if kind == 'quota_repair':
        from .clean_run import _read_bound
        child = _read_bound(package['thirds'])
        gate, review = _optional(path.parent/'gate-evidence.json'), _optional(path.parent/'stage-review.json')
        state = ('completed' if review and review['decision']=='continue' else 'stopped_after_review'
            if review else 'awaiting_stage_review' if gate else 'in_progress'
            if (path.parent/'thirds/run/attempts.jsonl').exists() else 'prepared')
        return dict(kind=kind, supported=True, state=state, package_sha256=digest(package),
            missing_third_tasks=len(child['jobs']), maximum_followup_tasks=package['maximum_followup_tasks'],
            carried_conservative_usd=child['parent_conservative_usd'], incremental_api_calls=0)
    return dict(kind=kind, supported=False, reason="Operator views support native and clean-run packages only.")


def _stage_binding(path, package, template, manifest, native):
    """Replay the existing clean native derivation without preparing stage files."""
    from . import clean_run, settlement
    root = path.parent / ("stage-" + str(manifest["index"]))
    checkpoint_path = root / "checkpoint.json"
    if not checkpoint_path.exists():
        return dict(verified=False, reason="existing native package has no clean stage checkpoint")
    parent, carried = None, "0"
    if manifest["index"] == 1 and package.get('completed_seed'):
        seed=clean_run._seed(package['completed_seed'],package['archived_reference'],package['accepted_profile'])
        if seed['review']['decision']!='continue':raise ValueError('repaired prefix review does not permit continuation')
        parent=seed['reference'];carried=seed['report']['phase_costs']['conservative_total_usd']
    replacement=manifest['index']==1 and package.get('recollection_source')
    if replacement:
        evidence=clean_run._recollection(replacement,package['accepted_profile'])
        carried=evidence[0]['phase_costs']['conservative_total_usd']
    if manifest["index"] > 1:
        previous = path.parent / ("stage-" + str(manifest["index"] - 1))
        report_path = clean_run._accepted_root(previous) / "report/report.json"
        parent = clean_run._bound_file(report_path)
        report = settlement.load_parent(report_path)[0]
        gate = load(previous / "gate-evidence.json")
        review = load(previous / "stage-review.json")
        clean_run._validate_review(review, gate)
        if (review["decision"] != "continue" or gate.get("clean_package_sha256") != digest(package)
                or gate.get("stage_index") != manifest["index"] - 1 or gate.get("report") != parent
                or gate.get("costs") != report["phase_costs"]):
            raise ValueError("clean native stage predecessor continuation binding differs")
        for name in ("report", "comparison", "audit", "ordinary_closure", "accepted_closure"):
            clean_run._verify_bound(gate[name])
        carried = report["phase_costs"]["conservative_total_usd"]
    _, expected = clean_run._native(template, manifest["questions"], root,
        package["shared_ceiling_usd"], carried, package["accepted_profile"], package["mode"],
        search_provider_ids=clean_run._recollection_ids(package['accepted_profile'])if replacement else None)
    expected_checkpoint = dict(profile=clean_run.STAGE_PROFILE, clean_manifest_path=str(path),
        clean_package_sha256=digest(package), index=manifest["index"],
        accepted_profile=package["accepted_profile"], parent_report=parent,
        carried_usd=carried, native_package_sha256=digest(expected))
    if package.get('archive_comparison') == clean_run.FIRST_RUN:
        expected_checkpoint['archive_comparison'] = clean_run.FIRST_RUN
    if replacement:expected_checkpoint['recollection_source']=deepcopy(replacement)
    if native != expected or load(checkpoint_path) != expected_checkpoint:
        raise ValueError("clean native stage package/checkpoint derivation differs")
    return dict(verified=True, reason="exact clean native derivation and checkpoint replayed")


def _verify_review_stage(path, clean, manifest, role, stage_root):
    """Known clean subscription stages only; package gates plus retained audit."""
    from . import accepted_review, review_pipeline, settlement
    from .completion_audit import inspect_ledger
    package = load(path)
    bindings = []
    if role == "ordinary_review":
        review_pipeline.verify(package, path.parent)
        native_path, report_path = stage_root / "native/package.json", stage_root / "native/run/report.json"
        if native_path.exists() and report_path.exists():
            bindings.append(package.get("source_package_sha256") == digest(load(native_path))
                            and package.get("source_report_sha256") == digest(report_path.read_bytes())
                            and package.get("questions") == manifest.get("questions"))
        else:
            bindings.append(False)
        bindings.append("ordinary_review_task_bound" in manifest and len(package["jobs"]) <= manifest["ordinary_review_task_bound"])
    else:
        wrapper_path = path.parent.parent / "review-checkpoint.json"
        if wrapper_path.exists():
            wrapper = load(wrapper_path)
            accepted_review.verify(wrapper, wrapper_path.parent)
            bindings.append(wrapper.get("accepted_profile") == clean.get("accepted_profile")
                            and wrapper.get("mode") == clean.get("mode")
                            and wrapper.get("parent_report_path") == str(stage_root / "report/report.json")
                            and "accepted_review_task_bound" in manifest
                            and wrapper.get("maximum_review_tasks") == manifest["accepted_review_task_bound"])
        else:
            settlement.verify(package, path.parent)
            bindings.append(False)  # A child alone cannot authenticate the wrapper's declared scope.
    root = Path(package["run_directory"])
    bindings.append(root.resolve() == (path.parent / "run").resolve())
    classification = dict(state="no_ledger", unclassified_attempt_ids=[])
    if (root / "attempts.jsonl").exists():
        audit = inspect_ledger(root, allow_empty=not package["jobs"])
        classification = dict(state="unclassified" if audit["unclassified"] else "classified",
            unclassified_attempt_ids=deepcopy(audit["unclassified"]), counts=audit["counts"])
    _review_status(path, role)  # Check saved report/closure identities without replaying partial judgments.
    return dict(review_stage=role, package_path=str(path), package_verified=True,
        clean_stage_binding_verified=all(bindings), ledger_classification=classification,
        verified=all(bindings) and not classification["unclassified_attempt_ids"],
        limits="Package/source bindings and retained completion classification only; partial report judgments are not replayed as complete.")


def verify(package_path):
    """Dispatch existing offline verification gates; never run or preflight accounts."""
    path, package, kind = _read(package_path)
    checks = []
    verified = True
    classification = None
    stage_evidence_verified = None
    if kind == "native":
        from .preparation import verify_package
        from .completion_audit import inspect_ledger
        verify_package(package)
        checks.append("native_package_rebuild")
        root = Path(package["configuration"]["run_directory"])
        classification = dict(state="no_ledger", unclassified_attempt_ids=[])
        if (root / "attempts.jsonl").exists():
            from .request_profiles import audit_dispatch
            dispatch_proof = audit_dispatch(package, root)
            if dispatch_proof["verified_attempts"] or dispatch_proof.get("uncertain_attempt_ids"):
                checks.append(dict(search_dispatch=dispatch_proof))
            audit = inspect_ledger(root)
            checks.append(dict(native_completion_ledger=audit["counts"], unclassified=audit["unclassified"]))
            verified = not audit["unclassified"]
            classification = dict(state="classified" if verified else "unclassified", unclassified_attempt_ids=deepcopy(audit["unclassified"]))
        _native_status(package, path)  # Also check the saved report's package identity.
    elif kind == "clean":
        from . import clean_run, settlement
        _, template = clean_run.verify(path)
        if package.get('profile') == clean_run.LEAN_PROFILE:
            gate_checks=clean_run.verify_stage_gates(path,package)
            if not gate_checks:
                clean_run._verified_prefix(package['verified_seed'],package['accepted_profile'],transition=package.get('seed_transition'))
            partial=[]
            for stage in package['stages']:
                folder=path.parent/('stage-'+str(stage['index']))
                if (folder/'native/package.json').exists() and not (folder/'gate-evidence.json').exists():
                    partial.append(dict(stage_index=stage['index'],native=verify(folder/'native/package.json')))
            return dict(kind='clean',supported=True,verified=all(r['native']['verified']for r in partial),
                package_verified=True,package_sha256=digest(package),ledger_classification=None,
                stage_evidence_verified=True if gate_checks else None,
                checks=[dict(verified_gate_chain=gate_checks),dict(current_unclosed_stages=partial)],
                limits='Closed evidence byte hashes and current-stage audit only; historical judgments are not replayed.')
        checks.append("clean_lifecycle_rebuild")
        if package.get('completed_seed'):
            checks.append(dict(accepted_repaired_prefix=deepcopy(package['completed_seed']),verified_by='clean_lifecycle_rebuild'))
        stage_checks = []
        review_checks = []
        for manifest in package["stages"]:
            root = path.parent / ("stage-" + str(manifest["index"]))
            for role, review_path in _review_paths(root):
                if review_path.exists():
                    review_checks.append(dict(stage_index=manifest["index"],
                        **_verify_review_stage(review_path, package, manifest, role, root)))
            native_path = root / "native/package.json"
            if not native_path.exists():
                if (root / "checkpoint.json").exists():
                    stage_checks.append(dict(stage_index=manifest["index"], verified=False,
                        reason="stage checkpoint exists without its native package"))
                continue
            _, stage_native, stage_kind = _read(native_path)
            result = (verify(native_path) if stage_kind == "native" else
                      dict(supported=False, verified=False, reason="stage package is not native"))
            binding = (_stage_binding(path, package, template, manifest, stage_native)
                       if result.get("supported") else dict(verified=False, reason="unsupported stage native package"))
            stage_checks.append(dict(stage_index=manifest["index"], native_verification=result,
                stage_derivation=binding, verified=result["verified"] and binding["verified"]))
        stage_evidence_verified = all(s["verified"] for s in [*stage_checks, *review_checks]) if stage_checks or review_checks else None
        verified = stage_evidence_verified is not False
        checks.append(dict(existing_clean_native_stages=stage_checks))
        checks.append(dict(existing_clean_review_stages=review_checks))
        gate_checks = clean_run.verify_stage_gates(path, package)
        checks.append(dict(existing_clean_stage_gates=gate_checks))
        if (path.parent / "closure.json").exists():
            checks.append("closed_clean_report_and_gates_replay")
    elif kind == 'quota_repair':
        from . import clean_run
        from .common import verify_manifest
        from .completion_audit import inspect_ledger
        clean_run.verify_quota_repair(path)
        gate_path = path.parent/'gate-evidence.json'
        if gate_path.exists():
            gate=clean_run.verify_quota_repair_gate(path,package)
            review_path=path.parent/'stage-review.json'
            if review_path.exists():
                review=load(review_path);clean_run._validate_review(review,gate)
                if review.get('gate_sha256') != _ref(gate_path)['sha256']:
                    raise ValueError('quota review gate bytes changed')
                if review.get('source_review'):clean_run._verify_bound(review['source_review'])
                if review['decision']=='continue':
                    clean_run._verified_prefix(_ref(path.parent/'prefix.json'),package['accepted_profile'],check_files=False,load_evidence=False)
        else:
            gate=clean_run._read_bound(package['source_gate'])
            verify_manifest(clean_run._read_bound(gate['evidence_closure'])['files'])
        checks.append('complete_quota_repair_evidence_closure')
        for directory in (path.parent/'thirds/run',path.parent/'accepted/publication/run'):
            if (directory/'attempts.jsonl').exists():
                if package['mode'] == 'fixture':
                    from . import settlement
                    if not (directory/'closure.json').exists() or load(directory/'closure.json')['execution']['state'] != 'completed':
                        verified=False
                        checks.append(dict(fixture_execution='incomplete'))
                    else:
                        responses,_=settlement._fixture_responses(load(directory.parent/'package.json'))
                        checks.append(dict(fixture_completion_ledger=len(responses),native_inference_verified=False))
                    continue
                audit=inspect_ledger(directory)
                checks.append(dict(native_completion_ledger=audit['counts'],unclassified=audit['unclassified']))
                verified=verified and not audit['unclassified']
        stage_evidence_verified=gate_path.exists()
    else:
        return dict(kind=kind, supported=False, verified=False, reason="No operator verifier for this package kind.")
    return dict(kind=kind, supported=True, verified=verified, package_verified=True,
                stage_evidence_verified=stage_evidence_verified,
                ledger_classification=classification, package_sha256=digest(package), checks=checks,
                limits="Existing offline package/evidence gates only; no account access, human accuracy or paid execution verification.")


def credential_presence(package_path, env_file=None, environ=None):
    """Names and booleans only. Read a dotenv file only when explicitly supplied."""
    _, package, kind = _read(package_path)
    if kind == "unsupported":
        return dict(kind=kind, supported=False, authentication_verified=False)
    if kind == 'quota_repair':
        return dict(kind=kind,supported=True,credentials=[],all_present=True,
            requires_subscription_logins=['claude','codex'],authentication_verified=False,network_calls=0)
    natives = [package] if kind == "native" else [s["native_prototype"] for s in package["stages"]]
    refs, aliases = set(), {}
    for native in natives:
        refs.update(native["required_credential_references"])
        entries = [p["auth"] for p in native["providers"]]
        entries += [native.get("primary", {})]
        entries += list(native["configuration"].get("reviewers", {}).values())
        for entry in entries:
            if entry.get("secret_ref"):
                aliases.setdefault(entry["secret_ref"], set()).update(entry.get("aliases", []))
    from .transport import Credentials
    try:
        credentials = Credentials(env_file, environment=environ)
        found = [dict(reference=ref, present=any(bool(credentials.values.get(name)) for name in [ref, *sorted(aliases.get(ref, []))])) for ref in sorted(refs)]
    except (OSError, ValueError, UnicodeError, TypeError):
        raise ValueError("credential presence source could not be read; no authentication attempted") from None
    return dict(kind=kind, supported=True, credentials=found, all_present=all(r["present"] for r in found),
                sources=["environment", "explicit_env_file"] if env_file is not None else ["environment"],
                authentication_verified=False, network_calls=0)


def record_approval(package_path, receipt, output=None):
    """Record an already explicit owner receipt using the existing exact gates."""
    path, package, kind = _read(package_path)
    if not isinstance(receipt, dict):
        raise ValueError("an existing explicit owner approval receipt is required")
    if kind == "native":
        from .preparation import require_approval
        require_approval(package, receipt)
        scope = deepcopy(package["scope"])
    elif kind == "clean":
        from . import clean_run
        clean_run.verify(path)
        clean_run._approval(package, receipt)
        scope = dict(stages=[dict(index=s["index"], count=s["count"]) for s in package["stages"]], shared_ceiling_usd=package["shared_ceiling_usd"])
    elif kind == 'quota_repair':
        from . import clean_run
        _, child, _ = clean_run.verify_quota_repair(path)
        clean_run._approval(package,receipt)
        scope=dict(missing_third_tasks=len(child['jobs']),maximum_followup_tasks=package['maximum_followup_tasks'],incremental_api_calls=0)
    else:
        raise ValueError("approval recording supports native and clean-run packages only")
    target = Path(output).expanduser().absolute() if output is not None else path.parent / "approval.json"
    existed = target.exists()
    if target.is_symlink() or existed and load(target) != receipt:
        raise ValueError("conflicting existing approval receipt; no file was overwritten")
    if not existed:
        write_private(target, receipt)
    return dict(kind=kind, package_sha256=digest(package), receipt_sha256=digest(receipt),
                approval_path=str(target), already_recorded=existed, scope=scope, execution_started=False)
