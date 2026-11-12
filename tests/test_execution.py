from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from openweb_news import backends, fixtures, judging
from openweb_news.common import digest, encoded, load
from openweb_news.preparation import prepare, require_approval, verify_package
from openweb_news.review import classify, settle
from openweb_news.runner import run
from openweb_news.repair import redecode
from openweb_news.audit import sample
from openweb_news.transport import Credentials, dotenv, https
from openweb_news._method import providers
from .helpers import native_response, question, verdict


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.config = next(c for c in providers.configuration_catalog() if c["configuration_id"] == "valyu-search")
        q = question()
        q["contract"]["temporal_requirements"] = [dict(id="window", basis="publication",
            start_inclusive="2026-10-01T00:00:00Z", end_exclusive="2026-11-01T00:00:00Z")]
        response = native_response(self.config, text="Editorial report. Published **October** 12, 2026: the fictional Aster observatory opened.")
        response["results"][0]["publication_date"] = "2026-10-12"
        projected = providers.project_response(self.config, response, "2026-10-28T00:00:00Z")
        self.packet = judging.packet_for(q, self.config, projected["positions"][0], "policy-case")
        self.compiled = judging.compile_packet(self.packet, "2026-10-28T00:00:00Z", config=self.config)
        self.raw = verdict(self.compiled)
        self.raw["dates"] = [dict(candidate_id="finding-1", predicate_id="publication:window",
            link_status="not_demonstrated", role="first_publication", date_ids=["N1"],
            anchor_date_id="", additional_dates=[], context_support=[], reason="Base does not upgrade native unspecified publication.")]

    def test_no_free_text_override_or_silent_base_substitution(self):
        value = judging.decode_position(self.raw, self.compiled)
        self.assertEqual(value["state"], "valid", value)
        self.assertEqual(value["structural_base_grade"], 1)
        self.assertEqual(value["proposal_grade"], 2)
        self.assertEqual(value["policy_conformance"], "requires_own_source_review")
        self.assertEqual(classify(value), "nonconforming_numeric")
        self.raw["reason"] = "A temporal policy permits the proposed answer."
        self.assertEqual(classify(judging.decode_position(self.raw, self.compiled)), "nonconforming_numeric")

    def test_source_native_schema_has_no_invented_sidecar(self):
        self.assertNotIn("policy_applications", self.compiled["schema"]["properties"])
        self.assertNotIn("policy_applications", self.compiled["schema"]["required"])
        unexpected = dict(self.raw, policy_applications=[])
        self.assertEqual(judging.decode_position(unexpected, self.compiled)["state"], "invalid")
        self.assertNotIn("All roles return policy_applications", self.compiled["prompt"])
        self.assertIn("Current policy rule owners:", self.compiled["prompt"])
        self.assertIn("Company-country policy:", self.compiled["prompt"])
        self.assertIn("disagreement requires own-source review", self.compiled["prompt"])

    def test_policy_agreement_without_conformance_is_not_a_vote(self):
        decoded = judging.decode_position(self.raw, self.compiled)
        pos = dict(decoded, independent=True, prior_verdict_exposure=False)
        self.assertIsNone(settle(pos, pos)["grade"])

    def test_source_native_date_display_and_applicability_provenance(self):
        row = self.compiled["input"]["native_date_display"][0]
        self.assertEqual(row["declared_meaning"], "publication_unspecified")
        self.assertEqual(row["observation"], self.packet["date_observations"][0])
        binding = self.compiled["input"]["publication_estimate_applicability"]
        for key in ("configuration_sha256", "field_path", "mapping_id", "reviewer", "reason", "source_bindings"):
            self.assertTrue(binding[key])

    def test_source_url_lead_only_support_boundary(self):
        lead = deepcopy(self.raw)
        lead.update(overall_grade=1)
        lead["candidates"][0].update(answer_supported=False, clear_lead=True,
            support=[dict(field="url", quote=self.packet["returned_url"], occurrence=0)])
        self.assertEqual(classify(judging.decode_position(lead, self.compiled)), "conforming_numeric")
        answer = deepcopy(lead)
        answer["candidates"][0]["answer_supported"] = True
        self.assertEqual(judging.decode_position(answer, self.compiled)["state"], "invalid")

    def test_recovered_full_field_markdown_replay_and_schema_fields(self):
        data = self.compiled["input"]["text_dates"]
        self.assertEqual(judging.replay_mentions(self.packet, data), data)
        changed = deepcopy(data)
        changed[0]["display_text"] = "invented date"
        with self.assertRaises(ValueError):
            judging.replay_mentions(self.packet, changed)
        links = self.compiled["schema"]["properties"]["dates"]["items"]["properties"]
        self.assertNotIn("url", links["context_support"]["items"]["properties"]["field"]["enum"])
        self.assertNotIn("N1", links["anchor_date_id"]["enum"])


