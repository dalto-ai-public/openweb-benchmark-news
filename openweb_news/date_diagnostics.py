"""Saved-capture date diagnostics. No collection, grading or trust activation.

Witnesses are lexical URL dates and explicitly labeled own-text publication
datelines. They are selected, dependent observations, not publication truth.
"""
from collections import Counter
from copy import deepcopy
from datetime import date, datetime
from email.utils import parsedate_to_datetime
import json
import math
from pathlib import Path
import re
from urllib.parse import urlsplit

from . import date_reading, date_trust
from .common import digest, load
from ._method import text_dates

PROFILE = "saved-native-date-diagnostics-v1"
LEGACY_SCREENING_PROFILE = "exact-day-required-datelines-v1"
CALENDAR_SCREENING_PROFILE = "within-one-day-supporting-datelines-v2"
_URL_DATE = re.compile(r"(?<!\d)((?:19|20|21)\d{2})[-/](\d{1,2})[-/](\d{1,2})(?!\d)")
_CUE = re.compile(r"\b(?P<cue>originally published|first published|publication date|published|posted|last updated|updated|modified|indexed|crawled|fetched|acquired|event date)\b\s*(?:on\b\s*|at\b\s*|[:–—-]\s*)?", re.I)
_NEGATIVE = {"last updated", "updated", "modified", "indexed", "crawled", "fetched", "acquired", "event date"}
_THRESHOLDS = {"minimum_unique_urls", "minimum_url_agreement", "minimum_dateline_agreement", "maximum_native_later_rate"}
_DIAGNOSTIC_CONTROLS = {
    ("seltz-web-pro", "properties.fetched_at"): dict(
        purpose="Explicit diagnostic negative-control cohort; not a first-publication admission candidate.",
        basis="Official SDK names this property where stored by an index; the name does not independently establish fetch semantics.",
        source="https://pypi.org/pypi/seltz/1.18.0/json", revision="1.18.0", native_meaning_reclassified=False),
}


def _reading(value):
    """Shared parser plus stated-calendar extraction, including existing RFC support."""
    if not isinstance(value, str) or not value.strip():
        return dict(state="null_or_empty" if value in (None, "") else "non_text", day=None, reading=None)
    parsed = date_reading.parse(value)
    if parsed.get("type") == "ambiguous_numeric" or parsed.get("alternatives"):
        return dict(state="ambiguous", day=None, reading=parsed)
    if not parsed.get("interval"):
        return dict(state="unparseable", day=None, reading=parsed)
    if parsed.get("precision") not in {"day", "minute", "second", "fractional_second"}:
        return dict(state="coarse_precision", day=None, reading=parsed)
    normalized = parsed.get("normalized") or value
    try:
        if re.match(r"^\d{4}-\d{2}-\d{2}", normalized):
            day = date.fromisoformat(normalized[:10]).isoformat()
            parser = "shared_calendar_or_ISO"
        else:
            day = parsedate_to_datetime(value).date().isoformat()
            parser = "existing_RFC_date_support"
    except (ValueError, TypeError, OverflowError):
        return dict(state="unparseable_calendar_day", day=None, reading=parsed)
    return dict(state="parsed", day=day, parser=parser, reading=parsed)


def _url_witness(url):
    values = []
    invalid = []
    if not isinstance(url, str) or not url:
        return dict(state="missing_url", day=None, dates=[], invalid_dates=[])
    try:
        path = urlsplit(url).path
    except ValueError:
        return dict(state="invalid_url", day=None, dates=[], invalid_dates=[])
    for match in _URL_DATE.finditer(path):
        try:
            current = date(*map(int, match.groups())).isoformat()
            values.append(dict(quote=match[0], day=current, start=match.start(), end=match.end()))
        except ValueError:
            invalid.append(dict(quote=match[0], start=match.start(), end=match.end()))
    distinct = {v["day"] for v in values}
    return dict(state="ambiguous_multiple_dates" if len(distinct) > 1 else "parsed" if distinct else "invalid_date" if invalid else "no_path_date",
                day=next(iter(distinct)) if len(distinct) == 1 else None, dates=values, invalid_dates=invalid)


