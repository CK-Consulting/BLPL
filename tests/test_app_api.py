"""End-to-end API tests: the session gate, first-run setup, settings, and the
project git flow, exercised through the real FastAPI app.

The ``client`` fixture (tmp state roots, cheap Argon2id, fresh module import)
lives in conftest.py — it is shared with the reference/conversation API tests.
"""

from __future__ import annotations

import json
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


def test_health_reports_a_missing_kicad_cli_instead_of_failing(client, monkeypatch, tmp_path) -> None:
    # Found by the first CI run: a runner has no KiCad, and the health check
    # answered 500 instead of saying so.
    monkeypatch.setenv("PATH", str(tmp_path))
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["kicad_cli"] is None


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
    # A parts-comparison matrix, not a net-classes table: doctor now knows the
    # net-classes table is consumed by `blpl init`, so it stopped being a valid
    # example of a discarded one.
    (proj / "design.md").write_text(
        "## Candidate parts compared\n\n"
        "| Candidate | Price | Stock |\n|---|---|---|\n| A | 1.20 | 400 |\n\n"
        "## BOM\n\n| Ref | MPN | Package |\n|---|---|---|\n| U_MCU | ESP32-S3 | Module |\n",
        encoding="utf-8",
    )
    r = unlocked.get("/api/projects/scratch/preflight")
    assert r.status_code == 200
    body = r.json()
    codes = {f["code"] for f in body["findings"]}
    # The comparison table matches neither a BOM nor a pinout, so it is silently
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
    "vision is unset" into an illegal "vision routes to a
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
    assert got["effective"]["vision"] == ["ollama"]   # …but shown
    assert any("vision" in w for w in got["warnings"])

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

    r = unlocked.put("/api/settings/llm", json={"tasks": {"vision": ["opus"]}})
    assert r.status_code == 200, r.text
    got = unlocked.get("/api/settings").json()
    assert got["tasks"]["vision"] == ["opus"]
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


def test_a_scheme_is_filled_in_rather_than_refused(client) -> None:
    """`ollama:11434`, `blpl-ollama` and a bare IP are all reasonable things to
    type in a box labelled "base URL", and every one fails as a URL because it
    has no scheme — urllib reads the host as a relative path and the request
    goes nowhere, naming neither problem."""
    from app.main import _normalise_base

    for typed in ("ollama:11434", "blpl-ollama", "172.18.0.8:11434"):
        assert _normalise_base(typed).startswith("http://"), typed
    # An explicit scheme is left alone.
    assert _normalise_base("https://api.openai.com/v1") == "https://api.openai.com/v1"
    assert _normalise_base("") == ""


def test_listing_models_needs_a_key_for_hosted_providers(unlocked) -> None:
    """Anthropic and OpenAI will not enumerate their catalogue anonymously, and
    saying so beats a 401 relayed from upstream."""
    for kind in ("anthropic", "openai"):
        r = unlocked.post("/api/settings/llm/models", json={"kind": kind})
        assert r.status_code == 400, r.text
        assert "without a key" in r.json()["detail"]


def test_an_unreachable_server_is_not_an_empty_catalogue(unlocked) -> None:
    """"No models" and "could not ask" are different answers. Collapsing them
    would show an empty dropdown for a server that simply is not running."""
    r = unlocked.post(
        "/api/settings/llm/models",
        json={"kind": "ollama", "base_url": "127.0.0.1:1"},
    )
    assert r.status_code == 502
    assert "could not reach" in r.json()["detail"]


def test_a_key_can_be_checked_before_it_is_stored(unlocked) -> None:
    """The sequence that was impossible: a hosted provider will not list models
    without a key, and a key could only be attached to an endpoint that already
    existed — so the model name had to be guessed before it could be looked up.

    A key in the body is used for the request and not stored; the 502 here is
    the upstream refusing a fake key, which proves it got that far rather than
    being turned away locally for having none.
    """
    r = unlocked.post(
        "/api/settings/llm/models",
        json={"kind": "anthropic", "api_key": "sk-ant-not-a-real-key"},
    )
    assert r.status_code == 502, r.text
    assert "could not reach" in r.json()["detail"]
    # …and nothing was written.
    assert unlocked.get("/api/settings").json()["secrets"] == []


