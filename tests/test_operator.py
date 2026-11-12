from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from openweb_news import clean_run, fixtures, operator, profile_authority, review_pipeline, settlement
from openweb_news.common import digest, encoded, load, write_private
from openweb_news.preparation import prepare
from openweb_news.transport import Credentials
from . import test_clean_run as clean_fixture_cases
from .test_independent_method import accepted_fixture


class OperatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.package = prepare(fixtures.config(self.root / "native/run"), mode="live")
        self.path = self.root / "native/package.json"
        write_private(self.path, self.package)
        self.network = patch("socket.socket", side_effect=AssertionError("operator must not use network"))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.run_guard = patch("openweb_news.runner.run", side_effect=AssertionError("operator must not run"))
        self.run_guard.start()
        self.addCleanup(self.run_guard.stop)
        self.clean_guard = patch.object(clean_run, "run", side_effect=AssertionError("operator must not run clean lifecycle"))
        self.clean_guard.start()
        self.addCleanup(self.clean_guard.stop)

    def snapshot(self):
        return {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}

    def approval(self, package=None):
        return dict(decision="approved", package_sha256=digest(package or self.package), approved_by="synthetic owner", approved_at="2026-11-05T00:00:00Z")

    def ledger(self, *, unfinished=False):
        root = Path(self.package["configuration"]["run_directory"])
        request = self.package["search_requests"][0]
        task_id = "search-" + digest(dict(configuration=request["configuration_id"], question=request["question_id"]))
        wire = dict(kind="search", configuration_id=request["configuration_id"], reservation_usd="0.25")
        folder = root / "attempts/synthetic-attempt"
        write_private(folder / "request.json", wire)
        start = dict(event="started", task_id=task_id, attempt_id="synthetic-attempt", request_sha256=digest(wire))
        events = [start]
        if not unfinished:
            result = dict(transport_success=False, complete=True, http_status=503, raw_text="synthetic failure")
            write_private(folder / "result.json", result)
            events.append(dict(event="finished", task_id=task_id, attempt_id="synthetic-attempt", result_path="attempts/synthetic-attempt/result.json", result_sha256=digest((folder / "result.json").read_bytes())))
        write_private(root / "attempts.jsonl", b"".join(encoded(e) for e in events))
        return task_id

    def clean(self):
        native = deepcopy(self.package)
        later = deepcopy(native)
        later["required_credential_references"].append("SYNTHETIC_LATER_STAGE_KEY")
        package = dict(profile=clean_run.PROFILE, output_directory=str(self.root / "clean"),
            mode="live", shared_ceiling_usd="5", stages=[
                dict(index=1, count=10, native_prototype=native),
                dict(index=2, count=20, native_prototype=later)])
        path = self.root / "clean/clean-run.json"
        write_private(path, package)
        return path, package

    def partial_review(self, clean_path, role):
        root = clean_path.parent
        directory = (root / "stage-1/review" if role == "ordinary_review" else root / "stage-1-accepted-review/publication")
        job = dict(task_id=role + "-codex-toy", kind="model", backend="codex-subscription", role="codex", reservation_usd="0")
        package = dict(profile=review_pipeline.PROFILE if role == "ordinary_review" else settlement.PROFILE,
            run_directory=str(directory / "run"), jobs=[job])
        write_private(directory / "package.json", package)
        folder = directory / "run/attempts/unclassified-review"
        write_private(folder / "request.json", job)
        response = dict(transport_success=True, complete=True, http_status=200, dispatch_state="completed",
            raw_text="SYNTHETIC_RAW_RESPONSE_MUST_NOT_BE_IN_STATUS")
        write_private(folder / "result.json", response)
        events = [dict(event="started", task_id=job["task_id"], attempt_id="unclassified-review", request_sha256=digest(job)),
            dict(event="finished", task_id=job["task_id"], attempt_id="unclassified-review", result_path="attempts/unclassified-review/result.json", result_sha256=digest((folder / "result.json").read_bytes()))]
        write_private(directory / "run/attempts.jsonl", b"".join(encoded(e) for e in events))
        execution = dict(state="completed_with_holds", held_tasks=[job["task_id"]], permanent_stop_roles=["codex"])
        write_private(directory / "run/report.json", dict(package_sha256=digest(package), execution=execution))
        write_private(directory / "run/closure.json", dict(package_sha256=digest(package), execution=execution))
        return directory / "package.json", package

    def test_prepared_status_and_verify_are_read_only(self):
        before = self.snapshot()
        state = operator.status(self.path)
        self.assertEqual(state["state"], "prepared")
        self.assertEqual(state["completed"]["local_finished_tasks"], 0)
        self.assertIsNone(state["pending"]["exact_remaining_dispatches"])
        self.assertEqual(state["pending"]["search_cells_without_local_finished_task"], len(self.package["search_requests"]))
        result = operator.verify(self.path)
        self.assertTrue(result["verified"])
        self.assertIn("native_package_rebuild", result["checks"])
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(Path(self.package["configuration"]["run_directory"]).exists())

    def test_native_status_preserves_finished_failure_and_uncertain_cost_holds(self):
        task = self.ledger()
        run = Path(self.package["configuration"]["run_directory"])
        write_private(run / "report.json", dict(package_sha256=digest(self.package), execution=dict(
            state="completed_with_holds", committed_and_held_usd="0.25", stopped_reason="synthetic ceiling hold",
            held_tasks=[dict(task_id=task, stage="search", reason="synthetic hold")],
            recovery=[dict(account="synthetic-account", mode="stopped")])))
        before = self.snapshot()
        state = operator.status(self.path)
        self.assertEqual(state["completed"]["local_finished_attempts"], 1)
        self.assertEqual(state["costs"]["ledger_estimates_and_holds"]["conservative_total_usd"], "0.25")
        self.assertTrue(state["held"]["reported_tasks"])
        self.assertEqual(len(state["stops"]), 2)
        self.assertTrue(operator.verify(self.path)["verified"])
        self.assertEqual(self.snapshot(), before)

    def test_unfinished_attempt_is_visible_without_reconciliation(self):
        self.ledger(unfinished=True)
        before = self.snapshot()
        state = operator.status(self.path)
        self.assertEqual(state["held"]["unfinished_attempts"][0]["state"], "in_flight_or_interrupted")
        self.assertEqual(state["costs"]["ledger_estimates_and_holds"]["other_conservative_holds_and_allowances_usd"], "0.25")
        self.assertEqual(self.snapshot(), before)

    def test_status_event_stop_visible_without_report(self):
        run = Path(self.package["configuration"]["run_directory"])
        write_private(run / "status-events.jsonl", encoded(dict(account="synthetic-account", configuration="synthetic", mode="stopped")))
        self.assertEqual(operator.status(self.path)["stops"][0]["source"], "status_events")

    def test_verify_rejects_changed_package_or_bound_native_evidence(self):
        changed = deepcopy(self.package)
        changed["scope"]["search_cells"] += 1
        write_private(self.path, changed, replace=True)
        with self.assertRaises(ValueError):
            operator.verify(self.path)
        write_private(self.path, self.package, replace=True)
        self.ledger()
        result = Path(self.package["configuration"]["run_directory"]) / "attempts/synthetic-attempt/result.json"
        write_private(result, dict(changed=True), replace=True)
        with self.assertRaises(ValueError):
            operator.verify(self.path)

    def test_verify_reports_unclassified_native_completion_as_failure(self):
        root = Path(self.package["configuration"]["run_directory"])
        folder = root / "attempts/unclassified-toy"
        request = dict(kind="model", backend="codex-subscription", role="codex", reservation_usd="0.20")
        response = dict(transport_success=True, complete=True, http_status=200, dispatch_state="completed")
        write_private(folder / "request.json", request)
        write_private(folder / "result.json", response)
        events = [dict(event="started", task_id="codex-toy", attempt_id="unclassified-toy", request_sha256=digest(request)),
                  dict(event="finished", task_id="codex-toy", attempt_id="unclassified-toy", result_path="attempts/unclassified-toy/result.json", result_sha256=digest((folder / "result.json").read_bytes()))]
        write_private(root / "attempts.jsonl", b"".join(encoded(e) for e in events))
        before = self.snapshot()
        result = operator.verify(self.path)
        self.assertTrue(result["package_verified"])
        self.assertFalse(result["verified"])
        self.assertEqual(result["ledger_classification"], dict(state="unclassified", unclassified_attempt_ids=["unclassified-toy"]))
        self.assertEqual(self.snapshot(), before)

    def test_clean_status_reads_stage_gate_and_stops_without_mutating(self):
        path, package = self.clean()
        write_private(path.parent / "stage-1/gate-evidence.json", dict(costs=dict(conservative_total_usd="0.75")))
        before = self.snapshot()
        state = operator.status(path)
        self.assertEqual(state["state"], "awaiting_stage_review")
        self.assertEqual(state["stage"], 1)
        self.assertEqual(state["pending"]["stage_indices"], [1, 2])
        self.assertEqual(state["costs"]["stage_costs"][0]["costs"]["conservative_total_usd"], "0.75")
        self.assertEqual(self.snapshot(), before)
        write_private(path.parent / "stage-1/stage-review.json", dict(decision="stop"))
        self.assertEqual(operator.status(path)["state"], "stopped_after_review")

    def test_clean_verify_uses_existing_offline_owner(self):
        path, package = self.clean()
        before = self.snapshot()
        with patch.object(clean_run, "verify", return_value=(package, self.package)) as delegated:
            self.assertTrue(operator.verify(path)["verified"])
        delegated.assert_called_once_with(path.resolve())
        self.assertEqual(self.snapshot(), before)

    def test_clean_status_surfaces_known_review_holds_roles_and_counts_without_raw_responses(self):
        path, _ = self.clean()
        for role in ("ordinary_review", "accepted_review"):
            self.partial_review(path, role)
        before = self.snapshot()
        result = operator.status(path)
        reviews = result["stages"][0]["model_reviews"]
        self.assertEqual({r["review_stage"] for r in reviews}, {"ordinary_review", "accepted_review"})
        self.assertEqual({r["review_stage"] for r in result["stops"]}, {"ordinary_review", "accepted_review"})
        self.assertTrue(all(r["role"] == "codex" for r in result["stops"]))
        for review in reviews:
            self.assertEqual(review["completed"]["local_finished_jobs"], 1)
            self.assertEqual(review["state"], "completed_with_holds")
            self.assertTrue(review["held"]["reported_tasks"])
            self.assertTrue(review["closure_path"])
        self.assertNotIn("SYNTHETIC_RAW_RESPONSE_MUST_NOT_BE_IN_STATUS", json.dumps(result))
        self.assertEqual(self.snapshot(), before)

    def test_clean_partial_known_reviews_audit_unclassified_completions_before_closure(self):
        path, package = self.clean()
        for role in ("ordinary_review", "accepted_review"):
            self.partial_review(path, role)
        before = self.snapshot()
        # These unit fixtures isolate delegation to each existing package gate;
        # the completion ledger classifier itself is real and must reject the
        # successful synthetic response lacking native proof.
        with patch.object(clean_run, "verify", return_value=(package, self.package)), \
                patch.object(review_pipeline, "verify", return_value={}) as ordinary, \
                patch.object(settlement, "verify", return_value=({}, {}, self.package)) as accepted:
            result = operator.verify(path)
        self.assertTrue(result["package_verified"])
        self.assertFalse(result["stage_evidence_verified"])
        self.assertFalse(result["verified"])
        checks = result["checks"][2]["existing_clean_review_stages"]
        self.assertEqual({r["review_stage"] for r in checks}, {"ordinary_review", "accepted_review"})
        self.assertTrue(all(r["ledger_classification"]["unclassified_attempt_ids"] == ["unclassified-review"] for r in checks))
        self.assertEqual(ordinary.call_count, 1)
        self.assertEqual(accepted.call_count, 1)
        self.assertFalse((path.parent / "closure.json").exists())
        self.assertEqual(self.snapshot(), before)

    def test_clean_partial_native_without_checkpoint_cannot_claim_verified(self):
        path, package = self.clean()
        write_private(path.parent / "stage-1/native/package.json", self.package)
        before = self.snapshot()
        with patch.object(clean_run, "verify", return_value=(package, self.package)):
            result = operator.verify(path)
        self.assertTrue(result["package_verified"])
        self.assertFalse(result["verified"])
        self.assertFalse(result["stage_evidence_verified"])
        stage = result["checks"][1]["existing_clean_native_stages"][0]
        self.assertFalse(stage["stage_derivation"]["verified"])
        self.assertEqual(self.snapshot(), before)

    def test_real_partial_clean_root_audits_native_and_binds_checkpoint_without_execution(self):
        root = (self.root / "real-partial").resolve()
        # Build only a synthetic archived diagnostic via the existing fixture
        # helper. The clean stage itself is never executed, and credentials are
        # explicitly empty even during that offline fixture setup.
        with patch("openweb_news.runner.Credentials", side_effect=lambda *a, **k: Credentials(environment={})):
            cfg, selection, reference, _, _ = clean_fixture_cases.CleanRunTests().setup_run(root, counts=(1,))
        cfg.pop("live_profile_authority", None)
        cfg.pop("method_acceptance_package", None)
        cfg, _ = accepted_fixture(root / "explicit-authority", cfg)
        authority = profile_authority.read(cfg["method_acceptance_package"], cfg["reviewers"])
        cfg["live_profile_authority"] = authority
        template = prepare(cfg, mode="fixture")
        write_private(root / "template.json", template, replace=True)
        package = clean_run.prepare(root / "template.json", selection, root / "clean", ceiling="100",
            accepted_profile=authority["package_path"], archived_reference=reference, stage_counts=(1,), mode="fixture")
        path = root / "clean/clean-run.json"
        native = package["stages"][0]["native_prototype"]
        stage_root = path.parent / "stage-1"
        checkpoint = dict(profile=clean_run.STAGE_PROFILE, clean_manifest_path=str(path), clean_package_sha256=digest(package),
            index=1, accepted_profile=authority, parent_report=None, carried_usd="0", native_package_sha256=digest(native))
        write_private(stage_root / "checkpoint.json", checkpoint)
        write_private(stage_root / "native/package.json", native)
        folder = stage_root / "native/run/attempts/unclassified-partial"
        request = dict(kind="model", backend="codex-subscription", role="codex", reservation_usd="0")
        response = dict(transport_success=True, complete=True, http_status=200, dispatch_state="completed")
        write_private(folder / "request.json", request)
        write_private(folder / "result.json", response)
        events = [dict(event="started", task_id="codex-toy", attempt_id="unclassified-partial", request_sha256=digest(request)),
                  dict(event="finished", task_id="codex-toy", attempt_id="unclassified-partial", result_path="attempts/unclassified-partial/result.json", result_sha256=digest((folder / "result.json").read_bytes()))]
        write_private(stage_root / "native/run/attempts.jsonl", b"".join(encoded(e) for e in events))
        before = self.snapshot()
        result = operator.verify(path)
        self.assertTrue(result["package_verified"])
        self.assertFalse(result["stage_evidence_verified"])
        self.assertFalse(result["verified"])
        stage = result["checks"][1]["existing_clean_native_stages"][0]
        self.assertTrue(stage["stage_derivation"]["verified"])
        self.assertEqual(stage["native_verification"]["ledger_classification"], dict(state="unclassified", unclassified_attempt_ids=["unclassified-partial"]))
        self.assertFalse((path.parent / "closure.json").exists())
        self.assertEqual(self.snapshot(), before)
        write_private(stage_root / "checkpoint.json", dict(checkpoint, carried_usd="0.20"), replace=True)
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "stage package/checkpoint derivation differs"):
            operator.verify(path)
        self.assertEqual(self.snapshot(), before)

    def test_clean_surfaces_native_stops_and_holds(self):
        path, _ = self.clean()
        self.ledger(unfinished=True)
        write_private(path.parent / "stage-1/native/package.json", self.package)
        run = Path(self.package["configuration"]["run_directory"])
        write_private(run / "status-events.jsonl", encoded(dict(account="synthetic-account", mode="stopped")))
        result = operator.status(path)
        self.assertEqual(result["stops"][0]["stage_index"], 1)
        self.assertTrue(result["held"][0]["unfinished_attempts"])

    def test_clean_shared_holds_count_each_native_stage_once_not_cumulative_gates(self):
        path, package = self.clean()
        package["shared_ceiling_usd"] = "100"
        write_private(path, package, replace=True)
        for index, gate_cost in ((1, "0.20"), (2, "0.40")):
            native = deepcopy(self.package)
            native_root = path.parent / ("stage-" + str(index)) / "native"
            native["configuration"]["run_directory"] = str(native_root / "run")
            write_private(native_root / "package.json", native)
            request = dict(kind="search", reservation_usd="0.20")
            write_private(native_root / "run/attempts/toy/request.json", request)
            write_private(native_root / "run/attempts.jsonl", encoded(dict(event="started", task_id="search-toy", attempt_id="toy", request_sha256=digest(request))))
            write_private(native_root.parent / "gate-evidence.json", dict(costs=dict(conservative_total_usd=gate_cost)))
        before = self.snapshot()
        costs = operator.status(path)["costs"]
        self.assertEqual(costs["shared_committed_and_held_usd"], "0.40")
        self.assertEqual(costs["remaining_allowance_usd"], "99.60")
        self.assertIn("cumulative gate costs are not added", costs["shared_basis"])
        self.assertFalse(costs["invoice_verified"])
        self.assertEqual(self.snapshot(), before)

    def test_presence_returns_only_names_and_bools_without_loading_implicit_env(self):
        refs = self.package["required_credential_references"]
        secret = "SYNTHETIC_SECRET_VALUE_MUST_NOT_APPEAR"
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("openweb_news.transport.dotenv", side_effect=AssertionError("no implicit env file")), redirect_stdout(stdout), redirect_stderr(stderr):
            result = operator.credential_presence(self.path, environ={refs[0]: secret})
        self.assertFalse(result["authentication_verified"])
        self.assertFalse(result["all_present"])
        self.assertNotIn(secret, json.dumps(result) + stdout.getvalue() + stderr.getvalue())
        self.assertTrue(next(r["present"] for r in result["credentials"] if r["reference"] == refs[0]))

    def test_explicit_toy_env_file_and_clean_future_stage_union(self):
        path, package = self.clean()
        env = self.root / "toy.env"
        env.write_text("SYNTHETIC_LATER_STAGE_KEY='SYNTHETIC_PRIVATE_VALUE'\n")
        before = self.snapshot()
        result = operator.credential_presence(path, env_file=env, environ={})
        self.assertTrue(next(r["present"] for r in result["credentials"] if r["reference"] == "SYNTHETIC_LATER_STAGE_KEY"))
        self.assertNotIn("SYNTHETIC_PRIVATE_VALUE", json.dumps(result))
        self.assertEqual(result["sources"], ["environment", "explicit_env_file"])
        self.assertEqual(self.snapshot(), before)

    def test_bad_toy_env_has_no_value_in_exception_or_output(self):
        env = self.root / "invalid-toy.env"
        env.write_text("SYNTHETIC_SECRET_BAD_LINE_MUST_NOT_APPEAR\n")
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(ValueError) as caught:
            operator.credential_presence(self.path, env_file=env, environ={})
        self.assertNotIn("SYNTHETIC_SECRET_BAD_LINE_MUST_NOT_APPEAR", str(caught.exception) + out.getvalue())

    def test_record_existing_approval_is_exact_private_idempotent_and_conflict_safe(self):
        receipt = self.approval()
        output = self.root / "recorded/approval.json"
        first = operator.record_approval(self.path, receipt, output)
        self.assertFalse(first["already_recorded"])
        self.assertFalse(first["execution_started"])
        self.assertEqual(first["scope"], self.package["scope"])
        self.assertEqual(load(output), receipt)
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        self.assertTrue(operator.record_approval(self.path, receipt, output)["already_recorded"])
        with self.assertRaisesRegex(ValueError, "conflicting"):
            operator.record_approval(self.path, dict(receipt, approved_at="later explicit approval"), output)
        self.assertEqual(load(output), receipt)

    def test_record_rejects_vague_intent_missing_identity_and_wrong_package(self):
        for receipt in ("please proceed", {}, dict(self.approval(), decision="maybe"),
                        dict(self.approval(), approved_by=""), dict(self.approval(), package_sha256="wrong")):
            with self.subTest(receipt=receipt), self.assertRaises(ValueError):
                operator.record_approval(self.path, receipt)
        self.assertFalse((self.path.parent / "approval.json").exists())

    def test_clean_approval_uses_existing_gate_and_does_not_add_receipt_fields(self):
        path, package = self.clean()
        receipt = self.approval(package)
        with patch.object(clean_run, "verify", return_value=(package, self.package)) as delegated:
            result = operator.record_approval(path, receipt)
        delegated.assert_called_once_with(path.resolve())
        self.assertEqual(result["scope"]["shared_ceiling_usd"], "5")
        self.assertEqual(load(path.parent / "approval.json"), receipt)

    def test_unknown_package_is_explicitly_unsupported(self):
        path = self.root / "historical/package.json"
        write_private(path, dict(profile="historical-review-not-supported"))
        self.assertFalse(operator.status(path)["supported"])
        self.assertFalse(operator.verify(path)["verified"])
        self.assertFalse(operator.credential_presence(path, environ={})["supported"])
        with self.assertRaises(ValueError):
            operator.record_approval(path, {})
        write_private(path, ["unsupported historical object"], replace=True)
        self.assertFalse(operator.status(path)["supported"])


if __name__ == "__main__":
    unittest.main()
