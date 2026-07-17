#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$0")/.."
if python3 -c 'import pytest' >/dev/null 2>&1; then
  python3 -m pytest -q
else
  python3 -m unittest discover -s tests -v
fi