def _datelines(packet):
    publications, negative, unlabeled, rejected = [], [], [], []
    for field in packet.get("allowed_text", []):
        text = field["text"]
        visible, positions = text_dates.visible_text(text)
        covered = []
        cues = list(_CUE.finditer(visible))
        for cue_index, match in enumerate(cues):
            before = visible[:match.start()].rstrip(" \t")
            anchored = not before or before[-1] in "\n|•;"
            negative_role = match["cue"].lower() in _NEGATIVE
            tail_end = next((i for i in range(match.end(), len(visible)) if visible[i] in "\n|•;"), len(visible))
            if cue_index + 1 < len(cues):
                tail_end = min(tail_end, cues[cue_index + 1].start())
            tail = visible[match.end():tail_end].strip()
            # A labeled sentence about something published elsewhere is not an
            # own dateline. Require a complete date at the start of the value.
            start = re.sub(r"^(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|Mon|Tue|Wed|Thu|Fri|Sat|Sun)[,]?\s+", "", tail, flags=re.I)
            token = date_reading.DATE.match(start)
            reading = _reading(tail) if token else dict(state="unparseable_dateline", day=None, reading=None)
            if token:
                clock = date_reading.CLOCK.match(start[token.end():])
                end = token.end() + (clock.end() if clock else 0)
                if start[end:].strip(" \t.,"):
                    reading.update(state="ambiguous_dateline_context", day=None)
            raw_start = positions[match.start()] if positions else match.start()
            raw_end = positions[tail_end - 1] + 1 if tail_end > match.start() else raw_start
            row = dict(field=field["field"], cue=match["cue"], quote=text[raw_start:raw_end], start=raw_start, end=raw_end, **reading)
            if not negative_role and not anchored:
                row.update(state="ambiguous_subject_or_event_context", day=None)
                rejected.append(row)
            else:
                (negative if negative_role else publications).append(row)
            covered.append((match.start(), tail_end))
        for match in date_reading.DATE.finditer(visible):
            if not any(a <= match.start() < b for a, b in covered):
                unlabeled.append(dict(field=field["field"], quote=match[0], start=positions[match.start()],
                    end=positions[match.end() - 1] + 1, role="unlabeled_or_event_context_not_publication_witness"))
    days = {r["day"] for r in publications if r["day"]}
    unresolved = any(not r["day"] for r in publications)
    state = "ambiguous_datelines" if len(days) > 1 or days and unresolved or any(r["state"] == "ambiguous" for r in publications) else "parsed" if len(days) == 1 else "unparseable_dateline" if publications else "no_labeled_publication_dateline"
    return dict(state=state, day=next(iter(days)) if len(days) == 1 and not unresolved else None,
                publication_datelines=publications, negative_control_datelines=negative, unlabeled_dates=unlabeled,
                rejected_publication_contexts=rejected)


def _comparison(native_day, witness_day):
    if native_day is None or witness_day is None:
        return dict(state="not_comparable", delta_days=None)
    delta = (date.fromisoformat(native_day) - date.fromisoformat(witness_day)).days
    return dict(state="agree" if delta == 0 else "native_later" if delta > 0 else "native_earlier", delta_days=delta,
                exact_day_agreement=delta == 0, within_one_day=abs(delta) <= 1,
                later_more_than_one_day=delta > 1)


def _validate_thresholds(thresholds):
    if thresholds is None:
        return None
    if not isinstance(thresholds, dict) or not thresholds or set(thresholds) - _THRESHOLDS:
        raise ValueError("date diagnostic thresholds require known explicit metric names")
    for key, value in thresholds.items():
        if key == "minimum_unique_urls":
            valid = type(value) is int and value >= 0
        else:
            valid = type(value) in {int, float} and math.isfinite(value) and 0 <= value <= 1
        if not valid:
            raise ValueError("invalid date diagnostic threshold value")
    return deepcopy(thresholds)


def screening_definitions(profile=LEGACY_SCREENING_PROFILE):
    """Version suggestion meanings separately from unchanged diagnostic counts."""
    if profile == LEGACY_SCREENING_PROFILE:
        return dict(profile=profile, url_agreement_metric="url_exact_day_agreement_rate",
            dateline_agreement_metric="dateline_exact_day_agreement_rate",
            native_later_metric="native_later_rate",
            agreement="Exact stated calendar day.", native_later="Native stated calendar day is later by any positive number of days.",
            dateline_support="A requested dateline threshold is mandatory; missing or undersized samples are insufficient evidence.")
    if profile == CALENDAR_SCREENING_PROFILE:
        return dict(profile=profile, url_agreement_metric="url_within_one_day_rate",
            dateline_agreement_metric="dateline_within_one_day_rate",
            native_later_metric="witness_later_more_than_one_day_rate",
            agreement="Absolute difference between stated calendar days is at most one day, inclusive; no timezone is inferred.",
            native_later="Native stated calendar day is more than one day later than the comparable witness.",
            dateline_support="Apply a requested dateline threshold only when dateline comparable unique URLs meet the caller's explicit minimum_unique_urls; sparse datelines are reported supporting evidence and do not block a suggestion.")
    raise ValueError("unknown date diagnostic screening profile")