def test_capabilities_are_reported_per_model(unlocked, monkeypatch) -> None:
    """Ollama publishes what each model can do. Asked rather than inferred: a
    `thinking` model emits chain-of-thought a caller has to strip, and one
    without `tools` will not call a tool however the prompt is written — both
    of which look like the model misbehaving if you cannot see the difference.
    """
    import app.main as main

    shown = {
        "qwen2.5vl:7b": ["completion", "vision"],
        "kicad-4b": ["completion", "thinking", "tools"],
    }

    class FakeResp:
        def __init__(self, payload): self._p = payload
        def read(self): return json.dumps(self._p).encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_open(req, timeout=0):
        body = json.loads(req.data)
        return FakeResp({"capabilities": shown[body["model"]]})

    monkeypatch.setattr(main.urllib.request, "urlopen", fake_open)
    caps = main._model_capabilities(
        "ollama", "http://ollama:11434/api/tags", list(shown), {}
    )
    assert caps["qwen2.5vl:7b"] == ["completion", "vision"]
    assert caps["kicad-4b"] == ["completion", "thinking", "tools"]


def test_a_model_that_will_not_answer_is_unknown_not_incapable(unlocked, monkeypatch) -> None:
    """The distinction this codebase keeps everywhere: silence is not a no."""
    import app.main as main

    def boom(*a, **k):
        raise OSError("unreachable")

    monkeypatch.setattr(main.urllib.request, "urlopen", boom)
    assert main._model_capabilities("ollama", "http://x/api/tags", ["m"], {}) == {}


def test_a_datasheet_is_found_when_the_file_is_not_named_after_the_part(tmp_path) -> None:
    """Vendors do not name PDFs after the orderable part number, so building
    `<MPN>.pdf` only ever worked for files the fetcher downloaded itself."""
    from blpl.agent.tools import datasheet_files as df

    sheets = tmp_path / "datasheets"
    sheets.mkdir()
    for f in ("stm32u5g9nj.pdf", "NORA-B2_DataSheet_UBXDOC-102385.pdf", "MM8108-MF15457.pdf"):
        (sheets / f).write_bytes(b"%PDF-1.4\n")

    assert df.resolve(tmp_path, "MM8108-MF15457").how == "exact"
    assert df.resolve(tmp_path, "STM32U5G9NJH6Q").path.name == "stm32u5g9nj.pdf"
    assert df.resolve(tmp_path, "NORA-B206-00B").path.name.startswith("NORA-B2_")


def test_two_revisions_of_one_part_refuse_rather_than_guess(tmp_path) -> None:
    """The failure this exists to prevent. Silently picking v1.0 over v1.1 puts
    a pinout from the wrong revision into a BOM, and nothing downstream would
    question it."""
    from blpl.agent.tools import datasheet_files as df

    sheets = tmp_path / "datasheets"
    sheets.mkdir()
    for f in ("nRF9151_datasheet_rev_v1.0.pdf", "nRF9151_datasheet_rev_v1.1.pdf"):
        (sheets / f).write_bytes(b"%PDF-1.4\n")

    got = df.resolve(tmp_path, "NRF9151-LACA-R")
    assert not got.ok
    assert len(got.candidates) == 2
    assert "Say which" in got.detail

    # Naming one is a statement, and it is honoured.
    named = df.resolve(tmp_path, "NRF9151-LACA-R", file="nRF9151_datasheet_rev_v1.1.pdf")
    assert named.ok and named.how == "explicit"


def test_a_file_sharing_nothing_with_the_mpn_needs_the_map(tmp_path) -> None:
    """A Seeed module ordered as 100058045 ships as Wio-LR2021_Module_Datasheet.
    No amount of matching bridges that; the map is the only mechanism that can."""
    from blpl.agent.tools import datasheet_files as df

    sheets = tmp_path / "datasheets"
    sheets.mkdir()
    (sheets / "Wio-LR2021_Module_Datasheet.pdf").write_bytes(b"%PDF-1.4\n")

    assert not df.resolve(tmp_path, "100058045").ok
    df.record(tmp_path, "100058045", "Wio-LR2021_Module_Datasheet.pdf")
    found = df.resolve(tmp_path, "100058045")
    assert found.ok and found.how == "map"


