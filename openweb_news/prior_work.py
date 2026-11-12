"""Self-contained cost evidence for disjoint new work after a fresh judge epoch.

This is not a verdict migration. Overlapping source questions require exact-task
reuse, so a cost-only archive cannot accidentally authorize paying for them again.
"""
from pathlib import Path
import json

from .common import digest, encoded, load, private_directory, records, write_private
from .reuse import committed, freeze as freeze_history, read_snapshot, safe_file


PURPOSE = "disjoint-prior-work-obligations-v1"


def read(reference, target=None):
    path = Path(reference["manifest_path"]).expanduser().absolute()
    path = safe_file(path.parent, path.name)
    raw = path.read_bytes()
    if digest(raw) != reference["sha256"]:
        raise ValueError("prior-work manifest differs")
    value = json.loads(raw)
    if value["purpose"] != PURPOSE:
        raise ValueError("unknown prior-work purpose")
    from decimal import Decimal
    total = Decimal(0)
    sources, seen = [], set()
    for index, item in enumerate(value["histories"]):
        snapshot_path = safe_file(path.parent, item["path"])
        ref = dict(manifest_path=str(snapshot_path), sha256=item["sha256"])
        snapshot = read_snapshot(ref)
        source = load(snapshot_path.parent / "source-package.json")
        identity = snapshot["source_package_sha256"]
        if identity in seen:
            raise ValueError("prior history counted twice")
        seen.add(identity)
        sources.append(source)
        native_usage = source["configuration"]["execution_policy"].get("budget_accounting") in ("source-native-usage-v1", "source-native-cache-usage-v1")
        if index:
            parent = sources[index - 1]["configuration"].get("fresh_judging")
            if not parent:
                raise ValueError("unlinked prior history")
            fresh_raw = safe_file(path.parent, item["fresh_manifest_path"]).read_bytes()
            if digest(fresh_raw) != parent["sha256"]:
                raise ValueError("inherited fresh cost contract differs")
            fresh = json.loads(fresh_raw)
            if fresh["purpose"] != "fresh-source-judging-of-saved-collection-v1" or fresh["source_snapshot"]["sha256"] != item["sha256"]:
                raise ValueError("fresh epoch does not bind this prior history")
            # The approved fresh epoch explicitly adopted known source native
            # usage, including source attempts made under maximum reservations.
            native_usage = True
        total += committed(snapshot_path.parent, source, list(records(snapshot_path.parent / "events.jsonl")), native_usage=native_usage)
    if not sources or sources[-1]["configuration"].get("fresh_judging") or any(s["configuration"].get("prior_work") for s in sources):
        raise ValueError("incomplete or unsupported prior lineage")
    if value["committed_and_held_usd"] != str(total):
        raise ValueError("prior-work cost summary differs from immutable attempts")
    if target is not None:
        question_ids = {q["question_id"] for s in sources for q in s["question_bindings"]}
        if question_ids & {q["question_id"] for q in target["question_bindings"]}:
            raise ValueError("overlapping prior questions require exact-task reuse, not a cost-only archive")
        destination = Path(target["configuration"]["run_directory"]).expanduser().resolve()
        if destination.is_relative_to(path.parent.resolve()) or any(destination == Path(s["configuration"]["run_directory"]).expanduser().resolve() for s in sources):
            raise ValueError("new work must preserve its source histories")
        if total > Decimal(str(target["configuration"]["execution_policy"]["spending_ceiling_usd"])):
            raise ValueError("ceiling below prior obligations")
    return value


def freeze(source_package_path, output):
    output = Path(output).expanduser().absolute()
    if output.exists():
        raise ValueError("choose a new immutable prior-work archive")
    private_directory(output)
    source = load(source_package_path)
    first = freeze_history(source_package_path, output / "history-0")
    histories = [dict(path="history-0/manifest.json", sha256=first["sha256"])]
    from decimal import Decimal
    total = committed(output / "history-0", source, list(records(output / "history-0/events.jsonl")))
    fresh_ref = source["configuration"].get("fresh_judging")
    if fresh_ref:
        # Existing fresh validation verifies the original report, uncertainty
        # mapping and ancestor package before copying immutable evidence.
        from .fresh import _read
        fresh, parent, _ = _read(fresh_ref)
        parent_ref = fresh["source_snapshot"]
        snapshot = read_snapshot(parent_ref)
        source_root = Path(parent_ref["manifest_path"]).parent
        target_root = output / "history-1"
        for item in snapshot["files"]:
            write_private(safe_file(target_root, item["path"]), safe_file(source_root, item["path"]).read_bytes())
        manifest_raw = Path(parent_ref["manifest_path"]).read_bytes()
        write_private(target_root / "manifest.json", manifest_raw)
        fresh_raw = Path(fresh_ref["manifest_path"]).read_bytes()
        write_private(output / "fresh-contract.json", fresh_raw)
        histories.append(dict(path="history-1/manifest.json", sha256=parent_ref["sha256"], fresh_manifest_path="fresh-contract.json"))
        total += committed(target_root, parent, list(records(target_root / "events.jsonl")), native_usage=True)
    value = dict(purpose=PURPOSE, histories=histories, committed_and_held_usd=str(total),
                 meaning="source attempts retained once; native completed Pro usage at frozen rates, all missing/uncertain usage at full holds; no source votes or new authorization")
    write_private(output / "manifest.json", value)
    reference = dict(manifest_path=str(output / "manifest.json"), sha256=digest(encoded(value)))
    read(reference)
    return reference
