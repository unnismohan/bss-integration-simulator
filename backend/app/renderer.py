import json
import random
import re
from xml.sax.saxutils import escape
from defusedxml import ElementTree as ET
from defusedxml.common import DefusedXmlException
from typing import Any

from fastapi import HTTPException

from .schemas import ReplyConfig, ScenarioDefinition


def json_path(value: Any, path: str) -> Any:
    current = value
    for part in path.removeprefix("$").lstrip(".").split("."):
        if not part:
            continue
        if isinstance(current, list):
            current = current[int(part)]
        else:
            current = current[part]
    return current


def capture_values(scenario: ScenarioDefinition, body: bytes, headers: dict[str, str], query: dict[str, str], path_params: dict[str, str] | None = None) -> dict[str, Any]:
    values: dict[str, Any] = {}
    parsed_json = None
    parsed_xml = None
    for rule in scenario.captures:
        try:
            if rule.source == "header":
                values[rule.name] = headers[rule.path.lower()]
            elif rule.source == "query":
                values[rule.name] = query[rule.path]
            elif rule.source == "path":
                values[rule.name] = (path_params or {})[rule.path]
            elif rule.source == "json":
                if parsed_json is None:
                    parsed_json = json.loads(body.decode("utf-8"))
                values[rule.name] = json_path(parsed_json, rule.path)
            else:
                if parsed_xml is None:
                    parsed_xml = ET.fromstring(body)
                found = parsed_xml.find(rule.path, scenario.namespaces)
                if found is None:
                    raise KeyError(rule.path)
                values[rule.name] = found.text or ""
        except (KeyError, IndexError, ValueError, ET.ParseError, DefusedXmlException) as exc:
            raise HTTPException(status_code=400, detail=f"Could not capture '{rule.name}' from request") from exc
    return values


def resolve_fields(reply: ReplyConfig, captures: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, rule in reply.fields.items():
        if rule.source == "fixed":
            value = rule.value
        elif rule.source == "capture":
            if rule.capture not in captures:
                raise HTTPException(status_code=400, detail=f"Response field '{name}' refers to missing capture '{rule.capture}'")
            value = captures[rule.capture]
        elif rule.source == "random_int":
            value = random.randint(rule.minimum, rule.maximum)
        elif rule.source == "random_choice":
            value = random.choice(rule.choices)
        else:
            value = "".join(random.choice(rule.alphabet) for _ in range(rule.length))
        result[name] = value
    return result


def substitute(value: Any, fields: dict[str, Any]) -> Any:
    if isinstance(value, dict):
        return {key: substitute(child, fields) for key, child in value.items()}
    if isinstance(value, list):
        return [substitute(child, fields) for child in value]
    if isinstance(value, str):
        if value.startswith("{{") and value.endswith("}}"):
            key = value[2:-2].strip()
            return fields.get(key, value)
        for key, field_value in fields.items():
            value = value.replace("{{" + key + "}}", str(field_value))
    return value


def render(reply: ReplyConfig, captures: dict[str, Any], output_format: str = "json") -> tuple[bytes, str, int, dict[str, str]]:
    fields = resolve_fields(reply, captures)
    payload = substitute(reply.body, fields)
    content_type = reply.content_type
    if output_format == "json":
        if "json" not in content_type.lower():
            content_type = "application/json"
        encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    elif output_format == "xml":
        if isinstance(payload, (dict, list)):
            raise HTTPException(status_code=422, detail="XML response body must be a template string")
        if "xml" not in content_type.lower():
            content_type = "application/xml"
        xml_fields = {key: escape(str(value), {'"': '&quot;', "'": '&apos;'}) for key, value in fields.items()}
        encoded = substitute(str(reply.body), xml_fields).encode("utf-8")
    else:
        encoded = str(payload).encode("utf-8")
    return encoded, content_type, reply.status_code, reply.headers


def validate_captures(scenario: ScenarioDefinition):
    names_list = [capture.name for capture in scenario.captures]
    if len(names_list) != len(set(names_list)):
        raise ValueError("capture names must be unique")
    if any(not name.strip() for name in names_list):
        raise ValueError("capture names cannot be empty")
    names = set(names_list)
    replies = (
        [(scenario.response, scenario.response_format)]
        if scenario.flow == "sync"
        else [(scenario.ack, scenario.ack_format), (scenario.callback, scenario.callback_format)]
    )
    if scenario.flow == "async":
        names_needed = {scenario.async_config.correlation_capture}
        if not names_needed.issubset(names):
            raise ValueError("async correlation_capture must refer to a configured capture")
    for reply, output_format in replies:
        field_names = set(reply.fields)
        for field in reply.fields.values():
            if field.source == "capture" and field.capture not in names:
                raise ValueError(f"field references undefined capture '{field.capture}'")
        tokens: set[str] = set()
        def collect_tokens(value):
            if isinstance(value, str):
                tokens.update(re.findall(r"\{\{([^{}]+)\}\}", value))
            elif isinstance(value, dict):
                for child in value.values(): collect_tokens(child)
            elif isinstance(value, list):
                for child in value: collect_tokens(child)
        collect_tokens(reply.body)
        unknown = tokens - field_names
        if unknown:
            raise ValueError(f"response template references undefined value(s): {', '.join(sorted(unknown))}")
        if output_format == "xml":
            if not isinstance(reply.body, str):
                raise ValueError("XML response body must be a text template")
            xml_body = re.sub(r"\{\{([^{}]+)\}\}", "placeholder", reply.body)
            xml_body = re.sub(r"^\s*<\?xml[^?]*\?>", "", xml_body, count=1)
            try:
                ET.fromstring(xml_body)
            except (ET.ParseError, DefusedXmlException) as exc:
                raise ValueError(f"XML response template is not well formed: {exc}") from exc