def test_a_hand_written_binding_is_not_overwritten(tmp_path) -> None:
    """A row a person corrected outranks anything matched automatically."""
    from blpl.agent.tools import datasheet_files as df

    sheets = tmp_path / "datasheets"
    sheets.mkdir()
    (sheets / "a.pdf").write_bytes(b"%PDF-1.4\n")
    (sheets / "b.pdf").write_bytes(b"%PDF-1.4\n")
    df.record(tmp_path, "PART-1", "a.pdf")
    df.record(tmp_path, "PART-1", "b.pdf")
    assert df.resolve(tmp_path, "PART-1").path.name == "a.pdf"


def test_a_datasheet_path_cannot_escape_the_project(tmp_path) -> None:
    """`file` comes from a model reading a user's message."""
    from blpl.agent.tools import datasheet_files as df

    (tmp_path / "datasheets").mkdir()
    (tmp_path / "secret.pdf").write_bytes(b"%PDF-1.4\n")
    assert not df.resolve(tmp_path, "X", file="../secret.pdf").ok
    assert not df.resolve(tmp_path, "X", file="/etc/passwd").ok


def test_one_datasheet_serves_a_whole_family(tmp_path) -> None:
    """The normal case, not an edge one. Manufacturers publish one document per
    product line and the orderable MPN is a row in its ordering table, so
    expecting `<MPN>.pdf` per part is a shape the world does not have."""
    from blpl.agent.tools import datasheet_files as df

    sheets = tmp_path / "datasheets"
    sheets.mkdir()
    (sheets / "nRF54L15_nRF54L10_nRF54L05_Datasheet_v1.0.pdf").write_bytes(b"%PDF-1.4\n")

    for part in ("NRF54L15-QFAA-R", "NRF54L10-QFAA-R", "NRF54L05-QFAA-R"):
        got = df.resolve(tmp_path, part)
        assert got.ok and got.how == "family", part


def test_the_datasheet_beats_the_errata_beside_it(tmp_path) -> None:
    """A vendor ships several documents per family — datasheet, errata, design
    guidelines, AT-command manual — all named for the same line. Treating them
    as equally likely made the obvious case refuse to resolve."""
    from blpl.agent.tools import datasheet_files as df

    sheets = tmp_path / "datasheets"
    sheets.mkdir()
    for f in (
        "nRF9151_Rev_2_Errata_v1.0.pdf",
        "nRF9151_datasheet_rev_v1.1.pdf",
        "nRF9151_hardware-design-guidelines.pdf",
    ):
        (sheets / f).write_bytes(b"%PDF-1.4\n")

    got = df.resolve(tmp_path, "NRF9151-LACA-R")
    assert got.ok
    assert got.path.name == "nRF9151_datasheet_rev_v1.1.pdf"
    # The others are still reported, since which documents exist is worth knowing.
    assert len(got.candidates) == 2


def test_two_revisions_of_the_same_document_still_refuse(tmp_path) -> None:
    """Ranking resolves 'which kind of document'; it must not paper over 'which
    revision', where guessing puts a stale pinout into a BOM."""
    from blpl.agent.tools import datasheet_files as df

    sheets = tmp_path / "datasheets"
    sheets.mkdir()
    for f in ("nRF9151_datasheet_rev_v1.0.pdf", "nRF9151_datasheet_rev_v1.1.pdf"):
        (sheets / f).write_bytes(b"%PDF-1.4\n")

    got = df.resolve(tmp_path, "NRF9151-LACA-R")
    assert not got.ok and len(got.candidates) == 2


def test_the_searched_prefix_walks_down_to_the_family(tmp_path) -> None:
    """The full orderable MPN is often nowhere in its own datasheet — package
    and reel suffixes live in an ordering table text extraction does not
    recover. Measured, not assumed: NRF9151-LACA-R appears in none of Nordic's
    six documents, while the stem appears in all of them."""
    from blpl.agent.tools import datasheet_files as df

    sheets = tmp_path / "datasheets"
    sheets.mkdir()
    # A PDF with uncompressed, readable text.
    pdf = sheets / "family.pdf"
    pdf.write_bytes(b"%PDF-1.4\nBT (STM32U5G9NJ family reference) Tj ET\n%%EOF\n")

    assert df.mentions(pdf, "STM32U5G9NJH6Q")      # matched on a shorter prefix
    assert not df.mentions(pdf, "NRF9151-LACA-R")
    # Too short to mean anything is not a match.
    assert not df.mentions(pdf, "ST")


