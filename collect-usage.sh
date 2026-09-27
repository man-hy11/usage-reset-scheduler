#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  cat <<'EOF' >&2
사용법:
  collect-usage.sh status <codex|claude> [출력파일]
      구독 사용량을 API에서 직접 조회해 정규화된 JSON으로 stdout에 출력한다.
      출력파일이 주어지면 그 파일에도 같은 내용을 쓴다.

  collect-usage.sh next <상태파일> <폴백분> <임계치> <초파일>
      상태파일을 읽어 정보 블록을 stdout에 출력하고,
      다음 실행까지 대기할 초를 <초파일>에 쓴다.
      주간 한도가 소진되면 주간 리셋, 그 외에는 5시간 리셋 직후로 계산한다.
      값이 유효하지 않으면 폴백분으로 대체한다.

정규화된 JSON 필드:
  tool, fetched_at, five_hour_used_percent, five_hour_reset_at,
  weekly_used_percent, weekly_reset_at
  (reset_at 은 모두 unix epoch 초 단위 정수)
EOF
}

read_token_codex() {
  python3 -c "import json,os;print(json.load(open(os.path.expanduser('~/.codex/auth.json')))['tokens']['access_token'])"
}

read_token_claude() {
  python3 <<'PY'
import json
import os

config_dir = os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude"
config_dir = os.path.abspath(os.path.expandvars(os.path.expanduser(config_dir)))
credentials_path = os.path.join(config_dir, ".credentials.json")
with open(credentials_path) as credentials_file:
    credentials = json.load(credentials_file)
print(credentials["claudeAiOauth"]["accessToken"])
PY
}

fetch_codex() {
  local token
  token="$(read_token_codex)"
  curl -fsS --max-time 20 https://chatgpt.com/backend-api/wham/usage \
    -H "Authorization: Bearer $token"
}

fetch_claude() {
  local token
  token="$(read_token_claude)"
  curl -fsS --max-time 20 https://api.anthropic.com/api/oauth/usage \
    -H "Authorization: Bearer $token" \
    -H "anthropic-beta: oauth-2025-04-20"
}

normalize() {
  local tool="$1"
  local raw="$2"
  python3 - "$tool" "$raw" <<'PY'
import json, sys, time
from datetime import datetime

tool = sys.argv[1]
raw = json.loads(sys.argv[2])

if tool == "codex":
    rl = raw.get("rate_limit") or {}
    p, s = rl.get("primary_window") or {}, rl.get("secondary_window") or {}
    five_pct, five_reset = p.get("used_percent"), p.get("reset_at")
    week_pct, week_reset = s.get("used_percent"), s.get("reset_at")
else:
    five, week = raw.get("five_hour") or {}, raw.get("seven_day") or {}
    five_pct, week_pct = five.get("utilization"), week.get("utilization")
    five_reset, week_reset = five.get("resets_at"), week.get("resets_at")

def to_epoch(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v)
    return int(datetime.fromisoformat(v).timestamp())

out = {
    "tool": tool,
    "fetched_at": int(time.time()),
    "five_hour_used_percent": five_pct,
    "five_hour_reset_at": to_epoch(five_reset),
    "weekly_used_percent": week_pct,
    "weekly_reset_at": to_epoch(week_reset),
}
json.dump(out, sys.stdout, ensure_ascii=False, indent=2)
sys.stdout.write("\n")
PY
}

cmd_status() {
  local tool="${1:-}"
  local outfile="${2:-}"
  [[ "$tool" == "codex" || "$tool" == "claude" ]] || { usage; exit 1; }

  local raw normalized
  raw="$(fetch_"$tool")"
  normalized="$(normalize "$tool" "$raw")"

  if [[ -n "$outfile" ]]; then
    printf '%s\n' "$normalized" > "$outfile"
  fi
  printf '%s\n' "$normalized"
}

cmd_next() {
  local statusfile="${1:-}"
  local fallback_min="${2:-305}"
  local threshold="${3:-100}"
  local sleepfile="${4:-}"
  [[ -n "$statusfile" && -n "$sleepfile" ]] || { usage; exit 1; }

  python3 - "$statusfile" "$fallback_min" "$threshold" "$sleepfile" <<'PY'
import json, os, sys, time
from datetime import datetime
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
MAX = 691200
path, fallback_min, threshold, sleepfile = sys.argv[1], int(sys.argv[2]), float(sys.argv[3]), sys.argv[4]
fallback = fallback_min * 60
now = int(os.environ.get("COLLECT_USAGE_NOW_EPOCH", time.time()))

def stamp(epoch):
    return datetime.fromtimestamp(epoch, KST).strftime("%Y-%m-%d %H:%M KST")

def percent(value):
    return f"{float(value):g}%"

def emit(five_pct, five_reset, week_pct, week_reset):
    if None in (five_pct, week_pct, five_reset, week_reset):
        seconds = fallback
        five_txt = week_txt = "확인 불가"
    else:
        target = week_reset if week_pct >= threshold else five_reset

        if target is None:
            seconds = fallback
        elif target <= now:
            seconds = fallback
        elif target - now + 60 >= MAX:
            seconds = fallback
        else:
            seconds = target - now + 60

        five_txt = f"{percent(five_pct)} 사용 / 리셋 {stamp(five_reset)}"
        week_txt = f"{percent(week_pct)} 사용 / 리셋 {stamp(week_reset)}"

    print(f"현재시간 : {stamp(now)}")
    print(f"5시간 한도 및 리셋시간: {five_txt}")
    print(f"주간한도 및 리셋시간: {week_txt}")
    print(f"다음 loop시작 시간: {stamp(now + seconds)} (약 {seconds // 60}분 후)")

    with open(sleepfile, "w") as f:
        f.write(str(seconds))

five_pct = week_pct = five_reset = week_reset = None
try:
    with open(path) as f:
        d = json.load(f)
    five_pct = d.get("five_hour_used_percent")
    week_pct = d.get("weekly_used_percent")
    five_reset = d.get("five_hour_reset_at")
    week_reset = d.get("weekly_reset_at")
except (FileNotFoundError, json.JSONDecodeError, OSError):
    pass

emit(five_pct, five_reset, week_pct, week_reset)
PY
}

case "${1:-}" in
  status) shift; cmd_status "$@" ;;
  next)   shift; cmd_next "$@" ;;
  *)      usage; exit 1 ;;
esac
