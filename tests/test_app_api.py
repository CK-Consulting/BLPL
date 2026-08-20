"""End-to-end API tests: the session gate, first-run setup, settings, and the
project git flow, exercised through the real FastAPI app.

The ``client`` fixture (tmp state roots, cheap Argon2id, fresh module import)
lives in conftest.py — it is shared with the reference/conversation API tests.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from conftest import enqueue_only, queued_env, running_worker, sign_in


def _seed_remote(tmp_path: Path) -> str:
    work = tmp_path / "seed"
    work.mkdir()
    for a in (["init", "-b", "main"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", *a], cwd=work, check=True, capture_output=True)
    (work / "design.md").write_text("# design\n")
    subprocess.run(["git", "add", "-A"], cwd=work, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=work, check=True, capture_output=True)
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "--bare", str(work), str(bare)], check=True, capture_output=True)
    return f"file://{bare}"


# -- auth gate --------------------------------------------------------------


def test_health_is_reachable_without_a_session(client) -> None:
    assert client.get("/api/health").status_code == 200


def test_the_config_probe_is_reachable_without_a_session(client) -> None:
    """The UI must be able to tell "you are signed out" from "this server has no
    Clerk issuer set" — they look identical in a browser and have completely
    different remedies, so this one route answers before authentication."""
    assert client.get("/api/auth/config").json() == {"clerk_configured": True}


def test_the_api_is_closed_until_you_sign_in(client) -> None:
    assert client.get("/api/projects").status_code == 401
    assert client.get("/api/settings").status_code == 401


def test_an_unverifiable_token_is_refused(client) -> None:
    """The gate is the verification, not the presence of a header."""
    client.headers.update({"Authorization": "Bearer not-a-real-token"})
    assert client.get("/api/projects").status_code == 401


def test_signing_in_opens_the_gate_and_creates_the_user(client) -> None:
    sign_in(client)
    assert client.get("/api/projects").status_code == 200
    me = client.get("/api/me").json()
    assert me["clerk_user_id"] == "user_test" and me["email"] == "test@example.com"


def test_the_same_clerk_subject_resolves_to_one_user_row(client) -> None:
    """Sign-in is get-or-create, so a returning user must not accumulate rows —
    each one would come with its own separate set of provider keys."""
    sign_in(client)
    first = client.get("/api/me").json()["id"]
    for _ in range(3):
        assert client.get("/api/me").json()["id"] == first


# -- settings ---------------------------------------------------------------


def test_settings_shows_key_presence_never_values(client) -> None:
    sign_in(client)
    client.put("/api/settings/secrets/anthropic", json={"value": "sk-ant-SECRET"})

    s = client.get("/api/settings").json()
    providers = {row["provider"] for row in s["secrets"]}
    assert providers == {"anthropic"}
    # The value must appear nowhere in the settings payload.
    assert "sk-ant-SECRET" not in client.get("/api/settings").text


def test_the_registry_round_trips_and_rejects_nonsense(client) -> None:
    """The provider-shaped priority/models body is gone; endpoints and tasks are
    the only way to say this now, so there is one code path rather than two that
    could disagree about what is configured."""
    sign_in(client)
    ok = client.put(
        "/api/settings/llm",
        json={
            "endpoints": [
                {"name": "openai", "kind": "openai"},
                {"name": "anthropic", "kind": "anthropic"},
            ],
            "tasks": {"default": ["openai", "anthropic"]},
        },
    )
    assert ok.status_code == 200
    assert client.get("/api/settings").json()["tasks"]["default"] == ["openai", "anthropic"]

    # A chain naming an endpoint that does not exist would silently shorten the
    # fallback list, so it is refused rather than trimmed.
    bad = client.put(
        "/api/settings/llm",
        json={
            "endpoints": [{"name": "openai", "kind": "openai"}],
            "tasks": {"default": ["made-up"]},
        },
    )
    assert bad.status_code == 400


def test_a_deleted_key_disappears_from_settings(client) -> None:
    sign_in(client)
    client.put("/api/settings/secrets/openai", json={"value": "sk-oai"})
    client.delete("/api/settings/secrets/openai")
    assert client.get("/api/settings").json()["secrets"] == []


# -- projects (git-backed) --------------------------------------------------


def test_clone_registers_a_project(tmp_path, client) -> None:
    sign_in(client)
    remote = _seed_remote(tmp_path)
    r = client.post("/api/projects/clone", json={"name": "dev04", "remote": remote, "branch": "main"})
    assert r.status_code == 200
    listing = client.get("/api/projects").json()
    assert any(p["id"] == "dev04" and p["is_git"] for p in listing)


def test_git_status_reports_clean_after_clone(tmp_path, client) -> None:
    sign_in(client)
    remote = _seed_remote(tmp_path)
    client.post("/api/projects/clone", json={"name": "dev04", "remote": remote, "branch": "main"})
    st = client.get("/api/projects/dev04/git/status").json()
    assert st["branch"] == "main" and st["dirty"] is False and st["has_remote"] is True


def test_init_creates_a_local_project(client) -> None:
    sign_in(client)
    r = client.post("/api/projects/init", json={"name": "scratch"})
    assert r.status_code == 200
    st = client.get("/api/projects/scratch/git/status").json()
    assert st["has_remote"] is False


def test_running_an_llm_stage_without_a_key_is_a_clear_error(tmp_path, client) -> None:
    """Stage 1 needs a provider key. With none stored, the app must refuse up
    front with an actionable message, not fail deep inside the subprocess."""
    sign_in(client)
    client.post("/api/projects/init", json={"name": "scratch"})
    r = client.post("/api/projects/scratch/stages/stage1")
    assert r.status_code == 400
    assert "key" in r.json()["detail"].lower()


def test_an_llm_stage_injects_the_full_fallback_chain(tmp_path, client, monkeypatch) -> None:
    """With two keys and a two-provider priority, the stage subprocess must get
    the ordered chain plus both keys — that is what makes runtime failover work.

    We intercept the subprocess launch and inspect the env it would have run with,
    rather than actually invoking the pipeline."""
    import app.main as main


    sign_in(client)
    client.put(
        "/api/settings/llm",
        json={
            "endpoints": [
                {"name": "anthropic", "kind": "anthropic"},
                {"name": "openai", "kind": "openai"},
            ],
            "tasks": {"default": ["anthropic", "openai"], "review_panel": ["anthropic", "openai"]},
        },
    )
    client.put("/api/settings/secrets/anthropic", json={"value": "sk-ant-KEY"})
    client.put("/api/settings/secrets/openai", json={"value": "sk-oai-KEY"})
    client.post("/api/projects/init", json={"name": "scratch"})

    enqueue_only(client, "/api/projects/scratch/stages/stage1")
    env = queued_env("scratch")
    import json as _json
    chain = _json.loads(env["HDM_LLM_CHAIN"])
    assert [c["provider"] for c in chain] == ["anthropic", "openai"]

    # Each endpoint's key rides in its OWN variable, named by the chain entry.
    # A single shared ANTHROPIC_API_KEY could not carry two Anthropic endpoints
    # on different accounts, which is what the endpoint registry exists to allow.
    assert [c["key_env"] for c in chain] == ["BLPL_LLM_KEY__ANTHROPIC", "BLPL_LLM_KEY__OPENAI"]
    assert env["BLPL_LLM_KEY__ANTHROPIC"] == "sk-ant-KEY"
    assert env["BLPL_LLM_KEY__OPENAI"] == "sk-oai-KEY"

    # The primary is also mirrored onto the SDK's own variable, for anything
    # that reads it directly. Only the primary — two endpoints of one kind must
    # not fight over one global.
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-KEY"
    assert env["HDM_LLM_PROVIDER"] == "anthropic"


def test_a_deterministic_stage_runs_without_any_key(tmp_path, client) -> None:
    """doctor is deterministic — it must run with no provider configured. It will
    exit non-zero on an empty project, but the request itself must stream, not 400."""
    sign_in(client)
    client.post("/api/projects/init", json={"name": "scratch"})
    with running_worker():
        with client.stream("POST", "/api/projects/scratch/stages/doctor") as r:
            assert r.status_code == 200
            body = "".join(r.iter_text())
    assert "event: done" in body


# -- file editing -----------------------------------------------------------


def test_files_can_be_created_listed_read_and_updated(client) -> None:
    sign_in(client)
    client.post("/api/projects/init", json={"name": "scratch"})

    # A fresh project has no editable files.
    assert client.get("/api/projects/scratch/files").json() == []

    # Create one.
    w = client.put("/api/projects/scratch/files/overview.md", json={"content": "# Board\n"})
    assert w.status_code == 200
    listing = client.get("/api/projects/scratch/files").json()
    assert [f["name"] for f in listing] == ["overview.md"]

    # Read it back.
    assert client.get("/api/projects/scratch/files/overview.md").json()["content"] == "# Board\n"

    # Update it.
    client.put("/api/projects/scratch/files/overview.md", json={"content": "# Board v2\n"})
    assert client.get("/api/projects/scratch/files/overview.md").json()["content"] == "# Board v2\n"


def test_only_editable_suffixes_are_allowed(client) -> None:
    sign_in(client)
    client.post("/api/projects/init", json={"name": "scratch"})
    # A .py file is not a design input.
    assert client.put("/api/projects/scratch/files/evil.py", json={"content": "x"}).status_code == 400
    # project.yaml (config) is editable.
    assert client.put("/api/projects/scratch/files/project.yaml", json={"content": "project:\n"}).status_code == 200


def test_file_names_cannot_traverse_out_of_the_project(client) -> None:
    """A subdirectory is allowed; leaving the project is not.

    `sub/dir.md` used to be refused along with the rest, back when every design
    document sat in the project root. Multi-board projects put each board's
    design document in the board's own directory, so refusing a subdirectory
    made the real design document of every board unopenable. Widening what may
    be *named* is not the same as widening where it may *land*, which is what
    the rest of this list is here to hold.
    """
    sign_in(client)
    client.post("/api/projects/init", json={"name": "scratch"})
    for bad in ("../secret.md", ".hidden.md", "sub/../../secret.md", ".git/config.yaml"):
        r = client.put(f"/api/projects/scratch/files/{bad}", json={"content": "x"})
        assert r.status_code in (400, 404), f"{bad!r} should be rejected, got {r.status_code}"


def test_a_board_keeps_its_design_document_in_its_own_directory(client) -> None:
    """The multi-board shape, end to end: create it, find it in the listing,
    read it back. The editor ignores a selection missing from the listing, so
    a file that can be written but not listed is a click that does nothing."""
    sign_in(client)
    client.post("/api/projects/init", json={"name": "scratch"})

    w = client.put("/api/projects/scratch/files/base/design.md", json={"content": "# base\n"})
    assert w.status_code == 200, w.text

    listing = [f["name"] for f in client.get("/api/projects/scratch/files").json()]
    assert "base/design.md" in listing

    r = client.get("/api/projects/scratch/files/base/design.md")
    assert r.status_code == 200 and r.json()["content"] == "# base\n"


def test_everything_the_listing_offers_can_actually_be_opened(client) -> None:
    """The listing and the reader must agree on what a name means, or the
    workbench shows files it cannot open."""
    sign_in(client)
    client.post("/api/projects/init", json={"name": "scratch"})
    for rel in ("overview.md", "base/design.md", "sensor/design.md", "sensor/notes.yaml"):
        assert client.put(f"/api/projects/scratch/files/{rel}", json={"content": "x"}).status_code == 200

    for entry in client.get("/api/projects/scratch/files").json():
        got = client.get(f"/api/projects/scratch/files/{entry['name']}")
        assert got.status_code == 200, f"{entry['name']} listed but not readable"


def test_the_listing_does_not_wander_into_git_or_the_pipeline(client) -> None:
    """Recursing is what makes board directories visible; recursing everywhere
    would bury the design inputs under machinery."""
    sign_in(client)
    client.post("/api/projects/init", json={"name": "scratch"})
    client.put("/api/projects/scratch/files/base/design.md", json={"content": "x"})

    names = {f["name"] for f in client.get("/api/projects/scratch/files").json()}
    assert "base/design.md" in names
    assert not any(n.startswith(".") or "/." in n for n in names), names
    assert not any(n.startswith(".pipeline/") for n in names), names


def test_generated_pipeline_files_are_not_editable(client) -> None:
    """.pipeline/ artifacts are read-only outputs, reached through /artifacts, not
    the editor — the editor is only for design inputs."""
    sign_in(client)
    client.post("/api/projects/init", json={"name": "scratch"})
    # Even with a valid suffix, a path into .pipeline/ must not resolve here.
    r = client.get("/api/projects/scratch/files/.pipeline")
    assert r.status_code in (400, 404)


# -- cookie Secure flag: the footgun that dropped sessions over HTTP ----------


# -- whole-pipeline runner ----------------------------------------------------


def test_pipeline_range_streams_and_needs_no_key_when_llm_excluded(client) -> None:
    """A stage5→8 range has no LLM stage, so it must run with no key configured —
    it will exit non-zero on an unbuilt project, but the request itself streams."""
    sign_in(client)
    client.post("/api/projects/init", json={"name": "scratch"})
    with running_worker():
        with client.stream("POST", "/api/projects/scratch/pipeline?from_stage=stage5&to_stage=stage8") as r:
            assert r.status_code == 200
            body = "".join(r.iter_text())
        assert "event: done" in body


def test_pipeline_range_including_stage1_requires_a_key(client) -> None:
    sign_in(client)
    client.post("/api/projects/init", json={"name": "scratch"})
    r = client.post("/api/projects/scratch/pipeline?from_stage=stage0&to_stage=stage8")
    assert r.status_code == 400
    assert "key" in r.json()["detail"].lower()


def test_pipeline_rejects_a_backwards_range(client) -> None:
    sign_in(client)
    client.post("/api/projects/init", json={"name": "scratch"})
    r = client.post("/api/projects/scratch/pipeline?from_stage=stage8&to_stage=stage2")
    assert r.status_code == 400


def test_pipeline_rejects_an_unknown_stage(client) -> None:
    sign_in(client)
    client.post("/api/projects/init", json={"name": "scratch"})
    r = client.post("/api/projects/scratch/pipeline?from_stage=stage9&to_stage=stage9")
    assert r.status_code == 400


# -- fabrication readiness surfaced in the project list -----------------------


def test_project_list_flags_a_board_with_placeholders(client) -> None:
    import json as _json, os
    sign_in(client)
    client.post("/api/projects/init", json={"name": "scratch"})

    # No review yet → fab is null.
    proj = next(p for p in client.get("/api/projects").json() if p["id"] == "scratch")
    assert proj["fab"] is None

    # Drop a review_report.json that reports placeholders.
    import app.main as main
    pipeline = main.PROJECTS_ROOT / "scratch" / ".pipeline"
    pipeline.mkdir(parents=True, exist_ok=True)
    (pipeline / "review_report.json").write_text(
        _json.dumps({"summary": {"placeholders": 3, "emitter": 0, "design": 5, "expected": 2}})
    )
    proj = next(p for p in client.get("/api/projects").json() if p["id"] == "scratch")
    assert proj["fab"]["blocked"] is True
    assert proj["fab"]["placeholders"] == 3


def test_project_list_fab_is_null_when_review_is_unparseable(client) -> None:
    sign_in(client)
    client.post("/api/projects/init", json={"name": "scratch"})
    import app.main as main
    pipeline = main.PROJECTS_ROOT / "scratch" / ".pipeline"
    pipeline.mkdir(parents=True, exist_ok=True)
    (pipeline / "review_report.json").write_text("{ not json")
    proj = next(p for p in client.get("/api/projects").json() if p["id"] == "scratch")
    assert proj["fab"] is None


def test_preflight_needs_a_session(client) -> None:
    assert client.get("/api/projects/scratch/preflight").status_code == 401


def test_preflight_reports_what_stage0_would_discard(unlocked) -> None:
    """The panel's whole reason to exist: Stage 0 drops tables it cannot
    classify and says nothing, so the discard has to be visible *before* a run
    rather than inferred from an artifact diff afterwards."""
    import app.main as main

    unlocked.post("/api/projects/init", json={"name": "scratch"})
    proj = main.PROJECTS_ROOT / "scratch"
    (proj / "design.md").write_text(
        "## Net classes\n\n"
        "| Class | Trace width | Clearance |\n|---|---|---|\n| Power | 0.5 | 0.2 |\n\n"
        "## BOM\n\n| Ref | MPN | Package |\n|---|---|---|\n| U_MCU | ESP32-S3 | Module |\n",
        encoding="utf-8",
    )
    r = unlocked.get("/api/projects/scratch/preflight")
    assert r.status_code == 200
    body = r.json()
    codes = {f["code"] for f in body["findings"]}
    # The net-class table matches neither a BOM nor a pinout, so it is silently
    # dropped; the MCU has no pinout, so Stage 3 will halt on it.
    assert "DOC-001" in codes
    assert "DOC-011" in codes
    assert body["summary"]["tables_discarded"] == 1


def test_preflight_mutates_nothing(unlocked) -> None:
    # It is safe to call on every visit to the tab, which is why the panel does.
    import app.main as main

    unlocked.post("/api/projects/init", json={"name": "scratch"})
    proj = main.PROJECTS_ROOT / "scratch"
    (proj / "design.md").write_text("## Notes\n\nnothing here\n", encoding="utf-8")
    before = sorted(p.name for p in proj.iterdir())
    unlocked.get("/api/projects/scratch/preflight")
    assert sorted(p.name for p in proj.iterdir()) == before


def test_the_spa_fallback_refuses_to_escape_the_static_root(client) -> None:
    """A regression pin, not a fix: the catch-all resolves and re-checks
    containment, so `..` segments fall through to index.html instead of
    serving whatever they land on. Skipped when no bundle is built, since the
    route only mounts if one is."""
    import app.main as main

    if not (main._STATIC_DIR / "index.html").is_file():
        pytest.skip("no built frontend bundle — the SPA fallback is not mounted")
    for attempt in ("../../../etc/passwd", "..%2f..%2f..%2fetc%2fpasswd"):
        r = client.get(f"/{attempt}")
        assert r.status_code == 200
        assert "root:" not in r.text


def test_a_quarantined_file_is_never_served_as_a_pdf(client) -> None:
    """A PDF handed to the browser inline goes straight into a viewer, and a
    viewer is exactly what an `/OpenAction` is written to talk to. Retrieved
    files come back opaque, so opening one is a deliberate act."""
    import app.main as main
    from blpl.core import quarantine

    sign_in(client)
    client.post("/api/projects/init", json={"name": "scratch"})
    proj = main.PROJECTS_ROOT / "scratch"

    qdir = quarantine.quarantine_dir(proj)
    qdir.mkdir(parents=True, exist_ok=True)
    (qdir / "deadbeef1234-EVIL.pdf").write_bytes(b"%PDF-1.4\nheld\n%%EOF\n")

    r = client.get(
        "/api/projects/scratch/blob",
        params={"path": f"{quarantine.QUARANTINE_DIRNAME}/deadbeef1234-EVIL.pdf"},
    )
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/octet-stream"
    assert "attachment" in r.headers["content-disposition"]
    assert r.headers["x-content-type-options"] == "nosniff"


def test_a_released_datasheet_still_opens_in_the_browser(client) -> None:
    """The restriction is on unverified files, not on datasheets. A passed one
    is meant to be read."""
    import app.main as main

    sign_in(client)
    client.post("/api/projects/init", json={"name": "scratch"})
    proj = main.PROJECTS_ROOT / "scratch"

    sheets = proj / "datasheets"
    sheets.mkdir(parents=True, exist_ok=True)
    (sheets / "TPS62840.pdf").write_bytes(b"%PDF-1.4\nfine\n%%EOF\n")

    r = client.get("/api/projects/scratch/blob", params={"path": "datasheets/TPS62840.pdf"})
    assert r.status_code == 200
    assert "attachment" not in r.headers.get("content-disposition", "")


def test_a_server_key_error_says_what_it_received(monkeypatch) -> None:
    """The message used to state the requirement and never what arrived, so a
    variable set to the key *file's path* produced a correct-but-unhelpful
    sentence and a crash loop. The shape of a rejected value leaks nothing —
    it is not a usable key — and it is usually the whole diagnosis."""
    from app import serverkey

    with pytest.raises(serverkey.ServerKeyError) as path_like:
        serverkey._decode("data/server.key")
    said = str(path_like.value)
    assert "looks like a file path" in said
    # Not "leave it unset": `load` only ever reads $BLPL_DATA_ROOT/server.key,
    # and `load_or_create` mints a fresh key when that file is absent — so the
    # obvious-sounding advice would orphan whatever the referenced key sealed.
    assert "unset" in said and "unreadable" in said
    assert "$BLPL_DATA_ROOT/server.key" in said

    with pytest.raises(serverkey.ServerKeyError) as short:
        serverkey._decode("deadbeef")
    assert "8 characters" in str(short.value)


def test_a_hex_server_key_is_accepted() -> None:
    """64 hex characters are also valid base64, which decodes to 48 bytes and
    would be rejected if the decoder stopped at the first thing that parsed."""
    from app import serverkey

    assert len(serverkey._decode("ab" * 32)) == 32
    assert len(serverkey._decode(serverkey.encode(serverkey.generate()))) == 32


def test_unsetting_a_key_that_is_elsewhere_would_mint_a_new_one(tmp_path) -> None:
    """The reason the diagnostic no longer says "leave it unset".

    An operator whose key lives at /run/secrets/server.key, told to unset the
    variable, gets a brand-new key — and everything the old one sealed becomes
    unreadable. This is that sequence, so the advice cannot drift back.
    """
    from app import serverkey

    elsewhere = tmp_path / "secrets" / "server.key"
    elsewhere.parent.mkdir(parents=True)
    real = serverkey.generate()
    elsewhere.write_text(serverkey.encode(real) + "\n")

    data_root = tmp_path / "data"
    data_root.mkdir()
    assert serverkey.load(data_root) is None          # it does not look there
    minted = serverkey.load_or_create(data_root)      # …it makes a new one
    assert minted != real


def test_settings_survive_a_round_trip(unlocked) -> None:
    """A GET whose result cannot be PUT back unchanged is the bug.

    The screen used to receive every task with fallbacks already applied, then
    echo the whole map back on the next click. That turned a legal
    "datasheet_vision is unset" into an illegal "datasheet_vision routes to a
    blind endpoint", and validation refused it — including refusing the very
    edit that would have fixed it. Nothing in the routing tab could be changed.
    """
    unlocked.put("/api/settings/llm", json={"endpoints": [
        {"name": "ollama", "kind": "ollama", "model": "qwen", "vision": False},
        {"name": "opus", "kind": "anthropic", "vision": True},
    ]})
    unlocked.put("/api/settings/llm", json={"tasks": {"default": ["ollama"]}})

    got = unlocked.get("/api/settings").json()
    # Stored routes are what was stored; inherited ones are not invented into it.
    assert got["tasks"] == {"default": ["ollama"]}
    assert got["effective"]["datasheet_vision"] == ["ollama"]   # …but shown
    assert any("datasheet_vision" in w for w in got["warnings"])

    # The round trip that used to deadlock.
    back = unlocked.put("/api/settings/llm", json={"tasks": got["tasks"]})
    assert back.status_code == 200, back.text


def test_one_task_can_be_changed_without_resending_the_rest(unlocked) -> None:
    """Edits are merged, so a click on one task cannot be refused because of
    what some other task happens to hold."""
    unlocked.put("/api/settings/llm", json={"endpoints": [
        {"name": "ollama", "kind": "ollama", "model": "qwen", "vision": False},
        {"name": "opus", "kind": "anthropic", "vision": True},
    ]})
    unlocked.put("/api/settings/llm", json={"tasks": {"default": ["ollama"]}})

    r = unlocked.put("/api/settings/llm", json={"tasks": {"datasheet_vision": ["opus"]}})
    assert r.status_code == 200, r.text
    got = unlocked.get("/api/settings").json()
    assert got["tasks"]["datasheet_vision"] == ["opus"]
    assert got["tasks"]["default"] == ["ollama"]        # untouched
    assert not got["warnings"]                           # and the warning clears


def test_a_task_can_be_unset_so_it_inherits_again(unlocked) -> None:
    """An empty chain means "give this task no route of its own" — the only way
    back for a route that should never have existed."""
    unlocked.put("/api/settings/llm", json={"endpoints": [
        {"name": "opus", "kind": "anthropic", "vision": True},
    ]})
    unlocked.put("/api/settings/llm", json={"tasks": {"default": ["opus"], "chat": ["opus"]}})
    unlocked.put("/api/settings/llm", json={"tasks": {"chat": []}})

    got = unlocked.get("/api/settings").json()
    assert "chat" not in got["tasks"]
    assert got["effective"]["chat"] == ["opus"]