def test_a_listed_model_is_the_one_you_can_actually_send(unlocked, monkeypatch) -> None:
    """The id, never the display name.

    Precedence used to be model → name → id, which is right for Ollama (whose
    `name` *is* the wire identifier) and quietly wrong for anything publishing
    both. OpenRouter gives id `google/gemini-3.7-flash` and name
    `Google: Gemini 3.7 Flash`; the label won, went into the dropdown, and was
    saved as the model — so an endpoint chosen from a list this app generated
    could never resolve on the wire.
    """
    import io
    import json as _json

    from app import main as main_mod

    body = _json.dumps({
        "data": [
            {"id": "google/gemini-3.7-flash", "name": "Google: Gemini 3.7 Flash"},
            {"id": "openrouter/auto", "name": "Auto Router"},
        ]
    }).encode()

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(main_mod.urllib.request, "urlopen", lambda *a, **k: Resp(body))
    monkeypatch.setattr(main_mod, "_model_capabilities", lambda *a: {})
    r = unlocked.post(
        "/api/settings/llm/models",
        json={"kind": "openai-compatible", "base_url": "https://openrouter.ai/api/v1"},
    )
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["models"] == ["google/gemini-3.7-flash", "openrouter/auto"]
    # The label is carried separately, so a picker can show one and store the
    # other rather than conflating them.
    assert got["labels"]["openrouter/auto"] == "Auto Router"


def test_ollama_still_lists_its_own_names(unlocked, monkeypatch) -> None:
    """Ollama has no `id`, and its `model` and `name` are the same string — so
    the reordering must not disturb it."""
    import io
    import json as _json

    from app import main as main_mod

    body = _json.dumps({
        "models": [{"name": "qwen2.5vl:7b", "model": "qwen2.5vl:7b"}]
    }).encode()

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(main_mod.urllib.request, "urlopen", lambda *a, **k: Resp(body))
    monkeypatch.setattr(main_mod, "_model_capabilities", lambda *a: {})
    r = unlocked.post("/api/settings/llm/models", json={"kind": "ollama"})
    assert r.status_code == 200, r.text
    assert r.json()["models"] == ["qwen2.5vl:7b"]
    assert r.json()["labels"] == {}


def test_a_datasheet_in_a_category_folder_resolves(tmp_path) -> None:
    """People group uploads by function, not only by part number.

    Subdirectory PDFs were excluded outright, so ten datasheets dropped into
    ``datasheets/rf-dividers-switches/`` could not be resolved at all. Placement
    is no longer a hard exclusion; the filename is what decides the match.
    """
    from blpl.agent.tools import datasheet_files as df

    sheets = tmp_path / "datasheets"
    (sheets / "rf-dividers-switches").mkdir(parents=True)
    (sheets / "rf-dividers-switches" / "Infineon_BGS12P2L6_DataSheet_v02_00_EN.pdf").write_bytes(b"%PDF")
    (sheets / "stm32u5g9nj.pdf").write_bytes(b"%PDF")

    got = df.resolve(tmp_path, "BGS12P2L6")
    assert got.ok
    assert got.path.parent.name == "rf-dividers-switches"
    # A top-level part is unaffected by the widened search.
    assert df.resolve(tmp_path, "STM32U5G9NJH6Q").path.name == "stm32u5g9nj.pdf"


def test_a_part_folder_still_wins_over_a_filename_match(tmp_path) -> None:
    """Filing a document under a part number is a statement, and outranks any
    inference drawn from a filename somewhere else in the tree."""
    from blpl.agent.tools import datasheet_files as df

    sheets = tmp_path / "datasheets"
    (sheets / "NRF9151-LACA-R").mkdir(parents=True)
    (sheets / "NRF9151-LACA-R" / "the-real-one.pdf").write_bytes(b"%PDF")
    (sheets / "misc").mkdir()
    (sheets / "misc" / "nRF9151_datasheet_rev_v1.1.pdf").write_bytes(b"%PDF")

    got = df.resolve(tmp_path, "NRF9151-LACA-R")
    assert got.how == "folder"
    assert got.path.name == "the-real-one.pdf"


