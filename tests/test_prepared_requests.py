"""Prepared-request protocol checks; synthetic adapters and mocked owned I/O only."""
from dataclasses import FrozenInstanceError
from decimal import Decimal
import hashlib
import hmac
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
import urllib.error
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "test-platform"), str(ROOT / "test-platform/common")]
import api_client as api
import execution_jobs

spec = importlib.util.spec_from_file_location("prepared_signing_recipe", ROOT / "examples/adapter_recipes/signing.py")
recipe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recipe)


class Response(io.BytesIO):
    def __init__(self, body=b'{"ok":true}', status=200, content_type="application/json"):
        super().__init__(body)
        self.status = status
        self.headers = {"Content-Type": content_type}


class Headers:
    def __init__(self):
        self.validated = []
        self.builds = []

    def build_headers(self, token=None, **options):
        self.builds.append((token, options))
        result = {"Content-Type": "application/json"}
        if options.get("with_token"):
            result["Authorization"] = "Bearer " + token
        result.update(options.get("extra") or {})
        return result

    def validate_headers(self, headers):
        self.validated.append(headers)


class RecordingTransport:
    def __init__(self):
        self.calls = []

    def request_prepared(self, request, *, timeout=30):
        self.calls.append((request, timeout))
        return 200, {"ok": True}


class RecordingSigner:
    def __init__(self):
        self.calls = []
        self.delegate = recipe.SyntheticDigestSigner(b"synthetic-test-key")

    def sign(self, request):
        self.calls.append(request)
        return self.delegate.sign(request)


