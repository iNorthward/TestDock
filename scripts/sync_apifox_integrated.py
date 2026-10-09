#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""兼容入口：委托 scripts/run_apifox_inspection.py 执行完整巡检计划。"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSPECT = ROOT / "scripts" / "run_apifox_inspection.py"


def main():
    cmd = [sys.executable, str(INSPECT), *sys.argv[1:]]
    raise SystemExit(subprocess.call(cmd, cwd=str(ROOT)))


if __name__ == "__main__":
    main()