def test_archived_datasheets_stay_out_of_the_running(tmp_path) -> None:
    """.archive/ is where the user puts what they took out of play."""
    from blpl.agent.tools import datasheet_files as df

    sheets = tmp_path / "datasheets"
    (sheets / ".archive").mkdir(parents=True)
    (sheets / ".archive" / "stm32u5g9nj.pdf").write_bytes(b"%PDF")

    assert not df.resolve(tmp_path, "STM32U5G9NJH6Q").ok


def test_naming_a_file_reaches_into_a_subdirectory(tmp_path) -> None:
    """`file` is the documented escape hatch; advice that does not work is worse
    than no advice."""
    from blpl.agent.tools import datasheet_files as df

    sheets = tmp_path / "datasheets"
    (sheets / "rf-dividers-switches").mkdir(parents=True)
    (sheets / "rf-dividers-switches" / "BD0926-V9.3.pdf").write_bytes(b"%PDF")

    for name in ("BD0926-V9.3.pdf", "rf-dividers-switches/BD0926-V9.3.pdf"):
        got = df.resolve(tmp_path, "BD0926", file=name)
        assert got.how == "explicit", name


def test_a_document_filed_under_one_part_is_not_offered_for_another(tmp_path) -> None:
    """Codex, PR #8. Widening the search must not undo what a folder states.

    A per-MPN folder binds its contents to that part. Once every PDF joined the
    family-prefix pass, a document filed under NRF9151AAA could be returned for
    NRF9151BBB — a different package or revision, fed to the extractor with
    nothing downstream to question it. A folder named for a function still has
    to work, so the two are told apart rather than the exclusion coming back."""
    from blpl.agent.tools import datasheet_files as df

    sheets = tmp_path / "datasheets"
    (sheets / "NRF9151AAA").mkdir(parents=True)
    (sheets / "NRF9151AAA" / "nRF9151_datasheet.pdf").write_bytes(b"%PDF")

    assert not df.resolve(tmp_path, "NRF9151BBB").ok
    # Its own part still finds it, and a category folder is unaffected.
    assert df.resolve(tmp_path, "NRF9151AAA").how == "folder"
    (sheets / "rf-switches").mkdir()
    (sheets / "rf-switches" / "Infineon_BGS12P2L6_DataSheet.pdf").write_bytes(b"%PDF")
    assert df.resolve(tmp_path, "BGS12P2L6").ok


def test_an_exact_path_settles_a_duplicate_basename(tmp_path) -> None:
    """Codex, PR #8. Answering "give the full path" to someone who just did."""
    from blpl.agent.tools import datasheet_files as df

    sheets = tmp_path / "datasheets"
    for d in ("cat-a", "cat-b"):
        (sheets / d).mkdir(parents=True)
        (sheets / d / "foo.pdf").write_bytes(b"%PDF")

    got = df.resolve(tmp_path, "PART1", file="cat-a/foo.pdf")
    assert got.how == "explicit"
    assert got.path.parent.name == "cat-a"
    # An ambiguous bare name still refuses, and says both.
    bare = df.resolve(tmp_path, "PART1", file="foo.pdf")
    assert not bare.ok
    assert len(bare.candidates) == 2


def test_a_recorded_binding_survives_a_nested_file(tmp_path) -> None:
    """Codex, PR #8. A binding written after one success must not break the next.

    `record` stored a basename, and the map branch looked only in the root, so
    extracting a datasheet from a subdirectory wrote a row that could never
    resolve again — and the map branch answers before any matching, so it
    poisoned the automatic path it was meant to shortcut."""
    from blpl.agent.tools import datasheet_files as df

    sheets = tmp_path / "datasheets"
    (sheets / "rf-switches").mkdir(parents=True)
    (sheets / "rf-switches" / "nested.pdf").write_bytes(b"%PDF")

    df.record(tmp_path, "PART1", "rf-switches/nested.pdf")
    got = df.resolve(tmp_path, "PART1")
    assert got.ok, got.detail
    assert got.how == "map"
    assert got.path.parent.name == "rf-switches"


