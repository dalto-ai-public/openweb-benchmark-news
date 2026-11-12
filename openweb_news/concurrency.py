"""Measure planned Codex judgments at declared capacities without duplicate probes."""
from collections import Counter
from copy import deepcopy
import re
import threading
import time


def quota_signal(event):
    """Use native structured evidence; a healthy sparse status update is not an error."""
    params = event.get("params") or {}
    if event.get("method") == "account/rateLimits/updated":
        limits = params.get("rateLimits") or {}
        reason = limits.get("rateLimitReachedType")
        if reason in {"rate_limit_reached", "workspace_owner_credits_depleted", "workspace_member_credits_depleted",
                      "workspace_owner_usage_limit_reached", "workspace_member_usage_limit_reached"}:
            return reason
        if limits.get("spendControlReached") is True:
            return "native_spend_control_reached"
        for key in ("primary", "secondary"):
            used = (limits.get(key) or {}).get("usedPercent")
            if type(used) is int and used >= 100:
                return "native_quota_window_exhausted"
    error = params.get("error") or event.get("error") or {}
    info = error.get("codexErrorInfo") if isinstance(error, dict) else None
    if info in ("rateLimitExceeded", "usageLimitExceeded"):
        return info
    if isinstance(info, dict) and any(isinstance(v, dict) and v.get("httpStatusCode") == 429 for v in info.values()):
        return "native_http_429"
    return None


def validate(config, scheduling, reviewers, depth):
    if config is None:
        return
    required = {"stages", "advance_health_successes", "on_throttle"}
    if set(config) not in (required, required | {"remaining_case_cohorts", "historical_cases"}) or config["on_throttle"] != "end-escalation-and-use-last-completed-stage":
        raise ValueError("explicit concurrency trial stages/health/backoff required")
    stages = config["stages"]
    if not stages or reviewers["codex"].get("transport") != "app-server-stdio-v1":
        raise ValueError("concurrency trial requires native parallel Codex transport")
    previous_rank, previous_limit = 0, 0
    for stage in stages:
        if set(stage) != {"concurrency", "rank_end_inclusive"} or any(type(v) is not int for v in stage.values()):
            raise ValueError("concurrency trial requires exact integer stages")
        if stage["concurrency"] <= previous_limit or stage["rank_end_inclusive"] <= previous_rank:
            raise ValueError("stages must increase capacity and cover distinct rank strata")
        previous_rank, previous_limit = stage["rank_end_inclusive"], stage["concurrency"]
    if previous_rank != depth or stages[0]["concurrency"] != scheduling["accounts"]["codex-subscription"]["concurrency"]:
        raise ValueError("stages must cover the judging pool from the initial capacity")
    recovery = scheduling.get("recovery", {}).get("codex-subscription")
    if not recovery or recovery["maximum_concurrency"] != previous_limit or recovery["ramp_enabled"]:
        raise ValueError("trial owns escalation; source recovery must cover the maximum without its separate ramp")
    if config["advance_health_successes"] != recovery["ramp_successes"]:
        raise ValueError("trial advancement uses the source clean-success requirement")
    if "remaining_case_cohorts" in config:
        cases, historical = config["remaining_case_cohorts"], config["historical_cases"]
        if (not isinstance(cases, dict) or not isinstance(historical, list) or len(set(historical)) != len(historical)
                or set(cases) & set(historical) or any(not re.fullmatch(r"occurrence-[0-9a-f]{64}", c) for c in list(cases) + historical)
                or any(type(i) is not int or not 0 <= i < len(stages) for i in cases.values())
                or set(cases.values()) != set(range(len(stages)))):
            raise ValueError("remaining trial requires distinct bound cases, historical cases and each declared cohort")


def validate_saved_cohorts(package):
    config = package["configuration"].get("codex_concurrency_trial", {})
    if "remaining_case_cohorts" not in config:
        return
    from pathlib import Path
    from .common import digest, load, records
    from ._method.providers import project_response
    from .judging import METHOD
    reference = package["configuration"].get("reuse_snapshot")
    if not reference:
        raise ValueError("remaining-work trial requires exact saved attempt evidence")
    if "codex" not in package["configuration"].get("subscription_health_transition", {}).get("roles", []):
        raise ValueError("remaining-work trial requires the explicit Codex health epoch; old successes cannot advance new stages")
    # The trial's original checkpoint owns historical/new membership. A later
    # continuation must retain new-epoch measurements rather than relabel them.
    checkpoint = package["configuration"]["subscription_health_transition"]["source_snapshot"]
    root = Path(checkpoint["manifest_path"]).parent
    latest = {e["task_id"]: e for e in records(root / "events.jsonl") if e["event"] == "finished"}
    current, historical = set(), set()
    for search in package["search_requests"]:
        cid, qid = search["configuration_id"], search["question_id"]
        capture = latest.get("search-" + digest(dict(configuration=cid, question=qid)))
        if capture is None:
            raise ValueError("remaining-work trial requires every search capture; no unknown cohort membership")
        raw = load(root / capture["result_path"])
        provider = next(p for p in package["providers"] if p["configuration_id"] == cid)
        question = next(q for q in package["questions"] if q["question_id"] == qid)
        projected = project_response(provider, raw.get("raw_text", "").encode(), raw["captured_at"],
            http_status=raw.get("http_status") or 0, complete=raw.get("complete", False))
        for position in projected["positions"][:METHOD["pool_depth"]]:
            case = "occurrence-" + digest(dict(question=question, configuration=provider, position=position))
            current.add(case)
            if "codex-" + case in latest:
                historical.add(case)
    if set(config["historical_cases"]) != historical or set(config["remaining_case_cohorts"]) != current - historical:
        raise ValueError("remaining-work trial must separate every saved Codex position from every unsent occurrence")
