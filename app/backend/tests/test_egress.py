"""LLM endpoints against the egress allowlist that runs in workspaces leave by.

A run in a workspace reaches the internet only through Squid's allowlist, so
an endpoint on any other host failed part way through the run (Codex,
PR #20). The list stays the operator's; the app refuses such an endpoint up
front, when it is saved and when a run is queued, and says who can fix it.
"""

from __future__ import annotations

import pytest

from app import egress, providers

ON = {"BLPL_RUNS_IN_WORKSPACE": "1"}


def _env(tmp_path, **extra):
    return {**ON, "BLPL_DATA_ROOT": str(tmp_path / "data"), **extra}


def test_every_host_the_catalog_names_is_allowed_by_default(tmp_path):
    shipped = egress.read(egress.allowlist_files({})[0])
    hosts = providers.catalog_hosts()
    assert {"api.anthropic.com", "api.openai.com", "api.together.xyz",
            "api.fireworks.ai", "api.deepinfra.com", "openrouter.ai"} <= set(hosts)
    missing = [h for h in hosts if not egress.matches(h, shipped)]
    assert missing == []
    targets = [egress.Target(name, "openai-compatible", url)
               for name, url in providers.OPENAI_COMPATIBLE_SERVICES.items()]
    targets += [egress.Target("a", "anthropic"), egress.Target("o", "openai")]
    egress.check(targets, _env(tmp_path))


def test_a_host_on_neither_list_is_refused_with_who_can_fix_it(tmp_path):
    with pytest.raises(egress.EgressRefused) as e:
        egress.check([egress.Target("mine", "openai-compatible", "https://llm.example.net/v1")],
                     _env(tmp_path))
    assert str(e.value).startswith(
        "llm.example.net is not on this server's egress allowlist — ask the operator to add it")


def test_an_operator_added_host_passes_without_a_restart(tmp_path):
    target = [egress.Target("mine", "openai-compatible", "https://llm.example.net/v1")]
    local = tmp_path / "data" / "egress" / "allowlist.txt"
    local.parent.mkdir(parents=True)
    local.write_text("# ours\n.example.net\n")
    egress.check(target, _env(tmp_path))
    assert "llm.example.net" not in egress.read(egress.allowlist_files({})[0])


def test_squid_allows_https_on_443_only(tmp_path):
    for url in ("http://api.together.xyz/v1", "https://api.together.xyz:8443/v1"):
        with pytest.raises(egress.EgressRefused, match="HTTPS on port 443"):
            egress.check([egress.Target("t", "openai-compatible", url)], _env(tmp_path))


def test_services_on_the_egress_network_are_reached_directly(tmp_path):
    env = _env(tmp_path, OLLAMA_HOST="http://ollama:11434", BLPL_SANDBOX_HTTP_PROXY="http://squid:3128")
    egress.check([egress.Target("local", "ollama"),
                  egress.Target("vllm", "openai-compatible", "http://ollama:11434/v1")], env)


def test_with_workspaces_off_nothing_is_refused(tmp_path):
    egress.check([egress.Target("mine", "openai-compatible", "http://10.1.2.3:8000/v1")],
                 {"BLPL_DATA_ROOT": str(tmp_path / "data")})


# -- through the routes ---------------------------------------------------------------

CUSTOM = {"endpoints": [{"name": "mine", "kind": "openai-compatible",
                         "base_url": "https://llm.example.net/v1", "model": "m", "auth": "none"}],
          "tasks": {"default": ["mine"]}}


def test_saving_an_endpoint_the_workspace_cannot_reach_is_refused(client, monkeypatch):
    from conftest import sign_in

    sign_in(client)
    monkeypatch.setenv("BLPL_RUNS_IN_WORKSPACE", "1")
    r = client.put("/api/settings/llm", json=CUSTOM)
    assert r.status_code == 400
    assert "llm.example.net is not on this server's egress allowlist" in r.json()["detail"]
    assert "ask the operator to add it" in r.json()["detail"]
    monkeypatch.setenv("BLPL_RUNS_IN_WORKSPACE", "0")
    assert client.put("/api/settings/llm", json=CUSTOM).status_code == 200


def _post_without_following(client, path):
    """POST a run route and return its response, or None if it became a run.

    A queued run answers with a stream that ends only when a worker finishes
    it, so a refusal is read in a thread that is given up on after a while."""
    import threading

    got = []
    t = threading.Thread(target=lambda: got.append(client.post(path)), daemon=True)
    t.start()
    t.join(timeout=5)
    return got[0] if got else None


def test_queuing_a_run_on_an_unreachable_endpoint_is_refused(client, monkeypatch):
    import app.main as main
    from conftest import enqueue_only, init_body, queued_env, sign_in

    sign_in(client)
    assert client.put("/api/settings/llm", json=CUSTOM).status_code == 200  # saved while off
    client.post("/api/projects/init", json=init_body("scratch"))
    monkeypatch.setenv("BLPL_RUNS_IN_WORKSPACE", "1")
    r = _post_without_following(client, "/api/projects/scratch/stages/stage1")
    assert r is not None, "the run was queued and its stream followed"
    assert r.status_code == 400
    assert r.json()["detail"].startswith("llm.example.net is not on this server's egress allowlist")

    local = main._DATA / "egress" / "allowlist.txt"
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_text("llm.example.net\n")
    enqueue_only(client, "/api/projects/scratch/stages/stage1")
    assert "HDM_LLM_CHAIN" in queued_env("scratch")
    assert "llm.example.net" in client.get("/api/settings/egress").json()["allowlist"]
