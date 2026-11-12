from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from openweb_news import backends, fixtures
from openweb_news.codex_runtime import CodexRuntime, StdioClient, GLOBAL_PASSIVE_NOTIFICATIONS, THREAD_PASSIVE_NOTIFICATIONS
from openweb_news.common import digest, encoded, load, private_directory, write_private
from openweb_news.preparation import prepare
from openweb_news.runner import run


class FakeProcess:
    """Real local pipes and JSON routing, with no subprocess or network access."""
    def __init__(self, command, *, barrier=None, verdict=None, fault=None, **kwargs):
        self.command, self.environment = command, kwargs["env"]
        self.barrier, self.verdict, self.fault = barrier, verdict, fault
        self.requests, self.threads, self.jobs = [], {}, []
        self.active, self.maximum, self.returncode = 0, 0, None
        self.lock, self.output_lock, self.stopped = threading.Lock(), threading.Lock(), threading.Event()
        self.received = threading.Event()
        r, w = os.pipe()
        self.stdin, self._input = os.fdopen(w, "w", encoding="utf-8"), os.fdopen(r, "r", encoding="utf-8")
        r, w = os.pipe()
        self.stdout, self._output = os.fdopen(r, "r", encoding="utf-8"), os.fdopen(w, "w", encoding="utf-8")
        self.worker = threading.Thread(target=self._serve, daemon=True)
        self.worker.start()

    def emit(self, value):
        with self.output_lock:
            if not self.stopped.is_set():
                self._output.write(encoded(value).decode())
                self._output.flush()

    def event(self, method, params):
        self.emit(dict(method=method, params=params))

    def _serve(self):
        try:
            for line in self._input:
                request = json.loads(line)
                self.requests.append(request)
                self.received.set()
                method, params = request.get("method"), request.get("params", {})
                if method == "initialize":
                    self.emit(dict(id=request["id"], result={}))
                    if self.fault and self.fault.startswith("global-notice:"):
                        notice=self.fault.split(":",1)[1]
                        self.event(notice,dict(authMode="chatgpt",rateLimits={},summary="Synthetic notice",message="Synthetic notice",status="disconnected",installationId="synthetic",serverName="synthetic"))
                elif method == "account/read":
                    self.emit(dict(id=request["id"], result=dict(account=dict(type="apiKey" if self.fault == "auth" else "chatgpt", email="SENSITIVE_ACCOUNT_METADATA_NOT_FOR_LOGS"))))
                elif method == "thread/start":
                    identifier = "thread-1" if self.fault == "duplicate" else "thread-" + str(len(self.threads)+1)
                    thread = dict(id=identifier, turns=[], ephemeral=True, forkedFromId=None, parentThreadId=None)
                    response = dict(thread=thread, model=params["model"], modelProvider="openai", reasoningEffort="medium",
                                    approvalPolicy="never", sandbox=dict(type="readOnly", networkAccess=False), instructionSources=[])
                    if self.fault == "context":
                        response["instructionSources"] = ["unapproved-project"]
                    self.threads[identifier] = params
                    self.event("thread/started", dict(thread=thread))
                    self.emit(dict(id=request["id"], result=response))
                elif method == "thread/unsubscribe":
                    if self.fault == "unsubscribe-error":
                        self.emit(dict(id=request["id"],error=dict(code=-32000,message="synthetic cleanup failure")))
                        continue
                    self.emit(dict(id=request["id"],result=dict(status="unsubscribed")))
                    self.event("thread/closed",dict(threadId=params["threadId"]))
                elif method == "turn/start":
                    thread_id, turn_id = params["threadId"], "turn-"+params["threadId"]
                    self.event("turn/started", dict(threadId=thread_id, turn=dict(id=turn_id)))
                    self.emit(dict(id=request["id"], result=dict(turn=dict(id=turn_id))))
                    job = threading.Thread(target=self._judge, args=(thread_id, turn_id, params), daemon=True)
                    self.jobs.append(job)
                    job.start()
                elif method == "turn/interrupt":
                    self.emit(dict(id=request["id"], result={}))
                    self.event("turn/completed", dict(threadId=params["threadId"], turn=dict(id=params["turnId"], status="interrupted")))
        finally:
            for job in self.jobs:
                job.join(timeout=4)
            self.returncode = 0
            self._input.close()
            self._output.close()

    def _judge(self, thread_id, turn_id, params):
        with self.lock:
            self.active += 1
            self.maximum = max(self.maximum, self.active)
        try:
            if self.barrier:
                try:
                    self.barrier.wait(timeout=3)
                except threading.BrokenBarrierError:
                    return
            if self.fault == "timeout-one" and thread_id == "thread-1":
                return
            if self.stopped.is_set():
                return
            base = dict(threadId=thread_id, turnId=turn_id)
            if self.fault and self.fault.startswith("thread-notice:"):
                notice=self.fault.split(":",1)[1]
                model="other-model" if notice=="unsafe-safety-model" else "synthetic-model"
                if notice=="unsafe-safety-model":notice="model/safetyBuffering/updated"
                provider="other-provider" if notice=="unsafe-auth-provider" else "openai"
                if notice=="unsafe-auth-provider":notice="modelProvider/authRecoveryCompleted"
                self.event(notice,dict(base,model=model,fasterModel=None,provider=provider,message="Synthetic notice",summary="Synthetic notice",metadata={},verifications=[],threadName="synthetic",delta="",summaryIndex=0,itemId="synthetic",showBufferingUi=False,reasons=[],useCases=[]))
            if self.fault == "interaction":
                self.emit(dict(id="interaction", method="item/tool/requestUserInput", params=base))
                return
            echoed = params["input"] if self.fault != "echo" else [dict(type="text", text='{"wrong_case":true}')]
            self.event("item/completed", dict(base, item=dict(type="userMessage", content=echoed)))
            if self.fault == "tool":
                self.event("item/started", dict(base, item=dict(type="commandExecution")))
                return
            answer = self.verdict(json.loads(params["input"][0]["text"])) if self.verdict else json.loads(params["input"][0]["text"])
            text = "invalid JSON" if self.fault == "json" else encoded(answer).decode()
            self.event("turn/moderationMetadata", dict(base, turnId="other-turn" if self.fault == "moderation-route" else turn_id, metadata={}))
            self.event("thread/tokenUsage/updated", dict(base, tokenUsage=dict(total=dict(inputTokens=10, outputTokens=5))))
            self.event("item/completed", dict(base, item=dict(type="agentMessage", phase="final_answer", text=text,
                       memoryCitation="unapproved memory" if self.fault == "memory" else None)))
            self.event("turn/completed", dict(threadId=thread_id, turn=dict(id=turn_id, status="completed")))
        finally:
            with self.lock:
                self.active -= 1

    def poll(self):
        return self.returncode

    def terminate(self):
        self.stopped.set()
        if self.barrier:
            self.barrier.abort()
        self.stdin.close()

    kill = terminate

    def wait(self, timeout=None):
        self.worker.join(timeout=timeout)
        if self.worker.is_alive():
            raise subprocess.TimeoutExpired(self.command, timeout)
        return self.returncode


