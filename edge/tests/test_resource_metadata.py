"""Protected-resource metadata answers at the root and at the /mcp-derived path."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app import main


def test_metadata_is_served_where_clients_derive_it_from_the_endpoint():
    client = TestClient(main.app)
    root = client.get("/.well-known/oauth-protected-resource")
    derived = client.get("/.well-known/oauth-protected-resource/mcp")
    assert root.status_code == 200 and derived.status_code == 200
    assert derived.json() == root.json()
    assert derived.json()["authorization_servers"]
