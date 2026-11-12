"""Explicit saved-collection/new-judge epoch, separate from exact-task reuse.

The source snapshot stays complete and immutable. Only captured search evidence,
source cost obligations and uncertain-task holds cross into the new epoch.
"""
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
import json

from .accounting import USAGE_PROFILE, usage_cost
from .common import digest, encoded, load, write_private
from .reuse import freeze as freeze_history, read_snapshot, safe_file, source_lock


PURPOSE = "fresh-source-judging-of-saved-collection-v1"


def planned_requests(package, captures):
    """Expand the retained collection with the same portable judging primitives."""
    from ._method.providers import project_response
    from .judging import METHOD, blind_packet, build_pro_request, compile_packet, packet_for
    blocked = {tuple(row[k] for k in ("role", "configuration_id", "question_id", "rank"))
               for row in _read(package["configuration"]["fresh_judging"])[0]["summary"]["blocked_occurrences"]}
    for search in package["search_requests"]:
        cid, qid = search["configuration_id"], search["question_id"]
        response = captures.get("search-" + digest(dict(configuration=cid, question=qid)))
        if response is None or response.get("dispatch_state") == "uncertain" or response.get("credential_echo_redacted"):
            continue
        provider = next(p for p in package["providers"] if p["configuration_id"] == cid)
        question = next(q for q in package["questions"] if q["question_id"] == qid)
        projected = project_response(provider, response.get("raw_text", "").encode(), response["captured_at"],
                                     http_status=response.get("http_status") or 0, complete=response.get("complete", False))
        for position in projected["positions"][:METHOD["pool_depth"]]:
            case = "occurrence-" + digest(dict(question=question, configuration=provider, position=position))
            compiled = compile_packet(packet_for(question, provider, position, case), response["captured_at"], config=provider)
            for role in ("pro", "codex", "claude"):
                if (role, cid, qid, position["rank"]) in blocked:
                    continue
                if role == "pro":
                    request = dict(kind="model", role=role, backend="gemini-api", wire=build_pro_request(compiled))
                else:
                    reviewer = package["configuration"]["reviewers"][role]
                    request = dict(kind="model", role=role, backend=reviewer["backend"],
                                   backend_config=reviewer, blind_packet=blind_packet(compiled))
                yield role + "-" + case, request


def _read(reference):
    if set(reference) != {"manifest_path", "sha256"}:
        raise ValueError("fresh collection requires an exact manifest reference")
    path = Path(reference["manifest_path"]).expanduser().absolute()
    path = safe_file(path.parent, path.name)
    raw = path.read_bytes()
    if digest(raw) != reference["sha256"]:
        raise ValueError("fresh collection manifest differs")
    value = json.loads(raw)
    if value.get("purpose") != PURPOSE:
        raise ValueError("unsupported fresh collection purpose")
    snapshot = read_snapshot(value["source_snapshot"])
    parent = Path(value["source_snapshot"]["manifest_path"]).parent
    source_file = next(f for f in snapshot["files"] if f["path"] == "source-package.json")
    source_raw = (parent / "source-package.json").read_bytes()
    if digest(source_raw) != source_file["sha256"]:
        raise ValueError("source package changed during read")
    source = json.loads(source_raw)
    if source["configuration"].get("fresh_judging"):
        raise ValueError("this epoch format requires an original collection ledger; chained fresh epochs need inherited-cost provenance")
    report_path = safe_file(path.parent, "source-report.json")
    report_raw = report_path.read_bytes()
    if digest(report_raw) != value["source_report_sha256"]:
        raise ValueError("archived source report differs")
    report = json.loads(report_raw)
    if report["package_sha256"] != snapshot["source_package_sha256"]:
        raise ValueError("source report belongs to another package")
    summary, captures = _summary(parent, source, snapshot, report)
    if value["summary"] != summary:
        raise ValueError("fresh source accounting/uncertainty summary differs")
    return value, source, captures


