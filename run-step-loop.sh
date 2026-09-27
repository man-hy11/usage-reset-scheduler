#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  cat <<EOF
사용법: $0 [옵션]

Codex를 최소 프롬프트로 반복 호출해 사용량 창을 시작합니다.

옵션:
  -w, --wait-until TIME   최초 실행 시각. HH:MM 또는 YYYY-MM-DD HH:MM
  -d, --delay <분>        최초 실행 전 추가 대기 시간. 기본값: 0
      --interval <분>     조회 실패 시 재시도 간격. 기본값: 5
      --threshold <%>     주간 한도 소진 판정 기준. 기본값: 100
  -h, --help              도움말 표시

반복 규칙:
  호출 후 최신 사용량을 조회합니다. 주간 한도가 소진됐으면 주간 리셋 시각 + 1분,
  그 외에는 사용률과 관계없이 5시간 리셋 시각 + 1분에 다음 loop를 실행합니다.
  조회 또는 계산 실패 시 --interval 분 후 다시 시도합니다.
EOF
}

WAIT_UNTIL=""
DELAY_MIN=0
INTERVAL_MIN=5
THRESHOLD=100

while [[ $# -gt 0 ]]; do
  case "$1" in
    --wait-until|-w) WAIT_UNTIL="${2:-}"; shift 2 ;;
    --wait-until=*) WAIT_UNTIL="${1#*=}"; shift ;;
    --delay|-d) DELAY_MIN="${2:-}"; shift 2 ;;
    --delay=*) DELAY_MIN="${1#*=}"; shift ;;
    --interval) INTERVAL_MIN="${2:-}"; shift 2 ;;
    --interval=*) INTERVAL_MIN="${1#*=}"; shift ;;
    --threshold) THRESHOLD="${2:-}"; shift 2 ;;
    --threshold=*) THRESHOLD="${1#*=}"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "알 수 없는 인자: $1" >&2; usage >&2; exit 1 ;;
  esac
done

if ! [[ "$DELAY_MIN" =~ ^[0-9]+$ ]]; then
  echo "--delay는 0 이상의 정수(분)여야 합니다. (입력: $DELAY_MIN)" >&2
  exit 1
fi
if ! [[ "$INTERVAL_MIN" =~ ^[0-9]+$ ]]; then
  echo "--interval은 0 이상의 정수(분)여야 합니다. (입력: $INTERVAL_MIN)" >&2
  exit 1
fi
if ! [[ "$THRESHOLD" =~ ^[0-9]+$ ]] || (( THRESHOLD > 100 )); then
  echo "--threshold는 0 이상 100 이하의 정수여야 합니다. (입력: $THRESHOLD)" >&2
  exit 1
fi
if ! command -v codex >/dev/null 2>&1; then
  echo "codex 명령을 찾을 수 없습니다." >&2
  exit 1
fi

START_TIME="$(date '+%F %T')"
START_EPOCH="$(date '+%s')"

if [[ -n "$WAIT_UNTIL" ]]; then
  if [[ "$WAIT_UNTIL" =~ ^([0-9]{1,2}):([0-9]{2})$ ]]; then
    hour=$((10#${BASH_REMATCH[1]}))
    minute=$((10#${BASH_REMATCH[2]}))
    if (( hour > 23 || minute > 59 )); then
      echo "잘못된 --wait-until 시각입니다: '$WAIT_UNTIL'" >&2
      exit 1
    fi
    target_src="$(date '+%F') $hour:$minute:00"
  elif [[ "$WAIT_UNTIL" =~ ^([0-9]{4})-([0-9]{2})-([0-9]{2})\ ([0-9]{1,2}):([0-9]{2})$ ]]; then
    month=$((10#${BASH_REMATCH[2]}))
    day=$((10#${BASH_REMATCH[3]}))
    hour=$((10#${BASH_REMATCH[4]}))
    minute=$((10#${BASH_REMATCH[5]}))
    if (( hour > 23 || minute > 59 || month < 1 || month > 12 || day < 1 || day > 31 )); then
      echo "잘못된 --wait-until 시각입니다: '$WAIT_UNTIL'" >&2
      exit 1
    fi
    target_src="$WAIT_UNTIL:00"
  else
    echo "잘못된 --wait-until 형식입니다: '$WAIT_UNTIL'" >&2
    exit 1
  fi

  if ! target_epoch="$(date -d "$target_src" '+%s' 2>/dev/null)"; then
    echo "잘못된 --wait-until 날짜입니다: '$WAIT_UNTIL'" >&2
    exit 1
  fi
  if (( target_epoch <= START_EPOCH )); then
    echo "지정한 시각이 이미 과거입니다: '$WAIT_UNTIL' (현재: $START_TIME)" >&2
    exit 1
  fi

  echo "현재시간 : $START_TIME"
  echo "최초 loop시작 시간: $(date -d "@$target_epoch" '+%F %T')"
  while (( $(date '+%s') < target_epoch )); do
    remaining=$((target_epoch - $(date '+%s')))
    (( remaining > 60 )) && sleep 60 || sleep "$remaining"
  done
fi

if (( DELAY_MIN > 0 )); then
  echo "$DELAY_MIN분 뒤에 실행을 시작합니다."
  sleep "$((DELAY_MIN * 60))"
fi

COLLECT_USAGE="$SCRIPT_DIR/collect-usage.sh"
LOG_DIR="$SCRIPT_DIR/.codex-runs"
STATUS_FILE="$LOG_DIR/status.json"
SLEEP_FILE="$LOG_DIR/next-sleep.txt"
PROMPT_TEXT="Reply with OK."
mkdir -p "$LOG_DIR"
cd "$SCRIPT_DIR"

wait_for_next_loop() {
  local status_rc sleep_sec

  set +e
  "$COLLECT_USAGE" status codex "$STATUS_FILE" >/dev/null 2>&1
  status_rc=$?
  set -e
  if (( status_rc != 0 )); then
    rm -f "$STATUS_FILE"
  fi

  if ! "$COLLECT_USAGE" next "$STATUS_FILE" "$INTERVAL_MIN" "$THRESHOLD" "$SLEEP_FILE"; then
    echo "[$(date '+%F %T')] 다음 loop 시각 계산 실패. ${INTERVAL_MIN}분 후 재시도합니다."
    sleep "$((INTERVAL_MIN * 60))"
    return
  fi

  sleep_sec="$(<"$SLEEP_FILE")"
  if [[ "$sleep_sec" =~ ^[0-9]+$ ]]; then
    (( sleep_sec > 0 )) && sleep "$sleep_sec"
  else
    echo "[$(date '+%F %T')] 대기 시간 확인 실패. ${INTERVAL_MIN}분 후 재시도합니다."
    sleep "$((INTERVAL_MIN * 60))"
  fi
}

while true; do
  LOG_FILE="$LOG_DIR/loop-$(date '+%Y%m%d-%H%M%S').log"

  set +e
  codex exec \
    --sandbox danger-full-access \
    --model gpt-6-luna \
    -c model_reasoning_effort="low" \
    -C "$SCRIPT_DIR" \
    "$PROMPT_TEXT" | tee "$LOG_FILE"
  RUN_RC=$?
  set -e

  if (( RUN_RC != 0 )); then
    echo "[$(date '+%F %T')] Codex 호출 실패 (exit=$RUN_RC). 로그: $LOG_FILE"
  else
    echo "[$(date '+%F %T')] Codex 호출 완료. 로그: $LOG_FILE"
  fi

  wait_for_next_loop
done
