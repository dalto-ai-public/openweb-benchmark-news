from collections import Counter
from pathlib import Path
import json
import tempfile
import unittest

from openweb_news import fixtures
from openweb_news.common import code_bindings,asset_bindings,digest,write_private
from openweb_news.preparation import prepare
from openweb_news.runner import run
from openweb_news.review_bundle import provider_unknown_shares,finalize_bundle_report,PROFILE


class BundleReportingTests(unittest.TestCase):
    def test_real_score_table_before_and_after_json_roundtrip(self):
        with tempfile.TemporaryDirectory()as temp:
            report=run(prepare(fixtures.config(Path(temp)/'run'),mode='fixture'))
            expected=Counter(o['configuration_id']for o in report['outcomes']if o['grade']is None)
            live=provider_unknown_shares(report['scores'])
            saved=provider_unknown_shares(json.loads(json.dumps(report['scores'])))
            self.assertEqual(live,saved)
            for cid,entry in live.items():
                self.assertEqual(entry['unknown'],expected[cid])
                self.assertEqual(entry['slots'],len(fixtures.config(Path(temp)/'other')['questions'])*10)
                self.assertEqual(entry['share'],expected[cid]/entry['slots'])

    def test_reporting_repair_cannot_change_decision_code(self):
        with tempfile.TemporaryDirectory()as temp:
            root=Path(temp);code=code_bindings();code['adjudication.py']='changed'
            bundle=dict(profile=PROFILE,code=code,assets=asset_bindings())
            write_private(root/'bundle.json',bundle)
            write_private(root/'approval.json',dict(decision='approved',package_sha256=digest(bundle)))
            with self.assertRaises(ValueError):finalize_bundle_report(root/'bundle.json',root/'output')


if __name__=='__main__':unittest.main()
