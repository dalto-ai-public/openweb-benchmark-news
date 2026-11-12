"""Synthetic regressions for safe lease renewal and excluded native interventions."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from datetime import datetime,timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from openweb_news import caching,fixtures,judging,runner,subscription_health
from openweb_news.common import digest,encoded,load,write_private,records
from openweb_news.completion_audit import inspect_ledger
from openweb_news.ledger import Ledger
from openweb_news.scheduling import AdmissionBlocked
from openweb_news.preparation import prepare
from . import test_caching
from .test_subscription_health import native_claude


def stamp(t):return datetime.fromtimestamp(t,timezone.utc).isoformat()


class CacheRecoveryTests(TestCase):
    def test_preexpiry_margin_prevents_crossing_expiry_and_resumes_same_lease(self):
        for version in ('expiry-leases-v1','expiry-leases-v2'):
            with self.subTest(version=version),TemporaryDirectory()as tmp,Ledger(tmp,{}).locked()as ledger:
                now=[100.0];created={};calls=[];helper=test_caching.CacheTests()
                def transport(wire,folder):
                    calls.append(wire);name='cachedContents/lease_'+str(len(calls));created[name]=now[0]+3600
                    return helper.native(name=name,createTime=stamp(now[0]),expireTime=stamp(created[name]))
                cache,semantic,_=helper.setup_cache(ledger,transport,lambda:now[0])
                renewal=dict(profile=version,epoch='a'*64)
                if version.endswith('v2'):renewal['renew_before_seconds']=120
                cache.config['renewal']=renewal
                cache.wire(semantic);now[0]=3699.8
                with ThreadPoolExecutor(max_workers=8)as pool:wires=list(pool.map(lambda _:cache.wire(semantic),range(16)))
                now[0]+=0.4
                codes=[200 if created[w['body']['cachedContent']]>now[0]else 403 for w in wires]
                self.assertEqual(set(codes),{200}if version.endswith('v2')else{403})
                self.assertEqual(len(calls),2 if version.endswith('v2')else 1)
                restarted,_,_=helper.setup_cache(ledger,transport,lambda:now[0]);restarted.config['renewal']=renewal
                before=len(calls);resumed=restarted.wire(semantic)
                if version.endswith('v2'):
                    self.assertEqual(resumed,wires[0]);self.assertEqual(len(calls),before)

    def test_new_policy_validates_margin_and_keeps_uncertain_creation_held(self):
        with TemporaryDirectory()as tmp,Ledger(tmp,{}).locked()as ledger:
            helper=test_caching.CacheTests();now=[100];calls=[]
            def transport(wire,folder):
                calls.append(wire)
                return helper.native(createTime=stamp(now[0]),expireTime=stamp(now[0]+3600))if len(calls)==1 else dict(complete=False,dispatch_state='uncertain')
            cache,wire,_=helper.setup_cache(ledger,transport,lambda:now[0])
            cache.config['renewal']=dict(profile='expiry-leases-v2',epoch='a'*64,renew_before_seconds=120)
            caching.validate(cache.config,cache.package['configuration']['execution_policy'])
            for bad in (0,-1,3600,True):
                config=deepcopy(cache.config);config['renewal']['renew_before_seconds']=bad
                with self.assertRaises(ValueError):caching.validate(config,cache.package['configuration']['execution_policy'])
            cache.wire(wire);now[0]=3580
            for _ in range(2):
                with self.assertRaises(AdmissionBlocked):cache.wire(wire)
            self.assertEqual(len(calls),2)
            restarted,_,_=helper.setup_cache(ledger,transport,lambda:now[0]);restarted.config['renewal']=deepcopy(cache.config['renewal'])
            with self.assertRaises(AdmissionBlocked):restarted.wire(wire)
            self.assertEqual(len(calls),2)

    def test_native_safeguard_is_classified_excluded_and_never_votes_or_resends(self):
        with TemporaryDirectory()as tmp:
            root=Path(tmp);cfg=fixtures.config(root/'native');package=prepare(cfg,mode='fixture')
            base=native_claude(fixtures.model)
            def model(role,compiled,folder):
                raw=base(role,compiled,folder)
                if role=='claude':
                    events=list(records(folder/'stdout.jsonl'));events[-1]['safety_stops']=1
                    write_private(folder/'stdout.jsonl',b''.join(encoded(e)for e in events),replace=True)
                return raw
            with patch.object(fixtures,'model',side_effect=model):report=runner.run(package)
            audit=inspect_ledger(root/'native')
            self.assertEqual(audit['unclassified'],[])
            self.assertGreater(audit['counts'].get('claude:excluded_intervention',0),0)
            for outcome in report['outcomes']:
                if 'claude'in outcome['positions']:
                    self.assertEqual(outcome['positions']['claude']['state'],'unjudged')
                    self.assertNotIn('claude',outcome['selected_roles'])
            event=next(e for e in records(root/'native/attempts.jsonl')if e['event']=='finished'and e['task_id'].startswith('claude-'))
            folder=root/'native/attempts'/event['attempt_id'];request=load(folder/'request.json');raw=load(folder/'result.json')
            self.assertFalse(subscription_health.audit(request,raw,folder))
            proof=subscription_health.excluded_intervention(request,raw,folder)
            self.assertFalse(proof['vote_eligible']);self.assertFalse(subscription_health.terminal_verified(request,raw,folder))
            wrong=deepcopy(raw);wrong['served_model']='wrong-model'
            self.assertIsNone(subscription_health.excluded_intervention(request,wrong,folder))
            with patch.object(fixtures,'search',side_effect=AssertionError('no search')),patch.object(fixtures,'model',side_effect=AssertionError('no model')):
                resumed=runner.run(package)
            self.assertEqual(resumed['outcomes'],report['outcomes'])

    def test_stopped_scope_is_durable_and_does_not_turn_held_primary_into_third(self):
        from openweb_news.recovery import sibling_rule
        with TemporaryDirectory()as tmp:
            root=Path(tmp);cfg=fixtures.config(root/'run');cfg['questions']=[dict(cfg['questions'][0],question_id='synthetic-cache-stop')]
            cfg['execution_policy']['scheduling']['recovery']={k:sibling_rule('provider'if k.startswith('provider/')else'judge',v['concurrency'])for k,v in cfg['execution_policy']['scheduling']['accounts'].items()}
            pro_account='gemini-api/OPENWEB_NEWS_GEMINI_API_KEY'
            cfg['execution_policy']['scheduling']['accounts'][pro_account]['concurrency']=1
            cfg['execution_policy']['scheduling']['recovery'][pro_account]=sibling_rule('judge',1)
            probe=prepare(cfg,mode='fixture');empty=dict(status='native_result',title=dict(state='missing'),content=dict(state='missing'),url=dict(state='missing'),metadata={},additional_content={},native_dates=dict(observations=[]))
            estimates={}
            for q in cfg['questions']:
                compiled=judging.compile_packet(judging.packet_for(q,probe['providers'][0],empty,'cache-plan'),'2000-01-01T00:00:00Z',config=probe['providers'][0])
                estimates[digest(caching.payload(judging.build_pro_request(compiled)))]=5000
            cfg['system_cache']=test_caching.cache_config();cfg['system_cache'].update(estimated_system_tokens=estimates,renewal=dict(profile='expiry-leases-v2',epoch='a'*64,renew_before_seconds=120))
            cfg['execution_policy'].update(spending_ceiling_usd='50',budget_accounting=caching.PROFILE)
            package=prepare(cfg,mode='fixture')
            calls=[];original=fixtures.model
            def model(role,compiled,folder):
                calls.append(role)
                return dict(transport_success=False,complete=True,dispatch_state='completed',http_status=403,raw_text='{}')if role=='pro'else original(role,compiled,folder)
            with patch.object(fixtures,'model',side_effect=model):
                report=runner.run(package)
            self.assertTrue((root/'run/status-events.jsonl').exists())
            self.assertTrue(any(e['mode']=='stopped'for e in records(root/'run/status-events.jsonl')))
            self.assertEqual(calls.count('pro'),1)
            # A terminal403 still follows missing-primary voting; held/not_sent
            # primary callbacks are pending and do not request extra thirds.
            self.assertEqual(calls.count('claude'),1)
            self.assertEqual(report['execution']['state'],'completed_with_holds')

    def test_drained_recovery_reuses_paid_successes_and_only_sends_missing_work(self):
        from openweb_news import cache_recovery,reuse
        from openweb_news.recovery import sibling_rule
        from openweb_news.accounting import reservation
        from openweb_news._method.providers import project_response
        from openweb_news.common import now
        with TemporaryDirectory()as tmp:
            root=Path(tmp);cfg=fixtures.config(root/'source/run');base=cfg['questions'][0]
            cfg['questions']=[dict(base,question_id='cache-first'),dict(base,question_id='cache-uncertain'),dict(base,question_id='cache-next')]
            cfg['provider_ids']=cfg['provider_ids'][:1];cfg['tie_order']=cfg['provider_ids'];cfg['execution_policy']['spending_ceiling_usd']='50'
            from openweb_news._method.providers import configuration_catalog
            chosen=next(p for p in configuration_catalog(include_optional=True)if p['configuration_id']==cfg['provider_ids'][0])
            provider_account='provider/'+chosen['auth']['secret_ref']
            cfg['execution_policy']['scheduling']['accounts']={k:v for k,v in cfg['execution_policy']['scheduling']['accounts'].items()if not k.startswith('provider/')or k==provider_account}
            cfg['execution_policy']['scheduling']['recovery']={k:sibling_rule('provider'if k.startswith('provider/')else'judge',v['concurrency'])for k,v in cfg['execution_policy']['scheduling']['accounts'].items()}
            probe=prepare(cfg,mode='fixture');empty=dict(status='native_result',title=dict(state='missing'),content=dict(state='missing'),url=dict(state='missing'),metadata={},additional_content={},native_dates=dict(observations=[]));estimates={}
            for q in cfg['questions']:
                comp=judging.compile_packet(judging.packet_for(q,probe['providers'][0],empty,'cache-plan'),'2000-01-01T00:00:00Z',config=probe['providers'][0])
                estimates[digest(caching.payload(judging.build_pro_request(comp)))]=5000
            cfg['system_cache']=test_caching.cache_config();cfg['system_cache'].update(estimated_system_tokens=estimates,renewal=dict(profile='expiry-leases-v1',epoch='a'*64))
            cfg['execution_policy']['budget_accounting']=caching.PROFILE
            source=prepare(cfg,mode='fixture');write_private(root/'source/package.json',source)
            provider=source['providers'][0];q=source['questions'][0];search=source['search_requests'][0]
            def priced(request):
                return dict(request,reservation_usd=str(reservation(request,source)),reservation_basis='conservative estimate; not invoice or guaranteed upper bound')
            with Ledger(root/'source/run',dict(package_sha256=digest(source),mode='fixture')).locked()as ledger:
                request=priced(dict(search,kind='search',auth_reference=provider['auth']))
                raw=ledger.execute('search-'+digest(dict(configuration=provider['configuration_id'],question=q['question_id'])),request,lambda folder:fixtures.search(search,provider,folder))
                projected=project_response(provider,raw['raw_text'].encode(),raw['captured_at']);position=projected['positions'][0]
                cid='occurrence-'+digest(dict(question=q,configuration=provider,position=position))
                compiled=judging.compile_packet(judging.packet_for(q,provider,position,cid),raw['captured_at'],config=provider)
                semantic=judging.build_pro_request(compiled);group=caching.payload(semantic);key=digest(group);resource=dict(group_id=key,name='cachedContents/old_lease')
                t=datetime.fromisoformat('2026-10-28T00:00:00+00:00').timestamp()
                ctask='cache-create-'+digest(dict(group_id=key,epoch='a'*64,lease=0))
                creq=dict(kind='cache',role='pro',wire=dict(method='POST',endpoint=caching.ENDPOINT,body=dict(group,ttl='3600s')),reservation_usd='0.5',reservation_basis='synthetic retained cache hold')
                with patch('openweb_news.ledger.now',side_effect=[stamp(t),stamp(t+.1),stamp(t+3599.8),stamp(t+3600.2)]):
                    ledger.execute(ctask,creq,lambda folder:test_caching.CacheTests().native(name=resource['name'],createTime=stamp(t),expireTime=stamp(t+3600)))
                    preq=priced(dict(kind='model',role='pro',backend='gemini-api',wire=semantic))
                    def rejected(folder):
                        write_private(folder/'effective-wire.json',caching.cache_wire(semantic,resource,group))
                        body=encoded(dict(error=dict(code=403,status='PERMISSION_DENIED',message='CachedContent not found (or permission denied)')))
                        return dict(http_status=403,complete=True,transport_success=False,dispatch_state='completed',raw_text=body.decode(),retained_sha256=digest(body),recovery_validation_state='unjudged')
                    ledger.execute('pro-'+cid,preq,rejected)
                sreq=priced(dict(kind='model',role='codex',backend=cfg['reviewers']['codex']['backend'],backend_config=cfg['reviewers']['codex'],blind_packet=judging.blind_packet(compiled)))
                ledger.execute('codex-'+cid,sreq,lambda folder:dict(fixtures.model('codex',compiled,folder),complete=True,dispatch_state='completed',recovery_validation_state='valid'))
                q2=source['questions'][1];search2=source['search_requests'][1]
                req2=priced(dict(search2,kind='search',auth_reference=provider['auth']))
                raw2=ledger.execute('search-'+digest(dict(configuration=provider['configuration_id'],question=q2['question_id'])),req2,lambda folder:fixtures.search(search2,provider,folder))
                position2=project_response(provider,raw2['raw_text'].encode(),raw2['captured_at'])['positions'][0]
                cid2='occurrence-'+digest(dict(question=q2,configuration=provider,position=position2))
                compiled2=judging.compile_packet(judging.packet_for(q2,provider,position2,cid2),raw2['captured_at'],config=provider)
                req2=priced(dict(kind='model',role='pro',backend='gemini-api',wire=judging.build_pro_request(compiled2)))
                ledger.execute('pro-'+cid2,req2,lambda folder:dict(transport_success=False,complete=False,dispatch_state='uncertain',error_type='TimeoutError',recovery_validation_state='unjudged'))

            original_ledger=(root/'source/run/attempts.jsonl').read_bytes()
            reference=reuse.freeze(root/'source/package.json',root/'snapshot')
            config,target=cache_recovery.native_plan(source,root/'target',reference,None)
            self.assertEqual(config['execution_policy']['spending_ceiling_usd'],'50')
            carried=reuse.committed(root/'snapshot',source,list(records(root/'snapshot/events.jsonl')))
            self.assertEqual(target['reservation_forecast']['carried_reservation_usd'],str(carried))
            from decimal import Decimal
            self.assertEqual(target['reservation_forecast']['remaining_admission_usd'],str(Decimal('50')-carried))
            self.assertEqual(target['questions'],source['questions'])
            self.assertGreater(len(cache_recovery.archived_attempts(target)),0)
            calls=[];model=fixtures.model;collect=fixtures.search
            def collected(request,provider,folder):
                calls.append(('search',request['question_id']));return collect(request,provider,folder)
            def judged(role,compiled,folder):
                calls.append((role,compiled['packet']['question']['question_id']));return model(role,compiled,folder)
            with patch.object(fixtures,'search',side_effect=collected),patch.object(fixtures,'model',side_effect=judged):report=runner.run(target)
            self.assertEqual(calls.count(('search','cache-first')),0)
            self.assertEqual(calls.count(('pro','cache-first')),0)
            self.assertEqual(calls.count(('pro','cache-uncertain')),0)
            self.assertEqual(calls.count(('search','cache-uncertain')),0)
            self.assertEqual(report['execution']['state'],'completed')
            self.assertEqual(len(report['execution']['retained_terminal_uncertainty_tasks']),1)
            self.assertEqual(calls.count(('codex','cache-first')),0)
            self.assertEqual(calls.count(('search','cache-next')),1)
            self.assertEqual(calls.count(('pro','cache-next')),1)
            self.assertEqual(calls.count(('codex','cache-next')),1)
            self.assertEqual((root/'source/run/attempts.jsonl').read_bytes(),original_ledger)
            with patch.object(fixtures,'search',side_effect=AssertionError('no second search')),patch.object(fixtures,'model',side_effect=AssertionError('no second model')):
                resumed=runner.run(target)
            self.assertEqual(report['outcomes'],resumed['outcomes'])
            from openweb_news import batch
            next_config,next_package=batch.native_plan(target,[dict(base,question_id='next-cohort')],root/'future','50','0')
            self.assertNotIn('cache_expiry_recovery',next_config)
            self.assertNotIn('reuse_snapshot',next_config)
            self.assertEqual(next_config['system_cache']['renewal']['profile'],'expiry-leases-v2')
            self.assertEqual(next_config['system_cache']['renewal']['renew_before_seconds'],120)
            import shutil
            events=list(records(root/'snapshot/events.jsonl'))
            rejection=next(e for e in events if e['event']=='finished'and load(root/'snapshot'/e['result_path']).get('http_status')==403)
            create=next(e for e in events if e['event']=='finished'and e['task_id'].startswith('cache-create-'))
            for fault in ('different-message','401','402','expiry-not-crossed','already-expired','wrong-resource'):
                with self.subTest(rejection_guard=fault):
                    dest=root/('bad-'+fault);shutil.copytree(root/'snapshot',dest);ev=deepcopy(events)
                    raw=load(dest/rejection['result_path'])
                    if fault=='different-message':
                        body=encoded(dict(error=dict(code=403,status='PERMISSION_DENIED',message='Permission denied for this API key')))
                        raw.update(raw_text=body.decode(),retained_sha256=digest(body));write_private(dest/rejection['result_path'],raw,replace=True)
                    elif fault in {'401','402'}:
                        raw['http_status']=int(fault);write_private(dest/rejection['result_path'],raw,replace=True)
                    elif fault=='expiry-not-crossed':
                        response=load(dest/create['result_path']);body=json.loads(response['raw_text']);body['expireTime']=stamp(t+7200)
                        response['raw_text']=encoded(body).decode();write_private(dest/create['result_path'],response,replace=True)
                    elif fault=='already-expired':
                        next(e for e in ev if e['event']=='started'and e['attempt_id']==rejection['attempt_id'])['recorded_at']=stamp(t+3600.1)
                    else:
                        wire=load(dest/'attempts'/rejection['attempt_id']/'effective-wire.json');wire['body']['cachedContent']='cachedContents/not_the_saved_lease'
                        write_private(dest/'attempts'/rejection['attempt_id']/'effective-wire.json',wire,replace=True)
                    with self.assertRaises(ValueError):cache_recovery.incident(source,dest,ev)
            altered=deepcopy(target);altered['configuration']['execution_policy']['spending_ceiling_usd']='51'
            with self.assertRaises(ValueError):cache_recovery.compatible(source,altered)
            altered=deepcopy(target);altered['code']['runner.py']='unbound-target-code'
            with self.assertRaises(ValueError):cache_recovery.compatible(source,altered)
            for section in ('assets','primary'):
                bad=deepcopy(target);bad[section]={}
                with self.assertRaises(ValueError):cache_recovery.compatible(source,bad)
