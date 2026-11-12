from collections import Counter
from pathlib import Path
import tempfile
import time
import unittest

from openweb_news.concurrency import Trial, quota_signal, validate
from openweb_news.recovery import sibling_rule
from openweb_news.scheduling import Pipeline
from openweb_news.scheduling import Admission
from openweb_news.ledger import Ledger
from openweb_news.common import digest


def configuration():
    return dict(stages=[dict(concurrency=4, rank_end_inclusive=3), dict(concurrency=8, rank_end_inclusive=6),
                        dict(concurrency=16, rank_end_inclusive=10)], advance_health_successes=32,
                on_throttle="end-escalation-and-use-last-completed-stage")


class ConcurrencyTests(unittest.TestCase):
    def test_remaining_cohorts_use_new_work_only_and_replay_their_own_measurements(self):
        pipeline, _ = self.pipeline()
        config = configuration()
        historical = 'occurrence-' + digest('historical')
        case_ids = ['occurrence-' + digest(str(i)) for i in range(105)]
        config.update(historical_cases=[historical], remaining_case_cohorts={case: i//35 for i, case in enumerate(case_ids)})
        trial = Trial(config, 1)
        pipeline.trial = trial
        pipeline.sync = lambda: trial.advance(pipeline.recovery)
        trial.register(historical, dict(question_id='old', configuration_id='old', rank=1))
        old_measurement = dict(stage=0, started_at=1, finished_at=2, active_at_start=4, question_id='old', configuration_id='old')
        trial.observe(historical, dict(concurrency_measurement=old_measurement), 'valid')
        calls = []
        for case in case_ids:
            trial.register(case, dict(question_id='q', configuration_id='p', rank=1))
            def call(case=case):
                calls.append(case)
                measurement = trial.started(case)
                time.sleep(.008)
                return dict(transport_success=True, complete=True, dispatch_state='completed', concurrency_measurement=trial.finished(measurement))
            def complete(raw, case=case):
                pipeline.observe(case, 'codex-subscription', 'codex', raw, time.time(), case)
                trial.observe(case, raw, 'invalid')
            pipeline.submit('codex-subscription', call, complete, scope='codex', case=case)
        trial.collected('only-search')
        pipeline.run()
        report = trial.report()
        self.assertEqual(len(set(calls)), 105)
        self.assertEqual([s['measured_tasks'] for s in report['stages']], [35,35,35])
        self.assertEqual([s['observed_peak'] for s in report['stages']], [4,8,16])
        self.assertNotIn(historical, report['measurements'])
        self.assertTrue(all(s['validation_counts']=={'invalid':35} for s in report['stages']))
        replay = Trial(config, 1)
        for case, metadata in trial.cases.items():
            replay.register(case, {k:v for k,v in metadata.items() if k!='cohort'})
            if case in report['measurements']:
                row=report['measurements'][case]
                replay.observe(case, dict(concurrency_measurement={k:v for k,v in row.items() if k not in {'validation_state','dispatch_state','transport_success','elapsed_seconds'}}), row['validation_state'])
        self.assertEqual([s['measured_tasks'] for s in replay.report()['stages']], [35,35,35])

    def pipeline(self):
        rule = sibling_rule("judge", 4)
        rule["maximum_concurrency"] = 16
        pipeline = Pipeline({"codex-subscription": dict(concurrency=4, minimum_start_interval_seconds=0)},
                            recovery={"codex-subscription": rule})
        trial = Trial(configuration(), 1)
        pipeline.trial = trial
        pipeline.sync = lambda: trial.advance(pipeline.recovery)
        return pipeline, trial

    def test_native_quota_evidence_healthy_sparse_updates_and_internal_retry(self):
        for limits in ({}, dict(primary=dict(usedPercent=64)), dict(rateLimitReachedType=None, spendControlReached=None),
                       dict(credits=dict(hasCredits=False, unlimited=False))):
            self.assertIsNone(quota_signal(dict(method="account/rateLimits/updated", params=dict(rateLimits=limits))))
        for limits in (dict(primary=dict(usedPercent=100)), dict(rateLimitReachedType="rate_limit_reached"), dict(spendControlReached=True)):
            self.assertIsNotNone(quota_signal(dict(method="account/rateLimits/updated", params=dict(rateLimits=limits))))
        for info in ("usageLimitExceeded", "rateLimitExceeded", dict(responseStreamDisconnected=dict(httpStatusCode=429))):
            self.assertIsNotNone(quota_signal(dict(method="error", params=dict(error=dict(codexErrorInfo=info), willRetry=True))))
        self.assertIsNone(quota_signal(dict(method="error", params=dict(error=dict(message="a rate limit is mentioned in prose")))))

    def test_stages_use_once_only_balanced_work_and_reach_real_pool_capacities(self):
        pipeline, trial = self.pipeline()
        dispatched, results = [], []
        # Every stratum receives all three questions and five providers.
        for q in range(3):
            for provider in range(5):
                for rank in range(1, 11):
                    case = f"q{q}-p{provider}-r{rank}"
                    trial.register(case, dict(question_id=str(q), configuration_id=str(provider), rank=rank))
                    def call(case=case):
                        measurement = trial.started(case)
                        dispatched.append(case)
                        time.sleep(0.008)
                        return dict(transport_success=True, complete=True, dispatch_state="completed",
                                    concurrency_measurement=trial.finished(measurement))
                    def complete(raw, case=case):
                        pipeline.observe(case, "codex-subscription", "codex", raw, time.time(), case)
                        trial.observe(case, raw, "valid")
                        results.append(case)
                    pipeline.submit("codex-subscription", call, complete, scope="codex", case=case)
        trial.collected("only-search")
        pipeline.run()
        report = trial.report()
        self.assertEqual(len(dispatched), len(set(dispatched)))
        self.assertEqual(len(results), 150)
        self.assertEqual([s["measured_tasks"] for s in report["stages"]], [45, 45, 60])
        self.assertEqual([s["observed_peak"] for s in report["stages"]], [4, 8, 16])
        self.assertTrue(all(len(s["question_mix"]) == 3 and len(s["configuration_mix"]) == 5 for s in report["stages"]))

    def test_throttle_sticky_backoff_and_replay_do_not_reenter_higher_stage(self):
        pipeline, trial = self.pipeline()
        trial.current = 2
        trial.verified = 1
        trial.register("case", dict(question_id="q", configuration_id="p", rank=10))
        trial.observe_quota([dict(reason="native_http_429")])
        self.assertEqual(trial.capacity("codex-subscription", "case"), dict(limit=8, eligible=True))
        trial.observe_quota([])  # Healthy sparse updates do not clear it.
        trial.advance(pipeline.recovery)
        self.assertEqual(trial.capacity("codex-subscription", "case")["limit"], 8)
        trial.current = 0  # Fresh controller must replay durable quota evidence.
        trial.verified = 0
        trial.observe_quota([dict(reason="native_http_429")])
        self.assertEqual(trial.capacity("codex-subscription", "case")["limit"], 4)

    def test_failed_or_empty_cohort_finishes_without_deadlocking_future_work(self):
        pipeline, trial = self.pipeline()
        trial.collected("only-search")
        trial.register("late", dict(question_id="q", configuration_id="p", rank=10))
        done = []
        pipeline.submit("codex-subscription", lambda: {}, lambda raw: done.append(True), scope="codex", case="late")
        pipeline.run()
        self.assertEqual(done, [True])
        self.assertIsNotNone(trial.ended)

    def test_native_internal_retry_errors_reach_source_recovery_and_replay_once(self):
        with tempfile.TemporaryDirectory() as tmp, Ledger(tmp, {}).locked() as ledger:
            pipeline, trial = self.pipeline()
            policy = dict(spending_ceiling_usd="1", retry_policy=dict(max_total=0, max_search=0, max_model=0))
            admission = Admission(ledger, policy)
            evidence = []
            for index in range(3):
                evidence.append(dict(event_id=str(index), observed_at=100 + index, reason="native_http_429", thread_id="thread"))
                admission.restore_pacing(ledger, pipeline, {}, evidence)
                # The eventual valid answer must not erase internal retry failures.
                pipeline.recovery.observe("case" + str(index), "codex-subscription", "codex",
                    dict(transport_success=True, complete=True, dispatch_state="completed", native_quota_evidence=[evidence[-1]]), 100 + index, "valid")
            state = pipeline.recovery.scope("codex-subscription")
            self.assertEqual((state["mode"], state["pauses"], state["cooldown_until"]), ("cooldown", 1, 162))
            admission.restore_pacing(ledger, pipeline, {}, evidence)
            self.assertEqual(state["pauses"], 1)
            replay, _ = self.pipeline()
            Admission(ledger, policy).restore_pacing(ledger, replay, {}, evidence)
            self.assertEqual(replay.recovery.describe(), pipeline.recovery.describe())
            response = dict(transport_success=False, complete=False, dispatch_state="uncertain", native_quota_evidence=[evidence[-1]])
            fresh_pipeline, _ = self.pipeline()
            fresh_pipeline.recovery.observe("uncertain", "codex-subscription", "codex", response, 200, "unjudged")
            self.assertEqual(fresh_pipeline.recovery.scope("codex-subscription")["mode"], "cooldown")
            invalid_pipeline, _ = self.pipeline()
            for index in range(3):
                invalid_pipeline.recovery.observe(str(index), "codex-subscription", "codex",
                    dict(complete=True, transport_success=True, dispatch_state="completed", native_quota_evidence=[evidence[-1]]), 200 + index, "invalid")
            self.assertEqual(invalid_pipeline.recovery.scope("codex-subscription")["mode"], "cooldown")
