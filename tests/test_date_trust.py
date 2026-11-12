from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from openweb_news import date_trust
from openweb_news._method.providers import configuration_catalog
from openweb_news.common import digest, load, write_private


class DateTrustTests(unittest.TestCase):
    def setUp(self):
        catalog = {p["configuration_id"]: p for p in configuration_catalog(include_optional=True)}
        self.providers = [catalog[cid] for cid in ("keenable-pro", "seltz-web-pro", "tavily-news")]
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        source = self.root / "source.txt"
        source.write_text("Synthetic source-bound publication rule.\n")
        diagnostic = self.root / "diagnostic.json"
        diagnostic.write_text('{"synthetic":true,"selected_observations":[],"not_accuracy":true}\n')
        self.package = date_trust.prepare(self.providers, self.root / "review", diagnostic_paths=[diagnostic], source_bindings={"synthetic-source": source})
        self.path = self.root / "review/package.json"

    def approve(self, selections=None):
        approval = load(self.path.parent / "approval-template.json")
        approval.update(approved=True, approved_by="synthetic owner", approved_at="2026-11-05T00:00:00Z")
        if selections is None:
            selections = {
                "keenable-pro": dict(eligible_fields=["published_at"], reason="Synthetic exact-field choice"),
                "seltz-web-pro": dict(eligible_fields=["published_date"], reason="Synthetic top-level choice"),
                "tavily-news": dict(eligible_fields=[], reason="Synthetic explicit no admission"),
            }
        approval["selections"] = selections
        write_private(self.path.parent / "approval.json", approval)
        return approval

    def test_preparation_never_fabricates_owner_selection(self):
        template = load(self.path.parent / "approval-template.json")
        self.assertFalse(template["approved"])
        self.assertEqual(template["selections"], {p["configuration_id"]: None for p in self.providers})
        self.assertIn("possibly dependent", " ".join(self.package["limitations"]))
        self.assertFalse((self.path.parent / "approval.json").exists())
        for p in self.providers:
            self.assertIsNone(date_trust.eligible_fields(p))

    def test_exact_owner_admission_and_explicit_empty_are_distinct_from_legacy(self):
        self.approve()
        receipt = date_trust.read(self.path, self.providers)
        selected = date_trust.apply(self.providers, receipt)
        self.assertEqual([date_trust.eligible_fields(p) for p in selected], [["published_at"], ["published_date"], []])
        self.assertNotIn("date_trust", self.providers[0])
        self.assertEqual(selected[0]["date_mapping"], self.providers[0]["date_mapping"])
        self.assertEqual(selected[0]["date_trust"]["receipt_sha256"], receipt["receipt_sha256"])

    def test_native_meaning_is_available_without_trust_and_alias_not_inferred(self):
        keenable, seltz, _ = self.providers
        self.assertEqual(date_trust.field_semantics(keenable, "published_at"), ("publication_unspecified", "unspecified"))
        self.assertEqual(date_trust.field_semantics(keenable, "acquired_at"), ("index", "provider_reported"))
        self.assertEqual(date_trust.field_semantics(seltz, "published_date"), ("publication_unspecified", "unspecified"))
        self.assertIsNone(date_trust.field_semantics(seltz, "properties.published_date"))
        self.assertIsNone(date_trust.eligible_fields(seltz))

    def test_undecided_or_missing_provider_cannot_be_judged_under_new_method(self):
        for choices in ({p["configuration_id"]: None for p in self.providers}, {"keenable-pro": dict(eligible_fields=[], reason="Explicit no")}):
            with self.subTest(choices=choices):
                self.approve(choices)
                with self.assertRaises(ValueError):
                    date_trust.read(self.path, self.providers)
                (self.path.parent / "approval.json").unlink()

    def test_index_update_and_unproven_seltz_alias_cannot_be_admitted(self):
        for cid, field in (("keenable-pro", "acquired_at"), ("seltz-web-pro", "properties.published_date"), ("tavily-news", "published_date")):
            choices = {p["configuration_id"]: dict(eligible_fields=[], reason="Synthetic no admission") for p in self.providers}
            choices[cid]["eligible_fields"] = [field]
            self.approve(choices)
            with self.assertRaisesRegex(ValueError, "exact reviewed publication meaning"):
                date_trust.read(self.path, self.providers)
            (self.path.parent / "approval.json").unlink()

    def test_owner_and_evidence_tampering_rejected(self):
        self.approve()
        diagnostic = Path(self.package["diagnostics"][0]["pinned_path"])
        diagnostic.write_text("changed synthetic evidence")
        with self.assertRaisesRegex(ValueError, "bytes differ"):
            date_trust.read(self.path, self.providers)

    def test_registry_change_rejects_owner_receipt(self):
        self.approve()
        altered = deepcopy(date_trust._FIELDS)
        altered["keenable-pro"]["published_at"] = ("first_publication", "documented_estimated", True)
        with patch.object(date_trust, "_FIELDS", altered):
            with self.assertRaisesRegex(ValueError, "registry/configuration differs"):
                date_trust.read(self.path, self.providers)

    def test_only_private_extension_keys_may_differ(self):
        self.approve()
        receipt = date_trust.read(self.path, self.providers)
        selected = date_trust.apply(self.providers, receipt)
        selected[0]["judge_input_profile"] = "synthetic-selected-profile"
        self.assertEqual(date_trust.eligible_fields(selected[0]), ["published_at"])
        changed = deepcopy(self.providers)
        changed[0]["base_body"]["max_results"] = 11
        with self.assertRaises(ValueError):
            date_trust.read(self.path, changed)
        selected[0]["price_usd_per_call"] = "synthetic changed field"
        with self.assertRaises(ValueError):
            date_trust.eligible_fields(selected[0])

    def test_receipt_cannot_be_forged_or_reused_for_a_subset(self):
        self.approve()
        receipt = date_trust.read(self.path, self.providers)
        changed = deepcopy(receipt)
        changed["selections"]["keenable-pro"]["eligible_fields"] = []
        with self.assertRaises(ValueError):
            date_trust.apply(self.providers, changed)
        with self.assertRaises(ValueError):
            date_trust.apply(self.providers[:1], receipt)

    def test_pins_preserve_reviewed_source_when_original_file_changes(self):
        self.approve()
        Path(self.package["sources"][0]["source_path"]).write_text("Later source revision")
        receipt = date_trust.read(self.path, self.providers)
        self.assertEqual(receipt["package_sha256"], digest(self.package))

    def test_brave_article_publication_is_exact_candidate_without_automatic_admission(self):
        from openweb_news import blind_input, judging, publication
        from openweb_news._method import evidence
        from openweb_news._method.providers import project_response
        from .helpers import question
        brave = next(p for p in configuration_catalog(include_optional=True) if p["configuration_id"] == "brave-web")
        fields = date_trust.registry([brave])["providers"][0]["fields"]
        self.assertTrue(fields["article.date"]["admission_candidate"])
        self.assertFalse(fields["page_age"]["admission_candidate"])
        self.assertEqual(date_trust.field_semantics(brave, "article.date"), ("publication_unspecified", "unspecified"))
        self.assertEqual(judging.native_semantics(brave, "article.date"), ["unclear", "unspecified"])
        for index, selected_fields in enumerate(([], ["article.date"], ["page_age"])):
            with self.subTest(selected_fields=selected_fields):
                root = self.root / ("synthetic-brave-" + str(index))
                package = date_trust.prepare([brave], root, diagnostic_paths=[self.root / "diagnostic.json"],
                    source_bindings={"synthetic-source": self.root / "source.txt"})
                approval = load(root / "approval-template.json")
                self.assertIsNone(approval["selections"]["brave-web"])
                approval.update(approved=True, approved_by="synthetic test owner", approved_at="fixture",
                    selections={"brave-web": dict(eligible_fields=selected_fields, reason="Synthetic exact-field test only")})
                write_private(root / "approval.json", approval)
                if selected_fields == ["page_age"]:
                    with self.assertRaises(ValueError):
                        date_trust.read(root / "package.json", [brave])
                    continue
                receipt = date_trust.read(root / "package.json", [brave])
                selected = date_trust.apply([brave], receipt)[0]
                selected["judge_input_profile"] = blind_input.LIVE_PROFILE
                self.assertEqual(judging.native_policy_fields(selected), selected_fields)
                self.assertEqual(tuple(judging.native_semantics(selected, "article.date")), ("publication_unspecified", "unspecified"))
                self.assertEqual(judging.native_semantics(selected, "page_age"), ("unclear", "unspecified"))
                q = question("synthetic-brave-publication")
                window = dict(id="publication-window", basis="publication", start_inclusive="2026-01-01T00:00:00Z", end_exclusive="2026-01-02T00:00:00Z")
                q["contract"]["temporal_requirements"] = [window]
                raw = dict(web=dict(results=[dict(title="Synthetic article", description="An editorial report about Aster.",
                    url="https://synthetic.example/aster", article=dict(date="2026-01-01"), page_age="2026-02-01")]))
                position = project_response(selected, raw, "2026-11-05T00:00:00Z")["positions"][0]
                packet = judging.packet_for(q, selected, position, "synthetic-brave-date")
                compiled = judging.compile_packet(packet, "2026-11-05T00:00:00Z", config=selected)
                review = publication.packet_for(compiled, _source=True)
                eligible = [r["date_id"] for r in compiled["input"]["native_date_ids"] if r["relative_path"] == "article.date"] if selected_fields else []
                self.assertEqual(review["input"]["eligible_native_date_ids"], eligible)
                page_age = next(r for r in compiled["input"]["native_date_ids"] if r["relative_path"] == "page_age")
                self.assertNotIn(page_age["date_id"], review["input"]["eligible_native_date_ids"])
                self.assertEqual(evidence.publication_status(packet["date_observations"], window, True), "not_demonstrated")


if __name__ == "__main__":
    unittest.main()
