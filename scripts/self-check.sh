#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ok=0
fail() { echo "FAIL: $*"; ok=1; }
pass() { echo "OK  : $*"; }

command -v bash >/dev/null || fail bash
command -v jq >/dev/null || fail jq
if command -v python3 >/dev/null; then
    python3 -c 'import sys, sqlite3; assert sys.version_info >= (3, 9)' || fail "Python 3.9+ with sqlite3"
else
    fail python3
fi
[ -f "$ROOT/scripts/session-store.py" ] || fail session-store.py
command -v openssl >/dev/null || fail openssl
command -v curl >/dev/null || fail curl

[ -x "$ROOT/scripts/archive-engine.sh" ] || fail "archive-engine.sh not executable"
[ -x "$ROOT/scripts/config-manager.sh" ] || fail "config-manager.sh not executable"
[ -f "$ROOT/scripts/lib/credentials-store.sh" ] || fail credentials-store
[ -f "$ROOT/scripts/lib/log-maintenance.sh" ] || fail log-maintenance.sh
[ -f "$ROOT/scripts/lib/config-loader.sh" ] || fail config-loader.sh
[ -x "$ROOT/scripts/skill-interactive.sh" ] || fail skill-interactive.sh
[ -x "$ROOT/scripts/extractors/local-extractor.sh" ] || fail local-extractor.sh
[ -x "$ROOT/scripts/summarizers/cloud-summarizer.sh" ] || fail cloud-summarizer.sh
[ -f "$ROOT/SKILL.md" ] || fail SKILL.md
[ -f "$ROOT/README.md" ] || fail README.md

bash -n "$ROOT/scripts/archive-engine.sh" || fail "archive-engine bash -n"
bash -n "$ROOT/scripts/config-manager.sh" || fail "config-manager bash -n"
bash -n "$ROOT/scripts/lib/log-maintenance.sh" || fail "log-maintenance bash -n"

if [ -x "$ROOT/scripts/test-extractor-titles.sh" ]; then
    bash "$ROOT/scripts/test-extractor-titles.sh" || fail "extractor titles mismatch"
else
    fail "test-extractor-titles.sh missing"
fi

if [ -x "$ROOT/scripts/test-output-format.sh" ]; then
    bash "$ROOT/scripts/test-output-format.sh" || fail "output format mismatch"
else
    fail "test-output-format.sh missing"
fi

if [ -x "$ROOT/scripts/test-fail-guard.sh" ]; then
    bash "$ROOT/scripts/test-fail-guard.sh" || fail "fail guard structure"
else
    fail "test-fail-guard.sh missing"
fi

if [ -x "$ROOT/scripts/health-check.sh" ]; then
    bash -n "$ROOT/scripts/health-check.sh" || fail "health-check bash -n"
else
    fail "health-check.sh missing"
fi

if [ -x "$ROOT/scripts/test-wave10.sh" ]; then
    bash "$ROOT/scripts/test-wave10.sh" || fail "wave10 structure"
else
    fail "test-wave10.sh missing"
fi

pass "依赖与脚本语法"
exit "$ok"
