"""Explicit, hash-bound recovery of named interrupted attempts."""
from copy import deepcopy
import json
from pathlib import Path

from .common import digest, load, records

KEY = "interrupted_run_recovery"
PROFILE = "named-http-interruption-recovery-v1"
SUBSCRIPTION_PROFILE = "named-subscription-interruption-recovery-v1"
SUBSCRIPTION_FILES = {"interruption.py", "runner.py"}
CREDIT_PROFILE = "named-prepayment-restoration-v1"
CREDIT_FILES = SUBSCRIPTION_FILES | {'ledger.py', 'caching.py', 'scheduling.py'}
CHANGED_FILES = {"interruption.py", "ledger.py", "preparation.py", "reuse.py",
                 "runner.py", "scheduling.py", "transport.py", "recovery.py"}


def attempts(package):
    proof = package.get("configuration", {}).get(KEY)
    if not proof:
        return []
    source = _source(proof)
    return attempts(source) + proof["attempts"]


def _source(proof):
    from .reuse import safe_file
    ref = proof["source_snapshot"]
    path = Path(ref["manifest_path"])
    if digest(safe_file(path.parent, path.name).read_bytes()) != ref["sha256"]:
        raise ValueError("interruption source manifest changed")
    manifest = load(path)
    expected = next(f["sha256"] for f in manifest["files"] if f["path"] == "source-package.json")
    raw = safe_file(path.parent, "source-package.json").read_bytes()
    if digest(raw) != expected:
        raise ValueError("interruption source package changed")
    source = load(path.parent / "source-package.json")
    if digest(source) != manifest["source_package_sha256"] or digest(source) != proof["source_package_sha256"]:
        raise ValueError("interruption source package binding differs")
    return source


def validate(package):
    """Validate immutable evidence and scope; this never grants live approval."""
    proof = package["configuration"].get(KEY)
    if proof is None:
        return
    keys = {"profile", "source_snapshot", "source_package_sha256", "source_code_sha256",
            "target_code_sha256", "attempts", "reason"}
    if (not isinstance(proof, dict) or set(proof) != keys or proof["profile"] not in {PROFILE, SUBSCRIPTION_PROFILE, CREDIT_PROFILE}
            or not isinstance(proof["reason"], str) or not proof["reason"].strip()
            or not isinstance(proof["attempts"], list) or not proof["attempts"]):
        raise ValueError("explicit named HTTP interruption recovery required")
    from .reuse import safe_file
    ref = proof["source_snapshot"]
    if not isinstance(ref, dict) or set(ref) != {"manifest_path", "sha256"}:
        raise ValueError("interruption recovery requires its immutable source snapshot")
    path = Path(ref["manifest_path"])
    raw = safe_file(path.parent, path.name).read_bytes()
    if digest(raw) != ref["sha256"]:
        raise ValueError("interruption source manifest changed")
    manifest = load(path)
    root = path.parent
    bound = {f["path"]: f["sha256"] for f in manifest["files"]}

    def retained(name):
        raw = safe_file(root, name).read_bytes()
        if digest(raw) != bound.get(name):
            raise ValueError("interruption evidence hash differs")
        return load(root / name)

    source = retained("source-package.json")
    if source["configuration"].get(KEY):
        validate(source)
    if (digest(source) != proof["source_package_sha256"]
            or digest(source) != manifest["source_package_sha256"]
            or digest(source["code"]) != proof["source_code_sha256"]
            or digest(package["code"]) != proof["target_code_sha256"]):
        raise ValueError("interruption source/target identity differs")
    changed = {k for k in source["code"].keys() | package["code"].keys()
               if source["code"].get(k) != package["code"].get(k)}
    subscription = proof['profile'] in {SUBSCRIPTION_PROFILE, CREDIT_PROFILE}
    allowed = CREDIT_FILES if proof['profile'] == CREDIT_PROFILE else SUBSCRIPTION_FILES if subscription else CHANGED_FILES
    if not changed <= allowed:
        raise ValueError("interruption recovery changed code outside its narrow scope")
    for key in ("mode", "assets", "primary", "providers", "schema_plan"):
        if source[key] != package[key]:
            raise ValueError("interruption recovery changed frozen method/provider work")
    if digest((root / "events.jsonl").read_bytes()) != bound.get("events.jsonl"):
        raise ValueError("interruption source ledger changed")
    events = list(records(root / "events.jsonl"))
    starts = {e["attempt_id"]: e for e in events if e["event"] == "started"}
    ends = {e["attempt_id"]: e for e in events if e["event"] == "finished"}
    if starts.keys() != ends.keys():
        raise ValueError("interruption recovery requires a drained source")
    seen = set(); credit_rows = 0
    for row in proof["attempts"]:
        if (not isinstance(row, dict) or set(row) != {"task_id", "prior_attempt_id", "request_sha256", "prior_result_sha256"}
                or row["prior_attempt_id"] in seen):
            raise ValueError("distinct exact interrupted attempts required")
        aid = row["prior_attempt_id"]
        seen.add(aid)
        start, end = starts.get(aid, {}), ends.get(aid, {})
        if (start.get("task_id") != row["task_id"] or end.get("task_id") != row["task_id"]
                or start.get("request_sha256") != row["request_sha256"]
                or end.get("result_sha256") != row["prior_result_sha256"]):
            raise ValueError("named interrupted task/result binding differs")
        request = retained(f"attempts/{aid}/request.json")
        response = retained(end["result_path"])
        credit = proof['profile'] == CREDIT_PROFILE and credit_rejection(request, response)
        if credit:
            credit_rows += 1
            if not row['task_id'].startswith('cache-create-'):
                raise ValueError('credit restoration requires a cache creation rejection')
        eligible = (request.get('kind') == 'model' and request.get('role') == 'codex'
            and request.get('backend') == 'codex-subscription'
            and response.get('error_type') in {'RuntimeStopped', 'FileNotFoundError'}
            and response.get('dispatch_state') == 'uncertain'
            and response.get('fatal_configuration') is True) if subscription else (
                request.get('kind') in {'search', 'model'}
                and (request['kind'] != 'model' or request.get('backend') == 'gemini-api')
                and response.get('dispatch_state') in {'uncertain', 'not_sent'}
                and response.get('error_type') in {'gaierror', 'ConnectionRefusedError', 'TimeoutError', 'OSError'})
        if proof['profile'] == CREDIT_PROFILE and not eligible:
            eligible = (request.get('kind') == 'model' and request.get('role') == 'pro'
                and request.get('backend') == 'gemini-api'
                and response.get('dispatch_state') in {'uncertain', 'not_sent'}
                and response.get('error_type') in {'gaierror', 'ConnectionRefusedError', 'TimeoutError', 'OSError'})
        if (digest(request) != row["request_sha256"]
                or not (credit or eligible)
                or not credit and (response.get("complete") is not False
                    or response.get("transport_success") is not False or response.get("http_status") not in {None, 200})
                or response.get('verdict') is not None):
            raise ValueError("recovery can name only eligible interrupted sends, never completed judgments")
        task_starts = [e for e in events if e["event"] == "started" and e["task_id"] == row["task_id"]]
        if (task_starts[-1]["attempt_id"] != aid
                or len(task_starts) >= package["configuration"]["execution_policy"]["max_attempts_per_task"]):
            raise ValueError("named recovery must target the latest interrupted attempt within the attempt bound")
    if proof['profile'] == CREDIT_PROFILE and not credit_rows:
        raise ValueError('credit restoration requires an exact depleted-prepayment rejection')