def _suggestion(metrics, thresholds, candidate, *, negative_control=False,
                screening_profile=LEGACY_SCREENING_PROFILE):
    definitions = screening_definitions(screening_profile)
    _validate_screening_thresholds(screening_profile, thresholds)
    if thresholds is None:
        return dict(state="not_requested", thresholds=None, trust_activated=False,
            screening_profile=screening_profile, definitions=definitions)
    checks = []
    minimum = thresholds.get("minimum_unique_urls")
    if minimum is not None:
        checks.append(dict(metric="minimum_unique_urls", observed=metrics["witness_comparable_unique_urls"], required=minimum,
                           state="met" if metrics["witness_comparable_unique_urls"] >= minimum else "insufficient_evidence"))
    for key, metric, denominator, lower in (
        ("minimum_url_agreement", definitions["url_agreement_metric"], "url_comparable_unique_urls", True),
        ("minimum_dateline_agreement", definitions["dateline_agreement_metric"], "dateline_comparable_unique_urls", True),
        ("maximum_native_later_rate", definitions["native_later_metric"], "witness_comparable_unique_urls", False)):
        if key not in thresholds:
            continue
        observed, n = metrics[metric], metrics[denominator]
        if (screening_profile == CALENDAR_SCREENING_PROFILE and key == "minimum_dateline_agreement"
                and (not n or n < minimum)):
            checks.append(dict(metric=key, metric_definition=metric, observed=observed, denominator=n,
                required=thresholds[key], minimum_unique_urls=minimum, blocking=False,
                state="insufficient_supporting_evidence"))
            continue
        state = ("insufficient_evidence" if observed is None or minimum is not None and n < minimum else
                 "met" if (observed >= thresholds[key] if lower else observed <= thresholds[key]) else "unmet")
        checks.append(dict(metric=key, metric_definition=metric, observed=observed, denominator=n,
            required=thresholds[key], blocking=True, state=state))
    state = ("insufficient_evidence" if any(c["state"] == "insufficient_evidence" for c in checks) else
             "does_not_meet_requested_thresholds" if any(c["state"] == "unmet" for c in checks) else "meets_requested_thresholds")
    return dict(state=state, thresholds=deepcopy(thresholds), checks=checks, trust_activated=False,
        screening_profile=screening_profile, definitions=definitions,
        role_recommendation="not_a_reviewed_publication_admission_candidate" if candidate is False or negative_control else "owner_exact_field_review_required",
        limits="Caller thresholds suggest diagnostic review only; they are not an owner receipt, grading rule or dispatch gate.")


def _validate_screening_thresholds(profile, thresholds):
    if profile == CALENDAR_SCREENING_PROFILE and thresholds is not None:
        required = {"minimum_unique_urls", "minimum_url_agreement", "maximum_native_later_rate"}
        if not required <= thresholds.keys():
            raise ValueError("calendar screening requires explicit minimum_unique_urls, minimum_url_agreement and maximum_native_later_rate")


