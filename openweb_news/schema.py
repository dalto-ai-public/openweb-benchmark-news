"""Validation of the JSON Schema subset emitted by the frozen judge schema.

No remote references, coercion or response repair. Unknown keywords fail closed.
"""


def validate(value, schema, path="$", errors=None):
    errors = [] if errors is None else errors
    known = {"type", "properties", "required", "additionalProperties", "items",
             "enum", "anyOf", "maxItems", "minItems", "minimum", "maximum"}
    if set(schema) - known:
        errors.append(f"{path}: unsupported schema keyword")
        return errors
    if "anyOf" in schema and not any(not validate(value, branch) for branch in schema["anyOf"]):
        errors.append(f"{path}: no permitted alternative")
        return errors
    kind = schema.get("type")
    kinds = {"object": type(value) is dict, "array": type(value) is list,
             "integer": type(value) is int, "number": type(value) in {int, float},
             "string": type(value) is str, "boolean": type(value) is bool,
             "null": value is None}
    if kind and not kinds.get(kind, False):
        errors.append(f"{path}: expected {kind}")
        return errors
    if "enum" in schema and not any(type(value) is type(v) and value == v for v in schema["enum"]):
        errors.append(f"{path}: value outside enum")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        if set(schema.get("required", [])) - set(value):
            errors.append(f"{path}: missing required fields")
        if schema.get("additionalProperties") is False and set(value) - set(props):
            errors.append(f"{path}: additional fields")
        for key in set(value) & set(props):
            validate(value[key], props[key], path + "." + key, errors)
    if isinstance(value, list):
        if len(value) > schema.get("maxItems", len(value)) or len(value) < schema.get("minItems", 0):
            errors.append(f"{path}: array length differs")
        if "items" in schema:
            for index, item in enumerate(value):
                validate(item, schema["items"], f"{path}[{index}]", errors)
    if type(value) in {int, float}:
        if value < schema.get("minimum", value) or value > schema.get("maximum", value):
            errors.append(f"{path}: numeric bound differs")
    return errors
