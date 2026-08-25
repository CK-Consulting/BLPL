"""What a project has allowed the shared component library to do, and why.

Three settings, and the reasoning behind each is worth keeping next to the code
that enforces it, because two of them look more permissive than they are.

**consume** — whether this project may use what the library holds. Asking each
time is the default. A part record is manufacturer data, so consuming one is not
a disclosure; the reason to ask is that a library record is *someone else's
reading* of a datasheet, and adopting it silently makes it indistinguishable
from one this project verified itself.

**contribute** — whether what this project gathers goes back. Never, by default.

**unique_components** — the one that carries the actual privacy property, and
the reason the first two are not sufficient on their own.

A record's contents can be anonymised. Its *existence* cannot. A part that
nobody else in the library holds is identifying by presence alone: on a
deployment with a handful of users, "somebody added an L-band SATCOM front-end
last Tuesday" is close enough to naming the project that wanted it. Removing
every mention of the project from the record does not touch that.

So the safe contribution is the one that adds nothing to the *set* of parts:
enriching a record several people already hold — giving it a footprint it
lacked — reveals nothing new, because the part's presence was already
established by somebody else. Creating a record is the act that leaks, and it
gets its own answer.

This does leave a residue, and it should be said rather than glossed: if only
one person would plausibly hold a particular footprint, contributing it to an
existing record is still a weak signal. Much weaker than creating the record —
the part is already known to be in use — but not zero. `enrich_existing` is a
large reduction in exposure, not an elimination of it.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

#: The wording a person agrees to when they allow contribution. Stored by hash
#: against the project, so that changing it here means asking again rather than
#: inheriting an answer to a different question.
CONSENT_TEXT = (
    "It is ok to contribute anonymized component information — datasheets, pinouts, "
    "footprints, symbols and similar — to the app-wide components library. No information "
    "about this project, or about how any component relates to any other component in it, "
    "is included. Other users of the app benefit from the component information I "
    "contribute, and contributing reveals nothing about this project or about me to them."
)


def consent_sha() -> str:
    """The hash of the wording currently in force."""
    return hashlib.sha256(CONSENT_TEXT.encode("utf-8")).hexdigest()


CONTRIBUTE = ("never", "enrich_existing", "ask")
CONSUME = ("ask", "freely")
UNIQUE = ("never_contribute", "allow")

DEFAULTS = {"contribute": "never", "consume": "ask", "unique_components": "never_contribute"}


class PolicyError(ValueError):
    """A setting that is not one of the values it is allowed to take."""


def validate(contribute: str, consume: str, unique_components: str) -> dict:
    """Check a declaration, or say exactly which part of it is not a choice.

    Rejected rather than coerced to the default. A caller sending
    ``contribute="yes"`` has a bug or a stale client, and quietly reading it as
    "never" would hide that while looking like it worked — or, worse, a future
    typo in the permissive direction would be silently corrected today and
    silently honoured after a rename.
    """
    for name, value, allowed in (
        ("contribute", contribute, CONTRIBUTE),
        ("consume", consume, CONSUME),
        ("unique_components", unique_components, UNIQUE),
    ):
        if value not in allowed:
            raise PolicyError(f"{name} must be one of {', '.join(allowed)} — got {value!r}")
    return {"contribute": contribute, "consume": consume, "unique_components": unique_components}


@dataclass(frozen=True)
class Decision:
    """Whether an action is allowed, and what to say when it is not."""

    allowed: bool
    #: True when the answer is "not without asking the person first".
    needs_confirmation: bool = False
    reason: str = ""

    def __bool__(self) -> bool:      # so `if decide(...)` reads correctly
        return self.allowed and not self.needs_confirmation


def may_consume(policy) -> Decision:
    if policy is None:
        return Decision(False, reason="this project has not declared a library policy")
    if policy.consume == "freely":
        return Decision(True)
    return Decision(
        True,
        needs_confirmation=True,
        reason=(
            "this project asks to confirm before using anything from the shared library"
        ),
    )


def may_contribute(policy, *, record_exists: bool) -> Decision:
    """Whether this project may put a part into the shared library.

    ``record_exists`` is the whole question for a project that allows enrichment
    only: contributing to a part somebody else already holds adds nothing to the
    set of parts, and creating one announces a part nobody else has.
    """
    if policy is None:
        return Decision(False, reason="this project has not declared a library policy")

    if not record_exists and policy.unique_components == "never_contribute":
        return Decision(
            False,
            reason=(
                "the shared library does not hold this part, and this project does not "
                "contribute components that would be unique to it — a part nobody else has "
                "is identifying by its presence, whatever the record says"
            ),
        )

    if policy.contribute == "never":
        return Decision(False, reason="this project does not contribute to the shared library")
    if policy.contribute == "enrich_existing":
        if record_exists:
            return Decision(True)
        return Decision(
            False,
            reason=(
                "this project only adds to parts the library already holds, and this one is new"
            ),
        )
    if policy.contribute == "ask":
        return Decision(
            True,
            needs_confirmation=True,
            reason="this project asks for permission before contributing anything",
        )
    return Decision(False, reason=f"unknown contribute setting {policy.contribute!r}")


def has_consented(policy) -> bool:
    """Whether the agreement on file is to the wording in force now.

    A project that consented to an earlier version has agreed to a different
    sentence, and is treated as not having agreed to this one.
    """
    return bool(policy is not None and policy.consent_sha == consent_sha())
