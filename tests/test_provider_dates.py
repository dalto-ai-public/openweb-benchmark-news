from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from openweb_news import provider_dates, request_profiles as start_dates
from openweb_news._method.providers import build_request, configuration_catalog
from openweb_news.request_profiles import apply, BRAVE_UNDECORATED
from .helpers import question
from openweb_news.common import load, write_private


def dated(*, start="2026-01-10T00:00:00Z", end="2026-01-11T00:00:00Z", basis="publication"):
    q = question()
    q["contract"]["temporal_requirements"] = [dict(id="window", basis=basis, start_inclusive=start, end_exclusive=end)]
    return q


class ProviderDatesTests(unittest.TestCase):
    def test_mixed_start_profiles_preserve_config_query_and_other_request_settings(self):
        for cid in start_dates.MIXED_START_IDS:
            config = deepcopy(self.catalog[cid])
            if cid == 'brave-web':
                config = apply([config], {cid: BRAVE_UNDECORATED})[0]
            before = deepcopy(config)
            old = build_request(config, dated())
            new = build_request(config, dated(), date_profile=start_dates.MIXED_START_PROFILE)
            self.assertEqual(config, before)
            expected_body = deepcopy(old['body'])
            if cid == 'tavily-general':
                expected_body['start_date'] = '2026-01-09'
                self.assertNotIn('end_date', new['body'])
                self.assertNotIn('filter_by_published_date', new['body'])
                self.assertNotIn('include_published_date', new['body'])
            else:
                self.assertEqual(new['date_dispatch']['lower_date'], '2026-01-09')
                self.assertNotIn('freshness', new['body'])
            self.assertEqual(new['body'], expected_body)
            self.assertEqual(new['query_sha256'], old['query_sha256'])
            self.assertFalse(new['date_capability_receipt']['eligibility_proof'])
        for cid in ('firecrawl-web', 'searlo-web', 'oxylabs-search', 'tavily-news'):
            with self.assertRaises(ValueError):
                build_request(self.catalog[cid], dated(), date_profile=start_dates.MIXED_START_PROFILE)

    def test_mixed_profile_does_not_filter_events_or_missing_windows(self):
        for cid in start_dates.MIXED_START_IDS:
            for q in (question(), dated(basis='event')):
                old = build_request(self.catalog[cid], q)
                new = build_request(self.catalog[cid], q, date_profile=start_dates.MIXED_START_PROFILE)
                self.assertEqual(new, old)
        with self.assertRaises(ValueError):
            start_dates.validate_start_profiles([self.catalog['brave-web']], {'you-com-web': start_dates.MIXED_START_PROFILE})

    def test_dispatch_anchor_survives_midnight_and_resume_and_rejects_tampering(self):
        for cid in ('brave-web', 'you-com-web'):
            request = build_request(self.catalog[cid], dated(), date_profile=start_dates.MIXED_START_PROFILE)
            original = deepcopy(request)
            with TemporaryDirectory() as temp:
                path = Path(temp) / 'dispatch.json'
                first = start_dates.bind_dispatch(request, path, dispatched_at='2026-12-31T23:59:59Z')
                self.assertEqual(first['body']['freshness'], '2026-01-09to2027-01-02')
                self.assertEqual(start_dates.bind_dispatch(request, path, dispatched_at='2027-02-01T00:00:00Z'), first)
                self.assertEqual(start_dates.read_dispatch(request, path), first)
                self.assertEqual(request, original)
                saved = load(path)
                saved['effective_request']['body']['freshness'] = '2026-01-09to2027-01-01'
                write_private(path, saved, replace=True)
                with self.assertRaises(ValueError):
                    start_dates.read_dispatch(request, path)
                with self.assertRaises(ValueError):
                    start_dates.bind_dispatch(request, path)

    def test_dispatch_window_covers_current_utc_day_at_timezone_extremes(self):
        request = build_request(self.catalog['brave-web'], dated(), date_profile=start_dates.MIXED_START_PROFILE)
        with TemporaryDirectory() as temp:
            effective = start_dates.bind_dispatch(request, Path(temp)/'d.json', dispatched_at='2026-11-05T23:59:59Z')
            upper = effective['body']['freshness'].split('to')[1]
            # Even if exclusive midnight is interpreted at UTC+14, the
            # current UTC day is covered. This tests the chosen assumption,
            # not undocumented provider behavior.
            for offset in (-12, 0, 14):
                endpoint = datetime.fromisoformat(upper).replace(tzinfo=timezone(timedelta(hours=offset)))
                self.assertGreater(endpoint.astimezone(timezone.utc), datetime(2026,11,6,tzinfo=timezone.utc))
        future = build_request(self.catalog['brave-web'], dated(start='2027-01-10T00:00:00Z',end='2027-01-11T00:00:00Z'),date_profile=start_dates.MIXED_START_PROFILE)
        with TemporaryDirectory() as temp:
            with self.assertRaises(ValueError):
                start_dates.bind_dispatch(future, Path(temp)/'d.json',dispatched_at='2026-11-05T00:00:00Z')
        with self.assertRaises(ValueError):
            build_request(self.catalog['brave-web'],dated(start='2026-01-10T00:00:00'),date_profile=start_dates.MIXED_START_PROFILE)

    def test_runner_sends_resolved_range_reuses_completed_search_and_audits_attempts(self):
        from unittest.mock import patch
        from openweb_news import fixtures
        from openweb_news.preparation import prepare
        from openweb_news.runner import run
        from openweb_news.transport import https
        with TemporaryDirectory() as temp:
            root=Path(temp)
            cfg=fixtures.config(root/'run')
            cfg.update(provider_ids=['brave-web'],tie_order=['brave-web'],
                questions=[dated()],execution_stage='collection-only-v1',
                provider_date_profiles={'brave-web':start_dates.MIXED_START_PROFILE})
            cfg['execution_policy']['scheduling']['accounts'].pop('provider/OPENWEB_NEWS_EXA_API_KEY')
            package=prepare(cfg,mode='fixture')
            original=fixtures.search
            sent=[]
            def search(request, provider, folder):
                self.assertNotIn('date_dispatch',request)
                self.assertIn('freshness',request['body'])
                sent.append(request)
                return original(request,provider,folder)
            with patch('openweb_news.fixtures.search',side_effect=search):
                report=run(package)
            self.assertEqual(report['execution']['state'],'completed')
            self.assertIn('future bound',str(report['retrieval_date_controls']['limitations']))
            self.assertIn('future bound',(root/'run/report.html').read_text())
            self.assertEqual(len(sent),1)
            self.assertEqual(start_dates.audit_dispatch(package,root/'run')['verified_attempts'],1)
            with patch('openweb_news.fixtures.search',side_effect=AssertionError('completed task must not resend')):
                self.assertEqual(run(package)['collection'],report['collection'])
            attempt=next((root/'run/attempts').glob('*/effective-search-request.json'))
            changed=load(attempt);changed['body'].pop('freshness')
            write_private(attempt,changed,replace=True)
            with self.assertRaises(ValueError):
                run(package)
            with self.assertRaises(ValueError):
                https(package['search_requests'][0],None,None,root,timeout_seconds=1)

    def test_partial_search_scope_keeps_full_method_roster_and_never_sends_other_cells(self):
        from unittest.mock import patch
        from openweb_news import fixtures
        from openweb_news.preparation import prepare
        from openweb_news.runner import run
        with TemporaryDirectory() as temp:
            cfg=fixtures.config(Path(temp)/'run')
            cfg.update(provider_ids=['brave-web','exa-highlights'],tie_order=['brave-web','exa-highlights'],
                questions=[dated()],execution_stage='collection-only-v1',search_provider_ids=['brave-web'],
                provider_date_profiles={'brave-web':start_dates.MIXED_START_PROFILE})
            package=prepare(cfg,mode='fixture')
            self.assertEqual([p['configuration_id'] for p in package['providers']],cfg['provider_ids'])
            self.assertEqual([r['configuration_id'] for r in package['search_requests']],['brave-web'])
            self.assertEqual(package['scope']['result_slots'],10)
            original=fixtures.search
            def search(request,provider,folder):
                self.assertEqual(provider['configuration_id'],'brave-web')
                return original(request,provider,folder)
            with patch('openweb_news.fixtures.search',side_effect=search):
                report=run(package)
            self.assertEqual(len(report['collection']),1)
            for ids in ([],['unknown'],['brave-web','brave-web'],['exa-highlights','brave-web']):
                cfg['search_provider_ids']=ids
                with self.assertRaises(ValueError):prepare(cfg,mode='fixture')

    def test_completed_http_retry_retains_original_range_across_days(self):
        from unittest.mock import patch
        from openweb_news import fixtures
        from openweb_news.preparation import prepare
        from openweb_news.runner import run
        with TemporaryDirectory() as temp:
            root=Path(temp)
            cfg=fixtures.config(root/'run')
            cfg.update(provider_ids=['brave-web','exa-highlights'],tie_order=['brave-web','exa-highlights'],
                questions=[dated()],execution_stage='collection-only-v1',search_provider_ids=['brave-web'],
                provider_date_profiles={'brave-web':start_dates.MIXED_START_PROFILE})
            policy=cfg['execution_policy']
            policy['max_attempts_per_task']=2
            policy['retry_policy'].update(max_total=1,max_search=1,http_statuses=[503])
            package=prepare(cfg,mode='fixture');original=fixtures.search;sent=[]
            def search(request,provider,folder):
                sent.append(deepcopy(request))
                result=original(request,provider,folder)
                if len(sent)==1:
                    result.update(http_status=503,transport_success=False)
                return result
            with patch('openweb_news.fixtures.search',side_effect=search):
                with patch('openweb_news.common.now',return_value='2026-11-05T23:59:59Z'):
                    run(package)
                with patch('openweb_news.common.now',return_value='2026-11-09T12:00:00Z'):
                    run(package,retry_failed=True)
            self.assertEqual(len(sent),2)
            self.assertEqual(sent[0],sent[1])
            self.assertEqual(sent[1]['body']['freshness'],'2026-01-09to2026-11-07')
            self.assertEqual(start_dates.audit_dispatch(package,root/'run')['verified_attempts'],2)

    def test_dispatch_audit_preserves_exception_and_interrupted_attempts_without_resend(self):
        from openweb_news.ledger import Ledger
        from openweb_news.common import digest
        request=build_request(self.catalog['brave-web'],dated(),date_profile=start_dates.MIXED_START_PROFILE)
        package={'search_requests':[request]}
        task='search-'+digest(dict(configuration='brave-web',question=dated()['question_id']))
        for stage in ('before_receipt','after_receipt','after_effective','exception'):
            with self.subTest(stage=stage),TemporaryDirectory() as temp:
                root=Path(temp);receipt=root/'search-dispatch'/(task+'.json')
                class Crash(BaseException):pass
                def call(folder):
                    if stage!='before_receipt':
                        effective=start_dates.bind_dispatch(request,receipt,dispatched_at='2026-11-05T23:59:59Z')
                        if stage!='after_receipt':
                            write_private(folder/'effective-search-request.json',effective)
                    if stage=='exception':raise RuntimeError('synthetic failure')
                    raise Crash()
                with Ledger(root,{},uncertain_resends=1).locked() as ledger:
                    if stage=='exception':ledger.execute(task,request,call)
                    else:
                        with self.assertRaises(Crash):ledger.execute(task,request,call)
                # Opening with the existing recovery policy seals interrupted
                # attempts. The default (no resend) ledger still retains them.
                with Ledger(root,{},uncertain_resends=1).locked():pass
                proof=start_dates.audit_dispatch(package,root)
                self.assertEqual(proof['verified_attempts'],0)
                self.assertEqual(len(proof['uncertain_attempt_ids']),1)
                with Ledger(root,{}).locked() as ledger:
                    result=ledger.execute(task,request,lambda _:self.fail('uncertain search must not resend'))
                self.assertEqual(result['dispatch_state'],'uncertain')
                if receipt.exists():
                    saved=load(receipt);saved['effective_request']['body']['freshness']='tampered'
                    write_private(receipt,saved,replace=True)
                    with self.assertRaises(ValueError):start_dates.audit_dispatch(package,root)

    def test_actual_https_serialization_retains_declared_get_and_post_fields(self):
        import json
        from urllib.parse import parse_qs, urlsplit
        from unittest.mock import Mock, patch
        from openweb_news.transport import Credentials, https
        for cid in start_dates.MIXED_START_IDS:
            with self.subTest(cid=cid),TemporaryDirectory() as temp:
                root=Path(temp)
                request=build_request(self.catalog[cid],dated(),date_profile=start_dates.MIXED_START_PROFILE)
                effective=start_dates.bind_dispatch(request,root/'dispatch.json',dispatched_at='2026-11-05T23:59:59Z')
                response=Mock(status=200)
                response.read.return_value=b'{}';response.getheaders.return_value=[]
                connection=Mock();connection.getresponse.return_value=response
                with patch('openweb_news.transport.http.client.HTTPSConnection',return_value=connection):
                    result=https(effective,None,Credentials(environment={}),root,timeout_seconds=1)
                self.assertTrue(result['transport_success'])
                args,kwargs=connection.request.call_args
                if args[0]=='GET':
                    wire=parse_qs(urlsplit(args[1]).query)
                    self.assertEqual(wire['freshness'],['2026-01-09to2026-11-07'])
                    self.assertEqual(wire['q'],[dated()['provider_query']])
                else:
                    self.assertEqual(json.loads(kwargs['body']),effective['body'])

    def test_judged_native_html_discloses_retrieval_limits(self):
        from openweb_news import fixtures
        from openweb_news.preparation import prepare
        from openweb_news.runner import run
        with TemporaryDirectory()as temp:
            root=Path(temp)
            cfg=fixtures.config(root/'run')
            cfg.update(provider_ids=['brave-web','exa-highlights'],tie_order=['brave-web','exa-highlights'],
                questions=[question()],provider_date_profiles={'brave-web':start_dates.MIXED_START_PROFILE})
            result=run(prepare(cfg,mode='fixture'))
            self.assertEqual(result['execution']['state'],'completed')
            self.assertIn('future bound',(root/'run/report.html').read_text())

    def test_perplexity_default_context_profile_preserves_historical_catalog(self):
        from openweb_news.request_profiles import PERPLEXITY_DEFAULT_CONTEXT
        old = self.catalog['perplexity-web']
        original = deepcopy(old)
        new = apply([old], {'perplexity-web': PERPLEXITY_DEFAULT_CONTEXT})[0]
        before, after = build_request(old, dated()), build_request(new, dated())
        self.assertEqual(before['body'].pop('search_context_size'), 'high')
        self.assertEqual(before['body'], after['body'])
        self.assertNotIn('search_context_size', after['body'])
        self.assertEqual(old, original)
        self.assertTrue(provider_dates.capability(new)['reviewed'])
        with self.assertRaises(ValueError):
            apply([self.catalog['brave-web']], {'brave-web': PERPLEXITY_DEFAULT_CONTEXT})

    def test_tavily_general_is_separate_untrusted_product_without_news_optins(self):
        from openweb_news.date_trust import field_semantics, registry
        catalog = {p['configuration_id']: p for p in configuration_catalog(include_optional=True)}
        old, new = catalog['tavily-news'], catalog['tavily-general']
        self.assertEqual(old['base_body']['topic'], 'news')
        request = build_request(new, dated())
        self.assertEqual(request['body']['topic'], 'general')
        self.assertNotIn('include_published_date', request['body'])
        self.assertNotIn('filter_by_published_date', request['body'])
        self.assertNotIn('start_date', request['body'])
        self.assertTrue(provider_dates.capability(new)['reviewed'])
        self.assertFalse(provider_dates.capability(new)['fixed_publication_window_applicable'])
        self.assertEqual(field_semantics(new, 'published_date'), ('unclear', 'documented_estimated'))
        self.assertFalse(registry([new])['providers'][0]['fields']['published_date']['admission_candidate'])
        altered = deepcopy(new)
        altered['base_body']['topic'] = 'news'
        self.assertFalse(provider_dates.capability(altered)['reviewed'])

    def setUp(self):
        self.catalog = {p["configuration_id"]: p for p in configuration_catalog(include_optional=True)}

    def selected(self, cid):
        profile = {"linkup-fast": provider_dates.LINKUP_DATE_PROFILE,
                   "parallel-advanced": provider_dates.PARALLEL_DATE_PROFILE}[cid]
        return apply([self.catalog[cid]], {cid: profile})[0]

    def test_legacy_five_keep_exact_body_fields_and_no_new_receipt(self):
        expected = {
            "exa-highlights": {"startPublishedDate": "2026-01-09T23:59:59+00:00", "endPublishedDate": "2026-01-11T00:00:01+00:00"},
            "perplexity-web": {"search_after_date_filter": "01/09/2026", "search_before_date_filter": "01/11/2026"},
            "valyu-search": {"start_date": "2026-01-10", "end_date": "2026-01-11"},
            "keenable-pro": {"published_after": "2026-01-10T00:00:00+00:00", "published_before": "2026-01-11T00:00:00+00:00"},
            "seltz-web-pro": {"from_date": "2026-01-10T00:00:00+00:00", "to_date": "2026-01-11T00:00:00+00:00"},
        }
        for cid, fields in expected.items():
            with self.subTest(cid=cid):
                config = self.catalog[cid]
                request = build_request(config, dated())
                body = deepcopy(config["base_body"])
                body.update({key: dated()["provider_query"] for key in config["query_fields"]})
                body.update(fields)
                self.assertEqual(request["body"], body)
                self.assertNotIn("date_capability_receipt", request)
                self.assertEqual(apply([config], {}), [config])

    def test_single_day_linkup_is_explicitly_widened_not_strict_publication(self):
        original = self.catalog["linkup-fast"]
        q = dated()
        self.assertNotIn("fromDate", build_request(original, q)["body"])
        request = build_request(self.selected("linkup-fast"), q)
        self.assertEqual((request["body"]["fromDate"], request["body"]["toDate"]), ("2026-01-09", "2026-01-12"))
        self.assertEqual(request["body"]["q"], q["provider_query"])
        receipt = request["date_capability_receipt"]
        self.assertIn("unresolved", receipt["widening"])
        self.assertFalse(receipt["eligibility_proof"])
        cap = provider_dates.capability(original)
        self.assertEqual(cap["date_meaning"], "considered_result_date_unspecified")
        self.assertIn("inclusivity unspecified", cap["bounds"])

    def test_nonmidnight_and_timezone_normalize_without_rounding_inward(self):
        q = dated(start="2026-01-10T01:30:00+02:00", end="2026-01-10T05:45:00+02:00")
        request = build_request(self.selected("linkup-fast"), q)
        self.assertEqual((request["body"]["fromDate"], request["body"]["toDate"]), ("2026-01-08", "2026-01-11"))
        self.assertEqual(request["date_capability_receipt"]["requested_start_inclusive"], "2026-01-09T23:30:00+00:00")
        lower = build_request(self.selected("parallel-advanced"), q)
        self.assertEqual(lower["body"]["advanced_settings"]["source_policy"], {"after_date": "2026-01-08"})
        self.assertIn("no upper", lower["date_capability_receipt"]["widening"])

    def test_parallel_preserves_other_advanced_settings_and_no_invented_upper(self):
        config = self.selected("parallel-advanced")
        config["base_body"]["advanced_settings"] = {"source_policy": {"exclude_domains": ["synthetic.example"]}, "other": 3}
        body = build_request(config, dated())["body"]
        self.assertEqual(body["mode"], "advanced")
        self.assertEqual(body["advanced_settings"], {"source_policy": {"exclude_domains": ["synthetic.example"], "after_date": "2026-01-09"}, "other": 3})
        self.assertNotIn("before_date", str(body))

    def test_event_only_and_no_date_never_add_date_controls(self):
        for cid in ("linkup-fast", "parallel-advanced"):
            for q in (question(), dated(basis="event")):
                request = build_request(self.selected(cid), q)
                self.assertEqual(request["date_mapping_state"], "no_publication_requirement")
                self.assertEqual(request["date_capability_receipt"]["sent_date_fields"], {})

    def test_exact_endpoint_and_mode_required(self):
        for key, value in (("endpoint", "https://synthetic.example/other"), ("method", "GET")):
            config = deepcopy(self.catalog["linkup-fast"])
            config[key] = value
            with self.assertRaises(ValueError):
                apply([config], {"linkup-fast": provider_dates.LINKUP_DATE_PROFILE})
        config = deepcopy(self.catalog["linkup-fast"])
        config["base_body"]["depth"] = "deep"
        self.assertFalse(provider_dates.capability(config)["reviewed"])

    def test_unsupported_native_1970_boundary_is_an_explicit_omission(self):
        q = dated(start="1970-01-02T00:00:00Z", end="1970-01-03T00:00:00Z")
        config = self.selected("linkup-fast")
        report = provider_dates.prepare([config], [q])
        self.assertTrue(report["supported_omissions"])
        self.assertEqual(report["providers"][0]["requests"][0]["state"], "owner_decision_required_documented_1970_boundary")

    def test_capabilities_expose_omissions_and_partial_or_mixed_controls(self):
        ids = ("exa-highlights", "perplexity-web", "valyu-search", "keenable-pro", "seltz-web-pro", "linkup-fast", "parallel-advanced", "brave-web", "tavily-news", "firecrawl-web", "you-com-web", "searlo-web", "oxylabs-search")
        providers = [self.catalog[cid] for cid in ids]
        report = provider_dates.prepare(providers, [dated()])
        self.assertEqual({r["configuration_id"] for r in report["supported_omissions"]}, {"linkup-fast", "parallel-advanced"})
        rows = {r["capability"]["configuration_id"]: r for r in report["providers"]}
        for cid in ("brave-web", "tavily-news", "firecrawl-web", "you-com-web", "searlo-web"):
            self.assertTrue(rows[cid]["requests"][0]["unsupported_for_fixed_publication"])
            self.assertTrue(rows[cid]["capability"]["limitations"])
        self.assertEqual(rows["oxylabs-search"]["capability"]["fields"], [])
        self.assertFalse(report["live_execution_authorized"])

    def test_searlo_query_constraints_are_retained_without_rolling_mapping(self):
        q = dated()
        q["provider_query"] = "site:synthetic.example before:2026-02-01 Aster OR observatory"
        config = self.catalog["searlo-web"]
        request = build_request(config, q)
        self.assertEqual(request["body"]["q"], q["provider_query"])
        self.assertNotIn("dateRestrict", request["body"])
        self.assertIn("operator", " ".join(provider_dates.capability(config)["limitations"]))
        receipt = provider_dates.prepare([config], [q])["providers"][0]["requests"][0]
        self.assertEqual(receipt["query_compatibility"], "documented_operator_or_email_incompatibility")
        q["provider_query"] = "Aster OR observatory"
        receipt = provider_dates.prepare([config], [q])["providers"][0]["requests"][0]
        self.assertEqual(receipt["query_compatibility"], "OR_chain_length_undocumented_requires_review")
        with self.assertRaises(ValueError):
            apply([config], {"searlo-web": provider_dates.PROFILE})

    def test_existing_brave_profile_still_works_and_mixed_filter_cannot_activate(self):
        config = apply([self.catalog["brave-web"]], {"brave-web": BRAVE_UNDECORATED})[0]
        self.assertEqual(config["base_body"]["text_decorations"], "false")
        self.assertNotIn("freshness", build_request(config, dated())["body"])
        with self.assertRaises(ValueError):
            apply([config], {"brave-web": provider_dates.LINKUP_DATE_PROFILE})

    def test_invalid_timezone_or_empty_publication_window_rejected(self):
        for q in (dated(start="2026-01-10T00:00:00"), dated(end="2026-01-10T00:00:00Z")):
            with self.assertRaises(ValueError):
                build_request(self.selected("linkup-fast"), q)


if __name__ == "__main__":
    unittest.main()
