"""What a tool is, and what may be done with it.

A tool declaration has two audiences that must not be conflated. The model sees
a name, a description, and an input schema — enough to decide to call it. The
*executor* sees a policy: what kind of act this is, which arguments are paths
the sandbox must vet, and whether a human has to say yes first.

Keeping policy out of the model's view is deliberate. A model cannot be trusted
to declare its own call harmless, and a description that says "safe, no approval
needed" is exactly the sentence an injected instruction would write.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from blpl.agent.kicad_happy import CredResolver
from blpl.core.llm_chat import Endpoint, ToolDecl

from ..references import FilesystemSandbox

# What kind of act a tool performs. The taxonomy is borrowed from the KiCad
# assistant's tool registry, which learned it the hard way, plus an approval
# axis it lacked.
ToolKind = Literal["query", "file_read", "file_mutation", "network", "kicad_mutation", "dispatch"]

# auto       — run it, record it
# ask        — confirm once per session per tool
# ask_always — confirm every single call
Approval = Literal["auto", "ask", "ask_always"]


@dataclass
class ToolContext:
    """Everything a tool implementation is allowed to reach.

    Passing this explicitly rather than letting handlers import globals is what
    keeps a tool testable and keeps the sandbox non-optional: a handler cannot
    touch the filesystem without going through the object it was handed.
    """

    project_id: str
    project_dir: Path
    sandbox: FilesystemSandbox
    conversation: str = ""
    creds: CredResolver = field(default_factory=CredResolver)
    # task name → endpoint, for tools that themselves call a model (datasheet
    # extraction needs a vision-capable one, which is Phase 2's routing).
    endpoint_for: Callable[[str], Endpoint | None] = lambda _task: None
    # Hashes of files read this turn, so a later proposal is anchored to the
    # bytes the model actually reasoned about.
    read_shas: dict[str, str] = field(default_factory=dict)
    # Set by whoever owns the run; tools use it to report progress.
    on_progress: Callable[[str], None] | None = None
    extras: dict[str, Any] = field(default_factory=dict)

    def note(self, message: str) -> None:
        if self.on_progress:
            self.on_progress(message)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict
    kind: ToolKind
    handler: Callable[[ToolContext, dict], Awaitable[str]]
    approval: Approval = "auto"
    # Argument names holding paths. These are vetted by the sandbox *before* any
    # approval is asked: a call that could never be allowed should not become a
    # question a tired human clicks through.
    path_args: tuple[str, ...] = ()
    write_args: tuple[str, ...] = ()

    def to_decl(self) -> ToolDecl:
        return ToolDecl(name=self.name, description=self.description, input_schema=self.input_schema)


class ToolDenied(Exception):
    """The call was refused — by policy, by the sandbox, or by the user. Carries
    the reason, which goes back to the model so it can try something else."""


@dataclass
class ApprovalRequest:
    call_id: str
    tool: str
    kind: ToolKind
    args: dict
    summary: str

    def to_dict(self) -> dict:
        return {
            "call_id": self.call_id,
            "tool": self.tool,
            "kind": self.kind,
            "args": self.args,
            "summary": self.summary,
        }
