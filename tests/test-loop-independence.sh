#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

fail() {
  printf 'FAIL: %s\n' "$1" >&2
  exit 1
}

for script in run-step-loop.sh run-step-loop-claude.sh; do
  SOURCE="$(<"$ROOT/$script")"
  HELP="$("$ROOT/$script" --help 2>&1)"

  [[ "$SOURCE" != *"prompt.md"* ]] || fail "$script: prompt.md 의존성이 남아 있음"
  [[ "$SOURCE" != *"STEP_STATUS"* ]] || fail "$script: STEP_STATUS 의존성이 남아 있음"
  [[ "$SOURCE" != *"shortform-ai"* ]] || fail "$script: shortform-ai 의존성이 남아 있음"
  [[ "$SOURCE" != *"--smoke"* ]] || fail "$script: --smoke 분기가 남아 있음"
  [[ "$SOURCE" == *'PROMPT_TEXT="Reply with OK."'* ]] || fail "$script: 최소 프롬프트가 아님"
  [[ "$HELP" == *"5시간 리셋 시각 + 1분"* ]] || fail "$script: 도움말에 예약 규칙이 없음"
done

printf 'PASS: loop 스크립트 독립성\n'
