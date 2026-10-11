"""A software authenticator, for testing the passkey ceremonies for real.

WebAuthn is signature verification, and a test that stubs the verification tests
nothing — it asserts that a function which was told to return success returned
success. So this builds the actual artefacts: a CBOR attestation object, an
authenticator data structure with the right flags, and an ECDSA signature over
``authData || SHA256(clientDataJSON)``.

That is enough to prove the server checks what it claims to: swap the challenge,
the origin, the RP ID or the key and the server rejects it, which is the property
the tests here exist to pin down.

What it deliberately does *not* simulate is the PRF extension. PRF output never
crosses the authenticator boundary in a form a server can verify — it is opaque
32 bytes the browser hands over — so the tests supply it directly, exactly as a
browser would.
"""

from __future__ import annotations

import hashlib
import json
import os
import struct

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from webauthn.helpers import bytes_to_base64url

# Authenticator data flags, from the WebAuthn spec.
UP = 0x01  # user present
UV = 0x04  # user verified
AT = 0x40  # attested credential data included

AAGUID = b"\x00" * 16


class SoftwareAuthenticator:
    """One credential, on a fake security key."""

    def __init__(self, rp_id: str, credential_id: bytes | None = None):
        self.rp_id = rp_id
        self.credential_id = credential_id or os.urandom(32)
        self._key = ec.generate_private_key(ec.SECP256R1())
        self.sign_count = 0
        # What the browser would return from the PRF extension: stable per
        # credential, opaque to everyone but the authenticator.
        self.prf_output = os.urandom(32)

    # -- registration ---------------------------------------------------------

    def create(self, challenge: bytes, origin: str, uv: bool = True) -> dict:
        client_data = _client_data("webauthn.create", challenge, origin)
        auth_data = self._auth_data(flags=UP | (UV if uv else 0) | AT, attested=True)
        attestation = cbor2.dumps(
            {"fmt": "none", "attStmt": {}, "authData": auth_data}
        )
        return {
            "id": bytes_to_base64url(self.credential_id),
            "rawId": bytes_to_base64url(self.credential_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": bytes_to_base64url(client_data),
                "attestationObject": bytes_to_base64url(attestation),
                "transports": ["internal"],
            },
            "clientExtensionResults": {},
            "authenticatorAttachment": "platform",
        }

    # -- authentication -------------------------------------------------------

    def get(self, challenge: bytes, origin: str, uv: bool = True) -> dict:
        self.sign_count += 1
        client_data = _client_data("webauthn.get", challenge, origin)
        auth_data = self._auth_data(flags=UP | (UV if uv else 0), attested=False)
        signature = self._key.sign(
            auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256())
        )
        return {
            "id": bytes_to_base64url(self.credential_id),
            "rawId": bytes_to_base64url(self.credential_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": bytes_to_base64url(client_data),
                "authenticatorData": bytes_to_base64url(auth_data),
                "signature": bytes_to_base64url(signature),
                "userHandle": None,
            },
            "clientExtensionResults": {},
        }

    # -- internals ------------------------------------------------------------

    def _auth_data(self, flags: int, attested: bool) -> bytes:
        data = hashlib.sha256(self.rp_id.encode()).digest()
        data += bytes([flags])
        data += struct.pack(">I", self.sign_count)
        if attested:
            data += AAGUID
            data += struct.pack(">H", len(self.credential_id))
            data += self.credential_id
            data += self._cose_key()
        return data

    def _cose_key(self) -> bytes:
        numbers = self._key.public_key().public_numbers()
        return cbor2.dumps(
            {
                1: 2,  # kty: EC2
                3: -7,  # alg: ES256
                -1: 1,  # crv: P-256
                -2: numbers.x.to_bytes(32, "big"),
                -3: numbers.y.to_bytes(32, "big"),
            }
        )


def _client_data(ceremony: str, challenge: bytes, origin: str) -> bytes:
    return json.dumps(
        {
            "type": ceremony,
            "challenge": bytes_to_base64url(challenge),
            "origin": origin,
            "crossOrigin": False,
        }
    ).encode()
