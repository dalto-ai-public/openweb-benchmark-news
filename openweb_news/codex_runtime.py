"""Parallel fresh Codex judge threads in one private subscription runtime."""
from concurrent.futures import Future
from contextlib import ExitStack
import json
import os
from pathlib import Path
import queue
import select
import subprocess
import tempfile
import threading
import time

from . import backends
from .common import digest, encoded, private_directory, sync_directory, write_private


GLOBAL_PASSIVE_NOTIFICATIONS = {
    "remoteControl/status/changed", "warning", "account/updated",
    "account/rateLimits/updated", "configWarning", "deprecationNotice",
}
THREAD_PASSIVE_NOTIFICATIONS = {
    "thread/started", "thread/closed", "thread/status/changed", "thread/readState/changed", "thread/name/updated", "turn/started",
    "turn/moderationMetadata", "warning", "error", "item/agentMessage/delta",
    "item/reasoning/textDelta", "item/reasoning/summaryTextDelta", "item/reasoning/summaryPartAdded",
    "model/verification", "model/safetyBuffering/updated",
    "modelProvider/authRecoveryStarted", "modelProvider/authRecoveryCompleted",
}
HANDLED_THREAD_NOTIFICATIONS = {"item/started", "item/completed", "thread/tokenUsage/updated", "turn/completed"}


class RuntimeStopped(RuntimeError):
    pass


def recoverable_failure(error,reason):
    return isinstance(error,(RuntimeStopped,TimeoutError))and reason in {
        "Codex runtime exited","Codex protocol request timed out"}


