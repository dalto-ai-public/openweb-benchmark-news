"""Portable derived reports. Private run evidence stays separate from these views."""
import csv
import html
import io
import json

from .common import write_private


def compact_outcome(value):
    """Keep decoded evidence once; original native outputs remain in the ledger.

    Only an identical (or absent) conformance copy is removed. A future genuinely
    different conformance judgment must remain visible.
    """
    for positions in (value.get("positions", {}), value.get("saved_positions", {})):
        for position in positions.values():
            if position.get("conformance_judgment") is None or position.get("conformance_judgment") == position.get("judgment"):
                position.pop("conformance_judgment", None)
            position.pop("raw_verdict", None)
    return value


def write_reports(directory, value):
    from . import returned_fields
    value['returned_fields_analysis'] = returned_fields.analyze(value)
    returned_fields.write(directory, value['returned_fields_analysis'])
    value["report_format_version"] = 2
    value["native_output_location"] = "Private attempt ledger and role/case request/result artifacts; reports retain decoded judgments, grades, validation and available source hashes."
    for outcome in value.get("outcomes", []):
        compact_outcome(outcome)
    write_private(directory / "report.json", value, replace=True)
    accounting = (value.get('scores') or {}).get('accounting', {})
    collected = {r['configuration_id'] for r in value.get('collection', [])}
    partial_providers = bool(accounting and collected != set(accounting))
    if 'resolved_pairwise'in value and not partial_providers:
        pairs=io.StringIO(newline='');writer=csv.writer(pairs)
        writer.writerow(['configuration_a','configuration_b','metric','included_families','excluded_families','mean_paired_difference','wins','ties','losses'])
        for pair in value['resolved_pairwise']:
            writer.writerow([*pair['configurations'],pair['metric'],json.dumps(sorted(pair['family_differences'])),
                             json.dumps(pair['excluded_families'],sort_keys=True),pair['mean_paired_difference'],pair['wins'],pair['ties'],pair['losses']])
        write_private(directory/'pairwise.csv',pairs.getvalue().encode(),replace=True)
    if value.get("execution_stage") == "collection-only-v1" or partial_providers:
        columns = (["configuration_id", "question_id", "status"] if partial_providers else
                   ["configuration_id", "question_id", "status", "native_positions", "capture_time"])
        stream = io.StringIO(newline="")
        writer = csv.writer(stream)
        writer.writerow(columns)
        writer.writerows([[row.get(key, "") for key in columns] for row in value["collection"]])
        write_private(directory / "summary.csv", stream.getvalue().encode(), replace=True)
        rows = "".join("<tr>" + "".join("<td>" + html.escape(str(row.get(k, ""))) + "</td>" for k in columns) + "</tr>" for row in value["collection"])
        heading = 'Partial provider-cell component' if partial_providers else 'Provider integration only'
        limit = ('Only selected provider cells are represented. This component supplies no full-provider ranking or continuation gate. Use the merged checkpoint report.'
                 if partial_providers else 'No judge calls, answer grades or provider quality ranking. Native captures and projections remain private.')
        document = '<!doctype html><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'"><title>'+heading+'</title><h1>'+heading+'</h1><p>'+limit+'</p><table><tr>' + ''.join('<th>'+k+'</th>'for k in columns) + '</tr>' + rows + '</table>'
        document += ''.join('<p>'+html.escape(str(limit))+'</p>' for limit in value.get('report_limits', []))
        document += returned_fields.render_html(value['returned_fields_analysis'])
        write_private(directory / "report.html", document.encode(), replace=True)
        return
    scores = value["scores"]
    columns = ["configuration", "cutoff", "scheduled_slots", "answers", "leads", "unresolved", "duplicates", "failed_slots", "not_collected_slots"]
    rows = []
    for config, cutoffs in scores["accounting"].items():
        for k, accounting in cutoffs.items():
            counts = accounting["counts"]
            rows.append([config, k, accounting["slots"], counts.get("answer", 0), counts.get("lead", 0),
                         counts.get("unresolved", 0), counts.get("duplicate", 0), counts.get("failed_call", 0), counts.get("not_collected", 0)])
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(columns)
    # Configuration IDs are catalog-controlled; no untrusted article text is put
    # into spreadsheet cells, avoiding formula interpretation in this summary.
    writer.writerows(rows)
    write_private(directory / "summary.csv", stream.getvalue().encode(), replace=True)
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(["view", "configuration", "metric", "resolved_family_mean", "whole_scope_status",
                     "resolved_families", "released_families", "resolved_questions", "released_questions",
                     "method_version", "package_sha256"])
    for view in ("conditional_quality", "delivered_utility"):
        for name, metric in scores["aggregates"][view]["metrics"].items():
            for config, entry in metric["per_configuration"].items():
                writer.writerow([view, config, name, entry["resolved_family_mean"], entry["whole_scope"]["status"],
                                 metric["resolved_family_count"], metric["released_family_count"],
                                 metric["resolved_question_count"], metric["released_question_count"],
                                 value["method_version"], value["package_sha256"]])
    write_private(directory / "metrics.csv", stream.getvalue().encode(), replace=True)
    def esc(v):
        return html.escape(str(v), quote=True)
    body = "".join("<tr>" + "".join(f"<td>{esc(v)}</td>" for v in row) + "</tr>" for row in rows)
    head = "".join(f"<th>{esc(c)}</th>" for c in columns)
    avail = scores["availability"]
    summary = f"Availability strategy {esc(avail['strategy'])}; common answered questions {avail['common_question_count']}/{avail['released_question_count']}."
    execution = value.get("execution", {})
    state = execution.get("state", "not supplied")
    held_count = len(execution.get("held_tasks", []))
    progress = f"Execution: {esc(state)}; held tasks: {held_count}; saved tasks reused this invocation: {execution.get('reused_tasks', 0)}."
    if state in {"stopped_at_ceiling", "completed_with_holds"}:
        progress += " This is a partial report. Held or uncollected work does not establish a completed benchmark."
    metrics = []
    for name, metric in scores["aggregates"]["delivered_utility"]["metrics"].items():
        for config, entry in metric["per_configuration"].items():
            metrics.append(f"<tr><td>{esc(config)}</td><td>{esc(name)}</td><td>{esc(entry['resolved_family_mean'])}</td><td>{esc(metric['label'])}</td></tr>")
    document = f"""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<title>News benchmark report</title><style>body{{font:16px system-ui;margin:2rem;max-width:1100px}}table{{border-collapse:collapse;width:100%;margin:1rem 0}}td,th{{padding:.5rem;border:1px solid #ccc;text-align:left}}code{{overflow-wrap:anywhere}}</style>
<h1>News benchmark report</h1><p>{summary}</p>
<p><strong>{progress}</strong></p>
<p>Method: {esc(value['method_version'])}. Mode: {esc(value['mode'])}. Package: <code>{esc(value['package_sha256'])}</code>.</p>
<p>Unknown grades remain unresolved. Duplicate URLs retain their ranks and earn no repeat gain. Empty successful calls count as answered; failed calls deliver zero where the metric is defined.</p>
<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>
<h2>Delivered utility</h2><table><thead><tr><th>Configuration</th><th>Metric</th><th>Resolved family mean</th><th>Denominator</th></tr></thead><tbody>{''.join(metrics)}</tbody></table>
<p>Partial resolved means are not whole-bank scores. Report and event identity breadth can remain pending without reviewed identities. Model agreement is not human accuracy. Full metrics, availability, versions, source validity, original positions and unknown reasons are in report.json and the private run artifacts.</p>
{''.join('<p>'+esc(limit)+'</p>'for limit in value.get('report_limits',[]))}</html>"""
    document = document.replace('</html>', returned_fields.render_html(value['returned_fields_analysis'])+'</html>')
    write_private(directory / "report.html", document.encode(), replace=True)
    if value.get("questions"):
        from .phase_report import write_readable
        write_readable(directory, value)
