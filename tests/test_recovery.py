from copy import deepcopy
from collections import Counter
from decimal import Decimal
import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from openweb_news import fixtures
from openweb_news.common import digest, load, records, write_private
from openweb_news.ledger import Ledger
from openweb_news.preparation import prepare
from openweb_news.recovery import Recovery, sibling_rule
from openweb_news.runner import run
from openweb_news.reuse import freeze
from openweb_news.scheduling import Admission, Pipeline, task_ready_at
from openweb_news.scheduling import AdmissionBlocked


class RecoveryTests(unittest.TestCase):
    def test_replay_advances_cooldown_before_a_late_inflight_completion(self):
        from datetime import datetime, timezone
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            request = dict(kind='model', role='pro', reservation_usd='0.1', backend='gemini-api')
            events = []
            def stamp(t):
                return datetime.fromtimestamp(t, timezone.utc).isoformat()
            for task, aid, t in [('one', 'a', 100), ('two', 'b', 105)]:
                folder = root / 'attempts' / aid
                write_private(folder / 'request.json', request)
                events.append(dict(event='started', task_id=task, attempt_id=aid,
                                   recorded_at=stamp(t), request_sha256=digest(request)))
            for task, aid, t in [('one', 'a', 110), ('two', 'b', 200)]:
                response = dict(complete=False, transport_success=False, dispatch_state='uncertain')
                path = root / 'attempts' / aid / 'result.json'
                write_private(path, response)
                events.append(dict(event='finished', task_id=task, attempt_id=aid,
                                   recorded_at=stamp(t), request_sha256=digest(request),
                                   result_path=str(path.relative_to(root)), result_sha256=digest(response)))
            from openweb_news.common import encoded
            write_private(root / 'attempts.jsonl', b''.join(encoded(e) for e in events))
            ledger = Ledger(root, {})
            ledger.reload()
            accounts = dict(judge=dict(concurrency=4, minimum_start_interval_seconds=0))
            policy = dict(spending_ceiling_usd='1', scheduling=dict(accounts=accounts, recovery=dict(judge=sibling_rule('judge', 4))))
            admission = Admission(ledger, policy)
            pipeline = Pipeline(accounts, recovery=policy['scheduling']['recovery'])
            admission.restore_pacing(ledger, pipeline, dict(pro='judge'))
            self.assertEqual(pipeline.recovery.scope('judge')['mode'], 'stopped')
            self.assertEqual(pipeline.recovery.scope('judge')['pauses'], 2)

    def response(self, status=200, uncertain=False):
        return dict(http_status=status, complete=not uncertain, transport_success=status == 200 and not uncertain,
                    dispatch_state="uncertain" if uncertain else "completed")

    def test_provider_errors_pause_only_affected_configuration_and_auth_stops_account(self):
        r = Recovery(dict(provider=sibling_rule("provider", 2)))
        for i in range(3):
            r.observe(str(i), "provider", "news", self.response(503), 100+i)
        self.assertEqual(r.capacity("provider", "news", 103)["ready_at"], 162)
        self.assertEqual(r.capacity("provider", "web", 103)["ready_at"], 0)
        self.assertEqual(r.capacity("provider", "news", 162)["configuration_limit"], 1)
        for i in range(3):
            r.observe("good"+str(i), "provider", "news", self.response(), 163+i)
        self.assertEqual(r.capacity("provider", "news", 166)["configuration_limit"], 2)
        r.observe("denied", "provider", "news", self.response(403), 170)
        self.assertTrue(r.capacity("provider", "web", 171)["stopped"])

    def test_judge_uncertainty_pauses_account_and_second_pause_stops_it(self):
        r = Recovery(dict(judge=sibling_rule("judge", 4)))
        r.observe("one", "judge", "pro", self.response(None, True), 100)
        self.assertEqual(r.capacity("judge", "other", 101)["ready_at"], 160)
        # Late successes cannot clear a recorded cooldown.
        for i in range(8):
            r.observe("late"+str(i), "judge", "pro", self.response(), 110+i)
        self.assertEqual(r.scope("judge")["mode"], "cooldown")
        self.assertEqual(r.capacity("judge", "other", 160)["account_limit"], 2)
        for i in range(8):
            r.observe("recovered"+str(i),"judge","other",self.response(),161+i)
        self.assertEqual(r.scope("judge")["mode"],"normal")
        self.assertEqual(r.scope("judge")["pauses"],1)
        r.observe("two", "judge", "other", self.response(None, True), 170)
        self.assertTrue(r.capacity("judge", "pro", 171)["stopped"])

    def test_invalid_judge_outputs_count_as_account_errors(self):
        r = Recovery(dict(judge=sibling_rule('judge',4)))
        for i in range(3):
            r.observe(str(i),'judge','codex',self.response(),100+i,'invalid')
        self.assertEqual(r.capacity('judge','other',103)['ready_at'],162)

    def native_schema_failure(self):
        from openweb_news.judging import METHOD
        envelope = dict(modelVersion=METHOD["primary"]["model"],
            candidates=[dict(finishReason="STOP", content=dict(parts=[dict(text='{"invalid":"schema"}')]))])
        raw = json.dumps(envelope)
        return dict(self.response(), raw_text=raw, retained_sha256=digest(raw.encode()))

    def test_source_completed_gemini_schema_failures_do_not_pause_transport(self):
        r = Recovery(dict(judge=sibling_rule("judge", 4)))
        for i in range(3):
            r.observe(str(i), "judge", "pro", self.native_schema_failure(), 100+i, "invalid")
        self.assertEqual(r.scope("judge")["mode"], "normal")
        self.assertEqual(r.scope("judge")["pauses"], 0)
        self.assertEqual(r.scope("judge")["successes"], 3)

    def test_source_health_exception_rejects_blocks_bad_finish_model_and_subscriptions(self):
        from openweb_news.recovery import completed_expected_native_output
        good = self.native_schema_failure()
        self.assertTrue(completed_expected_native_output("pro", good, "invalid"))
        for scope in ("codex", "claude", "search/brave-news"):
            self.assertFalse(completed_expected_native_output(scope, good, "invalid"))
        mutations = [
            dict(good, complete=False), dict(good, dispatch_state="uncertain"),
            dict(good, http_status=429), dict(good, transport_success=False),
            dict(good, error_type="ConnectionError"), dict(good, fatal_configuration=True),
            dict(good, retained_sha256="tampered"), dict(good, credential_echo_redacted=True),
        ]
        for change in ("model", "block", "finish", "safety", "thought", "empty", "nontext", "extra"):
            envelope = json.loads(good["raw_text"])
            candidate = envelope["candidates"][0]
            if change == "model":
                envelope["modelVersion"] = "different-model"
            elif change == "block":
                envelope["promptFeedback"] = dict(blockReason="SAFETY")
            elif change == "finish":
                candidate["finishReason"] = "MAX_TOKENS"
            elif change == "safety":
                candidate["safetyRatings"] = [dict(blocked=True)]
            elif change == "thought":
                candidate["content"]["parts"][0]["thought"] = True
            elif change == "empty":
                candidate["content"]["parts"][0]["text"] = ""
            elif change == "nontext":
                candidate["content"]["parts"] = [dict(functionCall={})]
            else:
                envelope["candidates"].append(deepcopy(candidate))
            raw = json.dumps(envelope)
            mutations.append(dict(good, raw_text=raw, retained_sha256=digest(raw.encode())))
        for raw in mutations:
            self.assertFalse(completed_expected_native_output("pro", raw, "invalid"), raw)

    def test_interleaved_error_window_and_success_ramp_match_sibling(self):
        rule = sibling_rule("judge", 4)
        rule.update(ramp_enabled=True, maximum_concurrency=8)
        r = Recovery(dict(judge=rule))
        for i in range(32):
            r.observe(str(i), "judge", "pro", self.response(), i)
        self.assertEqual(r.capacity("judge", "pro", 33)["account_limit"], 8)
        for i in range(10):
            r.observe("mixed"+str(i), "judge", "pro", self.response(503 if i%2 == 0 else 200), 40+i)
        self.assertEqual(r.scope("judge")["mode"], "cooldown")

    def test_cooldown_does_not_hold_another_configuration_or_account(self):
        clock, completed, waits = [100.0], [], []
        def sleep(delay):
            waits.append(delay)
            clock[0] += delay
        rules = dict(provider=dict(concurrency=2, minimum_start_interval_seconds=0), other=dict(concurrency=1, minimum_start_interval_seconds=0))
        p = Pipeline(rules, clock=lambda:clock[0], sleeper=sleep,
                     recovery={k:sibling_rule("provider", v["concurrency"]) for k,v in rules.items()})
        for i in range(3):
            p.observe(str(i), "provider", "news", self.response(503), 100, "synthetic")
        p.submit("provider", lambda:"news", lambda x:completed.append((x, clock[0])), scope="news")
        p.submit("provider", lambda:"web", lambda x:completed.append((x, clock[0])), scope="web")
        p.submit("other", lambda:"other", lambda x:completed.append((x, clock[0])))
        p.run()
        self.assertEqual(dict(completed), dict(news=160, web=100, other=100))
        self.assertEqual(waits, [60])

    def config(self, root):
        cfg = fixtures.config(root/'run')
        policy = cfg["execution_policy"]
        policy["max_attempts_per_task"] = 3
        policy["retry_policy"] = dict(max_total=None, max_search=None, max_model=None,
            http_statuses=[429]+list(range(500,600)), backoff_seconds=[15,60])
        policy["scheduling"]["recovery"] = {k:sibling_rule("provider" if k.startswith("provider/") else "judge",v["concurrency"]) for k,v in policy["scheduling"]["accounts"].items()}
        return cfg

    def test_automatic_three_attempts_all_5xx_backoff_resume_and_no_global_allowance(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            cfg = self.config(root)
            package = prepare(cfg, mode="fixture")
            clock, sleeps, calls = [1000.0], [], {}
            original, real_pipeline = fixtures.model, Pipeline
            selected = []
            def model(role, compiled, folder):
                case = compiled["packet"]["case_id"]
                if role == "pro":
                    if not selected:
                        selected.append(case)
                    if case == selected[0]:
                        calls[case] = calls.get(case,0)+1
                        if calls[case] < 3:
                            return dict(self.response(599 if calls[case] == 2 else 503), raw_text="{}")
                return original(role, compiled, folder)
            def pipeline(rules, **kw):
                def sleep(delay):
                    sleeps.append(delay)
                    clock[0] += delay
                return real_pipeline(rules, clock=lambda:clock[0], sleeper=sleep, **kw)
            with patch("openweb_news.runner.Pipeline", side_effect=pipeline), patch("openweb_news.fixtures.model",side_effect=model), patch("openweb_news.ledger.now", side_effect=lambda:datetime.fromtimestamp(clock[0],timezone.utc).isoformat()):
                first = run(package, retry_failed=True)
                resumed = run(package, retry_failed=True)
            self.assertEqual(calls[selected[0]], 3)
            self.assertEqual(sleeps, [15,60])
            self.assertEqual(first["outcome_counts"], {"2":4,"1":2,"unknown":2})
            self.assertEqual(first["outcome_counts"], resumed["outcome_counts"])
            with Ledger(root/'run', dict(package_sha256=digest(package), mode="fixture")).locked() as ledger:
                admission = Admission(ledger, cfg['execution_policy'])
                replay = pipeline(cfg['execution_policy']['scheduling']['accounts'], recovery=cfg['execution_policy']['scheduling']['recovery'])
                admission.restore_pacing(ledger, replay, package['account_bindings'])
                before = deepcopy(replay.recovery.scopes)
                admission.restore_pacing(ledger, replay, package['account_bindings'])
                self.assertEqual(before, replay.recovery.scopes)
                self.assertEqual(admission.retries['model'], 2)
            self.assertTrue((root/'run/report.html').is_file())

    def test_priced_model_retry_keeps_exact_request_and_reservation_identity(self):
        """Fixture transports with real paid-mode accounting reach the live bug."""
        from openweb_news.accounting import reservation as priced_reservation
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            cfg = self.config(root)
            cfg['execution_policy']['spending_ceiling_usd'] = '150'
            cfg['execution_policy']['retry_policy']['backoff_seconds'] = [0, 0]
            for rule in cfg['execution_policy']['scheduling']['recovery'].values():
                rule['cooldown_seconds'] = 0
            package = prepare(cfg, mode='fixture')
            selected, attempts = [], Counter()
            original = fixtures.model
            def model(role, compiled, folder):
                case = compiled['packet']['case_id']
                if role == 'pro':
                    if not selected:
                        selected.append(case)
                    if case == selected[0]:
                        attempts[case] += 1
                        if attempts[case] < 3:
                            return dict(self.response(503), raw_text='{}')
                return original(role, compiled, folder)
            def price(request, package):
                return priced_reservation(request, dict(package, mode='live'))
            with patch('openweb_news.runner.reservation', side_effect=price), patch('openweb_news.fixtures.model', side_effect=model):
                report = run(package, retry_failed=True)
                resumed = run(package, retry_failed=True)
            self.assertEqual(attempts[selected[0]], 3)
            self.assertEqual(report['outcome_counts'], resumed['outcome_counts'])
            task = 'pro-' + selected[0]
            events = [e for e in records(root/'run/attempts.jsonl') if e['event']=='started' and e['task_id']==task]
            self.assertEqual(len(events), 3)
            requests = [load(root/'run/attempts'/e['attempt_id']/'request.json') for e in events]
            self.assertTrue(all(request == requests[0] for request in requests))
            self.assertEqual(price(requests[0], package), Decimal(requests[0]['reservation_usd']))

    def test_stopped_scope_yields_partial_report_without_sending_queued_work(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            cfg = self.config(root)
            for rule in cfg['execution_policy']['scheduling']['recovery'].values():
                rule['initial_concurrency'] = rule['maximum_concurrency'] = 1
                rule['reduced_concurrency'] = 1
            for rule in cfg['execution_policy']['scheduling']['accounts'].values():
                rule['concurrency'] = 1
            calls = []
            def denied(request, provider, folder):
                calls.append(provider['configuration_id'])
                return dict(self.response(401),raw_text='{}',captured_at='2026-01-01T00:00:00Z')
            with patch('openweb_news.fixtures.search', side_effect=denied):
                package = prepare(cfg,mode='fixture')
                report = run(package,retry_failed=True)
                resumed = run(package,retry_failed=True)
            self.assertEqual(len(calls), 2)
            self.assertEqual(report['execution']['state'], 'completed_with_holds')
            self.assertEqual(len(report['execution']['held_tasks']), 6)
            self.assertEqual(resumed['execution']['recovery'],report['execution']['recovery'])
            self.assertTrue((root/'run/report.html').is_file())

    def test_no_global_retry_count_still_obeys_cumulative_spending_ceiling(self):
        with tempfile.TemporaryDirectory() as temp, Ledger(temp,{}).locked() as ledger:
            policy = dict(spending_ceiling_usd='1', retry_policy=dict(max_total=None,max_search=None,max_model=None))
            admission = Admission(ledger,policy)
            request = dict(kind='model',reservation_usd='0.4')
            call = lambda _:self.response(503)
            for _ in range(2):
                ledger.execute('task',request,call,max_attempts=3,retry_failed=True,retry_http_statuses=[503],before_start=admission.reserve)
            with self.assertRaises(AdmissionBlocked):
                ledger.execute('task',request,call,max_attempts=3,retry_failed=True,retry_http_statuses=[503],before_start=admission.reserve)
            self.assertEqual(str(admission.total),'0.8')
            self.assertEqual(len(ledger.events()),4)

    def test_automatic_retry_branch_never_resends_uncertain_model_call(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            cfg = self.config(root)
            clock, calls, chosen = [1000.0], [], []
            original, real_pipeline = fixtures.model, Pipeline
            def model(role, compiled, folder):
                case = compiled['packet']['case_id']
                if role == 'pro':
                    if not chosen:
                        chosen.append(case)
                    if case == chosen[0]:
                        calls.append(case)
                        return dict(self.response(None,True),raw_text='{"partial":')
                return original(role,compiled,folder)
            def pipeline(rules,**kw):
                return real_pipeline(rules,clock=lambda:clock[0],sleeper=lambda seconds:clock.__setitem__(0,clock[0]+seconds),**kw)
            package = prepare(cfg,mode='fixture')
            with patch('openweb_news.runner.Pipeline',side_effect=pipeline),patch('openweb_news.fixtures.model',side_effect=model),patch('openweb_news.ledger.now',side_effect=lambda:datetime.fromtimestamp(clock[0],timezone.utc).isoformat()):
                first = run(package,retry_failed=True)
                resumed = run(package,retry_failed=True)
            self.assertEqual(len(calls),1)
            self.assertEqual(first['execution']['state'],'completed_with_holds')
            self.assertEqual(len(resumed['execution']['held_tasks']),1)
            self.assertEqual(len(resumed['outcomes']),8)

    def test_continuation_reuses_health_history_without_compiled_source_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            cfg = self.config(root)
            source = prepare(cfg,mode='fixture')
            write_private(root/'source.json',source)
            first = run(source)
            cfg['run_directory'] = str(root/'continued')
            cfg['reuse_snapshot'] = freeze(root/'source.json',root/'snapshot')
            continued = prepare(cfg,mode='fixture')
            with patch('openweb_news.fixtures.model',side_effect=AssertionError('no repeated judge')),patch('openweb_news.fixtures.search',side_effect=AssertionError('no repeated search')):
                result = run(continued)
            self.assertEqual(result['outcome_counts'],first['outcome_counts'])
            self.assertEqual(result['execution']['recovery'],first['execution']['recovery'])
            self.assertEqual(result['execution']['reused_tasks'],first['execution']['scheduled_tasks'])

    def test_backoff_and_scope_policy_validation(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg = self.config(Path(temp).resolve())
            for bad in ([15], [15,float('nan')]):
                candidate = deepcopy(cfg)
                candidate['execution_policy']['retry_policy']['backoff_seconds'] = bad
                with self.assertRaises(ValueError):
                    prepare(candidate)
            candidate = deepcopy(cfg)
            candidate['execution_policy']['scheduling']['recovery']['codex-subscription']['initial_concurrency'] = 3
            with self.assertRaises(ValueError):
                prepare(candidate)
            state = dict(dispatch=True,attempt=3,previous=self.response(503),completed_at='1970-01-01T00:01:40Z')
            self.assertEqual(task_ready_at(state,cfg['execution_policy']),160)
