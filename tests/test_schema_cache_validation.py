from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import sync_db_schema as schema_sync  # noqa: E402


def _cache(database="test_sample_project"):
    return {
        "database": database,
        "tables": [{
            "name": "demo",
            "columns": [{"COLUMN_NAME": "id", "COLUMN_TYPE": "bigint"}],
            "indexes": [],
            "fks": [],
        }],
    }


class SchemaCacheValidationTests(unittest.TestCase):
    def test_missing_schema_configuration_fails_with_neutral_key(self):
        with patch.object(schema_sync, "SCHEMA_CACHE_FILE", None):
            with self.assertRaisesRegex(RuntimeError, "PLATFORM_SCHEMA_JSON"):
                schema_sync.load_cached_schema()

    def test_missing_cache_is_the_only_case_returning_none(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.object(schema_sync, "SCHEMA_CACHE_FILE", Path(td) / "missing.json"):
                self.assertIsNone(schema_sync.load_cached_schema())

    def test_malformed_or_incomplete_cache_fails_with_context(self):
        invalid_payloads = (
            ("[]", "根节点必须是对象"),
            (json.dumps({"tables": []}), "缺少有效 database"),
            (json.dumps({"database": "db", "tables": {}}), "tables 必须是数组"),
            (json.dumps({"database": "db", "tables": [None]}), r"tables\[0\] 必须是对象"),
            (json.dumps({"database": "db", "tables": [{"name": "t"}]}),
             r"tables\[0\]\.columns 必须是对象数组"),
        )
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "schema.json"
            with patch.object(schema_sync, "SCHEMA_CACHE_FILE", path):
                for raw in (b"{invalid", b"\xff"):
                    with self.subTest(raw=raw):
                        path.write_bytes(raw)
                        with self.assertRaisesRegex(ValueError, "读取 schema 缓存失败"):
                            schema_sync.load_cached_schema()
                for payload, message in invalid_payloads:
                    with self.subTest(payload=payload):
                        path.write_text(payload, encoding="utf-8")
                        with self.assertRaisesRegex(ValueError, message):
                            schema_sync.load_cached_schema()

    def test_valid_cache_loads_without_rewriting_it(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "schema.json"
            original = json.dumps(_cache(), ensure_ascii=False)
            path.write_text(original, encoding="utf-8")
            with patch.object(schema_sync, "SCHEMA_CACHE_FILE", path):
                loaded = schema_sync.load_cached_schema()
            self.assertEqual(loaded, json.loads(original))
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_cross_database_diff_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "拒绝生成跨数据库差异"):
            schema_sync.compare_schemas(
                {"database": "staging_db", "tables": []},
                {"database": "test_sample_project", "tables": []},
            )

    def test_progress_sync_will_not_overwrite_cache_for_another_database(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "schema.json"
            original = json.dumps(_cache(), ensure_ascii=False)
            path.write_text(original, encoding="utf-8")
            current = {"database": "staging_db", "tables": []}
            with patch.object(schema_sync, "SCHEMA_CACHE_FILE", path), \
                    patch.object(schema_sync, "fetch_current_schema_with_progress", return_value=current):
                with self.assertRaisesRegex(ValueError, "拒绝覆盖缓存"):
                    schema_sync.sync_schema_with_progress(conn=object())
            self.assertEqual(path.read_text(encoding="utf-8"), original)


if __name__ == "__main__":
    unittest.main()