class PackageTests(unittest.TestCase):
    def test_invalid_spending_rates_and_success_status_retries_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg = fixtures.config(Path(temp) / "run")
            cfg["execution_policy"]["spending_ceiling_usd"] = "NaN"
            with self.assertRaises(ValueError):
                prepare(cfg)
            cfg = fixtures.config(Path(temp) / "run")
            cfg["execution_policy"]["retry_policy"]["http_statuses"] = [200]
            with self.assertRaises(ValueError):
                prepare(cfg)
            cfg["execution_policy"]["retry_policy"]["http_statuses"] = [503]
            cfg["reviewers"]["codex"] = dict(backend="openai-api", model="synthetic", secret_ref="OPENWEB_NEWS_OPENAI_API_KEY",
                reasoning_effort="high", max_output_tokens=1000, timeout_seconds=60,
                price_input_per_million_usd="-1", price_output_per_million_usd="10")
            with self.assertRaises(ValueError):
                prepare(cfg)
    def test_exact_approval_and_changed_bindings(self):
        with tempfile.TemporaryDirectory() as temp:
            package = prepare(fixtures.config(Path(temp) / "run"))
            with self.assertRaises(ValueError):
                require_approval(package, {})
            approval = dict(package_sha256=digest(package), decision="approved", approved_by="test owner", approved_at="2026-10-28T00:00:00Z")
            self.assertTrue(require_approval(package, approval))
            changed = deepcopy(package)
            changed["questions"][0]["provider_query"] += " changed"
            with self.assertRaises(ValueError):
                require_approval(changed, approval)

    def test_live_run_refuses_before_loading_credentials_or_transport(self):
        with tempfile.TemporaryDirectory() as temp:
            package = prepare(fixtures.config(Path(temp) / "run"))
            with patch("openweb_news.runner.Credentials", side_effect=AssertionError("credentials must not be loaded")):
                with self.assertRaises(ValueError):
                    run(package, approval={})

    def test_example_offline_resume_does_not_dispatch_again(self):
        with tempfile.TemporaryDirectory() as temp:
            package = prepare(fixtures.config(Path(temp) / "run"), mode="fixture")
            with patch("openweb_news.runner.https", side_effect=AssertionError("network forbidden")), patch("subprocess.run", side_effect=AssertionError("process forbidden")):
                first = run(package)
                with patch("openweb_news.fixtures.search", side_effect=AssertionError("completed search must not repeat")), patch("openweb_news.fixtures.model", side_effect=AssertionError("completed model must not repeat")):
                    second = run(package)
            self.assertEqual(first["scores"], second["scores"])
            self.assertEqual(first["outcome_counts"], {"2": 4, "1": 2, "unknown": 2})

    def test_single_saved_response_repair_has_no_calls_and_preserves_scores(self):
        with tempfile.TemporaryDirectory() as temp:
            package = prepare(fixtures.config(Path(temp) / "run"), mode="fixture")
            first = run(package)
            with patch("openweb_news.runner.https", side_effect=AssertionError("no calls")), patch("subprocess.run", side_effect=AssertionError("no reviewer")):
                repaired = redecode(package, Path(temp) / "repair")
            self.assertEqual(repaired["scores"], first["scores"])
            self.assertEqual(repaired["repair"]["new_live_calls"], 0)
            with self.assertRaises(ValueError):
                redecode(package, Path(temp) / "second-repair")

    def test_operational_sample(self):
        with tempfile.TemporaryDirectory() as temp:
            package = prepare(fixtures.config(Path(temp) / "run"), mode="fixture")
            run(package)
            receipt = sample(Path(temp) / "run", Path(temp) / "audit")
            self.assertEqual(receipt["selected_count"], 8)
            self.assertGreater(receipt["failure_records"], 0)


