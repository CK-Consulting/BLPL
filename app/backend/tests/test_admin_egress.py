"""Settings -> Admin -> Network access: the server-wide egress hosts.

Administrators are named by BLPL_ADMIN_EMAILS; the routes answer 404 to
anyone else, and every change is recorded with who made it.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")


def test_the_admin_section_is_for_admins_only(client, monkeypatch):
    from conftest import sign_in

    sign_in(client, email="member@example.com")
    monkeypatch.setenv("BLPL_ADMIN_EMAILS", "admin@example.com")
    assert client.get("/api/settings").json()["is_admin"] is False
    assert client.get("/api/admin/egress").status_code == 404
    assert client.post("/api/admin/egress/hosts", json={"host": "a.example.com"}).status_code == 404


def test_an_admin_adds_and_removes_server_wide_hosts(client, monkeypatch):
    from conftest import sign_in

    sign_in(client, email="admin@example.com")
    monkeypatch.setenv("BLPL_ADMIN_EMAILS", "admin@example.com")
    assert client.get("/api/settings").json()["is_admin"] is True
    view = client.get("/api/admin/egress").json()
    assert "api.anthropic.com" in view["defaults"] and view["hosts"] == []
    r = client.post("/api/admin/egress/hosts", json={"host": "api.vendor.example"})
    assert r.status_code == 200 and r.json()["host"] == "api.vendor.example"
    bad = client.post("/api/admin/egress/hosts", json={"host": "10.0.0.1"})
    assert bad.status_code == 400 and "not an IP address" in bad.json()["detail"]
    view = client.get("/api/admin/egress").json()
    assert [h["host"] for h in view["hosts"]] == ["api.vendor.example"]
    assert view["hosts"][0]["added_by"] == "admin@example.com"
    assert client.delete("/api/admin/egress/hosts/api.vendor.example").status_code == 200
    assert client.delete("/api/admin/egress/hosts/api.vendor.example").status_code == 404
    events = client.get("/api/admin/egress").json()["events"]
    assert [(e["host"], e["action"]) for e in events] == [
        ("api.vendor.example", "removed"), ("api.vendor.example", "added")]
