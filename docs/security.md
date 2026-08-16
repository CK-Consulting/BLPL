# Security posture

What BLPL protects, how, and — the part that matters more — what it does not.

This describes what is built. `docs/app-plan.md` is the design record and
predates Clerk; where the two disagree, this page is what the code does.

## The layers

Security here is a spectrum rather than a switch, and each layer is worth having
for a different reason. None of them makes the one below unnecessary.

| Layer | Protects against | Does nothing about |
|---|---|---|
| Full-volume encryption (**you**, at the OS) | A drive leaving the building | Anything while the machine is running |
| Per-user secrets (§ Provider keys) | A stolen database | An operator on a live server |
| Per-project keys (§ Sharing) | One member reading another's project | A member reading their own |
| Sealed workspaces (§ Storage at rest) | Backups, snapshots, a stolen disk | A project someone has open |

**Turn on full-volume encryption.** It is the cheapest layer, it is the only one
that covers a machine that is off, and nothing in this application substitutes
for it. LUKS on Linux, FileVault on macOS.

## Identity

Clerk is the only door. There is no local password, no passphrase login, and no
SSO path of our own — those existed and were removed, because a second way in is
a second thing to get wrong.

Session tokens are verified against Clerk's JWKS with the **issuer pinned**. That
pin is the load-bearing check: a correctly-signed token from a different Clerk
instance is still a token signed by Clerk, and without pinning it would be
accepted.

## Provider keys

Every user brings their own — their own Anthropic key, their own Ollama host,
their own OpenAI-compatible endpoint.

There is deliberately **no shared server key and no environment-variable
fallback**. An `ANTHROPIC_API_KEY` in `app/.env` is not read by the application,
by design: on a multi-user server a shared key is one person's bill and everyone
else's spending power, and the failure is silent until the invoice arrives.

Keys are sealed under the user's master key, which is derived from their
passphrase with Argon2id and never stored. A stolen database yields wrapped
blobs.

## Sharing

A project has one key. Each member holds a copy of it wrapped to their own
X25519 public key (ECDH → HKDF → AES-GCM), so granting access is re-wrapping 32
bytes for one more person rather than handing out anything shared.

Two honest limits:

* **Removing a member cannot unread what they already read.** Revoking a grant
  stops future access. It does not reach into a copy they took.
* **An invitation link is weaker than a keypair.** Someone without an account has
  no public key to wrap to, so the key is wrapped under a secret carried in the
  emailed link. That link is good for **24 hours**, and re-inviting mints a fresh
  secret rather than resending the old one.

A non-member asking for a project gets **404, not 403**. 403 would confirm the
project exists, which is enough to enumerate other people's project names one
guess at a time.

## Storage at rest

A project's files — the repository and every member's worktree — are archived
into one AES-GCM blob under the project key when nobody is in it, and restored
when someone opens it.

### Why not per-file encryption

Because **diffing must not break**. KiCad was chosen because its files are text,
which is what makes the pipeline parametric and deterministic at a fraction of
the cost of proprietary EDA tools. A git-crypt-style filter would turn every
`.kicad_sch` diff into binary noise and take that reason with it. Encrypting at
the storage boundary instead means git only ever sees plaintext: diff, blame and
merge are untouched, and they are not aware any of this happened.

### Why the whole project, worktrees included

Not a choice. A git worktree is not a copy — it holds a `.git` *file* pointing
back into the repository's object store. Sealing the project alone would delete
that store and orphan every member's checkout; sealing one member's worktree
alone would leave the repository, which holds every version of everything they
have committed, in plaintext beside it.

So a project is open when **anyone** has it open and sealed once **everybody**
has finished. Per-member sealing sounds stronger and is not available: while one
member is in there, the shared object store is on disk in the clear regardless.

### When it re-seals

* **On lock** — you said you were done. Only if nobody else is still in it.
* **On idle**, after 30 minutes untouched — the far more common case of closing
  a laptop without saying anything.
* **Never while a run is in flight.** The worker is another container reading
  those files, and sealing mid-run would delete the working copy out from under
  a stage.

Sealing happens in the API process because that is where the keys are. A worker
has no way to obtain one, which is also why a worker cannot seal.

### What it costs you to know

* A project **nobody had open** — on a stolen disk, in a backup, in a snapshot:
  inert.
* A project **someone has open**: plaintext on disk, key in the API process's
  memory. Sealing is not a sandbox.
* **The operator of a running server**: unchanged. They can read an open
  project. No amount of application-level encryption changes this, and claiming
  otherwise would be the dishonest version of this page.

It deliberately does not seal to tmpfs. Plaintext in RAM would vanish on
restart, which sounds better until it takes uncommitted work with it. A
workspace left open by a crash stays on disk and is sealed when its owner next
locks — a window, and a window is better than losing an afternoon.

### Opening is not a second check

The UI decrypts a sealed project on the way in, without a "decrypt" button. That
is not a check being skipped: a client only learns a project is sealed by asking
for it, so the intent is already established, and the key is in the session
either way. A second click would add friction and no security.

## Known gaps

* **The PRF (passkey) wrapping slot exists in the schema; the WebAuthn ceremony
  is not built.** The intent is Cloudflare-style step-up — sign in with Clerk,
  then touch a key — so the passphrase becomes the recovery path rather than the
  daily one.
* **A stuck run blocks sealing.** A run whose worker died is reclaimed after its
  heartbeat goes stale, but while it is queued the project stays open. This fails
  in the safe direction — files intact, not encrypted — and is worth knowing.
* **Key rotation is not implemented.** Changing a passphrase re-wraps the master
  key; there is no path to re-key a project.

## If a key leaks

Force-pushing a scrubbed history does **not** make GitHub delete the objects, and
a key that has been on a public wire should be considered public. Rotate it at
the provider. That is the only step that actually revokes anything.
