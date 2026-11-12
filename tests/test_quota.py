from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from openweb_news.quota import QuotaGate, window_status, WAIT_REFRESH_SECONDS
from openweb_news.scheduling import Pipeline


def limits(used=100, reset=100):
    return dict(limitId='codex', primary=dict(usedPercent=used, resetsAt=reset, windowDurationMins=300), secondary=None)


class QuotaTests(unittest.TestCase):
    def test_external_reset_is_detected_early_without_credit_consumption(self):
        now=[50];client=Mock()
        client.request.side_effect=[dict(rateLimits=limits(reset=86400)),dict(rateLimits=limits(0,172800))]
        with tempfile.TemporaryDirectory()as tmp:
            gate=QuotaGate(client,Path(tmp),clock=lambda:now[0])
            self.assertEqual(gate.capacity('codex-subscription')['ready_at'],86400)
            now[0]+=WAIT_REFRESH_SECONDS-1
            self.assertEqual(gate.capacity('codex-subscription')['ready_at'],86400)
            self.assertEqual(client.request.call_count,1)
            now[0]+=1
            self.assertEqual(gate.capacity('codex-subscription')['ready_at'],0)
            self.assertEqual(client.request.call_count,2)
            now[0]+=WAIT_REFRESH_SECONDS*2
            self.assertEqual(gate.capacity('codex-subscription')['ready_at'],0)
            self.assertEqual(client.request.call_count,2)
            self.assertTrue(all(c.args[0]=='account/rateLimits/read'for c in client.request.call_args_list))
            from openweb_news.common import load
            self.assertFalse(load(Path(tmp)/'quota-status.json')['reset_credits_consumed'])

    def test_periodic_refresh_retains_wait_and_real_account_stops(self):
        now=[50];client=Mock()
        client.request.side_effect=[dict(rateLimits=limits(reset=86400)),dict(rateLimits=limits(reset=86400)),
            dict(rateLimits=dict(limits(0,172800),spendControlReached=True))]
        with tempfile.TemporaryDirectory()as tmp:
            gate=QuotaGate(client,Path(tmp),clock=lambda:now[0])
            gate.capacity('codex-subscription');now[0]+=WAIT_REFRESH_SECONDS
            self.assertEqual(gate.capacity('codex-subscription')['ready_at'],86400)
            now[0]+=WAIT_REFRESH_SECONDS
            self.assertTrue(gate.capacity('codex-subscription')['stopped'])

    def test_window_wait_needs_native_reset_and_rechecked_capacity(self):
        now = [50]
        client = Mock()
        client.request.side_effect = [dict(rateLimits=limits()), dict(rateLimits=limits(0, 200))]
        with tempfile.TemporaryDirectory() as tmp:
            gate = QuotaGate(client, Path(tmp), clock=lambda:now[0])
            self.assertEqual(gate.capacity('codex-subscription')['ready_at'], 100)
            now[0] = 100
            self.assertEqual(gate.capacity('codex-subscription')['ready_at'], 0)
            self.assertEqual(client.request.call_count, 2)
            self.assertTrue(all(c.args[0] == 'account/rateLimits/read' for c in client.request.call_args_list))
            self.assertFalse(gate.capacity('other')['stopped'])

    def test_two_exhausted_windows_do_not_hold_between_their_resets(self):
        now=[50];both=dict(limits(reset=100),secondary=dict(usedPercent=100,resetsAt=200))
        client=Mock();client.request.side_effect=[dict(rateLimits=both),dict(rateLimits=limits(0,300))]
        with tempfile.TemporaryDirectory() as tmp:
            gate=QuotaGate(client,Path(tmp),clock=lambda:now[0])
            self.assertEqual(gate.capacity('codex-subscription')['ready_at'],200)
            now[0]=100
            self.assertFalse(gate.capacity('codex-subscription')['stopped'])
            self.assertEqual(gate.capacity('codex-subscription')['ready_at'],200)
            now[0]=200
            self.assertEqual(gate.capacity('codex-subscription')['ready_at'],0)
            self.assertEqual(client.request.call_count,2)

    def test_unknown_stale_exhaustion_credit_limit_and_other_bucket_hold(self):
        for value in (None, {}, limits(reset=40), dict(limits(), spendControlReached=True),
                      dict(limits(), limitId='other'), dict(limits(), rateLimitReachedType='workspace_owner_credits_depleted')):
            self.assertTrue(window_status(value, 50)['stopped'])
        both = dict(limits(reset=100), secondary=dict(usedPercent=100,resetsAt=200))
        self.assertEqual(window_status(both,50)['ready_at'],200)

    def test_multi_day_wait_rechecks_reset_keeps_other_accounts_running(self):
        now=[0];order=[]
        client=Mock();client.request.side_effect=lambda *args:dict(rateLimits=limits(reset=86400*3)if now[0]<86400*3 else limits(0,86400*4))
        with tempfile.TemporaryDirectory() as tmp:
            gate=QuotaGate(client,Path(tmp),clock=lambda:now[0])
            sleeps=[]
            def sleep(seconds):
                sleeps.append(seconds);now[0]+=seconds
            pipe=Pipeline({a:dict(concurrency=1,minimum_start_interval_seconds=0)for a in ('codex-subscription','pro')},clock=lambda:now[0],sleeper=sleep)
            pipe.quota_gate=gate
            pipe.submit('codex-subscription',lambda:order.append('codex'),lambda raw:None)
            pipe.submit('pro',lambda:order.append('pro'),lambda raw:None)
            pipe.run()
            self.assertEqual(order,['pro','codex']);self.assertLessEqual(max(sleeps),30)
            self.assertGreater(client.request.call_count,2)
            self.assertGreaterEqual(now[0],86400*3)

    def test_repeated_structured_window_notifications_do_not_create_recovery_errors(self):
        from openweb_news.quota import verified_window_event
        from openweb_news.concurrency import quota_signal
        value=limits();value["rateLimitReachedType"]="rate_limit_reached"
        reason=quota_signal(dict(method="account/rateLimits/updated",params=dict(rateLimits=value)))
        event=dict(reason=reason,observed_at=50,thread_id=None,native_window_status=value,native_window_reset_at=100)
        self.assertTrue(verified_window_event(event))
        from openweb_news.ledger import Ledger
        from openweb_news.scheduling import Admission
        from openweb_news.recovery import sibling_rule
        with tempfile.TemporaryDirectory() as tmp, Ledger(tmp, {}).locked() as ledger:
            pipe=Pipeline({'codex-subscription':dict(concurrency=1,minimum_start_interval_seconds=0)},recovery={'codex-subscription':sibling_rule('judge',1)})
            admission=Admission(ledger,dict(spending_ceiling_usd='1'),package=dict(configuration=dict(subscription_quota={'profile':'native-window-resume-v1'})))
            admission.restore_pacing(ledger,pipe,{},[dict(event,event_id=str(i))for i in range(10)])
            self.assertEqual(pipe.recovery.scope('codex-subscription')['pauses'],0)
            actual=dict(event,event_id='actual',thread_id='real-turn')
            admission.restore_pacing(ledger,pipe,{},[actual])
            self.assertEqual(pipe.recovery.scope('codex-subscription')['consecutive_errors'],1)
        self.assertFalse(verified_window_event(dict(event,thread_id="actual-model-error")))
        self.assertFalse(verified_window_event(dict(event,native_window_reset_at=200)))
        self.assertFalse(verified_window_event(dict(event,native_window_status=dict(value,spendControlReached=True))))

    def test_quota_gate_never_clears_actual_persistent_recovery_stop(self):
        from openweb_news.recovery import sibling_rule
        now=[0];account='codex-subscription'
        pipe=Pipeline({account:dict(concurrency=1,minimum_start_interval_seconds=0)},recovery={account:sibling_rule('judge',1)},clock=lambda:now[0])
        pipe.recovery.scope(account)['mode']='stopped'
        pipe.quota_gate=Mock();pipe.quota_gate.capacity.return_value=dict(stopped=False,ready_at=0)
        seen=[]
        pipe.submit(account,lambda:self.fail('stopped account dispatched'),seen.append)
        pipe.run();self.assertEqual(seen[0]['dispatch_state'],'not_sent')
