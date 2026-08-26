"""Who exists, and which provider keys belong to them.

Two tables, and the interesting part is the ownership edge between them: a
provider key belongs to a *user*, which is the whole reason the SQLite vault had
to go. Everything else here is bookkeeping.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    """A person, as Clerk knows them.

    ``clerk_user_id`` is the identity, not the email: Clerk lets a user change
    their address, and several sign-in methods can land on one account. Keying
    on email would silently split or merge accounts when that happens — and a
    merge here means one user reading another's provider keys.

    The email is stored anyway, for display and for the operator to recognise a
    row, and refreshed on each sign-in.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Null for a *placeholder*: someone invited by email who has never signed in.
    # They exist so an invitation has something to point at, and are claimed by
    # the first Clerk account that proves control of the address. Nothing about a
    # placeholder is trusted — it holds an email and nothing else.
    clerk_user_id: Mapped[str | None] = mapped_column(
        String(255), unique=True, index=True, nullable=True
    )
    email: Mapped[str] = mapped_column(String(320), default="", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )
    # Null until onboarding is finished. The gate reads this and nothing else,
    # so "has this user set up" is one column rather than a rule spread across
    # several tables that could disagree.
    profile_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )

    keys: Mapped[list["ProviderKey"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    master_key: Mapped["UserMasterKey | None"] = relationship(
        back_populates="user", cascade="all, delete-orphan", uselist=False
    )
    credentials: Mapped[list["UserKeyCredential"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    endpoints: Mapped[list["LlmEndpoint"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    task_routes: Mapped[list["LlmTaskRoute"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    memberships: Mapped[list["ProjectMember"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    keypair: Mapped["UserKeypair | None"] = relationship(
        back_populates="user", cascade="all, delete-orphan", uselist=False
    )


class ProjectPolicy(Base):
    """What one project has declared about the shared component library.

    In the database rather than in the project, and that is the point rather
    than a convenience. Everything inside a project directory is proposable: the
    design assistant can offer an edit to any file there, and a person accepting
    a diff is not reading it as a permissions change. A consent setting a model
    can propose relaxing is not a consent setting. This sits where no tool can
    reach it, and only an explicit settings call moves it.

    Three declarations, and none has a silent default. A project is created with
    them stated, because "nobody asked, so we assumed the permissive thing" is
    the failure this exists to prevent.

    ``unique_components`` is the subtle one, and the reason the other two are not
    enough by themselves. A component nobody else holds is identifying by its
    mere presence: stripping a record of any mention of the project does not
    hide that somebody is designing with an exotic part. So the safe
    contribution is *enriching a record that already exists* — adding a footprint
    to a part several people already hold reveals nothing — and creating a new
    record is a separate act needing its own answer.
    """

    __tablename__ = "project_policy"
    __table_args__ = (
        UniqueConstraint("project_id", name="uq_project_policy_project"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("project.id", ondelete="CASCADE"))

    #: never | enrich_existing | ask
    contribute: Mapped[str] = mapped_column(String(32), default="never")
    #: ask | freely
    consume: Mapped[str] = mapped_column(String(32), default="ask")
    #: never_contribute | allow
    unique_components: Mapped[str] = mapped_column(String(32), default="never_contribute")

    #: The consent wording in force when this was agreed, by hash, and when.
    #: Consent to a policy that later changes is not consent to the new one, so
    #: the text is pinned; a changed hash means asking again rather than assuming
    #: the old answer still stands.
    consent_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    consent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ProviderKey(Base):
    """One user's API key for one configured endpoint, sealed at rest.

    Only the ciphertext and its nonce are stored — the plaintext exists in this
    process for the length of one request, on its way into a stage subprocess's
    environment. There is deliberately no column that could hold a key in the
    clear, so a stray SELECT or a pg_dump cannot leak one.

    The endpoint name is the AES-GCM associated data (see vault.encrypt_secret),
    so a ciphertext is bound to the row it belongs to: moving the bytes from one
    endpoint to another fails authentication instead of quietly handing back the
    wrong key.
    """

    __tablename__ = "provider_key"
    __table_args__ = (
        # One key per (user, endpoint). The upsert in the settings route relies
        # on this, and without it a user could accumulate silent duplicates and
        # never know which one a run actually used.
        UniqueConstraint("user_id", "endpoint", name="uq_provider_key_user_endpoint"),
        Index("ix_provider_key_user", "user_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    # The endpoint's name from blpl.toml, not a provider kind — two Anthropic
    # endpoints on different accounts are different rows, which is the whole
    # point of the endpoint registry.
    endpoint: Mapped[str] = mapped_column(String(128))
    nonce: Mapped[bytes] = mapped_column(LargeBinary(12))
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=_now
    )

    user: Mapped[User] = relationship(back_populates="keys")


class GitEndpoint(Base):
    """One user's credential for one git host, sealed at rest.

    The clone dialog used to say "auth to the remote uses the deploy's git
    credentials" — credentials that had no storage, no env var, and no way to
    exist. This is their home, and it is per-user for the same reason provider
    keys are: a shared deploy credential would mean every signed-in user pushing
    and pulling as the operator, on the operator's account.

    Modeled on ProviderKey deliberately. Only ciphertext and nonce are stored,
    sealed under the user's master key with the endpoint's own name as the
    AES-GCM associated data — so a ciphertext cannot be replayed into another
    endpoint or another user, and no SELECT or pg_dump can produce a plaintext
    token. The secret is the token for HTTPS or the private key for SSH; it
    exists in this process only inside a request, on its way into a git
    subprocess's environment.

    ``host`` is what a remote URL is matched against, so the clone form never
    asks "which credential" — pasting a GitHub URL finds the GitHub row. Which
    is exactly why host is unique per user: matching is the ONLY selection
    mechanism there is, and a second credential for the same host would make
    every clone, pull and push a coin toss between identities — a wrong-account
    push being the kind of failure that looks like success. Two accounts on one
    host need a selector this app does not have; until it does, the store
    refuses the second row rather than guessing.
    """

    __tablename__ = "git_endpoint"
    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_git_endpoint_user_name"),
        UniqueConstraint("user_id", "host", name="uq_git_endpoint_user_host"),
        Index("ix_git_endpoint_user", "user_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    #: User-facing label: "github", "work-gitlab". The AES-GCM associated data.
    name: Mapped[str] = mapped_column(String(64))
    #: The hostname remotes are matched against: "github.com", "git.corp.example".
    host: Mapped[str] = mapped_column(String(255))
    #: https_token | ssh_key
    method: Mapped[str] = mapped_column(String(16))
    #: The username the credential belongs to. Required for HTTPS (basic auth
    #: needs one; GitLab tokens want "oauth2", GitHub accepts anything). Unused
    #: for SSH, where the key is the identity.
    username: Mapped[str | None] = mapped_column(String(128), nullable=True)
    nonce: Mapped[bytes] = mapped_column(LargeBinary(12))
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=_now
    )


class UserMasterKey(Base):
    """One user's master key, wrapped under their passphrase.

    Only the wrapping is stored. The plaintext master key exists in this
    process's memory for the length of an unlocked session and nowhere else —
    not in this table, not on disk, and never in a response body.

    The Argon2id parameters live beside the salt because a later unlock must use
    exactly the ones the material was created with. Raising the cost for new
    users therefore does not lock out existing ones.
    """

    __tablename__ = "user_master_key"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    kdf_salt: Mapped[bytes] = mapped_column(LargeBinary(16))
    kdf_time_cost: Mapped[int] = mapped_column()
    kdf_memory_kib: Mapped[int] = mapped_column()
    kdf_parallelism: Mapped[int] = mapped_column()
    nonce: Mapped[bytes] = mapped_column(LargeBinary(12))
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    user: Mapped[User] = relationship(back_populates="master_key")


class UserKeyCredential(Base):
    """A second way to unwrap the same master key — the WebAuthn PRF slot.

    Empty today. It exists now because the alternative is a migration over live
    user data later: every row here wraps the *same* master key the passphrase
    wraps, so adding a passkey is an INSERT and losing one is a DELETE, with no
    provider key or project file re-encrypted either way.

    ``prf_salt`` is the input handed to the authenticator's PRF evaluation. It is
    not secret — it is the "which key" selector — but it must be stable, because
    a different salt produces a different secret and the wrapping stops opening.
    """

    __tablename__ = "user_key_credential"
    __table_args__ = (UniqueConstraint("credential_id", name="uq_credential_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    label: Mapped[str] = mapped_column(String(128), default="")
    # Base64url of the WebAuthn credential id.
    credential_id: Mapped[str] = mapped_column(String(512))
    # COSE public key from registration, for verifying later assertions. Not
    # what protects the master key — a forged assertion yields no PRF output, so
    # the unwrap fails regardless — but checking the signature stops this
    # endpoint being a free oracle.
    public_key: Mapped[bytes] = mapped_column(LargeBinary, default=b"")
    # The authenticator's signature counter. Zero forever on most platform
    # authenticators, which is legitimate; it only detects cloning for the ones
    # that implement it.
    sign_count: Mapped[int] = mapped_column(Integer, default=0)
    prf_salt: Mapped[bytes] = mapped_column(LargeBinary(32))
    nonce: Mapped[bytes] = mapped_column(LargeBinary(12))
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    user: Mapped[User] = relationship(back_populates="credentials")


class LlmEndpoint(Base):
    """One user's named place to send an LLM request.

    This lived in blpl.toml until it turned out to be install-wide: a second
    person finishing setup rewrote the first person's routing, and the symptom
    was someone else's provider quietly becoming your default. Keys were already
    per-user; the registry naming them was not, which is a mismatch that only
    shows up once there are two people.

    The columns mirror appconfig.Endpoint exactly, so the resolver and every
    validation rule keep working on rows loaded from here — the storage moved,
    the model did not.
    """

    __tablename__ = "llm_endpoint"
    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_llm_endpoint_user_name"),
        Index("ix_llm_endpoint_user", "user_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(128), default="")
    base_url: Mapped[str] = mapped_column(String(512), default="")
    auth: Mapped[str] = mapped_column(String(16), default="vault")
    # Null means "infer from kind" — the same tri-state appconfig.Endpoint uses,
    # because a local server's vision support genuinely cannot be guessed and a
    # wrong guess makes the datasheet extractor read nothing at all.
    vision: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=None)
    # Null means "work it out" — discovered from the server where one will say,
    # inferred from the model id otherwise. Stated here when neither is right,
    # which is the ordinary case for a self-hosted model: the same weights serve
    # 128k or 1M depending on how the server was started, and Ollama publishes
    # neither number.
    context_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    max_output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    user: Mapped[User] = relationship(back_populates="endpoints")


class LlmTaskRoute(Base):
    """Which of a user's endpoints serve one task, in fallback order.

    The chain is a JSON array rather than a row per position. Order is the whole
    content of this table — index 0 is tried first — and an array keeps that
    order as one atomic value instead of something maintained across rows, where
    a partial write would silently reorder a fallback chain.
    """

    __tablename__ = "llm_task_route"
    __table_args__ = (UniqueConstraint("user_id", "task", name="uq_llm_task_user_task"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    task: Mapped[str] = mapped_column(String(64))
    endpoints: Mapped[list] = mapped_column(JSON, default=list)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    user: Mapped[User] = relationship(back_populates="task_routes")


class Project(Base):
    """A design, and who it belongs to.

    Lived in blpl.toml until projects needed owners. The file records a name, a
    remote and a branch, and nothing about *whose* it is — which is why both
    sign-ins landed in the same project and could have edited it.

    ``name`` is globally unique because it is the directory name on disk. Two
    users cannot each have a "baseboard" until working copies are namespaced per
    user, which is the worktree work in the worker-pool phase. Until then the
    constraint is honest about what the filesystem allows.
    """

    __tablename__ = "project"
    __table_args__ = (UniqueConstraint("name", name="uq_project_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    remote: Mapped[str] = mapped_column(String(512), default="")
    branch: Mapped[str] = mapped_column(String(128), default="main")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    owner: Mapped[User] = relationship(foreign_keys=[owner_id])
    members: Mapped[list["ProjectMember"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


class ProjectMember(Base):
    """Who may see and change one project.

    The owner gets a row here too, rather than being implied by
    ``Project.owner_id`` alone. One table answers "may this person touch this
    project", so no permission check has to remember to also consider ownership
    — the case that gets forgotten exactly once and then silently allows or
    denies the wrong person.

    ``role`` distinguishes what only an owner may do: share, unshare, delete.
    Everything else a member can do, which matches "an allowlist of who has
    permission" rather than a permissions matrix nobody asked for.
    """

    __tablename__ = "project_member"
    __table_args__ = (
        UniqueConstraint("project_id", "user_id", name="uq_project_member"),
        Index("ix_project_member_user", "user_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("project.id", ondelete="CASCADE"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(16), default="member")  # owner | member
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    project: Mapped[Project] = relationship(back_populates="members")
    user: Mapped[User] = relationship(back_populates="memberships")


class ProjectInvitation(Base):
    """An offer of access that the recipient has to accept.

    Sharing used to take effect the moment the owner clicked. That is fine for a
    row in a table and wrong for anything with consequences: being added to a
    project puts it in your list, spends your provider key on its runs, and — once
    project files are encrypted — hands you material you are then responsible for.
    Opting in should be a choice, the way it is for a repository invitation.

    The recipient must already have an account. That looks like a limitation next
    to inviting any email address, and it is the encryption that makes it one
    worth having: granting access means wrapping the project key for someone, and
    the owner has to be unlocked to do it. An invitation to an address nobody
    holds would either carry no key for anyone — so acceptance would need the
    owner back, unlocked, at a moment nobody can predict — or attach to whoever
    claims that address next, which is a way to hand someone your design.

    Expiry is not tidiness. A pending invitation is a standing grant waiting to
    be taken; one forgotten for a year is a way into a project whose owner has
    long since stopped thinking about it.
    """

    __tablename__ = "project_invitation"
    __table_args__ = (
        # One live invitation per (project, invitee). Re-inviting updates the
        # existing row rather than stacking duplicates that would each have to
        # be revoked separately.
        UniqueConstraint("project_id", "invitee_id", name="uq_invitation_project_invitee"),
        Index("ix_invitation_invitee", "invitee_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("project.id", ondelete="CASCADE"))
    invitee_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    invited_by_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    # pending | accepted | declined | revoked. Kept after the fact rather than
    # deleted, so "did I already turn this down" has an answer and a declined
    # invitation is not silently re-sent on a loop.
    status: Mapped[str] = mapped_column(String(16), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    responded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    # Set only when the invitee had no account. The project key is wrapped under
    # a key derived from a secret that exists nowhere but the emailed link, so
    # the invitee can take it up alone — no keypair of theirs existed to seal to,
    # and requiring the owner back later would defeat the point of inviting.
    #
    # Cleared the moment it is redeemed: the wrapped copy is re-sealed to the new
    # account's real key, and this one is destroyed so the link cannot be used
    # twice.
    key_nonce: Mapped[bytes | None] = mapped_column(LargeBinary(12), nullable=True)
    key_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)

    project: Mapped[Project] = relationship()
    invitee: Mapped[User] = relationship(foreign_keys=[invitee_id])
    invited_by: Mapped[User] = relationship(foreign_keys=[invited_by_id])


class UserKeypair(Base):
    """One user's X25519 keypair — the address others can send them a key at.

    The public half is stored in the clear, deliberately: it is what makes it
    possible to grant somebody access while they are offline. The private half is
    sealed under that user's master key, so it is readable only while they have
    an unlocked session, exactly like their provider keys.

    Symmetric crypto alone could not do this. Granting would need both people
    unlocked at the same instant — the owner to read the project key, the
    recipient to have theirs derived — which makes "invite now, accept tomorrow"
    impossible.
    """

    __tablename__ = "user_keypair"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    public_key: Mapped[bytes] = mapped_column(LargeBinary(32))
    private_nonce: Mapped[bytes] = mapped_column(LargeBinary(12))
    private_ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    user: Mapped[User] = relationship(back_populates="keypair")


class ProjectKeyGrant(Base):
    """One project's key, wrapped for one member.

    Every grant for a project wraps the *same* key — that is what makes a shared
    project readable by several people without re-encrypting anything when the
    membership changes. Adding a member is an INSERT here; removing one is a
    DELETE.

    Removing a grant does not make already-copied data unreadable, and nothing
    here pretends otherwise: someone who held the project key can have kept it.
    What it does is stop them getting the *next* version. Rotating the project
    key after a removal is the stronger answer and is deliberately not automatic,
    because it means rewriting every encrypted artefact.
    """

    __tablename__ = "project_key_grant"
    __table_args__ = (
        UniqueConstraint("project_id", "user_id", name="uq_grant_project_user"),
        Index("ix_grant_user", "user_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("project.id", ondelete="CASCADE"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    # The sealed-box ephemeral public key. Fresh per grant, so two grants of one
    # project key look unrelated.
    ephemeral_public: Mapped[bytes] = mapped_column(LargeBinary(32))
    nonce: Mapped[bytes] = mapped_column(LargeBinary(12))
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    project: Mapped[Project] = relationship()
    user: Mapped[User] = relationship()


class ProjectActivity(Base):
    """What happened to a project, and when.

    Exists because "most recently worked on" has to be answerable. The
    filesystem knows when a file changed but not who changed it or whether
    anyone merely looked; git knows about commits but not about runs, imports or
    shares. Neither can order a dashboard the way someone actually thinks about
    their own work.

    Deliberately append-only and deliberately coarse. This is a feed and a sort
    key, not an audit log — it records that a run started, not the argv, and it
    is not consulted for any permission decision. Treating it as evidence later
    would be a mistake; it is written on a best-effort basis and a failure to
    record must never fail the thing being recorded.
    """

    __tablename__ = "project_activity"
    __table_args__ = (
        # The dashboard's two questions: "what did I touch last" and "what
        # happened in this project", so both directions get an index.
        Index("ix_activity_user_time", "user_id", "created_at"),
        Index("ix_activity_project_time", "project_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("project.id", ondelete="CASCADE"))
    # Nullable: some things happen to a project without a person doing them, and
    # attributing those to whoever happened to trigger the request would be a
    # lie the feed then repeats.
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # opened | edited | ran | imported | shared | joined
    kind: Mapped[str] = mapped_column(String(24))
    detail: Mapped[str] = mapped_column(String(512), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    project: Mapped[Project] = relationship()
    user: Mapped["User | None"] = relationship()


class Run(Base):
    """One stage invocation: queued, claimed, executed, recorded.

    This was SQLite plus an in-memory fan-out, which is exactly what pinned the
    server to a single worker. A second API process could not see the first's
    runs, and an SSE reader attached to the wrong process saw nothing — so the
    fix is not "share the queue", it is to stop the API executing anything at
    all. It enqueues; workers claim and run.

    Claiming is SELECT ... FOR UPDATE SKIP LOCKED, which is a queue Postgres
    already knows how to be. Two workers racing for the same row is the normal
    case, not an error: one wins, the other skips to the next.

    Logs stay files on the shared volume. They are append-heavy streaming text,
    which a database is a poor home for, and a file can be tailed by any process
    that can see it — which removes the need for a broker entirely.
    """

    __tablename__ = "run"
    __table_args__ = (
        # The claim query orders by this and filters on status, and it runs on
        # every worker poll.
        Index("ix_run_status_created", "status", "created_at"),
        Index("ix_run_project", "project_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("project.id", ondelete="CASCADE"))
    # Who asked for it. Kept even after they leave the project, because a run
    # that happened is a fact about the past.
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(64))
    cmd: Mapped[list] = mapped_column(JSON)
    # The subprocess environment, sealed under the server key.
    #
    # It carries the user's provider API keys, which a worker cannot obtain any
    # other way: they are sealed under that user's master key, which exists only
    # in an unlocked API session. So the API resolves them while the user is
    # present and hands them to the job. Sealed rather than plain because this
    # row outlives the request, and cleared the moment the run finishes so the
    # window is the run's lifetime rather than forever.
    env_nonce: Mapped[bytes | None] = mapped_column(LargeBinary(12), nullable=True)
    env_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    # queued | running | done | cancelling | cancelled
    status: Mapped[str] = mapped_column(String(16), default="queued")
    # Which worker holds it. Only for operators reading the table — nothing
    # depends on it, because a worker that dies must not leave a row that only
    # it could have released.
    claimed_by: Mapped[str] = mapped_column(String(64), default="")
    exit_code: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Refreshed while running, so a worker that vanishes can be told apart from
    # one that is merely slow.
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    project: Mapped[Project] = relationship()
    user: Mapped["User | None"] = relationship()
