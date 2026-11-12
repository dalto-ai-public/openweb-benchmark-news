from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from openweb_news import judging
from openweb_news._method import providers, scoring
from openweb_news.common import asset_bindings, digest
from openweb_news.ledger import Ledger, UncertainAttempt
from openweb_news.review import classify, needs_third, settle
from .helpers import native_response, question, verdict


class ProviderTests(unittest.TestCase):
    def test_pinned_native_contracts_independent_of_catalog(self):
        # Independently frozen expected native list/text shapes, not generated
        # from the catalog under test. Every text value remains synthetic.
        expected = {
            "brave-news": ("results", "description", "string", "url"),
            "brave-web": ("web.results", "description", "string", "url"),
            "bright-data-serp": ("organic", "description", "string", "link"),
            "exa-highlights": ("results", "highlights", "string_array", "url"),
            "firecrawl-news": ("data.news", "snippet", "string", "url"),
            "firecrawl-web": ("data.web", "description", "string", "url"),
            "linkup-fast": ("results", "content", "string", "url"),
            "oxylabs-search": ("data", "description", "string", "url"),
            "parallel-advanced": ("results", "excerpts", "string_array", "url"),
            "perplexity-web": ("results", "snippet", "string", "url"),
            "searlo-news": ("items", "snippet", "string", "link"),
            "searlo-web": ("items", "snippet", "string", "link"),
            "tavily-news": ("results", "content", "string", "url"),
            "tavily-general": ("results", "content", "string", "url"),
            "valyu-search": ("results", "description", "string", "url"),
            "you-com-web": ("results.web", "snippets", "string_array", "url"),
            "keenable-pro": ("results", "snippet", "string", "url"),
            "seltz-web-pro": ("documents", "snippets", "object_text_array", "url"),
            "serper-web": ("organic", "snippet", "string", "link"),
            "scavio-web": ("organic_results", "snippet", "string", "link"),
        }
        catalog = providers.configuration_catalog(include_optional=True)
        self.assertEqual(set(expected), {c["configuration_id"] for c in catalog})
        for config in catalog:
            with self.subTest(config=config["configuration_id"]):
                container, content, shape, url = expected[config["configuration_id"]]
                fields = dict(container=container, content=content, content_shape=shape, url=url, title="title")
                raw = native_response(dict(result_fields=fields))
                value = providers.project_response(config, raw, "2026-10-28T00:00:00Z")
                self.assertEqual(value["status"], "success")
                self.assertEqual(value["positions"][0]["url"]["value"], "https://news.example/aster")
                self.assertEqual(value["positions"][0]["content"]["value"],
                                 ["Editorial report: the fictional Aster observatory opened."] if shape in {"string_array", "object_text_array"} else
                                 "Editorial report: the fictional Aster observatory opened.")

    def test_every_adapter_and_empty_failure_shapes(self):
        for config in providers.configuration_catalog(include_optional=True):
            with self.subTest(config=config["configuration_id"]):
                request = providers.build_request(config, question())
                self.assertEqual(request["question_id"], "synthetic-1")
                response = providers.project_response(config, native_response(config), "2026-10-28T00:00:00Z")
                self.assertEqual(response["status"], "success")
                self.assertEqual(response["positions"][0]["rank"], 1)
                empty = providers.project_response(config, native_response(config, empty=True), "2026-10-28T00:00:00Z")
                self.assertEqual(empty["status"], "success_empty")
                wrong = providers.project_response(config, {}, "2026-10-28T00:00:00Z")
                self.assertEqual(wrong["status"], "missing_results")
                failed = providers.project_response(config, b"partial", "2026-10-28T00:00:00Z", http_status=429)
                self.assertEqual(failed["status"], "rate_limit_or_quota_error")

    def test_event_window_does_not_become_publication_filter(self):
        q = question()
        q["contract"]["temporal_requirements"] = [dict(id="e", basis="event", stage="opened", kind="opening",
            start_inclusive="2026-10-01T00:00:00Z", end_exclusive="2026-11-01T00:00:00Z")]
        config = next(c for c in providers.configuration_catalog() if c["configuration_id"] == "exa-highlights")
        request = providers.build_request(config, q)
        self.assertNotIn("startPublishedDate", request["body"])


