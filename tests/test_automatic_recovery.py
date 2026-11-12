from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import json
import time
from openweb_news.codex_runtime import StdioClient,RuntimeStopped,recoverable_failure
from .test_codex_runtime import FakeProcess

from openweb_news.common import digest,load,private_directory,write_private
from openweb_news.ledger import Ledger
from openweb_news.recovery import Recovery,sibling_rule
from openweb_news.request_profiles import apply,BRAVE_UNDECORATED
from openweb_news._method.providers import configuration_catalog,build_request
from .helpers import question
from openweb_news import fixtures
from openweb_news.preparation import prepare
from openweb_news.resilience import POLICY
from openweb_news.runner import run


class AutomaticRecoveryTests(unittest.TestCase):
    def test_actual_rpc_rejection_stays_fatal_but_timeout_is_transient(self):
        class Process(FakeProcess):
            def emit(self,value):
                if 'id'in value:
                    if self.fault=='timeout':return
                    value=dict(id=value['id'],error=dict(code=-32000,message='synthetic rejected request'))
                return super().emit(value)
        for fault in ('reject','timeout'):
            process=Process([],fault=fault,env={});client=StdioClient(process)
            try:
                with self.assertRaises((RuntimeStopped,TimeoutError))as caught:
                    client.request('initialize',{},time.monotonic()+.05)
                self.assertEqual(recoverable_failure(caught.exception,client.failure),fault=='timeout')
                self.assertEqual(client.failure,'Codex protocol request timed out'if fault=='timeout'else'Codex rejected a protocol request')
            finally:client.close()
        self.assertFalse(recoverable_failure(RuntimeStopped(),'Codex protocol stream failed isolation or integrity checks'))

    def test_main_subscription_runtime_restart_retains_completed_searches_and_pro(self):
        with tempfile.TemporaryDirectory()as temp:
            root=Path(temp);cfg=fixtures.config(root/'run');cfg['automatic_recovery']=deepcopy(POLICY)
            cfg['reviewers']['codex']['transport']='app-server-stdio-v1'
            policy=cfg['execution_policy'];policy['spending_ceiling_usd']='150';policy['max_attempts_per_task']=3;policy['retry_policy'].update(backoff_seconds=[0,0],max_total=None,max_search=None,max_model=None)
            policy['scheduling']['accounts']['codex-subscription']['concurrency']=1
            policy['scheduling']['recovery']={k:sibling_rule('provider'if k.startswith('provider/')else'judge',v['concurrency'])for k,v in policy['scheduling']['accounts'].items()}
            for rule in policy['scheduling']['recovery'].values():rule['cooldown_seconds']=0
            package=prepare(cfg);instances=[];codex_calls=[];http_calls=[]
            class Runtime:
                def __init__(self,*args,**kwargs):
                    self.client=SimpleNamespace(failure=None,quota_evidence=[]);self.number=len(instances);instances.append(self)
                def __enter__(self):return self
                def __exit__(self,*args):pass
                def review(self,packet,folder):
                    codex_calls.append(packet['input']['case_id'])
                    if self.number==0:
                        self.client.failure='Codex runtime exited'
                        return dict(transport_success=False,complete=False,dispatch_state='uncertain',fatal_configuration=True,recoverable_runtime_failure=True,error_type='RuntimeStopped')
                    return fixtures.model('codex',load(root/'run/compiled'/(packet['input']['case_id']+'.json')),folder)
            def subscription(config,packet,folder,preflight_only=False,**kwargs):
                if preflight_only:return dict(transport_success=True)
                return fixtures.model('claude',load(root/'run/compiled'/(packet['input']['case_id']+'.json')),folder)
            def transport(wire,auth,credentials,folder,**kwargs):
                http_calls.append(wire)
                if 'configuration_id'in wire:
                    provider=next(p for p in package['providers']if p['configuration_id']==wire['configuration_id'])
                    return fixtures.search(wire,provider,folder)
                cid=json.loads(wire['body']['contents'][0]['parts'][0]['text'])['case_id']
                return fixtures.model('pro',load(root/'run/compiled'/(cid+'.json')),folder)
            approval=dict(decision='approved',package_sha256=digest(package),approved_by='fixture',approved_at='fixture')
            with patch('openweb_news.codex_runtime.CodexRuntime',Runtime),patch('openweb_news.backends.subscription',side_effect=subscription),patch('openweb_news.runner.https',side_effect=transport),patch('openweb_news.transport.Credentials.get',return_value='synthetic'):
                report=run(package,approval=approval)
            self.assertEqual(report['execution']['state'],'completed')
            self.assertGreaterEqual(len(instances),2)
            self.assertEqual(len(codex_calls),len(report['outcomes'])+1)
            self.assertEqual(len(http_calls),package['scope']['search_cells']+len(report['outcomes']))
    def test_full_runner_recovers_a_transient_without_manual_resume(self):
        with tempfile.TemporaryDirectory()as temp:
            root=Path(temp);cfg=fixtures.config(root/'run');cfg['automatic_recovery']=deepcopy(POLICY)
            policy=cfg['execution_policy'];policy['max_attempts_per_task']=3;policy['retry_policy']['backoff_seconds']=[0,0]
            policy['retry_policy'].update(max_total=None,max_search=None,max_model=None)
            policy['scheduling']['recovery']={k:sibling_rule('provider'if k.startswith('provider/')else'judge',v['concurrency'])for k,v in policy['scheduling']['accounts'].items()}
            for rule in policy['scheduling']['recovery'].values():rule['cooldown_seconds']=0
            package=prepare(cfg,mode='fixture');original=fixtures.search;count=[0]
            def search(*args):
                count[0]+=1
                if count[0]==1:raise ConnectionResetError('injected uncertain send')
                return original(*args)
            with patch('openweb_news.runner.fixtures.search',side_effect=search):report=run(package)
            self.assertEqual(report['execution']['state'],'completed')
            self.assertEqual(count[0],package['scope']['search_cells']+1)
            with patch('openweb_news.runner.fixtures.search',side_effect=AssertionError('resend')):
                resumed=run(package)
            self.assertEqual(resumed['outcome_counts'],report['outcome_counts'])

    def test_uncertain_resend_once_keeps_both_attempts_and_then_reuses(self):
        with tempfile.TemporaryDirectory()as temp:
            ledger=Ledger(temp,{'package':'test'},uncertain_resends=1)
            uncertain=dict(transport_success=False,complete=False,dispatch_state='uncertain')
            called=[];reserved=[]
            with ledger.locked():
                def call(folder):called.append(folder);return uncertain
                for _ in range(3):
                    ledger.execute('task',{'request':'same'},call,max_attempts=3,retry_failed=True,
                                   before_start=lambda *args:reserved.append(args))
                self.assertEqual(len(called),2)
                self.assertEqual(len(reserved),2)
                self.assertEqual(sum(e['event']=='started'for e in ledger.events()),2)
                self.assertEqual(sum(e['event']=='finished'for e in ledger.events()),2)
                for path in called:self.assertEqual(load(path/'result.json'),uncertain)

    def test_crash_reconciliation_preserves_partial_and_resumes_once(self):
        with tempfile.TemporaryDirectory()as temp:
            root=Path(temp);request={'request':'same'};aid=digest({'task_id':'task','attempt':1})
            ledger=Ledger(root,{'package':'test'},uncertain_resends=1)
            with ledger.locked():
                folder=private_directory(root/'attempts'/aid)
                write_private(folder/'request.json',request)
                write_private(folder/'partial.bin',b'partial native response')
                ledger.append(dict(event='started',task_id='task',attempt_id=aid,request_sha256=digest(request)))
            resumed=Ledger(root,{'package':'test'},uncertain_resends=1)
            with resumed.locked():
                self.assertTrue(resumed.events()[-1]['reconciled_after_interruption'])
                value=resumed.execute('task',request,lambda _:dict(transport_success=True,complete=True,dispatch_state='completed'),max_attempts=3,retry_failed=True)
                self.assertTrue(value['transport_success'])
                self.assertEqual((folder/'partial.bin').read_bytes(),b'partial native response')
                resumed.execute('task',request,lambda _:self.fail('completed resend'),max_attempts=3,retry_failed=True)
                self.assertEqual(sum(e['event']=='started'for e in resumed.events()),2)

    def test_transient_pauses_cool_down_but_auth_stops(self):
        rule=sibling_rule('judge',4);rule['transient_pause_action']='cooldown';rule['cooldown_seconds']=1
        rules={'account':rule};recovery=Recovery(rules)
        for i in range(4):
            at=i*10
            recovery.capacity('account','pro',at)
            recovery.observe(str(i),'account','pro',dict(transport_success=False,complete=False,dispatch_state='uncertain'),at)
            self.assertFalse(recovery.capacity('account','pro',at+2)['stopped'])
        recovery.observe('auth','account','pro',dict(http_status=401,transport_success=False,complete=True),100)
        self.assertTrue(recovery.capacity('account','pro',102)['stopped'])
        for status in (401,402,403):
            recovery=Recovery(rules)
            recovery.observe('temporary','account','pro',dict(transport_success=False,complete=False,dispatch_state='uncertain'),0)
            recovery.observe('permanent','account','pro',dict(http_status=status,transport_success=False,complete=True),.5)
            self.assertTrue(recovery.capacity('account','pro',10)['stopped'])

    def test_request_profile_changes_only_declared_brave_setting(self):
        brave=next(p for p in configuration_catalog()if p['configuration_id']=='brave-web')
        old=build_request(brave,question())
        new=build_request(apply([brave],{'brave-web':BRAVE_UNDECORATED})[0],question())
        self.assertNotIn('text_decorations',old['body'])
        self.assertEqual(new['body'].pop('text_decorations'),'false')
        # Configuration is versioned separately; other wire controls are unchanged.
        self.assertEqual(old['body'],new['body'])
        self.assertEqual(old['endpoint'],new['endpoint'])
        self.assertNotIn('text_decorations',brave['base_body'])


if __name__=='__main__':unittest.main()
