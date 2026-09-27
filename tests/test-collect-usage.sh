#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SLEEP_FILE="$(mktemp)"
trap 'rm "$SLEEP_FILE"' EXIT

fail() {
  printf 'FAIL: %s\n' "$1" >&2
  exit 1
}

OUTPUT="$(
  COLLECT_USAGE_NOW_EPOCH=1790469000 \
    "$ROOT/collect-usage.sh" next \
      "$ROOT/tests/fixtures/usage.json" 305 100 "$SLEEP_FILE"
)"

mapfile -t LINES <<<"$OUTPUT"
[[ ${#LINES[@]} -eq 4 ]] || fail "출력은 정확히 네 줄이어야 함 (actual=${#LINES[@]})"
[[ "${LINES[0]}" == "현재시간 : 2026-09-27 09:30 KST" ]] || fail "현재시간 형식 불일치: ${LINES[0]}"
[[ "${LINES[1]}" == "5시간 한도 및 리셋시간: 100% 사용 / 리셋 2026-09-27 09:40 KST" ]] || fail "5시간 한도 형식 불일치: ${LINES[1]}"
[[ "${LINES[2]}" == "주간한도 및 리셋시간: 42% 사용 / 리셋 2026-10-04 09:30 KST" ]] || fail "주간 한도 형식 불일치: ${LINES[2]}"
[[ "${LINES[3]}" == "다음 loop시작 시간: 2026-09-27 09:41 KST (약 11분 후)" ]] || fail "다음 loop 형식 불일치: ${LINES[3]}"
[[ "$(<"$SLEEP_FILE")" == "660" ]] || fail "리셋 1분 뒤까지의 대기시간이 아님"

OUTPUT="$(
  COLLECT_USAGE_NOW_EPOCH=1790469000 \
    "$ROOT/collect-usage.sh" next \
      "$ROOT/tests/fixtures/usage-not-exhausted.json" 5 100 "$SLEEP_FILE"
)"
[[ "$(<"$SLEEP_FILE")" == "660" ]] || fail "5시간 한도 미소진 시에도 리셋 시각을 사용해야 함"
[[ "$OUTPUT" == *"다음 loop시작 시간: 2026-09-27 09:41 KST (약 11분 후)"* ]] || fail "미소진 상태의 다음 loop 시각 불일치"

OUTPUT="$(
  COLLECT_USAGE_NOW_EPOCH=1790469000 \
    "$ROOT/collect-usage.sh" next \
      "$ROOT/tests/fixtures/usage-both-exhausted.json" 305 100 "$SLEEP_FILE"
)"
[[ "$(<"$SLEEP_FILE")" == "1260" ]] || fail "두 한도 소진 시 주간 리셋을 우선하지 않음"
[[ "$OUTPUT" == *"다음 loop시작 시간: 2026-09-27 09:51 KST (약 21분 후)"* ]] || fail "주간 리셋 기준 다음 loop 시각 불일치"

OUTPUT="$(
  COLLECT_USAGE_NOW_EPOCH=1790469000 \
    "$ROOT/collect-usage.sh" next \
      "$ROOT/tests/fixtures/not-found.json" 7 100 "$SLEEP_FILE"
)"
mapfile -t LINES <<<"$OUTPUT"
[[ ${#LINES[@]} -eq 4 ]] || fail "조회 실패 시에도 정확히 네 줄이어야 함"
[[ "${LINES[1]}" == "5시간 한도 및 리셋시간: 확인 불가" ]] || fail "조회 실패 시 5시간 표시 불일치"
[[ "${LINES[2]}" == "주간한도 및 리셋시간: 확인 불가" ]] || fail "조회 실패 시 주간 표시 불일치"
[[ "$(<"$SLEEP_FILE")" == "420" ]] || fail "조회 실패 시 폴백 대기시간 불일치"

printf 'PASS: collect-usage 출력 및 다음 loop 계산\n'
