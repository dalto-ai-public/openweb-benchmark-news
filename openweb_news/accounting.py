"""One owner for conservative admission estimates; never an invoice guarantee."""
from decimal import Decimal
import json
from .common import digest, encoded


USAGE_PROFILE = "source-native-usage-v1"
FIXTURE_PROFILE = "conservative-rate-simulation-v1"


def report_costs(package,directory):
    """Published completed-Pro estimate alongside frozen admission holds."""
    from copy import deepcopy
    from .common import records,load
    starts={e['attempt_id']:e for e in records(directory/'attempts.jsonl')if e['event']=='started'}
    ends={e['attempt_id']:e for e in records(directory/'attempts.jsonl')if e['event']=='finished'}
    published=Decimal(0);conservative=Decimal(0);other=Decimal(0);count=0
    for aid,event in starts.items():
        request=load(directory/'attempts'/aid/'request.json')
        response=load(directory/ends[aid]['result_path'])if aid in ends else{}
        cost=usage_cost(request,response,package)
        if cost is None:
            other+=Decimal(request['reservation_usd'])
            continue
        conservative+=cost;count+=1
        raw=json.loads(response['raw_text']);short=raw['usageMetadata']['promptTokenCount']<=200000
        p=deepcopy(package);p['primary']['accounting_rates'].update(input_per_million_usd='2'if short else'4',output_per_million_usd='12'if short else'18')
        if 'system_cache'in p['configuration']:
            p['configuration']['system_cache']['cached_input_per_million_usd']='0.2'if short else'0.4'
        published+=usage_cost(request,response,p)
    return dict(completed_pro_calls=count,published_completed_pro_usd=str(published),conservative_completed_pro_usd=str(conservative),
                other_conservative_holds_and_allowances_usd=str(other),conservative_total_usd=str(conservative+other),
                published_pro_plus_conservative_residual_usd=str(published+other),
                price_source='https://ai.google.dev/gemini-api/docs/pricing#gemini-3.1-pro-preview',
                billing='Token-price estimates and retained planning holds, not a verified invoice. Subscription usage separate.')


def usage_cost(request, response, package):
    """Source-style completed Pro usage cost; unknown usage releases no hold.

    Apply the package's frozen rates with no assumed cache discount. This is
    usage-derived admission accounting, never an account-specific invoice.
    """
    if (request.get("kind") != "model" or request.get("backend") != "gemini-api"
            or response.get("complete") is not True or response.get("http_status") != 200
            or response.get("dispatch_state") == "uncertain"
            or response.get("error_type") or response.get("credential_echo_redacted")):
        return None
    try:
        raw = response["raw_text"].encode()
        if response.get("retained_sha256") != digest(raw):
            return None
        native = json.loads(raw)
        if native.get("modelVersion") != package["primary"]["model"]:
            return None
        usage = native["usageMetadata"]
        prompt, total, candidate = (usage[k] for k in ("promptTokenCount", "totalTokenCount", "candidatesTokenCount"))
        thought, cached = usage.get("thoughtsTokenCount", 0), usage.get("cachedContentTokenCount", 0)
        if any(type(v) is not int or v < 0 for v in (prompt, total, candidate, thought, cached)):
            return None
        if total < prompt or cached > prompt:
            return None
        output = max(total - prompt, candidate + thought)
        rates = package["primary"]["accounting_rates"]
        input_rate, output_rate = (Decimal(rates[k]) for k in ("input_per_million_usd", "output_per_million_usd"))
        if any(not rate.is_finite() or rate < 0 for rate in (input_rate, output_rate)):
            return None
        cached_rate = input_rate
        if package.get("configuration", {}).get("execution_policy", {}).get("budget_accounting") == "source-native-cache-usage-v1":
            cached_rate = Decimal(str(package["configuration"]["system_cache"]["cached_input_per_million_usd"]))
            if not cached_rate.is_finite() or not 0 <= cached_rate <= input_rate:
                return None
        return ((prompt - cached) * input_rate + cached * cached_rate + output * output_rate) / Decimal(1_000_000)
    except (KeyError, ValueError, TypeError, AttributeError):
        return None


def reservation(request, package):
    kind = request["kind"]
    if package["mode"] == "fixture" and package.get("configuration", {}).get("fixture_accounting") != FIXTURE_PROFILE:
        return Decimal(0)
    if kind == "search":
        provider = next(p for p in package["providers"] if p["configuration_id"] == request["configuration_id"])
        return Decimal(provider["price_usd_per_call"]) * 2
    backend = request["backend"]
    if backend.endswith("-subscription"):
        return Decimal(0)
    if backend == "gemini-api":
        rates = package["primary"]["accounting_rates"]
        input_rate, output_rate = Decimal(rates["input_per_million_usd"]), Decimal(rates["output_per_million_usd"])
        maximum = package["primary"]["settings"]["maxOutputTokens"]
    else:
        config = request["backend_config"]
        input_rate = Decimal(str(config["price_input_per_million_usd"]))
        output_rate = Decimal(str(config["price_output_per_million_usd"]))
        maximum = config["max_output_tokens"]
    # Conservative accounting estimate, not an invoice guarantee or token count.
    # Admission metadata is attached after estimating the semantic request. It
    # must not enlarge that estimate when an eligible retry enters submit again.
    semantic = {key: value for key, value in request.items()
                if key not in {"reservation_usd", "reservation_basis"}}
    estimated_input = len(encoded(semantic)) + 1024
    return (estimated_input * input_rate + maximum * output_rate) / Decimal(1_000_000)
