from copy import deepcopy

from openweb_news.common import asset_path, load
from openweb_news._method.two_step import date_predicate


def question(ident="synthetic-1"):
    return dict(question_id=ident, revision=1, family_id=ident, need_id=ident,
                provider_query="Find editorial reports about the fictional Aster observatory opening.",
                geography=dict(applies_to="no_geographic_restriction", regions=["global"]),
                contract=dict(source_rule=dict(qualifying_material=["editorial reporting"], independence_required=False),
                              temporal_requirements=[], as_of_requirement=None,
                              allowed_snippet_languages=["*"], background_context_allowed=False,
                              qualifying_predicates=["The item reports the fictional Aster observatory opening."],
                              requested_facets=[]))


def native_response(config, *, text="Editorial report: the fictional Aster observatory opened.", empty=False):
    fields = config["result_fields"]
    row = {fields["url"]: "https://news.example/aster", fields["title"]: text,
           fields["content"]: [dict(text=text)] if fields["content_shape"] == "object_text_array" else
           [text] if fields["content_shape"] == "string_array" else text}
    for field, shape in fields.get("additional_content", {}).items():
        row[field] = [text] if shape == "string_array" else text
    value = [] if empty else [row]
    for part in reversed(fields["container"].split(".")):
        value = {part: value}
    return value


def verdict(compiled, grade=2):
    packet = compiled["packet"]
    quote = dict(field="title", quote=packet["allowed_text"][0]["text"], occurrence=0)
    def condition(spec):
        return dict(predicate_id=spec["predicate_id"], status="pass",
                    reason="The exact own editorial report supports this condition.", support=[deepcopy(quote)])
    records, conditions = [], []
    for spec in packet["required_conditions"]:
        if not spec["required"]:
            continue
        if date_predicate(spec):
            continue
        (records if spec["scope"] == "record" else conditions).append(condition(spec))
    if grade == 1:
        conditions[0].update(status="not_demonstrated", support=[], reason="Specific pointer with missing answer proof.")
    if grade == 0:
        conditions[0].update(status="fail", reason="The own item establishes a disqualifier.")
    return dict(schema_version=1, case_id=packet["case_id"], status="judged", overall_grade=grade,
                reason="Synthetic model response for software verification.", dates=[], record_conditions=records,
                candidates=[dict(candidate_id="finding-1", claim="The fictional observatory opened.",
                                 answer_supported=grade == 2, clear_lead=grade != 0, support=[quote],
                                 conditions=conditions, supported_facet_ids=[])])
