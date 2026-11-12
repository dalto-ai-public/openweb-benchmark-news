"""A retained verdict must come from a successful native completion."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest

from openweb_news import source_audit,judging
from openweb_news.common import load,write_private,digest,encoded
from openweb_news.checkpoint import COST_KEYS
from . import test_date_replay


class SourceAuditTests(unittest.TestCase):
    def test_failed_native_verdict_cannot_authenticate_copied_source(self):
        for classification,success in [('normal',True),('recovered',True),('failed_or_incomplete',False),('failed_or_incomplete',True)]:
            with self.subTest(classification=classification,success=success),TemporaryDirectory()as temp:
                root=Path(temp);args=test_date_replay.DateReplayTests().setup_source(root)
                review=load(root/'pipeline/package.json');case=load(root/'pipeline/inputs/case.json')
                case['verdicts']={'codex':case['verdicts']['codex']};write_private(root/'pipeline/inputs/case.json',case,replace=True)
                review['cases'][0]['sha256']=digest((root/'pipeline/inputs/case.json').read_bytes())
                run=root/'native/run';package=dict(configuration=dict(run_directory=str(run)),code={},assets={})
                write_private(root/'native/package.json',package);review['source_package_sha256']=digest(package)
                write_private(root/'pipeline/package.json',review,replace=True)
                costs={k:'0'for k in COST_KEYS};report=load(args['source_report']);report.update(collection=[],phase_costs=costs)
                write_private(args['source_report'],report,replace=True)
                write_private(run/'report.json',dict(package_sha256=digest(package),source_versions=dict(code={},assets={}),collection=[]))
                cid=case['compiled']['packet']['case_id'];write_private(run/'compiled'/(cid+'.json'),case['compiled'])
                request=dict(role='codex',blind_packet=judging.blind_packet(case['compiled']))
                raw=dict(verdict=case['verdicts']['codex'],transport_success=success,dispatch_state='completed'if success else 'uncertain')
                write_private(run/'attempts/a/request.json',request);write_private(run/'attempts/a/response.json',raw)
                event=dict(event='finished',attempt_id='a',task_id='codex-'+cid,result_path='attempts/a/response.json',result_sha256=digest(raw),request_sha256=digest(request))
                write_private(run/'attempts.jsonl',encoded(event))
                proof=dict(unclassified=[],counts={},records=[dict(attempt_id='a',classification=classification)])
                with patch('openweb_news.source_audit.inspect_ledger',return_value=proof),patch('openweb_news.source_audit.report_costs',return_value=costs):
                    if classification in {'normal','recovered'}:
                        self.assertEqual(source_audit.audit(args['source_report'],[root/'pipeline/package.json'],[root/'native/package.json'])['firstpass_positions'],1)
                    else:
                        with self.assertRaisesRegex(ValueError,'provenance incomplete'):
                            source_audit.audit(args['source_report'],[root/'pipeline/package.json'],[root/'native/package.json'])
