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
| Passkey unlock (§ Unlocking with a passkey) | Guessed, reused or keylogged passphrases | An operator on a live server |
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

## Unlocking with a passkey

Clerk proves who you are; a touch proves it is still you at this keyboard and
produces the key material. Two factors doing two different jobs, rather than one
prompt asked twice.

**The key does not come from the signature.** A WebAuthn assertion proves
possession and produces no secret — which is exactly why an SSO login cannot
open an encrypted vault on its own. It comes from the **PRF extension**: the
authenticator evaluates a pseudo-random function over a stored salt and returns
32 bytes, stable for that credential, that salt and this relying party, existing
nowhere until someone touches the key.

```
touch ──▶ authenticator ──▶ PRF(salt) ──HKDF──▶ KEK ──unwrap──▶ master key
```

The salt is stored in the clear. It selects which key, it is not the secret.

**Both slots open the same master key.** A passphrase and a passkey are two
doors to one key, not two keys — so adding a passkey re-encrypts nothing, losing
one locks you out of nothing, and you cannot delete your way out of your own
data. Removing your last passkey is therefore allowed.

**The PRF output crosses the wire.** The browser posts those 32 bytes to the
server, which derives the KEK and unwraps. That is the same trust model the
passphrase already has, for the same unavoidable reason: stage runs are
server-side subprocesses that need the key. What the passkey improves is real
but narrower than it looks — the secret is hardware-generated rather than
human-chosen, it is bound to this relying party so a phishing origin cannot
obtain it, and it cannot be shoulder-surfed or keylogged. What it does not
change: an operator of a running server still sees the key in memory.

Assertions are verified against a stored public key with a server-issued
challenge that is burned on use. Strictly, the AES-GCM tag is what protects the
master key — a forged assertion yields no PRF output, so the unwrap fails
regardless — but checking the signature stops the endpoint being a free oracle
and stops a captured request being replayed.

Set `BLPL_WEBAUTHN_RP_ID` and `BLPL_WEBAUTHN_ORIGIN`. Unset, the server infers
them from the request origin, which works and drops a defence-in-depth check.
**Changing the RP ID invalidates every enrolled passkey** — pick it once.

## Files retrieved from the internet

A datasheet is the only thing in a project that arrives from outside it.
Everything else is written by the people working on the board or generated from
what they wrote. Retrieved files therefore get their own directory and their own
rules.

```
project/
  retrieved/                 arrives here, untrusted
    quarantine.json          the ledger: what came in, from where, what was found
    <sha256[:12]>-<mpn>.pdf
  datasheets/                only files that have passed live here
```

The two-directory shape is the control. Code reading `datasheets/` is reading
files that were inspected; code reading `retrieved/` knows what it is touching.
Nothing has to remember a policy, because the path states it.

### Two checks, asking different questions

**What will the file do?** (`blpl/core/pdf_inspect.py`) PDF is a container
format with an action model attached: a file can carry JavaScript, name a
program to launch, submit a form to a URL, or pull in a remote document — and
`/OpenAction` and `/AA` make any of that happen *when the file is opened*, with
no click.

For a hardware programme that last part deserves stating plainly. A datasheet
that phones home on open is not only a malware risk; it tells whoever sent it
that this organisation is looking at this part, on this day. A BOM is a
competitive secret and a part list is most of one.

Two evasions are handled explicitly, because a check that misses them is
theatre:

* **Hex-escaped names.** PDF allows `#xx` for any character in a name, so
  `/JavaScript` may be written `/J#61vaScript`. Names are normalised before
  comparison.
* **Compressed object streams.** Since PDF 1.5 most structure lives in
  Flate-compressed object streams, so `b"/OpenAction" in data` is a test that a
  modern hostile file passes. Every stream that decompresses is scanned too.

One case is reported rather than papered over: an **encrypted** PDF has its
streams enciphered, so there is nothing to match. That is `cannot_inspect`, not
clean, and the file is held.

