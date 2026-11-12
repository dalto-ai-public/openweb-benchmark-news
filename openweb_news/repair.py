"""One offline decoder/scorer repair branch over immutable saved model responses."""
from pathlib import Path

from . import judging
from .common import asset_bindings, code_bindings, digest, load, now, private_directory, records, write_private
from .reporting import write_reports
from .review import needs_third, settle
from ._method import scoring


def redecode(package, directory):
    """No transport, credentials, reviewer process, changed packet or new policy."""
    branch = package["configuration"]["execution_policy"]["repair_branch"]
    if branch.get("max_repairs") != 1 or branch.get("mode") != "saved_response_redecode" or branch.get("additional_live_tasks") != 0:
        raise ValueError("package does not contain this exact offline repair branch")
    if package["assets"] != asset_bindings():
        raise ValueError("repair cannot change frozen method assets")
    source = Path(package["configuration"]["run_directory"])
    package_hash = digest(package)
    if load(source / "binding.json") != dict(package_sha256=package_hash, mode=package["mode"]):
        raise ValueError("saved run belongs to a different package")
    output = private_directory(directory)
    if output == source or output.is_relative_to(source):
        raise ValueError("repair output must be separate from the original run")
    claim = dict(package_sha256=package_hash, code=code_bindings(), output=str(output))
    marker = source / "offline-repair-claim.json"
    if marker.exists():
        if load(marker) != claim:
            raise ValueError("single repair branch was already claimed by different code/output")
    else:
        write_private(marker, claim)
    events = list(records(source / "attempts.jsonl"))
    finished = {row["task_id"]: row for row in events if row["event"] == "finished"}

    def saved(task_id):
        event = finished.get(task_id)
        if not event:
            return None, None
        path = source / event["result_path"]
        if digest(path.read_bytes()) != event["result_sha256"]:
            raise ValueError("saved repair response hash differs")
        request = load(path.parent / "request.json")
        if digest(request) != event["request_sha256"]:
            raise ValueError("saved repair request hash differs")
        return load(path), request

    original = load(source / "report.json")
    outcomes, labels, reasons, findings = [], {}, {}, {}
    for old in original["outcomes"]:
        case_id = old["case_id"]
        compiled = load(source / "compiled" / (case_id + ".json"))
        judging.verify_compiled(compiled)
        if old["packet_sha256"] != compiled["packet_sha256"]:
            raise ValueError("repair packet changed")
        positions = {}
        for role in ("pro", "codex", "claude"):
            raw, request = saved(role + "-" + case_id)
            if raw is None:
                continue
            if role == "pro":
                body = request["wire"]["body"]
                import json
                if body["systemInstruction"]["parts"][0]["text"] != compiled["prompt"] or json.loads(body["contents"][0]["parts"][0]["text"]) != compiled["input"]:
                    raise ValueError("saved primary request differs from its packet")
                value = judging.response_result(raw.get("raw_text", "").encode(), compiled,
                    http_status=raw.get("http_status") or 0, complete=raw.get("complete", False))
            else:
                if request["blind_packet"] != judging.blind_packet(compiled):
                    raise ValueError("saved reviewer request differs from its packet")
                value = judging.decode_position(raw.get("verdict"), compiled) if raw.get("transport_success") else dict(state="unjudged", grade=None)
            positions[role] = dict(value, independent=True, prior_verdict_exposure=False,
                                   backend_observation=old["positions"][role].get("backend_observation"))
        first = positions.get("pro", dict(state="unjudged", grade=None, independent=True, prior_verdict_exposure=False))
        second = positions.get("codex", dict(state="unjudged", grade=None, independent=True, prior_verdict_exposure=False))
        third = positions.get("claude") if needs_third(first, second) else None
        new = dict(case_id=case_id, question_id=old["question_id"], configuration_id=old["configuration_id"],
                   rank=old["rank"], packet_sha256=old["packet_sha256"], original_grade=old["grade"],
                   saved_positions=positions, **settle(first, second, third))
        outcomes.append(new)
        labels[case_id], reasons[case_id] = new["grade"], new["reason"]
        selected = new["positions"].get(new["selected_roles"][0]) if new["selected_roles"] else None
        judgment = ((selected or {}).get("conformance_judgment") or (selected or {}).get("judgment") or {})
        findings[case_id] = [dict(report_id=None, event_id=None, facet_ids=c["supported_facet_ids"])
                             for c in judgment.get("candidates", []) if c["answer_supported"]] if new["grade"] == 2 else []
    cells = {}
    for collection in original["collection"]:
        key = collection["configuration_id"], collection["question_id"]
        projection = load(source / "projections" / (collection["task_id"] + ".json"))
        ranked = []
        for outcome in sorted((r for r in outcomes if (r["configuration_id"], r["question_id"]) == key), key=lambda r: r["rank"]):
            native = projection["positions"][outcome["rank"] - 1].get("url", {})
            ranked.append(dict(rank=outcome["rank"], evidence_id=outcome["case_id"], url=native.get("value") if native.get("state") == "present" else None))
        cells[key] = dict(status=collection["status"], ranked=ranked)
    cfg = package["configuration"]
    scores = scoring.score(package["questions"], [p["configuration_id"] for p in package["providers"]], cells,
                           labels, reasons, cfg["coverage_target"], cfg["tie_order"], findings=findings)
    report = dict(original, generated_at=now(), mode="saved_response_redecode", scores=scores, outcomes=outcomes,
                  repair=dict(parent_package_sha256=package_hash, original_code=package["code"], current_code=code_bindings(),
                              new_live_calls=0, scope="same saved inputs and responses; decoder/scorer repair only"))
    from collections import Counter
    report["outcome_counts"] = dict(Counter("unknown" if v is None else str(v) for v in labels.values()))
    write_reports(output, report)
    return report
