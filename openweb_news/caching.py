"""Optional full-system cache, following the source runner's exact-input boundary.

Full semantic model requests remain the task identity. Cache lifecycle attempts
have their own durable sends and conservative cost holds; they never reuse votes.
"""
from copy import deepcopy
from decimal import Decimal
import json
import re
import threading
import time

from .common import digest, encoded, load
from .ledger import UncertainAttempt
from .scheduling import AdmissionBlocked, timestamp


PROFILE = "source-native-cache-usage-v1"
ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/cachedContents"


def payload(wire):
    model = wire["endpoint"].split("/models/", 1)[1].split(":", 1)[0]
    return dict(model="models/" + model, systemInstruction=deepcopy(wire["body"]["systemInstruction"]))


def cache_wire(wire, resource, group):
    """Replace the complete system instruction and prove all other fields equal."""
    if payload(wire) != group or resource["group_id"] != digest(group):
        raise ValueError("cache resource differs from complete system/model")
    result = deepcopy(wire)
    if "cachedContent" in result["body"]:
        raise ValueError("semantic request already has cachedContent")
    del result["body"]["systemInstruction"]
    result["body"]["cachedContent"] = resource["name"]
    restored = deepcopy(result)
    restored["body"].pop("cachedContent")
    restored["body"]["systemInstruction"] = deepcopy(group["systemInstruction"])
    if restored != wire:
        raise ValueError("cache changed effective model input")
    return result


def validate(config, policy):
    if config is None:
        return
    keys = {"ttl_seconds", "expiry_tolerance_seconds", "minimum_reuses", "minimum_input_tokens",
            "estimated_system_tokens", "cached_input_per_million_usd", "storage_per_million_token_hour_usd",
            "complete_create_fallback_statuses", "expiry_fallback", "cleanup_after_drain"}
    if set(config) not in (keys, keys | {"renewal"}) or policy.get("budget_accounting") != PROFILE:
        raise ValueError("explicit complete cache policy and cache accounting profile required")
    for key in ("ttl_seconds", "minimum_reuses", "minimum_input_tokens"):
        if type(config[key]) is not int or config[key] <= 0:
            raise ValueError("positive cache lifecycle/eligibility counts required")
    if type(config["expiry_tolerance_seconds"]) is not int or config["expiry_tolerance_seconds"] < 0:
        raise ValueError("nonnegative cache expiry tolerance required")
    if any(type(config[k]) is not bool for k in ("expiry_fallback", "cleanup_after_drain")):
        raise ValueError("cache fallback and cleanup must be explicit")
    if (not isinstance(config["estimated_system_tokens"], dict) or
            any(type(v) is not int or v < 0 for v in config["estimated_system_tokens"].values())):
        raise ValueError("cache eligibility requires per-system token estimates")
    for key in ("cached_input_per_million_usd", "storage_per_million_token_hour_usd"):
        rate = Decimal(str(config[key]))
        if not rate.is_finite() or rate < 0:
            raise ValueError("finite nonnegative cache rates required")
    statuses = config["complete_create_fallback_statuses"]
    if not isinstance(statuses, list) or any(type(v) is not int or not 400 <= v < 500 or v in (401, 403, 408, 429) for v in statuses):
        raise ValueError("fallback requires explicitly approved completed non-auth client errors")

    renewal = config.get("renewal")
    if renewal is not None:
        if (not isinstance(renewal, dict) or renewal.get('profile') not in {'expiry-leases-v1','expiry-leases-v2'}
                or not isinstance(renewal.get('epoch'),str) or not re.fullmatch(r"[0-9a-f]{64}",renewal['epoch'])):
            raise ValueError("cache renewal requires an exact private epoch and expiry-leases profile")
        expected={'profile','epoch'}|({'renew_before_seconds'}if renewal['profile']=='expiry-leases-v2'else set())
        if set(renewal)!=expected or ('renew_before_seconds'in renewal and
                (type(renewal['renew_before_seconds'])is not int or not 0<renewal['renew_before_seconds']<config['ttl_seconds'])):
            raise ValueError('cache renewal margin must be positive and shorter than its TTL')


