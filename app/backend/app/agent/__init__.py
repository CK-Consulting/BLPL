"""The agent platform: what tools exist, what they may do, and who says yes.

Split from app/chat.py once tools grew past reading files. The chat panel is one
*caller* of this platform; the batch lane (blpl/agent/dispatch.py under the run
manager) is another, and a KiCad bridge will be a third.
"""

from .executor import ToolExecutor
from .registry import default_tools, parts_tools, project_tools
from .toolspec import ApprovalRequest, ToolContext, ToolDenied, ToolKind, ToolSpec

__all__ = [
    "ApprovalRequest",
    "ToolContext",
    "ToolDenied",
    "ToolExecutor",
    "ToolKind",
    "ToolSpec",
    "default_tools",
    "parts_tools",
    "project_tools",
]
