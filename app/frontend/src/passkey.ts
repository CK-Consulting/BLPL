// The browser half of the passkey ceremonies. See app/backend/app/passkeys.py
// for what the server does with the result and why the PRF output is what
// actually decrypts anything.
//
// Conversions are done by hand rather than with PublicKeyCredential
// .parseCreationOptionsFromJSON(): the server's options carry a PRF salt, and a
// parser that does not know about the extension quietly drops it — leaving a
// ceremony that succeeds, returns no PRF output, and fails at the last step with
// nothing to point at. Fifteen lines of base64url is cheaper than that bug.

import { postJSON } from "./api";

export type PasskeyInfo = { id: number; label: string; created_at: string | null };

/** Whether this browser can do WebAuthn at all. PRF support is a further
 *  question that only the authenticator can answer, and it answers it by
 *  returning a result or not — which is why enrolment checks for the output
 *  rather than asking in advance. */
export function passkeysAvailable(): boolean {
  return typeof window !== "undefined" && !!window.PublicKeyCredential;
}

export async function enrolPasskey(label: string): Promise<PasskeyInfo> {
  const options = await postJSON<any>("/api/passkeys/register/begin", { label });
  const salt = b64urlToBytes(options.extensions.prf.eval.first);

  const created = (await navigator.credentials.create({
    publicKey: {
      ...options,
      challenge: b64urlToBytes(options.challenge),
      user: { ...options.user, id: b64urlToBytes(options.user.id) },
      excludeCredentials: (options.excludeCredentials ?? []).map((c: any) => ({
        ...c,
        id: b64urlToBytes(c.id),
      })),
      extensions: { prf: { eval: { first: salt } } },
    },
  })) as PublicKeyCredential | null;
  if (!created) throw new Error("No passkey was created.");

  // Some authenticators return the PRF output from creation; others only from a
  // subsequent assertion. Both are spec-compliant, so try the cheap path and
  // fall back to asking for one more touch.
  let prf = prfResult(created);
  if (!prf) prf = await prfByAssertion(created.rawId, salt, options.rpId);
  if (!prf) {
    throw new Error(
      "This authenticator does not support the PRF extension, so it cannot " +
        "unlock your data. Try a platform passkey (Touch ID, Windows Hello) or a " +
        "recent security key.",
    );
  }

  return postJSON<PasskeyInfo>("/api/passkeys/register/finish", {
    credential: serialiseAttestation(created),
    prf_output: bytesToB64url(prf),
    label,
  });
}

export async function unlockWithPasskey(): Promise<void> {
  const options = await postJSON<any>("/api/passkeys/auth/begin");
  const assertion = (await navigator.credentials.get({
    publicKey: {
      ...options,
      challenge: b64urlToBytes(options.challenge),
      allowCredentials: (options.allowCredentials ?? []).map((c: any) => ({
        ...c,
        id: b64urlToBytes(c.id),
      })),
      // evalByCredential, because each credential has its own salt and the
      // browser picks the entry for whichever key is actually touched.
      extensions: {
        prf: {
          evalByCredential: Object.fromEntries(
            Object.entries(options.extensions.prf.evalByCredential).map(
              ([id, e]: [string, any]) => [id, { first: b64urlToBytes(e.first) }],
            ),
          ),
        },
      },
    },
  })) as PublicKeyCredential | null;
  if (!assertion) throw new Error("No passkey was used.");

  const prf = prfResult(assertion);
  if (!prf) {
    throw new Error(
      "That passkey returned no PRF secret, so it cannot unlock your data. " +
        "Remove it and add it again, or use your passphrase.",
    );
  }

  await postJSON("/api/passkeys/auth/finish", {
    credential: serialiseAssertion(assertion),
    prf_output: bytesToB64url(prf),
  });
}

/** A second ceremony purely to obtain the PRF output.
 *
 *  The challenge here is generated locally and the assertion is thrown away —
 *  it is never sent to the server, so there is nothing for a server-issued
 *  challenge to protect. What is wanted is the extension result, which only a
 *  get() can produce on these authenticators. */
async function prfByAssertion(
  rawId: ArrayBuffer,
  salt: Uint8Array<ArrayBuffer>,
  rpId: string,
): Promise<Uint8Array | null> {
  const assertion = (await navigator.credentials.get({
    publicKey: {
      challenge: crypto.getRandomValues(new Uint8Array(32)),
      rpId,
      allowCredentials: [{ type: "public-key", id: rawId }],
      userVerification: "required",
      extensions: { prf: { eval: { first: salt } } },
    },
  })) as PublicKeyCredential | null;
  return assertion ? prfResult(assertion) : null;
}

function prfResult(credential: PublicKeyCredential): Uint8Array | null {
  const results = (credential.getClientExtensionResults() as any)?.prf?.results;
  return results?.first ? new Uint8Array(results.first) : null;
}

function serialiseAttestation(credential: PublicKeyCredential) {
  const response = credential.response as AuthenticatorAttestationResponse;
  return {
    id: credential.id,
    rawId: bytesToB64url(new Uint8Array(credential.rawId)),
    type: credential.type,
    response: {
      clientDataJSON: bytesToB64url(new Uint8Array(response.clientDataJSON)),
      attestationObject: bytesToB64url(new Uint8Array(response.attestationObject)),
    },
    clientExtensionResults: {},
  };
}

function serialiseAssertion(credential: PublicKeyCredential) {
  const response = credential.response as AuthenticatorAssertionResponse;
  return {
    id: credential.id,
    rawId: bytesToB64url(new Uint8Array(credential.rawId)),
    type: credential.type,
    response: {
      clientDataJSON: bytesToB64url(new Uint8Array(response.clientDataJSON)),
      authenticatorData: bytesToB64url(new Uint8Array(response.authenticatorData)),
      signature: bytesToB64url(new Uint8Array(response.signature)),
      userHandle: response.userHandle
        ? bytesToB64url(new Uint8Array(response.userHandle))
        : null,
    },
    clientExtensionResults: {},
  };
}

// Returns Uint8Array<ArrayBuffer> rather than plain Uint8Array: a Uint8Array
// over a SharedArrayBuffer is not a BufferSource, and WebAuthn takes a
// BufferSource. Copying into a fresh buffer settles it at the type level and
// costs nothing at these sizes.
function b64urlToBytes(value: string): Uint8Array<ArrayBuffer> {
  const padded = value.replace(/-/g, "+").replace(/_/g, "/");
  const binary = atob(padded + "=".repeat((4 - (padded.length % 4)) % 4));
  const bytes = new Uint8Array(new ArrayBuffer(binary.length));
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

function bytesToB64url(bytes: Uint8Array): string {
  let binary = "";
  bytes.forEach((b) => (binary += String.fromCharCode(b)));
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}
