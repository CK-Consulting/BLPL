"""Creating and reading project key grants.

The layer between "who is a member" (app/projectacl.py) and the crypto that
makes a key hand-off possible (app/projectkey.py).

Everything here is created on demand rather than migrated in, for one reason:
sealing a user's private key needs their master key, which exists only while
they have an unlocked session. A migration cannot arrange that, and a background
job cannot either — so the first unlocked session that needs a keypair creates
one, and the first unlocked owner who grants a project creates its key.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import projectkey, vault
from .models import Project, ProjectKeyGrant, ProjectMember, User, UserKeypair


class NoGrant(LookupError):
    """This user has no wrapped copy of that project's key.

    Distinct from "not a member": membership is the permission, a grant is the
    means. They should agree, and when they do not it is worth a specific error
    rather than a decryption failure ten frames later.
    """


class NoKeypair(LookupError):
    """This user has no keypair yet, and none can be made without their master
    key — so they are locked, and the caller should say so."""


def ensure_keypair(session: Session, user: User, master_key: bytes) -> tuple[bytes, bytes]:
    """This user's (private, public) keypair, creating it on first use.

    The private half is sealed under the master key with the user's own id as
    associated data, so a keypair row cannot be moved to another user and still
    open.
    """
    row = session.scalar(select(UserKeypair).where(UserKeypair.user_id == user.id))
    if row is not None:
        private = vault.decrypt_secret(
            master_key, _aad(user), row.private_nonce, row.private_ciphertext
        )
        return bytes.fromhex(private), row.public_key

    private, public = projectkey.new_keypair()
    nonce, ciphertext = vault.encrypt_secret(master_key, _aad(user), private.hex())
    session.add(
        UserKeypair(
            user_id=user.id,
            public_key=public,
            private_nonce=nonce,
            private_ciphertext=ciphertext,
        )
    )
    session.flush()
    return private, public


def public_key_of(session: Session, user: User) -> bytes | None:
    """Someone else's public key, if they have one.

    None means they have never had an unlocked session, so there is nowhere to
    send them a key yet — the caller has to say that rather than fail obscurely.
    """
    row = session.scalar(select(UserKeypair).where(UserKeypair.user_id == user.id))
    return row.public_key if row else None


def grant_to(session: Session, project: Project, user: User, project_key: bytes) -> None:
    """Wrap the project key for one user. Replaces any existing grant."""
    public = public_key_of(session, user)
    if public is None:
        raise NoKeypair(f"user {user.id} has no keypair yet")
    sealed = projectkey.seal_to(public, project_key)
    existing = session.scalar(
        select(ProjectKeyGrant).where(
            ProjectKeyGrant.project_id == project.id, ProjectKeyGrant.user_id == user.id
        )
    )
    if existing is None:
        session.add(
            ProjectKeyGrant(
                project_id=project.id,
                user_id=user.id,
                ephemeral_public=sealed.ephemeral_public,
                nonce=sealed.nonce,
                ciphertext=sealed.ciphertext,
            )
        )
    else:
        existing.ephemeral_public = sealed.ephemeral_public
        existing.nonce = sealed.nonce
        existing.ciphertext = sealed.ciphertext
    session.flush()


def revoke_grant(session: Session, project: Project, user_id: int) -> bool:
    """Drop someone's wrapped copy.

    This stops them reading anything sealed *from now on*. It cannot unread what
    they already had — someone who held the key may have kept it — and nothing
    here pretends otherwise. Rotating the project key is the stronger answer and
    is deliberately manual, because it means rewriting every encrypted artefact.
    """
    row = session.scalar(
        select(ProjectKeyGrant).where(
            ProjectKeyGrant.project_id == project.id, ProjectKeyGrant.user_id == user_id
        )
    )
    if row is None:
        return False
    session.delete(row)
    session.flush()
    return True


def project_key_for(session: Session, project: Project, user: User, master_key: bytes) -> bytes:
    """The project's key, opened with this user's private key."""
    row = session.scalar(
        select(ProjectKeyGrant).where(
            ProjectKeyGrant.project_id == project.id, ProjectKeyGrant.user_id == user.id
        )
    )
    if row is None:
        raise NoGrant(f"no grant of project {project.name!r} for user {user.id}")
    private, _ = ensure_keypair(session, user, master_key)
    return projectkey.open_sealed(
        private,
        projectkey.SealedTo(
            ephemeral_public=row.ephemeral_public, nonce=row.nonce, ciphertext=row.ciphertext
        ),
    )


def create_project_key(session: Session, project: Project, owner: User, master_key: bytes) -> bytes:
    """Mint a project's key and grant it to its owner."""
    ensure_keypair(session, owner, master_key)
    key = projectkey.new_project_key()
    grant_to(session, project, owner, key)
    return key


def has_key(session: Session, project: Project) -> bool:
    return session.scalar(
        select(ProjectKeyGrant.id).where(ProjectKeyGrant.project_id == project.id).limit(1)
    ) is not None


def members_without_grants(session: Session, project: Project) -> list[int]:
    """Members holding no wrapped copy — the gap between permission and means.

    Should be empty. It will not be for projects that predate this machinery, or
    when someone was invited before they had ever unlocked and so had no public
    key to send to. Naming the gap is what lets it be closed deliberately rather
    than discovered as a decryption failure.
    """
    member_ids = set(
        session.scalars(
            select(ProjectMember.user_id).where(ProjectMember.project_id == project.id)
        )
    )
    granted = set(
        session.scalars(
            select(ProjectKeyGrant.user_id).where(ProjectKeyGrant.project_id == project.id)
        )
    )
    return sorted(member_ids - granted)


def _aad(user: User) -> str:
    """Associated data binding a sealed private key to its owner, so a row moved
    between users fails to open rather than quietly working."""
    return f"user-keypair:{user.id}"


def backfill_for_owner(session: Session, user: User, master_key: bytes) -> int:
    """Mint keys for this user's projects that have none, and grant to members.

    Projects created before this machinery existed have no key, and no migration
    could have given them one: sealing needs a master key, which exists only
    inside an unlocked session. So the work happens the next time the owner
    unlocks, which is the first moment it is possible.

    Members who have never unlocked are skipped rather than failing the batch —
    they have no public key to seal to yet, and they will be picked up the next
    time this runs. Returns how many projects were touched, for the log.
    """
    from .models import Project

    owned = session.scalars(select(Project).where(Project.owner_id == user.id))
    touched = 0
    for project in owned:
        if has_key(session, project):
            # Already keyed; only close gaps in who holds it.
            key = None
            missing = members_without_grants(session, project)
            if not missing:
                continue
            key = project_key_for(session, project, user, master_key)
        else:
            key = create_project_key(session, project, user, master_key)
            missing = members_without_grants(session, project)
        for user_id in missing:
            member = session.get(User, user_id)
            if member is None or public_key_of(session, member) is None:
                continue  # never unlocked; nowhere to send it yet
            grant_to(session, project, member, key)
        touched += 1
    return touched
