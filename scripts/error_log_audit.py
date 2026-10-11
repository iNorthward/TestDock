#!/usr/bin/env python3
"""Compatibility entry for a selected Pack tool capability."""
import sys
from pathlib import Path
sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parents[1] / "test-platform")]
from pack_registry import load_pack_tool_module

try:
    _implementation = load_pack_tool_module("error_log_audit")
except ValueError as exc:
    if __name__ == "__main__":
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)
    raise
if __name__ == "__main__":
    raise SystemExit(_implementation.main())
sys.modules[__name__] = _implementation
