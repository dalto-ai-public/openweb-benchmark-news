from copy import deepcopy
from decimal import Decimal
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from openweb_news import fixtures, fresh, judging, reuse
from openweb_news.accounting import USAGE_PROFILE, usage_cost
from openweb_news.common import digest, encoded, load, write_private
from openweb_news.ledger import Ledger
from openweb_news.preparation import prepare
from openweb_news.runner import run
from openweb_news.scheduling import Admission, Pipeline


class UsageTests(unittest.TestCase):
    def native(self, **counters):
        usage = dict(promptTokenCount=100, totalTokenCount=125,
                     candidatesTokenCount=10, thoughtsTokenCount=15, cachedContentTokenCount=50)
        usage.update(counters)
        raw = encoded(dict(modelVersion=judging.METHOD["primary"]["model"], usageMetadata=usage))
        return dict(raw_text=raw.decode(), retained_sha256=digest(raw), complete=True,
                    dispatch_state="completed", http_status=200)

    def package(self):
        return dict(primary=judging.METHOD["primary"])

    def test_native_cost_includes_thinking_once_and_uses_no_cache_discount(self):
        request = dict(kind="model", backend="gemini-api")
        self.assertEqual(usage_cost(request, self.native(), self.package()), Decimal("0.00085"))
        # A larger native total is not silently discarded.
        self.assertEqual(usage_cost(request, self.native(totalTokenCount=140), self.package()), Decimal("0.00112"))
        self.assertEqual(usage_cost(request, self.native(cachedContentTokenCount=0), self.package()), Decimal("0.00085"))

    def test_missing_invalid_uncertain_and_non_pro_usage_releases_nothing(self):
        request = dict(kind="model", backend="gemini-api")
        good = self.native()
        bad = [dict(good, complete=False), dict(good, dispatch_state="uncertain"),
               dict(good, http_status=500), dict(good, error_type="ConnectionError"),
               dict(good, retained_sha256="tampered"), dict(good, credential_echo_redacted=True)]
        for change in (dict(promptTokenCount=-1), dict(promptTokenCount=True),
                       dict(totalTokenCount=99), dict(cachedContentTokenCount=101),
                       dict(thoughtsTokenCount=None)):
            bad.append(self.native(**change))
        missing = json.loads(good["raw_text"])
        del missing["usageMetadata"]["totalTokenCount"]
        raw = encoded(missing)
        bad.append(dict(good, raw_text=raw.decode(), retained_sha256=digest(raw)))
        for value in bad:
            self.assertIsNone(usage_cost(request, value, self.package()))
        self.assertIsNone(usage_cost(dict(request, backend="codex-subscription"), good, self.package()))

    def test_settlement_resumes_once_and_missing_usage_keeps_full_hold(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            policy = dict(spending_ceiling_usd="1", budget_accounting=USAGE_PROFILE,
                          retry_policy=dict(max_total=0, max_search=0, max_model=0))
            request = dict(kind="model", role="pro", reservation_usd="0.5", backend="gemini-api")
            with Ledger(root, {}) .locked() as ledger:
                admission = Admission(ledger, policy, package=self.package(), initial_committed_usd="0.1")
                ledger.execute("one", request, lambda _: self.native(), before_start=admission.reserve)
                pipeline = Pipeline(dict(google=dict(concurrency=1, minimum_start_interval_seconds=0)))
                admission.restore_pacing(ledger, pipeline, dict(pro="google"))
                ledger.execute("two", request, lambda _: dict(complete=False, dispatch_state="uncertain"),
                               before_start=admission.reserve)
                restored = Admission(ledger, policy, package=self.package(), initial_committed_usd="0.1")
                self.assertEqual(restored.total, Decimal("0.60085"))
                pipeline = Pipeline(dict(google=dict(concurrency=1, minimum_start_interval_seconds=0)))
                restored.restore_pacing(ledger, pipeline, dict(pro="google"))
                restored.restore_pacing(ledger, pipeline, dict(pro="google"))
                self.assertEqual(restored.total, Decimal("0.60085"))

    def test_usage_above_maximum_stops_further_admission(self):
        from openweb_news.scheduling import AdmissionBlocked
        with tempfile.TemporaryDirectory() as temp, Ledger(temp, {}).locked() as ledger:
            request = dict(kind="model", role="pro", reservation_usd="0.0001", backend="gemini-api")
            policy = dict(spending_ceiling_usd="1", budget_accounting=USAGE_PROFILE,
                          retry_policy=dict(max_total=0, max_search=0, max_model=0))
            a = Admission(ledger, policy, package=self.package())
            ledger.execute("one", request, lambda _: self.native(), before_start=a.reserve)
            pipeline = Pipeline(dict(google=dict(concurrency=1, minimum_start_interval_seconds=0)))
            a.restore_pacing(ledger, pipeline, dict(pro="google"))
            with self.assertRaises(AdmissionBlocked):
                ledger.execute("two", request, lambda _: self.native(), before_start=a.reserve)
            self.assertEqual(len([e for e in ledger.events() if e["event"] == "started"]), 1)


class FreshTests(unittest.TestCase):
    def source(self, root, uncertain=False):
        cfg = fixtures.config(root / "old")
        package = prepare(cfg, mode="fixture")
        write_private(root / "source.json", package)
        original = fixtures.model
        picked = []
        def model(role, compiled, folder):
            if uncertain and role == "codex" and not picked:
                picked.append(role + "-" + compiled["packet"]["case_id"])
                return dict(complete=False, transport_success=False, dispatch_state="uncertain")
            return original(role, compiled, folder)
        with patch("openweb_news.fixtures.model", side_effect=model):
            report = run(package)
        return package, report, picked

    def target(self, root, reference):
        cfg = fixtures.config(root / "new")
        cfg["fresh_judging"] = reference
        cfg["execution_policy"]["budget_accounting"] = USAGE_PROFILE
        return prepare(cfg, mode="fixture")

    def test_fresh_judges_saved_searches_without_old_votes_then_resumes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            source, original, _ = self.source(root)
            original_bytes = (root / "old/attempts.jsonl").read_bytes()
            original_report = (root / "old/report.json").read_bytes()
            ref = fresh.freeze(root / "source.json", root / "snapshot")
            target = self.target(root, ref)
            calls = []
            native_model = fixtures.model
            def model(role, compiled, folder):
                calls.append(role)
                return native_model(role, compiled, folder)
            with patch("openweb_news.fixtures.search", side_effect=AssertionError("no new search")), patch("openweb_news.fixtures.model", side_effect=model):
                new = run(target)
            self.assertEqual(new["scores"], original["scores"])
            self.assertGreater(len(calls), 0)
            self.assertEqual(new["execution"]["reused_search_cells"], 8)
            self.assertEqual(new["execution"]["fresh_judging"]["source_package_sha256"], digest(source))
            bindings = {r["task_id"]: r for r in target["reservation_forecast"]["request_bindings"]}
            for event in map(json.loads, (root / "new/attempts.jsonl").read_text().splitlines()):
                if event["event"] == "started":
                    request = load(root / "new/attempts" / event["attempt_id"] / "request.json")
                    amount = request.pop("reservation_usd")
                    request.pop("reservation_basis")
                    self.assertEqual(bindings[event["task_id"]]["request_sha256"], digest(request))
                    self.assertEqual(bindings[event["task_id"]]["reservation_usd"], amount)
            with patch("openweb_news.fixtures.search", side_effect=AssertionError("no search")), patch("openweb_news.fixtures.model", side_effect=AssertionError("no repeated judge")):
                self.assertEqual(run(target)["scores"], new["scores"])
            self.assertEqual((root / "old/attempts.jsonl").read_bytes(), original_bytes)
            self.assertEqual((root / "old/report.json").read_bytes(), original_report)
            self.assertNotIn("OPENWEB_NEWS_BRAVE_API_KEY", target["required_credential_references"])

    def test_uncertain_source_role_is_never_dispatched_in_fresh_epoch(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            _, _, blocked = self.source(root, uncertain=True)
            ref = fresh.freeze(root / "source.json", root / "snapshot")
            target = self.target(root, ref)
            native_model = fixtures.model
            def model(role, compiled, folder):
                self.assertNotIn(role + "-" + compiled["packet"]["case_id"], blocked)
                return native_model(role, compiled, folder)
            with patch("openweb_news.fixtures.search", side_effect=AssertionError("no search")), patch("openweb_news.fixtures.model", side_effect=model):
                report = run(target)
            held = report["execution"]["held_tasks"]
            self.assertTrue(any(h["task_id"] == blocked[0] for h in held))
            self.assertNotIn(blocked[0], {e["task_id"] for e in map(json.loads, (root / "new/attempts.jsonl").read_text().splitlines())})

    def test_case_id_change_cannot_bypass_uncertain_occurrence_hold(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            self.source(root, uncertain=True)
            ref = fresh.freeze(root / "source.json", root / "snapshot")
            target = self.target(root, ref)
            manifest = fresh._read(ref)[0]
            coordinate = manifest["summary"]["blocked_occurrences"][0]
            native_digest = digest
            def renamed(value):
                result = native_digest(value)
                return "changed-" + result if isinstance(value, dict) and set(value) == {"question", "configuration", "position"} else result
            with patch("openweb_news.runner.digest", side_effect=renamed), patch("openweb_news.fixtures.search", side_effect=AssertionError("no search")):
                report = run(target)
            row = next(row for row in report["outcomes"] if all(row[k] == coordinate[k] for k in ("configuration_id", "question_id", "rank")))
            self.assertTrue(row["case_id"].startswith("occurrence-changed-"))
            self.assertEqual(row["positions"][coordinate["role"]]["state"], "unjudged")
            self.assertTrue(any(h["task_id"] == coordinate["role"] + "-" + row["case_id"] for h in report["execution"]["held_tasks"]))

    def test_archived_health_is_separate_and_global_budget_stop_is_not_cleared(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            _, report, _ = self.source(root)
            report["execution"]["recovery"] = dict(archived_test_account=dict(stopped=True, pause_count=2))
            write_private(root / "old/report.json", report, replace=True)
            ref = fresh.freeze(root / "source.json", root / "snapshot")
            target = self.target(root, ref)
            result = run(target)
            self.assertEqual(result["execution"]["fresh_judging"]["archived_recovery"], report["execution"]["recovery"])
            self.assertEqual(result["execution"]["fresh_judging"]["new_health_epoch_accounts"],
                             sorted({target["account_bindings"][r] for r in ("pro", "codex", "claude")}))
            report["execution"]["stopped_reason"] = "spending admission ceiling reached"
            write_private(root / "old/report.json", report, replace=True)
            with self.assertRaisesRegex(ValueError, "global admission stop"):
                fresh.freeze(root / "source.json", root / "stopped-snapshot")

    def test_changed_scope_and_mutable_source_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            self.source(root)
            ref = fresh.freeze(root / "source.json", root / "snapshot")
            cfg = fixtures.config(root / "new")
            cfg["fresh_judging"] = ref
            cfg["execution_policy"]["budget_accounting"] = USAGE_PROFILE
            changed = deepcopy(cfg)
            changed["questions"][0]["provider_query"] += " changed"
            with self.assertRaises(ValueError):
                prepare(changed, mode="fixture")
            changed = deepcopy(cfg)
            changed["reuse_snapshot"] = reuse.freeze(root / "source.json", root / "other-snapshot")
            with self.assertRaises(ValueError):
                prepare(changed, mode="fixture")
            (root / "snapshot/source-report.json").write_text("{}")
            with self.assertRaises(ValueError):
                prepare(cfg, mode="fixture")


class FixtureAccountingTests(unittest.TestCase):
    def test_explicit_simulation_uses_existing_nonzero_rates_and_cannot_enter_live(self):
        from openweb_news.accounting import FIXTURE_PROFILE, reservation
        with tempfile.TemporaryDirectory() as temp:
            cfg = fixtures.config(Path(temp)/'run')
            ordinary = prepare(cfg, mode='fixture')
            request = dict(kind='search', configuration_id=cfg['provider_ids'][0])
            self.assertEqual(reservation(request, ordinary), 0)
            cfg['fixture_accounting'] = FIXTURE_PROFILE
            simulated = prepare(cfg, mode='fixture')
            amount = reservation(request, simulated)
            self.assertGreater(amount, 0)
            self.assertGreater(Decimal(simulated['reservation_forecast']['base_search_usd']), 0)
            self.assertGreater(Decimal(simulated['reservation_forecast']['base_model_usd']), 0)
            self.assertEqual(amount, reservation(request, dict(simulated, mode='live')))
            with self.assertRaisesRegex(ValueError, 'fixture-only accounting'):
                prepare(cfg, mode='live')
            cfg['fixture_accounting'] = 'undeclared-simulation'
            with self.assertRaises(ValueError):
                prepare(cfg, mode='fixture')
