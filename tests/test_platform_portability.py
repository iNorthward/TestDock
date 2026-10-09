"""Run declared Core plus demo in a copy with no Example Project code or assets."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PlatformPortabilityTests(unittest.TestCase):
    def test_core_and_demo_run_without_sample_project_or_shared_git_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            inventory = json.loads((ROOT / "resources/platform-core-files.json").read_text())
            for relative in [*inventory, "resources/platform-core-files.json", "packs/demo_pack/pack.json"]:
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / relative, target)
            shutil.copytree(ROOT / "examples/demo_pack", root / "examples/demo_pack",
                            ignore=shutil.ignore_patterns("__pycache__"))
            code = '''
import json, socket, sys, tempfile
from pathlib import Path
root = Path.cwd()
sys.path[:0] = [str(root), str(root / "test-platform"), str(root / "scripts")]
def forbidden(*args, **kwargs):
    raise AssertionError("portable demo opened a socket")
socket.socket.connect = socket.socket.connect_ex = forbidden
import server
from check_platform_isolation import build_report
from page_filter_helpers import validate_page
assert build_report()["ok"]
assert [p["id"] for p in server.cat.PACKS] == ["demo_pack"]
result = server.cat.exec_case("DEMO-R01")
assert result["status"] == "passed", result
assert validate_page({"ok": True, "data": {"records": [{"userId": 1}]}})[0]
with tempfile.TemporaryDirectory() as directory:
    server.rh.DB_PATH = Path(directory) / "history.db"
    identifier = server.rh.record_case(result, source="portable-demo")
    assert server.rh.run_detail(identifier)["status"] == "passed"
assert not any(name.startswith(("packs.sample_project", "packs.sample_project.adapter")) for name in sys.modules)
for relative in json.loads((root / "resources/platform-core-files.json").read_text()):
    name = Path(relative).stem
    if name in sys.modules:
        assert Path(sys.modules[name].__file__).resolve().is_relative_to(root), name
print("portable demo passed")
'''
            # -S prevents inherited sitecustomize from pre-importing the parent
            # checkout. The native offline sandbox still protects the process.
            completed = subprocess.run([sys.executable, "-S", "-c", code], cwd=root,
                                       env={"PATH": os.environ["PATH"], "PLATFORM_PACKS": "demo_pack",
                                            "PLATFORM_ENV_FILE": "/dev/null", "PYTHONDONTWRITEBYTECODE": "1"},
                                       capture_output=True, text=True, timeout=30)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stdout.strip(), "portable demo passed")