def test_vis_is_design_work_not_other() -> None:
    """A project's architecture drawing is usually the first thing made and the
    thing most returned to. Filed under "other" it sits behind a fold with the
    scratch files."""
    pytest.importorskip("fastapi")
    from app.main import _tree_role

    assert _tree_role(Path("vis/rf-block-diagram.mmd")) == "design"
    assert _tree_role(Path("vis/rf-block-diagram.png")) == "design"
    assert _tree_role(Path("diagrams/overview.mmd")) == "design"
    # And the rules it must not disturb.
    assert _tree_role(Path("datasheets/x.pdf")) == "datasheet"
    assert _tree_role(Path("overview.md")) == "design"
    assert _tree_role(Path("arch.mmd")) == "diagram"
    assert _tree_role(Path("scratch.bin")) == "other"


def test_the_skill_table_matches_the_palette(tmp_path) -> None:
    """Codex, PR #9. The shapes are only real if the writer is told the truth.

    A `classDef` cannot set a node's shape, so the renderer cannot enforce the
    shape half of the design language — the class table in the skill is what
    does, by telling whoever writes the diagram which shape a class takes. That
    makes the table load-bearing, and a table that drifts from the palette is
    worse than no table: it is confidently wrong."""
    import json
    import re
    from pathlib import Path as P

    root = P(__file__).resolve().parent.parent
    theme = json.loads((root / "mermaid" / "themes" / "blpl-dark.json").read_text())
    skill = (root / "blpl" / "skills" / "hardware-design" / "SKILL.md").read_text()

    rows = dict(
        (m.group(1), (m.group(2).strip(), m.group(3).strip()))
        for m in re.finditer(r"^\| `([a-z]+)` \| ([^|]+) \| ([^|]+) \|", skill, re.M)
    )
    assert rows, "the class table is gone from the skill"
    for name, spec in theme["classes"].items():
        assert name in rows, f"{name} is in the palette but not in the skill's table"
        shape, hue = rows[name]
        assert shape == spec["shape"], f"{name}: skill says {shape!r}, palette says {spec['shape']!r}"
        assert hue == spec["hue"], f"{name}: skill says {hue!r}, palette says {spec['hue']!r}"
    for name in rows:
        assert name in theme["classes"], f"{name} is in the skill's table but not the palette"


def test_a_pipeline_range_carries_a_chain_for_each_stage_it_will_run(client):
    """A per-stage route has to reach the process that spends the token.

    The range resolved a single `default` chain, so a project deliberately
    routing stage0 to a cheap model and stage1 to an expensive one got the
    default for both — while running either stage on its own honoured the
    route. A setting that is stored and displayed but never consulted reads as
    configured, which is worse than not offering it.
    """
    import json as _json

    sign_in(client)
    client.put(
        "/api/settings/llm",
        json={
            "endpoints": [
                {"name": "cheap", "kind": "openai"},
                {"name": "dear", "kind": "anthropic"},
            ],
            "tasks": {
                "default": ["dear"],
                "stage0": ["cheap"],
                "stage1": ["dear"],
            },
        },
    )
    client.put("/api/settings/secrets/cheap", json={"value": "sk-cheap"})
    client.put("/api/settings/secrets/dear", json={"value": "sk-dear"})
    client.post("/api/projects/init", json={"name": "routed"})

    enqueue_only(client, "/api/projects/routed/pipeline?from_stage=stage0&to_stage=stage8")
    env = queued_env("routed")

    chains = _json.loads(env["HDM_LLM_CHAINS"])
    assert [c["endpoint"] for c in chains["stage0"]] == ["cheap"]
    assert [c["endpoint"] for c in chains["stage1"]] == ["dear"]
    # Both stages' keys travel, because both stages run in this one child.
    assert env["BLPL_LLM_KEY__CHEAP"] == "sk-cheap"
    assert env["BLPL_LLM_KEY__DEAR"] == "sk-dear"


