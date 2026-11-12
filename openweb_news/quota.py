"""Native Codex quota-window admission, independent of actual error recovery."""
import math
import time
from .common import write_private, load

PROFILE = "native-window-resume-v1"
WAIT_REFRESH_SECONDS = 300


def validate(config, reviewers):
    if config is None:
        return
    if config != {"profile": PROFILE, "bucket": "codex", "on_unknown": "hold", "consume_reset_credits": False}:
        raise ValueError("quota policy requires explicit native window gating without reset-credit consumption")
    if reviewers["codex"].get("transport") != "app-server-stdio-v1":
        raise ValueError("native quota admission requires the app-server transport")


def window_status(limits, now):
    """Only a future native reset can justify waiting instead of a safe hold."""
    if not isinstance(limits, dict) or limits.get("limitId") not in (None, "codex"):
        return dict(stopped=True, ready_at=0, reason="selected native quota bucket unavailable")
    if limits.get("spendControlReached") or limits.get("rateLimitReachedType") not in (None, "rate_limit_reached"):
        return dict(stopped=True, ready_at=0, reason="native account/credit limit requires inspection")
    windows = [limits.get(k) for k in ("primary", "secondary") if limits.get(k) is not None]
    if not windows:
        return dict(stopped=True, ready_at=0, reason="native quota windows unavailable")
    exhausted = []
    for w in windows:
        used, reset = w.get("usedPercent"), w.get("resetsAt")
        if (type(used) is not int or not 0 <= used <= 100 or type(reset) not in (int, float)
                or not math.isfinite(reset)):
            return dict(stopped=True, ready_at=0, reason="native quota window malformed")
        if used >= 100:
            exhausted.append(reset)
    if exhausted:
        if max(exhausted) <= now:
            return dict(stopped=True, ready_at=0, reason="exhausted native window needs a fresh status")
        return dict(stopped=False, ready_at=max(exhausted), reason="waiting for native quota reset")
    if limits.get("rateLimitReachedType"):
        return dict(stopped=True, ready_at=0, reason="native limit has no verifiable exhausted window")
    return dict(stopped=False, ready_at=0, reason="native quota available")


def verified_window_event(event):
    if event.get("thread_id") is not None or event.get("reason") not in {"native_quota_window_exhausted", "rate_limit_reached"}:
        return False
    state = window_status(event.get("native_window_status"), event["observed_at"])
    return not state["stopped"] and state["ready_at"] == event.get("native_window_reset_at") and bool(state["ready_at"])


class QuotaGate:
    def __init__(self, client, directory, *, clock=time.time):
        self.client, self.directory, self.clock = client, directory, clock
        path = self.directory / "quota-status.json"
        prior = load(path) if path.exists() else {}
        if prior and prior.get("profile") != PROFILE:
            raise ValueError("saved quota admission profile differs")
        self.observations = prior.get("observations", [])
        self.reset_at = None
        self.last_refresh_at = None
        self.refresh()

    def refresh(self):
        raw = self.client.request("account/rateLimits/read", {}, time.monotonic() + 30)
        selected = (raw.get("rateLimitsByLimitId") or {}).get("codex", raw.get("rateLimits"))
        self.client.quota_status = selected
        self.record(selected)
        self.last_refresh_at = self.clock()

    def record(self, selected):
        safe = {k:selected.get(k) for k in ("limitId", "rateLimitReachedType", "spendControlReached")} if isinstance(selected, dict) else {}
        for key in ("primary", "secondary"):
            w = (selected or {}).get(key)
            safe[key] = {k:w.get(k) for k in ("usedPercent", "windowDurationMins", "resetsAt")} if isinstance(w, dict) else None
        self.observations.append(dict(observed_at=self.clock(), limits=safe))
        write_private(self.directory / "quota-status.json", dict(profile=PROFILE, observations=self.observations,
            inference_performed=False, reset_credits_consumed=False), replace=True)

    def capacity(self, account):
        if account != "codex-subscription":
            return dict(stopped=False, ready_at=0)
        now = self.clock()
        if self.reset_at is not None and (now >= self.reset_at or now-self.last_refresh_at >= WAIT_REFRESH_SECONDS):
            self.reset_at = None
            # External resets may restore capacity before the advertised time.
            # Neither elapsed time nor a reset notification authorizes dispatch
            # without fresh native capacity evidence.
            self.refresh()
        limits = self.client.quota_status
        state = window_status(limits, now)
        self.reset_at = state["ready_at"] or None
        return state
