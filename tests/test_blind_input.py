"""Synthetic proof for inactive projection groundwork, not live route integration."""
from copy import deepcopy
import json
import unittest

from openweb_news import adjudication, blind_input, judging, publication
from openweb_news._method import providers
from openweb_news._method import two_step, text_dates
from openweb_news.common import digest
from .helpers import question, verdict


CAPTURED = "2026-11-01T00:00:00Z"
TEXT = "Editorial report. Published October 12, 2026: Aster raised $100 at 25%; A&B **Exa**."
CATALOG_IDS = {
    "brave-news", "brave-web", "bright-data-serp", "exa-highlights",
    "firecrawl-news", "firecrawl-web", "linkup-fast", "oxylabs-search",
    "parallel-advanced", "perplexity-web", "searlo-news", "searlo-web",
    "tavily-news", "tavily-general", "valyu-search", "you-com-web", "keenable-pro",
    "serper-web", "scavio-web", "seltz-web-pro",
}


def put(row, path, value):
    parts = path.split(".")
    for part in parts[:-1]:
        row = row.setdefault(part, {})
    row[parts[-1]] = value


def compiled_for(config, *, index=0, noise=901234, text=TEXT, date="2026-10-12",
                 publication_window=True, identity="first", source_change=None):
    """Exercise real native containers/dotted fields, including Seltz properties."""
    fields = config["result_fields"]
    row = {}
    put(row, fields["url"], "https://news.example/aster")
    put(row, fields["title"], text)
    shape = fields["content_shape"]
    content = ([dict(text=text), dict(text="Second exact snippet.")]
               if shape == "object_text_array" else [text, "Second exact snippet."]
               if shape == "string_array" else text)
    put(row, fields["content"], content)
    for name, additional_shape in fields.get("additional_content", {}).items():
        put(row, name, [text] if additional_shape == "string_array" else text)
    for name in fields["metadata"]:
        if name in {"global_rank", "score", "position", "rank", "price"}:
            put(row, name, noise)
        elif name == "source":
            put(row, name, "Synthetic editorial publisher")
        elif name == "type":
            put(row, name, "editorial")
        elif name == "meta_url":
            put(row, name, dict(scheme="https", hostname="news.example", netloc="www.news.example",
                                path="› Topics › Aster", favicon="https://icons.example/synthetic.ico"))
        elif name in {"news", "properties"}:
            continue  # Optional news absent; Seltz properties.title already set.
        else:
            put(row, name, date)
    if config["configuration_id"] == "seltz-web-pro":
        # Observed production shape: title is not supplied in properties; date
        # leaves already become separate native observations in the adapter.
        row["properties"] = dict(fetched_at="2026-10-29T12:00:00Z", published_date=date)
    if config["configuration_id"] == "brave-web":
        row["article"] = dict(date=date)
    row["own_date"] = date  # A real recognized date field for every adapter.
    if source_change:
        source_change(row)
    raw = [dict(row) for _ in range(index + 1)]
    for part in reversed(fields["container"].split(".")):
        raw = {part: raw}
    projected = providers.project_response(config, raw, CAPTURED)
    position = projected["positions"][index]
    q = question("synthetic-" + identity)
    q["revision"] = 1 if identity == "first" else 2
    if publication_window:
        q["contract"]["temporal_requirements"] = [dict(
            id="window", basis="publication", start_inclusive="2026-10-01T00:00:00Z",
            end_exclusive="2026-11-01T00:00:00Z")]
    case = "occurrence-" + digest(dict(question=q, configuration=config, position=position))
    packet = judging.packet_for(q, config, position, case)
    return judging.compile_packet(packet, CAPTURED, config=config)


def packets(compiled):
    result = dict(ordinary=judging.blind_packet(compiled),
                  publication=publication.packet_for(compiled))
    app = compiled["input"]["publication_estimate_applicability"]
    if app:
        ids = [r["date_id"] for r in compiled["input"]["native_date_ids"]
               if r["relative_path"] == app["field_path"]]
        result["adjudication"] = adjudication.packet_for(compiled, ids)
    return result


def projected(compiled, packet=None):
    return blind_input.project(packet or judging.blind_packet(compiled),
                               source_packet=compiled["packet"])


