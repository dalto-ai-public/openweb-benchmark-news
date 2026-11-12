from copy import deepcopy
from decimal import Decimal
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from openweb_news import fixtures, fresh, prior_work, reuse
from openweb_news.common import code_bindings, digest, load, write_private
from openweb_news.preparation import prepare
from openweb_news.runner import run
from .helpers import question


class ExpansionTests(unittest.TestCase):
    def test_four_phase_chain_preserves_uncertainty_accounting_and_quota_journal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            config = fixtures.config(root / 'phase-1')
            config['execution_policy']['spending_ceiling_usd'] = '20'
            all_questions = [question('phase-question-' + str(i)) for i in range(8)]
            original_search, original_model = fixtures.search, fixtures.model
            searched, judged, uncertain = [], [], []

            def search(request, *args):
                searched.append((request['question_id'], request['configuration_id']))
                return original_search(request, *args)

            def model(role, compiled, folder):
                key = (role, compiled['packet']['case_id'])
                self.assertNotIn(key, judged)
                judged.append(key)
                if role == 'codex' and not uncertain:
                    uncertain.append('codex-' + key[1])
                    return dict(transport_success=False, complete=False, dispatch_state='uncertain')
                return original_model(role, compiled, folder)

            from openweb_news.common import encoded
            quota_event = dict(event_id='early-native-quota', observed_at=100,
                               reason='native_http_429', thread_id='synthetic-thread')
            previous = None
            with patch('openweb_news.fixtures.search', side_effect=search), \
                 patch('openweb_news.fixtures.model', side_effect=model), \
                 patch('openweb_news.runner.reservation', return_value=Decimal('0.01')):
                for phase, count in enumerate((2, 4, 6, 8), 1):
                    config['questions'] = all_questions[:count]
                    config['run_directory'] = str(root / ('phase-' + str(phase)))
                    if previous:
                        config['reuse_snapshot'] = reuse.freeze(previous, root / ('snapshot-' + str(phase)))
                        config['reuse_scope'] = 'exact-method-expansion-v1'
                    package = prepare(config, mode='fixture')
                    package_path = root / ('phase-' + str(phase) + '.json')
                    write_private(package_path, package)
                    result = run(package)
                    folder = Path(config['run_directory'])
                    quota_path = folder / 'codex-runtime/quota-events.jsonl'
                    if phase == 1:
                        write_private(quota_path, encoded(quota_event))
                    else:
                        self.assertEqual(quota_path.read_bytes(), encoded(quota_event))
                    events = load_events(folder / 'attempts.jsonl')
                    starts = [e for e in events if e['event'] == 'started']
                    self.assertEqual(len(searched), count * 2)
                    self.assertEqual(len(set(searched)), len(searched))
                    self.assertEqual(sum(e['task_id'] == uncertain[0] for e in starts), 1)
                    self.assertEqual(Decimal(result['execution']['committed_and_held_usd']),
                                     Decimal(len(starts)) * Decimal('0.01'))
                    with patch('openweb_news.fixtures.search', side_effect=AssertionError('repeat')), \
                         patch('openweb_news.fixtures.model', side_effect=AssertionError('repeat')):
                        self.assertEqual(run(package)['outcomes'], result['outcomes'])
                    previous = package_path
                changed = deepcopy(config)
                changed['run_directory'] = str(root / 'changed-effort')
                changed['reviewers']['codex']['reasoning_effort'] = 'low'
                with self.assertRaises(ValueError):
                    prepare(changed, mode='fixture')

    def test_adopted_scale_budget_continuation_cannot_clear_real_stops(self):
        from openweb_news.scale import compatible
        source=dict(configuration=dict(scale_expansion={"frozen":"pilot-proof"},run_directory="old",reuse_snapshot={"old":"snapshot"},
            execution_policy=dict(spending_ceiling_usd="150",scheduling=dict(repeated_pause_stop=2),max_attempts_per_task=3),reviewers={"codex":"medium"}))
        target=deepcopy(source)
        target["configuration"].update(run_directory="new",reuse_snapshot={"new":"snapshot"})
        target["configuration"]["execution_policy"]["spending_ceiling_usd"]="5000"
        self.assertFalse(compatible(source,target))
        for mutate in (lambda p:p["configuration"]["execution_policy"]["scheduling"].update(repeated_pause_stop=999),
                       lambda p:p["configuration"]["execution_policy"].update(max_attempts_per_task=99),
                       lambda p:p["configuration"]["reviewers"].update(codex="low")):
            bad=deepcopy(target);mutate(bad)
            with self.assertRaises(ValueError):compatible(source,bad)

    def test_narrow_accounting_code_fix_requires_explicit_proof_and_reuses_all_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            old_code = code_bindings()
            old_code['accounting.py'] = '0' * 64
            cfg = fixtures.config(root / 'source')
            with patch('openweb_news.preparation.code_bindings', return_value=old_code):
                source = prepare(cfg, mode='fixture')
                original = run(source)
            write_private(root / 'source.json', source)
            cfg['run_directory'] = str(root / 'continued')
            cfg['reuse_snapshot'] = reuse.freeze(root / 'source.json', root / 'snapshot')
            with self.assertRaises(ValueError):
                prepare(cfg, mode='fixture')
            cfg['reuse_code_compatibility'] = dict(profile='reservation-metadata-idempotency-v1',
                source_package_sha256=digest(source), source_code_sha256=digest(source['code']),
                target_code_sha256=digest(code_bindings()))
            continued = prepare(cfg, mode='fixture')
            with patch('openweb_news.fixtures.search', side_effect=AssertionError('no repeat')), patch('openweb_news.fixtures.model', side_effect=AssertionError('no repeat')):
                result = run(continued)
            self.assertEqual(result['outcomes'], original['outcomes'])
            self.assertEqual(result['execution'].get('scheduled_tasks', 0), 0)
            from openweb_news.accounting import reservation as original_price
            def wrong_price(request, package):
                amount = original_price(request, package)
                return amount + Decimal('0.000001') if request.get('backend') == 'gemini-api' else amount
            with patch('openweb_news.accounting.reservation', side_effect=wrong_price), self.assertRaises(ValueError):
                prepare(cfg, mode='fixture')
            cfg['reuse_code_compatibility']['source_package_sha256'] = 'wrong'
            with self.assertRaises(ValueError):
                prepare(cfg, mode='fixture')
            other = deepcopy(source)
            other['code']['judging.py'] = '1' * 64
            target = deepcopy(continued)
            target['configuration']['reuse_code_compatibility'].update(source_package_sha256=digest(other), source_code_sha256=digest(other['code']))
            self.assertFalse(reuse.compatible_accounting_fix(other, target))

    def test_quota_evidence_survives_snapshot_and_append_on_same_run_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            config = fixtures.config(root / "source")
            package = prepare(config, mode="fixture")
            run(package)
            write_private(root / "source.json", package)
            from openweb_news.common import encoded
            first = dict(event_id="first", observed_at=100, reason="native_http_429", thread_id="old")
            write_private(root / "source/codex-runtime/quota-events.jsonl", encoded(first))
            ref = reuse.freeze(root / "source.json", root / "snapshot")
            config["run_directory"] = str(root / "continued")
            config["reuse_snapshot"] = ref
            continued = prepare(config, mode="fixture")
            run(continued)
            quota = root / "continued/codex-runtime/quota-events.jsonl"
            self.assertEqual(quota.read_bytes(), encoded(first))
            second = dict(first, event_id="second", observed_at=110)
            quota.write_bytes(encoded(first) + encoded(second))
            run(continued)
            self.assertEqual(quota.read_bytes(), encoded(first) + encoded(second))
            quota.write_bytes(encoded(second))
            with self.assertRaises(ValueError):
                run(continued)

    def test_expansion_reuses_overlap_and_uncertainty_then_resumes_without_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            config = fixtures.config(root / "pilot")
            source = prepare(config, mode="fixture")
            write_private(root / "source.json", source)
            original_model = fixtures.model
            picked = []
            def model(role, compiled, folder):
                if role == "codex" and not picked:
                    picked.append("codex-" + compiled["packet"]["case_id"])
                    return dict(transport_success=False, complete=False, dispatch_state="uncertain")
                return original_model(role, compiled, folder)
            with patch("openweb_news.fixtures.model", side_effect=model):
                initial = run(source)
            ref = reuse.freeze(root / "source.json", root / "snapshot")
            config["run_directory"] = str(root / "expanded")
            config["questions"].append(question("new-only"))
            config["reuse_snapshot"] = ref
            config["reuse_scope"] = "exact-method-expansion-v1"
            expanded = prepare(config, mode="fixture")
            sent = []
            original_search = fixtures.search
            def search(request, *args):
                sent.append(request["question_id"])
                return original_search(request, *args)
            with patch("openweb_news.fixtures.search", side_effect=search):
                result = run(expanded)
            self.assertEqual(sent, ["new-only", "new-only"])
            self.assertEqual(sum(e["task_id"] == picked[0] and e["event"] == "started" for e in load_events(root / "expanded/attempts.jsonl")), 1)
            old = {o["case_id"]: o for o in initial["outcomes"]}
            self.assertTrue(all(o == old[o["case_id"]] for o in result["outcomes"] if o["case_id"] in old))
            with patch("openweb_news.fixtures.search", side_effect=AssertionError("repeat")), patch("openweb_news.fixtures.model", side_effect=AssertionError("repeat")):
                resumed = run(expanded)
            self.assertEqual(resumed["outcomes"], result["outcomes"])
            config["questions"][0]["revision"] += 1
            with self.assertRaises(ValueError):
                prepare(config, mode="fixture")

    def test_prior_costs_self_contained_fresh_ancestor_once_and_overlap_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            config = fixtures.config(root / "original")
            config["execution_policy"]["spending_ceiling_usd"] = "10"
            original = prepare(config, mode="fixture")
            write_private(root / "original.json", original)
            with patch("openweb_news.runner.reservation", return_value=Decimal("0.01")):
                old_report = run(original)
            reference = fresh.freeze(root / "original.json", root / "fresh-source")
            config["run_directory"] = str(root / "fresh")
            config["fresh_judging"] = reference
            config["execution_policy"]["budget_accounting"] = "source-native-usage-v1"
            new_epoch = prepare(config, mode="fixture")
            write_private(root / "fresh.json", new_epoch)
            with patch("openweb_news.runner.reservation", return_value=Decimal("0.01")):
                new_report = run(new_epoch)
            ref = prior_work.freeze(root / "fresh.json", root / "obligations")
            value = prior_work.read(ref)
            self.assertEqual(Decimal(value["committed_and_held_usd"]), Decimal(new_report["execution"]["committed_and_held_usd"]))
            shutil.rmtree(root / "original")
            shutil.rmtree(root / "fresh")
            shutil.rmtree(root / "fresh-source")
            config = fixtures.config(root / "disjoint")
            config["execution_policy"]["spending_ceiling_usd"] = "10"
            config["prior_work"] = ref
            with self.assertRaises(ValueError):
                prepare(config, mode="fixture")
            config["questions"] = [question("new-only")]
            package = prepare(config, mode="fixture")
            report = run(package)
            self.assertEqual(report["execution"]["committed_and_held_usd"], value["committed_and_held_usd"])
            events_before = (root / "disjoint/attempts.jsonl").read_bytes()
            run(package)
            self.assertEqual((root / "disjoint/attempts.jsonl").read_bytes(), events_before)
            manifest = load(ref["manifest_path"])
            manifest["histories"].append(deepcopy(manifest["histories"][0]))
            write_private(Path(ref["manifest_path"]), manifest, replace=True)
            with self.assertRaises(ValueError):
                prior_work.read(dict(ref, sha256=digest(manifest)))


def load_events(path):
    from openweb_news.common import records
    return list(records(path))
