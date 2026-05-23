#!/usr/bin/env bash
# Pre-commit hook — runs `make check` so we never commit code that breaks
# ruff / mypy / pyright / unit tests.
#
# To bypass in an emergency: `git commit --no-verify`.

set -euo pipefail

cd "$(git rev-parse --show-toplevel)"
echo "==> .claude pre-commit: make check"
make check
