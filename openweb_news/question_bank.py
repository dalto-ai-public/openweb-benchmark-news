"""Validate and freeze private source questions without selecting or rewriting them."""
import json
from pathlib import Path

from .common import digest, encoded, write_private
from .preparation import validate_questions
from ._method import evidence
from ._method.providers import _instant
from .judging import compile_packet


def _read(source_path):
    raw = Path(source_path).read_bytes()
    questions = []
    for number, line in enumerate(raw.decode("utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
            # Refuse non-finite JSON numbers, including in retained unknown fields.
            encoded(value)
        except (ValueError, TypeError) as error:
            raise ValueError(f"bank line {number}: invalid JSON object") from error
        if not isinstance(value, dict):
            raise ValueError(f"bank line {number}: question must be a JSON object")
        ident = value.get("question_id")
        if not isinstance(ident, str) or not ident.strip():
            raise ValueError(f"bank line {number}: question_id must be nonempty text")
        questions.append(value)
    try:
        validate_questions(questions)
        for question in questions:
            _validate_contract(question)
    except (KeyError, TypeError, AttributeError, ValueError) as error:
        raise ValueError(f"invalid question bank: {error}") from error
    report = dict(source_sha256=digest(raw), question_count=len(questions),
                  question_ids=[q["question_id"] for q in questions],
                  structural_validation="valid", semantic_validation="not_verified",
                  limits="Structural validation does not establish source rights, semantic admission, or execution approval. These remain explicit in the execution package.")
    return raw, questions, report


def _validate_contract(question):
    """Exercise the existing method contract and check its explicit date bounds."""
    if not isinstance(question["provider_query"], str) or not question["provider_query"].strip():
        raise ValueError("provider_query must be nonempty text")
    requirements = question["contract"]["temporal_requirements"]
    if not isinstance(requirements, list):
        raise ValueError("temporal_requirements must be a list")
    for requirement in requirements:
        # The same timestamp parser owns native publication request bounds.
        start, end = (_instant(requirement[key]) for key in ("start_inclusive", "end_exclusive"))
        if start >= end:
            raise ValueError("temporal window must have positive duration")
    as_of = evidence.as_of_meaning(question)
    if as_of:
        for key in ("target_time", "cutoff"):
            if key in as_of and evidence.date_interval(as_of[key])["interval"] is None:
                raise ValueError(f"invalid as-of {key}")
    packet = evidence.build_input(question,
        dict(question_id=question["question_id"], question_revision=question["revision"],
             evidence_id="bank-structural-validation", allowed_text=[], native_field_states={},
             native_record_state="native_result"),
        dict(evidence_id="bank-structural-validation", observed_url=dict(state="missing"), observed_metadata={}), [])
    compile_packet(packet, "2000-01-01T00:00:00Z")


def validate(source_path):
    """Return a read-only structural report; make no semantic admission claim."""
    return _read(source_path)[2]


def freeze(source_path, directory, phase_counts=None, *, resume=False):
    """Preserve exact source bytes and freeze every question in source order."""
    raw, questions, report = _read(source_path)
    counts = [len(questions)] if phase_counts is None else phase_counts
    if (not isinstance(counts, (list, tuple)) or not counts
            or any(type(count) is not int or count <= 0 for count in counts)
            or sum(counts) != len(questions)):
        raise ValueError("phase counts must be positive integers covering the complete bank")
    root = Path(directory).expanduser().absolute()
    if root.is_symlink() or root.exists() and not resume:
        raise ValueError("bank output directory must be new; existing files are preserved")
    files = {"bank.jsonl": raw, "source.json": encoded(report)}
    phases, offset = [], 0
    for number, count in enumerate(counts, 1):
        rows = questions[offset:offset + count]
        phases.append(dict(phase=number, question_ids=[q["question_id"] for q in rows],
                           questions_sha256=digest(rows)))
        files[f"phase-{number}-questions.jsonl"] = b"".join(encoded(q) for q in rows)
        offset += count
    selection = dict(bank_sha256=digest(raw), question_order=report["question_ids"], phases=phases)
    files["selection.json"] = encoded(selection)
    manifest = dict(files=[dict(path=name, sha256=digest(value)) for name, value in sorted(files.items())])
    files["VERIFICATION-MANIFEST.json"] = encoded(manifest)
    # Resume may finish an interrupted freeze, but never bless changed bytes.
    for name, value in files.items():
        path = root / name
        if path.is_symlink() or path.exists() and path.read_bytes() != value:
            raise ValueError("frozen bank bytes changed")
    for name, value in files.items():
        if not (root / name).exists():
            write_private(root / name, value)
    return dict(**report, selection_path=str(root / "selection.json"),
                selection_sha256=digest(files["selection.json"]),
                manifest_path=str(root / "VERIFICATION-MANIFEST.json"), manifest_sha256=digest(manifest))