def plan(package):
    from .judging import METHOD, build_pro_request, compile_packet, packet_for
    config = package["configuration"].get("system_cache")
    if config is None:
        return []
    validate(config, package["configuration"]["execution_policy"])
    empty = dict(status="native_result", title=dict(state="missing"), content=dict(state="missing"),
                 url=dict(state="missing"), metadata={}, additional_content={}, native_dates=dict(observations=[]))
    groups = {}
    for question in package["questions"]:
        provider = package["providers"][0]
        wire = build_pro_request(compile_packet(packet_for(question, provider, empty, "cache-plan"), "2000-01-01T00:00:00Z", config=provider))
        group = payload(wire)
        key = digest(group)
        row = groups.setdefault(key, dict(group_id=key, payload=group, planned_reuses=0))
        row["planned_reuses"] += len(package["providers"]) * METHOD["pool_depth"]
    if set(config["estimated_system_tokens"]) != set(groups):
        raise ValueError("cache estimates must cover exactly the selected full-system groups")
    rate = Decimal(package["primary"]["accounting_rates"]["input_per_million_usd"])
    cached = Decimal(str(config["cached_input_per_million_usd"]))
    storage = Decimal(str(config["storage_per_million_token_hour_usd"]))
    for row in groups.values():
        tokens = config["estimated_system_tokens"][row["group_id"]]
        overhead = Decimal(tokens) * (rate + storage * config["ttl_seconds"] / 3600) / 1_000_000
        savings = Decimal(tokens) * (rate - cached) * row["planned_reuses"] / 1_000_000
        # Planned estimates are not native API acceptance, actual hits or invoices.
        row.update(estimated_system_tokens=tokens, estimated_lifecycle_usd=str(overhead),
                   estimated_generation_saving_usd=str(savings),
                   eligible=tokens >= config["minimum_input_tokens"] and row["planned_reuses"] >= config["minimum_reuses"] and savings > overhead)
    return sorted(groups.values(), key=lambda row: row["group_id"])


