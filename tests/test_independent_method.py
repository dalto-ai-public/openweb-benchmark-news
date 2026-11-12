from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest

from openweb_news import blind_input, date_trust, fixtures, profile_authority
from openweb_news.common import digest, load, records, write_private
from openweb_news.preparation import prepare, selected_providers
from openweb_news.runner import run


def accepted_fixture(root, cfg=None):
    cfg = fixtures.config(root / 'run') if cfg is None else deepcopy(cfg)
    providers = selected_providers(cfg, cfg['provider_ids'])
    write_private(root / 'diagnostic.json', dict(synthetic=True, negative_control=True))
    write_private(root / 'source.txt', b'Synthetic method evidence only.\n')
    trust = date_trust.prepare(providers, root / 'trust', diagnostic_paths=[root / 'diagnostic.json'],
                               source_bindings={'synthetic': root / 'source.txt'})
    receipt = load(root / 'trust/approval-template.json')
    receipt.update(approved=True, approved_by='synthetic owner', approved_at='fixture',
        selections={p['configuration_id']: dict(eligible_fields=[], reason='Synthetic explicit exclusion') for p in providers})
    write_private(root / 'trust/approval.json', receipt)
    cfg.update(judge_input_profile=blind_input.LIVE_PROFILE, date_trust_package=str(root / 'trust/package.json'))
    method = profile_authority.prepare_method(cfg, root / 'method', mode='fixture')
    write_private(root / 'method/approval.json', dict(approved=True, accept_method=True,
        approved_by='synthetic owner', approved_at='fixture', package_sha256=digest(method)))
    cfg['method_acceptance_package'] = str(root / 'method/package.json')
    return cfg, method


class IndependentMethodTests(unittest.TestCase):
    def test_real_owner_receipts_freeze_visible_inputs_and_resume_without_calls(self):
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            cfg, method = accepted_fixture(root)
            with patch('openweb_news.settlement.load_parent', side_effect=AssertionError('no old authority import')):
                package = prepare(cfg, mode='fixture')
            self.assertNotIn('source_report_path', method)
            self.assertNotIn('method_acceptance_package', package['configuration'])
            self.assertTrue(all(p['judge_input_profile'] == blind_input.LIVE_PROFILE for p in package['providers']))
            self.assertEqual(package['date_capabilities']['supported_omissions'], [])
            report = run(package)
            self.assertEqual(report['execution']['state'], 'completed')
            retained = {event['task_id']: load(root / 'run' / event['result_path'])
                        for event in records(root / 'run/attempts.jsonl') if event['event'] == 'finished'}
            from openweb_news.review_pipeline import _verdict
            for outcome in report['outcomes']:
                for role, position in outcome['positions'].items():
                    if position.get('state') == 'valid':
                        raw = _verdict(role, retained[role + '-' + outcome['case_id']])
                        self.assertEqual(raw['case_id'], blind_input.MODEL_CASE)
                        self.assertEqual(position['decoder_verdict']['case_id'], outcome['case_id'])
            with patch('openweb_news.fixtures.search', side_effect=AssertionError('no repeat search')), \
                    patch('openweb_news.fixtures.model', side_effect=AssertionError('no repeat judge')):
                self.assertEqual(run(package)['scores'], report['scores'])

    def test_unaccepted_changed_options_and_fixture_authority_cannot_authorize_live(self):
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            cfg, method = accepted_fixture(root)
            for change in (
                lambda value: value.update(provider_ids=list(reversed(value['provider_ids']))),
                lambda value: value.update(date_reading_profile="quoted-date-reading-v4"),
                lambda value: value.update(provider_request_profiles={'brave-web': 'brave-undecorated-v1'}),
                lambda value: value['reviewers']['codex'].update(model='different-model'),
            ):
                altered = deepcopy(cfg)
                change(altered)
                with self.assertRaises(ValueError):
                    prepare(altered, mode='fixture')
            with self.assertRaises(ValueError):
                prepare(cfg, mode='live')
            receipt = load(root / 'method/approval.json')
            receipt['accept_method'] = False
            write_private(root / 'method/approval.json', receipt, replace=True)
            with self.assertRaises(ValueError):
                prepare(cfg, mode='fixture')

    def test_native_literal_reader_requires_explicit_bound_method_option(self):
        with TemporaryDirectory() as temp:
            root=Path(temp).resolve()
            cfg=fixtures.config(root/'run');cfg['date_reading_profile']='quoted-date-reading-v4'
            cfg,method=accepted_fixture(root,cfg)
            self.assertEqual(method['profile_bindings']['date_profile'],'quoted-date-reading-v4')
            self.assertEqual(method['method_options']['date_reading_profile'],'quoted-date-reading-v4')
            package=prepare(cfg,mode='fixture')
            self.assertEqual(package['configuration']['live_profile_authority']['profile_bindings']['date_profile'],
                             'quoted-date-reading-v4')
            altered=deepcopy(cfg);altered.pop('date_reading_profile')
            with self.assertRaises(ValueError):prepare(altered,mode='fixture')

    def test_retrieval_start_selection_is_method_bound_without_changing_provider_identity(self):
        from openweb_news.request_profiles import MIXED_START_PROFILE
        with TemporaryDirectory() as temp:
            root=Path(temp).resolve()
            cfg=fixtures.config(root/'run')
            cfg['provider_ids']=['brave-web','exa-highlights']
            cfg['tie_order']=list(cfg['provider_ids'])
            before=selected_providers(cfg,cfg['provider_ids'])
            cfg['provider_date_profiles']={'brave-web':MIXED_START_PROFILE}
            cfg,method=accepted_fixture(root,cfg)
            self.assertEqual(selected_providers(cfg,cfg['provider_ids']),before)
            self.assertEqual(method['method_options']['provider_date_profiles'],cfg['provider_date_profiles'])
            prepare(cfg,mode='fixture')
            altered=deepcopy(cfg);altered.pop('provider_date_profiles')
            with self.assertRaises(ValueError):prepare(altered,mode='fixture')

    def test_method_acceptance_does_not_approve_execution_or_mutate_input(self):
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            cfg, method = accepted_fixture(root)
            before = deepcopy(cfg)
            package = prepare(cfg, mode='fixture')
            self.assertEqual(cfg, before)
            self.assertEqual(package['approval_state'], 'synthetic_offline_only')
            self.assertFalse(load(root / 'method/approval-template.json')['approved'])
            self.assertEqual(method['profile_bindings']['reviewers'], cfg['reviewers'])
            cfg['reviewers']['codex']['model'] = 'mutated source config'
            self.assertNotEqual(method['profile_bindings']['reviewers'], cfg['reviewers'])


if __name__ == '__main__':
    unittest.main()
