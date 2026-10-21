"""Synthetic exact-encoding/signing recipe. Copy into the selected Pack.

This example signs raw URL query text. Real protocols must explicitly choose
their query canonicalization, numeric policy, signed headers and key source.
Importing it never reads credentials, logs request data or opens a connection.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from decimal import Decimal


class ExactJsonEncoder:
    """An explicit example policy: finite Decimal as number; float is refused.

    A Pack must validate its request contract before choosing Decimal. This
    encoder does not infer amount/ID field types and does not decode responses.
    """

    def encode(self, data, *, method, url):
        def value(item):
            if item is None:
                return "null"
            if type(item) is bool:
                return "true" if item else "false"
            if type(item) is int:
                return str(item)
            if isinstance(item, Decimal) and item.is_finite():
                return format(item, "f")
            if isinstance(item, str):
                return json.dumps(item, ensure_ascii=False)
            if isinstance(item, dict):
                if any(not isinstance(key, str) for key in item):
                    raise ValueError("JSON object keys must be strings")
                return "{" + ",".join(value(key) + ":" + value(child) for key, child in item.items()) + "}"
            if isinstance(item, (list, tuple)):
                return "[" + ",".join(value(child) for child in item) + "]"
            raise ValueError("Unsupported JSON input; no implicit numeric conversion")

        return value(data).encode("utf-8")


def synthetic_signature_input(request):
    """Unambiguous example fields; body presence is distinct from its digest."""
    return "\n".join((request.method, request.path, request.query,
                      "absent" if request.body is None else "present",
                      hashlib.sha256(request.body if request.body is not None else b"").hexdigest())).encode("utf-8")


class SyntheticDigestSigner:
    def __init__(self, key, *, key_id="synthetic"):
        if not isinstance(key, bytes) or not key:
            raise ValueError("Supply an explicit signing key as bytes")
        if not isinstance(key_id, str) or not key_id:
            raise ValueError("Supply an explicit key identifier")
        self._key = key
        self.key_id = key_id

    def sign(self, request):
        digest = hmac.new(self._key, synthetic_signature_input(request), hashlib.sha256).hexdigest()
        return {"X-Synthetic-Key-Id": self.key_id, "X-Synthetic-Signature": digest}
