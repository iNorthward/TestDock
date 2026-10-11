#!/usr/bin/env bash
# Static gates and changed-source offline regression; never all business cases.
set -euo pipefail
cd "$(dirname "$0")/.."
export PLATFORM_ENV_FILE=/dev/null
export PLATFORM_PACKS=demo_pack
export PYTHONDONTWRITEBYTECODE=1
python3 scripts/check_neutral_distribution.py --json
python3 scripts/check_platform_isolation.py --json
python3 scripts/validate_catalog.py --strict
python3 scripts/targeted_regression.py "$@"