class JudgeTests(unittest.TestCase):
    def setUp(self):
        self.config = providers.configuration_catalog()[0]
        projected = providers.project_response(self.config, native_response(self.config), "2026-10-28T00:00:00Z")
        self.packet = judging.packet_for(question(), self.config, projected["positions"][0], "case-1")
        self.compiled = judging.compile_packet(self.packet, "2026-10-28T00:00:00Z", config=self.config)

    def test_grades_and_unknown_are_preserved(self):
        for grade in (0, 1, 2):
            value = judging.decode_position(verdict(self.compiled, grade), self.compiled)
            self.assertEqual(value["state"], "valid", value)
            self.assertEqual(value["grade"], grade)
        raw = verdict(self.compiled)
        raw["overall_grade"] = None
        value = judging.decode_position(raw, self.compiled)
        self.assertEqual(value["state"], "valid")
        self.assertIsNone(value["grade"])

    def test_bad_quote_is_invalid_for_numeric_conformance(self):
        raw = verdict(self.compiled)
        raw["candidates"][0]["support"][0]["quote"] = "fact from a different result"
        decoded = judging.decode_position(raw, self.compiled)
        self.assertNotEqual(decoded["state"], "valid")
        self.assertIsNone(decoded["grade"])

    def test_packet_and_hash_isolation(self):
        blind = judging.blind_packet(dict(self.compiled, previous_grade=2))
        self.assertNotIn("previous_grade", blind)
        self.assertNotIn("packet", blind)
        self.assertNotIn("configuration_id", blind["input"]["complete_question"])
        self.assertNotIn("rank", blind["input"]["complete_question"])
        changed = deepcopy(self.compiled)
        changed["input"]["captured_at"] = "different"
        with self.assertRaises(ValueError):
            judging.blind_packet(changed)

    def test_primary_model_and_finish_validation(self):
        native = dict(modelVersion=judging.METHOD["primary"]["model"], candidates=[dict(finishReason="STOP",
            content=dict(parts=[dict(text=json.dumps(verdict(self.compiled)))]))])
        self.assertEqual(judging.response_result(json.dumps(native).encode(), self.compiled)["grade"], 2)
        native["modelVersion"] = "unapproved-substitute"
        self.assertEqual(judging.response_result(json.dumps(native).encode(), self.compiled)["state"], "unjudged")


def position(grade, state="valid", conform=True):
    return dict(grade=grade, state=state,
                policy_conformance="same_as_structural_base" if conform else "unresolved",
                independent=True, prior_verdict_exposure=False)


class ReviewTests(unittest.TestCase):
    def test_every_pair_of_classifications(self):
        states = [position(0), position(1), position(2), position(None), position(2, conform=False),
                  position(None, "invalid"), position(None, "unjudged")]
        for a in states:
            for b in states:
                with self.subTest(a=a, b=b):
                    first = [p["grade"] for p in (a, b) if classify(p) == "conforming_numeric"]
                    required = bool(first) and (len(first) == 1 or first[0] != first[1])
                    self.assertEqual(needs_third(a, b), required)
                    for third in [position(0), position(1), position(2), position(None), position(None, "invalid")]:
                        result = settle(a, b, third if required else None)
                        grades = first + ([third["grade"]] if required and classify(third) == "conforming_numeric" else [])
                        expected = next((g for g in (0, 1, 2) if grades.count(g) >= 2), None)
                        self.assertEqual(result["grade"], expected)

    def test_failed_primary_does_not_become_valid(self):
        result = settle(position(None, "invalid"), position(2), position(2))
        self.assertEqual(result["grade"], 2)
        self.assertEqual(result["states"]["pro"], "invalid")

    def test_no_average_and_no_exposed_third_vote(self):
        self.assertIsNone(settle(position(0), position(2), position(1))["grade"])
        exposed = dict(position(2), prior_verdict_exposure=True)
        with self.assertRaises(ValueError):
            settle(position(0), position(2), exposed)


