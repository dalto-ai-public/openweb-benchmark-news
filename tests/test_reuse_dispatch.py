"""Self-contained date-controlled reuse, using real collection and ledger owners."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest

from openweb_news import fixtures, request_profiles, reuse
from openweb_news.common import digest, encoded, load, records, write_private
from openweb_news.ledger import Ledger
from openweb_news.preparation import prepare
from openweb_news.runner import run
from .test_provider_dates import dated


class DispatchReuseTests(unittest.TestCase):
    def source(self, root):
        cfg = fixtures.config(root/'source-run')
        cfg.update(provider_ids=['brave-web'], tie_order=['brave-web'], questions=[dated()],
            provider_date_profiles={'brave-web': request_profiles.MIXED_START_PROFILE})
        cfg['execution_policy']['scheduling']['accounts'].pop('provider/OPENWEB_NEWS_EXA_API_KEY')
        source = prepare(cfg, mode='fixture')
        write_private(root/'source.json', source)
        report = run(source)
        return cfg, source, report

    def target(self, cfg, root, ref):
        cfg = deepcopy(cfg)
        cfg.update(run_directory=str(root/'target-run'), reuse_snapshot=ref)
        return prepare(cfg, mode='fixture')

    def test_freeze_import_audit_resume_preserves_bytes_outcomes_and_cost_once(self):
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            cfg, source, original = self.source(root)
            ref = reuse.freeze(root/'source.json', root/'snapshot')
            receipt = next((root/'source-run/search-dispatch').glob('*.json'))
            saved = receipt.read_bytes()
            target = self.target(cfg, root, ref)
            with patch.object(fixtures, 'search', side_effect=AssertionError('search repeated')), \
                    patch.object(fixtures, 'model', side_effect=AssertionError('judgment repeated')):
                result = run(target)
                first_ledger = (root/'target-run/attempts.jsonl').read_bytes()
                resumed = run(target)
            self.assertEqual(result['outcomes'], original['outcomes'])
            self.assertEqual(result['scores'], original['scores'])
            self.assertEqual(resumed['outcomes'], result['outcomes'])
            self.assertEqual(resumed['execution']['committed_and_held_usd'], original['execution']['committed_and_held_usd'])
            self.assertEqual(first_ledger, (root/'target-run/attempts.jsonl').read_bytes())
            self.assertEqual((root/'target-run/search-dispatch'/receipt.name).read_bytes(), saved)
            self.assertEqual((root/'snapshot/search-dispatch'/receipt.name).read_bytes(), saved)
            self.assertEqual(request_profiles.audit_dispatch(target, root/'target-run')['verified_attempts'], 1)
            sent = next((root/'source-run/attempts').glob('*/effective-search-request.json')).read_bytes()
            copied = next((root/'target-run/attempts').glob('*/effective-search-request.json')).read_bytes()
            self.assertEqual(sent, copied)
            self.assertEqual(result['collection'], original['collection'])
            self.assertIn('freshness', load(receipt)['effective_request']['body'])

    def test_missing_changed_or_unbound_receipts_fail_closed(self):
        for change in ('missing', 'changed', 'unbound', 'effective_unbound', 'symlink'):
            with self.subTest(change=change), TemporaryDirectory() as temp:
                root = Path(temp).resolve()
                cfg, _, _ = self.source(root)
                ref = reuse.freeze(root/'source.json', root/'snapshot')
                path = next((root/'snapshot/search-dispatch').glob('*.json'))
                if change == 'missing': path.unlink()
                elif change == 'changed': path.write_bytes(b'{}')
                elif change == 'symlink':
                    original = path.read_bytes(); path.unlink()
                    write_private(root/'outside.json', original); path.symlink_to(root/'outside.json')
                else:
                    manifest = load(ref['manifest_path'])
                    manifest['files'] = [r for r in manifest['files'] if not (
                        r['path'].startswith('search-dispatch/') if change == 'unbound' else
                        r['path'].endswith('/effective-search-request.json'))]
                    write_private(ref['manifest_path'], manifest, replace=True)
                    ref['sha256'] = digest(encoded(manifest))
                with self.assertRaises((ValueError, FileNotFoundError)):
                    self.target(cfg, root, ref)

    def test_interrupted_import_resumes_and_sealed_loss_is_refused(self):
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            cfg, _, _ = self.source(root)
            ref = reuse.freeze(root/'source.json', root/'snapshot')
            target = self.target(cfg, root, ref)
            original = reuse.write_private
            def interrupted(path, value, **kwargs):
                if Path(path).parent.name == 'search-dispatch':
                    raise OSError('synthetic interrupted copy')
                return original(path, value, **kwargs)
            binding = dict(package_sha256=digest(target), mode='fixture')
            with Ledger(root/'target-run', binding).locked() as ledger:
                with patch.object(reuse, 'write_private', side_effect=interrupted), self.assertRaises(OSError):
                    reuse.materialize(target, ledger)
                self.assertFalse((root/'target-run/reuse.json').exists())
                reuse.materialize(target, ledger)
                self.assertEqual(request_profiles.audit_dispatch(target, root/'target-run')['verified_attempts'], 1)
                next((root/'target-run/search-dispatch').glob('*.json')).unlink()
                with self.assertRaisesRegex(ValueError, 'sealed import lost'):
                    reuse.materialize(target, ledger)

    def test_unfinished_send_retains_anchor_and_rejects_lost_or_changed_evidence(self):
        for change in ('none', 'missing_anchor', 'changed_effective'):
            with self.subTest(change=change), TemporaryDirectory() as temp:
                root = Path(temp).resolve()
                self.source(root)
                ledger = root/'source-run/attempts.jsonl'
                events = [e for e in records(ledger) if not (e['event'] == 'finished' and e['task_id'].startswith('search-'))]
                write_private(ledger, b''.join(encoded(e) for e in events), replace=True)
                if change == 'missing_anchor':
                    next((root/'source-run/search-dispatch').glob('*.json')).unlink()
                if change == 'changed_effective':
                    path = next((root/'source-run/attempts').glob('*/effective-search-request.json'))
                    write_private(path, {}, replace=True)
                if change == 'none':
                    ref = reuse.freeze(root/'source.json', root/'snapshot')
                    manifest = reuse.read_snapshot(ref)
                    self.assertEqual(manifest['statistics']['uncertain_attempts'], 1)
                    self.assertEqual(len([r for r in manifest['files'] if r['path'].startswith('search-dispatch/')]), 1)
                else:
                    with self.assertRaisesRegex(ValueError, 'effective'):
                        reuse.freeze(root/'source.json', root/'snapshot')
