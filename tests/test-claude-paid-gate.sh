#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

fail() {
  printf 'FAIL: %s\n' "$1" >&2
  exit 1
}

run_check() {
  CLAUDE_AUTH_STATUS_JSON="$1" \
  PATH="$ROOT/tests/bin:$PATH" \
    "$ROOT/run-step-loop-claude.sh" --check-subscription 2>&1
}

for plan in pro max team enterprise; do
  OUTPUT="$(run_check "{\"loggedIn\":true,\"authMethod\":\"claude.ai\",\"subscriptionType\":\"$plan\"}")"
  [[ "$OUTPUT" == *"유료 구독 확인: $plan"* ]] || fail "$plan 유료 플랜을 허용하지 않음: $OUTPUT"
  [[ "$OUTPUT" != *"SKIP"* ]] || fail "$plan 유료 플랜을 SKIP함"
done

OUTPUT="$(run_check '{"loggedIn":true,"authMethod":"claude.ai","subscriptionType":"free"}')"
[[ "$OUTPUT" == *"SKIP"* ]] || fail "무료 플랜을 SKIP하지 않음"

OUTPUT="$(run_check '{"loggedIn":false,"authMethod":"none","subscriptionType":null}')"
[[ "$OUTPUT" == *"SKIP"* ]] || fail "로그아웃 계정을 SKIP하지 않음"

OUTPUT="$(run_check '{"loggedIn":true,"authMethod":"apiKey","subscriptionType":"pro"}')"
[[ "$OUTPUT" == *"SKIP"* ]] || fail "API 인증을 구독 계정으로 허용함"

OUTPUT="$(run_check 'not-json')"
[[ "$OUTPUT" == *"SKIP"* ]] || fail "잘못된 상태 JSON을 SKIP하지 않음"

OUTPUT="$(
  CLAUDE_AUTH_STATUS_EXIT=7 \
  PATH="$ROOT/tests/bin:$PATH" \
    "$ROOT/run-step-loop-claude.sh" --check-subscription 2>&1
)"
[[ "$OUTPUT" == *"SKIP"* ]] || fail "상태 명령 실패를 SKIP하지 않음"

printf 'PASS: Claude 유료 구독 게이트\n'
