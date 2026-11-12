"""Offline, exact-field owner admission; native date meaning remains independent.

Diagnostics are selected observations, not truth labels or an accuracy threshold.
Admission only permits the common own-occurrence credibility/conflict review.
"""
from copy import deepcopy
from pathlib import Path

from .common import asset_path, digest, load, now, write_private
from .provider_dates import capability

PROFILE = "owner-native-date-trust-v1"
REGISTRY_VERSION = "native-date-meaning-and-admission-v1"
_EXTENSIONS = {"date_trust", "judge_input_profile"}

# These are exact result-relative paths, never aliases or inferred JSON keys.
# Previously reviewed named fields remain candidates; new fields need the same
# explicit owner choice. Negative controls remain available under native roles.
_FIELDS = {
    "exa-highlights": {"publishedDate": ("first_publication", "documented_estimated", True)},
    "parallel-advanced": {"publish_date": ("publication_unspecified", "unspecified", True)},
    "perplexity-web": {"date": ("publication_unspecified", "unspecified", True), "last_updated": ("update", "provider_reported", False)},
    "valyu-search": {"publication_date": ("publication_unspecified", "unspecified", True), "crawl_date": ("crawl", "provider_reported", False)},
    "keenable-pro": {"published_at": ("publication_unspecified", "unspecified", True), "acquired_at": ("index", "provider_reported", False)},
    "seltz-web-pro": {"published_date": ("publication_unspecified", "unspecified", True)},
    "brave-web": {"article.date": ("publication_unspecified", "unspecified", True), "page_age": ("unclear", "unspecified", False), "page_fetched": ("crawl", "provider_reported", False)},
    "tavily-news": {"published_date": ("unclear", "documented_estimated", False)},
    "tavily-general": {"published_date": ("unclear", "documented_estimated", False)},
}
_LIMITATIONS = [
    "URL-date agreement is selected and possibly dependent evidence, not verified original-publication accuracy.",
    "No numerical acceptance threshold or automatic trust follows from diagnostics or retrieval filters.",
    "Owner-selected fields still require the existing per-occurrence credibility, event-role and conflict review.",
    "Crawl, fetch, indexing and update dates do not satisfy the original-publication rule.",
    "Seltz properties.published_date is not established as an alias for top-level Document.published_date.",
]


def configuration_sha256(config):
    return digest({k: v for k, v in config.items() if k not in _EXTENSIONS})


def field_semantics(config, relative_path):
    """Exact documented native role; never an admission or eligibility decision."""
    if not capability(config)["reviewed"]:
        return None
    row = _FIELDS.get(config["configuration_id"], {}).get(relative_path)
    return tuple(row[:2]) if row else None


def registry(providers):
    """Receipt for reviewed exact products and candidate/negative-control fields."""
    rows = []
    for config in providers:
        cap = capability(config)
        fields = _FIELDS.get(config["configuration_id"], {}) if cap["reviewed"] else {}
        docs = deepcopy(cap["docs"])
        if config["configuration_id"] == "brave-web":
            docs.append(dict(url="https://api-dashboard.search.brave.com/api-reference/web/search/get", checked_at="2026-11-05",
                             revision="Exact nested web.results[].article.date describes article publication; page_age is separately publication or modification."))
        if config["configuration_id"] == "seltz-web-pro":
            docs.append(dict(url="https://docs.seltz.ai/api-reference/openapi.json", checked_at="2026-11-05",
                             revision="1.4.1; Document.published_date exact top-level field; rendered request reference has newer coverage"))
        rows.append(dict(configuration_id=config["configuration_id"], configuration_sha256=configuration_sha256(config),
            endpoint=config["endpoint"], method=config["method"], mode=cap["mode"], docs=docs,
            fields={name: dict(date_kind=row[0], derivation=row[1], admission_candidate=row[2],
                documented_meaning=("publication or last update; first publication unresolved" if config["configuration_id"] in {"brave-web", "tavily-news", "tavily-general"} and name in {"page_age", "published_date"} else row[0]),
                recommendation="Consider exact field for the common reviewed-estimate rule; owner decision required." if row[2] else "Do not admit as first-publication evidence under the current rule.") for name, row in fields.items()},
            limitations=deepcopy(_LIMITATIONS)))
    return dict(version=REGISTRY_VERSION, providers=rows,
                source_bindings={name: digest(asset_path(name).read_bytes()) for name in ("method.json", "policies.json", "providers.json")},
                code_bindings={name: digest(Path(__file__).with_name(name).read_bytes()) for name in ("date_trust.py", "provider_dates.py")})


def _roster(providers):
    rows = {p["configuration_id"]: configuration_sha256(p) for p in providers}
    if not rows or len(rows) != len(providers):
        raise ValueError("date trust requires a nonempty unique selected provider roster")
    return rows


def _pin(paths, root, role):
    result = []
    for index, source in enumerate(paths):
        path = Path(source).expanduser().resolve()
        raw = path.read_bytes()
        target = root / "evidence" / f"{role}-{index:03d}.bin"
        write_private(target, raw)
        result.append(dict(role=role, source_path=str(path), pinned_path=str(target), sha256=digest(raw)))
    return result


