from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from decimal import Decimal
import tempfile
import unittest
from unittest.mock import patch

from openweb_news import caching, fixtures
from openweb_news.accounting import usage_cost
from openweb_news.common import digest, encoded, load
from openweb_news.ledger import Ledger
from openweb_news.preparation import prepare
from openweb_news.scheduling import Admission, AdmissionBlocked
from . import test_fresh


def cache_config():
    return dict(ttl_seconds=3600, expiry_tolerance_seconds=5, minimum_reuses=3,
                minimum_input_tokens=4096, estimated_system_tokens={},
                cached_input_per_million_usd="0.4", storage_per_million_token_hour_usd="4.5",
                complete_create_fallback_statuses=[400], expiry_fallback=True, cleanup_after_drain=True)


class CacheTests(unittest.TestCase):
    def test_named_depleted_credit_cache_retry_is_once_and_keeps_failed_bytes(self):
        for fail_again in (False, True):
            with self.subTest(fail_again=fail_again):
                self._credit_cache_retry(fail_again)

    def _credit_cache_retry(self, fail_again):
        from openweb_news.common import records
        from openweb_news.interruption import KEY, CREDIT_PROFILE, credit_rejection
        with tempfile.TemporaryDirectory() as tmp:
            raw = encoded(dict(error=dict(code=402, status='RESOURCE_EXHAUSTED',
                message='Your prepayment credits are depleted. Synthetic billing fixture.')))
            rejected = dict(complete=True, dispatch_state='completed', transport_success=False,
                http_status=402, raw_text=raw.decode(), retained_sha256=digest(raw))
            with Ledger(tmp, {}).locked() as ledger:
                cache, wire, key = self.setup_cache(ledger, lambda w,f: rejected)
                with self.assertRaises(AdmissionBlocked): cache.wire(wire)
            from pathlib import Path
            root = Path(tmp); end = list(records(root/'attempts.jsonl'))[-1]
            request = load(root/'attempts'/end['attempt_id']/'request.json')
            self.assertTrue(credit_rejection(request, rejected))
            for change in ({'http_status':403}, {'complete':False}, {'transport_success':True},
                    {'http_status':429}, {'dispatch_state':'uncertain'}, {'retained_sha256':'0'*64}, {'verdict':{}}):
                self.assertFalse(credit_rejection(request, dict(rejected, **change)))
            self.assertFalse(credit_rejection(dict(request,kind='model'),rejected))
            for error in (dict(code=402,status='OTHER',message='Your prepayment credits are depleted.'),
                    dict(code=402,status='RESOURCE_EXHAUSTED',message='Other billing problem')):
                different=encoded(dict(error=error))
                self.assertFalse(credit_rejection(request,dict(rejected,raw_text=different.decode(),retained_sha256=digest(different))))
            selected = [dict(task_id=end['task_id'], prior_attempt_id=end['attempt_id'],
                request_sha256=end['request_sha256'], prior_result_sha256=end['result_sha256'])]
            calls = []
            with Ledger(tmp, {}).locked() as ledger:
                ordinary,wire,_=self.setup_cache(ledger,lambda w,f:calls.append(w) or self.native())
                with self.assertRaises(AdmissionBlocked):ordinary.wire(wire)
                self.assertEqual(calls,[])  # Restored billing is never an automatic retry.
            with Ledger(tmp, {}, interrupted_attempts=selected).locked() as ledger:
                cache, wire, key = self.setup_cache(ledger, lambda w,f: calls.append(w) or (rejected if fail_again else self.native()))
                cache.package['configuration']['execution_policy']['max_attempts_per_task']=3
                cache.package['configuration']['execution_policy']['retry_policy'].update(max_total=1,max_model=1)
                cache.package['configuration'][KEY]={'profile':CREDIT_PROFILE}
                with patch('openweb_news.interruption.attempts', return_value=selected):
                    from openweb_news.scheduling import Pipeline
                    from openweb_news.recovery import sibling_rule
                    policy=cache.package['configuration']['execution_policy']
                    accounts=dict(judge=dict(concurrency=1,minimum_start_interval_seconds=0))
                    policy['scheduling']=dict(accounts=accounts,recovery=dict(judge=sibling_rule('judge',1)))
                    admission=Admission(ledger,policy,package=cache.package)
                    pipeline=Pipeline(accounts,recovery=policy['scheduling']['recovery'])
                    admission.restore_pacing(ledger,pipeline,dict(pro='judge'))
                    self.assertFalse(pipeline.recovery.capacity('judge','pro',100)['stopped'])
                    old_total=admission.total;cache.admission=admission
                    for _ in range(2):
                        if fail_again:
                            with self.assertRaises(AdmissionBlocked): cache.wire(wire)
                        else: cache.wire(wire)
                    self.assertEqual(Admission(ledger,policy,package=cache.package).retries['model'],1)
                self.assertEqual(len(calls),1)
                self.assertGreater(admission.total,old_total)
            self.assertEqual(load(root/end['result_path']), rejected)
            self.assertEqual(len([e for e in records(root/'attempts.jsonl') if e['event']=='started']),2)

    def semantic(self):
        return dict(method="POST", endpoint="https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-pro-preview:generateContent",
                    body=dict(systemInstruction=dict(parts=[dict(text="A" * 18000), dict(text="second instruction")]),
                              contents=[dict(parts=[dict(text="different native item")])], generationConfig=dict(responseSchema={"exact": True}, temperature=1)))

    def setup_cache(self, ledger, transport, clock=lambda: 100):
        wire = self.semantic()
        group = caching.payload(wire)
        key = digest(group)
        config = cache_config()
        config["estimated_system_tokens"][key] = 4500
        policy = dict(spending_ceiling_usd="10", budget_accounting=caching.PROFILE,
                      retry_policy=dict(max_total=0, max_search=0, max_model=0))
        package = dict(primary=test_fresh.UsageTests().package()["primary"],
                       configuration=dict(system_cache=config, execution_policy=policy),
                       cache_plan=[dict(group_id=key, payload=group, eligible=True)])
        admission = Admission(ledger, policy, package=package)
        return caching.SystemCache(package, ledger, admission, transport, clock=clock), wire, key

    def native(self, **changes):
        value = dict(name="cachedContents/test_1", model="models/gemini-3.1-pro-preview",
                     createTime="1970-01-01T00:00:00+00:00", expireTime="1970-01-01T01:00:00+00:00",
                     usageMetadata=dict(totalTokenCount=4500))
        value.update(changes)
        raw = encoded(value)
        return dict(complete=True, dispatch_state="completed", http_status=200,
                    transport_success=True, raw_text=raw.decode(), retained_sha256=digest(raw))

    def test_concurrent_creation_once_full_effective_wire_and_cleanup_resume(self):
        with tempfile.TemporaryDirectory() as tmp, Ledger(tmp, {}).locked() as ledger:
            calls = []
            def transport(wire, folder):
                calls.append(deepcopy(wire))
                return self.native() if wire["method"] == "POST" else dict(complete=True, transport_success=True, dispatch_state="completed", http_status=204)
            cache, wire, key = self.setup_cache(ledger, transport)
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda _: cache.wire(wire), range(24)))
            self.assertEqual(len(calls), 1)
            restored = deepcopy(results[0])
            restored["body"].pop("cachedContent")
            restored["body"]["systemInstruction"] = wire["body"]["systemInstruction"]
            self.assertEqual(restored, wire)
            self.assertEqual(len(calls[0]["body"]["systemInstruction"]["parts"]), 2)
            maximum = cache.admission.total
            self.assertGreater(maximum, Decimal("0.1"))
            cache.cleanup()
            self.assertEqual([c["method"] for c in calls], ["POST", "DELETE"])
            again, _, _ = self.setup_cache(ledger, transport)
            again.wire(wire)
            again.cleanup()
            self.assertEqual(len(calls), 2)
            self.assertEqual(again.admission.total, maximum)  # No guessed deletion credit.

    def test_uncertain_creation_never_recreated_or_fallen_back(self):
        with tempfile.TemporaryDirectory() as tmp, Ledger(tmp, {}).locked() as ledger:
            calls = []
            cache, wire, _ = self.setup_cache(ledger, lambda w, f: calls.append(w) or dict(complete=False, dispatch_state="uncertain"))
            for _ in range(2):
                with self.assertRaises(AdmissionBlocked):
                    cache.wire(wire)
            cache.cleanup()
            self.assertEqual(len(calls), 1)
            self.assertEqual(cache.observations["cleanup"]["state"], "held_after_uncertainty")

    def test_expiry_fallback_and_completed_create_rejection_do_not_renew(self):
        for response, clock in ((self.native(), lambda: 4000),
                                (dict(complete=True, dispatch_state="completed", http_status=400), lambda: 100)):
            with tempfile.TemporaryDirectory() as tmp, Ledger(tmp, {}).locked() as ledger:
                calls = []
                cache, wire, _ = self.setup_cache(ledger, lambda w, f: calls.append(w) or response, clock)
                self.assertEqual(cache.wire(wire), wire)
                self.assertEqual(cache.wire(wire), wire)
                self.assertEqual(len(calls), 1)

    def test_expiry_leases_renew_once_across_threads_and_resume_without_repeating_unknown(self):
        from datetime import datetime, timezone
        def stamp(t): return datetime.fromtimestamp(t, timezone.utc).isoformat()
        with tempfile.TemporaryDirectory() as tmp, Ledger(tmp, {}).locked() as ledger:
            now = [100]
            calls = []
            def transport(wire, folder):
                calls.append(wire)
                if len(calls) == 3:
                    return dict(complete=False, dispatch_state="uncertain")
                return self.native(createTime=stamp(now[0]), expireTime=stamp(now[0]+3600), name="cachedContents/lease_"+str(len(calls)))
            cache, wire, key = self.setup_cache(ledger, transport, lambda:now[0])
            cache.config["renewal"] = dict(profile="expiry-leases-v1", epoch="a"*64)
            first = cache.wire(wire)
            now[0] = 3800
            with ThreadPoolExecutor(max_workers=8) as pool:
                renewed = list(pool.map(lambda _:cache.wire(wire),range(24)))
            self.assertEqual(len(calls),2)
            self.assertNotEqual(first,renewed[0])
            self.assertEqual(len({digest(r)for r in renewed}),1)
            again, _, _ = self.setup_cache(ledger, transport, lambda:now[0])
            again.config["renewal"] = dict(cache.config["renewal"])
            self.assertEqual(again.wire(wire),renewed[0]);self.assertEqual(len(calls),2)
            now[0]=7500
            for _ in range(2):
                with self.assertRaises(AdmissionBlocked):again.wire(wire)
            self.assertEqual(len(calls),3)
            resumed, _, _ = self.setup_cache(ledger, transport, lambda:now[0])
            resumed.config["renewal"] = dict(cache.config["renewal"])
            with self.assertRaises(AdmissionBlocked):resumed.wire(wire)
            self.assertEqual(len(calls),3)

    def test_invalid_resource_and_mismatched_full_system_are_rejected(self):
        for changes in (dict(model="models/other"), dict(name="https://private.invalid/secret"),
                        dict(expireTime="1970-01-02T00:00:00+00:00"), dict(usageMetadata={})):
            with tempfile.TemporaryDirectory() as tmp, Ledger(tmp, {}).locked() as ledger:
                cache, wire, key = self.setup_cache(ledger, lambda w, f: self.native(**changes))
                with self.assertRaises(AdmissionBlocked):
                    cache.wire(wire)
        wire = self.semantic()
        with self.assertRaises(ValueError):
            caching.cache_wire(wire, dict(group_id="bad", name="cachedContents/test"), caching.payload(wire))

    def test_cached_usage_discount_requires_bound_profile_and_native_validity(self):
        package = test_fresh.UsageTests().package()
        package["configuration"] = dict(execution_policy=dict(budget_accounting=caching.PROFILE), system_cache=cache_config())
        request = dict(kind="model", backend="gemini-api")
        self.assertEqual(usage_cost(request, test_fresh.UsageTests().native(), package), Decimal("0.00067"))
        self.assertIsNone(usage_cost(request, test_fresh.UsageTests().native(cachedContentTokenCount=101), package))

    def test_preparation_binds_all_system_groups_and_financial_eligibility(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = fixtures.config(tmp)
            baseline = prepare(config, mode="fixture")
            cache = cache_config()
            # Use the same compile primitive as plan; provider is irrelevant to full system.
            from openweb_news.judging import build_pro_request, compile_packet, packet_for
            empty = dict(status="native_result", title=dict(state="missing"), content=dict(state="missing"), url=dict(state="missing"), metadata={}, additional_content={}, native_dates=dict(observations=[]))
            for question in baseline["questions"]:
                provider = baseline["providers"][0]
                wire = build_pro_request(compile_packet(packet_for(question, provider, empty, "cache-plan"), "2000-01-01T00:00:00Z", config=provider))
                cache["estimated_system_tokens"][digest(caching.payload(wire))] = 6000
            config["system_cache"] = cache
            config["execution_policy"]["budget_accounting"] = caching.PROFILE
            prepared = prepare(config, mode="fixture")
            self.assertTrue(all(row["eligible"] for row in prepared["cache_plan"]))
            cache["estimated_system_tokens"]["not-a-selected-system"] = 6000
            with self.assertRaises(ValueError):
                prepare(config, mode="fixture")

    def test_live_wiring_with_only_fake_transports_caches_planned_calls_and_resumes(self):
        from datetime import datetime, timedelta, timezone
        from pathlib import Path
        from types import SimpleNamespace
        import json
        from openweb_news import judging
        from openweb_news.runner import run
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            config = fixtures.config(root)
            config["execution_policy"].update(spending_ceiling_usd="150", budget_accounting=caching.PROFILE)
            config["system_cache"] = cache_config()
            # Discover complete systems without enabling an estimated group yet.
            baseline = prepare(fixtures.config(root), mode="fixture")
            empty = dict(status="native_result", title=dict(state="missing"), content=dict(state="missing"), url=dict(state="missing"), metadata={}, additional_content={}, native_dates=dict(observations=[]))
            for question in baseline["questions"]:
                provider = baseline["providers"][0]
                wire = judging.build_pro_request(judging.compile_packet(judging.packet_for(question, provider, empty, "cache-plan"), "2000-01-01T00:00:00Z", config=provider))
                config["system_cache"]["estimated_system_tokens"][digest(caching.payload(wire))] = 6000
            package = prepare(config, mode="live")
            approval = dict(package_sha256=digest(package), decision="approved", approved_by="synthetic-test", approved_at="2026-11-01")
            calls = []
            def compiled(case):
                return load(root / "compiled" / (case + ".json"))
            class Runtime:
                client = SimpleNamespace(quota_evidence=[])
                def __init__(self, *args, **kwargs): pass
                def __enter__(self): return self
                def __exit__(self, *args): pass
                def review(self, blind, folder):
                    return fixtures.model("codex", compiled(blind["input"]["case_id"]), folder)
            def subscription(settings, blind, folder, *, preflight_only=False, **kwargs):
                if preflight_only: return dict(transport_success=True)
                return fixtures.model("claude", compiled(blind["input"]["case_id"]), folder)
            def transport(wire, auth, credentials, folder, **kwargs):
                calls.append(wire)
                if wire["method"] == "DELETE":
                    return dict(complete=True, transport_success=True, http_status=204, dispatch_state="completed")
                if wire["endpoint"] == caching.ENDPOINT:
                    now = datetime.now(timezone.utc)
                    raw = encoded(dict(name="cachedContents/test_" + str(len(calls)), model=wire["body"]["model"],
                        createTime=now.isoformat(), expireTime=(now + timedelta(seconds=3600)).isoformat(), usageMetadata=dict(totalTokenCount=6000)))
                    return dict(raw_text=raw.decode(), retained_sha256=digest(raw), complete=True, transport_success=True, http_status=200, dispatch_state="completed")
                if ":generateContent" in wire["endpoint"]:
                    case = next(v["text"] for v in wire["body"]["contents"][0]["parts"] if "text" in v)
                    item = compiled(json.loads(case)["case_id"])
                    response = fixtures.model("pro", item, folder)
                    if response.get("http_status") == 200:
                        native = json.loads(response["raw_text"])
                        native["usageMetadata"] = dict(promptTokenCount=10000, totalTokenCount=10100, candidatesTokenCount=100, cachedContentTokenCount=6000)
                        raw = encoded(native)
                        response.update(raw_text=raw.decode(), retained_sha256=digest(raw), dispatch_state="completed")
                    return response
                provider = next(p for p in package["providers"] if p["configuration_id"] == wire["configuration_id"])
                return fixtures.search(wire, provider, folder)
            with patch("openweb_news.runner.https", side_effect=transport), patch("openweb_news.codex_runtime.CodexRuntime", Runtime), patch("openweb_news.backends.subscription", side_effect=subscription), patch("openweb_news.transport.Credentials.get", return_value="synthetic-credential"):
                report = run(package, approval=approval)
                generation_count = sum(":generateContent" in wire["endpoint"] for wire in calls)
                self.assertGreater(generation_count, 0)
                self.assertTrue(all("cachedContent" in wire["body"] for wire in calls if ":generateContent" in wire["endpoint"]))
                self.assertEqual(sum(w["endpoint"] == caching.ENDPOINT for w in calls), len(package["cache_plan"]))
                self.assertGreater(report["execution"]["primary_native_cached_input_tokens"], 0)
                before = len(calls)
                run(package, approval=approval)
                self.assertEqual(len(calls), before)