class SystemCache:
    def __init__(self, package, ledger, admission, transport, *, clock=time.time):
        self.package, self.ledger, self.admission, self.transport, self.clock = package, ledger, admission, transport, clock
        self.config = package["configuration"]["system_cache"]
        self.groups = {g["group_id"]: g for g in package["cache_plan"] if g["eligible"]}
        self.locks = {key: threading.Lock() for key in self.groups}
        self.resources = {}
        self.leases = {}
        self.observations = {}

    def _execute(self, task, wire, maximum):
        request = dict(kind="cache", role="pro", wire=wire, reservation_usd=str(maximum),
                       reservation_basis="full byte allowance for creation and full approved TTL storage; no deletion credit")
        if self.config.get("renewal") and task.startswith("cache-create-"):
            saved = self.ledger.task_events(task)
            if saved:
                start = next(e for e in saved if e["event"] == "started")
                request = load(self.ledger.directory / "attempts" / start["attempt_id"] / "request.json")
            else:
                request["lease_requested_at"] = self.clock()
        from .interruption import KEY, CREDIT_PROFILE, attempts
        proof = self.package['configuration'].get(KEY, {})
        ends = [e for e in self.ledger.task_events(task) if e['event'] == 'finished']
        named = bool(proof.get('profile') == CREDIT_PROFILE and ends and any(
            row['prior_attempt_id'] == ends[-1]['attempt_id'] for row in attempts(self.package)))
        return self.ledger.execute(task, request, lambda folder: self.transport(wire, folder),
            max_attempts=self.package['configuration']['execution_policy']['max_attempts_per_task'] if named else 1,
            retry_failed=named, before_start=self.admission.reserve)

    def _resource(self, key, result):
        if result.get("dispatch_state") == "uncertain" or not result.get("complete"):
            raise AdmissionBlocked("uncertain cache creation; no fallback or recreation")
        if result.get("http_status") in self.config["complete_create_fallback_statuses"]:
            self.observations[key] = dict(state="completed_create_fallback", http_status=result["http_status"])
            return None
        if result.get("http_status") not in (200, 201) or result.get("credential_echo_redacted"):
            raise AdmissionBlocked("cache creation failed; no automatic resend or fallback")
        try:
            if digest(result["raw_text"].encode()) != result["retained_sha256"]:
                raise ValueError("cache response bytes differ")
            value = json.loads(result["raw_text"])
            created = timestamp(value["createTime"])
            expires = timestamp(value["expireTime"])
            if (not re.fullmatch(r"cachedContents/[A-Za-z0-9_-]+", value["name"]) or
                    value["model"] != self.groups[key]["payload"]["model"] or
                    not 0 < expires - created <= self.config["ttl_seconds"] + self.config["expiry_tolerance_seconds"]):
                raise ValueError("cache native resource/model/expiry differs")
            tokens = value["usageMetadata"]["totalTokenCount"]
            if type(tokens) is not int or tokens < 0 or tokens > len(encoded(self.groups[key]["payload"])) + 1024:
                raise ValueError("cache usage outside reserved allowance")
            return dict(group_id=key, name=value["name"], expires_at=expires, native_usage=value["usageMetadata"])
        except (ValueError, TypeError, KeyError):
            raise AdmissionBlocked("invalid cache resource; retain lifecycle hold and inspect evidence") from None

    def identity(self, key, lease):
        renewal = self.config.get("renewal")
        return digest(dict(group_id=key, epoch=renewal["epoch"], lease=lease)) if renewal else key

    def wire(self, semantic):
        group = payload(semantic)
        key = digest(group)
        if key not in self.groups:
            return semantic
        with self.locks[key]:
            margin=(self.config.get('renewal')or{}).get('renew_before_seconds',0)
            while True:
                lease = self.leases.setdefault(key, 0)
                identity = self.identity(key, lease)
                saved = bool(self.ledger.task_events("cache-create-" + identity))
                if key not in self.resources:
                    wire = dict(method="POST", endpoint=ENDPOINT, body=dict(group, ttl=str(self.config["ttl_seconds"]) + "s"))
                    rates = self.package["primary"]["accounting_rates"]
                    tokens = len(encoded(group)) + 1024
                    maximum = Decimal(tokens) * (Decimal(rates["input_per_million_usd"]) + Decimal(str(self.config["storage_per_million_token_hour_usd"])) * self.config["ttl_seconds"] / 3600) / 1_000_000
                    try:
                        result = self._execute("cache-create-" + identity, wire, maximum)
                    except UncertainAttempt:
                        raise AdmissionBlocked("uncertain saved cache create; no recreation or fallback") from None
                    self.resources[key] = self._resource(key, result)
                    if self.config.get("renewal") and self.resources[key] is not None:
                        start = next(e for e in self.ledger.task_events("cache-create-" + identity) if e["event"] == "started")
                        request = load(self.ledger.directory / "attempts" / start["attempt_id"] / "request.json")
                        if self.resources[key]["expires_at"] <= request["lease_requested_at"]+margin:
                            del self.resources[key]
                            raise AdmissionBlocked("cache lacks the declared usable lifetime at creation; no renewal or fallback")
                resource = self.resources[key]
                if resource is None:
                    return semantic  # A complete approved rejection is not a renewal opportunity.
                deleted = self.ledger.task_events("cache-delete-" + identity)
                retired = False
                if deleted:
                    done = next((e for e in reversed(deleted) if e["event"] == "finished"), None)
                    result = load(self.ledger.directory / done["result_path"]) if done else {}
                    if self.config.get("renewal") and (result.get("dispatch_state") == "uncertain" or not result.get("complete")
                            or result.get("http_status") not in (200, 204, 404)):
                        raise AdmissionBlocked("unverified cache retirement; no renewal")
                    retired = True
                expired = resource["expires_at"]-margin <= self.clock()
                if expired or retired:
                    self.observations[key] = dict(state="expired" if expired else "retired", resource=resource, lease=lease)
                    if self.config.get("renewal") and (saved or retired):
                        self.leases[key] += 1
                        del self.resources[key]
                        continue
                    if self.config["expiry_fallback"]:
                        return semantic
                    raise AdmissionBlocked("expired/retired cache; full-input fallback not approved")
                self.observations[key] = dict(state="available", resource=resource, lease=lease)
                return cache_wire(semantic, resource, group)

    def cleanup(self):
        if not self.config["cleanup_after_drain"]:
            return
        # An uncertain generation may still reference the cache. TTL bounds its
        # service storage; local evidence is never deleted or shortened.
        events = self.ledger.events()
        finished = {e["attempt_id"]: e for e in events if e["event"] == "finished"}
        for event in events:
            if event["event"] != "started":
                continue
            request = load(self.ledger.directory / "attempts" / event["attempt_id"] / "request.json")
            if request.get("role") != "pro":
                continue
            done = finished.get(event["attempt_id"])
            if not done or load(self.ledger.directory / done["result_path"]).get("dispatch_state") == "uncertain":
                self.observations["cleanup"] = dict(state="held_after_uncertainty")
                return
        if self.admission.stopped_reason:
            self.observations["cleanup"] = dict(state="held_by_admission_stop")
            return
        for key, resource in self.resources.items():
            if resource is None or resource["expires_at"] <= self.clock():
                continue
            wire = dict(method="DELETE", endpoint=ENDPOINT.rsplit("/", 1)[0] + "/" + resource["name"], body={})
            try:
                result = self._execute("cache-delete-" + self.identity(key, self.leases[key]), wire, Decimal(0))
                self.observations[key]["cleanup"] = dict(http_status=result.get("http_status"), dispatch_state=result.get("dispatch_state"))
            except (AdmissionBlocked, UncertainAttempt):
                self.observations[key]["cleanup"] = dict(state="held; no resend")

    def report(self):
        return dict(plan=self.package["cache_plan"], observations=self.observations,
                    accounting="native cache reads use frozen rates; full conservative creation/storage holds retained; no invoice claim")
