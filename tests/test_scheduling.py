from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from openweb_news import backends, fixtures
from openweb_news.common import digest, load
from openweb_news.ledger import Ledger
from openweb_news.preparation import prepare
from openweb_news.runner import run
from openweb_news.scheduling import Admission, AdmissionBlocked, Pipeline, retry_after_deadline, task_ready_at, timestamp


class SchedulingTests(unittest.TestCase):
    def test_large_ready_queue_avoids_scanning_when_account_is_full(self):
        from openweb_news.recovery import sibling_rule
        count=52000
        # This checks queue scanning, not wall-clock pacing. A fixed clock
        # keeps clock corrections from turning ready jobs into future jobs.
        pipe=Pipeline({"account":dict(concurrency=8,minimum_start_interval_seconds=0)},
            clock=lambda:1000, recovery={"account":sibling_rule("judge",8)})
        completed=[]
        for index in range(count):pipe.submit("account",lambda:None,completed.append,scope="pro")
        with patch.object(pipe.recovery,"capacity",wraps=pipe.recovery.capacity) as calls:
            pipe.run()
            self.assertLessEqual(calls.call_count,count*2)
        self.assertEqual(len(completed),count)

    def test_saved_completed_configuration_failure_is_not_repeated_or_accepted(self):
        original=fixtures.model
        failed=[]
        def model(role,compiled,folder):
            case=compiled["packet"]["case_id"]
            if role=="claude" and not failed:
                failed.append(case)
                return dict(transport_success=False,fatal_configuration=True,dispatch_state="completed",verdict={"must_not_be_used":True})
            self.assertFalse(role=="claude" and case==failed[0] if failed else False,"saved failed call must not repeat")
            return original(role,compiled,folder)
        with tempfile.TemporaryDirectory() as temp:
            package=prepare(fixtures.config(Path(temp)/"run"),mode="fixture")
            with patch("openweb_news.fixtures.model",side_effect=model):
                with self.assertRaises(RuntimeError):run(package)
                report=run(package)
            outcome=next(row for row in report["outcomes"] if row["case_id"]==failed[0])
            self.assertEqual(outcome["positions"]["claude"]["state"],"unjudged")
            self.assertEqual(len(report["execution"]["held_tasks"]),1)

    def test_ceiling_stop_writes_partial_report_and_resume_reuses_paid_results(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg = fixtures.config(Path(temp) / "run")
            cfg["execution_policy"]["spending_ceiling_usd"] = "0.35"
            package = prepare(cfg, mode="fixture")
            with patch("openweb_news.runner.reservation", return_value="0.1"):
                first = run(package)
                second = run(package)
            self.assertEqual(first["execution"]["state"], "stopped_at_ceiling")
            self.assertGreater(len(first["execution"]["held_tasks"]), 0)
            self.assertEqual(first["execution"]["retained_started_attempts"], 3)
            self.assertEqual(second["execution"]["retained_started_attempts"], 3)
            self.assertGreater(second["execution"]["reused_tasks"], 0)
            self.assertIn("partial report", (Path(temp) / "run/report.html").read_text())
            self.assertIn("not_collected_slots", (Path(temp) / "run/summary.csv").read_text())
            self.assertTrue(any("not_collected" in counts["counts"] for cutoffs in first["scores"]["accounting"].values() for counts in cutoffs.values()))

    def test_interrupted_task_is_held_while_other_work_finishes(self):
        class SimulatedCrash(BaseException):
            pass
        original = fixtures.model
        failed = []
        lock = threading.Lock()
        def interrupted(role, compiled, folder):
            with lock:
                if role == "pro" and not failed:
                    failed.append(compiled["packet"]["case_id"])
                    raise SimulatedCrash()
            return original(role, compiled, folder)
        def resumed(role, compiled, folder):
            self.assertFalse(role == "pro" and compiled["packet"]["case_id"] == failed[0], "uncertain primary must not repeat")
            return original(role, compiled, folder)
        with tempfile.TemporaryDirectory() as temp:
            package = prepare(fixtures.config(Path(temp) / "run"), mode="fixture")
            with patch("openweb_news.fixtures.model", side_effect=interrupted):
                with self.assertRaises(SimulatedCrash):
                    run(package)
            with patch("openweb_news.fixtures.model", side_effect=resumed):
                report = run(package)
            self.assertEqual(report["execution"]["state"], "completed_with_holds")
            self.assertEqual(len(report["outcomes"]), 8)
            self.assertEqual(report["execution"]["held_tasks"][0]["dispatch_state"], "uncertain")
            with Ledger(Path(temp) / "run", dict(package_sha256=digest(package), mode="fixture")).locked() as ledger:
                events = ledger.task_events("pro-" + failed[0])
                self.assertEqual([event["event"] for event in events], ["started"])

    def test_exhausted_retry_allowance_keeps_original_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg = fixtures.config(Path(temp) / "run")
            cfg["execution_policy"]["max_attempts_per_task"] = 2
            cfg["execution_policy"]["retry_policy"]["http_statuses"] = [503]
            package = prepare(cfg, mode="fixture")
            first = run(package)
            second = run(package, retry_failed=True)
            self.assertEqual(first["scores"], second["scores"])
            self.assertEqual(first["execution"]["retained_started_attempts"], second["execution"]["retained_started_attempts"])

    def test_same_task_is_single_flight_and_other_tasks_overlap(self):
        with tempfile.TemporaryDirectory() as temp, Ledger(temp, {}).locked() as ledger:
            barrier = threading.Barrier(2)
            entered = threading.Event()
            calls = []
            def send(_):
                calls.append(1)
                entered.set()
                barrier.wait(timeout=3)
                return dict(transport_success=True)
            with ThreadPoolExecutor(max_workers=3) as pool:
                first = pool.submit(ledger.execute, "one", {}, send)
                self.assertTrue(entered.wait(timeout=3))
                duplicate = pool.submit(ledger.execute, "one", {}, lambda _: self.fail("duplicate send"))
                other = pool.submit(ledger.execute, "two", {}, send)
                self.assertTrue(first.result()["transport_success"])
                self.assertEqual(first.result(), duplicate.result())
                self.assertEqual(first.result(), other.result())
            self.assertEqual(len(calls), 2)
            self.assertEqual(len(ledger.events()), 4)

    def test_spending_and_retry_admission_are_atomic_and_denials_unsent(self):
        with tempfile.TemporaryDirectory() as temp, Ledger(temp, {}).locked() as ledger:
            policy = dict(spending_ceiling_usd="1", retry_policy=dict(max_total=0, max_search=0, max_model=0))
            admission = Admission(ledger, policy)
            request = dict(kind="model", reservation_usd="0.7")
            barrier = threading.Barrier(2)
            sent = []
            def execute(task):
                barrier.wait(timeout=3)
                try:
                    return ledger.execute(task, request, lambda _: sent.append(task) or dict(transport_success=True), before_start=admission.reserve)
                except AdmissionBlocked:
                    return "unsent"
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(execute, ("one", "two")))
            self.assertEqual(results.count("unsent"), 1)
            self.assertEqual(len(sent), 1)
            self.assertEqual(len(ledger.events()), 2)
            self.assertEqual(str(admission.total), "0.7")
            admission = Admission(ledger, policy)
            self.assertEqual(str(admission.total), "0.7")
            with self.assertRaises(AdmissionBlocked):
                admission.reserve(sent[0], dict(kind="model", reservation_usd="0"), 2)

    def test_account_delay_does_not_block_other_work_or_busy_poll(self):
        clock = [100.0]
        sleeps, events = [], []
        def sleep(seconds):
            sleeps.append(seconds)
            clock[0] += seconds
        rules = {name: dict(concurrency=1, minimum_start_interval_seconds=0) for name in ("delayed", "ready")}
        pipeline = Pipeline(rules, clock=lambda: clock[0], sleeper=sleep)
        pipeline.defer("delayed", 105)
        pipeline.submit("delayed", lambda: "delayed", lambda result: events.append((result, clock[0])))
        pipeline.submit("ready", lambda: "ready", lambda result: events.append((result, clock[0])))
        pipeline.run()
        self.assertEqual(events, [("ready", 100), ("delayed", 105)])
        self.assertEqual(sleeps, [5])

    def test_interval_and_future_ready_job_do_not_hold_ready_job_in_same_account(self):
        clock, events = [100.0], []
        pipeline = Pipeline({"account": dict(concurrency=2, minimum_start_interval_seconds=2)},
                            clock=lambda: clock[0], sleeper=lambda delay: clock.__setitem__(0, clock[0] + delay))
        pipeline.submit("account", lambda: "later", lambda value: events.append((value, clock[0])), ready_at=110)
        pipeline.submit("account", lambda: "first", lambda value: events.append((value, clock[0])))
        pipeline.submit("account", lambda: "second", lambda value: events.append((value, clock[0])))
        pipeline.run()
        self.assertEqual(events, [("first", 100), ("second", 102), ("later", 110)])

    def test_retry_after_forms_and_remaining_backoff_survive_resume(self):
        finish = 100.0
        date = format_datetime(datetime.fromtimestamp(140, tz=timezone.utc), usegmt=True)
        self.assertEqual(retry_after_deadline(dict(response_headers=[("Retry-After", "30")]), finish), 130)
        self.assertEqual(retry_after_deadline(dict(response_headers=[("retry-after", date)]), finish), 140)
        self.assertEqual(retry_after_deadline(dict(response_headers=[("Retry-After", "nan")]), finish), finish)
        policy = dict(retry_policy=dict(backoff_seconds=5))
        state = dict(dispatch=True, previous=dict(response_headers=[("Retry-After", "30")]),
                     completed_at=datetime.fromtimestamp(finish, tz=timezone.utc).isoformat())
        self.assertEqual(task_ready_at(state, policy), 130)
        self.assertEqual(task_ready_at(dict(dispatch=False), policy), 0)

    def test_account_retry_floor_preserves_other_accounts_and_longer_retry_after(self):
        state = dict(dispatch=True, attempt=2, previous=dict(response_headers=[]),
                     completed_at=datetime.fromtimestamp(100, tz=timezone.utc).isoformat())
        policy = dict(retry_policy=dict(backoff_seconds=[15, 60]), scheduling=dict(accounts={
            "firecrawl": dict(concurrency=2, minimum_start_interval_seconds=6.5,
                              retry_backoff_floor_seconds=60),
            "other": dict(concurrency=2, minimum_start_interval_seconds=0)}))
        self.assertEqual(task_ready_at(state, policy, "firecrawl"), 160)
        self.assertEqual(task_ready_at(state, policy, "other"), 115)
        state["previous"]["response_headers"] = [("Retry-After", "90")]
        self.assertEqual(task_ready_at(state, policy, "firecrawl"), 190)
        # The retained completion time fixes the deadline; resume cannot renew it.
        self.assertEqual(task_ready_at(state, policy, "firecrawl"), 190)
        with tempfile.TemporaryDirectory() as temp:
            cfg = fixtures.config(Path(temp) / "run")
            account = cfg["execution_policy"]["scheduling"]["accounts"]["provider/OPENWEB_NEWS_EXA_API_KEY"]
            account["retry_backoff_floor_seconds"] = 60
            self.assertEqual(prepare(cfg, mode="fixture")["configuration"]["execution_policy"]["scheduling"]["accounts"]["provider/OPENWEB_NEWS_EXA_API_KEY"]["retry_backoff_floor_seconds"], 60)
            account["retry_backoff_floor_seconds"] = -1
            with self.assertRaises(ValueError):
                prepare(cfg, mode="fixture")

    def test_pipeline_overlaps_search_and_independent_first_positions(self):
        original_search, original_model = fixtures.search, fixtures.model
        joined = threading.Event()
        barrier = threading.Barrier(2, action=joined.set)
        locked = threading.Lock()
        chosen = []
        def search(request, provider, folder):
            if request["question_id"] == "synthetic-empty":
                self.assertTrue(joined.wait(timeout=3), "collection blocked independent model work")
            return original_search(request, provider, folder)
        def model(role, compiled, folder):
            case = compiled["packet"]["case_id"]
            with locked:
                if not chosen:
                    chosen.append(case)
                wait_for_peer = case == chosen[0] and role in {"pro", "codex"}
            if wait_for_peer:
                barrier.wait(timeout=3)
            return original_model(role, compiled, folder)
        with tempfile.TemporaryDirectory() as temp:
            package = prepare(fixtures.config(Path(temp) / "run"), mode="fixture")
            with patch("openweb_news.fixtures.search", side_effect=search), patch("openweb_news.fixtures.model", side_effect=model):
                first = run(package)
            self.assertTrue(joined.is_set())
            # Even an account still in Retry-After must reuse completed tasks now.
            from openweb_news import scheduling
            with patch.object(scheduling.Pipeline, "run", side_effect=AssertionError("no pending work on cached resume")):
                # The runner can skip entering the scheduler for a complete replay.
                second = run(package)
            self.assertEqual(first["scores"], second["scores"])

    def test_scheduling_is_exact_and_shared_accounts_are_bound(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg = fixtures.config(Path(temp) / "run")
            cfg["execution_policy"]["scheduling"]["accounts"]["codex-subscription"]["concurrency"] = 2
            cfg["reviewers"]["codex"]["transport"] = "serial-cli-v1"
            with self.assertRaises(ValueError):
                prepare(cfg)
            cfg = fixtures.config(Path(temp) / "run")
            cfg["provider_overrides"] = {"exa-highlights": dict(secret_ref="OPENWEB_NEWS_BRAVE_API_KEY")}
            del cfg["execution_policy"]["scheduling"]["accounts"]["provider/OPENWEB_NEWS_EXA_API_KEY"]
            package = prepare(cfg, mode="fixture")
            self.assertEqual(package["account_bindings"]["search/brave-news"], package["account_bindings"]["search/exa-highlights"])
            cfg = fixtures.config(Path(temp) / "run")
            del cfg["execution_policy"]["scheduling"]["accounts"]["claude-subscription"]
            with self.assertRaises(ValueError):
                prepare(cfg)

    def test_worker_capacity_is_shared_by_all_tasks_in_account(self):
        barrier, mutex = threading.Barrier(2), threading.Lock()
        active, peak = [0], [0]
        completed = []
        def call():
            with mutex:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            barrier.wait(timeout=3)
            with mutex:
                active[0] -= 1
            return True
        pipeline = Pipeline({"shared": dict(concurrency=2, minimum_start_interval_seconds=0)})
        for _ in range(6):
            pipeline.submit("shared", call, completed.append)
        pipeline.run()
        self.assertEqual(peak[0], 2)
        self.assertEqual(completed, [True] * 6)

    def test_capability_preflight_is_reused_but_auth_rechecked(self):
        calls = []
        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            output = ('--safe-mode --tools --strict-mcp-config --disable-slash-commands --no-session-persistence' if '--help' in cmd else
                      'test-version' if '--version' in cmd else '{"authMethod":"claude.ai"}' if 'status' in cmd else
                      '{"type":"result","structured_output":{"fixture":true}}\n')
            return subprocess.CompletedProcess(cmd, 0, stdout=output, stderr="")
        with tempfile.TemporaryDirectory() as temp:
            config = dict(backend="claude-subscription", executable=sys.executable, model="synthetic", cli_version="test-version", reasoning_effort="high", timeout_seconds=60)
            env = dict(HOME=temp, PATH="/bin")
            preflight = backends.subscription(config, None, Path(temp), parent_env=env, run=fake_run, preflight_only=True)
            packet = dict(prompt="Only own evidence", input={}, schema={"type": "object"})
            backends.subscription(config, packet, Path(temp), parent_env=env, run=fake_run, preflight=preflight)
            self.assertEqual(sum('--help' in cmd for cmd in calls), 1)
            self.assertEqual(sum('--version' in cmd for cmd in calls), 1)
            self.assertEqual(sum('status' in cmd for cmd in calls), 2)
            broken = deepcopy(preflight)
            broken["executable_identity"][1] += 1
            with self.assertRaises(ValueError):
                backends.subscription(config, packet, Path(temp), parent_env=env, run=fake_run, preflight=broken)


if __name__ == "__main__":
    unittest.main()