def test_a_range_that_stops_before_stage1_carries_only_stage0s_route(client):
    import json as _json

    sign_in(client)
    client.put(
        "/api/settings/llm",
        json={
            "endpoints": [{"name": "cheap", "kind": "openai"}, {"name": "dear", "kind": "anthropic"}],
            "tasks": {"default": ["dear"], "stage0": ["cheap"], "stage1": ["dear"]},
        },
    )
    client.put("/api/settings/secrets/cheap", json={"value": "sk-cheap"})
    client.put("/api/settings/secrets/dear", json={"value": "sk-dear"})
    client.post("/api/projects/init", json={"name": "shortrange"})

    enqueue_only(client, "/api/projects/shortrange/pipeline?from_stage=stage0&to_stage=stage0")
    chains = _json.loads(queued_env("shortrange")["HDM_LLM_CHAINS"])
    assert list(chains) == ["stage0"]


def test_a_range_runs_when_only_the_stage_it_reaches_is_routable(client):
    """Previously a 400: the range asked for `default`, found nothing usable
    there, and refused a run whose stage1 was perfectly well configured."""
    import json as _json

    sign_in(client)
    client.put(
        "/api/settings/llm",
        json={
            "endpoints": [{"name": "dear", "kind": "anthropic"}],
            "tasks": {"default": [], "stage1": ["dear"]},
        },
    )
    client.put("/api/settings/secrets/dear", json={"value": "sk-dear"})
    client.post("/api/projects/init", json={"name": "onlystage1"})

    enqueue_only(client, "/api/projects/onlystage1/pipeline?from_stage=stage1&to_stage=stage8")
    chains = _json.loads(queued_env("onlystage1")["HDM_LLM_CHAINS"])
    assert [c["endpoint"] for c in chains["stage1"]] == ["dear"]


# --- opening a project in the KiCad desktop ---------------------------------
#
# The desktop autostarts a bare `kicad`, so opening it from the workbench
# landed on an empty install: every file mounted and readable, none of them
# opened. That reads as "the desktop is broken" and is the most common thing
# to conclude about it.


def _emit_board(client, name: str, board: str | None = None) -> None:
    """Put a .kicad_pro where stage 6 would have left one."""
    import app.main as main

    proj = main.PROJECTS_ROOT / name
    pipeline = proj / ".pipeline"
    pipeline.mkdir(parents=True, exist_ok=True)
    stem = f"{name}_{board}_2026-01-02_030405Z" if board else f"{name}_2026-01-02_030405Z"
    (pipeline / f"{stem}.kicad_pro").write_text("{}", encoding="utf-8")


def test_opening_a_project_writes_the_path_the_desktop_will_see(client, monkeypatch):
    import app.main as main

    sign_in(client)
    client.post("/api/projects/init", json={"name": "openme"})
    _emit_board(client, "openme")

    r = client.post("/api/projects/openme/kicad/open")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["opened"] is True
    # The path is the *container's*, not this process's — the watcher runs on
    # the other side of a bind mount and a host path would name nothing there.
    assert body["path"].startswith("/config/projects/openme/.pipeline/")
    assert body["path"].endswith(".kicad_pro")

    request = main.PROJECTS_ROOT / ".blpl-kicad-open"
    assert request.read_text(encoding="utf-8").strip() == body["path"]


def test_a_project_with_no_board_yet_is_not_an_error(client):
    """A pipeline that has not reached stage 6 has nothing to open. That is a
    fact about the project, not something the caller did wrong."""
    sign_in(client)
    client.post("/api/projects/init", json={"name": "unbuilt"})

    r = client.post("/api/projects/unbuilt/kicad/open")
    assert r.status_code == 200
    body = r.json()
    assert body["opened"] is False and "emitted" in body["reason"]


def test_a_single_project_mount_translates_to_the_root(client, monkeypatch):
    """With KICAD_DESKTOP_PROJECT naming a project, compose mounts that
    project's *contents* at /config/projects — so the same board has a
    different path in the container, and translating it with the other
    assumption would name a file that does not exist there."""
    import app.main as main

    sign_in(client)
    client.post("/api/projects/init", json={"name": "solo"})
    _emit_board(client, "solo")
    monkeypatch.setenv("KICAD_DESKTOP_PROJECT", "solo")

    body = client.post("/api/projects/solo/kicad/open").json()
    assert body["opened"] is True
    assert body["path"].startswith("/config/projects/.pipeline/"), body["path"]
    # The request file has to land at the top of what is mounted, too.
    assert (main.PROJECTS_ROOT / "solo" / ".blpl-kicad-open").is_file()


