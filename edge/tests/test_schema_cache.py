"""The cached validator behaves exactly like jsonschema.validate, minus the re-checks."""
from __future__ import annotations

import jsonschema
import pytest
from mcp.server.lowlevel import server as lowlevel

from app import mcp_app  # noqa: F401  (installs the cache)
from app.schema_cache import _CachedJsonschema

SCHEMA = {
    "type": "object",
    "properties": {"name": {"type": "string"}, "n": {"type": "integer", "minimum": 0}},
    "required": ["name"],
    "additionalProperties": False,
}


def test_installed_in_the_sdk():
    assert isinstance(lowlevel.jsonschema, _CachedJsonschema)


@pytest.mark.parametrize("instance", [
    {"name": "a"}, {"name": "a", "n": 3}, {}, {"name": 1}, {"name": "a", "n": -1}, {"name": "a", "x": 1}, "str",
])
def test_same_outcome_and_message_as_jsonschema(instance):
    def outcome(validate):
        try:
            validate(instance=instance, schema=SCHEMA)
            return None
        except jsonschema.ValidationError as e:
            return e.message
    assert outcome(_CachedJsonschema().validate) == outcome(jsonschema.validate)


def test_schema_is_checked_once_and_still_checked(monkeypatch):
    calls = []
    real = jsonschema.Draft202012Validator.check_schema
    monkeypatch.setattr(jsonschema.Draft202012Validator, "check_schema",
                        classmethod(lambda cls, s, **kw: calls.append(1) or real(s, **kw)))
    v = _CachedJsonschema()
    schema = {**SCHEMA, "$schema": "https://json-schema.org/draft/2020-12/schema"}
    for _ in range(5):
        v.validate({"name": "a"}, schema)
    assert len(calls) == 1
    with pytest.raises(jsonschema.SchemaError):
        v.validate({}, {"type": 12})


def _call(client, name, arguments):
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                  "params": {"name": name, "arguments": arguments}},
                    headers={"Accept": "application/json, text/event-stream"})
    assert r.status_code == 200
    return r.json()["result"]


def test_tools_call_validates_output_through_the_cache(monkeypatch):
    """The hot path: a successful call's structured result is checked against the
    tool's outputSchema — by the cached validator, once per schema."""
    from fastapi.testclient import TestClient
    from app import main
    seen = []
    shim = lowlevel.jsonschema
    real = shim.validate
    monkeypatch.setattr(shim, "validate", lambda instance, schema: seen.append(schema) or real(instance, schema))
    with TestClient(main.app) as client:  # lifespan starts the MCP session manager
        for _ in range(3):
            result = _call(client, "whoami", {})
            assert not result.get("isError"), result
    assert len(seen) == 3 and all(s == seen[0] for s in seen)
