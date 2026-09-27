#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  cat <<EOF
사용법: $0 [계정번호 ...] [옵션]

Claude를 최소 프롬프트로 반복 호출해 사용량 창을 시작합니다.

계정 선택:
  $0 -w "14:00"         계정 미지정: user1
  $0 2 -w "14:00"       user2만 실행
  $0 1 2 3 -w "14:00"   user1~user3 병렬 실행
  user1은 ~/.claude, userN은 ~/.claude-account-N을 CLAUDE_CONFIG_DIR로 사용합니다.

계정 관리:
      --add-account N     userN 로그인 등록 (N은 2 이상)
      --list-accounts     등록 계정과 구독 상태 표시
      --remove-account N  userN을 복구 가능한 백업명으로 이동

옵션:
  --model <model>         Claude 모델. 기본값: claude-haiku-4-5
  --effort <effort>       Claude effort. 기본값: low
  -w, --wait-until TIME   최초 실행 시각. HH:MM 또는 YYYY-MM-DD HH:MM
  -d, --delay <분>        최초 실행 전 추가 대기 시간. 기본값: 0
      --interval <분>     조회 실패 시 재시도 간격. 기본값: 5
      --threshold <%>     주간 한도 소진 판정 기준. 기본값: 100
      --check-subscription
                          유료 구독 여부만 확인하고 종료
  -h, --help              도움말 표시

반복 규칙:
  Pro, Max, Team, Enterprise 구독만 실행하며 그 외 계정은 SKIP합니다.
  호출 후 최신 사용량을 조회합니다. 주간 한도가 소진됐으면 주간 리셋 시각 + 1분,
  그 외에는 사용률과 관계없이 5시간 리셋 시각 + 1분에 다음 loop를 실행합니다.
  조회 또는 계산 실패 시 --interval 분 후 다시 시도합니다.
EOF
}

