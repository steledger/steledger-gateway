"""Cache the JSON-Schema validators the MCP SDK builds on every tool call.

The SDK's low-level server calls `jsonschema.validate(instance, schema)` twice per
`tools/call` — once for the arguments, once for the structured result. That
function re-checks the *schema itself* against the 2020-12 meta-schema and builds
a fresh validator every time; jsonschema's own docs say to reuse a validator when
the schema does not change. Profiled under load on 2026-10-11 it was ~60% of the
edge's CPU, the reason one vCPU topped out near 20 reads/s.

`install()` swaps the SDK module's `jsonschema` for a shim whose `validate` keeps
one checked validator per distinct schema and raises exactly what the original
would: the schema is still checked (once), and the error is chosen by the same
`best_match`.
"""
from __future__ import annotations

import json

import jsonschema
from jsonschema import exceptions, validators
from mcp.server.lowlevel import server as _lowlevel


class _CachedJsonschema:
    ValidationError = jsonschema.ValidationError
    SchemaError = jsonschema.SchemaError

    def __init__(self) -> None:
        self._validators: dict[str, object] = {}

    def validate(self, instance, schema) -> None:
        # Keyed by content, not identity: the SDK may rebuild tool definitions.
        key = json.dumps(schema, sort_keys=True, default=str)
        validator = self._validators.get(key)
        if validator is None:
            cls = validators.validator_for(schema)
            cls.check_schema(schema)
            validator = self._validators[key] = cls(schema)
        error = exceptions.best_match(validator.iter_errors(instance))
        if error is not None:
            raise error

    def __getattr__(self, name):  # anything else the SDK might reach for
        return getattr(jsonschema, name)


def install() -> None:
    if not isinstance(_lowlevel.jsonschema, _CachedJsonschema):
        _lowlevel.jsonschema = _CachedJsonschema()
