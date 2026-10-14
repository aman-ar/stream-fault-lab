#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
PYTHON="${LAB_PYTHON:-python3}"
"$PYTHON" -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11 or newer is required. Install: brew install python"'
if [ ! -x .venv/bin/python ]; then "$PYTHON" -m venv .venv; fi
LAB_DEPENDENCY_DIGEST="$(shasum -a 256 requirements.txt)"
LAB_INSTALLED_DIGEST=""
if [ -f .venv/requirements.sha256 ]; then LAB_INSTALLED_DIGEST="$(cat .venv/requirements.sha256)"; fi
if [ "$LAB_DEPENDENCY_DIGEST" != "$LAB_INSTALLED_DIGEST" ] || ! .venv/bin/python -c 'import aiohttp; assert aiohttp.__version__ == "3.14.4"' 2>/dev/null; then
  .venv/bin/python -m pip install -r requirements.txt
  printf '%s\n' "$LAB_DEPENDENCY_DIGEST" > .venv/requirements.sha256
fi
.venv/bin/python scripts/bootstrap.py
exec .venv/bin/python -m lab.app "$@"
