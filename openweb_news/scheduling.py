"""Account worker pools, event-driven dependencies and durable admission estimates."""
from collections import Counter, deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import ExitStack
from datetime import datetime, timezone
from decimal import Decimal
from email.utils import parsedate_to_datetime
import math
import time
import threading

from .common import digest, load


def account_bindings(providers, reviewers, primary):
    bindings = {"search/" + p["configuration_id"]: "provider/" + p["auth"]["secret_ref"] for p in providers}
    bindings["pro"] = "gemini-api/" + primary["secret_ref"]
    for role, config in reviewers.items():
        backend = config["backend"]
        bindings[role] = backend if backend.endswith("-subscription") else backend + "/" + config["secret_ref"]
    return bindings


def validate_scheduling(value, bindings, reviewers=None):
    if not isinstance(value, dict) or set(value) not in ({"accounts", "cache_mode"}, {"accounts", "cache_mode", "recovery"}) or value["cache_mode"] != "implicit-prefix-and-exact-tasks":
        raise ValueError("scheduling requires explicit accounts and implicit-prefix-and-exact-tasks cache mode")
    if set(value["accounts"]) != set(bindings.values()):
        raise ValueError("scheduling accounts must exactly match selected credential/backend accounts")
    for name, rule in value["accounts"].items():
        if not {"concurrency", "minimum_start_interval_seconds"} <= set(rule) or set(rule) - {"concurrency", "minimum_start_interval_seconds", "retry_backoff_floor_seconds"}:
            raise ValueError("each scheduling account requires concurrency and minimum start interval")
        if type(rule["concurrency"]) is not int or rule["concurrency"] < 1:
            raise ValueError("account concurrency must be a positive integer")
        interval = rule["minimum_start_interval_seconds"]
        if type(interval) not in (int, float) or not math.isfinite(interval) or interval < 0:
            raise ValueError("account start interval must be finite and nonnegative")
        floor = rule.get("retry_backoff_floor_seconds", 0)
        if type(floor) not in (int, float) or not math.isfinite(floor) or floor < 0:
            raise ValueError("account retry floor must be finite and nonnegative")
        if name == "codex-subscription" and (reviewers or {}).get("codex", {}).get("transport", "serial-cli-v1") != "app-server-stdio-v1" and rule["concurrency"] != 1:
            raise ValueError("Legacy Codex exec transport requires serial authentication handling")
    if "recovery" in value:
        from .recovery import sibling_rule
        if set(value["recovery"]) != set(value["accounts"]):
            raise ValueError("recovery rules must match selected accounts")
        for name, rule in value["recovery"].items():
            if set(rule) - {"transient_pause_action"} != set(sibling_rule("judge", 1)) or rule["kind"] not in {"judge", "provider"}:
                raise ValueError("recovery requires explicit complete scope rules")
            if rule.get("transient_pause_action", "stop") not in {"stop", "cooldown"}:
                raise ValueError("unsupported transient recovery action")
            counts = ("initial_concurrency", "maximum_concurrency", "reduced_concurrency", "error_window_size", "error_threshold", "consecutive_errors", "recovery_successes", "ramp_successes", "repeated_pause_stop")
            if any(type(rule[k]) is not int or rule[k] < 1 for k in counts):
                raise ValueError("positive recovery counts required")
            if rule["initial_concurrency"] != value["accounts"][name]["concurrency"] or not rule["reduced_concurrency"] <= rule["initial_concurrency"] <= rule["maximum_concurrency"] or rule["error_threshold"] > rule["error_window_size"]:
                raise ValueError("inconsistent recovery capacities or thresholds")
            if type(rule["cooldown_seconds"]) not in (int, float) or not math.isfinite(rule["cooldown_seconds"]) or rule["cooldown_seconds"] < 0 or any(type(rule[k]) is not bool for k in ("ramp_enabled", "uncertain_immediate_pause")):
                raise ValueError("finite cooldown and explicit recovery flags required")
            if name == "codex-subscription" and (reviewers or {}).get("codex", {}).get("transport", "serial-cli-v1") != "app-server-stdio-v1" and rule["maximum_concurrency"] != 1:
                raise ValueError("Legacy Codex exec cannot ramp beyond one")


def timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def retry_after_deadline(response, finished_at):
    """Honor both HTTP delay seconds and dates; never reset an old delay on resume."""
    deadline = finished_at
    for key, value in response.get("response_headers", []):
        if key.lower() != "retry-after":
            continue
        try:
            seconds = float(value)
            if math.isfinite(seconds) and seconds >= 0:
                deadline = max(deadline, finished_at + seconds)
        except (ValueError, TypeError):
            try:
                date = parsedate_to_datetime(value)
                if date.tzinfo is None:
                    date = date.replace(tzinfo=timezone.utc)
                deadline = max(deadline, date.timestamp())
            except (ValueError, TypeError, OverflowError):
                pass
    return deadline


class AdmissionBlocked(RuntimeError):
    pass


class RetryBudgetExhausted(AdmissionBlocked):
    pass


class Admission:
    """Called under the ledger mutex; index holds once, update before each send."""
    def __init__(self, ledger, policy, *, package=None, initial_committed_usd="0"):
        self.policy = policy
        self.package = package
        self._mutex = threading.RLock()
        self._settled = set()
        self.total = Decimal(initial_committed_usd)
        if not self.total.is_finite() or self.total < 0:
            raise ValueError("invalid inherited cost obligation")
        if policy.get("budget_accounting") and package is None:
            raise ValueError("usage accounting requires the frozen package rates")
        self.counts = Counter()
        self.retries = Counter()
        self.requests = {}
        self.stopped_reason = None
        self.pacing_cursor = 0
        self.quota_seen = set()
        self.superseded_semantic_health = set()
        if package and any(package.get("configuration", {}).get(key) for key in ("question_projection_compatibility", "semantic_health_retirement")):
            from .reuse import superseded_geography_health
            self.superseded_semantic_health = superseded_geography_health(package)
        if package and package.get("configuration", {}).get("subscription_health_transition"):
            from .subscription_health import transition_attempts
            self.superseded_semantic_health |= transition_attempts(package)
        if package and package.get("configuration", {}).get("interrupted_run_recovery"):
            from .interruption import attempts
            self.superseded_semantic_health |= {r["prior_attempt_id"] for r in attempts(package)}
        if package and package.get('configuration',{}).get('cache_expiry_recovery'):
            from .cache_recovery import archived_attempts
            self.superseded_semantic_health |= archived_attempts(package)
        for event in ledger.events():
            if event["event"] == "started":
                path = ledger.directory / "attempts" / event["attempt_id"] / "request.json"
                if digest(path.read_bytes()) != event["request_sha256"]:
                    raise ValueError("saved admission request binding differs")
                request = load(path)
                self.requests[event["attempt_id"]] = {key: request[key] for key in ("kind", "role", "configuration_id") if key in request}
                self.total += Decimal(request["reservation_usd"])
                if self.counts[event["task_id"]]:
                    self.retries['model' if request['kind'] == 'cache' else request['kind']] += 1
                self.counts[event["task_id"]] += 1
            elif event["event"] == "finished":
                self._settle(ledger, event)
        if self.total > Decimal(str(policy["spending_ceiling_usd"])):
            self.stopped_reason = "inherited committed costs and holds exceed ceiling"

    def _settle(self, ledger, event):
        if self.policy.get("budget_accounting") not in ("source-native-usage-v1", "source-native-cache-usage-v1"):
            return
        with self._mutex:
            attempt = event["attempt_id"]
            if attempt in self._settled:
                return
            request = load(ledger.directory / "attempts" / attempt / "request.json")
            if digest(request) != event["request_sha256"]:
                raise ValueError("settlement request binding differs")
            path = ledger.directory / event["result_path"]
            if digest(path.read_bytes()) != event["result_sha256"]:
                raise ValueError("settlement result binding differs")
            from .accounting import usage_cost
            cost = usage_cost(request, load(path), self.package)
            if cost is not None:
                maximum = Decimal(request["reservation_usd"])
                self.total += cost - maximum
                if cost > maximum:
                    self.stopped_reason = "usage-derived cost exceeded reserved maximum; review required"
            self._settled.add(attempt)

    def restore_pacing(self, ledger, pipeline, bindings, quota_events=()):
        events, cursor = ledger.events_since(self.pacing_cursor)
        ordered = [(timestamp(event["recorded_at"]), False, event) for event in events]
        ordered.extend((event["observed_at"], True, event) for event in quota_events if event["event_id"] not in self.quota_seen)
        for finished_at, native_quota, event in sorted(ordered, key=lambda row: row[:2]):
            if native_quota:
                from .quota import verified_window_event
                if (pipeline.recovery and not (self.package and self.package.get("configuration", {}).get("subscription_quota")
                        and verified_window_event(event))):
                    pipeline.recovery.observe("native-quota-" + event["event_id"], "codex-subscription", "codex",
                        dict(complete=True, dispatch_state="completed", http_status=429, transport_success=False), finished_at)
                self.quota_seen.add(event["event_id"])
                continue
            if event["attempt_id"] not in self.requests:
                path = ledger.directory / "attempts" / event["attempt_id"] / "request.json"
                if digest(path.read_bytes()) != event["request_sha256"]:
                    raise ValueError("new pacing request binding differs")
                request = load(path)
                self.requests[event["attempt_id"]] = {key: request[key] for key in ("kind", "role", "configuration_id") if key in request}
            request = self.requests[event["attempt_id"]]
            stage = "search/" + request["configuration_id"] if request["kind"] == "search" else request["role"]
            account = bindings[stage]
            if event["event"] == "started":
                pipeline.last_start[account] = max(pipeline.last_start.get(account, 0), timestamp(event["recorded_at"]))
                if pipeline.recovery:
                    pipeline.recovery.capacity(account, stage, timestamp(event["recorded_at"]))
            elif event["event"] == "finished":
                path = ledger.directory / event["result_path"]
                if digest(path.read_bytes()) != event["result_sha256"]:
                    raise ValueError("saved pacing result binding differs")
                response = load(path)
                self._settle(ledger, event)
                pipeline.defer(account, retry_after_deadline(response, timestamp(event["recorded_at"])))
                if event["attempt_id"] not in self.superseded_semantic_health:
                    if pipeline.recovery:
                        # Replay elapsed cooldown before a late completion, even
                        # when no new attempt started during a machine sleep.
                        pipeline.recovery.capacity(account, stage, timestamp(event["recorded_at"]))
                    native_completion = False
                    if self.package and stage in self.package.get("configuration", {}).get("subscription_completion_health", {}).get("roles", []):
                        from .subscription_health import verified
                        full_request = load(ledger.directory / "attempts" / event["attempt_id"] / "request.json")
                        native_completion = verified(full_request, response, ledger.directory / "attempts" / event["attempt_id"])
                    pipeline.observe(event["attempt_id"], account, stage, response, timestamp(event["recorded_at"]), event["task_id"], native_completion=native_completion)
        self.pacing_cursor = cursor

    def reserve(self, task_id, request, attempt):
        with self._mutex:
            return self._reserve(task_id, request, attempt)

    def _reserve(self, task_id, request, attempt):
        if self.stopped_reason:
            raise AdmissionBlocked(self.stopped_reason)
        amount = Decimal(request["reservation_usd"])
        retry_kind = 'model' if request['kind'] == 'cache' else request['kind']
        if attempt > 1:
            bounds = self.policy["retry_policy"]
            if (bounds["max_total"] is not None and sum(self.retries.values()) >= bounds["max_total"]) or (bounds["max_"+retry_kind] is not None and self.retries[retry_kind] >= bounds["max_"+retry_kind]):
                raise RetryBudgetExhausted("approved retry count reached; no call dispatched")
        if self.total + amount > Decimal(str(self.policy["spending_ceiling_usd"])):
            self.stopped_reason = "spending admission ceiling reached; no call dispatched"
            raise AdmissionBlocked(self.stopped_reason)
        if attempt > 1:
            self.retries[retry_kind] += 1
        self.total += amount
        self.counts[task_id] += 1


