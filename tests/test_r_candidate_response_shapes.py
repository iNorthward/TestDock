from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import gen_r_series_candidates as gen_r  # noqa: E402


def _response(schema: dict, media_type: str = "application/json") -> dict:
    return {
        "responses": {
            "200": {
                "content": {media_type: {"schema": schema}},
            },
        },
    }


def _wrapped(data_schema: dict) -> dict:
    return _response({
        "type": "object",
        "properties": {
            "success": {"type": "boolean"},
            "code": {"type": "integer"},
            "msg": {"type": "string"},
            "data": data_schema,
        },
    })


class RCandidateResponseShapeTests(unittest.TestCase):
    def test_standard_page_keeps_envelope_and_page_contracts(self):
        spec = _wrapped({
            "type": "object",
            "properties": {
                "records": {"type": "array", "items": {"type": "object"}},
                "total": {"type": "integer"},
                "current": {"type": "integer"},
                "size": {"type": "integer"},
                "pages": {"type": "integer"},
            },
        })

        response = gen_r.walk_response(spec)
        result = gen_r.derive_for_op("GET", "/demo/page", spec, {})

        self.assertEqual(response["kind"], "page")
        self.assertEqual(
            [item["kind"] for item in result["candidates"]],
            ["contract_base", "page_envelope"],
        )


    def test_unwrapped_scalar_does_not_receive_standard_envelope_candidate(self):
        spec = _response({"type": "string"})

        result = gen_r.derive_for_op("GET", "/demo/language", spec, {})

        self.assertEqual(result["respKind"], "raw_scalar")
        self.assertEqual([item["kind"] for item in result["candidates"]], ["raw_response_type"])
        self.assertEqual(result["candidates"][0]["scalarType"], "string")

    def test_explicit_binary_response_gets_file_contract(self):
        spec = _response(
            {"type": "string", "format": "binary"},
            "application/octet-stream",
        )

        result = gen_r.derive_for_op("GET", "/demo/export", spec, {})

        self.assertEqual(result["respKind"], "file")
        self.assertEqual([item["kind"] for item in result["candidates"]], ["file_response_contract"])

    def test_raw_array_candidate_carries_item_type(self):
        spec = _response({
            "type": "array",
            "items": {"type": "integer"},
        })

        result = gen_r.derive_for_op("GET", "/demo/ids", spec, {})

        self.assertEqual(result["respKind"], "raw_array")
        self.assertEqual(result["candidates"][0]["kind"], "raw_response_array")
        self.assertEqual(result["candidates"][0]["itemType"], "integer")

    def test_ambiguous_data_field_or_multiple_media_types_fail_closed(self):
        business_object = _response({
            "type": "object",
            "properties": {"data": {"type": "string"}, "label": {"type": "string"}},
        })
        mixed = {
            "responses": {"200": {"content": {
                "application/json": {"schema": {"type": "string"}},
                "application/octet-stream": {"schema": {"type": "string", "format": "binary"}},
            }}}
        }

        self.assertEqual(gen_r.walk_response(business_object)["kind"], "unresolved")
        self.assertEqual(gen_r.walk_response(mixed)["kind"], "unresolved")


if __name__ == "__main__":
    unittest.main()
