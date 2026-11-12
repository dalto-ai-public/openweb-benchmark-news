from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest

from openweb_news import clean_run, settlement
from openweb_news.common import digest, file_digest, file_manifest, verify_manifest, load, write_private
from openweb_news.preparation import prepare
from . import test_clean_run
from .test_independent_method import accepted_fixture


class LeanCheckpointTests(unittest.TestCase):
    def test_parallel_streaming_hashes_are_deterministic_and_fail_on_tamper(self):
        with TemporaryDirectory() as temp:
            files = [Path(temp)/str(i) for i in range(12)]
            for i, path in enumerate(files): path.write_bytes(bytes([i])*100000)
            serial = file_manifest(files, workers=1)
            self.assertEqual(serial, file_manifest(reversed(files), workers=4))
            self.assertEqual(serial[0]['sha256'], digest(Path(serial[0]['path']).read_bytes()))
            files[3].write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, 'changed'): verify_manifest(serial)
            files[3].write_bytes(bytes([3])*100000)
            self.assertEqual(verify_manifest(serial),serial)
            files[3].unlink()
            with self.assertRaises(FileNotFoundError): verify_manifest(serial)
            files[3].symlink_to(files[0])
            with self.assertRaises(ValueError): file_digest(files[3])

    def setup_prefix(self, root):
        helper = test_clean_run.CleanRunTests()
        cfg, selection, archive, _, order = helper.setup_run(root, counts=(1,1,1))
        cfg, _ = accepted_fixture(root/'authority', cfg)
        write_private(root/'template.json', prepare(cfg, mode='fixture'), replace=True)
        old = clean_run.prepare(root/'template.json', selection, root/'old', ceiling='100',
            accepted_profile=cfg['method_acceptance_package'], archived_reference=archive,
            stage_counts=(1,), mode='fixture')
        approval = dict(decision='approved', package_sha256=digest(old), approved_by='synthetic owner', approved_at='fixture')
        first = clean_run.run(root/'old/clean-run.json', approval)
        helper.review(root/'old/clean-run.json', first)
        prefix = clean_run.bootstrap_checkpoint(root/'old/stage-1/gate-evidence.json',
            root/'old/stage-1/stage-review.json', root/'prefix',
            evidence_roots=[root/'old/stage-1', root/'old/stage-1-accepted-review'],
            case_packages=[root/'old/stage-1/review/package.json'],
            native_package=root/'old/stage-1/native/package.json', accepted_profile=cfg['method_acceptance_package'])
        p = clean_run.prepare(root/'template.json', selection, root/'lean', ceiling='100',
            accepted_profile=cfg['method_acceptance_package'], archived_reference=archive,
            stage_counts=(1,1), mode='fixture', verified_prefix=prefix)
        approval = dict(decision='approved', package_sha256=digest(p), approved_by='synthetic owner', approved_at='fixture')
        return helper, p, approval, first

    def test_remaining_stages_preserve_prefix_costs_and_never_replay_it(self):
        from decimal import Decimal
        with TemporaryDirectory() as temp, patch('openweb_news.runner.reservation',
                side_effect=lambda request, package: Decimal('0.10') if request['kind']=='search' else Decimal('0')):
            root=Path(temp).resolve()
            helper,p,approval,old = self.setup_prefix(root)
            path=root/'lean/clean-run.json'
            with patch('openweb_news.clean_run._recollection', side_effect=AssertionError('historical replay')):
                first=clean_run.run(path,approval)
                self.assertEqual(first['state'],'awaiting_stage_review')
                a=load(old['evidence']['report']['path']);b=load(first['evidence']['report']['path'])
                self.assertEqual(b['outcomes'][:len(a['outcomes'])],a['outcomes'])
                self.assertEqual(len(b['questions']),2)
                from openweb_news.accounting import report_costs
                native=load(root/'lean/stage-1/native/package.json')
                added=report_costs(native,Path(native['configuration']['run_directory']))
                for key in clean_run.checkpoint.COST_KEYS:
                    self.assertEqual(Decimal(b['phase_costs'][key]),Decimal(a['phase_costs'][key])+Decimal(added[key]))
                # Completed gate resume and verdict record may not rerun native
                # execution, ordinary review or the old parent replay.
                with patch('openweb_news.clean_run.run_native',side_effect=AssertionError('duplicate native work')), \
                     patch('openweb_news.checkpoint.completed_review',side_effect=AssertionError('duplicate review audit')):
                    self.assertEqual(clean_run.run(path,approval),first)
                    helper.review(path,first,gate_sha256=first['gate_sha256'])
                # Crash after the durable verdict but before exposing its prefix.
                (root/'lean/stage-1/prefix.json').unlink()
                second=clean_run.run(path,approval)
                self.assertTrue((root/'lean/stage-1/prefix.json').exists())
                self.assertEqual(second['stage_index'],2)
                self.assertEqual(len(load(second['evidence']['report']['path'])['questions']),3)
                helper.review(path,second,gate_sha256=second['gate_sha256'])
                self.assertEqual(clean_run.run(path,approval)['state'],'completed')
                from openweb_news import operator
                self.assertTrue(operator.verify(path)['verified'])
                self.assertEqual(operator.status(path)['state'],'completed')

    def test_tampered_raw_evidence_blocks_dispatch_without_replay(self):
        with TemporaryDirectory() as temp:
            root=Path(temp).resolve();helper,p,approval,old=self.setup_prefix(root)
            prefix=load(p['verified_seed']['path']);closure=load(prefix['closure']['path'])
            raw=next(Path(r['path']) for r in closure['files'] if r['path'].endswith('/result.json'))
            raw.write_bytes(raw.read_bytes()+b' ')
            with patch('openweb_news.clean_run.run_native',side_effect=AssertionError('dispatch before trust')):
                with self.assertRaisesRegex(ValueError,'changed'):
                    clean_run.run(root/'lean/clean-run.json',approval)
            self.assertFalse((root/'lean/stage-1').exists())

    def test_raw_response_tamper_is_rejected_by_bootstrap_and_resume(self):
        with TemporaryDirectory() as temp:
            root=Path(temp).resolve();helper,p,approval,old=self.setup_prefix(root)
            prefix=load(p['verified_seed']['path']);closure=load(prefix['closure']['path'])
            raw=next(Path(r['path']) for r in closure['files'] if r['path'].endswith('/response.bin'))
            raw.write_bytes(raw.read_bytes()+b' ')
            with self.assertRaisesRegex(ValueError,'changed'):
                clean_run.run(root/'lean/clean-run.json',approval)
            with self.assertRaisesRegex(ValueError,'changed'):
                clean_run.bootstrap_checkpoint(root/'old/stage-1/gate-evidence.json',root/'old/stage-1/stage-review.json',root/'bad-prefix',
                    evidence_roots=[root/'old/stage-1',root/'old/stage-1-accepted-review'],
                    case_packages=[root/'old/stage-1/review/package.json'],native_package=root/'old/stage-1/native/package.json',
                    accepted_profile=p['accepted_profile']['package_path'])

    def test_gate_byte_tamper_blocks_record_and_stop_blocks_next_stage(self):
        with TemporaryDirectory() as temp:
            root=Path(temp).resolve();helper,p,approval,old=self.setup_prefix(root)
            path=root/'lean/clean-run.json';first=clean_run.run(path,approval)
            gate=root/'lean/stage-1/gate-evidence.json';before=gate.read_bytes();gate.write_bytes(before+b' ')
            with self.assertRaisesRegex(ValueError,'exact gate file bytes'):
                helper.review(path,first,gate_sha256=first['gate_sha256'])
            gate.write_bytes(before)
            helper.review(path,first,decision='stop',gate_sha256=first['gate_sha256'])
            with patch('openweb_news.clean_run.run_native',side_effect=AssertionError('stop bypassed')):
                self.assertEqual(clean_run.run(path,approval)['state'],'stopped_after_review')
            self.assertFalse((root/'lean/stage-2').exists())

    def test_resume_rechecks_older_raw_files_beyond_latest_stage(self):
        with TemporaryDirectory() as temp:
            root=Path(temp).resolve();helper,p,approval,old=self.setup_prefix(root)
            path=root/'lean/clean-run.json';first=clean_run.run(path,approval)
            helper.review(path,first,gate_sha256=first['gate_sha256'])
            seed=load(p['verified_seed']['path']);files=load(seed['closure']['path'])['files']
            raw=next(Path(r['path']) for r in files if r['path'].endswith('/response.bin'))
            original=raw.read_bytes();raw.write_bytes(original+b' ')
            with patch('openweb_news.clean_run.run_native',side_effect=AssertionError('crash trust bypassed')):
                with self.assertRaisesRegex(ValueError,'changed'):clean_run.run(path,approval)
            raw.write_bytes(original)
            second=clean_run.run(path,approval)
            self.assertEqual(second['stage_index'],2)

    def test_unverified_case_dictionary_cannot_mint_a_scoring_snapshot(self):
        with TemporaryDirectory() as temp:
            root=Path(temp).resolve();helper,p,approval,old=self.setup_prefix(root)
            prefix=load(p['verified_seed']['path']);snapshot=load(prefix['snapshot']['path'])
            with self.assertRaisesRegex(ValueError,'fully verified'):
                settlement.snapshot_parent(snapshot['report']['path'],{},snapshot['native'],root/'unverified',case_refs={})