def compatible(source, target):
    old, new = source["configuration"].get(KEY), target["configuration"].get(KEY)
    if old and old == new:
        validate(target)
        return False
    if old and not new:
            raise ValueError("continuation changed or dropped adopted interruption recovery")
    if not new:
        return False
    validate(target)
    if (new["source_package_sha256"] != digest(source)
            or new["source_snapshot"] != target["configuration"].get("reuse_snapshot")):
        raise ValueError("interruption transition must use its exact drained source")
    for key in ("questions", "question_bindings", "prompt_profiles", "scope"):
        if source[key] != target[key]:
            raise ValueError("interruption recovery may not expand or change questions")
    mutable = {"run_directory", "reuse_snapshot", KEY}
    if new['profile'] in {SUBSCRIPTION_PROFILE, CREDIT_PROFILE}:
        _subscription_choices(source, target, allowed=CREDIT_FILES if new['profile'] == CREDIT_PROFILE else SUBSCRIPTION_FILES)
        mutable |= {'live_profile_authority', 'method_acceptance_package'}
    if ({k: v for k, v in source["configuration"].items() if k not in mutable}
            != {k: v for k, v in target["configuration"].items() if k not in mutable}):
        raise ValueError("interruption recovery changed settings, budget or policies")
    return True


def subscription_only(package):
    return package.get('configuration', {}).get(KEY, {}).get('profile') == SUBSCRIPTION_PROFILE


