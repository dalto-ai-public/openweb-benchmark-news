"""Offline candidate diagnostics over authenticated, completed publication reviews."""
from collections import Counter
from pathlib import Path

from . import publication
from .common import code_bindings, digest, load, now, private_directory, write_private
from .settlement import PROFILE as SETTLEMENT_PROFILE, load_parent, _completed_responses

PROFILE = 'publication-checker-replay-diagnostic-v1'


def replay(source_report, directory):
    """Never dispatch, adopt grades, or write a replacement benchmark report."""
    source = Path(source_report).resolve()
    root = Path(directory).resolve()
    source_root = source.parent.parent
    if root.is_relative_to(source_root) or source.is_relative_to(root):
        raise ValueError('diagnostics must be outside their source run')
    source_sha = digest(source.read_bytes())
    report, cases, _ = load_parent(source)
    if (report['mode'] != SETTLEMENT_PROFILE
            or report['publication_review']['profile'] != publication.PROFILE):
        raise ValueError('completed original publication review required')
    package = load(source_root / 'package.json')
    responses = _completed_responses(package)
    outcomes = {o['case_id']: o for o in report['outcomes']}
    previous = {r['case_id']: r for r in report['publication_review']['records']}
    rows, gains = [], Counter()
    candidate_counts = Counter(report['outcome_counts'])
    for row in package['roster']:
        cid = row['case_id']
        compiled = cases[cid]['compiled']
        packet = publication.packet_for(compiled)
        own = {role: responses['publication-' + role + '-' + cid] for role in ('codex', 'claude')}
        if any(digest(raw) != previous[cid]['provenance'][role]['response_sha256'] for role, raw in own.items()):
            raise ValueError('saved review response binding differs')
        old = publication.corroborate(compiled, packet, own)
        if any(old[key] != previous[cid][key] for key in ('accepted', 'grade', 'terminal', 'audits')):
            raise ValueError('frozen checker replay differs')
        candidate = publication.corroborate(compiled, packet, own, checker_profile=publication.CHECKER_PROFILE)
        if old['accepted'] and (not candidate['accepted'] or candidate['grade'] != old['grade']):
            raise ValueError('checker changed a previously accepted review')
        gained = candidate['accepted'] and outcomes[cid]['grade'] is None
        if gained:
            gains[str(candidate['grade'])] += 1
            candidate_counts['unknown'] -= 1
            candidate_counts[str(candidate['grade'])] += 1
        rows.append(dict(case_id=cid, configuration_id=outcomes[cid]['configuration_id'],
            stratum=row['route']['pattern'], current_grade=outcomes[cid]['grade'],
            candidate_grade=candidate['grade'], candidate_accepted=candidate['accepted'],
            would_change_unknown=bool(gained), audits=candidate['audits'],
            original_review_record_sha256=digest(previous[cid]),
            response_sha256={role: digest(raw) for role, raw in own.items()}))
    if digest(source.read_bytes()) != source_sha:
        raise ValueError('source report changed during replay')

    def counts(items):
        return dict(Counter(str(r['candidate_grade']) if r['candidate_accepted'] else 'unresolved' for r in items))

    summary = dict(profile=PROFILE, checker_profile=publication.CHECKER_PROFILE,
        generated_at=now(), code=code_bindings(), source_report_sha256=source_sha,
        source_package_sha256=digest(package), source_report_path=str(source),
        candidate_not_adopted=True, model_calls=0, search_calls=0, pro_calls=0,
        completed_review_responses_replayed=2 * len(rows), cases=len(rows),
        candidate_pair_dispositions=counts(rows), additional_candidate_grades=dict(gains),
        current_outcome_counts=report['outcome_counts'], candidate_outcome_counts=dict(candidate_counts),
        by_provider={key: counts([r for r in rows if r['configuration_id'] == key])
                     for key in sorted({r['configuration_id'] for r in rows})},
        by_stratum={key: counts([r for r in rows if r['stratum'] == key])
                    for key in sorted({r['stratum'] for r in rows})},
        validation_counts=dict(Counter(role + ':' + ('valid' if r['audits'][role]['valid'] else 'invalid')
            for r in rows for role in ('codex', 'claude'))),
        source_report_unchanged=True, previous_accepted_reviews_unchanged=True,
        limits='Diagnostic candidate only. No benchmark grades are adopted. Source spans and model agreement do not establish accuracy; negative-control calibration remains a separate proposed validation.')
    root = private_directory(root)
    write_private(root / 'case-audits.json', rows)
    summary['case_audits_sha256'] = digest((root / 'case-audits.json').read_bytes())
    write_private(root / 'summary.json', summary)
    lines = ['# Offline publication checker diagnostic', '',
        'Candidate only; the benchmark report and all original responses remain unchanged.', '',
        f"Replayed {summary['completed_review_responses_replayed']} completed responses; no model calls.",
        f"Candidate pair dispositions: {summary['candidate_pair_dispositions']}.",
        f"Additional candidate grades: {summary['additional_candidate_grades']}.", '', summary['limits'], '']
    write_private(root / 'summary.md', '\n'.join(lines).encode())
    return summary