def test_a_project_the_desktop_has_not_mounted_is_refused(client, monkeypatch):
    sign_in(client)
    client.post("/api/projects/init", json={"name": "mounted"})
    client.post("/api/projects/init", json={"name": "elsewhere"})
    _emit_board(client, "elsewhere")
    monkeypatch.setenv("KICAD_DESKTOP_PROJECT", "mounted")

    body = client.post("/api/projects/elsewhere/kicad/open").json()
    assert body["opened"] is False and "not the one mounted" in body["reason"]


def test_the_board_is_matched_as_a_delimited_name_not_a_substring(client, monkeypatch):
    """`_latest_with_origin` does a plain `in` test on the filename, so a bare
    board name also matches a *different* board whose filename happens to
    contain it — a project called `core-board` matches "core" in every one of
    its files — and the reverse sort then hands back the wrong board."""
    import app.main as main

    sign_in(client)
    client.post("/api/projects/init", json={"name": "core-board"})
    # Board resolution is the manifest's job and is tested elsewhere; what is
    # under test here is the pattern this route hands to the artifact lookup.
    monkeypatch.setattr(main, "_resolve_board", lambda *a, **k: "core")
    pipeline = main.PROJECTS_ROOT / "core-board" / ".pipeline"
    pipeline.mkdir(parents=True, exist_ok=True)
    for stem in ("core-board_core_2026-01-01_000000Z", "core-board_sb-ant_2026-01-01_000000Z"):
        (pipeline / f"{stem}.kicad_pro").write_text("{}", encoding="utf-8")

    r = client.post("/api/projects/core-board/kicad/open?board=core")
    assert r.status_code == 200, f"{r.status_code}: {r.text}"
    body = r.json()
    assert body["opened"] is True
    assert body["path"].endswith("core-board_core_2026-01-01_000000Z.kicad_pro"), body["path"]


def test_a_collaborators_worktree_says_why_a_named_mount_cannot_show_it(client, monkeypatch):
    """A member who is not the owner works in a worktree beside the project.
    With the whole root mounted that is still inside the mount; with one project
    named, only the owner's checkout is there. Opening the owner's copy instead
    would show somebody else's tree and omit their edits, so it says so — and
    the reason has to be specific enough to act on."""
    import app.main as main
    from app import worktrees

    sign_in(client)
    client.post("/api/projects/init", json={"name": "shared"})

    # A board that exists only in a member's worktree.
    wt = main.PROJECTS_ROOT / worktrees.WORKTREES_DIR / "shared" / "u99" / ".pipeline"
    wt.mkdir(parents=True, exist_ok=True)
    (wt / "shared_2026-01-01_000000Z.kicad_pro").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("KICAD_DESKTOP_PROJECT", "shared")
    monkeypatch.setattr(main, "_project_dir", lambda *a, **k: wt.parent)

    body = client.post("/api/projects/shared/kicad/open").json()
    assert body["opened"] is False
    assert "worktree" in body["reason"], body["reason"]


def test_a_worktree_is_reachable_when_the_whole_root_is_mounted(client, monkeypatch):
    """The default shape mounts the projects root, and worktrees live inside
    it — so a collaborator's board translates fine and must not be refused."""
    import app.main as main
    from app import worktrees

    sign_in(client)
    client.post("/api/projects/init", json={"name": "shared2"})
    wt = main.PROJECTS_ROOT / worktrees.WORKTREES_DIR / "shared2" / "u99" / ".pipeline"
    wt.mkdir(parents=True, exist_ok=True)
    (wt / "shared2_2026-01-01_000000Z.kicad_pro").write_text("{}", encoding="utf-8")
    monkeypatch.delenv("KICAD_DESKTOP_PROJECT", raising=False)
    monkeypatch.setattr(main, "_project_dir", lambda *a, **k: wt.parent)

    body = client.post("/api/projects/shared2/kicad/open").json()
    assert body["opened"] is True
    assert "/.worktrees/shared2/u99/" in body["path"], body["path"]