def credit_rejection(request, response):
    """A completed cache billing rejection, never an inferred model failure."""
    if (request.get('kind') != 'cache' or request.get('role') != 'pro'
            or request.get('wire', {}).get('method') != 'POST'
            or request.get('wire', {}).get('endpoint') != 'https://generativelanguage.googleapis.com/v1beta/cachedContents'
            or response.get('dispatch_state') != 'completed' or response.get('complete') is not True
            or response.get('transport_success') is not False or response.get('http_status') != 402
            or response.get('verdict') is not None or response.get('credential_echo_redacted')):
        return False
    try:
        text = response['raw_text']
        if digest(text.encode()) != response['retained_sha256']: return False
        error = json.loads(text)['error']
        return (error.get('code') == 402 and error.get('status') == 'RESOURCE_EXHAUSTED'
            and isinstance(error.get('message'), str)
            and error['message'].startswith('Your prepayment credits are depleted.'))
    except (KeyError, ValueError, TypeError, AttributeError):
        return False


def _subscription_choices(source, target, *, allowed=SUBSCRIPTION_FILES):
    """Rebind orchestration code only; retain authenticated old method receipts."""
    from .clean_run import _same_choices
    from .profile_authority import read
    old = source['configuration'].get('live_profile_authority')
    new = target['configuration'].get('live_profile_authority')
    if bool(old) != bool(new):
        raise ValueError('subscription recovery changed method authority presence')
    if old:
        path = Path(old['package_path']); method = load(path); receipt = load(path.parent/'approval.json')
        if (digest(method) != old['package_sha256'] or digest(receipt) != old['approval_sha256']
                or receipt.get('approved') is not True or receipt.get('accept_method') is not True
                or not receipt.get('approved_by') or not receipt.get('approved_at')
                or receipt.get('package_sha256') != digest(method)
                or method['profile_bindings'] != old['profile_bindings']
                or old['profile_bindings']['semantic_code'] != source['code']
                or new != read(new['package_path'], target['configuration']['reviewers'])
                or old['mode'] != new['mode'] or old['method_options'] != new['method_options']):
            raise ValueError('subscription recovery method receipts differ')
        _same_choices(old, new, allowed=allowed)


def prepare_subscription(source_path, reference, directory, attempts, *, reason, accepted_profile=None):
    """Prepare only Codex recovery and eligible Claude work; no live calls.

    The source must be drained. Pro stays pending, and this native child cannot
    produce a final lifecycle gate while its required Pro judgments are missing.
    """
    return _prepare_native_recovery(source_path, reference, directory, attempts,
        reason=reason, accepted_profile=accepted_profile, profile=SUBSCRIPTION_PROFILE)


def prepare_credit_restoration(source_path, reference, directory, attempts, *, reason, accepted_profile=None):
    """Prepare an exact cache-credit retry plus normal remaining native judging."""
    return _prepare_native_recovery(source_path, reference, directory, attempts,
        reason=reason, accepted_profile=accepted_profile, profile=CREDIT_PROFILE)


def _prepare_native_recovery(source_path, reference, directory, attempts, *, reason, accepted_profile, profile):
    from .common import code_bindings, write_private
    from .preparation import prepare
    from .reuse import read_snapshot
    source_path = Path(source_path).resolve(); source = load(source_path)
    root = Path(directory).resolve(); source_run = Path(source['configuration']['run_directory']).resolve()
    if root.exists() or root.is_relative_to(source_run) or source_run.is_relative_to(root):
        raise ValueError('subscription recovery requires a separate new directory')
    manifest = read_snapshot(reference)
    if manifest['source_package_sha256'] != digest(source):
        raise ValueError('subscription recovery snapshot belongs to another source')
    cfg = deepcopy(source['configuration'])
    cfg.update(run_directory=str(root/'run'), reuse_snapshot=reference,
        questions=source['questions'], provider_ids=[p['configuration_id'] for p in source['providers']])
    if accepted_profile is not None:
        from .profile_authority import read
        cfg['live_profile_authority'] = read(accepted_profile, cfg['reviewers'])
        cfg['method_acceptance_package'] = cfg['live_profile_authority']['package_path']
    cfg[KEY] = dict(profile=profile, source_snapshot=reference,
        source_package_sha256=digest(source), source_code_sha256=digest(source['code']),
        target_code_sha256=digest(code_bindings()), attempts=deepcopy(attempts), reason=reason)
    package = prepare(cfg, mode=source['mode'])
    # Authenticate the transition even when no prior run has been materialized.
    compatible(source, package)
    write_private(root/'config.json', cfg); write_private(root/'package.json', package)
    scope = ('Only named Codex resends and eligible Claude judgments. No searches or Pro calls.'
        if profile == SUBSCRIPTION_PROFILE else
        'Exact named depleted-credit cache rejection retry, optional named Codex resends and remaining normal native judging. '
        'No repeated successful judgments or new questions; original spending ceiling applies.')
    write_private(root/'approval-template.json', dict(decision='NOT_APPROVED', package_sha256=digest(package),
        approved_by='', approved_at='', scope=scope+' Retain original attempts, accounting and possible duplicate subscription inference.'))
    return package
