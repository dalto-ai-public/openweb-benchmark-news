"""Read-only native provenance and accounting audit for a saved derived report."""
from decimal import Decimal
from pathlib import Path

from . import judging,review_pipeline
from .accounting import report_costs
from .checkpoint import COST_KEYS
from .common import digest,load,records
from .completion_audit import inspect_ledger


def audit(report_path,review_packages,native_packages):
    report_path=Path(report_path).resolve();source_sha=digest(report_path.read_bytes());report=load(report_path)
    by_id={o['case_id']:o for o in report['outcomes']};required=set();native_bindings=set();case_count=set()
    for path in map(Path,review_packages):
        p=load(path);native_bindings.add(p['source_package_sha256'])
        if p.get('replacement_package_sha256'):native_bindings.add(p['replacement_package_sha256'])
        for ref in p['cases']:
            if ref['case_id']not in by_id:continue
            file=path.parent/ref['path']
            if file.is_symlink()or not file.resolve().is_relative_to(path.parent.resolve())or digest(file.read_bytes())!=ref['sha256']:raise ValueError('source case binding differs')
            case=load(file);compiled=case['compiled'];judging.verify_compiled(compiled)
            if compiled['packet_sha256']!=by_id[ref['case_id']]['packet_sha256']:continue
            case_count.add(ref['case_id'])
            for role,verdict in case['verdicts'].items():required.add((compiled['input_sha256'],role,digest(verdict)))
    if case_count!=set(by_id):raise ValueError('source provenance scope incomplete')
    matched=set();collection_hashes=set();entries=[];seen=set();totals={k:Decimal(0)for k in COST_KEYS}
    for path in map(Path,native_packages):
        path=path.resolve();package=load(path);package_sha=digest(package)
        if package_sha in seen or package_sha not in native_bindings:raise ValueError('duplicate or unbound native accounting package')
        seen.add(package_sha);run=Path(package['configuration']['run_directory']);rpath=run/'report.json';native=load(rpath)
        if native['package_sha256']!=package_sha or native['source_versions']!=dict(code=package['code'],assets=package['assets']):raise ValueError('native source version/report binding differs')
        from .request_profiles import audit_dispatch
        dispatch_proof=audit_dispatch(package,run)
        ledger=run/'attempts.jsonl';ledger_sha=digest(ledger.read_bytes());proof=inspect_ledger(run)
        if proof['unclassified']:raise ValueError('unclassified native source completion')
        completed={r['attempt_id']for r in proof['records']if r['classification']in {'normal','recovered'}}
        collection_hashes.update(digest(row)for row in native['collection'])
        for event in records(ledger):
            if event['event']!='finished':continue
            raw_path=run/event['result_path'];request=load(raw_path.parent/'request.json');raw=load(raw_path)
            if digest(raw_path.read_bytes())!=event['result_sha256']or digest(request)!=event['request_sha256']:raise ValueError('source native attempt binding differs')
            role=request.get('role');task=event['task_id']
            if role not in {'pro','codex','claude'}or not task.startswith(role+'-'):continue
            if raw.get('transport_success')is not True:continue
            if role=='pro':
                if raw.get('complete')is not True or raw.get('http_status')!=200:continue
            elif event['attempt_id']not in completed:continue
            cid=task[len(role)+1:]
            if cid not in by_id:continue
            file=run/'compiled'/(cid+'.json')
            if not file.exists():raise ValueError('native compiled input missing')
            compiled=load(file);judging.verify_compiled(compiled)
            if compiled['packet_sha256']!=by_id[cid]['packet_sha256']:continue
            if role=='pro':
                if request.get('wire')!=judging.build_pro_request(compiled):raise ValueError('original Pro input differs')
            elif request.get('blind_packet')!=judging.blind_packet(compiled):raise ValueError('original subscription input differs')
            try:verdict=review_pipeline._verdict(role,raw)
            except (ValueError,KeyError,IndexError,TypeError):continue
            if verdict is not None:matched.add((compiled['input_sha256'],role,digest(verdict)))
        costs=report_costs(package,run)
        for key in COST_KEYS:totals[key]+=Decimal(costs[key])
        if digest(ledger.read_bytes())!=ledger_sha:raise ValueError('source ledger changed during audit')
        entries.append(dict(package_path=str(path),package_sha256=digest(path.read_bytes()),report_sha256=digest(rpath.read_bytes()),ledger_sha256=ledger_sha,native_counts=proof['counts'],costs=costs,
                            **({'search_dispatch':dispatch_proof} if dispatch_proof['verified_attempts'] or dispatch_proof.get('uncertain_attempt_ids') else {})))
    if required-matched:raise ValueError(f'original native verdict provenance incomplete: {len(required-matched)} positions')
    if any(digest(row)not in collection_hashes for row in report['collection']):raise ValueError('collection does not match original native reports')
    if any(totals[k]!=Decimal(report['phase_costs'][k])for k in COST_KEYS):raise ValueError('recomputed original costs differ')
    if digest(report_path.read_bytes())!=source_sha:raise ValueError('source report changed during audit')
    return dict(profile='saved-native-source-audit-v1',source_report_sha256=source_sha,cases=len(case_count),firstpass_positions=len(required),native=entries,costs={k:str(v)for k,v in totals.items()},new_calls=0,
        limits='Retained requests, native completion proofs, copied verdict identity and provider-reported usage/reservations; not an invoice or human accuracy proof.')
