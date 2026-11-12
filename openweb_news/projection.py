"""Explicit saved-response projections, never a runtime container fallback."""
from copy import deepcopy


SEARLO_PROFILES = {"searlo-news": ("searlo-native-news-v1", "news"),
                   "searlo-web": ("searlo-native-organic-v1", "organic")}


def apply_profiles(providers, profiles):
    if not isinstance(profiles, dict):
        raise ValueError("provider projection profiles must be an explicit configuration map")
    selected = {p["configuration_id"] for p in providers}
    if set(profiles) - selected:
        raise ValueError("projection profile names an unselected configuration")
    result = deepcopy(providers)
    for provider in result:
        cid = provider["configuration_id"]
        if cid not in profiles:
            continue
        expected = SEARLO_PROFILES.get(cid)
        if expected is None or profiles[cid] != expected[0] or provider["result_fields"]["container"] != "items":
            raise ValueError("unknown native provider projection profile")
        provider["result_fields"]["container"] = expected[1]
        provider["projection_profile"] = expected[0]
    return result