def model_verdict(value, projection):
    result = deepcopy(value)
    fields = {field: alias for alias, field in projection["binding"]["field_aliases"].items()}

    def visit(node):
        if isinstance(node, dict):
            if "field" in node and "quote" in node:
                node["field"] = fields[node["field"]]
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(result)
    if "case_id" in result:
        result["case_id"] = blind_input.MODEL_CASE
    return result


def restore(value, projection):
    return blind_input.restore_verdict(value, projection["binding"],
                                      expected_binding_sha256=projection["binding_sha256"])


class BlindInputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = providers.configuration_catalog(include_optional=True)
        cls.valyu = next(c for c in cls.catalog if c["configuration_id"] == "valyu-search")

    def test_all_catalog_adapter_paths_and_three_packet_builders(self):
        self.assertEqual({c["configuration_id"] for c in self.catalog}, CATALOG_IDS)
        counts = dict(ordinary=0, publication=0, adjudication=0)
        for config in self.catalog:
            compiled = compiled_for(config)
            before = deepcopy(compiled)
            for route, packet in packets(compiled).items():
                with self.subTest(configuration=config["configuration_id"], route=route):
                    original = deepcopy(packet)
                    view = projected(compiled, packet)
                    counts[route] += 1
                    model = view["packet"]
                    visible = json.dumps(model, sort_keys=True)
                    self.assertEqual(model["prompt"], packet["prompt"])
                    self.assertEqual(model["input"]["allowed_text"], compiled["packet"]["allowed_text"])
                    self.assertEqual(model["input"]["returned_url"], compiled["packet"]["returned_url"])
                    self.assertEqual(model["input"]["complete_contract"], compiled["packet"]["question"]["contract"])
                    self.assertEqual(model["input"]["case_id"], blind_input.MODEL_CASE)
                    self.assertNotIn(compiled["packet"]["case_id"], visible)
                    self.assertNotIn("901234", visible)
                    for key in ("configuration_sha256", "mapping_id", "field_path", "relative_path",
                                "source_path", "observation_mapping_id", "applicability_mapping_id"):
                        self.assertNotIn('"' + key + '"', visible)
                    for source_date, model_date in zip(compiled["input"]["native_date_ids"], model["input"]["native_date_ids"]):
                        for key in ("date_id", "raw_value", "state", "date_kind", "derivation", "precision",
                                    "timezone", "timezone_state", "interval", "calendar_comparison_interval"):
                            self.assertEqual(model_date[key], source_date[key])
                    self.assertEqual(model["input"]["text_dates"], compiled["input"]["text_dates"])
                    self.assertEqual(packet, original)
            self.assertEqual(compiled, before)
        self.assertEqual(counts, dict(ordinary=20, publication=20, adjudication=4))

    def test_rank_score_price_result_index_configuration_and_case_noise_are_invariant(self):
        for config in self.catalog:
            with self.subTest(configuration=config["configuration_id"]):
                left = compiled_for(config)
                changed_config = dict(config, synthetic_configuration_identity="different")
                right = compiled_for(changed_config, index=2, noise=887766, identity="second")
                self.assertNotEqual(left["packet_sha256"], right["packet_sha256"])
                for route, packet in packets(left).items():
                    a, b = projected(left, packet), projected(right, packets(right)[route])
                    self.assertEqual(a["packet"], b["packet"])
                    self.assertNotEqual(a["binding_sha256"], b["binding_sha256"])

    def test_semantic_text_dates_roles_source_facts_and_full_contract_survive(self):
        base = compiled_for(self.valyu)
        original = projected(base)["packet"]["input"]
        for changed in (compiled_for(self.valyu, text=TEXT + " Material fact."),
                        compiled_for(self.valyu, date="2026-10-17"),
                        compiled_for(self.valyu, source_change=lambda r: r.update(source="Another publisher"))):
            self.assertNotEqual(projected(changed)["packet"]["input"], original)
        packet = deepcopy(base["packet"])
        packet["date_observations"][0]["date_kind"] = "update"
        changed = judging.compile_packet(packet, CAPTURED, config=self.valyu)
        self.assertEqual(projected(changed)["packet"]["input"]["native_date_ids"][0]["date_kind"], "update")
        self.assertNotEqual(projected(changed)["packet"]["input"], original)
        for key, value in (("qualifying_predicates", ["A different exact requirement."]),
                           ("as_of_requirement", dict(meaning="historical_knowledge", cutoff="2026-10-20T00:00:00Z",
                                timezone="UTC", question_phrase="known by October 20", boundary="inclusive"))):
            packet = deepcopy(base["packet"])
            packet["question"]["contract"][key] = value
            # As-of conditions are owned by the source builder, not this helper.
            if key == "as_of_requirement":
                from openweb_news._method import evidence
                packet["as_of_interpretation"] = evidence.as_of_meaning(packet["question"])
                packet["required_conditions"] = evidence.required_conditions(packet["question"], packet["as_of_interpretation"])
            changed = judging.compile_packet(packet, CAPTURED, config=self.valyu)
            self.assertNotEqual(projected(changed)["packet"]["input"], original)
            self.assertEqual(projected(changed)["packet"]["input"]["complete_contract"][key], value)
        # Genuine brand names, currency, percent, punctuation and markup survive.
        self.assertIn(TEXT, [r["text"] for r in original["allowed_text"]])
        self.assertIn("Valyu", projected(base)["packet"]["prompt"])

    def test_question_history_is_private_and_unknown_question_fields_fail(self):
        compiled = compiled_for(self.valyu)
        expected = projected(compiled)["packet"]
        packet = deepcopy(compiled["packet"])
        packet["question"].update(family_id="private family", need_id="private need",
                                   provenance={"previous_grade": 2}, history=["old wording"])
        changed = judging.compile_packet(packet, CAPTURED, config=self.valyu)
        self.assertEqual(projected(changed)["packet"], expected)
        packet["question"]["unreviewed_meaning"] = "Do not silently omit a new semantic field."
        changed = judging.compile_packet(packet, CAPTURED, config=self.valyu)
        with self.assertRaisesRegex(ValueError, "unsupported-source-shape: question fields"):
            projected(changed)

    def test_relative_empty_null_dates_and_collection_only_context_are_retained(self):
        for value in ("two days ago", "", None, "2026-10", "2026-10-12T14:30:00+02:00"):
            with self.subTest(value=value):
                compiled = compiled_for(self.valyu, date=value)
                view = projected(compiled)["packet"]["input"]
                self.assertEqual(view["native_date_estimates"], compiled["input"]["native_date_estimates"])
                self.assertEqual(view["captured_at"], CAPTURED)
                self.assertEqual(view["captured_at_role"], "collection_provenance_only")
                for before, after in zip(compiled["input"]["native_date_ids"], view["native_date_ids"]):
                    self.assertEqual(before["raw_value"], after["raw_value"])
                    self.assertEqual(before["state"], after["state"])
                    self.assertEqual(before["date_kind"], after["date_kind"])

    def test_nested_objects_unknown_metadata_and_non_scalar_dates_fail_without_loss(self):
        for config_id, field, value in (
            ("searlo-news", "news", {"source": "Publisher", "publishedDate": "2026-10-12"}),
            ("brave-web", "meta_url", {"hostname": "news.example", "path": "/article"}),
            ("seltz-web-pro", "properties", {"title": TEXT, "publisher": "Publisher"}),
        ):
            config = next(c for c in self.catalog if c["configuration_id"] == config_id)
            compiled = compiled_for(config, source_change=lambda r: r.update({field: value}))
            before = deepcopy(compiled)
            with self.subTest(configuration=config_id), self.assertRaisesRegex(ValueError, "unsupported-source-shape: nested metadata"):
                projected(compiled)
            self.assertEqual(compiled, before)
        compiled = compiled_for(self.valyu)
        packet = deepcopy(compiled["packet"])
        packet["native_metadata"].append(dict(field="native:unreviewed", text='"important"', role="eligibility_only"))
        changed = judging.compile_packet(packet, CAPTURED, config=self.valyu)
        with self.assertRaisesRegex(ValueError, "unmapped metadata field"):
            projected(changed)
        changed = compiled_for(self.valyu, source_change=lambda r: r.update(own_date={"date": "2026-10-12", "score": 50}))
        with self.assertRaisesRegex(ValueError, "non-scalar native date"):
            projected(changed)

    def test_source_scalars_and_explicit_seltz_title_object_keep_exact_serialization(self):
        for config in self.catalog:
            compiled = compiled_for(config)
            view = projected(compiled)
            aliases = view["binding"]["field_aliases"]
            source = {r["field"]: r["text"] for r in compiled["packet"]["native_metadata"]}
            for row in view["packet"]["input"]["native_metadata"]:
                text = source[aliases[row["field"]]]
                span = view["binding"]["source_leaf_spans"].get(row["field"])
                self.assertEqual(row["text"], text[span["start"]:span["end"]] if span else text)
        seltz = next(c for c in self.catalog if c["configuration_id"] == "seltz-web-pro")
        view = projected(compiled_for(seltz, source_change=lambda r: r.update(properties={"title": TEXT})))
        properties = next(alias for alias, field in view["binding"]["field_aliases"].items()
                          if field == "native:properties")
        row = next(r for r in view["packet"]["input"]["native_metadata"] if r["field"] == properties)
        self.assertEqual(json.loads(row["text"]), {"title": TEXT})

    def test_seltz_duplicate_date_container_omission_retains_every_native_observation(self):
        config = next(c for c in self.catalog if c["configuration_id"] == "seltz-web-pro")
        for properties in (dict(fetched_at="2026-10-29T12:00:00Z", published_date="2026-10-12"),
                           dict(fetched_at="2026-10-29T12:00:00Z"),
                           dict(fetched_at=None, published_date=None)):
            compiled = compiled_for(config, source_change=lambda r: r.update(properties=properties))
            before = deepcopy(compiled)
            for route, packet in packets(compiled).items():
                with self.subTest(properties=properties, route=route):
                    view = projected(compiled, packet)
                    self.assertNotIn("native:properties", view["binding"]["field_aliases"].values())
                    dates = view["packet"]["input"]["native_date_ids"]
                    self.assertEqual(len(dates), len(compiled["input"]["native_date_ids"]))
                    for original, retained in zip(compiled["input"]["native_date_ids"], dates):
                        for key in ("date_id", "raw_value", "state", "date_kind", "derivation", "interval"):
                            self.assertEqual(retained[key], original[key])
                    for key, value in properties.items():
                        original = next(r for r in compiled["input"]["native_date_ids"]
                                        if r["relative_path"] == "properties." + key)
                        retained = next(r for r in dates if r["date_id"] == original["date_id"])
                        self.assertEqual(retained["raw_value"], value)
                        self.assertEqual(retained["date_kind"], "unclear")
            self.assertEqual(compiled, before)

    def test_seltz_duplicate_dates_require_each_exact_leaf_and_no_unknown_siblings(self):
        config = next(c for c in self.catalog if c["configuration_id"] == "seltz-web-pro")
        base = compiled_for(config)
        for field in ("fetched_at", "published_date"):
            for mode in ("mismatch", "missing"):
                packet = deepcopy(base["packet"])
                if mode == "mismatch":
                    row = next(r for r in packet["native_metadata"] if r["field"] == "native:properties")
                    value = json.loads(row["text"])
                    value[field] = "2000-01-01"
                    row["text"] = json.dumps(value, sort_keys=True)
                else:
                    packet["date_observations"] = [r for r in packet["date_observations"]
                                                    if r["relative_path"] != "properties." + field]
                changed = judging.compile_packet(packet, CAPTURED, config=config)
                before = deepcopy(changed)
                with self.subTest(field=field, mode=mode), self.assertRaisesRegex(ValueError, "properties date leaf not retained exactly"):
                    projected(changed)
                self.assertEqual(changed, before)
        for properties in (dict(fetched_at=123), dict(published_date=True),
                           dict(fetched_at=["2026-10-29"]),
                           dict(fetched_at="2026-10-29", publisher="Unknown source fact"), {}):
            changed = compiled_for(config, source_change=lambda r: r.update(properties=properties))
            before = deepcopy(changed)
            with self.subTest(properties=properties), self.assertRaisesRegex(ValueError, "unsupported-source-shape"):
                projected(changed)
            self.assertEqual(changed, before)

    def test_brave_typed_display_leaves_preserve_breadcrumbs_and_netloc_disagreement(self):
        config = next(c for c in self.catalog if c["configuration_id"] == "brave-web")
        compiled = compiled_for(config)
        before = deepcopy(compiled)
        source = next(r for r in compiled["packet"]["native_metadata"] if r["field"] == "native:meta_url")
        original = json.loads(source["text"])
        for route, packet in packets(compiled).items():
            with self.subTest(route=route):
                view = projected(compiled, packet)
                display = [r for r in view["packet"]["input"]["native_metadata"] if "source_display_kind" in r]
                self.assertEqual(len(display), 4)
                by_kind = {r["source_display_kind"]: r for r in display}
                self.assertEqual(json.loads(by_kind["source_navigation_display"]["text"]), "› Topics › Aster")
                self.assertEqual(json.loads(by_kind["source_netloc_display"]["text"]), "www.news.example")
                self.assertEqual(json.loads(by_kind["source_hostname_display"]["text"]), "news.example")
                self.assertEqual(json.loads(by_kind["source_scheme_display"]["text"]), "https")
                self.assertNotIn(original["favicon"], json.dumps(view["packet"]))
                for leaf in display:
                    self.assertEqual(leaf["value_type"], "string")
                    span = view["binding"]["source_leaf_spans"][leaf["field"]]
                    self.assertEqual(span["source_field"], "native:meta_url")
                    self.assertEqual(source["text"][span["start"]:span["end"]], leaf["text"])
        self.assertEqual(compiled, before)

    def test_brave_leaf_quotes_restore_exact_original_occurrence_with_duplicate_values(self):
        config = next(c for c in self.catalog if c["configuration_id"] == "brave-web")
        display = dict(favicon="shared", hostname="shared", netloc="shared", path="shared shared", scheme="shared")
        compiled = compiled_for(config, publication_window=False, source_change=lambda r: r.update(meta_url=display))
        before = deepcopy(compiled)
        view = projected(compiled)
        leaf_rows = [r for r in view["packet"]["input"]["native_metadata"] if "source_display_kind" in r]
        source = next(r for r in compiled["packet"]["native_metadata"] if r["field"] == "native:meta_url")
        for leaf in leaf_rows:
            for occurrence in ((1, 2) if leaf["source_display_kind"] == "source_navigation_display" else (0, 1)):
                raw = model_verdict(verdict(compiled), view)
                quote = dict(field=leaf["field"], quote="shared", occurrence=occurrence)
                raw["record_conditions"][0]["support"] = [quote]
                raw_before = deepcopy(raw)
                normalized = restore(raw, view)
                restored = normalized["record_conditions"][0]["support"][0]
                expected = text_dates.exact_span(leaf["text"], "shared", occurrence)
                bound_span = view["binding"]["source_leaf_spans"][leaf["field"]]
                checked = two_step.resolve_quotes(compiled["packet"], [restored])[0]
                self.assertEqual(checked["start"], bound_span["start"] + expected["start"])
                self.assertEqual(checked["end"], bound_span["start"] + expected["end"])
                self.assertEqual(restored["field"], "native:meta_url")
                self.assertEqual(restored["quote"], "shared")
                self.assertGreater(restored["occurrence"], 1)  # Earlier favicon value also matches.
                self.assertEqual(judging.decode_position(normalized, compiled)["state"], "valid")
                self.assertEqual(raw, raw_before)
        self.assertEqual(compiled, before)

    def test_brave_escaped_leaf_quotes_require_literal_json_and_bound_spans(self):
        config = next(c for c in self.catalog if c["configuration_id"] == "brave-web")
        display = dict(favicon="icon", hostname="host", netloc="netloc", path='› A"B \\ C **fold**', scheme="https")
        compiled = compiled_for(config, publication_window=False, source_change=lambda r: r.update(meta_url=display))
        view = projected(compiled)
        leaf = next(r for r in view["packet"]["input"]["native_metadata"]
                    if r.get("source_display_kind") == "source_navigation_display")
        raw = model_verdict(verdict(compiled), view)
        literal = json.dumps(display["path"], ensure_ascii=False)[1:-1]
        raw["record_conditions"][0]["support"] = [dict(field=leaf["field"], quote=literal, occurrence=1)]
        restored = restore(raw, view)
        self.assertEqual(two_step.resolve_quotes(compiled["packet"], restored["record_conditions"][0]["support"])[0]["quote"], literal)
        for quote, occurrence in ((display["path"], 1), ("C fold", 1), ("host", 1), ("fold", 2), ("made up", 1)):
            bad = deepcopy(raw)
            bad["record_conditions"][0]["support"][0].update(quote=quote, occurrence=occurrence)
            with self.subTest(quote=quote), self.assertRaises(ValueError):
                restore(bad, view)
        binding = deepcopy(view["binding"])
        binding["source_leaf_spans"][leaf["field"]]["start"] += 1
        with self.assertRaisesRegex(ValueError, "source/projection binding differs"):
            blind_input.restore_verdict(raw, binding, expected_binding_sha256=digest(binding))

    def test_brave_display_unknown_siblings_types_and_noncanonical_source_fail(self):
        config = next(c for c in self.catalog if c["configuration_id"] == "brave-web")
        original = dict(favicon="icon", hostname="news.example", netloc="news.example", path="› Aster", scheme="https")
        values = []
        for key in original:
            values.append(dict(original, **{key: None}))
            values.append({k: v for k, v in original.items() if k != key})
        values.append(dict(original, publisher="Unknown semantic leaf"))
        for display in values:
            compiled = compiled_for(config, source_change=lambda r: r.update(meta_url=display))
            before = deepcopy(compiled)
            with self.subTest(keys=sorted(display)), self.assertRaisesRegex(ValueError, "nested metadata native:meta_url"):
                projected(compiled)
            self.assertEqual(compiled, before)
        compiled = compiled_for(config)
        packet = deepcopy(compiled["packet"])
        row = next(r for r in packet["native_metadata"] if r["field"] == "native:meta_url")
        row["text"] = json.dumps(json.loads(row["text"]), ensure_ascii=True, sort_keys=True)
        changed = judging.compile_packet(packet, CAPTURED, config=config)
        with self.assertRaisesRegex(ValueError, "noncanonical source-display JSON"):
            projected(changed)

    def test_nested_noise_metadata_is_rejected_without_discarding_source(self):
        for value in ({"amount": 10, "publisher_note": "Material source fact"},
                      [10, "Unreviewed source meaning"]):
            compiled = compiled_for(self.valyu, source_change=lambda row: row.update(price=value))
            before = deepcopy(compiled)
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "nested noise metadata"):
                projected(compiled)
            self.assertEqual(compiled, before)

    def test_retained_url_and_field_state_copies_must_match_source(self):
        compiled = compiled_for(self.valyu)
        for change in (
            lambda data: data.update(returned_url="https://other.example/different-publisher"),
            lambda data: data["native_field_states"].update(title="missing"),
            lambda data: data.update(native_record_state="non_object_result"),
        ):
            for route, packet in packets(compiled).items():
                with self.subTest(route=route):
                    changed = deepcopy(packet)
                    change(changed["input"])
                    before = deepcopy(changed)
                    with self.assertRaisesRegex(ValueError, "model/source packet binding differs"):
                        projected(compiled, changed)
                    self.assertEqual(changed, before)

    def test_ordinary_quote_alias_restoration_and_existing_exact_quote_decoder(self):
        compiled = compiled_for(self.valyu, publication_window=False)
        view = projected(compiled)
        source_raw = verdict(compiled)
        source_raw["reason"] = 'Literal M1, native:source, $100 and 25% stay untouched.'
        source_quote = dict(field="native:source", quote="Synthetic editorial publisher", occurrence=1)
        source_raw["record_conditions"][0]["support"] = [source_quote]
        raw = model_verdict(source_raw, view)
        before = deepcopy(raw)
        normalized = restore(raw, view)
        self.assertEqual(normalized, source_raw)
        self.assertEqual(raw, before)
        self.assertEqual(judging.decode_position(normalized, compiled)["state"], "valid")
        bad = deepcopy(raw)
        bad["candidates"][0]["support"][0]["quote"] = "Facts from another result."
        # The helper restores references; the existing decoder still owns spans.
        self.assertNotEqual(judging.decode_position(restore(bad, view), compiled)["state"], "valid")

    def test_publication_and_adjudication_quote_slots_restore_without_text_rewriting(self):
        compiled = compiled_for(self.valyu)
        own_quote = dict(field="title", quote=TEXT, occurrence=1)
        publication_raw = verdict(compiled)
        publication_raw.update(contrary_reading="Literal field M1 is only a string here.",
                               complete_occurrence_reviewed=True,
                               policy_applications=[dict(candidate_ids=["finding-1"], predicate_id="publication:window",
                                   policy=publication.NATIVE_POLICY, status="pass", checks=[dict(check=check, status="pass",
                                       reason="Synthetic own-source check.", support=[deepcopy(own_quote)])
                                       for check in publication.CHECKS])])
        publication_raw["dates"] = [dict(candidate_id="finding-1", predicate_id="publication:window",
            link_status="established", role="first_publication", date_ids=["N1"], anchor_date_id="",
            additional_dates=[deepcopy(own_quote)], context_support=[deepcopy(own_quote)], reason="Own dates.")]
        adjudication_raw = dict(disposition="unknown", selected_native_date_ids=["N1"],
            checks=[dict(check=check, status="unknown", reason="Synthetic check.", support=[deepcopy(own_quote)])
                    for check in adjudication.CHECKS], contrary_reading="Literal M1.", reason="Own uncertainty.")
        for route, source_raw in (("publication", publication_raw), ("adjudication", adjudication_raw)):
            with self.subTest(route=route):
                view = projected(compiled, packets(compiled)[route])
                self.assertEqual(restore(model_verdict(source_raw, view), view), source_raw)

    def test_hidden_metadata_and_unknown_alias_cannot_be_quoted(self):
        compiled = compiled_for(self.valyu, publication_window=False)
        view = projected(compiled)
        aliases = view["binding"]["field_aliases"]
        self.assertNotIn("native:price", aliases.values())
        for bad_field in ("native:price", "native:source", "M999", "date:0"):
            raw = model_verdict(verdict(compiled), view)
            raw["record_conditions"][0]["support"] = [dict(field=bad_field, quote="901234", occurrence=1)]
            with self.subTest(field=bad_field), self.assertRaises(ValueError):
                restore(raw, view)

    def test_source_projection_alias_and_profile_tamper_are_rejected(self):
        compiled = compiled_for(self.valyu, publication_window=False)
        view = projected(compiled)
        raw = model_verdict(verdict(compiled), view)
        changes = (
            lambda b: b["source_packet"].update(case_id="changed"),
            lambda b: b["packet"]["input"].update(case_id="changed"),
            lambda b: b["field_aliases"].update(M999="native:price"),
            lambda b: b.update(projected_packet_sha256="different"),
            lambda b: b.update(profile="unknown"),
        )
        for change in changes:
            binding = deepcopy(view["binding"])
            change(binding)
            with self.assertRaisesRegex(ValueError, "binding hash differs"):
                blind_input.restore_verdict(raw, binding, expected_binding_sha256=view["binding_sha256"])
        # A self-consistent envelope hash alone does not excuse a false alias map.
        binding = deepcopy(view["binding"])
        binding["field_aliases"]["M999"] = "native:price"
        with self.assertRaisesRegex(ValueError, "source/projection binding differs"):
            blind_input.restore_verdict(raw, binding, expected_binding_sha256=digest(binding))
        with self.assertRaisesRegex(ValueError, "unsupported blind-input profile"):
            blind_input.project(judging.blind_packet(compiled), source_packet=compiled["packet"], profile="future")

    def test_inconsistent_source_copies_or_eligibility_fail_preparation(self):
        compiled = compiled_for(self.valyu)
        for change in (
            lambda p: p["input"]["allowed_text"][0].update(text="different"),
            lambda p: p["input"]["native_date_ids"][0].update(raw_value="2000-01-01"),
            lambda p: p["input"]["native_date_display"][0].update(declared_meaning="event"),
            lambda p: p["input"].update(eligible_native_date_ids=["N999"]),
            lambda p: p["input"]["publication_estimate_applicability"].update(unreviewed_source_fact="unknown"),
        ):
            packet = judging.blind_packet(compiled)
            change(packet)
            with self.assertRaises(ValueError):
                projected(compiled, packet)


if __name__ == "__main__":
    unittest.main()
