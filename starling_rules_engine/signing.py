"""Implements the request-signing scheme Starling requires for payment
endpoints (`BearerAndSignature` in their OpenAPI spec - a plain `Bearer`
token is accepted for read-only calls like `/accounts`, `/payees`, and
feed items, but rejected on `PUT /payments/local/account/.../category/...`).

Implemented per Starling's official sample code at
https://github.com/starlingbank/api-samples/tree/master/public-api-examples/message-signing
(see in particular `StarlingApiClient.java` and `SignatureUtils.java`
there, and `InitiatePersonalAccessPayment.java` for the personal-access-
token flow this module supports). If payments start failing with a
signature-related error, re-check this module against that sample first.

The scheme, end to end:
1. Generate an RSA key pair and upload the *public* key in the Starling
   Developer Portal against your personal access token; the portal gives
   you back a `keyUid` for it. Keep the private key file local - it's as
   sensitive as the access token itself.
2. For every payment-creation request: compute a `Date` header, a
   `Digest` header (base64 SHA-512 of the exact raw request body bytes),
   and a detached signature over the string
   `(request-target): <method> <path>\\nDate: <date>\\nDigest: <digest>`
   signed with the private key (RSASSA-PKCS1-v1.5 / SHA-256, Starling's
   `rsa-sha256` algorithm name).
3. Send `Date`, `Digest`, and an `Authorization` header of the form
   `Bearer <token>;Signature keyid="<keyUid>",algorithm="rsa-sha256",
   headers="(request-target) Date Digest",signature="<base64 sig>"`.

The `Date` and `Digest` values used to build the signed text MUST be the
exact same values sent as headers, and the digest MUST be of the exact
bytes sent as the request body - any mismatch (even whitespace) fails
signature verification.
"""

from __future__ import annotations

import base64
import hashlib
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

SIGNING_ALGORITHM = "rsa-sha256"


class SigningError(RuntimeError):
    pass


@dataclass
class SigningKey:
    key_uid: str
    private_key: RSAPrivateKey


def load_signing_key(key_uid: str, private_key_path: Path) -> SigningKey:
    """Load the RSA private key used to sign payment requests.

    The key file must be an unencrypted PEM-encoded RSA private key
    (PKCS#1 or PKCS#8), unless `STARLING_SIGNING_KEY_PASSPHRASE` is set in
    the environment, in which case it's used to decrypt the key.
    """
    if not key_uid:
        raise SigningError("signing_key_uid is not set")
    if not private_key_path.exists():
        raise SigningError(f"signing_private_key_path {private_key_path} does not exist")

    passphrase = os.environ.get("STARLING_SIGNING_KEY_PASSPHRASE") or None
    data = private_key_path.read_bytes()
    try:
        private_key = serialization.load_pem_private_key(
            data, password=passphrase.encode("utf-8") if passphrase else None
        )
    except (ValueError, TypeError) as exc:
        raise SigningError(f"could not load private key from {private_key_path}: {exc}") from exc

    if not isinstance(private_key, RSAPrivateKey):
        raise SigningError(
            f"{private_key_path} is not an RSA private key - Starling also accepts ECDSA keys, "
            "but this module only implements RSA (rsa-sha256) signing"
        )
    return SigningKey(key_uid=key_uid, private_key=private_key)


@dataclass
class SignedHeaders:
    date: str
    digest: str
    authorization: str


def sign_request(*, method: str, base_url: str, path: str, body: str, access_token: str, signing_key: SigningKey) -> SignedHeaders:
    """Build the Date/Digest/Authorization headers for one payment request.

    `path` is the request path passed to `StarlingClient._request` (e.g.
    "/payments/local/account/{uid}/category/{uid}"); `base_url` is the
    client's configured API base (e.g. "https://api.starlingbank.com/api/v2")
    - its own path component ("/api/v2") is prepended, since Starling's
    signature covers the full request path, not just the part after the
    API version prefix. `body` must be the exact string that will be sent
    as the request body.
    """
    date = datetime.now(timezone.utc).isoformat()
    digest = base64.b64encode(hashlib.sha512(body.encode("utf-8")).digest()).decode("ascii")

    full_path = urlparse(base_url).path.rstrip("/") + path
    text_to_sign = f"(request-target): {method.lower()} {full_path}\nDate: {date}\nDigest: {digest}"

    signature = signing_key.private_key.sign(
        text_to_sign.encode("utf-8"),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    signature_b64 = base64.b64encode(signature).decode("ascii")

    authorization = (
        f'Bearer {access_token};Signature keyid="{signing_key.key_uid}",'
        f'algorithm="{SIGNING_ALGORITHM}",headers="(request-target) Date Digest",'
        f'signature="{signature_b64}"'
    )
    return SignedHeaders(date=date, digest=digest, authorization=authorization)