class BackendTests(unittest.TestCase):
    def test_subscription_environment_omits_api_credentials_and_overrides(self):
        parent = dict(HOME="/synthetic/home", PATH="/bin",
                      OPENAI_BASE_URL="https://unwanted.example", CLAUDE_CODE_USE_BEDROCK="1", CODEX_HOME="/synthetic/auth")
        parent["OPENAI_API_KEY"] = "secret"
        parent["ANTHROPIC_API_KEY"] = "secret"
        for kind in ("claude", "codex"):
            env = backends.reviewer_environment(kind, parent=parent)
            self.assertNotIn("OPENAI_API_KEY", env)
            self.assertNotIn("ANTHROPIC_API_KEY", env)
            self.assertNotIn("OPENAI_BASE_URL", env)
            self.assertNotIn("CLAUDE_CODE_USE_BEDROCK", env)
            if kind == "claude":
                self.assertEqual(env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"], "1")
                self.assertEqual(env["DISABLE_TELEMETRY"], "1")
                self.assertEqual(env["DISABLE_ERROR_REPORTING"], "1")

    def test_codex_cli_receives_only_packet_and_explicit_isolation(self):
        calls = []
        def fake_run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            if "--help" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout="--ignore-user-config --ignore-rules --ephemeral --output-schema", stderr="")
            if "status" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout="Logged in using ChatGPT", stderr="")
            self.assertNotIn("OPENAI_API_KEY", kwargs["env"])
            self.assertIn("--ignore-user-config", cmd)
            self.assertIn('forced_login_method="chatgpt"', cmd)
            self.assertIn("features.shell_tool=false", cmd)
            Path(cmd[cmd.index("--output-last-message") + 1]).write_text('{"fixture":true}')
            return subprocess.CompletedProcess(cmd, 0, stdout='{"type":"turn.completed","usage":{"input_tokens":10}}\n', stderr="")
        with tempfile.TemporaryDirectory() as temp:
            auth = Path(temp) / ".codex"
            auth.mkdir()
            (auth / "auth.json").write_text('{"synthetic":"test-only"}')
            value = backends.subscription(dict(backend="codex-subscription", executable=sys.executable,
                model="synthetic-model", reasoning_effort="high", timeout_seconds=60),
                dict(prompt="Only this packet", input={"own": "evidence"}, schema={"type": "object"}),
                Path(temp), parent_env=dict(PATH="/bin", HOME=temp, OPENAI_API_KEY="forbidden"), run=fake_run)
        self.assertTrue(value["transport_success"], value)
        self.assertEqual(value["auth_observation"], "chatgpt")
        self.assertEqual(json.loads(calls[-1][1]["input"]), {"own": "evidence"})

    def test_codex_overlay_omits_instructions_and_preserves_native_auth_refresh(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            auth = root / "auth"
            auth.mkdir()
            original = auth / "auth.json"
            original.write_text('{"synthetic":"original"}')
            (auth / "AGENTS.md").write_text("must never enter reviewer context")
            with backends.isolated_codex_environment({"HOME": temp, "CODEX_HOME": str(auth)}, root / "work") as env:
                overlay = Path(env["CODEX_HOME"])
                self.assertFalse((overlay / "AGENTS.md").exists())
                self.assertTrue((overlay / "auth.json").is_symlink())
                refreshed = overlay / "refreshed.json"
                refreshed.write_text('{"synthetic":"native-refresh"}')
                refreshed.replace(overlay / "auth.json")
            self.assertEqual(load(original), {"synthetic": "native-refresh"})

    def test_stale_auth_overlay_blocks_before_another_reviewer(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            original = root / "auth.json"
            original.write_text('{"synthetic":"test"}')
            (root / ".news-reviewer-auth-stale").mkdir()
            with self.assertRaises(RuntimeError):
                with backends.isolated_codex_environment({"HOME": temp, "CODEX_HOME": temp}, root / "work"):
                    self.fail("stale auth must block")

    def test_claude_auth_and_tool_audit_fail_closed(self):
        def fake_run(cmd, **kwargs):
            if "--help" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout="--safe-mode --tools --strict-mcp-config --disable-slash-commands --no-session-persistence", stderr="")
            if "status" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout='{"authMethod":"claude.ai"}', stderr="")
            self.assertIn("--safe-mode", cmd)
            events = [dict(type="system", subtype="init", tools=["Read"]), dict(type="result", structured_output={"fixture": True})]
            return subprocess.CompletedProcess(cmd, 0, stdout="\n".join(json.dumps(e) for e in events), stderr="")
        with tempfile.TemporaryDirectory() as temp:
            value = backends.subscription(dict(backend="claude-subscription", executable=sys.executable,
                model="synthetic-model", reasoning_effort="high", timeout_seconds=60),
                dict(prompt="Only this packet", input={}, schema={"type": "object"}), Path(temp),
                parent_env=dict(PATH="/bin", HOME=temp), run=fake_run)
        self.assertFalse(value["transport_success"])
        self.assertIn("unexpected context or tools", value["errors"][0])

    def test_env_file_is_literal_and_aliases_not_automatic(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "private.env"
            path.write_text('OPENWEB_NEWS_TEST="$(never-execute)"\n')
            self.assertEqual(dotenv(path)["OPENWEB_NEWS_TEST"], "$(never-execute)")

    def test_claude_privacy_flags_and_unknown_telemetry_plugin_fail_closed(self):
        for privacy,telemetry in [(True,False),(False,False),(True,True)]:
            with self.subTest(privacy=privacy,telemetry=telemetry),tempfile.TemporaryDirectory() as temp:
                def fake_run(cmd,**kw):
                    self.assertEqual(kw["env"]["DISABLE_TELEMETRY"],"1")
                    if "--help" in cmd:
                        return subprocess.CompletedProcess(cmd,0,stdout="--safe-mode --tools --strict-mcp-config --disable-slash-commands --no-session-persistence",stderr="")
                    if "status" in cmd:
                        return subprocess.CompletedProcess(cmd,0,stdout='{"authMethod":"claude.ai"}',stderr="")
                    plugins=[dict(name="cc-plugin-telemetry",path="builtin",source="cc-plugin-telemetry@builtin")] if telemetry else []
                    events=[dict(type="system",subtype="init",model="synthetic-model",tools=["StructuredOutput"],plugins=plugins,mcp_servers=[],skills=[],analytics_disabled=privacy,product_feedback_disabled=privacy),dict(type="result",structured_output={"fixture":True})]
                    return subprocess.CompletedProcess(cmd,0,stdout="\n".join(json.dumps(e) for e in events),stderr="")
                result=backends.subscription(dict(backend="claude-subscription",executable=sys.executable,model="synthetic-model",reasoning_effort="high",timeout_seconds=60),dict(prompt="Only own packet",input={},schema={"type":"object"}),Path(temp),parent_env=dict(HOME=temp,PATH="/bin",DISABLE_TELEMETRY="0"),run=fake_run)
                self.assertEqual(result["transport_success"],privacy and not telemetry)
                if not result["transport_success"]:self.assertTrue(result["fatal_configuration"])
            with self.assertRaises(ValueError):
                Credentials(environment={"BRAVE_API_KEY": "other-account"}).get("OPENWEB_NEWS_BRAVE_API_KEY")


if __name__ == "__main__":
    unittest.main()