def prepare(providers, directory, *, diagnostic_paths, source_bindings):
    """Pin reviewed files and prepare an undecided owner selection for every config.

    source_bindings maps descriptive source names to local source-byte paths.
    Neither the package nor its template authorizes trust or paid execution.
    """
    root = Path(directory).expanduser().resolve()
    roster = _roster(providers)
    if diagnostic_paths is None or not source_bindings:
        raise ValueError("date trust preparation requires explicit diagnostics availability and source bytes")
    if any(k in p for p in providers for k in _EXTENSIONS):
        raise ValueError("prepare date trust before attaching trust or judge input profiles")
    diagnostics = _pin(diagnostic_paths, root, "diagnostic")
    sources = _pin(list(source_bindings.values()), root, "source")
    for row, name in zip(sources, source_bindings):
        row["name"] = name
    reg = registry(providers)
    package = dict(profile=PROFILE, created_at=now(), output_directory=str(root), providers=roster,
                   registry=reg, registry_sha256=digest(reg), diagnostics=diagnostics, sources=sources,
                   limitations=deepcopy(_LIMITATIONS),
                   scope="Exact native-field admission only; no query/filter change, judgment, dispatch or spending approval.")
    if not diagnostics:
        package["diagnostics_state"] = "unavailable-first-run"
    write_private(root / "package.json", package)
    write_private(root / "approval-template.json", dict(approved=False, package_sha256=digest(package),
                  approved_by="", approved_at="", selections={cid: None for cid in roster}, scope=package["scope"]))
    return package


def _selections(value, reg):
    rows = {r["configuration_id"]: r for r in reg["providers"]}
    if not isinstance(value, dict) or set(value) != set(rows):
        raise ValueError("date trust owner selection must cover every selected provider exactly")
    selected = {}
    for cid, choice in value.items():
        if not isinstance(choice, dict) or set(choice) != {"eligible_fields", "reason"}:
            raise ValueError("date trust selection is undecided; select exact fields or explicit [] with a reason")
        fields = choice["eligible_fields"]
        if (not isinstance(fields, list) or any(not isinstance(f, str) for f in fields)
                or len(set(fields)) != len(fields) or not isinstance(choice["reason"], str) or not choice["reason"].strip()):
            raise ValueError("invalid exact date trust field selection")
        candidates = {name for name, row in rows[cid]["fields"].items() if row["admission_candidate"]}
        if not set(fields).issubset(candidates):
            raise ValueError("date trust field lacks exact reviewed publication meaning; review its registry entry first")
        selected[cid] = deepcopy(choice)
    return selected


def read(package_path, providers):
    """Verify current registry/configs, retained source bytes and complete owner choice."""
    path = Path(package_path).expanduser().resolve()
    package = load(path)
    approval = load(path.parent / "approval.json")
    reg = registry(providers)
    if (package.get("profile") != PROFILE or package.get("output_directory") != str(path.parent)
            or package.get("providers") != _roster(providers) or package.get("registry") != reg
            or package.get("registry_sha256") != digest(reg)
            or approval.get("approved") is not True or approval.get("package_sha256") != digest(package)
            or not approval.get("approved_by") or not approval.get("approved_at")):
        raise ValueError("date trust owner authority or reviewed registry/configuration differs")
    if (not package.get("sources") or not package.get("diagnostics")
            and package.get("diagnostics_state") != "unavailable-first-run"):
        raise ValueError("date trust reviewed diagnostics/source receipt is missing")
    for row in [*package["diagnostics"], *package["sources"]]:
        pinned = Path(row["pinned_path"]).resolve()
        if not pinned.is_relative_to(path.parent / "evidence") or digest(pinned.read_bytes()) != row["sha256"]:
            raise ValueError("date trust reviewed diagnostic/source bytes differ")
    selected = _selections(approval.get("selections"), reg)
    return dict(profile=PROFILE, package_path=str(path), package_sha256=digest(package),
                receipt_sha256=digest(approval), registry_sha256=digest(reg),
                providers=deepcopy(package["providers"]), selections=selected)


def apply(providers, receipt):
    """Attach verified private admission authority to each selected configuration."""
    if read(receipt["package_path"], providers) != receipt:
        raise ValueError("date trust receipt differs from exact owner authority")
    result = deepcopy(providers)
    for config in result:
        cid = config["configuration_id"]
        config["date_trust"] = dict(profile=PROFILE, state="selected",
            eligible_fields=deepcopy(receipt["selections"][cid]["eligible_fields"]),
            configuration_sha256=configuration_sha256(config), package_sha256=receipt["package_sha256"],
            receipt_sha256=receipt["receipt_sha256"], registry_sha256=receipt["registry_sha256"],
            field_registry_sha256=digest(registry([config])))
    return result


def eligible_fields(config):
    """None preserves legacy behavior; [] is an explicit owner-selected rejection."""
    if "date_trust" not in config:
        return None
    trust = config["date_trust"]
    if (not isinstance(trust, dict) or trust.get("profile") != PROFILE or trust.get("state") != "selected"
            or trust.get("configuration_sha256") != configuration_sha256(config)
            or trust.get("field_registry_sha256") != digest(registry([config]))
            or any(not isinstance(trust.get(k), str) or len(trust[k]) != 64 for k in ("package_sha256", "receipt_sha256", "registry_sha256"))):
        raise ValueError("date trust attachment differs from selected configuration")
    # Validate exact paths without conflating native meaning with admission.
    choice = {"eligible_fields": trust.get("eligible_fields"), "reason": "attached owner selection"}
    _selections({config["configuration_id"]: choice}, registry([config]))
    return deepcopy(trust["eligible_fields"])
