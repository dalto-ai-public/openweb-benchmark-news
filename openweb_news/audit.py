"""Reproducible operational sample preparation; no new grading or model calls."""
from collections import defaultdict
from pathlib import Path

from .common import digest, load, private_directory, write_private


def sample(run_directory, output):
    source = Path(run_directory)
    report = load(source / "report.json")
    groups = defaultdict(list)
    failures = []
    for row in report["collection"]:
        if row["status"] not in {"success", "success_empty"}:
            failures.append(dict(kind="collection", record=row))
    for row in report["outcomes"]:
        key = row["configuration_id"], str(row["grade"]), row["states"]["pro"]
        groups[key].append(row)
        if any(state in {"invalid", "unjudged", "nonconforming_numeric"} for state in row["states"].values()):
            failures.append(dict(kind="judgment", record=row))
    for rows in groups.values():
        rows.sort(key=lambda row: digest(row["case_id"].encode()))
    selected = []
    while len(selected) < 20 and any(groups.values()):
        for key in sorted(groups):
            if groups[key] and len(selected) < 20:
                row = groups[key].pop(0)
                selected.append(dict(outcome=row, compiled=load(source / "compiled" / (row["case_id"] + ".json"))))
    destination = private_directory(output)
    write_private(destination / "all-failures.json", failures)
    write_private(destination / "judgment-sample.json", selected)
    receipt = dict(selection="round-robin sorted configuration/final-grade/primary-state strata; SHA256(case_id) order within stratum",
                   selected_count=len(selected), requested_count=20, failure_records=len(failures),
                   package_sha256=report["package_sha256"],
                   meaning="operational inspection sample; not blind human calibration, accuracy estimate or an additional model round")
    write_private(destination / "selection.json", receipt)
    return receipt