class StdioClient:
    """Multiplex JSON-RPC over a dedicated process; never reconnect or resend."""
    def __init__(self, process, journal=None, *, quota_journal=None, prior_quota=()):
        self.process = process
        self.journal = journal
        self.lock, self.writer = threading.RLock(), threading.Lock()
        self.pending, self.queues, self.used_threads = {}, {}, set()
        self.sequence, self.failure = 0, None
        self.quota_evidence = list(prior_quota)
        self.quota_journal = quota_journal
        self.quota_status = None
        os.set_blocking(process.stdin.fileno(), False)
        self.reader = threading.Thread(target=self._read, name="news-codex-events", daemon=True)
        self.reader.start()

    def stop(self, reason="Codex runtime stopped"):
        with self.lock:
            if self.failure:
                return
            self.failure = reason
            for future in self.pending.values():
                future.set_exception(RuntimeStopped(reason))
            self.pending.clear()
            for mailbox in self.queues.values():
                mailbox.put({"runtime_failure": reason})
        if self.process.poll() is None:
            self.process.terminate()

    def _send(self, value, deadline=None):
        deadline = deadline if deadline is not None else time.monotonic() + 30
        if not self.writer.acquire(timeout=max(0, deadline - time.monotonic())):
            raise TimeoutError("Codex stdin busy")
        try:
            remaining = memoryview(encoded(value))
            while remaining:
                if self.failure or time.monotonic() >= deadline:
                    raise RuntimeStopped("Codex send stopped or timed out")
                try:
                    written = os.write(self.process.stdin.fileno(), remaining)
                    remaining = remaining[written:]
                except BlockingIOError:
                    select.select([], [self.process.stdin.fileno()], [], max(0, deadline-time.monotonic()))
        finally:
            self.writer.release()

    def notify(self, method, params):
        self._send(dict(method=method, params=params))

    def request(self, method, params, deadline):
        with self.lock:
            if self.failure or time.monotonic() >= deadline:
                raise RuntimeStopped(self.failure or "Codex request deadline elapsed")
            self.sequence += 1
            identifier = self.sequence
            future = self.pending[identifier] = Future()
        try:
            self._send(dict(id=identifier, method=method, params=params), deadline)
            return future.result(timeout=max(0, deadline - time.monotonic()))
        except Exception as error:
            if isinstance(error,TimeoutError):
                self.stop("Codex protocol request timed out")
            elif isinstance(error,RuntimeStopped)and str(error)=="Codex rejected a protocol request":
                self.stop("Codex rejected a protocol request")
            else:
                self.stop("Codex protocol request failed isolation or integrity checks")
            raise

    def register(self, thread_id):
        with self.lock:
            if thread_id in self.used_threads:
                self.stop("Codex returned an already used judge thread")
                raise RuntimeStopped(self.failure)
            self.used_threads.add(thread_id)
            return self.queues.setdefault(thread_id, queue.Queue())

    def release(self, thread_id):
        """Retire only a known completed subscription; journals stay intact."""
        with self.lock:
            if thread_id not in self.used_threads:
                raise ValueError("cannot release an unknown judge thread")
            self.queues.pop(thread_id, None)

    def _read(self):
        try:
            for line in self.process.stdout:
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError("non-object protocol message")
                with self.lock:
                    from .concurrency import quota_signal
                    if value.get("method") == "account/rateLimits/updated":
                        limits = (value.get("params") or {}).get("rateLimits")
                        if isinstance(limits, dict) and limits.get("limitId") in (None, "codex"):
                            self.quota_status = limits
                    selected_bucket = value.get("method") != "account/rateLimits/updated" or ((value.get("params") or {}).get("rateLimits") or {}).get("limitId") in (None, "codex")
                    reason = quota_signal(value) if selected_bucket else None
                    if reason:
                        event = dict(reason=reason, observed_at=time.time(), thread_id=(value.get("params") or {}).get("threadId"),
                                     event_id=digest(dict(native_sha256=digest(value), sequence=len(self.quota_evidence) + 1)))
                        if value.get("method") == "account/rateLimits/updated" and not event["thread_id"]:
                            from .quota import window_status
                            status = window_status(self.quota_status, event["observed_at"])
                            if not status["stopped"] and status["ready_at"]:
                                event["native_window_reset_at"] = status["ready_at"]
                                event["native_window_status"] = {k:self.quota_status.get(k) for k in ("limitId", "primary", "secondary", "rateLimitReachedType", "spendControlReached")}
                        if self.quota_journal:
                            self.quota_journal.write(encoded(event))
                            os.fsync(self.quota_journal.fileno())
                        self.quota_evidence.append(event)
                    if "id" in value and "method" not in value:
                        future = self.pending.pop(value["id"], None)
                        if future is None:
                            raise ValueError("unmatched protocol response")
                        if "error" in value:
                            future.set_exception(RuntimeStopped("Codex rejected a protocol request"))
                        else:
                            future.set_result(value["result"])
                    elif "id" in value:
                        # A judge must never request a tool, permission or user interaction.
                        self._send(dict(id=value["id"], error=dict(code=-32601, message="Judge interactions disabled")))
                        raise ValueError("unexpected server interaction")
                    else:
                        params = value.get("params", {})
                        thread_id = params.get("threadId")
                        if value.get("method") == "thread/started":
                            thread_id = params.get("thread", {}).get("id")
                        if thread_id:
                            # Keep late events even after a timed-out attempt has closed.
                            # Account RPC responses never enter this notification journal.
                            if self.journal:
                                self.journal.write(encoded(value))
                            if value.get("method") not in THREAD_PASSIVE_NOTIFICATIONS | HANDLED_THREAD_NOTIFICATIONS:
                                raise ValueError("unexpected judge notification, including after interruption")
                            if value.get("method") in {"item/started", "item/completed"}:
                                item = params.get("item", {})
                                if item.get("type") not in {"userMessage", "agentMessage", "reasoning"} or item.get("memoryCitation"):
                                    raise ValueError("unexpected tool/memory item, including after interruption")
                            if thread_id not in self.used_threads or thread_id in self.queues:
                                self.queues.setdefault(thread_id, queue.Queue()).put(value)
                        elif value.get("method") not in GLOBAL_PASSIVE_NOTIFICATIONS:
                            raise ValueError("unrouted protocol notification")
                        elif value.get("method") == "account/updated" and params.get("authMode") not in {None, "chatgpt"}:
                            raise ValueError("subscription auth mode changed")
                        elif self.journal:
                            self.journal.write(encoded(value))
        except Exception:
            self.stop("Codex protocol stream failed isolation or integrity checks")
        finally:
            self.stop("Codex runtime exited")

    def close(self):
        self.stop()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.reader.join(timeout=5)
        for stream in (self.process.stdin, self.process.stdout):
            if stream:
                stream.close()


