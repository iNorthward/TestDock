#!/usr/bin/env python3
"""Resolve a launch context without installing adapters or importing cases."""
from __future__ import annotations

import sys
import os
import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "test-platform")]

from platform_config import load_project_env, panel_port
from pack_registry import _configured_pack_id, _read_candidates, _select


def _launch_pack():
    load_project_env()
    packs = _select(_read_candidates(_configured_pack_id()))
    if len(packs) != 1:
        raise RuntimeError("服务启动需要唯一的项目 Pack")
    pack = packs[0]
    defaults = pack.get("config_defaults", {})
    if not isinstance(defaults, dict):
        raise RuntimeError("Pack config_defaults 必须是对象")
    return pack, panel_port(default=defaults.get('PLATFORM_PANEL_PORT', 8801))


def launch_configuration():
    pack, port = _launch_pack()
    return pack['id'], port


def launch_run_directory():
    pack, port = _launch_pack()
    legacy = os.environ.get('PLATFORM_PANEL_LEGACY_PORT') or pack.get('config_defaults', {}).get('PLATFORM_PANEL_LEGACY_PORT')
    if legacy:
        if not legacy.isascii() or not legacy.isdecimal() or not 1 <= int(legacy) <= 65535:
            raise ValueError('PLATFORM_PANEL_LEGACY_PORT 须为合法端口')
        if port == int(legacy):
            return ROOT / '.run'
    return ROOT / '.run' / f"{pack['id']}-{port}"


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', action='store_true')
    args = parser.parse_args()
    if args.run_dir:
        print(launch_run_directory())
    else:
        pack_id, port = launch_configuration()
        print(pack_id, port)
