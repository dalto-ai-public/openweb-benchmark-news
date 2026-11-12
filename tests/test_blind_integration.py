"""Offline proof of the selected model view at every dispatch boundary."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from openweb_news import accepted_review, adjudication, backends, blind_input, date_trust, judging, publication, runner
from openweb_news import fixtures, profile_authority, review_pipeline, settlement
from openweb_news.common import asset_bindings, code_bindings, digest, encoded, load, write_private
from openweb_news.review_pipeline import decode_value
from openweb_news._method import providers, judgment_support
from .helpers import verdict
from .test_blind_input import CAPTURED, TEXT, compiled_for, model_verdict
from . import test_codex_runtime, test_publication


class BlindIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.catalog = providers.configuration_catalog(include_optional=True)
        self.provider = next(c for c in self.catalog if c['configuration_id'] == 'valyu-search')

    def compiled(self, config=None, **options):
        return compiled_for(self.selected(config or self.provider), **options)

    def selected(self, source):
        config = deepcopy(source)
        reg = date_trust.registry([config])
        fields = [name for name, row in reg['providers'][0]['fields'].items() if row['admission_candidate']]
        config['date_trust'] = dict(profile=date_trust.PROFILE, state='selected', eligible_fields=fields,
            configuration_sha256=date_trust.configuration_sha256(config), field_registry_sha256=digest(reg),
            receipt_sha256='a' * 64, package_sha256='b' * 64, registry_sha256=digest(reg))
        config['judge_input_profile'] = blind_input.LIVE_PROFILE
        return config

    def model_value(self, compiled, value=None, source_packet=None):
        projection = (compiled['model_projection'] if source_packet is None else
                      blind_input.project(source_packet, source_packet=compiled['packet'], profile=blind_input.LIVE_PROFILE))
        return model_verdict(value or verdict(compiled), projection)

    def method_authority(self, provider, reviewers):
        binding = profile_authority.live_bindings(reviewers, selection_profile=publication.SELECTION_PROFILE)
        binding.update(judge_input_profile=blind_input.LIVE_PROFILE,
            provider_configurations={provider['configuration_id']: date_trust.configuration_sha256(provider)},
            date_trust=dict(package_sha256=provider['date_trust']['package_sha256'],
                receipt_sha256=provider['date_trust']['receipt_sha256'], selections={provider['configuration_id']:
                    dict(eligible_fields=provider['date_trust']['eligible_fields'])}))
        return dict(profile=profile_authority.METHOD_PROFILE, mode='fixture', package_path='synthetic-method',
            profile_bindings=binding, method_options=dict(judge_input_profile=blind_input.LIVE_PROFILE,
                date_trust_package='synthetic-date-trust'))

    def assert_model_view(self, compiled, packet):
        visible = json.dumps({key: packet[key] for key in ('input', 'schema', 'prompt')}, ensure_ascii=False)
        self.assertIn(TEXT, visible)
        self.assertIn('A&B **Exa**', visible)  # Genuine evidence is not name-sanitized.
        self.assertNotIn(compiled['packet']['case_id'], visible)
        for field in ('mapping_id', 'mapping_version', 'relative_path', 'field_path', 'source_path',
                      'configuration_sha256', 'observation_mapping_id', 'applicability_mapping_id'):
            self.assertNotIn('"' + field + '"', visible)
        for name in ('Valyu', 'Perplexity', 'Tavily', 'Brave', 'Parallel', 'Firecrawl', 'Searlo'):
            self.assertNotIn(name, visible)
        self.assertNotIn('901234', visible)
        self.assertEqual(packet['input']['allowed_text'], compiled['packet']['allowed_text'])
        self.assertEqual(packet['input']['returned_url'], compiled['packet']['returned_url'])
        self.assertEqual(packet['input']['complete_contract'], compiled['packet']['question']['contract'])

    def test_all_catalog_dispatches_and_noise_invariance(self):
        for config in self.catalog:
            with self.subTest(provider=config['configuration_id']):
                left = self.compiled(config)
                right = self.compiled(dict(config, synthetic_identity='changed'), index=2, noise=887766, identity='second')
                before = deepcopy(left)
                ordinary = judging.blind_packet(left)
                self.assert_model_view(left, ordinary)
                self.assertEqual(ordinary, judging.blind_packet(right))
                review = publication.packet_for(left)
                self.assert_model_view(left, review)
                self.assertEqual(review, publication.packet_for(right))
                app = left['input']['publication_estimate_applicability']
                if app:
                    ids = [row['date_id'] for row in left['input']['native_date_ids']
                           if row['relative_path'] in judging.applicability_fields(app)]
                    a = adjudication.packet_for(left, ids)
                    b = adjudication.packet_for(right, ids)
                    self.assert_model_view(left, a)
                    self.assertEqual(a, b)
                pro = judging.build_pro_request(left)['body']
                self.assertEqual(pro['systemInstruction']['parts'][0]['text'], ordinary['prompt'])
                self.assertEqual(json.loads(pro['contents'][0]['parts'][0]['text']), ordinary['input'])
                self.assertEqual(pro['generationConfig']['responseFormat']['text']['schema'],
                                 judgment_support.wire_schema(ordinary['schema']))
                self.assertEqual(left, before)

    def test_legacy_unchanged_and_profile_or_projection_tamper_rejected(self):
        legacy = compiled_for(self.provider, publication_window=False)
        self.assertNotIn('judge_input_profile', legacy)
        self.assertEqual(judging.blind_packet(legacy)['input'], legacy['input'])
        self.assertIn('Valyu', legacy['prompt'])
        current = self.compiled(publication_window=False)
        for mutate in (lambda c: c.update(judge_input_profile=blind_input.PROFILE),
                       lambda c: c.pop('judge_input_profile'),
                       lambda c: c['model_projection']['packet']['input'].update(case_id='other'),
                       lambda c: c['model_projection']['binding']['field_aliases'].update(M1='title')):
            bad = deepcopy(current); mutate(bad)
            with self.assertRaises(ValueError):
                judging.blind_packet(bad)
        old = verdict(legacy)
        self.assertEqual(judging.decode_position(old, current)['state'], 'invalid')
        new = self.model_value(current)
        self.assertEqual(judging.decode_position(new, legacy)['state'], 'invalid')

    def test_explicit_empty_trust_never_inherits_named_field_admission(self):
        legacy = compiled_for(self.provider)
        self.assertIsNotNone(legacy['input']['publication_estimate_applicability'])
        config = self.selected(self.provider)
        config['date_trust']['eligible_fields'] = []
        compiled = compiled_for(config)
        self.assertIsNone(compiled['input']['publication_estimate_applicability'])
        self.assertEqual(publication.packet_for(compiled)['input']['eligible_native_date_ids'], [])
        self.assertEqual(compiled['packet']['date_observations'][0]['date_kind'],
                         legacy['packet']['date_observations'][0]['date_kind'])
        missing = dict(self.provider, judge_input_profile=blind_input.LIVE_PROFILE)
        with self.assertRaisesRegex(ValueError, 'explicit date trust'):
            judging.compile_packet(legacy['packet'], CAPTURED, config=missing)
        tampered = self.selected(self.provider); tampered['synthetic_tamper'] = True
        with self.assertRaisesRegex(ValueError, 'date trust attachment'):
            judging.compile_packet(legacy['packet'], CAPTURED, config=tampered)

    def test_original_verdict_restored_once_on_native_and_replay_routes(self):
        compiled = self.compiled(publication_window=False)
        raw = self.model_value(compiled)
        before = deepcopy(raw)
        for role in ('codex', 'claude'):
            decoded = runner.decode_native(role, dict(transport_success=True, verdict=raw), compiled,
                                           dict(text_profile='raw-v1', date_profile=None))
            self.assertEqual(decoded['state'], 'valid', decoded)
            self.assertEqual(decoded['raw_verdict'], raw)
            self.assertEqual(decoded['decoder_verdict']['case_id'], compiled['packet']['case_id'])
            self.assertEqual(decode_value(decoded['raw_verdict'], compiled, 'raw-v1'),
                             decode_value(raw, compiled, 'raw-v1'))
            self.assertEqual(judging.decode_position(decoded['decoder_verdict'], compiled)['state'], 'invalid')
        native = dict(modelVersion=judging.METHOD['primary']['model'], candidates=[dict(
            finishReason='STOP', content=dict(parts=[dict(text=json.dumps(raw))]))])
        decoded = runner.decode_native('pro', dict(raw_text=json.dumps(native), http_status=200, complete=True), compiled,
                                       dict(text_profile='raw-v1', date_profile=None))
        self.assertEqual(decoded['state'], 'valid', decoded)
        self.assertEqual(decoded['raw_verdict'], raw)
        self.assertEqual(decoded['raw_sha256'], digest(json.dumps(native).encode()))
        bad = deepcopy(raw); bad['candidates'][0]['support'][0]['quote'] = 'invented exact quote'
        self.assertEqual(judging.decode_position(bad, compiled)['state'], 'invalid')
        bad = deepcopy(raw); bad['unexpected'] = True
        self.assertEqual(judging.decode_position(bad, compiled)['state'], 'invalid')
        self.assertEqual(raw, before)

    def test_publication_and_adjudication_restore_before_full_source_checks(self):
        fixture = test_publication.PublicationTests(); fixture.setUp()
        config = self.selected(fixture.provider)
        compiled = judging.compile_packet(fixture.compiled['packet'], CAPTURED, config=config)
        packet = publication.packet_for(compiled)
        source_packet = publication.packet_for(compiled, _source=True)
        raw = self.model_value(compiled, fixture.output(2), source_packet)
        result = publication.validate_review(raw, compiled, packet)
        self.assertTrue(result['valid'], result)
        self.assertEqual(result['raw_verdict'], raw)
        self.assertEqual(result['decoder_verdict']['case_id'], compiled['packet']['case_id'])
        responses = {r: dict(verdict=deepcopy(raw), transport_success=True, native_completion_verified=True)
                     for r in ('codex', 'claude')}
        self.assertTrue(publication.corroborate(compiled, packet, responses)['accepted'])
        bad = deepcopy(raw); bad['policy_applications'][0]['checks'][0]['support'][0]['quote'] = 'invented'
        self.assertFalse(publication.validate_review(bad, compiled, packet)['valid'])
        # Adjudication has no occurrence root; quote aliases still restore and
        # literal validation remains owned by the source review validator.
        compiled = self.compiled()
        ids = [r['date_id'] for r in compiled['input']['native_date_ids']
               if r['relative_path'] in judging.applicability_fields(compiled['input']['publication_estimate_applicability'])]
        source = adjudication.packet_for(compiled, ids, _source=True)
        raw = dict(disposition='confirm', selected_native_date_ids=ids,
                   checks=[dict(check=k, status='pass', reason='Supported own item.',
                                support=[dict(field='title', quote=TEXT, occurrence=1)]) for k in adjudication.CHECKS],
                   contrary_reading='A changing page would conflict.', reason='All existing checks supported.')
        wire = self.model_value(compiled, raw, source)
        restored = judging.restore_model_verdict(wire, compiled, source)
        self.assertTrue(adjudication.validate_disposition(restored, source)['accepted'])
        bad = deepcopy(wire); bad['checks'][0]['support'][0]['quote'] = 'invented'
        self.assertFalse(adjudication.validate_disposition(judging.restore_model_verdict(bad, compiled, source), source)['accepted'])

    def test_actual_api_and_subscription_payloads_use_same_view(self):
        compiled = self.compiled(publication_window=False)
        ids = [r['date_id'] for r in compiled['input']['native_date_ids']
               if r['relative_path'] in judging.applicability_fields(compiled['input']['publication_estimate_applicability'])]
        packets = [judging.blind_packet(compiled), publication.packet_for(compiled), adjudication.packet_for(compiled, ids)]
        for packet in packets:
            for backend in ('openai-api', 'anthropic-api'):
                calls = []
                def transport(request, *args, **kwargs):
                    calls.append(deepcopy(request))
                    return dict(transport_success=False)
                with TemporaryDirectory() as temp, patch('openweb_news.backends.https', side_effect=transport):
                    backends.api(dict(backend=backend, model='synthetic-model', max_output_tokens=100,
                                      reasoning_effort='medium', secret_ref='synthetic', timeout_seconds=5), packet, Path(temp), None)
                body = calls[0]['body']
                self.assertEqual(body.get('instructions', body.get('system')), packet['prompt'])
                payload = body['input'] if backend == 'openai-api' else body['messages'][0]['content']
                self.assertEqual(json.loads(payload), packet['input'])
                schema = (body['text']['format']['schema'] if backend == 'openai-api' else body['output_config']['format']['schema'])
                self.assertEqual(schema, packet['schema'] if backend == 'openai-api' else backends.anthropic_schema(packet['schema']))
            for kind in ('codex', 'claude'):
                captured = []
                def process(command, **kwargs):
                    if '--help' in command:
                        flags = '--ignore-user-config --ignore-rules --ephemeral --output-schema --safe-mode --tools --strict-mcp-config --disable-slash-commands --no-session-persistence'
                        return subprocess.CompletedProcess(command, 0, stdout=flags, stderr='')
                    if 'status' in command:
                        status = 'Logged in using ChatGPT' if kind == 'codex' else '{"authMethod":"claude.ai"}'
                        return subprocess.CompletedProcess(command, 0, stdout=status, stderr='')
                    captured.append(json.loads(kwargs['input']))
                    if kind == 'codex':
                        schema_path = Path(command[command.index('--output-schema') + 1])
                        self.assertEqual(load(schema_path), packet['schema'])
                        self.assertEqual((schema_path.parent / 'instructions.md').read_text(), packet['prompt'])
                        Path(command[command.index('--output-last-message') + 1]).write_text('{}')
                        events = [dict(type='turn.completed')]
                    else:
                        self.assertEqual(command[command.index('--system-prompt') + 1], packet['prompt'])
                        self.assertEqual(json.loads(command[command.index('--json-schema') + 1]), backends.anthropic_schema(packet['schema']))
                        events = [dict(type='result', structured_output={})]
                    return subprocess.CompletedProcess(command, 0, stdout='\n'.join(json.dumps(e) for e in events), stderr='')
                with TemporaryDirectory() as temp:
                    root = Path(temp); (root / '.codex').mkdir(); (root / '.codex' / 'auth.json').write_text('{}')
                    result = backends.subscription(dict(backend=kind + '-subscription', executable=sys.executable,
                        model='synthetic-model', reasoning_effort='medium', timeout_seconds=5), packet, root,
                        parent_env=dict(HOME=temp, PATH='/bin'), run=process)
                self.assertTrue(result['transport_success'], result)
                self.assertEqual(captured, [packet['input']])

    def test_actual_app_server_payload_has_no_private_compiler_envelope(self):
        compiled = self.compiled(publication_window=False)
        ids = [r['date_id'] for r in compiled['input']['native_date_ids']
               if r['relative_path'] in judging.applicability_fields(compiled['input']['publication_estimate_applicability'])]
        packets = [judging.blind_packet(compiled), publication.packet_for(compiled), adjudication.packet_for(compiled, ids)]
        with TemporaryDirectory() as temp:
            fixture = test_codex_runtime.RuntimeTests()
            runtime, processes = fixture.setup_runtime(Path(temp), concurrency=1, verdict=lambda data: self.model_value(compiled))
            with runtime:
                for index, packet in enumerate(packets):
                    result = runtime.review(packet, Path(temp) / ('response-' + str(index)))
                    self.assertTrue(result['transport_success'], result)
            starts = [r['params'] for r in processes[0].requests if r.get('method') == 'thread/start']
            turns = [r['params'] for r in processes[0].requests if r.get('method') == 'turn/start']
            for packet, start, turn in zip(packets, starts, turns):
                self.assertEqual(start['baseInstructions'], packet['prompt'])
                self.assertEqual(json.loads(turn['input'][0]['text']), packet['input'])
                self.assertEqual(turn['outputSchema'], packet['schema'])
            self.assertNotIn('source_packet', json.dumps(starts + turns))

    def test_native_metadata_quotes_restore_to_original_exact_source_spans(self):
        config = next(c for c in self.catalog if c['configuration_id'] == 'brave-web')
        compiled = self.compiled(config, publication_window=False)
        packet = judging.blind_packet(compiled)
        metadata = next(row for row in packet['input']['native_metadata']
                        if row.get('source_display_kind') == 'source_hostname_display')
        raw = self.model_value(compiled)
        raw['record_conditions'][0]['support'].append(dict(field=metadata['field'], quote='news.example', occurrence=1))
        decoded = judging.decode_position(raw, compiled)
        self.assertEqual(decoded['state'], 'valid', decoded)
        restored = decoded['decoder_verdict']['record_conditions'][0]['support'][-1]
        self.assertEqual(restored['field'], 'native:meta_url')
        self.assertEqual(raw['record_conditions'][0]['support'][-1]['field'], metadata['field'])
        bad = deepcopy(raw); bad['record_conditions'][0]['support'][-1]['quote'] = 'invented.publisher'
        self.assertEqual(judging.decode_position(bad, compiled)['state'], 'invalid')

    def test_fixture_method_cannot_verify_live_zero_job_review(self):
        provider = self.selected(self.provider)
        reviewers = fixtures.config(Path('/tmp/synthetic-unused'))['reviewers']
        authority = self.method_authority(provider, reviewers)
        binding = authority['profile_bindings']
        package = dict(profile=review_pipeline.PROFILE, mode='fixture', code=code_bindings(), assets=asset_bindings(),
            maximum_new_subscription_tasks=0, incremental_api_calls=0, jobs=[], cases=[], reviewers=reviewers,
            live_profile_authority=authority, method_options=authority['method_options'],
            date_profile=binding['date_profile'], text_profile=binding['text_profile'])
        with patch.object(profile_authority, 'read', return_value=authority):
            self.assertEqual(review_pipeline.verify(package, Path('/tmp')), {})
            package['mode'] = 'live'
            with self.assertRaisesRegex(ValueError, 'mode'):
                review_pipeline.verify(package, Path('/tmp'))
            package['mode'] = 'fixture'; package.pop('live_profile_authority')
            with self.assertRaisesRegex(ValueError, 'acceptance'):
                review_pipeline.verify(package, Path('/tmp'))

    def test_settlement_rejects_legacy_and_wrong_provider_even_when_known(self):
        reviewers = fixtures.config(Path('/tmp/synthetic-unused'))['reviewers']
        provider = self.selected(self.provider)
        authority = self.method_authority(provider, reviewers)
        legacy = compiled_for(self.provider)
        cid = legacy['packet']['case_id']
        cases = {cid: dict(compiled=legacy, provider=self.provider)}
        for grade in (None, 1):
            parent = dict(outcomes=[dict(case_id=cid, grade=grade)])
            with self.assertRaisesRegex(ValueError, 'case input method'):
                settlement.selected(parent, cases, reviewers, authority=authority)
        with self.assertRaisesRegex(ValueError, 'case input method'):
            settlement.jobs_for([dict(case_id=cid)], cases, reviewers, authority=authority)
        current = self.compiled()
        selected_cases = {current['packet']['case_id']: dict(compiled=current, provider=provider)}
        for missing in (None, dict(profile_bindings=authority['profile_bindings'])):
            with self.assertRaisesRegex(ValueError, 'downgrade'):
                settlement.selected(dict(outcomes=[]), selected_cases, reviewers, authority=missing)
            with self.assertRaisesRegex(ValueError, 'downgrade'):
                settlement.jobs_for([], selected_cases, reviewers, authority=missing)
        wrong = deepcopy(provider); wrong['configuration_id'] = 'unadmitted-provider'
        cases = {current['packet']['case_id']: dict(compiled=current, provider=wrong)}
        with self.assertRaisesRegex(ValueError, 'case input method'):
            settlement.selected(dict(outcomes=[]), cases, reviewers, authority=authority)

    def test_settlement_and_checkpoint_cannot_promote_fixture_method(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = test_publication.PublicationTests(); fixture.setUp()
            parent, _, native = fixture.parent(root)
            provider = self.selected(self.provider)
            reviewers = native['configuration']['reviewers']
            authority = self.method_authority(provider, reviewers)
            native['mode'] = 'fixture'
            native['configuration'].update(authority['method_options'], live_profile_authority=authority)
            parent['outcomes'] = []  # Zero jobs still cannot acquire live authority.
            source = root / 'source/run/report.json'; write_private(source, parent)
            with patch.object(profile_authority, 'read', return_value=authority), \
                 patch.object(settlement, 'load_parent', return_value=(parent, {}, native)):
                child = settlement.prepare_settlement(source, root / 'fixture', accepted_profile='synthetic-method')
                self.assertEqual(child['mode'], 'fixture')
                settlement.verify(child, root / 'fixture')
                bad = deepcopy(child); bad['mode'] = 'live'
                with self.assertRaisesRegex(ValueError, 'mode'):
                    settlement.verify(bad, root / 'fixture')
                bad = deepcopy(child); bad.pop('accepted_profile')
                with self.assertRaisesRegex(ValueError, 'source method authority'):
                    settlement.verify(bad, root / 'fixture')
                with self.assertRaisesRegex(ValueError, 'mode'):
                    accepted_review.prepare(source, 'synthetic-method', root / 'bad', mode='live')
                checkpoint = accepted_review.prepare(source, 'synthetic-method', root / 'accepted', mode='fixture')
                self.assertEqual(checkpoint['mode'], 'fixture')
                self.assertEqual(load(root / 'accepted/publication/package.json')['mode'], 'fixture')

    def test_replacement_cannot_downgrade_new_method_before_jobs(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            provider = self.selected(self.provider)
            cfg = fixtures.config(root / 'source/run')
            authority = self.method_authority(provider, cfg['reviewers'])
            cfg.update(authority['method_options'], live_profile_authority=authority)
            source = dict(configuration=cfg, mode='fixture', providers=[provider])
            source_path = root / 'source-package.json'; write_private(source_path, source)
            write_private(root / 'source/run/report.json', dict(package_sha256=digest(source)))
            replacement = dict(configuration=fixtures.config(root / 'replacement/run'), mode='fixture', providers=[self.provider])
            replacement_path = root / 'replacement.json'; write_private(replacement_path, replacement)
            with patch.object(profile_authority, 'read', return_value=authority):
                with self.assertRaisesRegex(ValueError, 'replacement method authority'):
                    review_pipeline.prepare_review(source_path, ['synthetic'], root / 'review', replacement_package=replacement_path)
            self.assertFalse((root / 'review').exists())

    def test_nonempty_fixture_method_review_closes_and_loads_read_only(self):
        from openweb_news.review import settle
        from openweb_news.reporting import write_reports
        with TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            fixture = test_publication.PublicationTests(); fixture.setUp()
            parent, _, native = fixture.parent(root)
            provider = self.selected(fixture.provider)
            compiled = judging.compile_packet(fixture.compiled['packet'], CAPTURED, config=provider)
            authority = self.method_authority(provider, native['configuration']['reviewers'])
            native['mode'] = 'fixture'; native['providers'] = [provider]
            native['configuration'].update(authority['method_options'], live_profile_authority=authority)
            position = judging.decode_position(self.model_value(compiled, fixture.raw), compiled)
            position.update(independent=True, prior_verdict_exposure=False)
            parent['outcomes'][0].update(settle(position, position))
            parent.update(execution=dict(state='completed'), outcome_counts={'unknown':1})
            cid = compiled['packet']['case_id']
            cases = {cid: dict(compiled=compiled, provider=provider)}
            source = root / 'source/run/report.json'; write_reports(source.parent, parent)
            original_loader = settlement.load_parent
            def source_loader(path, **kwargs):
                return (parent,cases,native) if Path(path).resolve()==source else original_loader(path, **kwargs)
            raw = self.model_value(compiled, fixture.output(2), publication.packet_for(compiled, _source=True))
            calls = []
            def call(job, folder):
                calls.append(job['task_id'])
                return dict(verdict=deepcopy(raw), transport_success=True, native_completion_verified=True)
            with patch.object(profile_authority, 'read', return_value=authority), \
                 patch.object(settlement, 'load_parent', side_effect=source_loader):
                package = accepted_review.prepare(source, 'synthetic-method', root/'accepted', allow_inference=True, mode='fixture')
                self.assertEqual(package['review_tasks'], 2)
                path = root/'accepted/review-checkpoint.json'
                result = accepted_review.run(path, {}, call=call)
                self.assertEqual(result['outcomes'][0]['grade'], 2)
                report = root/'accepted/report/report.json'
                replay, _, _ = accepted_review.load_completed(report)
                self.assertEqual(replay['outcomes'], result['outcomes'])
                accepted_review.run(path, {}, call=lambda *_: self.fail('fixture review repeated'))
                self.assertEqual(len(calls), 2)
                child_path = root/'accepted/publication/package.json'
                child = load(child_path)
                closure = load(root/'accepted/publication/run/closure.json')
                self.assertIsNone(closure['native_ledger_sha256'])
                self.assertTrue(closure['fixture_ledger_sha256'])
                bad = deepcopy(child); bad['mode'] = 'live'
                write_private(child_path, bad, replace=True)
                with self.assertRaises(ValueError):
                    original_loader(root/'accepted/publication/run/report.json')
                write_private(child_path, child, replace=True)
                result_path = next((root/'accepted/publication/run/attempts').glob('*/result.json'))
                retained = result_path.read_bytes()
                altered = load(result_path); altered['verdict']['reason'] = 'tampered synthetic response'
                write_private(result_path, altered, replace=True)
                with self.assertRaisesRegex(ValueError, 'request/result'):
                    accepted_review.load_completed(report)
                write_private(result_path, retained, replace=True)
                bad = deepcopy(child); bad['accepted_profile']['profile'] = 'legacy-fixture'
                write_private(child_path, bad, replace=True)
                with self.assertRaisesRegex(ValueError, 'fixture review cannot'):
                    original_loader(root/'accepted/publication/run/report.json')

    def native_link_value(self, compiled, *, field, role='first_publication', date_ids=None, context=False):
        ident=next(row['date_id'] for row in compiled['input']['native_date_ids'] if row['relative_path']==field)
        value=verdict(compiled)
        own=compiled['packet']['allowed_text'][0]
        def own_fields(node):
            if isinstance(node,dict):
                if node.get('field')=='title' and 'quote' in node:node['field']=own['field']
                for child in node.values():own_fields(child)
            elif isinstance(node,list):
                for child in node:own_fields(child)
        own_fields(value)
        value['dates']=[dict(candidate_id='finding-1', predicate_id='publication:window', link_status='established',
            role=role,date_ids=date_ids or [ident],anchor_date_id='',additional_dates=[],
            context_support=[dict(field=own['field'],quote=own['text'],occurrence=1)] if context else [],
            reason='The linked own evidence establishes the selected publication period.')]
        return value

    def test_owner_excluded_exa_native_cannot_pass_or_fail_publication(self):
        exa=next(c for c in self.catalog if c['configuration_id']=='exa-highlights')
        for date, expected in [('2026-10-12',2),('2026-09-12',0)]:
            selected=self.selected(exa)
            admitted=compiled_for(selected,date=date)
            excluded=deepcopy(selected);excluded['date_trust']['eligible_fields']=[]
            denied=compiled_for(excluded,date=date)
            observed=next(r for r in denied['packet']['date_observations'] if r['relative_path']=='publishedDate')
            self.assertEqual((observed['date_kind'],observed['derivation']),('first_publication','documented_estimated'))
            for compiled, grade in [(admitted,expected),(denied,1)]:
                value=self.native_link_value(compiled,field='publishedDate')
                decoded=judging.decode_position(self.model_value(compiled,value),compiled)
                self.assertEqual(decoded['structural_base_grade'],grade,decoded)
                status=next(r['status'] for r in decoded['judgment']['candidates'][0]['conditions'] if r['predicate_id']=='publication:window')
                self.assertEqual(status,'not_demonstrated' if compiled is denied else 'pass' if expected==2 else 'fail')
            value=self.native_link_value(denied,field='publishedDate',date_ids=['D1'],context=True)
            own=judging.decode_position(self.model_value(denied,value),denied)
            self.assertEqual(own['structural_base_grade'],2,own)
        legacy=compiled_for(exa)
        old=judging.decode_position(self.native_link_value(legacy,field='publishedDate'),legacy)
        self.assertEqual(old['structural_base_grade'],2,old)
        config=self.selected(exa);config['date_trust']['eligible_fields']=[]
        own_old=compiled_for(config,text='Editorial report. Published September 12, 2026: Aster opened.')
        value=self.native_link_value(own_old,field='publishedDate',date_ids=['D1'],context=True)
        decoded=judging.decode_position(self.model_value(own_old,value),own_old)
        self.assertEqual(decoded['structural_base_grade'],0,decoded)
        # The final source validator independently rejects a forged native proof,
        # including a caller claiming scoped review without field admission.
        from openweb_news._method import evidence
        normalized=old['judgment']
        errors,_=evidence.validate_judgment(legacy['packet'],normalized,calendar_days=True,
            reviewed_publications={('finding-1','publication:window')},native_publication_fields=())
        self.assertTrue(any('not admitted' in error for error in errors),errors)

    def test_unadmitted_native_cannot_disambiguate_own_date_but_own_year_can(self):
        from openweb_news.date_reading import LATEST_PROFILE
        exa=next(c for c in self.catalog if c['configuration_id']=='exa-highlights')
        for admitted in (True,False):
            config=self.selected(exa)
            if not admitted:config['date_trust']['eligible_fields']=[]
            compiled=compiled_for(config,date='2026-01-12',text='Editorial report. Published 01/12/2026: Aster opened.')
            source=deepcopy(compiled['packet'])
            source['question']['contract']['temporal_requirements'][0].update(start_inclusive='2026-01-01T00:00:00Z',end_exclusive='2026-02-01T00:00:00Z')
            compiled=judging.compile_packet(source,'2027-01-01T00:00:00Z',config=config)
            value=self.native_link_value(compiled,field='publishedDate',date_ids=['D1'],context=True)
            decoded=judging.decode_position(self.model_value(compiled,value),compiled,date_profile=LATEST_PROFILE)
            self.assertEqual(decoded['structural_base_grade'],2 if admitted else None,decoded)
        config=self.selected(exa);config['date_trust']['eligible_fields']=[]
        compiled=compiled_for(config,text='Editorial report. Published October 12: Aster opened.')
        native_id=next(row['date_id'] for row in compiled['input']['native_date_ids'] if row['relative_path']=='publishedDate')
        value=self.native_link_value(compiled,field='publishedDate',date_ids=['D1',native_id],context=True)
        decoded=judging.decode_position(self.model_value(compiled,value),compiled,date_profile=LATEST_PROFILE)
        self.assertEqual(decoded['structural_base_grade'],1,decoded)
        compiled=compiled_for(config,text='Editorial report. Published October 12: Aster opened in 2026.')
        own_ids=[r['date_id'] for r in compiled['input']['text_dates']]
        value=self.native_link_value(compiled,field='publishedDate',date_ids=own_ids,context=True)
        decoded=judging.decode_position(self.model_value(compiled,value),compiled,date_profile=LATEST_PROFILE)
        self.assertEqual(decoded['structural_base_grade'],2,decoded)

    def test_explicit_native_admission_also_gates_reviewed_availability(self):
        from openweb_news._method import two_step
        compiled=self.compiled()
        source=deepcopy(compiled['packet'])
        source['as_of_interpretation']=dict(cutoff='2026-10-31T23:59:59.999999Z',boundary='inclusive',precision='day')
        native=next(row for row in compiled['input']['native_date_ids'] if row['relative_path']=='publication_date')
        link=self.native_link_value(compiled,field='publication_date')['dates'][0]
        link.update(predicate_id='as_of_knowledge',role='fact_availability')
        before=deepcopy(source)
        for fields,status in [(None,'pass'),((native['field'],),'pass'),((),'not_demonstrated')]:
            result,_=two_step.date_condition(source,dict(predicate_id='as_of_knowledge'),link,calendar_days=True,
                reviewed_publication_fields=(native['field'],),native_publication_fields=fields)
            self.assertEqual(result['status'],status,result)
        self.assertEqual(source,before)

    def test_new_native_admission_gate_matches_scoped_publication_review(self):
        for cid,field in [('exa-highlights','publishedDate'),('keenable-pro','published_at'),('seltz-web-pro','published_date')]:
            provider=next(c for c in self.catalog if c['configuration_id']==cid)
            for admitted in (True,False):
                with self.subTest(provider=cid,admitted=admitted):
                    config=self.selected(provider)
                    if not admitted:config['date_trust']['eligible_fields']=[]
                    compiled=compiled_for(config,source_change=(lambda row:row.update(published_date='2026-10-12')) if cid=='seltz-web-pro' else None)
                    native=next(row for row in compiled['input']['native_date_ids'] if row['relative_path']==field)
                    self.assertEqual(judging.native_publication_fields(compiled),
                        (native['field'],) if admitted else ())
                    role=native['date_kind']
                    value=self.native_link_value(compiled,field=field,role=role,context=True)
                    quote=dict(field=compiled['packet']['allowed_text'][0]['field'],quote=compiled['packet']['allowed_text'][0]['text'],occurrence=1)
                    value.update(complete_occurrence_reviewed=True,contrary_reading='A changing page or conflicting date would defeat this inference.',
                        policy_applications=[dict(candidate_ids=['finding-1'],predicate_id='publication:window',policy=publication.NATIVE_POLICY,status='pass',
                            checks=[dict(check=k,status='pass',reason='Exact own item/date context supports the existing inference.',support=[deepcopy(quote)]) for k in publication.CHECKS])])
                    packet=publication.packet_for(compiled)
                    model=self.model_value(compiled,value,publication.packet_for(compiled,_source=True))
                    result=publication.validate_review(model,compiled,packet,checker_profile=publication.CHECKER_PROFILE)
                    self.assertEqual(result['valid'],admitted,result)

    def test_terminal_review_identity_binds_private_occurrence_despite_same_model_view(self):
        config=self.selected(self.provider)
        first=compiled_for(config)
        second=compiled_for(config,index=2,noise=887766,identity='second')
        self.assertEqual(publication.packet_for(first),publication.packet_for(second))
        reviewers=fixtures.config(Path('/tmp/synthetic-unused'))['reviewers']
        authority=self.method_authority(config,reviewers)
        key=settlement.identity(first,reviewers,authority=authority)
        self.assertEqual(key,settlement.identity(deepcopy(first),reviewers,authority=authority))
        self.assertNotEqual(key,settlement.identity(second,reviewers,authority=authority))


if __name__ == '__main__':
    unittest.main()