class Trial:
    def __init__(self, config, search_cells, *, clock=time.time):
        self.config, self.search_cells, self.clock = config, search_cells, clock
        self.current, self.verified, self.ended = 0, 0, None
        self.collections = set()
        self.cases, self.completed, self.measurements = {}, set(), {}
        self.active, self.peak = Counter(), Counter()
        self.lock = threading.RLock()
        self.quota_evidence = []

    def register(self, case, metadata):
        if "remaining_case_cohorts" in self.config:
            cohort = self.config["remaining_case_cohorts"].get(case)
            if cohort is None and case not in self.config["historical_cases"]:
                raise ValueError("occurrence missing from the exact remaining-work concurrency plan")
        else:
            cohort = next(i for i, row in enumerate(self.config["stages"]) if metadata["rank"] <= row["rank_end_inclusive"])
        self.cases[case] = dict(metadata, cohort=cohort)

    def collected(self, task):
        self.collections.add(task)

    def observe_quota(self, evidence):
        if evidence:
            self.quota_evidence = deepcopy(evidence)
            self.ended = "native throttling/quota exhaustion; escalation ended for this run"

    def advance(self, recovery):
        if recovery.scope("codex-subscription")["pauses"]:
            self.ended = self.ended or "source account recovery paused; no further escalation"
        if self.ended or len(self.collections) != self.search_cells:
            return
        while self.current < len(self.config["stages"]) - 1:
            cohort = {case for case, row in self.cases.items() if row["cohort"] == self.current}
            if not cohort <= self.completed:
                return
            health = recovery.scope("codex-subscription")
            if not cohort or health["mode"] != "normal" or health["successes"] < self.config["advance_health_successes"]:
                self.ended = "preceding planned cohort did not establish source healthy advancement; remaining work uses last completed stage"
                return
            self.verified = self.current
            self.current += 1

    def capacity(self, account, case):
        if account != "codex-subscription":
            return None
        index = self.verified if self.ended else self.current
        return dict(limit=self.config["stages"][index]["concurrency"],
                    eligible=bool(self.ended) or self.cases[case]["cohort"] <= self.current)

    def started(self, case):
        with self.lock:
            index = self.verified if self.ended else self.current
            self.active[index] += 1
            self.peak[index] = max(self.peak[index], self.active[index])
            return dict(stage=index, configured_concurrency=self.config["stages"][index]["concurrency"],
                        started_at=self.clock(), active_at_start=self.active[index], **self.cases[case])

    def finished(self, measurement):
        with self.lock:
            self.active[measurement["stage"]] -= 1
            return dict(measurement, finished_at=self.clock())

    def observe(self, case, raw, state):
        self.completed.add(case)
        if self.cases[case]["cohort"] is None:
            return  # Prior paid work is retained, never a new-stage measurement.
        measurement = raw.get("concurrency_measurement")
        if measurement:
            self.measurements[case] = dict(measurement, validation_state=state,
                dispatch_state=raw.get("dispatch_state"), transport_success=raw.get("transport_success"),
                elapsed_seconds=raw.get("elapsed_seconds"))
        elif raw.get("held_reason"):
            self.ended = "held planned judgment; no synthetic measurement or further escalation"

    def report(self):
        stages = []
        for index, config in enumerate(self.config["stages"]):
            rows = [row for row in self.measurements.values() if row["stage"] == index]
            duration = max((r["finished_at"] for r in rows), default=0) - min((r["started_at"] for r in rows), default=0)
            stages.append(dict(config, measured_tasks=len(rows),
                observed_peak=max((r["active_at_start"] for r in rows), default=0),
                stage_elapsed_seconds=duration, completed_per_minute=len(rows)*60/duration if duration > 0 else None,
                validation_counts=dict(Counter(row["validation_state"] for row in rows)),
                question_mix=dict(Counter(row["question_id"] for row in rows)),
                configuration_mix=dict(Counter(row["configuration_id"] for row in rows))))
        return dict(plan=self.config, stages=stages, escalation_ended=self.ended,
                    quota_evidence=self.quota_evidence, measurements=self.measurements,
                    limitation="different planned occurrences, rank-balanced cohorts where returned; no duplicate judgments, universal quota or quality claim")
