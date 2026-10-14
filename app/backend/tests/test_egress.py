"""Workspace egress: per-run grants, the administrators' list, and what is
still checked when an endpoint is saved (app/egress.py).

A run in a workspace reaches the internet only through Squid, which asks the
control plane about every host for the run's own credential. A member's own
LLM endpoint is reachable from their runs, not from anyone else's; the
administrators' hosts and the shipped defaults from every run.
"""

from __future__ import annotations

import json
import os
import socket
from datetime import timedelta

import pytest

from app import egress, providers

pytest.importorskip("sqlalchemy")


@pytest.fixture
def db(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app import models

    engine = create_engine(f"sqlite:///{tmp_path / 'e.db'}")
    models.Base.metadata.create_all(engine)
    with sessionmaker(engine)() as session:
        yield session


def _user(db, email="admin@example.com", clerk="user_a"):
    from app.models import User

    u = User(clerk_user_id=clerk, email=email)
    db.add(u)
    db.flush()
    return u


# -- the shipped defaults -----------------------------------------------------------------


def test_every_host_the_catalog_names_is_a_default():
    shipped = egress.defaults()
    hosts = providers.catalog_hosts()
    assert {"api.anthropic.com", "api.openai.com", "api.together.xyz",
            "api.fireworks.ai", "api.deepinfra.com", "openrouter.ai"} <= set(hosts)
    assert [h for h in hosts if not egress.matches(h, shipped)] == []


def test_the_distributor_apis_kicad_happy_calls_are_defaults():
    for host in ("api.digikey.com", "api.mouser.com", "api.element14.com",
                 "jlcsearch.tscircuit.com", "wmsc.lcsc.com", "api.nexar.com",
                 "identity.nexar.com", "github.com"):
        assert egress.matches(host, egress.defaults()), host
    # A document host is not: documents come through the control plane.
    assert not egress.matches("ww1.microchip.com", egress.defaults())


# -- grants ---------------------------------------------------------------------------------


def test_a_members_own_endpoint_is_reachable_from_their_run_only(db):
    alice_env = {"HDM_LLM_CHAIN": json.dumps([
        {"provider": "openai-compatible", "endpoint": "mine",
         "base_url": "https://llm.alice.example/v1"}])}
    a, a_tok = egress.issue(db, kind="run", project="p", hosts=egress.run_hosts(alice_env))
    b, b_tok = egress.issue(db, kind="run", project="p", hosts=[])
    assert egress.may_reach(db, a, "llm.alice.example", 443)
    assert not egress.may_reach(db, b, "llm.alice.example", 443)
    for g in (a, b):
        assert egress.may_reach(db, g, "api.anthropic.com", 443)
        assert not egress.may_reach(db, g, "elsewhere.example", 443)
        assert not egress.may_reach(db, g, "api.anthropic.com", 8443)
        assert egress.may_reach(db, g, "github.com", 22)
        assert not egress.may_reach(db, g, "llm.alice.example", 22)


def test_a_grant_authenticates_with_its_own_token_until_it_is_revoked_or_expires(db):
    name, token = egress.issue(db, kind="run", project="p")
    assert egress.authenticate(db, name, token)
    assert not egress.authenticate(db, name, token + "x")
    assert not egress.authenticate(db, "g-unknown", token)
    old, old_tok = egress.issue(db, kind="git", project="p", ttl=timedelta(seconds=-1))
    assert not egress.authenticate(db, old, old_tok)
    assert not egress.may_reach(db, old, "api.anthropic.com", 443)
    egress.revoke(db, name)
    assert not egress.authenticate(db, name, token)
    assert not egress.may_reach(db, name, "api.anthropic.com", 443)


def test_a_malformed_question_is_a_no(db):
    name, token = egress.issue(db, kind="run", project="p")
    assert egress.decide(db, {"op": "auth", "user": name, "password": token})
    for q in ({}, {"op": "acl", "user": name, "host": "api.anthropic.com", "port": "x"},
              {"op": "other", "user": name}, {"op": "acl", "user": name}):
        assert not egress.decide(db, q)


def test_the_proxy_variables_carry_the_credential():
    env = egress.proxy_env("g-1", "t/o+k", {"BLPL_SANDBOX_HTTP_PROXY": "http://squid:3128"})
    assert env["HTTPS_PROXY"] == "http://g-1:t%2Fo%2Bk@squid:3128"
    assert egress.proxy_env("g-1", "t", {}) == {}


# -- the administrators' list -------------------------------------------------------------


def test_an_admin_host_reaches_every_run_and_removal_takes_it_away(db):
    admin = _user(db)
    g, _ = egress.issue(db, kind="run", project="p")
    assert not egress.may_reach(db, g, "api.vendor.example", 443)
    assert egress.add_host(db, admin, " API.Vendor.Example. ") == "api.vendor.example"
    egress.add_host(db, admin, ".parts.example")
    assert egress.may_reach(db, g, "api.vendor.example", 443)
    assert egress.may_reach(db, g, "eu.parts.example", 443)
    assert egress.remove_host(db, admin, "api.vendor.example")
    assert not egress.may_reach(db, g, "api.vendor.example", 443)
    from app.models import EgressHostEvent

    events = [(e.host, e.action, e.email) for e in db.query(EgressHostEvent).order_by("id")]
    assert events == [("api.vendor.example", "added", "admin@example.com"),
                      (".parts.example", "added", "admin@example.com"),
                      ("api.vendor.example", "removed", "admin@example.com")]


@pytest.mark.parametrize("raw, message", [
    ("10.0.0.5", "not an IP address"), ("::1", "not an IP address"),
    ("*.example.com", "leading dot"), ("https://api.example.com", "hostname only"),
    ("api.example.com:443", "hostname only"), ("intranet", "full hostname"),
    ("db.internal", "not a public host"), ("printer.local", "not a public host"),
    ("bad_host.example", "not a valid hostname"), ("", "enter a hostname"),
])
def test_server_wide_entries_are_validated(raw, message):
    with pytest.raises(ValueError, match=message):
        egress.normalise_entry(raw)


def test_admins_are_named_by_verified_email(db):
    env = {"BLPL_ADMIN_EMAILS": "Admin@Example.com, ops@example.com"}
    assert egress.is_admin(_user(db), env)
    assert not egress.is_admin(_user(db, "someone@example.com", "user_b"), env)
    placeholder = _user(db, "ops@example.com", None)
    assert not egress.is_admin(placeholder, env)
    assert not egress.is_admin(_user(db, "x@example.com", "user_c"), {})


# -- the Squid helper's socket ------------------------------------------------------------


def test_the_decision_socket_fails_closed(tmp_path):
    from app import egress_server

    def decide(q):
        if q.get("user") == "boom":
            raise RuntimeError("database gone")
        return q.get("user") == "ok"

    path = str(tmp_path / "decide.sock")
    server = egress_server.serve(path, decide)
    try:
        assert oct(os.stat(path).st_mode & 0o777) == "0o666"

        def ask(line: bytes) -> bytes:
            with socket.socket(socket.AF_UNIX) as s:
                s.connect(path)
                s.sendall(line)
                return s.makefile("rb").readline()

        assert ask(b'{"op":"auth","user":"ok"}\n') == b"OK\n"
        assert ask(b'{"op":"auth","user":"boom"}\n') == b"ERR\n"
        assert ask(b"not json\n") == b"ERR\n"
        assert ask(b'["a list"]\n') == b"ERR\n"
        assert ask(b'{"user":"' + b"x" * 5000 + b'"}\n') == b"ERR\n"
    finally:
        server.shutdown()
        server.server_close()


# -- what is still checked at save --------------------------------------------------------

ON = {"BLPL_RUNS_IN_WORKSPACE": "1"}


def _public(_h):
    return [(socket.AF_INET, 0, 0, "", ("93.184.216.34", 443))]


def test_a_members_own_https_host_is_no_longer_refused():
    egress.check([egress.Target("mine", "openai-compatible", "https://llm.alice.example/v1")],
                 ON, resolve=_public)


@pytest.mark.parametrize("url, message", [
    ("http://llm.example.net/v1", "HTTPS on port 443"),
    ("https://llm.example.net:8443/v1", "HTTPS on port 443"),
    ("https://10.1.2.3/v1", "private or local address"),
    ("https://169.254.169.254/latest", "private or local address"),
    ("https://localhost/v1", "private or local address"),
])
def test_what_squid_refuses_for_anyone_is_said_at_save(url, message):
    with pytest.raises(egress.EgressRefused, match=message):
        egress.check([egress.Target("t", "openai-compatible", url)], ON, resolve=_public)


def test_a_name_that_resolves_privately_is_refused():
    def private(_h):
        return [(socket.AF_INET, 0, 0, "", ("192.168.1.20", 443))]

    with pytest.raises(egress.EgressRefused, match="resolves to a private address"):
        egress.check([egress.Target("t", "openai-compatible", "https://llm.home.example")],
                     ON, resolve=private)


def test_with_workspaces_off_nothing_is_refused():
    egress.check([egress.Target("t", "openai-compatible", "http://10.1.2.3:8000/v1")], {})


def test_services_on_the_egress_network_are_reached_directly():
    env = {**ON, "OLLAMA_HOST": "http://ollama:11434",
           "BLPL_SANDBOX_HTTP_PROXY": "http://squid:3128"}
    egress.check([egress.Target("vllm", "openai-compatible", "http://ollama:11434/v1")], env)


# -- through the routes -------------------------------------------------------------------

CUSTOM = {"endpoints": [{"name": "mine", "kind": "openai-compatible",
                         "base_url": "https://llm.example.net/v1", "model": "m", "auth": "none"}],
          "tasks": {"default": ["mine"]}}


def test_saving_a_members_own_endpoint_needs_nobody_else(client, monkeypatch):
    from conftest import enqueue_only, init_body, queued_env, sign_in

    sign_in(client)
    monkeypatch.setenv("BLPL_RUNS_IN_WORKSPACE", "1")
    monkeypatch.setattr(egress.socket, "getaddrinfo", lambda *a, **k: _public(None))
    assert client.put("/api/settings/llm", json=CUSTOM).status_code == 200
    client.post("/api/projects/init", json=init_body("scratch"))
    enqueue_only(client, "/api/projects/scratch/stages/stage1")
    assert "llm.example.net" in queued_env("scratch")["HDM_LLM_CHAIN"]
    bad = {**CUSTOM, "endpoints": [{**CUSTOM["endpoints"][0],
                                    "base_url": "http://llm.example.net/v1"}]}
    r = client.put("/api/settings/llm", json=bad)
    assert r.status_code == 400 and "HTTPS on port 443" in r.json()["detail"]
    assert "ask the operator" not in r.text


def test_every_grant_of_a_run_is_revoked_together(db):
    first, t1 = egress.issue(db, kind="run", project="p", run_id="run_1")
    retry, t2 = egress.issue(db, kind="run", project="p", run_id="run_1")
    other, t3 = egress.issue(db, kind="run", project="p", run_id="run_2")
    assert egress.revoke_run(db, "run_1") == 2
    assert not egress.authenticate(db, first, t1) and not egress.authenticate(db, retry, t2)
    assert egress.authenticate(db, other, t3)
