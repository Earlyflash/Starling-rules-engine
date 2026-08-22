import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from starling_rules_engine.signing import load_signing_key
from starling_rules_engine.starling_client import StarlingClient
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


def _make_signing_key(tmpdir):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path = Path(tmpdir) / "signing-private.pem"
    path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return load_signing_key("11111111-1111-1111-1111-111111111111", path)


def _mock_response(status_code=200, json_body=None):
    resp = MagicMock()
    resp.ok = 200 <= status_code < 300
    resp.status_code = status_code
    resp.content = b"{}" if json_body is not None else b""
    resp.json.return_value = json_body
    resp.text = json.dumps(json_body) if json_body is not None else ""
    return resp


class TestMakeLocalPayment(unittest.TestCase):
    def test_raises_without_signing_key(self):
        client = StarlingClient("token", session=MagicMock())
        with self.assertRaises(RuntimeError):
            client.make_local_payment(
                account_uid="acc-1",
                category_uid="cat-1",
                payee_account_uid="payee-acc-1",
                amount_minor_units=500,
                currency="GBP",
                reference="REIMB test",
                external_identifier="feed-1",
            )

    def test_sends_correct_body_and_signed_headers(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            signing_key = _make_signing_key(tmpdir)
            session = MagicMock()
            session.request.return_value = _mock_response(200, {"paymentOrderUid": "order-1"})

            client = StarlingClient("token", signing_key=signing_key, session=session)
            client.make_local_payment(
                account_uid="acc-1",
                category_uid="cat-1",
                payee_account_uid="payee-acc-1",
                amount_minor_units=500,
                currency="GBP",
                reference="December Expenses Reimbursement",  # deliberately > 18 chars
                external_identifier="feed-1",
            )

            session.request.assert_called_once()
            call = session.request.call_args
            method, url = call.args
            self.assertEqual(method, "PUT")
            self.assertEqual(url, "https://api.starlingbank.com/api/v2/payments/local/account/acc-1/category/cat-1")

            sent_body = json.loads(call.kwargs["data"])
            self.assertEqual(
                sent_body,
                {
                    "externalIdentifier": "feed-1",
                    "destinationPayeeAccountUid": "payee-acc-1",
                    "reference": "December Expenses Reimbursement"[:18],
                    "amount": {"currency": "GBP", "minorUnits": 500},
                },
            )
            self.assertNotIn("payeeUid", sent_body)

            headers = call.kwargs["headers"]
            self.assertIn("Date", headers)
            self.assertIn("Digest", headers)
            self.assertIn('Signature keyid="11111111', headers["Authorization"])
            self.assertTrue(headers["Authorization"].startswith("Bearer token;Signature"))


class TestReadEndpoints(unittest.TestCase):
    def test_list_accounts_uses_plain_bearer(self):
        session = MagicMock()
        session.request.return_value = _mock_response(
            200,
            {
                "accounts": [
                    {"accountUid": "acc-1", "defaultCategory": "cat-1", "currency": "GBP", "name": "Personal"}
                ]
            },
        )
        client = StarlingClient("token", session=session)
        accounts = client.list_accounts()

        self.assertEqual(accounts[0].account_uid, "acc-1")
        headers = session.request.call_args.kwargs["headers"]
        self.assertEqual(headers["Authorization"], "Bearer token")
        self.assertNotIn("Digest", headers)


if __name__ == "__main__":
    unittest.main()
