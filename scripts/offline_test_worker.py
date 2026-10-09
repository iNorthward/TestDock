"""Internal fresh-process worker; runs only module names in the supplied plan."""
import json
import os
from pathlib import Path
import sys
import unittest

from offline_runtime import install, read_context

install()
context=read_context()
plan=json.loads(Path(sys.argv[1]).read_text())
sys.path[:0]=[str(Path(context['root'])/'tests'),context['root']]
suite=unittest.defaultTestLoader.loadTestsFromNames(plan['python'])
result=unittest.TextTestRunner(verbosity=1).run(suite)
summary={'testsRun':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),'ok':result.wasSuccessful()}
Path(sys.argv[2]).write_text(json.dumps(summary))
raise SystemExit(0 if summary['ok'] else 1)