class PreparedRequestTests(unittest.TestCase):
    def setUp(self):
        self.auth = Headers()
        self.transport = RecordingTransport()
        self.signer = RecordingSigner()
        self.addCleanup(patch.stopall)
        patch.object(api, "_REQUEST_AUTH", None).start()
        patch.object(api, "_RESPONSE_DECODER", None).start()
        # No test in this module can reach a remote service accidentally.
        self.open = patch.object(api.PREPARED_OPENER, "open", side_effect=lambda *a, **kw: Response()).start()
        self.legacy_open = patch.object(api.OPENER, "open", side_effect=lambda *a, **kw: Response()).start()

    def session(self, **overrides):
        options = {"base": "https://owned.invalid", "request_auth": self.auth,
                   "request_signer": self.signer, "prepared_transport": self.transport}
        options.update(overrides)
        return api.ApiSession(**options)

    def assert_signature(self, request):
        expected = hmac.new(b"synthetic-test-key", recipe.synthetic_signature_input(request), hashlib.sha256).hexdigest()
        self.assertEqual(request.headers["X-Synthetic-Signature"], expected)

    def test_absent_empty_bytes_and_explicit_json_null_stay_distinct(self):
        session = self.session()
        session.post("/write", with_token=False)
        session.post("/write", raw_body=b"", with_token=False)
        session.post("/write", api.JSON_NULL, with_token=False)
        self.assertEqual([call[0].body for call in self.transport.calls], [None, b"", b"null"])
        for request, _ in self.transport.calls:
            self.assert_signature(request)
        self.assertNotEqual(self.transport.calls[0][0].headers["X-Synthetic-Signature"],
                            self.transport.calls[1][0].headers["X-Synthetic-Signature"])

    def test_default_json_preserves_number_string_boolean_and_large_integer(self):
        data = {"id": 9007199254740993, "textId": "9007199254740993", "flag": False, "zero": 0}
        self.session().post("/write", data, with_token=False)
        decoded = json.loads(self.transport.calls[0][0].body)
        self.assertIs(type(decoded["id"]), int)
        self.assertIs(type(decoded["textId"]), str)
        self.assertIs(type(decoded["flag"]), bool)
        self.assertEqual(decoded, data)

    def test_explicit_encoder_runs_once_and_signs_exact_unicode_decimal_bytes(self):
        encoder = Mock(wraps=recipe.ExactJsonEncoder())
        data = {"label": "空 格+%", "amount": Decimal("123456789012345678.000000000000000001"), "flag": False}
        self.session(request_encoder=encoder).post("/write", data, with_token=False)
        request, _ = self.transport.calls[0]
        self.assertEqual(request.body, '{"label":"空 格+%","amount":123456789012345678.000000000000000001,"flag":false}'.encode())
        encoder.encode.assert_called_once_with(data, method="POST", url="https://owned.invalid/write")
        self.assertIs(self.signer.calls[0].body, request.body)
        self.assertEqual(data["amount"], Decimal("123456789012345678.000000000000000001"))
        self.assert_signature(request)

    def test_raw_body_bypasses_encoder_even_for_empty_body(self):
        encoder = Mock()
        self.session(request_encoder=encoder).post("/write", raw_body=b"", with_token=False)
        encoder.encode.assert_not_called()
        self.assertEqual(self.transport.calls[0][0].body, b"")

    def test_no_body_does_not_invoke_encoder_but_explicit_null_passes_none(self):
        encoder = Mock(wraps=recipe.ExactJsonEncoder())
        session = self.session(request_encoder=encoder)
        session.get("/read", with_token=False)
        encoder.encode.assert_not_called()
        session.post("/write", api.JSON_NULL, with_token=False)
        encoder.encode.assert_called_once_with(None, method="POST", url="https://owned.invalid/write")
        self.assertEqual(self.transport.calls[1][0].body, b"null")

    def test_get_signer_reads_exact_final_path_and_encoded_query(self):
        self.session().get("/read?existing=%25", params={"q": "空 格+%", "zero": 0, "many": ["one", "two"]}, with_token=False)
        request = self.transport.calls[0][0]
        self.assertEqual(request.path, "/read")
        self.assertEqual(request.query, "existing=%25&q=%E7%A9%BA+%E6%A0%BC%2B%25&zero=0&many=one&many=two")
        self.assertEqual(request.url, self.signer.calls[0].url)
        self.assertIsNone(request.body)
        self.assert_signature(request)

    def test_prepared_intent_and_headers_are_frozen_and_detached(self):
        headers = {"X-Context": "first"}
        request = api.prepare_request("get", "https://owned.invalid/read", headers, request_signer=self.signer)
        headers["X-Context"] = "later"
        self.assertEqual(request.headers["X-Context"], "first")
        with self.assertRaises(FrozenInstanceError):
            request.url = "https://other.invalid"
        with self.assertRaises(FrozenInstanceError):
            request.body = b"changed"
        with self.assertRaises(TypeError):
            request.headers["X-Context"] = "changed"
        with self.assertRaises(TypeError):
            self.signer.calls[0].headers["X-Context"] = "changed"

    def test_repr_omits_url_auth_and_body(self):
        request = api.prepare_request("POST", "https://owned.invalid/private?secret=query-value",
                                      {"Authorization": "private-auth-value"}, raw_body=b"private-body-value")
        for private in ("owned.invalid", "query-value", "private-auth-value", "private-body-value"):
            self.assertNotIn(private, repr(request))

    def test_empty_path_is_normalized_before_signing(self):
        request = api.prepare_request("get", "https://owned.invalid?x=%25", {}, request_signer=self.signer)
        self.assertEqual(request.url, "https://owned.invalid/?x=%25")
        self.assertEqual(self.signer.calls[0].path, "/")
        self.assert_signature(request)

    def test_invalid_body_types_and_conflicting_inputs_never_send(self):
        for kwargs in ({"raw_body": "text"}, {"raw_body": bytearray(b"data")}, {"data": {}, "raw_body": b"data"}):
            with self.subTest(kwargs=kwargs), self.assertRaises((ValueError, TypeError)):
                self.session().post("/write", with_token=False, **kwargs)
        self.assertFalse(self.transport.calls)
        self.assertFalse(self.signer.calls)

    def test_encoder_must_return_bytes_and_default_does_not_coerce_decimal(self):
        for value in (None, "json", bytearray(b"json")):
            encoder = Mock()
            encoder.encode.return_value = value
            with self.subTest(value=value), self.assertRaises(TypeError):
                self.session(request_encoder=encoder).post("/write", {"ok": True}, with_token=False)
        with self.assertRaises(TypeError):
            self.session().post("/write", {"amount": Decimal("1.1")}, with_token=False)
        self.assertFalse(self.transport.calls)

    def test_exact_recipe_rejects_float_nonfinite_and_nonstring_object_keys(self):
        encoder = recipe.ExactJsonEncoder()
        for data in ({"amount": 0.1}, {"amount": Decimal("NaN")}, {1: "value"}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.session(request_encoder=encoder).post("/write", data, with_token=False)
        with self.assertRaises(ValueError):
            self.session().post("/write", {"value": float("inf")}, with_token=False)
        self.assertFalse(self.transport.calls)

    def test_invalid_target_or_header_is_rejected_before_signer_and_send(self):
        for url, headers in (("https://owned.invalid/a b", {}), ("https://owned.invalid/#fragment", {}),
                             ("https://owned.invalid/\nsecret", {}), ("https://user:pass@owned.invalid/", {}),
                             ("file:///tmp/example", {}), ("https://owned.invalid/", {"X-Value": "bad\r\nvalue"}),
                             ("https://owned.invalid/", {"X-A": "one", "x-a": "two"}),
                             ("https://owned.invalid/", {"Transfer-Encoding": "chunked"})):
            with self.subTest(url=url), self.assertRaises((ValueError, TypeError)):
                api.http("GET", url, headers, request_signer=self.signer, prepared_transport=self.transport)
        self.assertFalse(self.signer.calls)
        self.assertFalse(self.transport.calls)

    def test_body_length_is_validated_before_signing(self):
        request = api.prepare_request("POST", "https://owned.invalid/", {"Content-Length": "2"}, raw_body=b"{}")
        self.assertEqual(request.body, b"{}")
        with self.assertRaises(ValueError):
            api.prepare_request("POST", "https://owned.invalid/", {"Content-Length": "1"}, raw_body=b"{}", request_signer=self.signer)
        self.assertFalse(self.signer.calls)

    def test_signature_failure_or_invalid_signature_headers_never_send(self):
        signer = Mock()
        signer.sign.side_effect = RuntimeError("synthetic refusal")
        with self.assertRaisesRegex(RuntimeError, "synthetic refusal"):
            self.session(request_signer=signer).post("/write", {}, with_token=False)
        for headers in (None, {"Content-Type": "different"}, {"content-type": "different"}, {"X-Sign": "bad\nvalue"}):
            signer = Mock()
            signer.sign.return_value = headers
            with self.subTest(headers=headers), self.assertRaises((TypeError, ValueError)):
                self.session(request_signer=signer).post("/write", {}, with_token=False)
        self.assertFalse(self.transport.calls)

    def test_existing_token_auth_is_used_and_final_headers_are_validated(self):
        tokens = Mock()
        tokens.get_token.return_value = "synthetic-token"
        self.session(default_user="reader", token_provider=tokens).post("/write", {"ok": True}, extra_headers={"X-Context": "selected"})
        request = self.transport.calls[0][0]
        tokens.get_token.assert_called_once_with("reader", "")
        self.assertEqual(request.headers["Authorization"], "Bearer synthetic-token")
        self.assertEqual(request.headers["X-Context"], "selected")
        self.assertIs(self.auth.validated[0], request.headers)
        self.assertIn("Authorization", self.signer.calls[0].headers)
        self.assertIn("X-Synthetic-Signature", self.auth.validated[0])

    def test_validation_failure_after_signing_never_sends(self):
        self.auth.validate_headers = Mock(side_effect=ValueError("synthetic auth refusal"))
        with self.assertRaisesRegex(ValueError, "synthetic auth refusal"):
            self.session().post("/write", {}, with_token=False)
        self.assertEqual(len(self.signer.calls), 1)
        self.assertFalse(self.transport.calls)

    def test_prepared_request_cannot_use_legacy_transport_or_wrong_protocol(self):
        legacy = Mock()
        with self.assertRaises(ValueError):
            self.session(transport=legacy)
        with self.assertRaises(TypeError):
            api.ApiSession(base="https://owned.invalid", prepared_transport=object())
        session = api.ApiSession(base="https://owned.invalid", transport=legacy, request_auth=self.auth)
        for kwargs in ({"data": api.JSON_NULL}, {"raw_body": b""}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                session.request("POST", "/write", with_token=False, **kwargs)
        legacy.request.assert_not_called()
        self.assertFalse(self.auth.builds)

    def test_cross_origin_rejected_before_auth_or_signer(self):
        tokens = Mock()
        with self.assertRaises(ValueError):
            self.session(token_provider=tokens).get("https://other.invalid/read")
        tokens.get_token.assert_not_called()
        self.assertFalse(self.auth.builds)
        self.assertFalse(self.signer.calls)
        self.assertFalse(self.transport.calls)

    def test_invalid_prepared_target_is_rejected_before_token_lookup(self):
        tokens = Mock()
        for path in ("/invalid path", "/fragment#private", "/control\nprivate"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.session(token_provider=tokens).get(path)
        tokens.get_token.assert_not_called()
        self.assertFalse(self.auth.builds)
        self.assertFalse(self.signer.calls)

    def test_explicit_external_request_omits_auth_and_signature(self):
        tokens = Mock()
        session = self.session(token_provider=tokens)
        session.get("https://other.invalid/read", allow_external=True, token="private-token")
        request = self.transport.calls[0][0]
        self.assertEqual(request.headers, {"Content-Type": "application/json", "Connection": "close"})
        self.assertFalse(self.signer.calls)
        self.assertFalse(self.auth.builds)
        self.assertFalse(self.auth.validated)
        tokens.get_token.assert_not_called()
        with self.assertRaises(ValueError):
            session.get("https://other.invalid/read", allow_external=True, extra_headers={"X-Context": "value"})
        self.assertEqual(len(self.transport.calls), 1)

    def test_default_and_injected_transports_receive_equal_prepared_intent(self):
        session = self.session(request_encoder=recipe.ExactJsonEncoder())
        session.post("/write?q=a%2Bb", {"label": "原文"}, with_token=False)
        expected = self.transport.calls[0][0]
        default = self.session(request_encoder=recipe.ExactJsonEncoder(), prepared_transport=None)
        status, body = default.post("/write?q=a%2Bb", {"label": "原文"}, with_token=False, timeout=7)
        sent = self.open.call_args.args[0]
        self.assertEqual((status, body), (200, {"ok": True}))
        self.assertEqual(sent.get_method(), expected.method)
        self.assertEqual(sent.full_url, expected.url)
        self.assertIs(sent.data, self.signer.calls[1].body)
        self.assertEqual(sent.data, expected.body)
        self.assertEqual({key.lower(): value for key, value in sent.header_items()},
                         {key.lower(): value for key, value in expected.headers.items()})
        self.assertEqual(self.open.call_args.kwargs["timeout"], 7)
        self.legacy_open.assert_not_called()

    def test_connection_is_frozen_to_default_wire_value_before_signing(self):
        headers = {"connection": "CLOSE"}
        request = api.prepare_request("GET", "https://owned.invalid/", headers, request_signer=self.signer)
        self.assertEqual(headers, {"connection": "CLOSE"})
        self.assertEqual(self.signer.calls[0].headers["connection"], "close")
        self.assertEqual(request.headers["connection"], "close")
        self.session(prepared_transport=None).get("/read", with_token=False)
        sent = self.open.call_args.args[0]
        self.assertEqual(sent.get_header("Connection"), "close")
        self.assertEqual(self.signer.calls[1].headers["Connection"], "close")

    def test_keepalive_and_signer_connection_override_are_rejected(self):
        for value in ("keep-alive", "upgrade", "close, keep-alive"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.session().get("/read", with_token=False, extra_headers={"Connection": value})
        self.assertFalse(self.signer.calls)
        signer = Mock()
        signer.sign.return_value = {"Connection": "keep-alive"}
        with self.assertRaises(ValueError):
            self.session(request_signer=signer).get("/read", with_token=False)
        self.assertFalse(self.transport.calls)

    def test_default_transport_never_reencodes_already_prepared_bytes(self):
        data = {"label": "原文"}
        encoder = Mock()
        encoder.encode.return_value = '{"label":"原文"}'.encode()
        with patch.object(api.json, "dumps", side_effect=AssertionError("unexpected second encoding")):
            self.session(request_encoder=encoder, prepared_transport=None).post("/write", data, with_token=False)
        encoder.encode.assert_called_once()
        self.assertEqual(self.open.call_args.args[0].data, '{"label":"原文"}'.encode())

    def test_signed_default_requests_refuse_all_redirect_targets(self):
        handler = api.NoPreparedRedirect()
        for code in (301, 302, 303, 307, 308):
            for target in ("https://owned.invalid/new", "https://other.invalid/new"):
                self.assertIsNone(handler.redirect_request(None, None, code, "", {}, target))
        error = urllib.error.HTTPError("https://owned.invalid/write", 302, "Found", {"Content-Type": "application/json"}, io.BytesIO(b'{"redirect":true}'))
        self.open.side_effect = error
        result = self.session(prepared_transport=None).post("/write", {}, with_token=False)
        self.assertEqual(result, (302, {"redirect": True}))
        self.assertEqual(self.open.call_count, 1)

    def test_timeout_and_authentication_refusal_do_not_replay(self):
        self.open.side_effect = TimeoutError()
        result = self.session(prepared_transport=None).post("/write", {}, with_token=False)
        self.assertEqual(result[0], 0)
        self.assertTrue(result[1]["_transport_error"])
        self.assertEqual(self.open.call_count, 1)
        self.assertEqual(len(self.signer.calls), 1)
        self.open.reset_mock()
        self.open.side_effect = urllib.error.HTTPError("https://owned.invalid/write", 401, "Denied", {"Content-Type": "application/json"}, io.BytesIO(b'{"denied":true}'))
        result = self.session(prepared_transport=None).post("/write", {}, with_token=False)
        self.assertEqual(result, (401, {"denied": True}))
        self.assertEqual(self.open.call_count, 1)
        self.assertEqual(len(self.signer.calls), 2)

    def test_transport_failure_does_not_expose_driver_details(self):
        self.open.side_effect = urllib.error.URLError("private-signature-and-url")
        result = self.session(prepared_transport=None).post("/write", {}, with_token=False)
        self.assertEqual(result[0], 0)
        self.assertNotIn("private-signature-and-url", repr(result))
        self.assertEqual(self.open.call_count, 1)

    def test_cancellation_is_checked_after_signing_before_io(self):
        with patch.object(execution_jobs, "checkpoint", side_effect=[None, RuntimeError("synthetic cancellation")]):
            with self.assertRaisesRegex(RuntimeError, "synthetic cancellation"):
                api.http("POST", "https://owned.invalid/write", {}, data={},
                         request_signer=self.signer, prepared_transport=self.transport)
        self.assertEqual(len(self.signer.calls), 1)
        self.assertFalse(self.transport.calls)

    def test_response_decoder_applies_to_success_and_http_error(self):
        decoder = Mock()
        decoder.decode_json.side_effect = lambda text, **kw: json.loads(text, parse_float=Decimal)
        session = self.session(prepared_transport=None, response_decoder=decoder)
        self.open.side_effect = lambda *a, **kw: Response(b'{"amount":0.123456789012345678}')
        status, body = session.get("/read", with_token=False)
        self.assertEqual(status, 200)
        self.assertEqual(body["amount"], Decimal("0.123456789012345678"))
        self.open.side_effect = urllib.error.HTTPError("https://owned.invalid/read", 400, "Bad Request", {}, io.BytesIO(b'{"amount":0.123456789012345678}'))
        status, body = session.get("/read", with_token=False)
        self.assertEqual(status, 400)
        self.assertEqual(body["amount"], Decimal("0.123456789012345678"))
        self.assertEqual(decoder.decode_json.call_count, 2)
        self.assertEqual(decoder.decode_json.call_args.kwargs, {"method": "GET", "url": "https://owned.invalid/read"})

    def test_injected_transport_owns_decoding_and_preserves_return_value(self):
        decoder = Mock(side_effect=AssertionError("must not decode twice"))
        self.transport.request_prepared = Mock(return_value=(400, {"amount": "1.25"}))
        result = self.session(response_decoder=decoder).get("/read", with_token=False)
        self.assertEqual(result, (400, {"amount": "1.25"}))
        decoder.decode_json.assert_not_called()

    def test_binary_and_parse_error_behavior_remains_available(self):
        session = self.session(prepared_transport=None)
        self.open.side_effect = lambda *a, **kw: Response(b"%PDF-binary", content_type="application/pdf")
        status, body = session.get("/read", with_token=False)
        self.assertEqual(status, 200)
        self.assertTrue(body["_binary"])
        self.open.side_effect = lambda *a, **kw: Response(b"invalid", status=400, content_type="text/plain")
        status, body = session.get("/read", with_token=False)
        self.assertEqual(status, 400)
        self.assertTrue(body["_parse_error"])

    def test_bounded_attachment_preserves_bytes_and_reads_at_most_limit_plus_one(self):
        payload = b"\x00test"
        session = self.session(prepared_transport=None)
        for limit in (len(payload) - 1, len(payload), len(payload) + 1):
            with self.subTest(limit=limit):
                response = Response(payload, content_type="application/octet-stream")
                response.read = Mock(wraps=response.read)
                self.open.side_effect = lambda *args, **kwargs: response
                status, body = session.get("/attachment", max_response_bytes=limit, with_token=False)
                self.assertEqual(status, 200)
                self.assertEqual(response.read.call_args_list[0].args, (limit + 1,))
                if limit < len(payload):
                    self.assertEqual(body, {"_parse_error": True, "_response_too_large": True,
                                            "_max_bytes": limit, "_content_type": "application/octet-stream"})
                    self.assertEqual(response.read.call_count, 1)
                else:
                    self.assertIsInstance(body, api.AttachmentResponse)
                    self.assertEqual(body.content, payload)

        self.legacy_open.side_effect = lambda *args, **kwargs: Response(b"123", content_type="application/octet-stream")
        status, body = api.http("GET", "https://owned.invalid/attachment", {}, max_response_bytes=3)
        self.assertEqual((status, body.content), (200, b"123"))
        metadata = {"size": len(body.content), "sha256": hashlib.sha256(body.content).hexdigest()}
        with tempfile.TemporaryDirectory() as directory:
            store = execution_jobs.JobStore(Path(directory) / "jobs.db")
            job, _ = store.create(kind="synthetic-attachment")
            store.finish(job["id"], result={"ok": True, "metadata": metadata})
            self.assertEqual(store.get(job["id"])["result"], {"ok": True, "metadata": metadata})

    def test_bounded_response_keeps_json_errors_and_rejects_truncated_attachment(self):
        session = self.session(prepared_transport=None)
        self.open.side_effect = urllib.error.HTTPError(
            "https://owned.invalid/attachment", 422, "Invalid",
            {"Content-Type": "application/problem+json", "Content-Disposition": "attachment"},
            io.BytesIO(b'{"invalid":false,"amount":"1.00"}'),
        )
        status, body = session.get("/attachment", max_response_bytes=128, with_token=False)
        self.assertEqual((status, body), (422, {"invalid": False, "amount": "1.00"}))
        truncated = Response(b"\x00ab", content_type="application/pdf")
        truncated.length = 5
        self.open.side_effect = lambda *args, **kwargs: truncated
        status, body = session.get("/attachment", max_response_bytes=128, with_token=False)
        self.assertEqual(status, 200)
        self.assertTrue(body["_response_incomplete"])
        truncated_json = Response(b'{"ok":true}', status=422)
        truncated_json.length = 5
        self.open.side_effect = lambda *args, **kwargs: truncated_json
        status, body = session.get("/attachment", max_response_bytes=128, with_token=False)
        self.assertEqual(status, 422)
        self.assertTrue(body["_response_incomplete"])
        self.assertNotIn("ok", body)

    def test_bounded_read_requires_positive_integer_and_default_transport(self):
        session = self.session(prepared_transport=None)
        for limit in (True, 0, -1, 1.2, "5"):
            with self.subTest(limit=limit), self.assertRaisesRegex(ValueError, "正整数"):
                session.get("/attachment", max_response_bytes=limit, with_token=False)
        with self.assertRaisesRegex(ValueError, "默认 HTTP"):
            self.session().get("/attachment", max_response_bytes=5, with_token=False)
        self.open.assert_not_called()
        self.assertEqual(self.auth.builds, [])

    def test_legacy_json_none_and_transport_data_are_unchanged(self):
        api.http("POST", "https://owned.invalid/write", {}, data={"label": "原文", "flag": False})
        sent = self.legacy_open.call_args.args[0]
        self.assertEqual(sent.data, json.dumps({"label": "原文", "flag": False}).encode())
        api.http("POST", "https://owned.invalid/write", {}, data=None)
        self.assertIsNone(self.legacy_open.call_args.args[0].data)
        legacy = Mock()
        legacy.request.return_value = (200, {"from": "legacy"})
        data = {"keep": "object"}
        session = api.ApiSession(base="https://owned.invalid", request_auth=self.auth, transport=legacy)
        self.assertEqual(session.post("/write", data, with_token=False), (200, {"from": "legacy"}))
        self.assertIs(legacy.request.call_args.kwargs["data"], data)
        self.open.assert_not_called()


if __name__ == "__main__":
    unittest.main()