def _summary(parent, source, snapshot, report):
    if report["execution"].get("stopped_reason"):
        raise ValueError("an account epoch cannot clear a source global admission stop")
    files = {f["path"]: f for f in snapshot["files"]}
    def bound(name):
        raw = safe_file(parent, name).read_bytes()
        if digest(raw) != files[name]["sha256"] or len(raw) != files[name]["bytes"]:
            raise ValueError("source evidence changed during read")
        return raw
    events = [json.loads(line) for line in bound("events.jsonl").decode().splitlines() if line.strip()]
    completed = {e["task_id"]: e for e in events if e["event"] == "finished"}
    coordinates = {row["case_id"]: {k: row[k] for k in ("configuration_id", "question_id", "rank")}
                   for row in report["outcomes"]}
    captures, blocked, model_cost, search_cost = {}, [], Decimal(0), Decimal(0)
    for event in events:
        if event["event"] != "started":
            continue
        task = event["task_id"]
        request = json.loads(bound("attempts/" + event["attempt_id"] + "/request.json"))
        finish = completed.get(task)
        result = json.loads(bound(finish["result_path"])) if finish else None
        if request["kind"] == "search":
            search_cost += Decimal(request["reservation_usd"])
            # Latest complete source outcome is retained, never recollected.
            if finish and finish["attempt_id"] == event["attempt_id"]:
                captures[task] = dict(result, source_attempt_id=event["attempt_id"],
                                      source_result_sha256=finish["result_sha256"])
            continue
        maximum = Decimal(request["reservation_usd"])
        if not finish or finish["attempt_id"] != event["attempt_id"]:
            # Earlier completed retry attempts have their own retained results.
            own = next((e for e in events if e["event"] == "finished" and e["attempt_id"] == event["attempt_id"]), None)
            result = json.loads(bound(own["result_path"])) if own else None
        cost = usage_cost(request, result, source) if result else None
        model_cost += maximum if cost is None else cost
        if cost is not None and cost > maximum:
            raise ValueError("source usage exceeds reserved maximum; owner review required")
        if not result or result.get("dispatch_state") == "uncertain" or result.get("complete") is False:
            blocked.append(task)
    blocked_occurrences = []
    for task in sorted(set(blocked)):
        role, case = task.split("-", 1)
        if case not in coordinates:
            raise ValueError("uncertain source task lacks a bound occurrence; inspect source before a fresh pass")
        blocked_occurrences.append(dict(role=role, **coordinates[case]))
    return dict(source_package_sha256=snapshot["source_package_sha256"],
                source_statistics=snapshot["statistics"], carried_model_cost_usd=str(model_cost),
                carried_cost_usd=str(model_cost + search_cost),
                blocked_tasks=sorted(set(blocked)),
                blocked_occurrences=blocked_occurrences,
                archived_recovery=deepcopy(report["execution"].get("recovery")),
                new_health_epoch_accounts=sorted({source["account_bindings"][r] for r in ("pro", "codex", "claude")}),
                meaning="no source votes; explicit new judge/health epoch; uncertain source tasks remain no-send"), captures


def freeze(source_package_path, output):
    """Freeze retained evidence for review; performs no call or cost mutation."""
    source_path = Path(source_package_path).expanduser().absolute()
    source = load(source_path)
    output = Path(output).expanduser().absolute()
    if output.exists():
        raise ValueError("choose a new immutable fresh collection directory")
    root = Path(source["configuration"]["run_directory"]).expanduser().resolve()
    with source_lock(root):
        source_report = (root / "report.json").read_bytes()
        snapshot = freeze_history(source_path, output / "source-history")
        parent = Path(snapshot["manifest_path"]).parent
        saved = read_snapshot(snapshot)
        report = load(root / "report.json")
        if (root / "report.json").read_bytes() != source_report:
            raise ValueError("source report changed during freeze")
        summary, _ = _summary(parent, source, saved, report)
        write_private(output / "source-report.json", source_report)
        value = dict(purpose=PURPOSE, source_snapshot=snapshot,
                     source_report_sha256=digest(source_report), summary=summary)
        write_private(output / "manifest.json", value)
    reference = dict(manifest_path=str(output / "manifest.json"),
                     sha256=digest(encoded(value)))
    _read(reference)
    return reference


def validate(reference, target):
    value, source, captures = _read(reference)
    for key in ("mode", "question_bindings", "providers", "primary"):
        if source[key] != target[key]:
            raise ValueError("fresh collection changed captured scope or backend: " + key)
    for key in ("search_cells", "result_slots", "primary_tasks", "conditional_third_tasks"):
        if source["scope"][key] != target["scope"][key]:
            raise ValueError("fresh collection changed planned occurrence scope")
    if source["configuration"]["reviewers"] != target["configuration"]["reviewers"]:
        raise ValueError("fresh epoch changed reviewer model/settings")
    if target["configuration"]["execution_policy"].get("budget_accounting") != USAGE_PROFILE:
        raise ValueError("fresh epoch requires explicit guarded usage accounting")
    destination = Path(target["configuration"]["run_directory"]).expanduser().resolve()
    snapshot_root = Path(reference["manifest_path"]).parent.resolve()
    old_root = Path(source["configuration"]["run_directory"]).expanduser().resolve()
    if destination == old_root or destination.is_relative_to(old_root) or destination.is_relative_to(snapshot_root):
        raise ValueError("fresh epoch must preserve its source and snapshot")
    if Decimal(value["summary"]["carried_cost_usd"]) > Decimal(str(target["configuration"]["execution_policy"]["spending_ceiling_usd"])):
        raise ValueError("fresh epoch ceiling is below carried costs")
    return value, captures


def begin(package, ledger):
    reference = package["configuration"].get("fresh_judging")
    if reference is None:
        return None
    value, captures = validate(reference, package)
    marker = ledger.directory / "fresh-epoch.json"
    receipt = dict(purpose=PURPOSE, snapshot_sha256=reference["sha256"],
                   **value["summary"], new_package_sha256=digest(package))
    if marker.exists():
        if load(marker) != receipt:
            raise ValueError("fresh epoch binding changed")
    else:
        if ledger.events():
            raise ValueError("unbound fresh epoch already contains attempts")
        write_private(marker, receipt)
    return dict(receipt=receipt, captures=captures,
                blocked_tasks=set(value["summary"]["blocked_tasks"]),
                blocked_occurrences={tuple(row[k] for k in ("role", "configuration_id", "question_id", "rank"))
                                     for row in value["summary"]["blocked_occurrences"]},
                initial_committed_usd=Decimal(value["summary"]["carried_cost_usd"]))
