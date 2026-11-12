import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from openweb_news import batch, question_bank
from openweb_news.common import digest, load, records
from .helpers import question


class QuestionBankTests(unittest.TestCase):
    def source(self, root, questions=None):
        rows = questions if questions is not None else [question("synthetic-3"), question("synthetic-1"), question("synthetic-2")]
        source = root / "input.jsonl"
        # Keep intentional blank lines, noncanonical formatting, Unicode and no final newline.
        source.write_bytes(("\n\r\n" + "\n\n".join(json.dumps(row, ensure_ascii=False, separators=(",", ": ")) for row in rows)).encode())
        return source

    def test_exact_source_unknown_fields_full_coverage_and_determinism(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            rows = [question("synthetic-3"), question("synthetic-1"), question("synthetic-2")]
            rows[0]["future_field"] = dict(text="בדיקה", nested=[None, 7, dict(unrecognized=True)])
            rows[1]["contract"]["future_contract_field"] = ["retain", "exactly"]
            source = self.source(root, rows)
            before = source.read_bytes()
            report = question_bank.validate(source)
            self.assertEqual(report["source_sha256"], digest(before))
            self.assertEqual(report["question_count"], 3)
            self.assertEqual(report["structural_validation"], "valid")
            self.assertEqual(report["semantic_validation"], "not_verified")
            self.assertEqual(set(root.iterdir()), {source})
            frozen = question_bank.freeze(source, root / "frozen", phase_counts=[1, 2])
            self.assertEqual(source.read_bytes(), before)
            self.assertEqual((root / "frozen/bank.jsonl").read_bytes(), before)
            ordered, refs = batch.frozen_order(frozen["selection_path"])
            self.assertEqual(ordered, rows)
            self.assertEqual(refs["selection_sha256"], frozen["selection_sha256"])
            self.assertEqual(refs["admission_manifest_sha256"], frozen["manifest_sha256"])
            self.assertEqual(list(records(root / "frozen/phase-2-questions.jsonl")), rows[1:])
            selection = load(frozen["selection_path"])
            self.assertEqual(selection["question_order"], [q["question_id"] for q in rows])
            manifest = load(frozen["manifest_path"])
            self.assertEqual({row["path"] for row in manifest["files"]},
                             {"source.json", "bank.jsonl", "selection.json", "phase-1-questions.jsonl", "phase-2-questions.jsonl"})
            for item in manifest["files"]:
                self.assertEqual(digest((root / "frozen" / item["path"]).read_bytes()), item["sha256"])
            question_bank.freeze(source, root / "again", phase_counts=[1, 2])
            self.assertEqual({p.name: p.read_bytes() for p in (root / "frozen").iterdir()},
                             {p.name: p.read_bytes() for p in (root / "again").iterdir()})
            self.assertEqual((root / "frozen").stat().st_mode & 0o777, 0o700)
            self.assertTrue(all(p.stat().st_mode & 0o777 == 0o600 for p in (root / "frozen").iterdir()))

    def test_default_phase_covers_whole_bank_and_existing_output_is_preserved(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            source = self.source(root)
            result = question_bank.freeze(source, root / "frozen")
            selection = load(result["selection_path"])
            self.assertEqual(len(selection["phases"]), 1)
            self.assertEqual(selection["phases"][0]["question_ids"], selection["question_order"])
            before = {p.name: p.read_bytes() for p in (root / "frozen").iterdir()}
            with self.assertRaises(ValueError):
                question_bank.freeze(source, root / "frozen")
            self.assertEqual(before, {p.name: p.read_bytes() for p in (root / "frozen").iterdir()})
            (root / "existing").mkdir()
            with self.assertRaises(ValueError):
                question_bank.freeze(source, root / "existing")
            self.assertEqual(list((root / "existing").iterdir()), [])

    def test_invalid_counts_leave_no_output(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            source = self.source(root)
            for counts in ([], [2], [4], [1, 1], [0, 3], [-1, 4], [True, 2], [1.0, 2], "3"):
                with self.subTest(counts=counts), self.assertRaises(ValueError):
                    question_bank.freeze(source, root / "rejected", phase_counts=counts)
                self.assertFalse((root / "rejected").exists())

    def test_invalid_ids_draft_geography_and_window_markers_leave_no_output(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            invalid = []
            for ident in (None, "", "  ", 1, [], {}):
                invalid.append([dict(question(), question_id=ident)])
            missing = question(); missing.pop("question_id")
            invalid.extend(([missing], [question(), question()], []))
            draft = question(); draft["geography"]["regions"] = ["unspecified"]
            invalid.append([draft])
            for key in ("provider_query", "contract", "geography"):
                draft = question()
                if key == "provider_query":
                    draft[key] += " {window}"
                else:
                    draft[key]["draft_field"] = dict(nested=["{window}"])
                invalid.append([draft])
            missing = question(); missing["contract"].pop("source_rule")
            invalid.extend(([missing], [None], [[]], [dict(question(), provider_query=" ")]))
            for rows in invalid:
                with self.subTest(rows=rows):
                    source = self.source(root, rows)
                    with self.assertRaises(ValueError):
                        question_bank.freeze(source, root / "rejected")
                    self.assertFalse((root / "rejected").exists())

    def test_invalid_temporal_bounds_and_unresolved_as_of_fail_before_writing(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            for basis in ("publication", "event"):
                for start, end in (("not-a-date", "2026-11-01T00:00:00Z"),
                                   ("2026-10-01T00:00:00", "2026-11-01T00:00:00Z"),
                                   ("2026-11-01T00:00:00Z", "2026-11-01T00:00:00Z"),
                                   ("2026-11-02T00:00:00Z", "2026-11-01T00:00:00Z")):
                    row = question()
                    row["contract"]["temporal_requirements"] = [dict(id="date", basis=basis, kind="opening", stage="opened",
                        start_inclusive=start, end_exclusive=end)]
                    source = self.source(root, [row])
                    with self.subTest(basis=basis, start=start), self.assertRaises(ValueError):
                        question_bank.freeze(source, root / "rejected")
                    self.assertFalse((root / "rejected").exists())
            for requirement in (dict(cutoff="2026-10-01"),
                                dict(meaning="historical_knowledge", cutoff="nonsense", boundary="inclusive", timezone="UTC", question_phrase="as of")):
                row = question(); row["contract"]["as_of_requirement"] = requirement
                with self.assertRaises(ValueError):
                    question_bank.freeze(self.source(root, [row]), root / "rejected")
                self.assertFalse((root / "rejected").exists())
            row["contract"]["as_of_requirement"]["cutoff"] = "2026-10-01"
            self.assertEqual(question_bank.validate(self.source(root, [row]))["question_count"], 1)

    def test_malformed_json_and_nonfinite_unknown_values_are_rejected(self):
        with TemporaryDirectory() as temp:
            root = Path(temp); source = root / "input.jsonl"
            nonfinite = json.dumps(dict(question(), unknown=float("inf"))).encode()
            for raw in (b'{"question_id":', nonfinite, b'\xff'):
                source.write_bytes(raw)
                with self.assertRaises(ValueError):
                    question_bank.freeze(source, root / "rejected")
                self.assertFalse((root / "rejected").exists())

    def test_frozen_order_detects_bank_selection_and_phase_tampering(self):
        with TemporaryDirectory() as temp:
            root = Path(temp); source = self.source(root)
            for name in ("bank.jsonl", "selection.json", "phase-1-questions.jsonl"):
                folder = root / name.replace(".", "-")
                result = question_bank.freeze(source, folder)
                target = folder / name
                target.write_bytes(target.read_bytes() + b"\n")
                with self.subTest(name=name), self.assertRaises(ValueError):
                    batch.frozen_order(result["selection_path"])


if __name__ == "__main__":
    unittest.main()
