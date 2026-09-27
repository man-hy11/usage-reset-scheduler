#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEST_HOME="$(mktemp -d)"
trap 'rm -rf -- "$TEST_HOME"' EXIT

fail() {
  printf 'FAIL: %s\n' "$1" >&2
  exit 1
}

mkdir -p "$TEST_HOME/.claude"

OUTPUT="$(HOME="$TEST_HOME" PATH="$ROOT/tests/bin:$PATH" "$ROOT/run-step-loop-claude.sh" --check-subscription)"
[[ "$OUTPUT" == *"[user1] 유료 구독 확인: pro"* ]] || fail "미지정 시 user1을 선택하지 않음: $OUTPUT"

OUTPUT="$(HOME="$TEST_HOME" PATH="$ROOT/tests/bin:$PATH" "$ROOT/run-step-loop-claude.sh" 2 --check-subscription)"
[[ "$OUTPUT" == *"[user2] SKIP:"* ]] || fail "user2 무료 플랜을 SKIP하지 않음: $OUTPUT"

OUTPUT="$(HOME="$TEST_HOME" PATH="$ROOT/tests/bin:$PATH" "$ROOT/run-step-loop-claude.sh" 1 2 3 --check-subscription)"
[[ "$OUTPUT" == *"[user1] 유료 구독 확인: pro"* ]] || fail "복수 선택에서 user1 누락"
[[ "$OUTPUT" == *"[user2] SKIP:"* ]] || fail "복수 선택에서 무료 user2를 SKIP하지 않음"
[[ "$OUTPUT" == *"[user3] 유료 구독 확인: max"* ]] || fail "복수 선택에서 user3 누락"

OUTPUT="$(HOME="$TEST_HOME" PATH="$ROOT/tests/bin:$PATH" "$ROOT/run-step-loop-claude.sh" --add-account 2)"
[[ "$OUTPUT" == *"LOGIN:$TEST_HOME/.claude-account-2"* ]] || fail "user2 로그인 경로 불일치: $OUTPUT"
[[ -d "$TEST_HOME/.claude-account-2" ]] || fail "user2 디렉터리가 생성되지 않음"

mkdir -p "$TEST_HOME/.claude-account-3"
mkdir -p "$TEST_HOME/.claude-account-10"
OUTPUT="$(HOME="$TEST_HOME" PATH="$ROOT/tests/bin:$PATH" "$ROOT/run-step-loop-claude.sh" --list-accounts)"
[[ "$OUTPUT" == *"user1"* && "$OUTPUT" == *"pro"* ]] || fail "목록에 user1 상태 누락"
[[ "$OUTPUT" == *"user2"* && "$OUTPUT" == *"free"* ]] || fail "목록에 user2 상태 누락"
[[ "$OUTPUT" == *"user3"* && "$OUTPUT" == *"max"* ]] || fail "목록에 user3 상태 누락"
[[ "$OUTPUT" == *"user10"* ]] || fail "두 자리 계정번호가 목록에서 누락"

OUTPUT="$(HOME="$TEST_HOME" PATH="$ROOT/tests/bin:$PATH" "$ROOT/run-step-loop-claude.sh" --remove-account 2)"
[[ ! -e "$TEST_HOME/.claude-account-2" ]] || fail "user2 원본 디렉터리가 남아 있음"
compgen -G "$TEST_HOME/.claude-account-2.removed-*" >/dev/null || fail "user2 복구 백업이 없음"
[[ "$OUTPUT" == *"복구 가능"* ]] || fail "제거 결과에 복구 안내가 없음"

set +e
OUTPUT="$(HOME="$TEST_HOME" PATH="$ROOT/tests/bin:$PATH" "$ROOT/run-step-loop-claude.sh" --remove-account 1 2>&1)"
RC=$?
set -e
(( RC != 0 )) || fail "user1 제거를 허용함"
[[ "$OUTPUT" == *"user1은 제거할 수 없습니다"* ]] || fail "user1 보호 안내 누락"

printf 'PASS: Claude 계정 관리자\n'