class LedgerTests(unittest.TestCase):
    def test_resume_and_changed_input(self):
        calls = []
        with tempfile.TemporaryDirectory() as temp:
            ledger = Ledger(temp, dict(package="one"))
            with ledger.locked():
                for _ in range(2):
                    result = ledger.execute("task", {"query": "same"}, lambda _: calls.append(1) or {"transport_success": True})
                self.assertEqual(calls, [1])
                with self.assertRaises(ValueError):
                    ledger.execute("task", {"query": "changed"}, lambda _: {})
            with self.assertRaises(ValueError):
                with Ledger(temp, dict(package="two")).locked():
                    pass

    def test_uncertain_attempt_is_never_resent(self):
        with tempfile.TemporaryDirectory() as temp:
            ledger = Ledger(temp, {})
            with ledger.locked():
                ledger.append(dict(event="started", task_id="task", attempt_id="lost", request_sha256=digest({})))
                with self.assertRaises(UncertainAttempt):
                    ledger.execute("task", {}, lambda _: self.fail("must not resend"))

    def test_only_explicit_completed_covered_failures_retry(self):
        with tempfile.TemporaryDirectory() as temp:
            ledger = Ledger(temp, {})
            calls = []
            with ledger.locked():
                failed = dict(transport_success=False, complete=True, http_status=503, dispatch_state="completed")
                ledger.execute("task", {}, lambda _: calls.append(1) or failed, max_attempts=2)
                ledger.execute("task", {}, lambda _: self.fail("no implicit retry"), max_attempts=2)
                ledger.execute("task", {}, lambda _: calls.append(2) or dict(transport_success=True),
                               max_attempts=2, retry_failed=True, retry_http_statuses=[503])
            self.assertEqual(calls, [1, 2])

    def test_completed_record_with_uncertain_delivery_still_cannot_retry(self):
        with tempfile.TemporaryDirectory() as temp:
            ledger = Ledger(temp, {})
            with ledger.locked():
                ledger.execute("task", {}, lambda _: dict(transport_success=False, dispatch_state="uncertain"), max_attempts=2)
                with self.assertRaises(UncertainAttempt):
                    ledger.execute("task", {}, lambda _: self.fail("uncertain resend"), max_attempts=2,
                                   retry_failed=True, retry_http_statuses=[503])

    def test_orphan_matching_request_can_resume_before_dispatch(self):
        from openweb_news.common import private_directory, write_private
        with tempfile.TemporaryDirectory() as temp:
            attempt_id = digest(dict(task_id="task", attempt=1))
            folder = private_directory(Path(temp) / "attempts" / attempt_id)
            write_private(folder / "request.json", {})
            with Ledger(temp, {}).locked() as ledger:
                result = ledger.execute("task", {}, lambda _: dict(transport_success=True))
            self.assertTrue(result["transport_success"])


class ScoringTests(unittest.TestCase):
    def test_duplicate_first_url_and_shared_unknown(self):
        cell = dict(status="success", ranked=[dict(rank=i+1, evidence_id=f"e{i}", url="https://example.test/a") for i in range(2)])
        slots = scoring.ranked_slots(cell, {"e0": 1, "e1": 2}, {})
        self.assertEqual([s["gain"] for s in slots], [1, 0])
        ideal = scoring.shared_ideal([cell], {"e0": 1, "e1": None}, {})
        self.assertIsNone(ideal["idcg"][3])
        self.assertEqual(scoring.question_metrics(slots, 3, 0, {}, set())["ndcg"]["status"], "not_applicable")

    def test_empty_is_answered_failure_delivers_zero(self):
        q = question()
        cells = {("a", q["question_id"]): dict(status="success_empty", ranked=[]),
                 ("b", q["question_id"]): dict(status="http_error", ranked=[])}
        value = scoring.score([q], ["a", "b"], cells, {}, {}, 0.9, ["a", "b"])
        self.assertEqual(value["availability"]["answered_question_ids"]["a"], [q["question_id"]])
        self.assertEqual(value["availability"]["answered_question_ids"]["b"], [])
        self.assertEqual(value["accounting"]["b"][3]["counts"]["failed_call"], 3)


if __name__ == "__main__":
    unittest.main()