def _metrics(rows):
    urls = {}
    for row in rows:
        if row["url"]:
            urls.setdefault(row["url"], []).append(row)
    unique = []
    for url, same in urls.items():
        native_days = {r["native"]["day"] for r in same if r["native"]["day"]}
        native_bad = any(not r["native"]["day"] for r in same)
        native_day = next(iter(native_days)) if len(native_days) == 1 and not native_bad else None
        dateline_days = {r["dateline_witness"]["day"] for r in same if r["dateline_witness"]["day"]}
        dateline_bad = any(r["dateline_witness"]["state"] in {"ambiguous_datelines", "unparseable_dateline"} for r in same)
        dateline = next(iter(dateline_days)) if len(dateline_days) == 1 and not dateline_bad else None
        url_day = same[0]["url_witness"]["day"]
        witnesses = {d for d in (url_day, dateline) if d}
        combined = next(iter(witnesses)) if len(witnesses) == 1 else None
        unique.append(dict(url=url, occurrences=len(same), native_day=native_day,
            repeated_native_conflict=len(native_days) > 1, repeated_dateline_conflict=len(dateline_days) > 1,
            witness_conflict=len(witnesses) > 1, url_comparison=_comparison(native_day, url_day),
            dateline_comparison=_comparison(native_day, dateline), witness_comparison=_comparison(native_day, combined)))
    result = dict(occurrences=len(rows), unique_urls=len(urls), missing_url_occurrences=sum(not r["url"] for r in rows),
        native_parse_states=dict(Counter(r["native"]["state"] for r in rows)),
        url_witness_states=dict(Counter(r["url_witness"]["state"] for r in rows)),
        dateline_witness_states=dict(Counter(r["dateline_witness"]["state"] for r in rows)),
        negative_control_datelines=sum(len(r["dateline_witness"]["negative_control_datelines"]) for r in rows),
        repeated_native_conflict_urls=sum(r["repeated_native_conflict"] for r in unique),
        repeated_dateline_conflict_urls=sum(r["repeated_dateline_conflict"] for r in unique),
        conflicting_witness_urls=sum(r["witness_conflict"] for r in unique))
    for name in ("url", "dateline", "witness"):
        comparable = [r[name + "_comparison"] for r in unique if r[name + "_comparison"]["delta_days"] is not None]
        occurrences = [r[name + "_comparison"] for r in rows if name != "witness" and r[name + "_comparison"]["delta_days"] is not None] if name != "witness" else []
        result[name + "_comparable_unique_urls"] = len(comparable)
        for metric, predicate in (("exact_day_agreement", lambda d: d == 0), ("within_one_day", lambda d: abs(d) <= 1),
                                  ("native_later", lambda d: d > 0), ("later_more_than_one_day", lambda d: d > 1)):
            count = sum(predicate(c["delta_days"]) for c in comparable)
            result[name + "_" + metric + "_unique_urls"] = count
            result[name + "_" + metric + "_rate"] = count / len(comparable) if comparable else None
            if name != "witness":
                result[name + "_" + metric + "_occurrences"] = sum(predicate(c["delta_days"]) for c in occurrences)
        if name != "witness":
            result[name + "_comparable_occurrences"] = len(occurrences)
    result["native_later_rate"] = result["witness_native_later_rate"]
    return result, unique