def task_ready_at(state, policy, account=None):
    if not state["dispatch"] or state["previous"] is None:
        return 0
    response = state["previous"]
    completed = timestamp(state["completed_at"])
    backoff = policy["retry_policy"]["backoff_seconds"]
    if isinstance(backoff, list):
        backoff = backoff[state["attempt"]-2]
    floor = policy.get("scheduling", {}).get("accounts", {}).get(account, {}).get("retry_backoff_floor_seconds", 0)
    backoff = max(backoff, floor)
    return max(completed + backoff, retry_after_deadline(response, completed))


class Pipeline:
    """Dispatch ready jobs immediately; wait for a completion or the next deadline."""
    def __init__(self, rules, *, clock=time.time, sleeper=time.sleep, recovery=None, classify=None):
        self.rules = rules
        self.clock, self.sleep = clock, sleeper
        self.queues = {name: deque() for name in rules}
        self.active = Counter()
        self.last_start, self.deferred = {}, {}
        from .recovery import Recovery
        self.recovery = Recovery(recovery) if recovery is not None else None
        self.classify = classify
        self.active_scopes = Counter()
        self.sync = None
        self.trial = None
        self.quota_gate = None
        self.on_health_change = None

    def submit(self, account, call, complete, *, ready_at=0, scope=None, case=None):
        self.queues[account].append((call, complete, ready_at, scope or account, case))

    def observe(self, identifier, account, scope, raw, finished_at, task_id, *, native_completion=False):
        if self.recovery:
            before={key:self.recovery.scope(account,key)['mode']for key in (None,scope)}
            validation = self.classify(task_id, raw) if self.classify else None
            self.recovery.observe(identifier, account, scope, raw, finished_at, validation, native_completion=native_completion)
            if self.on_health_change:
                for key in (None,scope):
                    state=self.recovery.scope(account,key)
                    if state['mode']in {'stopped','cooldown'}and state['mode']!=before[key]:
                        self.on_health_change(dict(account=account,configuration=key,mode=state['mode'],
                            task_id=task_id,attempt_id=identifier,observed_at=finished_at,http_status=raw.get('http_status')))

    def defer(self, account, until):
        self.deferred[account] = max(self.deferred.get(account, 0), until)

    def run(self):
        futures = {}
        with ExitStack() as stack:
            pools = {name: stack.enter_context(ThreadPoolExecutor(max_workers=self.recovery.rules[name]["maximum_concurrency"] if self.recovery else rule["concurrency"], thread_name_prefix="news-" + name.split("/")[0]))
                     for name, rule in self.rules.items()}
            try:
                while futures or any(self.queues.values()):
                    if self.sync:
                        self.sync()
                    deadlines = []
                    for name, queue in self.queues.items():
                        rule = self.rules[name]
                        while queue:
                            maximum = self.recovery.rules[name]["maximum_concurrency"] if self.recovery else rule["concurrency"]
                            if self.active[name] >= maximum:
                                break  # No queued scope can exceed the account's maximum.
                            current = self.clock()
                            account_ready = max(self.deferred.get(name, 0), self.last_start.get(name, -math.inf) + rule["minimum_start_interval_seconds"])
                            gate = self.quota_gate.capacity(name) if self.quota_gate else dict(stopped=False, ready_at=0)
                            if gate["stopped"]:
                                while queue:
                                    _, complete, _, _, _ = queue.popleft()
                                    complete(dict(transport_success=False, complete=False, dispatch_state="not_sent", held_reason=gate["reason"]))
                                break
                            account_ready = max(account_ready, gate["ready_at"])
                            eligible = None
                            future_ready = []
                            stopped = None
                            for i, job in enumerate(queue):
                                capacity = self.recovery.capacity(name, job[3], current) if self.recovery else dict(stopped=False, ready_at=0, account_limit=rule["concurrency"], configuration_limit=rule["concurrency"])
                                if self.trial:
                                    trial = self.trial.capacity(name, job[4])
                                    if trial:
                                        if not trial["eligible"]:
                                            continue
                                        for field, configuration in (("account_limit", None), ("configuration_limit", job[3])):
                                            healthy = self.recovery and self.recovery.scope(name, configuration)["mode"] == "normal"
                                            capacity[field] = trial["limit"] if healthy else min(capacity[field], trial["limit"])
                                if capacity["stopped"]:
                                    stopped = i
                                    break
                                if self.active[name] >= capacity["account_limit"] or self.active_scopes[name, job[3]] >= capacity["configuration_limit"]:
                                    continue
                                ready = max(job[2], account_ready, capacity["ready_at"])
                                if ready <= current:
                                    eligible = i
                                    break
                                future_ready.append(ready)
                            if stopped is not None:
                                _, complete, _, _, _ = queue[stopped]
                                del queue[stopped]
                                complete(dict(transport_success=False, complete=False, dispatch_state="not_sent", held_reason="affected account/configuration stopped by recovery policy"))
                                continue
                            if eligible is None:
                                deadlines.extend(future_ready)
                                break
                            call, complete, _, scope, _ = queue[eligible]
                            del queue[eligible]
                            futures[pools[name].submit(call)] = (name, scope, complete)
                            self.active[name] += 1
                            self.active_scopes[name, scope] += 1
                            self.last_start[name] = current
                    timeout = max(0, min(deadlines) - self.clock()) if deadlines else None
                    if futures:
                        done, _ = wait(futures, timeout=timeout, return_when=FIRST_COMPLETED)
                        for future in done:
                            name, scope, complete = futures.pop(future)
                            self.active[name] -= 1
                            self.active_scopes[name, scope] -= 1
                            complete(future.result())
                    elif deadlines:
                        self.sleep(min(timeout, 30) if self.quota_gate else timeout)
            except BaseException:
                # Running sends drain into durable results. Queued work never enters.
                for future in futures:
                    future.cancel()
                raise
