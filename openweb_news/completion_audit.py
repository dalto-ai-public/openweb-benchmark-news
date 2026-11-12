"""Offline native-completion regression over an explicit private run ledger."""
from collections import Counter
from pathlib import Path

from .common import asset_bindings, code_bindings, digest, load, now, records, write_private
from .subscription_health import audit, evidence_names, recovered_completion, terminal_verified, excluded_intervention


def inspect_ledger(directory, *, allow_empty=False):
    root=Path(directory).resolve()
    ledger=root/'attempts.jsonl'
    if not ledger.exists() and not allow_empty:raise ValueError('native ledger missing')
    before=digest(ledger.read_bytes()if ledger.exists()else b'');events=list(records(ledger))if ledger.exists()else[]
    starts={e['attempt_id']:e for e in events if e['event']=='started'}
    counts=Counter();rows=[];unclassified=[]
    for event in events:
        if event['event']!='finished':continue
        aid=event['attempt_id'];start=starts[aid]
        path=(root/event['result_path']).resolve();folder=(root/'attempts'/aid).resolve()
        if not path.is_relative_to(folder) or not folder.is_relative_to(root):raise ValueError('unsafe native audit path')
        request=load(folder/'request.json');response=load(path)
        if digest(request)!=start['request_sha256'] or digest(path.read_bytes())!=event['result_sha256'] or start['task_id']!=event['task_id']:
            raise ValueError('native request/result ledger binding changed')
        if request.get('backend') not in {'codex-subscription','claude-subscription'}:continue
        role=request['role'];entry=dict(attempt_id=aid,task_id=event['task_id'],role=role,request_sha256=digest(request),response_sha256=digest(path.read_bytes()))
        if response.get('transport_success')is not True or response.get('dispatch_state')!='completed':
            entry['classification']='failed_or_incomplete'
        else:
            counts[role+':completed']+=1
            if audit(request,response,folder):
                entry.update(classification='normal',files={n:digest((folder/n).read_bytes()) for n in evidence_names(role)})
            else:
                attached=response.get('subscription_completion_evidence') or {}
                proof=(attached if terminal_verified(request,response,folder) else None) if attached.get('normal_health') is False else recovered_completion(request,response,folder)
                if proof:entry.update(classification='recovered',proof=proof)
                else:
                    excluded=excluded_intervention(request,response,folder)
                    if excluded:entry.update(classification='excluded_intervention',proof=excluded)
                    else:entry['classification']='unclassified';unclassified.append(aid)
        counts[role+':'+entry['classification']]+=1;rows.append(entry)
    if before!=digest(ledger.read_bytes()if ledger.exists()else b''):raise ValueError('source ledger changed during audit')
    report=dict(profile='native-completion-ledger-audit-v1',generated_at=now(),source_ledger=str(ledger),source_ledger_sha256=before,
                code=code_bindings(),assets=asset_bindings(),counts=dict(counts),unclassified=unclassified,records=rows,new_calls=0,
                source_artifacts_unchanged=True,limits='Native completion and retained request binding only; no grade change, healthy-account exemption, input-echo claim for Claude, human accuracy or live closure proof.')
    return report


def audit_ledger(directory, output):
    root=Path(directory).resolve();output=Path(output).resolve()
    if output.is_relative_to(root):raise ValueError('audit output must preserve source artifacts')
    report=inspect_ledger(root)
    write_private(output/'audit.json',report)
    return dict(counts=report['counts'],unclassified=len(report['unclassified']),new_calls=0,report=str(output/'audit.json'))
