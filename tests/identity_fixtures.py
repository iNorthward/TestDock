"""Temporary, synthetic pools for tests; never read the developer's local pool."""
import sys
import tempfile
import unittest
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "test-platform"))
import identity_pool


class IdentityPoolTestCase(unittest.TestCase):
    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        target = Path(directory.name) / "identity_pool.json"
        mocked = patch.object(identity_pool, "pool_path", return_value=target)
        mocked.start()
        self.addCleanup(mocked.stop)
        identity_pool.save_pool(identity_pool.empty_pool())

    def identity(self, category, role, **fields):
        return identity_pool.upsert_identity({
            "id": f"{category}-{role}", "category": category, "role": role,
            "username": f"{role}@example.test", "password": "SYNTHETIC_PASSWORD",
            **fields,
        })

    def scene(self, name, *, refs=None, **data):
        return identity_pool.upsert_scenario({
            "id": f"scene-{name}", "name": name,
            "identity_refs": refs or {}, "data": data,
        })

    @contextmanager
    def pool_inputs(self, values, *, clear=False):
        """Synthetic scene/identity fields plus real execution flags for tests.

        Keys use scene:<name>:<field> / identity:<category>:<role>:<field>.
        These settings enter only the temporary pool, never business env vars.
        """
        original = deepcopy(dict(identity_pool.load_pool()))
        flags = {}
        try:
            identities = {}
            scenes = {}
            for key, value in values.items():
                parts = key.split(":")
                if parts[0] == "scene" and len(parts) == 3:
                    name, field = parts[1:]
                    scenes.setdefault(name, {})[field] = value
                elif parts[0] == "identity" and len(parts) == 4:
                    category, role, field = parts[1:]
                    identities.setdefault((category,role), {})[field] = value
                else:
                    flags[key] = value
            refs = {}
            for (category,role), fields in identities.items():
                current = identity_pool.get_identity(category,role) or {
                    "id": f"{category}-{role}", "category":category,"role":role,
                    "username":f"{role}@example.test","password":"SYNTHETIC_PASSWORD"}
                current.update(fields)
                identity = identity_pool.upsert_identity(current)
                for name, alias in getattr(self,"scene_identity_refs",{}).get((category,role),[]):
                    refs.setdefault(name,{})[alias]=identity["id"]
            for name in scenes.keys() | refs.keys():
                current=identity_pool.get_scenario(name) or {"id":"scene-"+name,"name":name,"data":{},"identity_refs":{}}
                current["data"].update(scenes.get(name,{}))
                current["identity_refs"].update(refs.get(name,{}))
                identity_pool.upsert_scenario(current)
            with patch.dict("os.environ", flags, clear=clear):
                yield
        finally:
            identity_pool.save_pool(original)