def inspect(compiled_directory, *, source_package_path=None, source_report_path=None,
            case_configurations=None, configurations=None, thresholds=None,
            screening_profile=LEGACY_SCREENING_PROFILE):
    """Return replayable private diagnostics from explicitly selected saved inputs.

    Paired native source package/report is preferred. The alternative is an
    explicit case-ID -> configuration-ID mapping and exact configuration map.
    No history, credential file, provider endpoint or model is consulted.
    """
    thresholds = _validate_thresholds(thresholds)
    definitions = screening_definitions(screening_profile)
    _validate_screening_thresholds(screening_profile, thresholds)
    directory = Path(compiled_directory).expanduser().resolve()
    if not directory.is_dir():
        raise ValueError("compiled diagnostics require an explicit existing directory")
    refs, failures, source_outcomes = [], [], {}
    if bool(source_package_path) != bool(source_report_path):
        raise ValueError("date diagnostics require paired source package and native report")
    if source_package_path:
        if case_configurations is not None or configurations is not None:
            raise ValueError("select source package/report or explicit configuration mapping")
        package_path, report_path = Path(source_package_path).resolve(), Path(source_report_path).resolve()
        package, report = load(package_path), load(report_path)
        if report.get("package_sha256") != digest(package):
            raise ValueError("date diagnostics source report/package binding differs")
        refs.extend([dict(path=str(p), sha256=digest(p.read_bytes())) for p in (package_path, report_path)])
        configurations = {p["configuration_id"]: p for p in package["providers"]}
        case_configurations = {}
        for outcome in report["outcomes"]:
            cid, case_id = outcome["configuration_id"], outcome["case_id"]
            if case_id in case_configurations and case_configurations[case_id] != cid:
                raise ValueError("date diagnostics source occurrence has conflicting configurations")
            case_configurations[case_id] = cid
            if case_id in source_outcomes and source_outcomes[case_id] != outcome:
                raise ValueError("date diagnostics source occurrence has conflicting outcome bindings")
            source_outcomes[case_id] = outcome
    configurations = configurations or {}
    case_configurations = case_configurations or {}
    if isinstance(configurations, list):
        configurations = {p["configuration_id"]: p for p in configurations}
    field_registry = {cid: date_trust.registry([config])["providers"][0]["fields"] for cid, config in configurations.items()}
    paths = sorted(directory.glob("*.json"))
    grouped, records, observed_cases = {}, [], set()
    if not paths:
        failures.append(dict(state="no_compiled_capture_evidence"))
    for path in paths:
        ref, local_rows = dict(path=str(path)), []
        try:
            raw = path.read_bytes()
            ref["sha256"] = digest(raw)
            refs.append(ref)
            compiled = json.loads(raw)
            packet = compiled["packet"]
            case_id = packet["case_id"]
            observed_cases.add(case_id)
            if path.stem != case_id:
                raise ValueError("compiled filename and packet occurrence differ")
            if compiled.get("packet_sha256") != digest(packet) or compiled.get("input_sha256") != digest(compiled["input"]):
                raise ValueError("saved compiled packet/input hash binding differs")
            outcome = source_outcomes.get(case_id, {})
            if outcome.get("packet_sha256") and outcome["packet_sha256"] != digest(packet):
                raise ValueError("compiled packet and source report outcome binding differ")
            if outcome.get("question_id") and outcome["question_id"] != packet["question"]["question_id"]:
                raise ValueError("compiled question and source report outcome differ")
            cid = case_configurations.get(case_id)
            if cid not in configurations:
                raise ValueError("missing exact configuration mapping")
            config = configurations[cid]
            if config.get("configuration_id") != cid:
                raise ValueError("explicit configuration identity differs")
            config_hash = digest(config)
            applicability = compiled.get("input", {}).get("publication_estimate_applicability")
            if applicability and applicability.get("configuration_sha256") != config_hash:
                raise ValueError("compiled applicability configuration binding differs")
            url = packet.get("returned_url")
            if url is not None and not isinstance(url, str):
                raise ValueError("saved returned URL is not text or null")
            url_witness, dateline = _url_witness(url), _datelines(packet)
            from .judging import native_publication_fields
            admitted = native_publication_fields(compiled)
            observations = packet["date_observations"]
            if not observations:
                local_rows.append(dict(case_id=case_id, configuration_id=cid, source=ref, state="no_native_date_observations"))
            for observation in observations:
                field = observation["relative_path"]
                graded_views = {profile: {r['field']: r for r in date_reading.native_reading_rows(
                    [observation], admitted, profile=profile)}
                    for profile in (date_reading.LATEST_PROFILE, date_reading.NATIVE_LITERAL_PROFILE)}
                if observation.get("mapping_id") and observation["mapping_id"] != digest(dict(configuration=config,
                        field=field, kind=observation["date_kind"], derivation=observation["derivation"])):
                    raise ValueError("native observation configuration/mapping binding differs")
                native = _reading(observation.get("raw_value"))
                field_info = field_registry[cid].get(field, {})
                diagnostic_control = _DIAGNOSTIC_CONTROLS.get((cid, field))
                role = dict(saved_date_kind=observation.get("date_kind"), saved_derivation=observation.get("derivation"),
                    current_registry_admission_candidate=field_info.get("admission_candidate"),
                    current_documented_meaning=field_info.get("documented_meaning"),
                    negative_control=observation.get("date_kind") in {"crawl", "index", "update", "fetch"}
                        or field_info.get("date_kind") in {"crawl", "index", "update", "fetch"} or diagnostic_control is not None,
                    diagnostic_control_basis=deepcopy(diagnostic_control),
                    authority_changed=False)
                row = dict(case_id=case_id, configuration_id=cid, configuration_sha256=config_hash, relative_path=field,
                    source=ref, url=url, native_observation=deepcopy(observation), native=native,
                    grading_reading=dict(
                        explicitly_admitted=admitted is not None and observation['field'] in admitted,
                        original_native_interval_resolved=bool(observation.get('interval')),
                        by_profile={profile: dict(interval_resolved=bool(view[observation['field']].get('interval')),
                            diagnostic_resolved_but_grading_unresolved=bool(native.get('day')) and not bool(view[observation['field']].get('interval')))
                            for profile, view in graded_views.items()},
                        limit='Parser compatibility only; field admission, role, applicability and conflicts still require their normal checks.'),
                    url_witness=url_witness, dateline_witness=dateline, date_role=role,
                    url_comparison=_comparison(native["day"], url_witness["day"]),
                    dateline_comparison=_comparison(native["day"], dateline["day"]),
                    negative_control_comparisons=[dict(field=d["field"], cue=d["cue"], comparison=_comparison(native["day"], d["day"])) for d in dateline["negative_control_datelines"]])
                local_rows.append(row)
            for row in local_rows:
                row["included_in_metrics"] = "relative_path" in row
                records.append(row)
                if row["included_in_metrics"]:
                    grouped.setdefault((cid, config_hash, row["relative_path"]), []).append(row)
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
            records.extend(dict(row, included_in_metrics=False) for row in local_rows)
            failures.append(dict(source=ref, state="unprocessed_or_partial_capture", reason=str(error), error_type=type(error).__name__, source_read_error=isinstance(error, OSError)))
    expected_cases = {cid for cid, row in source_outcomes.items() if row.get("packet_sha256")}
    missing_cases = sorted(expected_cases - observed_cases)
    failures.extend(dict(case_id=case_id, state="source_report_case_missing_compiled_capture") for case_id in missing_cases)
    groups = []
    for (cid, config_hash, field), rows in sorted(grouped.items()):
        metrics, unique = _metrics(rows)
        groups.append(dict(configuration_id=cid, configuration_sha256=config_hash, relative_path=field,
            date_roles=sorted({r["date_role"]["saved_date_kind"] for r in rows}, key=str),
            negative_control=any(r["date_role"]["negative_control"] for r in rows),
            current_registry_admission_candidate=rows[0]["date_role"]["current_registry_admission_candidate"],
            metrics=metrics, unique_url_observations=unique,
            grading_reader_coverage={profile: dict(
                occurrences=len(rows), explicitly_admitted_occurrences=sum(r['grading_reading']['explicitly_admitted'] for r in rows),
                interval_resolved_occurrences=sum(r['grading_reading']['by_profile'][profile]['interval_resolved'] for r in rows),
                diagnostic_resolved_but_grading_unresolved_occurrences=sum(r['grading_reading']['by_profile'][profile]['diagnostic_resolved_but_grading_unresolved'] for r in rows))
                for profile in (date_reading.LATEST_PROFILE, date_reading.NATIVE_LITERAL_PROFILE)},
            suggestion=_suggestion(metrics, thresholds, rows[0]["date_role"]["current_registry_admission_candidate"],
                negative_control=any(r["date_role"]["negative_control"] for r in rows), screening_profile=screening_profile)))
    unchanged = not any(f.get("source_read_error") for f in failures)
    for ref in refs:
        try:
            same = digest(Path(ref["path"]).read_bytes()) == ref["sha256"]
        except OSError:
            same = False
        if not same:
            unchanged = False
            failures.append(dict(source=ref, state="source_changed_during_diagnostics"))
    if paths != sorted(directory.glob("*.json")):
        unchanged = False
        failures.append(dict(state="compiled_directory_listing_changed"))
    for group in groups:
        group["suggestion"]["diagnostic_complete"] = not failures
        if failures:
            group["suggestion"]["scope_caveat"] = "Incomplete source coverage or capture failures; numbers describe only the retained valid subset."
    return dict(profile=PROFILE, diagnostic_complete=not failures, source_artifacts_unchanged=unchanged,
        compiled_directory=str(directory), compiled_files=len(paths), source_files=refs, records=records, failures=failures, groups=groups,
        coverage=dict(source_report_cases=len(source_outcomes), source_report_cases_with_packet_hash=len(expected_cases),
            observed_packet_cases=len(observed_cases), missing_expected_cases=missing_cases),
        explicit_mapping_sha256=digest(dict(case_configurations=case_configurations, configurations=configurations)),
        thresholds=thresholds, screening_profile=screening_profile, screening_definitions=definitions,
        trust_activated=False, grades_changed=False, network_calls=0,
        analysis=dict(agreement="Exact stated calendar day, plus separately reported historical within-one-day/later-more-than-one-day counts.",
            denominators="Exact returned URL strings grouped per exact configuration and native relative field; conflicting repeat native days and conflicting witnesses are retained but excluded from affected determinate denominators.",
            parsing="Shared date reader and existing RFC support; population and denominators can differ from the older ISO-only census.",
            datelines="Only line/separator-start Published/Posted/Publication date labels with an immediately following complete date; unlabeled/event/update dates do not become publication witnesses.",
            limits="Selected URL/dateline agreement is not independent publication truth, source credibility, historical availability, owner admission or a grading rule. Source hashes and shallow occurrence/configuration bindings are checked; old execution history is not replayed."))
