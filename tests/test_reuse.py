from copy import deepcopy
import io
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from openweb_news import fixtures, reuse
from openweb_news.common import digest, encoded, load, write_private
from openweb_news.ledger import Ledger
from openweb_news.preparation import prepare
from openweb_news.runner import run


class ReuseTests(unittest.TestCase):
    def test_trusted_root_alias_works_but_symlinked_evidence_does_not(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            path, _, original = self.source(root)
            alias = root / "root-alias"
            alias.symlink_to(root, target_is_directory=True)
            reference = reuse.freeze(alias / path.name, alias / "snapshot")
            package = self.continuation(root, reference)
            package["configuration"]["reuse_snapshot"]["manifest_path"] = str(alias / "snapshot/manifest.json")
            # Rebuild after choosing the equivalent trusted alias, so its path is bound.
            config = fixtures.config(alias / "continued")
            config["reuse_snapshot"] = package["configuration"]["reuse_snapshot"]
            continued = prepare(config, mode="fixture")
            self.assertEqual(run(continued)["scores"], original["scores"])
            self.assertEqual(reuse.safe_file(alias, "file.json"), root / "file.json")
            (root / "linked.json").symlink_to(path)
            with self.assertRaises(ValueError):
                reuse.safe_file(alias, "linked.json")

    def source(self, root, *, config=None):
        config = config or fixtures.config(root / "source-run")
        package = prepare(config, mode="fixture")
        report = run(package)
        path = root / "source-package.json"
        write_private(path, package)
        return path, package, report

    def continuation(self, root, reference, *, name="continued", ceiling="2"):
        config = fixtures.config(root / name)
        config["reuse_snapshot"] = reference
        config["execution_policy"]["spending_ceiling_usd"] = ceiling
        return prepare(config, mode="fixture")

    def test_completed_calls_and_empty_invalid_results_reuse_without_any_dispatch(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            original_model = fixtures.model
            def invalid_primary(role, compiled, folder):
                if role == "pro" and compiled["packet"]["question"]["question_id"] == "synthetic-primary-failure":
                    from openweb_news.judging import METHOD
                    raw = encoded(dict(modelVersion=METHOD["primary"]["model"], candidates=[dict(finishReason="STOP", content=dict(parts=[dict(text="{}")]))]))
                    write_private(folder / "response.bin", raw)
                    return dict(transport_success=True, raw_text=raw.decode(), http_status=200, complete=True)
                return original_model(role, compiled, folder)
            with patch("openweb_news.fixtures.model", side_effect=invalid_primary):
                source_path, source, original = self.source(root)
            original_events = (root / "source-run/attempts.jsonl").read_bytes()
            reference = reuse.freeze(source_path, root / "snapshot")
            package = self.continuation(root, reference)
            with patch("openweb_news.fixtures.search", side_effect=AssertionError("search must reuse")), patch("openweb_news.fixtures.model", side_effect=AssertionError("model must reuse")):
                result = run(package)
                resumed = run(package)
            self.assertEqual(result["scores"], original["scores"])
            self.assertEqual(result["outcomes"], original["outcomes"])
            self.assertEqual(result["execution"]["reused_tasks"], 28)
            self.assertEqual(result["execution"].get("scheduled_tasks", 0), 0)
            self.assertEqual(resumed["scores"], result["scores"])
            self.assertEqual((root / "continued/attempts.jsonl").read_bytes(), original_events)
            self.assertEqual((root / "source-run/attempts.jsonl").read_bytes(), original_events)
            self.assertEqual(result["execution"]["reuse"]["source_package_sha256"], digest(source))
            self.assertTrue(any(row["states"]["pro"] == "invalid" for row in result["outcomes"]))

    def test_ceiling_continuation_sends_only_remaining_work_and_charges_old_holds_once(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            config = fixtures.config(root / "source-run")
            config["execution_policy"]["spending_ceiling_usd"] = "0.35"
            with patch("openweb_news.runner.reservation", return_value="0.1"):
                path, _, partial = self.source(root, config=config)
                reference = reuse.freeze(path, root / "snapshot")
                package = self.continuation(root, reference, ceiling="4")
                original_search, original_model = fixtures.search, fixtures.model
                sent = []
                def search(*args):
                    sent.append("search")
                    return original_search(*args)
                def model(*args):
                    sent.append("model")
                    return original_model(*args)
                with patch("openweb_news.fixtures.search", side_effect=search), patch("openweb_news.fixtures.model", side_effect=model):
                    result = run(package)
            self.assertEqual(partial["execution"]["retained_started_attempts"], 3)
            self.assertEqual(result["execution"]["state"], "completed")
            self.assertEqual(result["execution"]["reuse"]["carried_reservation_usd"], "0.3")
            self.assertEqual(package["reservation_forecast"]["remaining_admission_usd"], "3.7")
            self.assertEqual(len(sent), 25)
            self.assertEqual(result["execution"]["retained_started_attempts"], 28)

    def test_chain_is_self_contained_and_does_not_duplicate_attempts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            path, _, original = self.source(root)
            first = self.continuation(root, reuse.freeze(path, root / "snapshot-one"))
            run(first)
            first_path = root / "first-package.json"
            write_private(first_path, first)
            reference = reuse.freeze(first_path, root / "snapshot-two")
            shutil.rmtree(root / "source-run")
            shutil.rmtree(root / "snapshot-one")
            (root / "source-package.json").unlink()
            (root / "first-package.json").unlink()
            second = self.continuation(root, reference, name="second")
            with patch("openweb_news.fixtures.search", side_effect=AssertionError("must reuse")), patch("openweb_news.fixtures.model", side_effect=AssertionError("must reuse")):
                result = run(second)
            self.assertEqual(result["scores"], original["scores"])
            self.assertEqual(result["execution"]["retained_started_attempts"], 28)
            self.assertEqual(result["execution"]["reuse"]["started_attempts"], 28)

    def test_changed_frozen_work_is_rejected_before_dispatch(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            path, _, _ = self.source(root)
            reference = reuse.freeze(path, root / "snapshot")
            for change in ("query", "reviewer", "provider", "mode"):
                with self.subTest(change=change):
                    config = fixtures.config(root / "continued")
                    config["reuse_snapshot"] = reference
                    if change == "query":
                        config["questions"][0]["provider_query"] += " changed"
                    elif change == "reviewer":
                        config["reviewers"]["codex"]["model"] = "another-model"
                    elif change == "provider":
                        config["provider_overrides"] = {"brave-news": dict(secret_ref="OTHER_KEY")}
                        accounts = config["execution_policy"]["scheduling"]["accounts"]
                        accounts["provider/OTHER_KEY"] = accounts.pop("provider/OPENWEB_NEWS_BRAVE_API_KEY")
                    with self.assertRaises(ValueError):
                        prepare(config, mode="live" if change == "mode" else "fixture")
            with patch("openweb_news.preparation.code_bindings", return_value={"changed.py": "changed"}):
                with self.assertRaises(ValueError):
                    self.continuation(root, reference)

    def test_tampered_snapshot_and_escaping_paths_fail_before_any_calls(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            path, _, _ = self.source(root)
            reference = reuse.freeze(path, root / "snapshot")
            package = self.continuation(root, reference)
            manifest = load(reference["manifest_path"])
            artifact = next(item for item in manifest["files"] if item["path"].endswith("result.json"))
            (root / "snapshot" / artifact["path"]).write_bytes(b"{}")
            with patch("openweb_news.fixtures.search", side_effect=AssertionError("no call")), patch("openweb_news.fixtures.model", side_effect=AssertionError("no call")):
                with self.assertRaises(ValueError):
                    run(package)
            linked = root / "snapshot" / artifact["path"]
            linked.unlink()
            linked.symlink_to(root / "source-run" / artifact["path"])
            with self.assertRaises(ValueError):
                reuse.read_snapshot(reference)
            manifest["files"][0]["path"] = "../outside"
            write_private(Path(reference["manifest_path"]), manifest, replace=True)
            changed = dict(reference, sha256=digest(encoded(manifest)))
            with self.assertRaises(ValueError):
                reuse.read_snapshot(changed)

    def test_source_result_corruption_and_active_run_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            path, source, _ = self.source(root)
            binding = dict(package_sha256=digest(source), mode="fixture")
            with Ledger(root / "source-run", binding).locked():
                with self.assertRaises(BlockingIOError):
                    reuse.freeze(path, root / "active-snapshot")
            result = next((root / "source-run/attempts").glob("*/result.json"))
            result.write_bytes(b"{}")
            with self.assertRaises(ValueError):
                reuse.freeze(path, root / "corrupt-snapshot")

    def test_uncertain_source_attempt_remains_held_without_resend(self):
        class Crash(BaseException):
            pass
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            source = prepare(fixtures.config(root / "source-run"), mode="fixture")
            path = root / "source-package.json"
            write_private(path, source)
            search = source["search_requests"][0]
            provider = next(p for p in source["providers"] if p["configuration_id"] == search["configuration_id"])
            task_id = "search-" + digest(dict(configuration=search["configuration_id"], question=search["question_id"]))
            request = dict(search, kind="search", auth_reference=provider["auth"], reservation_usd="0",
                           reservation_basis="conservative estimate; not invoice or guaranteed upper bound")
            with Ledger(root / "source-run", dict(package_sha256=digest(source), mode="fixture")).locked() as ledger:
                with self.assertRaises(Crash):
                    ledger.execute(task_id, request, lambda _: (_ for _ in ()).throw(Crash()))
            reference = reuse.freeze(path, root / "snapshot")
            package = self.continuation(root, reference)
            original = fixtures.search
            def send(request, *args):
                self.assertNotEqual((request["configuration_id"], request["question_id"]), (search["configuration_id"], search["question_id"]))
                return original(request, *args)
            with patch("openweb_news.fixtures.search", side_effect=send):
                result = run(package)
            self.assertEqual(result["execution"]["state"], "completed_with_holds")
            self.assertEqual(result["execution"]["reuse"]["uncertain_attempts"], 1)
            self.assertTrue(any(row["task_id"] == task_id for row in result["execution"]["held_tasks"]))

    def test_interrupted_materialization_resumes_without_duplicate_dispatch(self):
        for boundary in ("result.json", "reuse.json"):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as temp:
                self.interrupted_import(Path(temp).resolve(), boundary)

    def interrupted_import(self, root, boundary):
        path, _, original = self.source(root)
        package = self.continuation(root, reuse.freeze(path, root / "snapshot"))
        real_write = reuse.write_private
        failed = []
        def interrupted(path, *args, **kwargs):
            if path.name == boundary and not failed:
                failed.append(True)
                raise OSError("simulated import interruption")
            return real_write(path, *args, **kwargs)
        with patch("openweb_news.reuse.write_private", side_effect=interrupted):
            with self.assertRaises(OSError):
                run(package)
        with patch("openweb_news.fixtures.search", side_effect=AssertionError("must reuse")), patch("openweb_news.fixtures.model", side_effect=AssertionError("must reuse")):
            result = run(package)
        self.assertEqual(result["scores"], original["scores"])
        self.assertEqual(result["execution"]["retained_started_attempts"], 28)

    def test_finished_uncertain_delivery_is_held_even_with_retry_flag(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            original_model = fixtures.model
            failed = []
            def uncertain(role, compiled, folder):
                if role == "codex" and not failed:
                    failed.append(compiled["packet"]["case_id"])
                    return dict(transport_success=False, complete=False, dispatch_state="uncertain")
                return original_model(role, compiled, folder)
            with patch("openweb_news.fixtures.model", side_effect=uncertain):
                path, _, _ = self.source(root)
            package = self.continuation(root, reuse.freeze(path, root / "snapshot"))
            with patch("openweb_news.fixtures.search", side_effect=AssertionError("no send")), patch("openweb_news.fixtures.model", side_effect=AssertionError("no send")):
                report = run(package, retry_failed=True)
            self.assertEqual(report["execution"]["reuse"]["uncertain_attempts"], 1)
            self.assertEqual(report["execution"].get("scheduled_tasks", 0), 0)
            self.assertTrue(any(item["task_id"] == "codex-" + failed[0] for item in report["execution"]["held_tasks"]))

    def test_prepare_cli_freezes_private_bundle_without_calls(self):
        from openweb_news.cli import main
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            source_path, _, _ = self.source(root)
            config = fixtures.config(root / "continued")
            write_private(root / "config.json", config)
            with patch("sys.stdout", new=io.StringIO()), patch("subprocess.run", side_effect=AssertionError("no CLI inference")):
                status = main(["prepare", "--mode", "fixture", "--config", str(root / "config.json"), "--reuse-from", str(source_path), "--output", str(root / "next.json")])
            self.assertEqual(status, 0)
            package = load(root / "next.json")
            reference = package["configuration"]["reuse_snapshot"]
            self.assertEqual(Path(reference["manifest_path"]), root / ".reuse/next/manifest.json")
            self.assertEqual(reuse.read_snapshot(reference, package)["statistics"]["started_attempts"], 28)

    def test_carried_retry_and_spending_bounds_cannot_reset(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            config = fixtures.config(root / "source-run")
            config["execution_policy"]["spending_ceiling_usd"] = "5"
            config["execution_policy"]["max_attempts_per_task"] = 2
            config["execution_policy"]["retry_policy"].update(max_total=1, max_search=1, http_statuses=[503])
            with patch("openweb_news.runner.reservation", return_value="0.1"):
                path, source, _ = self.source(root, config=config)
                run(source, retry_failed=True)
            reference = reuse.freeze(path, root / "snapshot")
            cfg = deepcopy(config)
            cfg["run_directory"] = str(root / "continued")
            cfg["reuse_snapshot"] = reference
            cfg["execution_policy"]["spending_ceiling_usd"] = "2"
            with self.assertRaises(ValueError):
                prepare(cfg, mode="fixture")
            cfg["execution_policy"]["spending_ceiling_usd"] = "6"
            cfg["execution_policy"]["retry_policy"]["max_search"] = 0
            with self.assertRaises(ValueError):
                prepare(cfg, mode="fixture")
            cfg["execution_policy"]["retry_policy"]["max_search"] = 1
            package = prepare(cfg, mode="fixture")
            with patch("openweb_news.runner.reservation", return_value="0.1"), patch("openweb_news.fixtures.search", side_effect=AssertionError("no retry allowance reset")), patch("openweb_news.fixtures.model", side_effect=AssertionError("no model redispatch")):
                result = run(package, retry_failed=True)
            self.assertEqual(result["execution"]["reuse"]["retry_counts"], {"search": 1, "model": 0})
            self.assertEqual(result["execution"]["retained_started_attempts"], 29)

    def test_single_offline_repair_allowance_does_not_reset(self):
        from openweb_news.repair import redecode
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            path, source, _ = self.source(root)
            redecode(source, root / "original-repair")
            package = self.continuation(root, reuse.freeze(path, root / "snapshot"))
            report = run(package)
            self.assertTrue(report["execution"]["reuse"]["offline_repair_already_claimed"])
            with self.assertRaises(ValueError):
                redecode(package, root / "another-repair")


if __name__ == "__main__":
    unittest.main()
