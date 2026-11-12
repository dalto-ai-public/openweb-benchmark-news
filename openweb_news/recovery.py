"""Sibling recovery rules, driven by retained completion order and timestamps."""
from copy import deepcopy
import json

from .common import asset_path, digest, load


def known_not_sent_connect(response):
    """Only the transport's pre-HTTP connection boundary proves no send."""
    return (response.get("dispatch_state") == "not_sent"
            and response.get("transport_stage") == "pre_http_connect"
            and response.get("complete") is False
            and response.get("transport_success") is False
            and response.get("http_status") is None
            and bool(response.get("error_type")))


def completed_expected_native_output(configuration, response, validation):
    """Port the sibling's narrow Google-only transport-health exception.

    Strict schema/evidence rejection still supplies no vote. Do not extend this
    exception to subscription runtimes without a native completion audit.
    """
    if (configuration != "pro" or validation not in {"invalid", "unjudged"}
            or response.get("complete") is not True
            or response.get("http_status") != 200
            or response.get("transport_success") is not True
            or response.get("dispatch_state") != "completed"
            or response.get("error_type") or response.get("error")
            or response.get("fatal_configuration") or response.get("credential_echo_redacted")):
        return False
    try:
        raw = response["raw_text"].encode()
        if response.get("retained_sha256") != digest(raw):
            return False
        envelope = json.loads(raw)
        if (envelope.get("modelVersion") != load(asset_path("method.json"))["primary"]["model"]
                or envelope.get("error") or (envelope.get("promptFeedback") or {}).get("blockReason")):
            return False
        candidates = envelope.get("candidates")
        if not isinstance(candidates, list) or len(candidates) != 1:
            return False
        candidate = candidates[0]
        if candidate.get("finishReason") != "STOP" or any(r.get("blocked") for r in candidate.get("safetyRatings", [])):
            return False
        parts = candidate["content"]["parts"]
        return isinstance(parts, list) and bool(parts) and all(
            isinstance(part, dict) and not set(part) - {"text", "thought", "thoughtSignature"}
            and isinstance(part.get("text"), str) and bool(part["text"].strip())
            and not part.get("thought") for part in parts)
    except (KeyError, ValueError, TypeError, AttributeError):
        return False


def sibling_rule(kind, concurrency):
    """Portable rule values; account IDs and credentials remain configuration-owned."""
    judge = kind == "judge"
    return dict(kind=kind, initial_concurrency=concurrency,
                maximum_concurrency=concurrency, reduced_concurrency=max(1, concurrency//2),
                error_window_size=20 if judge else 10, error_threshold=5 if judge else 3,
                consecutive_errors=3, cooldown_seconds=60, recovery_successes=8 if judge else 3,
                ramp_enabled=False, ramp_successes=32 if judge else 4,
                repeated_pause_stop=2, uncertain_immediate_pause=True)


class Recovery:
    def __init__(self, rules):
        self.rules, self.scopes, self.observed = deepcopy(rules), {}, set()

    def scope(self, account, configuration=None):
        key = (account, configuration)
        rule = self.rules[account]
        return self.scopes.setdefault(key, dict(mode="normal", limit=rule["initial_concurrency"],
            pauses=0, cooldown_until=0, consecutive_errors=0, successes=0, window=[]))

    def capacity(self, account, configuration, now):
        rule = self.rules[account]
        scopes = [self.scope(account), self.scope(account, configuration)]
        for scope in scopes:
            if scope["mode"] == "cooldown" and now >= scope["cooldown_until"]:
                scope.update(mode="recovery", limit=rule["reduced_concurrency"],
                             successes=0, consecutive_errors=0, window=[])
        stopped = any(s["mode"] == "stopped" for s in scopes)
        return dict(stopped=stopped, ready_at=max(s["cooldown_until"] for s in scopes),
                    account_limit=scopes[0]["limit"], configuration_limit=scopes[1]["limit"])

    def describe(self):
        return [dict(account=account, configuration=configuration, **deepcopy(state))
                for (account, configuration), state in sorted(self.scopes.items(), key=lambda item:(item[0][0], item[0][1] or ""))]

    def observe(self, identifier, account, configuration, response, finished_at, validation=None, *, native_completion=False):
        if identifier in self.observed:
            return
        self.observed.add(identifier)
        if (response.get("native_quota_evidence") and response.get("complete") is True
                and response.get("transport_success") is True and response.get("dispatch_state") == "completed"
                and not response.get("fatal_configuration") and validation == "valid"):
            # Separately durable native events carry health observations in time
            # order. A successful internal retry supplies a vote, not a health
            # success that hides those errors or counts each error twice.
            return
        rule = self.rules[account]
        status = response.get("http_status")
        uncertain = response.get("dispatch_state") == "uncertain" or (
            response.get("complete") is False and not known_not_sent_connect(response))
        output_error = validation in {"invalid", "unjudged"} and not (native_completion or completed_expected_native_output(configuration, response, validation))
        error = uncertain or output_error or response.get("transport_success") is False or status in {401, 402, 403, 429} or type(status) is int and status >= 500
        shared = rule["kind"] == "judge" or status in {401, 402, 403, 429}
        target = self.scope(account) if shared else self.scope(account, configuration)
        for scope in (self.scope(account), self.scope(account, configuration)):
            if status in {401,402,403} and scope is target:
                # An in-flight permanent failure overrides an earlier cooldown.
                scope.update(mode="stopped",pauses=scope["pauses"]+1,cooldown_until=finished_at,
                             successes=0,consecutive_errors=scope["consecutive_errors"]+1)
                continue
            if scope["mode"] in {"stopped", "cooldown"} or error and scope is not target:
                continue
            scope["window"] = (scope["window"] + [error])[-rule["error_window_size"]:]
            scope["consecutive_errors"] = scope["consecutive_errors"]+1 if error else 0
            scope["successes"] = 0 if error else scope["successes"]+1
            trigger = error and (uncertain and rule["uncertain_immediate_pause"] or status in {401, 402, 403} or
                scope["consecutive_errors"] >= rule["consecutive_errors"] or sum(scope["window"]) >= rule["error_threshold"])
            if trigger:
                scope["pauses"] += 1
                scope.update(mode="stopped" if status in {401, 402, 403} or (rule.get("transient_pause_action", "stop") == "stop" and scope["pauses"] >= rule["repeated_pause_stop"]) else "cooldown",
                             cooldown_until=finished_at+rule["cooldown_seconds"], successes=0)
            elif not error and scope["mode"] == "recovery" and scope["successes"] >= rule["recovery_successes"]:
                scope.update(mode="normal", limit=rule["initial_concurrency"], successes=0, window=[])
            elif not error and scope["mode"] == "normal" and rule["ramp_enabled"] and scope["successes"] >= rule["ramp_successes"]:
                scope["limit"] = rule["maximum_concurrency"]