class CodexRuntime:
    """Code dispatches one case per fresh native agent thread, with no parent LLM."""
    def __init__(self, config, directory, *, preflight, parent_env=None, popen=subprocess.Popen):
        self.config, self.directory = config, private_directory(directory)
        self.preflight, self.parent_env, self.popen = preflight, parent_env, popen
        self.stack, self.client = ExitStack(), None

    def __enter__(self):
        try:
            env = backends.reviewer_environment("codex", parent=self.parent_env)
            executable = self.preflight["executable"]
            if not self.preflight.get("transport_success") or self.preflight.get("configuration_sha256") != digest(self.config) or backends.executable_identity(executable) != self.preflight.get("executable_identity"):
                raise ValueError("Codex configuration/executable changed after preflight")
            self.executable = executable
            self.work = private_directory(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="openweb-news-codex-")))
            env = self.stack.enter_context(backends.isolated_codex_environment(env, self.work))
            instructions = self.work / "coordinator.md"
            write_private(instructions, b"No judging task or evidence is assigned to the coordinator.\n")
            settings = backends.codex_settings(self.config, instructions)
            command = [executable, "--no-daemon", "app-server", "--listen", "stdio://", "--strict-config"]
            command += [part for key, value in settings.items() for part in ("-c", key + "=" + json.dumps(value))]
            fd = os.open(self.directory / "stderr.txt", os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
            stderr = self.stack.enter_context(os.fdopen(fd, "ab", buffering=0))
            fd = os.open(self.directory / "notification-events.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
            journal = self.stack.enter_context(os.fdopen(fd, "ab", buffering=0))
            self.stack.callback(lambda: os.fsync(journal.fileno()))
            process = self.popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr,
                                 text=True, encoding="utf-8", env=env, cwd=self.work, bufsize=1)
            quota_path = self.directory / "quota-events.jsonl"
            prior_quota = [json.loads(line) for line in quota_path.read_bytes().splitlines()] if quota_path.exists() else []
            fd = os.open(quota_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
            sync_directory(self.directory)
            quota_journal = self.stack.enter_context(os.fdopen(fd, "ab", buffering=0))
            self.client = StdioClient(process, journal=journal, quota_journal=quota_journal, prior_quota=prior_quota)
            self.stack.callback(self.client.close)
            deadline = time.monotonic() + 30
            self.client.request("initialize", dict(clientInfo=dict(name="openweb_news_benchmark", version="0.1.0"), capabilities=dict(experimentalApi=True)), deadline)
            self.client.notify("initialized", {})
            # Never persist the account response: it can include an email/address.
            account = self.client.request("account/read", dict(refreshToken=False), deadline)
            if (account.get("account") or {}).get("type") != "chatgpt":
                raise ValueError("Codex runtime is not using the selected subscription")
            receipt = dict(transport="app-server-stdio-v1", account_mode="subscription",
                          auth_observation="chatgpt", model=self.config["model"], reasoning_effort=self.config["reasoning_effort"],
                          cli_version=self.preflight.get("cli_version"), inference_performed=False)
            receipt_path = self.directory / "runtime.json"
            if receipt_path.exists():
                if json.loads(receipt_path.read_bytes()) != receipt:
                    raise ValueError("Codex runtime receipt differs from the resumed configuration")
            else:
                write_private(receipt_path, receipt)
            return self
        except BaseException:
            self.stack.close()
            raise

    def __exit__(self, *error):
        return self.stack.__exit__(*error)

    def review(self, packet, folder):
        started = time.monotonic()
        deadline = started + self.config["timeout_seconds"]
        thread_id, turn_id, verdict, usage = None, None, None, None
        try:
            if backends.executable_identity(self.executable) != self.preflight["executable_identity"]:
                raise ValueError("Codex executable changed during run")
            account = self.client.request("account/read", dict(refreshToken=False), deadline)
            if (account.get("account") or {}).get("type") != "chatgpt":
                raise ValueError("Codex subscription auth mode changed")
            work = private_directory(self.work / ("judge-" + digest(packet)))
            params = dict(model=self.config["model"], ephemeral=True, approvalPolicy="never", sandbox="read-only",
                          cwd=str(work), baseInstructions=packet["prompt"], developerInstructions="", dynamicTools=[],
                          allowProviderModelFallback=False)
            response = self.client.request("thread/start", params, deadline)
            write_private(folder / "runtime-thread.json", response)
            thread = response["thread"]
            thread_id = thread["id"]
            mailbox = self.client.register(thread_id)
            if (response.get("model") != self.config["model"] or response.get("modelProvider") != "openai" or
                response.get("reasoningEffort") != self.config["reasoning_effort"] or response.get("approvalPolicy") != "never" or
                response.get("sandbox") != dict(type="readOnly", networkAccess=False) or response.get("instructionSources") != [] or
                thread.get("ephemeral") is not True or thread.get("turns") or thread.get("forkedFromId") or thread.get("parentThreadId")):
                raise ValueError("Codex judge thread differs from frozen isolation/settings")
            payload = encoded(packet["input"]).decode()
            turn_params = dict(threadId=thread_id, model=self.config["model"], effort=self.config["reasoning_effort"],
                               input=[dict(type="text", text=payload)], outputSchema=packet["schema"])
            write_private(folder / "runtime-request.json", dict(thread_start=params, turn_start=turn_params))
            response = self.client.request("turn/start", turn_params, deadline)
            turn_id = response["turn"]["id"]
            answers, echo_seen, status = [], False, None
            passive = THREAD_PASSIVE_NOTIFICATIONS
            fd = os.open(folder / "runtime-events.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "ab", buffering=0) as stream:
                while status is None:
                    try:
                        event = mailbox.get(timeout=max(0, deadline - time.monotonic()))
                    except queue.Empty:
                        interrupt = dict(threadId=thread_id, turnId=turn_id)
                        write_private(folder / "runtime-interrupt-request.json", interrupt)
                        response = self.client.request("turn/interrupt", interrupt, time.monotonic()+30)
                        write_private(folder / "runtime-interrupt-response.json", response)
                        stream.write(encoded(dict(turn_wait_deadline_elapsed=True, **interrupt)))
                        os.fsync(stream.fileno())
                        return dict(transport_success=False, complete=False, dispatch_state="uncertain",
                                    selected_backend="codex-subscription", selected_transport="app-server-stdio-v1",
                                    thread_id=thread_id, turn_id=turn_id, error_type="TurnWaitDeadline",
                                    error="Codex case deadline elapsed; interrupt requested; no resend",
                                    elapsed_seconds=time.monotonic()-started)
                    stream.write(encoded(event))
                    if "runtime_failure" in event:
                        raise RuntimeStopped(event["runtime_failure"])
                    method, data = event["method"], event.get("params", {})
                    if data.get("threadId") not in {None, thread_id}:
                        raise ValueError("Codex event belongs to another thread")
                    if data.get("turnId") not in {None, turn_id}:
                        raise ValueError("Codex event belongs to another turn")
                    if method in {"modelProvider/authRecoveryStarted", "modelProvider/authRecoveryCompleted"} and data.get("provider") != "openai":
                        raise ValueError("Codex auth recovery belongs to another provider")
                    if method == "model/safetyBuffering/updated" and (data.get("model") != self.config["model"] or data.get("fasterModel") not in {None, self.config["model"]}):
                        raise ValueError("Codex safety metadata names a different model")
                    if method in {"item/started", "item/completed"}:
                        item = data["item"]
                        kind = item.get("type")
                        if kind not in {"userMessage", "agentMessage", "reasoning"} or item.get("memoryCitation"):
                            raise ValueError("unexpected Codex tool/memory/context item")
                        if method == "item/completed" and kind == "userMessage":
                            if json.loads("".join(part.get("text", "") for part in item["content"])) != packet["input"]:
                                raise ValueError("Codex input echo differs from this case")
                            echo_seen = True
                        if method == "item/completed" and kind == "agentMessage" and item.get("phase") != "commentary":
                            answers.append(item["text"])
                    elif method == "thread/tokenUsage/updated":
                        usage = data.get("tokenUsage")
                    elif method == "turn/completed":
                        if data["turn"]["id"] != turn_id:
                            raise ValueError("Codex completion belongs to another turn")
                        status = data["turn"]["status"]
                    elif method not in passive:
                        raise ValueError("unexpected Codex judge event")
                os.fsync(stream.fileno())
            if status == "completed" and echo_seen and len(answers) == 1:
                try:
                    verdict = json.loads(answers[0])
                except ValueError:
                    pass
            cleanup_error = None
            if status == "completed":
                try:
                    unsubscribe = dict(threadId=thread_id)
                    write_private(folder / "runtime-unsubscribe-request.json", unsubscribe)
                    released = self.client.request("thread/unsubscribe", unsubscribe, time.monotonic()+30)
                    write_private(folder / "runtime-unsubscribe-response.json", released)
                    if released.get("status") not in {"unsubscribed", "notSubscribed", "notLoaded"}:
                        raise ValueError("Codex thread unsubscribe response differs from the native protocol")
                    self.client.release(thread_id)
                except Exception as error:
                    self.client.stop("Codex thread cleanup failed; no further runtime dispatch")
                    cleanup_error = dict(error_type=type(error).__name__, reason="thread cleanup failed after known terminal completion; inference is not uncertain")
                    write_private(folder / "runtime-cleanup-error.json", cleanup_error)
            return dict(transport_success=verdict is not None and status == "completed", verdict=verdict,
                        complete=status == "completed", dispatch_state="completed" if status == "completed" else "uncertain",
                        selected_backend="codex-subscription", selected_transport="app-server-stdio-v1",
                        requested_model=self.config["model"], served_model=None, served_model_observation="not_emitted_by_runtime",
                        requested_settings=dict(reasoning_effort=self.config["reasoning_effort"]), usage=usage,
                        runtime_cleanup_error=cleanup_error,
                        native_quota_evidence=[event for event in self.client.quota_evidence if event["thread_id"] == thread_id],
                        account_mode="subscription", auth_observation="chatgpt", cli_version=self.preflight.get("cli_version"),
                        thread_id=thread_id, turn_id=turn_id, elapsed_seconds=time.monotonic()-started,
                        error=None if verdict is not None else "Codex judgment missing, invalid or unavailable")
        except Exception as error:
            failure=self.client.failure
            recoverable=recoverable_failure(error,failure)
            self.client.stop("Codex judge timed out or failed isolation/protocol checks")
            return dict(transport_success=False, complete=False, dispatch_state="uncertain", fatal_configuration=True,
                        selected_backend="codex-subscription", selected_transport="app-server-stdio-v1",
                        error_type=type(error).__name__, error="Codex runtime failed; inspect retained private events",
                        recoverable_runtime_failure=recoverable,
                        native_quota_evidence=[event for event in self.client.quota_evidence if event["thread_id"] == thread_id],
                        thread_id=thread_id, turn_id=turn_id, elapsed_seconds=time.monotonic()-started)
