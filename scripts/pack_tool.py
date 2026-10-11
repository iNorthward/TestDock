#!/usr/bin/env python3
"""Run an explicitly declared CLI tool from the selected Pack."""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'test-platform')]
from pack_registry import load_pack_tool_module


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('alias')
    parser.add_argument('arguments', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        implementation = load_pack_tool_module(args.alias)
    except ValueError as exc:
        parser.exit(2, str(exc) + '\n')
    sys.argv = [args.alias, *args.arguments]
    return implementation.main()


if __name__ == '__main__':
    raise SystemExit(main())