class RuntimeTests(unittest.TestCase):
    def test_retired_mailboxes_do_not_accumulate_or_hide_late_tool_events(self):
        process=FakeProcess([],env={});client=StdioClient(process)
        try:
            for i in range(1000):
                tid="completed-"+str(i);client.register(tid);client.release(tid)
                process.event("thread/closed",dict(threadId=tid))
            client.request("initialize",{},time.monotonic()+5)
            self.assertEqual(len(client.queues),0)
            self.assertEqual(len(client.used_threads),1000)
            process.event("item/completed",dict(threadId="completed-0",item=dict(type="commandExecution")))
            client.reader.join(timeout=2)
            self.assertIsNotNone(client.failure)
        finally:client.close()

    def test_other_quota_bucket_cannot_exhaust_codex_or_borrow_its_reset(self):
        from openweb_news.quota import verified_window_event
        process=FakeProcess([],env={})
        client=StdioClient(process)
        try:
            def update(bucket,used):
                process.event("account/rateLimits/updated",dict(rateLimits=dict(limitId=bucket,primary=dict(usedPercent=used,resetsAt=time.time()+3600),secondary=None)))
            update("codex",10)
            update("unrelated",100)
            client.request("initialize",{},time.monotonic()+2)
            self.assertEqual(client.quota_status["primary"]["usedPercent"],10)
            self.assertEqual(client.quota_evidence,[])
            update("codex",100)
            client.request("initialize",{},time.monotonic()+2)
            self.assertEqual(len(client.quota_evidence),1)
            self.assertTrue(verified_window_event(client.quota_evidence[0]))
            process.event("error",dict(threadId="actual-turn",error=dict(codexErrorInfo="rateLimitExceeded")))
            client.request("initialize",{},time.monotonic()+2)
            self.assertEqual(len(client.quota_evidence),2)
            self.assertFalse(verified_window_event(client.quota_evidence[-1]))
        finally:client.close()

    def setup_runtime(self, root, *, fault=None, concurrency=2, timeout=5, verdict=None):
        auth = private_directory(root / "auth")
        write_private(auth / "auth.json", dict(synthetic="dummy-auth-only"))
        config = dict(backend="codex-subscription", transport="app-server-stdio-v1", model="synthetic-model",
                      reasoning_effort="medium", timeout_seconds=timeout)
        preflight = dict(transport_success=True, executable=sys.executable, configuration_sha256=digest(config),
                         executable_identity=backends.executable_identity(sys.executable), cli_version="synthetic-cli")
        processes = []
        barrier = threading.Barrier(concurrency) if concurrency > 1 else None
        def popen(command, **kwargs):
            process = FakeProcess(command, barrier=barrier, fault=fault, verdict=verdict, **kwargs)
            processes.append(process)
            return process
        runtime = CodexRuntime(config, root / "runtime", preflight=preflight,
                               parent_env=dict(HOME=str(root), CODEX_HOME=str(auth), PATH="/bin", OPENAI_API_KEY="forbidden"), popen=popen)
        return runtime, processes

    def packet(self, case):
        return dict(prompt="Only own case "+case, input=dict(case=case),
                    schema=dict(type="object", properties=dict(case=dict(type="string", enum=[case])), required=["case"], additionalProperties=False))

    def test_known_stopped_runtime_holds_unsent_turns_without_ledger_attempts(self):
        from types import SimpleNamespace
        from openweb_news.common import records
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();cfg=fixtures.config(root)
            cfg["execution_policy"]["spending_ceiling_usd"]="150"
            package=prepare(cfg,mode="live")
            class Runtime:
                client=SimpleNamespace(failure="synthetic cleanup stop",quota_evidence=[])
                def __init__(self,*args,**kwargs):pass
                def __enter__(self):return self
                def __exit__(self,*args):pass
                def review(self,*args):raise AssertionError("known stopped runtime dispatched")
            def compiled(blind):return load(root/"compiled"/(blind["input"]["case_id"]+".json"))
            def subscription(config,blind,folder,preflight_only=False,**kwargs):
                if preflight_only:return dict(transport_success=True)
                return fixtures.model("claude",compiled(blind),folder)
            def transport(wire,auth,credentials,folder,**kwargs):
                if "configuration_id"in wire:
                    provider=next(p for p in package["providers"]if p["configuration_id"]==wire["configuration_id"])
                    return fixtures.search(wire,provider,folder)
                case=json.loads(wire["body"]["contents"][0]["parts"][0]["text"])["case_id"]
                return fixtures.model("pro",load(root/"compiled"/(case+".json")),folder)
            approval=dict(package_sha256=digest(package),decision="approved",approved_by="synthetic",approved_at="2026-11-01")
            with patch("openweb_news.codex_runtime.CodexRuntime",Runtime),patch("openweb_news.backends.subscription",side_effect=subscription),patch("openweb_news.runner.https",side_effect=transport),patch("openweb_news.transport.Credentials.get",return_value="synthetic"):
                report=run(package,approval=approval)
            self.assertFalse(any(e["task_id"].startswith("codex-")for e in records(root/"attempts.jsonl")))
            self.assertTrue(any(x["stage"]=="codex"and x["dispatch_state"]=="not_sent"for x in report["execution"]["held_tasks"]))

    def test_cleanup_failure_preserves_known_completed_native_judgment(self):
        from openweb_news.subscription_health import bind
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();runtime,processes=self.setup_runtime(root,fault="unsubscribe-error",concurrency=1)
            packet=self.packet("A");folder=private_directory(root/"case")
            with runtime:
                raw=runtime.review(packet,folder)
                self.assertTrue(raw["transport_success"])
                self.assertTrue(raw["complete"])
                self.assertEqual(raw["dispatch_state"],"completed")
                self.assertEqual(raw["verdict"],dict(case="A"))
                self.assertIsNotNone(raw["runtime_cleanup_error"])
                self.assertIsNotNone(runtime.client.failure)
                request=dict(kind="model",role="codex",backend="codex-subscription",backend_config=runtime.config,blind_packet=packet)
                self.assertIsNotNone(bind(request,raw,folder))
            self.assertEqual(sum(m.get("method")=="turn/start"for m in processes[0].requests),1)

    def test_parallel_fresh_threads_schema_routing_and_one_auth_overlay(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            runtime, processes = self.setup_runtime(root, concurrency=4)
            with runtime, ThreadPoolExecutor(max_workers=4) as pool:
                futures = [pool.submit(runtime.review, self.packet(case), private_directory(root / case)) for case in "ABCD"]
                results = [f.result() for f in futures]
                self.assertEqual(len(processes), 1)
                self.assertEqual(processes[0].maximum, 4)
                self.assertEqual([r["verdict"] for r in results], [dict(case=c) for c in "ABCD"])
                self.assertEqual(len({r["thread_id"] for r in results}), 4)
                self.assertEqual(len(list((root / "auth").glob(".news-reviewer-auth-*"))), 1)
                self.assertNotIn("OPENAI_API_KEY", processes[0].environment)
                turns = [m["params"] for m in processes[0].requests if m.get("method") == "turn/start"]
                self.assertEqual({p["effort"] for p in turns}, {"medium"})
                for turn in turns:
                    own = json.loads(turn["input"][0]["text"])["case"]
                    self.assertEqual(turn["outputSchema"]["properties"]["case"]["enum"], [own])
            self.assertFalse(list((root / "auth").glob(".news-reviewer-auth-*")))
            for path in root.rglob("*"):
                if path.is_file():
                    self.assertNotIn(b"SENSITIVE_ACCOUNT_METADATA_NOT_FOR_LOGS", path.read_bytes())

    def test_auth_context_tool_memory_echo_and_interaction_fail_closed(self):
        for fault in ("auth", "context", "tool", "memory", "echo", "interaction"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as temp:
                root = Path(temp).resolve()
                runtime, _ = self.setup_runtime(root, fault=fault, concurrency=1)
                if fault == "auth":
                    with self.assertRaises(ValueError):
                        with runtime:
                            self.fail("API auth must not start judge work")
                    continue
                with runtime:
                    result = runtime.review(self.packet("A"), private_directory(root / "case"))
                    self.assertFalse(result["transport_success"])
                    self.assertTrue(result["fatal_configuration"])
                    self.assertEqual(result["dispatch_state"], "uncertain")

    def test_case_timeout_interrupts_only_that_case_and_keeps_late_events(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            runtime, processes = self.setup_runtime(root, fault="timeout-one", concurrency=1, timeout=.05)
            with runtime:
                result = runtime.review(self.packet("A"), private_directory(root / "case"))
                self.assertEqual(result["dispatch_state"], "uncertain")
                self.assertNotIn("fatal_configuration", result)
                self.assertTrue((root / "case/runtime-events.jsonl").exists())
                again = runtime.review(self.packet("B"), private_directory(root / "other"))
                self.assertTrue(again["transport_success"])
                self.assertEqual(len(processes), 1)
                self.assertEqual(sum(m.get("method") == "turn/start" for m in processes[0].requests), 2)
                self.assertEqual(sum(m.get("method") == "turn/interrupt" for m in processes[0].requests), 1)
            self.assertIn(b'"interrupted"', (root / "runtime/notification-events.jsonl").read_bytes())

    def test_process_exit_releases_pending_requests_without_resending(self):
        process = FakeProcess(["synthetic"], env={})
        client = StdioClient(process)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(client.request, "unanswered", {}, time.monotonic()+2)
            self.assertTrue(process.received.wait(timeout=2))
            process.terminate()
            with self.assertRaises(RuntimeError):
                pending.result(timeout=2)
        client.close()
        self.assertEqual(sum(r.get("method") == "unanswered" for r in process.requests), 1)

    def test_late_tool_or_settings_event_after_interruption_still_stops_the_runtime(self):
        for method in ["item/started","thread/settings/updated"]:
          with self.subTest(method=method),tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            runtime, processes = self.setup_runtime(root, fault="timeout-one", concurrency=1, timeout=.05)
            with runtime:
                held = runtime.review(self.packet("A"), private_directory(root / "case"))
                processes[0].event(method, dict(threadId=held["thread_id"], turnId=held["turn_id"], item=dict(type="commandExecution")))
                other = runtime.review(self.packet("B"), private_directory(root / "other"))
                self.assertTrue(other["fatal_configuration"])
            self.assertIn(b'"commandExecution"', (root / "runtime/notification-events.jsonl").read_bytes())

    def test_full_stdin_pipe_obeys_deadline_without_a_blocking_write(self):
        read_input, write_input = os.pipe()
        read_output, write_output = os.pipe()
        class StalledProcess:
            stdin = os.fdopen(write_input, "w")
            stdout = os.fdopen(read_output, "r")
            stopped = False
            def poll(self):
                return 0 if self.stopped else None
            def terminate(self):
                self.stopped = True
                os.close(write_output)
            def wait(self, timeout=None):
                return 0
        process = StalledProcess()
        client = StdioClient(process)
        started = time.monotonic()
        try:
            with self.assertRaises(RuntimeError):
                client.request("blocked", dict(data="x"*1_000_000), started+.05)
            self.assertLess(time.monotonic()-started, 2)
        finally:
            client.close()
            os.close(read_input)

    def test_invalid_json_is_completed_unavailable_without_a_replacement(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            runtime, processes = self.setup_runtime(root, fault="json", concurrency=1)
            with runtime:
                result = runtime.review(self.packet("A"), private_directory(root / "case"))
            self.assertFalse(result["transport_success"])
            self.assertEqual(result["dispatch_state"], "completed")
            self.assertTrue(result["complete"])
            self.assertEqual(sum(m.get("method") == "turn/start" for m in processes[0].requests), 1)

    def test_moderation_metadata_is_passive_and_wrong_turn_still_fails(self):
        for fault in (None,"moderation-route"):
            with self.subTest(fault=fault),tempfile.TemporaryDirectory() as temp:
                root=Path(temp).resolve()
                runtime,_=self.setup_runtime(root,fault=fault,concurrency=1)
                with runtime:
                    result=runtime.review(self.packet("A"),private_directory(root/"case"))
                self.assertEqual(result["transport_success"],fault is None)
                if fault:
                    self.assertTrue(result["fatal_configuration"])
                else:
                    self.assertEqual(result["verdict"],dict(case="A"))

    def test_every_passive_notification_preserves_a_plain_judgment(self):
        for prefix,methods in [("global-notice:",GLOBAL_PASSIVE_NOTIFICATIONS),("thread-notice:",THREAD_PASSIVE_NOTIFICATIONS-{"thread/started","turn/started"})]:
            for method in sorted(methods):
                with self.subTest(method=method),tempfile.TemporaryDirectory() as temp:
                    root=Path(temp).resolve()
                    runtime,_=self.setup_runtime(root,fault=prefix+method,concurrency=1)
                    with runtime:
                        result=runtime.review(self.packet("A"),private_directory(root/"case"))
                    self.assertTrue(result["transport_success"],method)
                    self.assertEqual(result["verdict"],dict(case="A"))

    def test_model_provider_settings_and_unclassified_notifications_still_stop(self):
        for method in ["unsafe-safety-model","unsafe-auth-provider","thread/settings/updated","model/rerouted","unknown/new-notification"]:
            with self.subTest(method=method),tempfile.TemporaryDirectory() as temp:
                root=Path(temp).resolve()
                runtime,_=self.setup_runtime(root,fault="thread-notice:"+method,concurrency=1)
                with runtime:
                    result=runtime.review(self.packet("A"),private_directory(root/"case"))
                self.assertFalse(result["transport_success"])
                self.assertTrue(result["fatal_configuration"])

    def test_duplicate_thread_identifier_is_never_reused(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            runtime, processes = self.setup_runtime(root, fault="duplicate", concurrency=2)
            with runtime, ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(runtime.review, self.packet(c), private_directory(root / c)) for c in "AB"]
                results = [f.result() for f in futures]
            self.assertTrue(all(not r["transport_success"] for r in results))
            self.assertLessEqual(sum(m.get("method") == "turn/start" for m in processes[0].requests), 1)

    def run_live_fixture(self, *, fault=None):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            cfg = fixtures.config(root / "run")
            cfg["reviewers"]["codex"]["reasoning_effort"] = "medium"
            cfg["reviewers"]["codex"]["timeout_seconds"] = .2 if fault else 5
            cfg["execution_policy"]["spending_ceiling_usd"] = "150"
            package = prepare(cfg)
            approval = dict(decision="approved", package_sha256=digest(package), approved_by="synthetic owner", approved_at="2026-10-29T00:00:00Z")
            processes = []
            auth = private_directory(root / "auth")
            write_private(auth / "auth.json", dict(synthetic="dummy"))
            def verdict(data):
                compiled = load(root / "run/compiled" / (data["case_id"]+".json"))
                folder = private_directory(root / "native-fixtures" / data["case_id"])
                return fixtures.model("codex", compiled, folder)["verdict"]
            def factory(config, directory, *, preflight):
                def popen(command, **kwargs):
                    p = FakeProcess(command, barrier=threading_barrier, verdict=verdict, fault=fault, **kwargs)
                    processes.append(p)
                    return p
                return CodexRuntime(config, directory, preflight=preflight, popen=popen,
                                    parent_env=dict(HOME=str(root), CODEX_HOME=str(auth), PATH="/bin"))
            def subscription(config, packet, folder, *, preflight_only=False, **kwargs):
                if preflight_only:
                    return dict(transport_success=True, executable=sys.executable, configuration_sha256=digest(config),
                                executable_identity=backends.executable_identity(sys.executable), cli_version="synthetic-cli")
                compiled = load(root / "run/compiled" / (packet["input"]["case_id"]+".json"))
                return fixtures.model("claude", compiled, folder)
            def transport(request, auth, credentials, folder, **kwargs):
                if request.get("model"):
                    data = json.loads(request["body"]["contents"][0]["parts"][0]["text"])
                    compiled = load(root / "run/compiled" / (data["case_id"]+".json"))
                    return fixtures.model("pro", compiled, folder)
                provider = next(p for p in package["providers"] if p["configuration_id"] == request["configuration_id"])
                return fixtures.search(request, provider, folder)
            threading_barrier = threading.Barrier(4)
            with patch("openweb_news.runner.Credentials"), patch("openweb_news.runner.https", side_effect=transport), patch("openweb_news.backends.subscription", side_effect=subscription), patch("openweb_news.codex_runtime.CodexRuntime", side_effect=factory):
                report = run(package, approval=approval)
                resumed = run(package, approval=approval)
            if not fault:
                self.assertEqual(report["outcome_counts"], {"2": 4, "1": 2, "unknown": 2})
            else:
                self.assertEqual(report["execution"]["state"], "completed_with_holds")
                self.assertEqual(len(report["execution"]["held_tasks"]), 1)
                self.assertEqual(len(report["outcomes"]), 8)
                self.assertTrue((root / "run/report.html").is_file())
                finished = [r for r in (root / "run/attempts").glob('*/result.json') if load(r).get('selected_transport') == 'app-server-stdio-v1']
                self.assertEqual(sum(load(r).get('transport_success') is True for r in finished), 7)
            self.assertEqual(processes[0].maximum, 4)
            self.assertEqual(resumed["outcome_counts"], report["outcome_counts"])
            self.assertEqual(len(processes), 2)
            self.assertEqual(sum(m.get("method") == "turn/start" for p in processes for m in p.requests), 8)

    def test_runner_live_path_uses_one_parallel_runtime_and_resumes_without_judging(self):
        self.run_live_fixture()

    def test_runner_case_deadline_holds_one_case_and_reports_other_successes(self):
        self.run_live_fixture(fault="timeout-one")
