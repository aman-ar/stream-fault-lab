#!/bin/bash
# Run the unit tests (no FFmpeg, MediaMTX, or running lab needed).
# Use --smoke to run only the real-media check against a lab already started with ./start.sh.
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then "${LAB_PYTHON:-python3}" -m venv .venv; fi
.venv/bin/python -c 'import aiohttp' 2>/dev/null || .venv/bin/python -m pip install -q -r requirements.txt
if [ "${1:-}" = "--smoke" ]; then
  shift
  exec .venv/bin/python scripts/smoke.py "$@"
fi
exec .venv/bin/python -m unittest discover -s tests "$@"
