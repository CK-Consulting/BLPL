"""The agent platform: policy, approvals, the audit trail, and credentials.

What is under test is BLPL's half of the contract — the order the executor does
things in, and what it refuses. The order matters: the sandbox must rule before
a human is asked, or people are trained to click through questions that could
never have been allowed.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.agent import ToolContext, ToolExecutor, ToolSpec
from app.agent.registry import default_tools
from app.references import FilesystemSandbox, ReferenceManifest
from blpl.agent.kicad_happy import DISTRIBUTOR_CREDS, CredResolver
from blpl.core.llm_chat import ToolUseBlock


@pytest.fixture
def project(tmp_path: Path) -> Path:
    proj = tmp_path / "dev04"
    (proj / ".pipeline").mkdir(parents=True)
    (proj / "overview.md").write_text("# Board\n", encoding="utf-8")
    return proj


def _ctx(project: Path) -> ToolContext:
    return ToolContext(
        project_id="dev04",
        project_dir=project,
        sandbox=FilesystemSandbox(
            manifest=ReferenceManifest.empty(project_id="dev04", workspace_root=project)
        ),
    )


def _spec(name: str, *, approval="auto", kind="query", handler=None, **kw) -> ToolSpec:
    async def default_handler(ctx, args):
        return "ran"

    return ToolSpec(
        name=name,
        description="test tool",
        input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
        kind=kind,
        handler=handler or default_handler,
        approval=approval,
        **kw,
    )


def _run(executor, tool: str, **args):
    return asyncio.run(executor(ToolUseBlock(id=f"c-{tool}", name=tool, input=args)))


# -- approval policy ----------------------------------------------------------


def test_auto_tools_run_without_asking(project) -> None:
    asked: list = []

    async def approve(req):
        asked.append(req)
        return True

    ex = ToolExecutor([_spec("read_thing")], _ctx(project), approve=approve)
    assert _run(ex, "read_thing").content == "ran"
    assert asked == []


def test_ask_is_remembered_for_the_session_and_ask_always_is_not(project) -> None:
    asked: list[str] = []

    async def approve(req):
        asked.append(req.tool)
        return True

    ex = ToolExecutor(
        [_spec("search", approval="ask", kind="network"),
         _spec("spend", approval="ask_always", kind="dispatch")],
        _ctx(project),
        approve=approve,
    )
    _run(ex, "search")
    _run(ex, "search")
    _run(ex, "spend")
    _run(ex, "spend")
    # Asked once for the session-scoped tool, every time for the costly one:
    # approving a spend an hour ago is not consent for the next one.
    assert asked == ["search", "spend", "spend"]


def test_a_declined_call_returns_an_error_result_the_model_can_act_on(project) -> None:
    async def approve(req):
        return False

    ex = ToolExecutor([_spec("spend", approval="ask_always")], _ctx(project), approve=approve)
    res = _run(ex, "spend")
    assert res.is_error and "declined" in res.content and "Do not retry" in res.content


def test_a_tool_needing_approval_with_no_approver_is_refused_not_run(project) -> None:
    ran: list = []

    async def handler(ctx, args):
        ran.append(1)
        return "ran"

    ex = ToolExecutor([_spec("spend", approval="ask_always", handler=handler)], _ctx(project))
    res = _run(ex, "spend")
    assert res.is_error and not ran


def test_the_sandbox_rules_before_the_user_is_asked(project) -> None:
    """A call that could never be permitted must not become a dialog someone
    clicks through — every needless question makes the next one cheaper to
    wave past."""
    asked: list = []

    async def approve(req):
        asked.append(req)
        return True

    ex = ToolExecutor(
        [_spec("write_thing", approval="ask_always", kind="file_mutation",
               path_args=("path",), write_args=("path",))],
        _ctx(project),
        approve=approve,
    )
    res = _run(ex, "write_thing", path="/etc/passwd")
    assert res.is_error and "refused" in res.content
    assert asked == []


# -- failures are results, not crashes ---------------------------------------


def test_a_throwing_tool_becomes_an_error_result(project) -> None:
    async def boom(ctx, args):
        raise RuntimeError("the distributor exploded")

    ex = ToolExecutor([_spec("flaky", handler=boom)], _ctx(project))
    res = _run(ex, "flaky")
    assert res.is_error and "the distributor exploded" in res.content


def test_an_unknown_tool_is_reported(project) -> None:
    res = _run(ToolExecutor([_spec("known")], _ctx(project)), "unknown")
    assert res.is_error and "unknown tool" in res.content


def test_a_huge_result_is_elided_in_the_middle(project) -> None:
    async def huge(ctx, args):
        return "HEAD" + ("x" * 200_000) + "TAIL"

    res = _run(ToolExecutor([_spec("huge", handler=huge)], _ctx(project)), "huge")
    assert "HEAD" in res.content and "TAIL" in res.content and "elided" in res.content


# -- audit --------------------------------------------------------------------


def test_every_call_is_recorded_including_refusals(project) -> None:
    records: list[dict] = []

    async def approve(req):
        return False

    ex = ToolExecutor(
        [_spec("ok_tool"), _spec("denied", approval="ask_always")],
        _ctx(project),
        approve=approve,
        record=records.append,
    )
    _run(ex, "ok_tool")
    _run(ex, "denied")
    _run(ex, "nonexistent")

    assert [r["tool"] for r in records] == ["ok_tool", "denied", "nonexistent"]
    assert [r["status"] for r in records] == ["ok", "denied_user", "unknown_tool"]
    # Sequence numbers make a dropped audit row visible as a gap.
    assert [r["seq"] for r in records] == [1, 2, 3]


# -- credentials --------------------------------------------------------------


def test_digikey_oauth_env_names_are_remapped(monkeypatch) -> None:
    """This deployment stores DIGIKEY_OAUTH_*; every kicad-happy script reads
    DIGIKEY_CLIENT_*. Without the remap DigiKey does not fail — it reports 'no
    credentials' and the search silently returns worse data."""
    for var in ("DIGIKEY_CLIENT_ID", "DIGIKEY_CLIENT_SECRET"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("DIGIKEY_OAUTH_CLIENT_ID", "id-123")
    monkeypatch.setenv("DIGIKEY_OAUTH_CLIENT_SECRET", "secret-456")

    creds = CredResolver()
    assert creds.missing_for("digikey") == []
    env = creds.env_for("digikey")
    assert env["DIGIKEY_CLIENT_ID"] == "id-123"
    assert env["DIGIKEY_CLIENT_SECRET"] == "secret-456"


def test_a_missing_credential_is_named_not_guessed(monkeypatch) -> None:
    for var in ("DIGIKEY_CLIENT_ID", "DIGIKEY_CLIENT_SECRET",
                "DIGIKEY_OAUTH_CLIENT_ID", "DIGIKEY_OAUTH_CLIENT_SECRET"):
        monkeypatch.delenv(var, raising=False)
    creds = CredResolver()
    assert creds.missing_for("digikey") == ["DIGIKEY_CLIENT_ID", "DIGIKEY_CLIENT_SECRET"]
    # env_for returns None rather than a half-populated environment: the caller
    # must skip and say so, never "try anyway".
    assert creds.env_for("digikey") is None


def test_vault_credentials_beat_the_process_environment(monkeypatch) -> None:
    monkeypatch.setenv("ELEMENT14_API_KEY", "from-env")
    creds = CredResolver(extra={"ELEMENT14_API_KEY": "from-vault"})
    assert creds.value("ELEMENT14_API_KEY") == "from-vault"


def test_lcsc_needs_no_credentials() -> None:
    assert DISTRIBUTOR_CREDS["lcsc"] == ()
    assert CredResolver().missing_for("lcsc") == []


# -- the registry -------------------------------------------------------------


def test_costly_tools_ask_and_reads_do_not() -> None:
    by_name = {s.name: s for s in default_tools()}
    assert by_name["read_project_file"].approval == "auto"
    assert by_name["search_parts"].approval == "ask"          # network egress with your keys
    assert by_name["extract_datasheet_specs"].approval == "ask_always"  # spends money per call
    # A proposal writes nothing on its own — the accept step is the approval,
    # and asking twice for one decision trains people to click.
    assert by_name["propose_file_edit"].approval == "auto"


def test_extraction_refuses_without_a_vision_endpoint(project) -> None:
    """Routing extraction at a text-only model would read nothing and report
    success, so 'no vision endpoint' must be a refusal, not a fallback."""
    ctx = _ctx(project)
    (project / "datasheets").mkdir()
    (project / "datasheets" / "LM317.pdf").write_bytes(b"%PDF-1.4 fake")
    ex = ToolExecutor(default_tools(), ctx, approve=lambda r: _true())
    res = _run(ex, "extract_datasheet_specs", mpn="LM317")
    assert res.is_error and "vision" in res.content


async def _true():
    return True
