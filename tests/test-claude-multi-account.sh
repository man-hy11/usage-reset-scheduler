#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

fail() {
  printf 'FAIL: %s\n' "$1" >&2
  exit 1
}

OUTPUT="$(
  HOME="$ROOT/tests/fixtures/fake-home" \
  CLAUDE_CONFIG_DIR="$ROOT/tests/fixtures/claude-profile" \
  PATH="$ROOT/tests/bin:$PATH" \
    "$ROOT/collect-usage.sh" status claude
)"

[[ "$OUTPUT" == *'"tool": "claude"'* ]] || fail "Claude 상태 정규화 실패"
[[ "$OUTPUT" == *'"five_hour_used_percent": 12'* ]] || fail "별도 프로필 사용량을 읽지 못함"
[[ "$OUTPUT" == *'"weekly_used_percent": 34'* ]] || fail "별도 프로필 주간 사용량을 읽지 못함"

SOURCE="$(<"$ROOT/run-step-loop-claude.sh")"
HELP="$("$ROOT/run-step-loop-claude.sh" --help 2>&1)"
[[ "$SOURCE" == *'CLAUDE_CONFIG_DIR'* ]] || fail "Claude loop가 CLAUDE_CONFIG_DIR를 사용하지 않음"
[[ "$SOURCE" == *'start-limit-runs'* ]] || fail "계정별 실행 디렉터리가 없음"
[[ "$HELP" == *'CLAUDE_CONFIG_DIR'* ]] || fail "도움말에 다중 계정 실행법이 없음"

printf 'PASS: Claude 다중 계정 분리\n'