Hyperlinks (`/URI`) are recorded and *not* held against a file. Vendor
datasheets are full of them, a link is inert until clicked, and refusing them
all would refuse the entire corpus. A `/URI` reached from an `/OpenAction` is a
different matter and is caught by the `/OpenAction` finding.

**Is the file known bad?** (`blpl/core/av.py`) Optional, external, ClamAV by
default. See below.

Neither check substitutes for the other. A scanner's silence means "not a known
threat", which is not the same as "does nothing" — in testing, a PDF crafted to
submit a form on open was called clean by ClamAV and held by the inspector. The
inspector is the control that stops a datasheet phoning home; the scanner is the
control that recognises yesterday's malware.

### The invariant that makes the scanner optional

> An absent scanner reports `unscanned`. It never reports `clean`.

`clean` is a claim that something looked and found nothing. `unscanned` is an
admission that nothing looked. Collapsing them is how a control becomes
decoration: the ledger fills with ticks that mean "we did not check", and a year
later nobody remembers the difference. A configured-but-dead scanner reports
`unscanned` too, so a daemon that quietly died shows up as files nobody checked
rather than as files that passed.

### Running the scanner

ClamAV is a plain service and comes up with the stack. It used to be behind a
`scanning` profile, on the reasoning that clamd holding a ~1.5–2 GB signature
database in memory is a silly price for a laptop running tests. That reasoning
still holds for a laptop — and this is not one. A deployment that fetches
datasheets from the internet is exactly the case the scanner exists for, so
here it is part of the deployment rather than a decision.

`BLPL_QUARANTINE_REQUIRE_SCAN=1` is set on `backend` and `worker`, which holds
anything the scanner did not see. It was previously set on the `clamav`
service, where nothing reads it — so the setting was, in effect, off.

Once it is running, set `BLPL_QUARANTINE_REQUIRE_SCAN=1` on `backend` and
`worker` to hold anything the scanner did not see. Leave it off until then, or
every datasheet is held.

### What happens to a file that does not pass

It is kept, not deleted. It is evidence — which part, which distributor, what
was in it — and deleting it destroys the only record of an event worth knowing
about while guaranteeing the same download happens again tomorrow. The fetch
reports that it was held and why, rather than reporting that the part has no
datasheet, which is a different and misleading sentence.

Retrieved files are served over `/blob` as `application/octet-stream` with an
attachment disposition and `X-Content-Type-Options: nosniff`. A PDF handed to a
browser inline goes straight into a viewer, and a viewer is exactly what an
`/OpenAction` is written to talk to. Opening a held file has to be a deliberate
act. Files that passed are served normally — the restriction is on unverified
files, not on datasheets.

### What this does not cover

* **Non-PDF retrievals.** There is one inspector and it reads PDFs. Anything
  else is `cannot_inspect` and is held.
* **The renderer.** `pdftotext`, where installed, parses untrusted input. It is
  not in the backend image today, so kicad-happy falls back to scanning raw
  bytes for strings — less capable and, as it happens, less exposed.
* **Content disarm.** Nothing is rewritten. A file with active content is held
  whole rather than stripped and released, because a rewritten PDF is a new
  file whose fidelity nobody has checked.
* **Third-party scanning services.** Deliberately not used. Uploading a
  project's datasheets to a multi-scanner service would publish the part list,
  which is the thing this section is partly trying to protect.

## Known gaps

* **A stuck run blocks sealing.** A run whose worker died is reclaimed after its
  heartbeat goes stale, but while it is queued the project stays open. This fails
  in the safe direction — files intact, not encrypted — and is worth knowing.
* **Key rotation is not implemented.** Changing a passphrase re-wraps the master
  key; there is no path to re-key a project.
* **Quarantined files are sealed with the project.** A held file stays inside
  the encrypted archive. That is deliberate — it is evidence and it belongs with
  the project — but it does mean a sealed project can contain something that was
  refused.

## If a key leaks

Force-pushing a scrubbed history does **not** make GitHub delete the objects, and
a key that has been on a public wire should be considered public. Rotate it at
the provider. That is the only step that actually revokes anything.
