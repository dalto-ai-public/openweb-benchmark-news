from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from openweb_news import date_diagnostics, judging
from openweb_news._method.providers import configuration_catalog, project_response
from openweb_news.common import digest, load, write_private
from .helpers import question


class DateDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.compiled = self.root / "compiled"
        self.compiled.mkdir()
        self.config = next(c for c in configuration_catalog(include_optional=True) if c["configuration_id"] == "keenable-pro")
        self.mapping, self.outcomes = {}, []
        guard = patch("socket.socket", side_effect=AssertionError("diagnostics must not use network"))
        guard.start()
        self.addCleanup(guard.stop)

    def save(self, case_id, native="2026-01-01T09:00:00Z", *, url="https://synthetic.example/2026/01/01/story", text="Published: January 1, 2026", extra=None):
        q = question("question-" + case_id)
        row = dict(url=url, title="Aster editorial", snippet=text, published_at=native, **(extra or {}))
        position = project_response(self.config, dict(results=[row]), "2026-11-05T00:00:00Z")["positions"][0]
        compiled = judging.compile_packet(judging.packet_for(q, self.config, position, case_id), "2026-11-05T00:00:00Z", config=self.config)
        write_private(self.compiled / (case_id + ".json"), compiled)
        self.mapping[case_id] = self.config["configuration_id"]
        self.outcomes.append(dict(case_id=case_id, configuration_id=self.config["configuration_id"], question_id=q["question_id"], packet_sha256=compiled["packet_sha256"]))
        return compiled

    def inspect(self, **kwargs):
        return date_diagnostics.inspect(self.compiled, case_configurations=self.mapping,
            configurations={self.config["configuration_id"]: self.config}, **kwargs)

    def source_pair(self):
        package = dict(providers=[self.config])
        write_private(self.root / "package.json", package)
        write_private(self.root / "report.json", dict(package_sha256=digest(package), outcomes=self.outcomes))
        return dict(source_package_path=self.root / "package.json", source_report_path=self.root / "report.json")

    def group(self, report, field="published_at"):
        return next(g for g in report["groups"] if g["relative_path"] == field)

    def snapshot(self):
        return {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}

    def test_paired_sources_exact_and_historical_counts_are_read_only(self):
        self.save("one")
        self.save("two", "2026-01-02T09:00:00Z", url="https://synthetic.example/2026/01/01/other")
        pair = self.source_pair()
        before = self.snapshot()
        result = date_diagnostics.inspect(self.compiled, **pair)
        self.assertTrue(result["diagnostic_complete"])
        self.assertTrue(result["source_artifacts_unchanged"])
        metrics = self.group(result)["metrics"]
        self.assertEqual(metrics["url_comparable_unique_urls"], 2)
        self.assertEqual(metrics["url_exact_day_agreement_unique_urls"], 1)
        self.assertEqual(metrics["url_within_one_day_unique_urls"], 2)
        self.assertEqual(metrics["url_later_more_than_one_day_unique_urls"], 0)
        self.assertEqual(metrics["native_later_rate"], 0.5)
        self.assertEqual(self.group(result)["suggestion"]["state"], "not_requested")
        self.assertFalse(result["trust_activated"])
        self.assertEqual(self.snapshot(), before)

    def test_RFC_unknown_and_ambiguous_values_are_preserved(self):
        self.save("rfc", "Thu, 01 Jan 2026 10:00:00 GMT")
        self.save("bad", "not a native date", url="https://synthetic.example/no-date")
        self.save("ambiguous", "01/02/2026", url="https://synthetic.example/ambiguous")
        result = self.inspect()
        rows = {r["case_id"]: r for r in result["records"] if r.get("relative_path") == "published_at"}
        self.assertEqual(rows["rfc"]["native"]["parser"], "existing_RFC_date_support")
        self.assertEqual(rows["bad"]["native"]["state"], "unparseable")
        self.assertEqual(rows["ambiguous"]["native"]["state"], "ambiguous")
        self.assertEqual(self.group(result)["metrics"]["occurrences"], 3)

    def test_publication_and_update_same_line_keep_separate_roles(self):
        self.save("mixed", text="Published: January 1, 2026 Updated: January 2, 2026")
        row = next(r for r in self.inspect()["records"] if r.get("relative_path") == "published_at")
        self.assertEqual(row["dateline_witness"]["day"], "2026-01-01")
        self.assertEqual(row["dateline_witness"]["negative_control_datelines"][0]["day"], "2026-01-02")
        self.assertEqual(row["dateline_comparison"]["state"], "agree")

    def test_subject_event_multilingual_and_ambiguous_labels_are_not_publication_truth(self):
        texts = ("The study was published on January 1, 2026.",
                 "Published: January 1, 2026 describes another source's report",
                 "Event date: January 1, 2026", "Publié le 1 janvier 2026", "Published: 01/02/2026")
        for i, text in enumerate(texts):
            self.save("context-" + str(i), text=text, url="https://synthetic.example/context/" + str(i))
        result = self.inspect()
        rows = [r for r in result["records"] if r.get("relative_path") == "published_at"]
        self.assertTrue(all(r["dateline_witness"]["day"] is None for r in rows))
        self.assertEqual(self.group(result)["metrics"]["dateline_comparable_unique_urls"], 0)
        self.assertTrue(rows[0]["dateline_witness"]["rejected_publication_contexts"])
        self.assertTrue(rows[2]["dateline_witness"]["negative_control_datelines"])

    def test_duplicate_URL_conflict_is_not_an_inflated_independent_sample(self):
        self.save("a")
        self.save("b")
        self.save("c", "2026-01-03T09:00:00Z")
        metrics = self.group(self.inspect())["metrics"]
        self.assertEqual(metrics["occurrences"], 3)
        self.assertEqual(metrics["unique_urls"], 1)
        self.assertEqual(metrics["repeated_native_conflict_urls"], 1)
        self.assertEqual(metrics["url_comparable_unique_urls"], 0)
        self.assertEqual(metrics["url_comparable_occurrences"], 3)

    def test_negative_control_agreement_does_not_become_admission(self):
        self.save("negative", extra=dict(acquired_at="2026-01-01T09:00:00Z"))
        thresholds = dict(minimum_unique_urls=1, minimum_url_agreement=1.0,
            minimum_dateline_agreement=1.0, maximum_native_later_rate=0.0)
        group = self.group(self.inspect(thresholds=thresholds), "acquired_at")
        self.assertTrue(group["negative_control"])
        self.assertFalse(group["current_registry_admission_candidate"])
        self.assertEqual(group["suggestion"]["state"], "meets_requested_thresholds")
        self.assertEqual(group["suggestion"]["role_recommendation"], "not_a_reviewed_publication_admission_candidate")
        self.assertFalse(group["suggestion"]["trust_activated"])

    def test_thresholds_are_explicit_and_insufficient_denominators_visible(self):
        self.save("one")
        result = self.inspect(thresholds=dict(minimum_unique_urls=2, minimum_url_agreement=0.9))
        self.assertEqual(self.group(result)["suggestion"]["state"], "insufficient_evidence")
        for invalid in ({}, {"unknown_metric": 1}, {"minimum_url_agreement": 1.1},
                        {"minimum_unique_urls": True}, {"maximum_native_later_rate": float("nan")}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.inspect(thresholds=invalid)

    def test_calendar_screening_uses_one_day_tolerance_and_sparse_datelines_do_not_block(self):
        for index in range(30):
            self.save("calendar-" + str(index),
                native="2026-01-03T00:15:00+14:00" if index == 29 else "2026-01-02T00:15:00+14:00",
                url="https://synthetic.example/2026/01/01/story-" + str(index),
                text="Published: January 1, 2026" if index < 6 else "Aster event on January 1, 2026.")
        thresholds = dict(minimum_unique_urls=30, minimum_url_agreement=0.9,
            minimum_dateline_agreement=0.9, maximum_native_later_rate=0.05)
        before = self.snapshot()
        legacy = self.inspect(thresholds=thresholds)
        current = self.inspect(thresholds=thresholds, screening_profile=date_diagnostics.CALENDAR_SCREENING_PROFILE)
        old_group, group = self.group(legacy), self.group(current)
        self.assertEqual(old_group["metrics"], group["metrics"])
        self.assertEqual(old_group["suggestion"]["state"], "insufficient_evidence")
        self.assertEqual(group["suggestion"]["state"], "meets_requested_thresholds")
        metrics = group["metrics"]
        self.assertEqual(metrics["url_exact_day_agreement_unique_urls"], 0)
        self.assertEqual(metrics["url_within_one_day_unique_urls"], 29)
        self.assertEqual(metrics["witness_native_later_unique_urls"], 30)
        self.assertEqual(metrics["witness_later_more_than_one_day_unique_urls"], 1)
        checks = {check["metric"]: check for check in group["suggestion"]["checks"]}
        self.assertEqual(checks["minimum_url_agreement"]["metric_definition"], "url_within_one_day_rate")
        self.assertEqual(checks["maximum_native_later_rate"]["observed"], 1 / 30)
        supporting = checks["minimum_dateline_agreement"]
        self.assertEqual(supporting["denominator"], 6)
        self.assertEqual(supporting["minimum_unique_urls"], 30)
        self.assertEqual(supporting["state"], "insufficient_supporting_evidence")
        self.assertFalse(supporting["blocking"])
        for stricter in (dict(thresholds, maximum_native_later_rate=0.02),
                         dict(thresholds, minimum_url_agreement=0.99)):
            self.assertEqual(date_diagnostics._suggestion(metrics, stricter, True,
                screening_profile=date_diagnostics.CALENDAR_SCREENING_PROFILE)["state"], "does_not_meet_requested_thresholds")
        self.assertEqual(current["records"], legacy["records"])
        self.assertFalse(current["trust_activated"])
        self.assertFalse(current["grades_changed"])
        self.assertEqual(self.snapshot(), before)

    def test_adequate_dateline_support_uses_within_one_day_and_can_fail_its_threshold(self):
        for index in range(31):
            self.save("support-" + str(index), native="2026-01-02T00:15:00Z",
                url="https://synthetic.example/2026/01/01/story-" + str(index),
                text="Published: January 4, 2026" if index == 30 else "Published: January 1, 2026")
        thresholds = dict(minimum_unique_urls=30, minimum_url_agreement=1.0,
            minimum_dateline_agreement=1.0, maximum_native_later_rate=0.0)
        group = self.group(self.inspect(thresholds=thresholds,
            screening_profile=date_diagnostics.CALENDAR_SCREENING_PROFILE))
        checks = {check["metric"]: check for check in group["suggestion"]["checks"]}
        support = checks["minimum_dateline_agreement"]
        self.assertEqual(support["denominator"], 31)
        self.assertEqual(support["observed"], 30 / 31)
        self.assertEqual(support["metric_definition"], "dateline_within_one_day_rate")
        self.assertTrue(support["blocking"])
        self.assertEqual(support["state"], "unmet")
        self.assertEqual(group["suggestion"]["state"], "does_not_meet_requested_thresholds")
        thresholds["minimum_dateline_agreement"] = 0.9
        self.assertEqual(date_diagnostics._suggestion(group["metrics"], thresholds, True,
            screening_profile=date_diagnostics.CALENDAR_SCREENING_PROFILE)["state"], "meets_requested_thresholds")

    def test_screening_profile_is_explicit_legacy_replays_and_new_profile_has_no_hidden_thresholds(self):
        self.save("one")
        thresholds = dict(minimum_unique_urls=1, minimum_url_agreement=1.0,
            minimum_dateline_agreement=1.0, maximum_native_later_rate=0.0)
        default = self.inspect(thresholds=thresholds)
        self.assertEqual(default, self.inspect(thresholds=thresholds,
            screening_profile=date_diagnostics.LEGACY_SCREENING_PROFILE))
        self.assertEqual(default["screening_definitions"]["url_agreement_metric"], "url_exact_day_agreement_rate")
        current = self.inspect(screening_profile=date_diagnostics.CALENDAR_SCREENING_PROFILE)
        self.assertEqual(self.group(current)["suggestion"]["state"], "not_requested")
        self.assertIsNone(current["thresholds"])
        with self.assertRaisesRegex(ValueError, "unknown.*screening profile"):
            self.inspect(screening_profile="unversioned-meaning")
        for missing in ("minimum_unique_urls", "minimum_url_agreement", "maximum_native_later_rate"):
            partial = {key: value for key, value in thresholds.items() if key != missing}
            with self.subTest(missing=missing), self.assertRaisesRegex(ValueError, "requires explicit"):
                self.inspect(thresholds=partial, screening_profile=date_diagnostics.CALENDAR_SCREENING_PROFILE)

    def test_packet_input_and_report_packet_corruption_is_retained_as_failure(self):
        original = self.save("one")
        pair = self.source_pair()
        for part in ("packet", "input", "report"):
            changed = deepcopy(original)
            if part == "packet":
                changed["packet"]["date_observations"][0]["raw_value"] = "2025-01-01"
            elif part == "input":
                changed["input"]["captured_at"] = "2025-01-01T00:00:00Z"
            else:
                report = load(self.root / "report.json")
                report["outcomes"][0]["packet_sha256"] = "different"
                write_private(self.root / "report.json", report, replace=True)
            write_private(self.compiled / "one.json", changed, replace=True)
            result = date_diagnostics.inspect(self.compiled, **pair)
            self.assertFalse(result["diagnostic_complete"])
            self.assertTrue(result["failures"])
            self.assertEqual(result["groups"], [])

    def test_wrong_source_report_package_rejects_before_any_output(self):
        self.save("one")
        pair = self.source_pair()
        report = load(self.root / "report.json")
        report["package_sha256"] = "wrong"
        write_private(self.root / "report.json", report, replace=True)
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "source report/package binding differs"):
            date_diagnostics.inspect(self.compiled, **pair)
        self.assertEqual(self.snapshot(), before)

    def test_missing_captures_empty_directory_missing_mapping_and_bad_JSON_are_explicit(self):
        self.save("one")
        self.outcomes.append(dict(case_id="missing", configuration_id=self.config["configuration_id"], packet_sha256="missing-hash"))
        pair = self.source_pair()
        result = date_diagnostics.inspect(self.compiled, **pair)
        self.assertFalse(result["diagnostic_complete"])
        self.assertEqual(result["coverage"]["missing_expected_cases"], ["missing"])
        self.mapping.clear()
        self.assertFalse(self.inspect()["diagnostic_complete"])
        write_private(self.compiled / "broken.json", b"{ broken JSON")
        self.assertTrue(any(r.get("error_type") == "JSONDecodeError" for r in self.inspect()["failures"]))
        empty = self.root / "empty"
        empty.mkdir()
        self.assertFalse(date_diagnostics.inspect(empty)["diagnostic_complete"])

    def test_lexical_parse_is_not_reported_as_grader_compatibility(self):
        self.save("month-name", "Jan 1, 2026")
        result = self.inspect()
        row = next(r for r in result['records'] if r.get('relative_path') == 'published_at')
        self.assertEqual(row['native']['state'], 'parsed')
        self.assertFalse(row['grading_reading']['original_native_interval_resolved'])
        self.assertFalse(row['grading_reading']['explicitly_admitted'])
        for coverage in self.group(result)['grading_reader_coverage'].values():
            self.assertEqual(coverage['diagnostic_resolved_but_grading_unresolved_occurrences'], 1)
            self.assertEqual(coverage['interval_resolved_occurrences'], 0)

    def test_partial_native_observation_failure_does_not_contribute_metrics(self):
        compiled = self.save("partial")
        compiled["packet"]["date_observations"].append(dict(relative_path="bad", raw_value="2026-01-01", mapping_id="invalid"))
        compiled["packet_sha256"] = digest(compiled["packet"])
        write_private(self.compiled / "partial.json", compiled, replace=True)
        result = self.inspect()
        self.assertFalse(result["diagnostic_complete"])
        self.assertEqual(result["groups"], [])
        self.assertTrue(result["records"])
        self.assertTrue(all(not r["included_in_metrics"] for r in result["records"]))

    def test_unreadable_compiled_file_is_preserved_as_per_file_failure(self):
        self.save("unreadable")
        target = (self.compiled / "unreadable.json").resolve()
        original = Path.read_bytes
        def read(path):
            if path.resolve() == target:
                raise PermissionError("synthetic unreadable compiled capture")
            return original(path)
        with patch.object(Path, "read_bytes", read):
            result = self.inspect()
        self.assertFalse(result["diagnostic_complete"])
        self.assertFalse(result["source_artifacts_unchanged"])
        self.assertTrue(any(f.get("source_read_error") for f in result["failures"]))

    def test_seltz_fetched_property_is_diagnostic_control_without_role_or_alias_remap(self):
        config = next(c for c in configuration_catalog(include_optional=True) if c["configuration_id"] == "seltz-web-pro")
        q = question("synthetic-seltz-property")
        row = dict(url="https://synthetic.example/2026/01/01/story", published_date="2026-01-01T09:00:00Z",
            properties=dict(title="Aster editorial", published_date="2026-01-01T09:00:00Z", fetched_at="2026-01-01T09:00:00Z"),
            snippets=[dict(text="Published: January 1, 2026")])
        position = project_response(config, dict(documents=[row]), "2026-11-05T00:00:00Z")["positions"][0]
        compiled = judging.compile_packet(judging.packet_for(q, config, position, "seltz-properties"), "2026-11-05T00:00:00Z", config=config)
        original = next(o for o in compiled["packet"]["date_observations"] if o["relative_path"] == "properties.fetched_at")
        self.assertEqual(original["date_kind"], "unclear")
        write_private(self.compiled / "seltz-properties.json", compiled)
        before = self.snapshot()
        result = date_diagnostics.inspect(self.compiled,
            case_configurations={"seltz-properties": "seltz-web-pro"}, configurations={"seltz-web-pro": config},
            thresholds=dict(minimum_unique_urls=1, minimum_url_agreement=1.0))
        self.assertTrue(result["diagnostic_complete"])
        fetched = next(r for r in result["records"] if r.get("relative_path") == "properties.fetched_at")
        self.assertEqual(fetched["native_observation"], original)
        self.assertEqual(fetched["date_role"]["saved_date_kind"], "unclear")
        self.assertTrue(fetched["date_role"]["negative_control"])
        self.assertFalse(fetched["date_role"]["diagnostic_control_basis"]["native_meaning_reclassified"])
        group = self.group(result, "properties.fetched_at")
        self.assertIsNone(group["current_registry_admission_candidate"])
        self.assertEqual(group["suggestion"]["state"], "meets_requested_thresholds")
        self.assertEqual(group["suggestion"]["role_recommendation"], "not_a_reviewed_publication_admission_candidate")
        self.assertIsNone(self.group(result, "properties.published_date")["current_registry_admission_candidate"])
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