MODEL="claude-haiku-4-5"
EFFORT="low"
WAIT_UNTIL=""
DELAY_MIN=0
INTERVAL_MIN=5
THRESHOLD=100
CHECK_SUBSCRIPTION=false
ACCOUNT_IDS=()
ACCOUNT_ACTION=""
ACCOUNT_ACTION_ID=""
INTERNAL_SINGLE=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --model) MODEL="${2:-}"; shift 2 ;;
    --model=*) MODEL="${1#*=}"; shift ;;
    --effort) EFFORT="${2:-}"; shift 2 ;;
    --effort=*) EFFORT="${1#*=}"; shift ;;
    --wait-until|-w) WAIT_UNTIL="${2:-}"; shift 2 ;;
    --wait-until=*) WAIT_UNTIL="${1#*=}"; shift ;;
    --delay|-d) DELAY_MIN="${2:-}"; shift 2 ;;
    --delay=*) DELAY_MIN="${1#*=}"; shift ;;
    --interval) INTERVAL_MIN="${2:-}"; shift 2 ;;
    --interval=*) INTERVAL_MIN="${1#*=}"; shift ;;
    --threshold) THRESHOLD="${2:-}"; shift 2 ;;
    --threshold=*) THRESHOLD="${1#*=}"; shift ;;
    --check-subscription) CHECK_SUBSCRIPTION=true; shift ;;
    --add-account)
      [[ $# -ge 2 ]] || { echo "--add-account에 계정번호가 필요합니다." >&2; exit 1; }
      ACCOUNT_ACTION="add"; ACCOUNT_ACTION_ID="$2"; shift 2 ;;
    --add-account=*) ACCOUNT_ACTION="add"; ACCOUNT_ACTION_ID="${1#*=}"; shift ;;
    --remove-account)
      [[ $# -ge 2 ]] || { echo "--remove-account에 계정번호가 필요합니다." >&2; exit 1; }
      ACCOUNT_ACTION="remove"; ACCOUNT_ACTION_ID="$2"; shift 2 ;;
    --remove-account=*) ACCOUNT_ACTION="remove"; ACCOUNT_ACTION_ID="${1#*=}"; shift ;;
    --list-accounts) ACCOUNT_ACTION="list"; shift ;;
    --single-account)
      [[ $# -ge 2 ]] || { echo "--single-account에 계정번호가 필요합니다." >&2; exit 1; }
      INTERNAL_SINGLE=true; ACCOUNT_IDS+=("$2"); shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *)
      if [[ "$1" =~ ^[1-9][0-9]*$ ]]; then
        ACCOUNT_IDS+=("$1")
        shift
      else
        echo "알 수 없는 인자: $1" >&2
        usage >&2
        exit 1
      fi
      ;;
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
if ! command -v claude >/dev/null 2>&1; then
  echo "claude 명령을 찾을 수 없습니다." >&2
  exit 1
fi
if ! command -v jq >/dev/null 2>&1; then
  echo "jq가 필요합니다." >&2
  exit 1
fi

account_dir() {
  local account_id="$1"
  if [[ "$account_id" == "1" ]]; then
    printf '%s\n' "$HOME/.claude"
  else
    printf '%s\n' "$HOME/.claude-account-$account_id"
  fi
}

validate_account_id() {
  [[ "$1" =~ ^[1-9][0-9]*$ ]]
}

run_for_account() {
  local account_id="$1"
  shift
  if [[ "$account_id" == "1" ]]; then
    env -u CLAUDE_CONFIG_DIR CLAUDE_ACCOUNT_ID=1 "$@"
  else
    CLAUDE_CONFIG_DIR="$(account_dir "$account_id")" CLAUDE_ACCOUNT_ID="$account_id" "$@"
  fi
}

classify_subscription_json() {
  local auth_json="$1"
  python3 - "$auth_json" <<'PY'
import json
import sys

try:
    status = json.loads(sys.argv[1])
except (json.JSONDecodeError, TypeError):
    print("인증 상태 JSON 파싱 실패")
    raise SystemExit(1)

if status.get("loggedIn") is not True:
    print("로그인되지 않은 계정")
    raise SystemExit(1)

auth_method = str(status.get("authMethod") or "").lower()
if auth_method != "claude.ai":
    print(f"Claude 구독 로그인이 아님 (authMethod={auth_method or 'unknown'})")
    raise SystemExit(1)

plan = str(status.get("subscriptionType") or "").lower()
paid_plans = {"pro", "max", "team", "enterprise"}
if plan not in paid_plans:
    print(f"유료 Claude 구독이 아님 (subscriptionType={plan or 'unknown'})")
    raise SystemExit(1)

print(plan)
PY
}

check_paid_subscription() {
  local auth_json
  if ! auth_json="$(claude auth status --json 2>/dev/null)"; then
    echo "인증 상태 확인 실패"
    return 1
  fi

  classify_subscription_json "$auth_json"
}

if [[ -n "$ACCOUNT_ACTION" ]]; then
  if (( ${#ACCOUNT_IDS[@]} > 0 )); then
    echo "계정 관리 옵션과 실행 계정번호를 함께 사용할 수 없습니다." >&2
    exit 1
  fi

  case "$ACCOUNT_ACTION" in
    add)
      if ! validate_account_id "$ACCOUNT_ACTION_ID" || (( 10#$ACCOUNT_ACTION_ID < 2 )); then
        echo "추가할 계정번호는 2 이상의 정수여야 합니다." >&2
        exit 1
      fi
      account_path="$(account_dir "$ACCOUNT_ACTION_ID")"
      mkdir -p "$account_path"
      echo "user$ACCOUNT_ACTION_ID 로그인을 시작합니다: $account_path"
      CLAUDE_CONFIG_DIR="$account_path" claude auth login
      exit $?
      ;;
    remove)
      if ! validate_account_id "$ACCOUNT_ACTION_ID"; then
        echo "제거할 계정번호는 1 이상의 정수여야 합니다." >&2
        exit 1
      fi
      if [[ "$ACCOUNT_ACTION_ID" == "1" ]]; then
        echo "user1은 제거할 수 없습니다 (기존 기본 계정 보호)." >&2
        exit 1
      fi
      account_path="$(account_dir "$ACCOUNT_ACTION_ID")"
      if [[ ! -e "$account_path" ]]; then
        echo "등록된 user$ACCOUNT_ACTION_ID 계정을 찾을 수 없습니다: $account_path" >&2
        exit 1
      fi
      backup_path="$account_path.removed-$(date '+%Y%m%d-%H%M%S')-$$"
      mv -- "$account_path" "$backup_path"
      echo "user$ACCOUNT_ACTION_ID 제거 완료 (복구 가능): $backup_path"
      exit 0
      ;;
    list)
      account_ids=(1)
      shopt -s nullglob
      for account_path in "$HOME"/.claude-account-*; do
        suffix="${account_path##*.claude-account-}"
        if [[ -d "$account_path" && "$suffix" =~ ^[2-9]$|^[1-9][0-9]+$ ]]; then
          account_ids+=("$suffix")
        fi
      done
      shopt -u nullglob
      mapfile -t account_ids < <(printf '%s\n' "${account_ids[@]}" | sort -nu)
      printf '%-8s %-12s %s\n' "계정" "구독" "경로"
      for account_id in "${account_ids[@]}"; do
        account_path="$(account_dir "$account_id")"
        if auth_json="$(run_for_account "$account_id" claude auth status --json 2>/dev/null)"; then
          if plan="$(classify_subscription_json "$auth_json")"; then
            status_text="$plan"
          else
            status_text="SKIP: $plan"
          fi
        else
          status_text="SKIP: 인증 상태 확인 실패"
        fi
        printf 'user%-4s %-12s %s\n' "$account_id" "$status_text" "$account_path"
      done
      exit 0
      ;;
  esac
fi

if (( ${#ACCOUNT_IDS[@]} == 0 )); then
  ACCOUNT_IDS=(1)
fi

declare -A seen_accounts=()
unique_accounts=()
for account_id in "${ACCOUNT_IDS[@]}"; do
  if ! validate_account_id "$account_id"; then
    echo "잘못된 계정번호입니다: $account_id" >&2
    exit 1
  fi
  account_id=$((10#$account_id))
  if [[ -z "${seen_accounts[$account_id]:-}" ]]; then
    seen_accounts[$account_id]=1
    unique_accounts+=("$account_id")
  fi
done
ACCOUNT_IDS=("${unique_accounts[@]}")

if [[ "$INTERNAL_SINGLE" != "true" ]] && (( ${#ACCOUNT_IDS[@]} > 1 )); then
  child_common=(
    --model "$MODEL"
    --effort "$EFFORT"
    --delay "$DELAY_MIN"
    --interval "$INTERVAL_MIN"
    --threshold "$THRESHOLD"
  )
  if [[ -n "$WAIT_UNTIL" ]]; then
    child_common+=(--wait-until "$WAIT_UNTIL")
  fi
  if [[ "$CHECK_SUBSCRIPTION" == "true" ]]; then
    child_common+=(--check-subscription)
  fi

  child_pids=()
  for account_id in "${ACCOUNT_IDS[@]}"; do
    "$SCRIPT_DIR/run-step-loop-claude.sh" --single-account "$account_id" "${child_common[@]}" &
    child_pids+=("$!")
  done

  stop_children() {
    local child_pid
    for child_pid in "${child_pids[@]}"; do
      kill "$child_pid" 2>/dev/null || true
    done
    wait || true
  }
  trap 'stop_children; exit 130' INT TERM

  child_rc=0
  for child_pid in "${child_pids[@]}"; do
    if ! wait "$child_pid"; then
      child_rc=1
    fi
  done
  exit "$child_rc"
fi

ACCOUNT_ID="${ACCOUNT_IDS[0]}"
export CLAUDE_ACCOUNT_ID="$ACCOUNT_ID"
if [[ "$ACCOUNT_ID" == "1" ]]; then
  unset CLAUDE_CONFIG_DIR
else
  export CLAUDE_CONFIG_DIR="$(account_dir "$ACCOUNT_ID")"
fi

if PAID_PLAN="$(check_paid_subscription)"; then
  echo "[user$ACCOUNT_ID] 유료 구독 확인: $PAID_PLAN"
  if [[ "$CHECK_SUBSCRIPTION" == "true" ]]; then
    exit 0
  fi
else
  echo "[user$ACCOUNT_ID] SKIP: $PAID_PLAN"
  exit 0
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
CLAUDE_PROFILE_DIR="$(python3 - <<'PY'
import os

path = os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude"
print(os.path.abspath(os.path.expandvars(os.path.expanduser(path))))
PY
)"
LOG_DIR="$CLAUDE_PROFILE_DIR/start-limit-runs"
STATUS_FILE="$LOG_DIR/status.json"
SLEEP_FILE="$LOG_DIR/next-sleep.txt"
PROMPT_TEXT="Reply with OK."
mkdir -p "$LOG_DIR"
cd "$SCRIPT_DIR"

wait_for_next_loop() {
  local status_rc sleep_sec

  set +e
  "$COLLECT_USAGE" status claude "$STATUS_FILE" >/dev/null 2>&1
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

run_claude() {
  RUN_RC=0
  claude --dangerously-skip-permissions \
    --model "$MODEL" \
    --effort "$EFFORT" \
    -p --output-format stream-json --verbose --include-partial-messages \
    "$PROMPT_TEXT" \
    | jq -rj 'select(.type == "stream_event" and .event.delta.type? == "text_delta") | .event.delta.text' \
    | tee "$LOG_FILE" || RUN_RC=$?
  echo
}

while true; do
  LOG_FILE="$LOG_DIR/loop-$(date '+%Y%m%d-%H%M%S').log"

  set +e
  run_claude
  set -e

  if (( RUN_RC != 0 )); then
    echo "[$(date '+%F %T')] Claude 호출 실패 (exit=$RUN_RC). 로그: $LOG_FILE"
  else
    echo "[$(date '+%F %T')] Claude 호출 완료. 로그: $LOG_FILE"
  fi

  wait_for_next_loop
done
