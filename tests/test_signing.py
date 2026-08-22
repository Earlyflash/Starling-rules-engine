import base64
import hashlib
import tempfile
import unittest
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from starling_rules_engine.signing import SigningError, load_signing_key, sign_request


def _write_key_pair(tmpdir) -> tuple[Path, rsa.RSAPublicKey]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path = Path(tmpdir) / "signing-private.pem"
    path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return path, private_key.public_key()


class TestLoadSigningKey(unittest.TestCase):
    def test_missing_file_raises(self):
        with self.assertRaises(SigningError):
            load_signing_key("key-uid", Path("/nonexistent/key.pem"))

    def test_missing_key_uid_raises(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path, _ = _write_key_pair(tmpdir)
            with self.assertRaises(SigningError):
                load_signing_key("", path)

    def test_loads_valid_key(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path, _ = _write_key_pair(tmpdir)
            key = load_signing_key("key-uid", path)
            self.assertEqual(key.key_uid, "key-uid")


class TestSignRequest(unittest.TestCase):
    def test_digest_matches_body_and_signature_verifies(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path, public_key = _write_key_pair(tmpdir)
            signing_key = load_signing_key("11111111-1111-1111-1111-111111111111", path)

            body = '{"externalIdentifier":"abc","amount":{"currency":"GBP","minorUnits":100}}'
            signed = sign_request(
                method="PUT",
                base_url="https://api.starlingbank.com/api/v2",
                path="/payments/local/account/acc-1/category/cat-1",
                body=body,
                access_token="test-token",
                signing_key=signing_key,
            )

            expected_digest = base64.b64encode(hashlib.sha512(body.encode("utf-8")).digest()).decode("ascii")
            self.assertEqual(signed.digest, expected_digest)

            self.assertIn('Bearer test-token;Signature keyid="11111111', signed.authorization)
            self.assertIn('algorithm="rsa-sha256"', signed.authorization)
            self.assertIn('headers="(request-target) Date Digest"', signed.authorization)

            # Extract the base64 signature and verify it against the public key,
            # reconstructing the exact text that should have been signed.
            sig_marker = 'signature="'
            start = signed.authorization.index(sig_marker) + len(sig_marker)
            signature_b64 = signed.authorization[start:-1]
            signature = base64.b64decode(signature_b64)

            text_to_sign = (
                f"(request-target): put /api/v2/payments/local/account/acc-1/category/cat-1\n"
                f"Date: {signed.date}\nDigest: {signed.digest}"
            )
            public_key.verify(
                signature,
                text_to_sign.encode("utf-8"),
                padding.PKCS1v15(),
                hashes.SHA256(),
            )  # raises InvalidSignature if it doesn't verify


if __name__ == "__main__":
    unittest.main()
