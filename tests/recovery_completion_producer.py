"""Serialize completed recovery facts using the already accepted frozen runtime.
Test helper copied byte-for-byte to the private execution workspace.
No credentials, dispatch, or replacement runtime implementation.
Run only after the native launcher exits and its ledger drains.
"""
from copy import deepcopy
from pathlib import Path
import argparse
from openweb_news import clean_run, interruption, profile_authority, reuse
from openweb_news.common import code_bindings, digest, load, now, records, write_private
from openweb_news.preparation import verify_package


def capture(package_path, approval_path, directory, *, provenance_restorations=()):
    package_path=Path(package_path).resolve();native=load(package_path)
    root=Path(directory).resolve();run=Path(native['configuration']['run_directory'])
    if root.exists() or root.is_relative_to(run) or native['code']!=code_bindings():
        raise ValueError('matching retained runtime and new separate output required')
    with reuse.source_lock(run):
        approval=load(approval_path);verify_package(native)
        if (approval.get('decision')!='approved' or approval.get('package_sha256')!=digest(native)
                or not approval.get('approved_by') or not approval.get('approved_at')):
            raise ValueError('exact native approval required')
        interruption.validate(native)
        proof=native['configuration'][interruption.KEY];source=interruption._source(proof)
        if not interruption.compatible(source,native):raise ValueError('recovery transition differs')
        authority=profile_authority.read(native['configuration']['live_profile_authority']['package_path'],native['configuration']['reviewers'])
        if authority!=native['configuration']['live_profile_authority']:raise ValueError('native authority differs')
        report_path=run/'report.json';report=load(report_path)
        events=list(records(run/'attempts.jsonl'))
        starts={e['attempt_id']for e in events if e['event']=='started'}
        if (starts!={e['attempt_id']for e in events if e['event']=='finished'}
                or report['package_sha256']!=digest(native) or report['execution']['state']!='completed'
                or report['execution']['retained_started_attempts']!=len(starts)
                or report['source_versions']!=dict(code=native['code'],assets=native['assets'])):
            raise ValueError('native recovery must be complete and drained')
        for ref in provenance_restorations:
            if digest(Path(ref['path']).read_bytes())!=ref['sha256']:raise ValueError('restoration proof changed')
            restoration=load(ref['path'])
            if (restoration['package_sha256']!=digest(native) or restoration['new_searches']!=0
                    or restoration['new_judgments']!=0):raise ValueError('restoration source or calls differ')
            for row in restoration['records']:
                for key in ('source','target'):
                    if digest(Path(row[key]).read_bytes())!=row['sha256']:raise ValueError('restored receipt bytes differ')
        audit=clean_run._audit(native)
        write_private(root/'native-audit.json',audit)
        source_path=Path(proof['source_snapshot']['manifest_path']).parent/'source-package.json'
        bound=clean_run._bound_file
        receipt=dict(profile='completed-native-recovery-validation-v1',recorded_at=now(),validated=True,live_calls=0,
            native_package=bound(package_path),source_native=bound(source_path),
            owner_approval=bound(Path(approval_path).resolve()),report=bound(report_path),
            ledger=bound(run/'attempts.jsonl'),audit=bound(root/'native-audit.json'),
            source_snapshot=deepcopy(proof['source_snapshot']),named_attempts_sha256=digest(proof['attempts']),
            method_sha256=authority['package_sha256'],method_receipt_sha256=authority['approval_sha256'],
            runtime_code=deepcopy(native['code']),runtime_code_sha256=digest(native['code']),
            producer=bound(Path(__file__).resolve()),producer_sha256=digest(Path(__file__).read_bytes()),
            provenance_restorations=list(provenance_restorations))
        write_private(root/'receipt.json',receipt)
    return bound(root/'receipt.json')

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('package');parser.add_argument('approval');parser.add_argument('output')
    parser.add_argument('--restoration',required=True)
    args=parser.parse_args()
    print(capture(args.package,args.approval,args.output,
        provenance_restorations=[clean_run._bound_file(Path(args.restoration).resolve())]))

if __name__=='__main__':main()
