"""Optional saved-label score replay through explicit bound external inputs."""
from collections import Counter, defaultdict
from pathlib import Path

from .common import digest, load, records
from ._method.scoring import score


def replay(spec):
    paths = {}
    for name, binding in spec["inputs"].items():
        path = Path(binding["path"])
        if digest(path.read_bytes()) != binding["sha256"]:
            raise ValueError(f"replay {name} input hash differs")
        paths[name] = path
    questions = list(records(paths["questions"]))
    bank = {q["question_id"]: q for q in questions}
    if len(bank) != len(questions):
        raise ValueError("duplicate replay question")
    occurrences = {row["evidence_id"]: row for row in records(paths["occurrences"])}
    cells, owners = {}, {}
    for row in records(paths["cells"]):
        key = row["configuration_id"], row["question_id"]
        if key in cells:
            raise ValueError("duplicate replay cell")
        ranked = []
        for rank, eid in enumerate(row["evidence_ids"], 1):
            occurrence = occurrences[eid]
            native = occurrence["native_position"]
            if (occurrence["configuration_id"], occurrence["question_id"]) != key or native["rank"] != rank or occurrence["question_revision"] != bank[key[1]]["revision"]:
                raise ValueError("saved occurrence/cell/question join differs")
            ranked.append(dict(evidence_id=eid, rank=rank,
                               url=native["url"].get("value") if native["url"]["state"] == "present" else None))
            if eid in owners:
                raise ValueError("repeated saved occurrence ID")
            owners[eid] = row["question_id"]
        if ranked and row["status"] not in {"success", "success_empty"}:
            raise ValueError("unanswered replay cell has results")
        cells[key] = dict(status=row["status"], ranked=ranked)
    if set(owners) != set(occurrences):
        raise ValueError("replay occurrence outside cells")
    labels, reasons = {eid: None for eid in owners}, {}
    for line in paths["qrels"].read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        qid, _, eid, grade = line.split()
        if owners.get(eid) != qid or grade not in {"0", "1", "2"} or labels[eid] is not None:
            raise ValueError("qrel join/grade differs")
        labels[eid] = int(grade)
    for row in records(paths["pending"]):
        eid = row.get("evidence_id") or row["case_id"]
        if owners.get(eid) != row["question_id"] or labels[eid] is not None or eid in reasons:
            raise ValueError("pending join differs")
        reasons[eid] = row.get("reason") or "unresolved"
    for eid, grade in labels.items():
        if grade is None:
            reasons.setdefault(eid, "not_in_label_set")
    findings = defaultdict(list)
    for row in records(paths["findings"]):
        eid = row.get("evidence_id") or row["case_id"]
        if owners.get(eid) != row["question_id"]:
            raise ValueError("finding join differs")
        findings[eid].append({key: row.get(key) for key in ("report_id", "event_id", "facet_ids")})
    params = spec["parameters"]
    scope = set(params.get("question_ids") or bank)
    if not scope <= set(bank):
        raise ValueError("replay question outside bank")
    for qid in scope:
        if any(q["question_id"] not in scope for q in questions if q["family_id"] == bank[qid]["family_id"]):
            raise ValueError("replay scope splits a declared family")
    dates = [r for r in records(paths["native_dates"]) if r["question_id"] in scope] if "native_dates" in paths else None
    aliases = load(paths["aliases"]) if "aliases" in paths else {}
    result = score([q for q in questions if q["question_id"] in scope], spec["configuration_ids"], cells,
                   labels, reasons, params["coverage_target"], params["tie_order"], findings=findings,
                   aliases=aliases, pairs=params.get("pairs", []), native_dates=dates)
    expected = load(paths["scores"])
    differences = []
    # JSON normalization compares integer cutoff keys to the saved JSON strings.
    normalized = __import__("json").loads(__import__("json").dumps(result))
    for key in result:
        if normalized[key] != expected.get(key):
            differences.append(key)
    return dict(exact_match=not differences, compared_sections=list(result), differing_sections=differences,
                questions=len(scope), families=len({bank[q]["family_id"] for q in scope}),
                configurations=len(spec["configuration_ids"]),
                numeric_grades=sum(grade is not None for eid, grade in labels.items() if owners[eid] in scope),
                unknown_grades=sum(grade is None for eid, grade in labels.items() if owners[eid] in scope),
                input_hashes={name: binding["sha256"] for name, binding in spec["inputs"].items()},
                limits="Saved-label arithmetic replay only; no new judgment reproduction, live access, human accuracy or publication proof.")
