#!/usr/bin/env python3
"""兼容入口：请改用 scripts/sync_agent_rules.py。"""
from __future__ import annotations

import runpy
from pathlib import Path

if __name__ == "__main__":
    runpy.run_path(str(Path(__file__).resolve().parent / "sync_agent_rules.py"), run_name="__main__")
