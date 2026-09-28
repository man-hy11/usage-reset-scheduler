# usage-reset-scheduler

Claude Code(`claude` CLI)와 Codex CLI(`codex`)를 여러 계정에 걸쳐 최소 호출로 반복 실행해, 사용량 창(5시간 / 주간)을 리셋 직후 다시 시작시키는 스케줄러입니다. 도구가 섞여 있어도(Claude 계정 + Codex 계정) **하나의 우선순위 큐**로 관리합니다.

## 요구 사항

- Python 3.10+
- `pip install -r requirements.txt` (테스트도 실행하려면 `pip install -r requirements-dev.txt`)
- 사용할 도구의 CLI가 PATH에 있고, 최소 한 번은 로그인되어 있을 것
  - Claude: `claude auth login`
  - Codex: `codex login`

## 빠른 시작

```bash
# 계정 미지정 시 user1(claude 도구)을 대상으로 실행
python3 scheduler.py

# 여러 계정을 동시에 스케줄에 등록 (순차 실행, 동시에 두 계정을 실행하지 않음)
# 각 계정은 claude일 수도 codex일 수도 있다 — 등록된 도구를 따른다
python3 scheduler.py 1 2 3 4

# 계정 번호를 나열하는 대신, 존재하는(로그인된) 계정 중 유료 구독인 계정을
# 1번부터 순서대로 최대 N개 자동 선택 (계정이 4개면 4개까지, 6개면 6개까지;
# free 계정은 건너뛰고 다음 번호를 확인). account 1은 --add-account 없이도
# 항상 후보에 포함된다. account_ids나 계정 관리 옵션(-a/-l/-r/-c)과는
# 함께 쓸 수 없음
python3 scheduler.py --count 6   # 또는: python3 scheduler.py -n 6

# 특정 시각에 최초 실행 시작
python3 scheduler.py 1 -w "14:00"
python3 scheduler.py 1 -w "2026-10-01 09:00"

# 작업 루프: 유료 계정 4개를 번갈아 쓰며 ~/shortform-ai의 prompt.md를 계속 실행
# (한도가 찬 계정은 리셋 시각까지 쉬고, 쓸 수 있는 다음 계정이 같은 step을 이어받음)
python3 scheduler.py --project-loop --project-dir ~/shortform-ai -n 4
```

## 동작 방식 (요약)

- 지정한 각 계정의 구독 상태를 시작 시(재시작 포함) 확인합니다. 유료 플랜만 통과하고, 무료·미로그인·조회실패 계정은 `free_skip`이 됩니다.
  - 플랜은 로그인 당시 로컬에 저장된 값(`claude auth status`의 `subscriptionType`, Codex `id_token`의 플랜 클레임)이 아니라 **실시간 API**로 판단합니다 — Claude는 `/api/oauth/profile`의 `organization_type`, Codex는 `wham/usage`의 `plan_type`. 재로그인 없이도 유료→무료, 무료→유료 전환을 모두 잡아냅니다. 실시간 조회가 실패하면(네트워크·429 등) 로컬 값으로 대신 판단합니다.
  - `scheduled` 계정은 매 실행 직전에 구독을 다시 확인해, 무료로 바뀌었으면 실행하지 않고 `free_skip`으로 내립니다.
  - `free_skip` 계정은 60분(`PLAN_RECHECK_MIN`)마다 구독을 다시 확인해, 유료로 바뀌었으면 `scheduled`로 올려 바로 실행합니다.
  - Claude: Pro/Max/Team/Enterprise
  - Codex: Plus/Pro/Team/Business/Enterprise
- 남은 계정들은 우선순위 큐(다음 실행 예정 시각 기준)로 관리되며, 도구 호출(`claude`/`codex`)은 **한 번에 한 계정씩 순차로만** 실행됩니다. claude 계정과 codex 계정이 섞여 있어도 같은 큐 안에서 순서대로 처리됩니다.
- 호출 후 사용량을 재조회해 다음 실행 시각을 계산합니다: 주간 한도가 임계치 이상 소진됐으면 주간 리셋 + 1분, 그 외에는 5시간 리셋 + 1분.
- 사용량 조회가 실패한 계정은 `retry_pending` 상태로 남아 `--interval`분 간격으로 재시도합니다. 큐에서 어떤 계정이든 다음 차례로 뽑힐 때마다, 그 시점에 실패 이력이 있는 계정들도 먼저 재조회를 시도합니다. 실패가 반복돼도 포기하지 않고 계속 재시도합니다(5회부터는 로그 심각도만 올라감).
- **스크립트를 새로 실행할 때마다(재시작 포함) 모든 상태를 처음부터 다시 계산합니다** — 구독 여부, 로그인 이메일, 5시간/주간 한도, 다음 실행 시각까지 전부 실시간으로 다시 조회한 뒤 새 큐로 시작합니다. `.schedule/queue.json`에 저장된 이전 스케줄은 읽지 않습니다. (같은 계정 번호를 다른 사람이 넘겨받아 이메일이 바뀌는 경우, 이전 소유자 기준으로 저장된 실행 시각을 그대로 물려받는 걸 막기 위한 설계입니다.) `-w`/`-d`로 시각을 직접 지정하면 그 값이 우선하고, 아무것도 지정하지 않으면 방금 조회한 한도를 기준으로 다음 실행 시각을 계산합니다(5시간/주간 리셋 중 알맞은 쪽 + 1분, 리셋 정보가 없으면 `--interval` 뒤). `.schedule/queue.json`은 이렇게 계산된 상태를 실행 도중(같은 프로세스 안에서) 매 주기 저장해 두는 용도로만 쓰이며, 스케줄러가 도중에 죽었다가 프로세스를 재시작하면 최신 데이터로 다시 시작합니다.
- 모든 Claude 계정(user1 포함)은 `~/.claude`의 설정을 공유합니다. `settings.json`, `CLAUDE.md`, `skills`, `plugins`, `commands`, `agents`, `hud`, `statusline*`은 계정 폴더 안에 `~/.claude`로의 심볼릭 링크로 연결되어 `~/.claude` 쪽 변경이 모든 계정에 즉시 반영됩니다(기존 파일은 `.pre-shared-<시각>/`에 백업). user 범위 MCP 서버(`~/.claude.json`의 `mcpServers`)는 로그인 정보와 같은 파일에 있어 링크할 수 없으므로 스케줄러 시작 시와 `--add-account` 시 각 계정의 `.claude.json`으로 복사됩니다. claude.ai 커넥터(Google Drive 등)는 계정 단위라 공유되지 않습니다. 스케줄러의 호출은 `--strict-mcp-config`로 MCP 서버를 띄우지 않습니다.
- 각 계정이 어느 도구(claude/codex)인지는 `.schedule/accounts.json`에 저장됩니다 — `--add-account`로 등록할 때 결정됩니다. 등록된 적 없는 계정 번호는 하위 호환을 위해 기본적으로 `claude`로 취급합니다.
- 큐를 한 바퀴 돌 때마다(계정 하나를 처리할 때마다) 전체 계정의 현재 상태를 콘솔에 출력합니다. 시작 시(재시작 포함) 각 계정의 로그인 이메일·구독 플랜·최신 한도를 한 번 조회해 채워 넣고, 이후 매 실행마다 갱신합니다. `최근 실행`은 그 계정의 실행 로그 파일명(`loop-<타임스탬프>.log`)에서 복원하므로, 스케줄러를 재시작해도 마지막으로 실행한 시각이 남습니다. 아직 실행 기록이 없는 계정은 이 줄을 출력하지 않습니다. 시각은 날짜까지 포함합니다(주간 리셋은 오늘과 최대 7일 차이 날 수 있으므로 시:분:초만으로는 언제인지 구분할 수 없기 때문입니다):
  ```
  ==========큐상태============
  1. user4 [codex]
    계정: user4@example.com(plus)
    상태: retry_pending (실패 3회)
    최근 실행: 2026-09-27 14:12:03
    다음 재시도: 2026-09-27 14:17:10
  2. user1 [claude]
    계정: user1@example.com(pro)
    상태: scheduled
    최근 실행: 2026-09-27 16:58:12
    다음 실행: 2026-09-27 17:00:59
    5시간 한도: 42% (리셋 2026-09-27 17:00:59)
    주간 한도: 17% (리셋 2026-10-04 09:30:00)
  3. user2 [claude]
    상태: free_skip
    최근 실행: 2026-09-27 15:03:44
    다음 구독 확인: 2026-09-27 15:09:49
  =========================
  ```
- 종료는 `Ctrl+C`(KeyboardInterrupt)로만 가능합니다 — 정상 동작 중에는 큐가 저절로 비어 루프가 끝나는 일이 없습니다.

## 계정 관리

계정 번호와 실제 설정 디렉터리 매핑 (도구별로 별도):

| 계정 번호 | claude 설정 디렉터리 | codex 설정 디렉터리 |
|---|---|---|
| `N` (1 포함) | `~/.usage-reset-scheduler/accounts/claude-N` | `~/.usage-reset-scheduler/accounts/codex-N` |

모든 계정이 같은 규칙을 쓰며, `user1`도 예외가 아닙니다. **`~/.claude`와 `~/.codex`는 스케줄러 계정이 아닙니다** — 사용자가 직접 `claude`/`codex`를 실행할 때 쓰는 폴더이므로 스케줄러가 쓰지 않습니다(공유 설정의 원본으로만 읽습니다). 이렇게 분리하면 스케줄러의 일회성 `-p` 호출이 사용자의 인증 정보·세션 기록·실행 로그와 섞이지 않습니다. 같은 `.credentials.json`을 두 프로세스가 각자 갱신하다 토큰이 무효화되는 일도 막습니다.

같은 계정 번호를 claude와 codex 양쪽에 동시에 등록할 수는 없습니다 — 번호 하나는 등록 시점에 결정된 도구 하나에만 대응합니다.

```bash
# 계정 추가 — 도구를 대화형으로 선택
python3 scheduler.py --add-account 4      # 또는: python3 scheduler.py -a 4
# 사용할 도구를 선택하세요:
# 1. claude (기본값)
# 2. codex
# 입력 (Enter=1):

# 등록된 계정, 도구, 구독 상태, 로그인 이메일 목록 확인
python3 scheduler.py --list-accounts      # 또는: python3 scheduler.py -l

# 계정 제거 (완전 삭제가 아니라 타임스탬프 붙은 이름으로 백업 이동)
python3 scheduler.py --remove-account 2   # 또는: python3 scheduler.py -r 2
```

**`--add-account`에 이미 등록된 번호를 다시 넘기면?**
디렉터리를 삭제하고 새로 만드는 것이 아니라, 기존 디렉터리에 그대로 로그인을 다시 실행합니다(claude는 `claude auth login`, codex는 `codex login`). 즉 로그인 자격증명만 갱신되고, 그 외 캐시나 설정 파일은 유지됩니다. 도구 선택 프롬프트가 다시 뜨며, 여기서 이전과 다른 도구를 고르면 등록된 도구 자체도 바뀝니다. 완전히 초기화하려면 `--remove-account`로 백업 이동시킨 뒤 `--add-account`로 새로 만드세요.

**`user1`은 `--remove-account`로 제거할 수 없습니다** — 기본 계정 보호를 위해 항상 거부됩니다.

`--list-accounts` 출력 예:
```
user1    [claude] pro          user1@example.com         ~/.usage-reset-scheduler/accounts/claude-1
user2    [claude] pro          user2@example.com      ~/.usage-reset-scheduler/accounts/claude-2
user4    [codex ] plus         user4@example.com            ~/.usage-reset-scheduler/accounts/codex-4
user5    [claude] SKIP: 인증 상태 확인 실패 -                            ~/.usage-reset-scheduler/accounts/claude-5
```
이메일을 확인할 수 없는 경우(미로그인, 조회 실패 등)에는 `-`로 표시됩니다.

## 구독 상태 및 한도 확인하기

도구를 실제로 호출하지 않고 구독 상태와 현재 5시간/주간 한도 사용률·리셋 시각을 보고 싶을 때:

```bash
python3 scheduler.py 1 2 4 --check-subscription   # 또는: python3 scheduler.py 1 2 4 -c
```

출력 예:
```
[user1] [claude] 유료 구독 확인: user1@example.com(pro)
[user1]   5시간 한도: 42% 사용, 리셋 2026-09-27 17:00:59
[user1]   주간 한도: 17% 사용, 리셋 2026-10-03 09:30:00
[user2] [claude] SKIP: 유료 구독이 아님 (subscriptionType=free)
[user4] [codex] 유료 구독 확인: user4@example.com(plus)
[user4]   5시간 한도: 71% 사용, 리셋 2026-09-27 19:08:37
[user4]   주간 한도: 27% 사용, 리셋 2026-10-04 08:56:55
```

무료/미로그인 계정(`SKIP`)은 한도 조회를 건너뜁니다. 유료 구독이 확인됐지만 한도 API 조회 자체가 실패하면(`사용량 조회 실패: ...`) 그 줄만 건너뛰고 다음 계정으로 진행합니다.

## 작업 루프 (`--project-loop`)

리셋용 "OK" 호출 대신, 대상 프로젝트의 프롬프트(`prompt.md`)를 실제로 실행하는 모드입니다. `~/shortform-ai`의 `run-step-loop-claude.sh`/`run-step-loop.sh`를 여러 계정으로 이어서 돌리는 것과 같습니다.

```bash
python3 scheduler.py --project-loop --project-dir ~/shortform-ai -n 4
python3 scheduler.py --project-loop --project-dir ~/shortform-ai 2 3 4 --prompt-file prompt.md
```

동작:
- 한 계정으로 계속 실행합니다. 출력의 `STEP_STATUS: COMPLETE` / `FAILURE_ANALYSIS` / `FAILURE_IMPROVEMENT`를 보면 같은 계정으로 다음 실행을 이어갑니다.
- 한도에 걸리면(claude: `rate_limit_event` rejected / 429, codex: `turn.failed`의 "usage limit") 그 계정을 리셋 시각 + 1분까지 쉬게 하고, 쓸 수 있는 다음 계정이 **같은 step을 처음부터** 다시 실행합니다. 진행 상태는 `PHASE.md`, worklog, 작업 트리에 남아 있습니다.
- 계정을 쓰기 직전마다 사용량을 다시 조회해, 그 사이 한도가 찬 계정은 건너뜁니다.
- 쓸 수 있는 계정이 없으면 가장 빠른 리셋 시각까지 기다렸다가 재개합니다.
- 로그인 만료 같은 계정 오류는 그 계정만 빼 두고 60분마다 다시 확인합니다.
- 종료: `STEP_STATUS: INCOMPLETE`(권한/승인 문제라 계정을 바꿔도 해결되지 않음), 완료 상태도 한도 신호도 없는 실패가 3회 연속, 또는 `Ctrl+C`(종료 코드 130).

모델: `--model`/`--effort`는 **claude 계정에만** 적용되며, 이 모드의 기본값은 `claude-opus-5-5` / `high`입니다. codex 계정은 codex CLI 기본 설정으로 실행합니다.

로그: `<프로젝트>/.claude-runs/` 또는 `.codex-runs/`에 `step-<타임스탬프>-user<N>.log`(텍스트, 기존 스크립트 로그와 같은 형식)와 `step-<타임스탬프>-user<N>.raw.jsonl`(CLI 원본 출력)이 남습니다.

동시 실행:
- 같은 프로젝트에 작업 루프를 두 번 띄우면 두 번째는 시작하지 않습니다(프로젝트별 잠금, `.schedule/locks/`). 다른 프로젝트끼리는 동시에 돌릴 수 있습니다.
- 리셋 스케줄러(`python3 scheduler.py -n 4`)와 작업 루프는 동시에 돌려도 됩니다. claude는 토큰 갱신을 CLI가 잠금으로 순서를 맞추고, codex는 인증 실패 시 `auth.json`을 다시 읽습니다. codex에서 드물게 "refresh token was already used"가 나면 `-a N`으로 다시 로그인하세요.
- 작업 루프가 도는 동안 **같은 프로젝트에서 기존 `run-step-loop*.sh`를 돌리거나 직접 편집하지 마세요.** 이 잠금으로는 막지 못합니다.
- 작업 루프는 `.schedule/queue.json`을 읽거나 쓰지 않으므로, 리셋 스케줄러의 "최근 실행"에는 작업 루프의 실행이 표시되지 않습니다.

## 전체 옵션

```
python3 scheduler.py [계정번호 ...] [옵션]
```

| 옵션 | 짧은 형태 | 기본값 | 설명 |
|---|---|---|---|
| `--model MODEL` | — | 계정 도구별 기본값 | 호출할 모델. 생략 시 claude는 `claude-haiku-4-5`, codex는 `gpt-6-luna` |
| `--effort EFFORT` | — | `low` | reasoning effort |
| `--wait-until TIME` | `-w` | (즉시) | 최초 실행 시각. `HH:MM` 또는 `YYYY-MM-DD HH:MM`. 과거 시각은 에러로 거부됩니다 |
| `--delay N` | `-d` | `0` | 최초 실행 전 추가 대기 시간(분) |
| `--interval N` | — | `5` | 사용량 조회 실패 시 재시도 간격(분) |
| `--threshold N` | — | `100` | 주간 한도 소진 판정 기준(%) |
| `--check-subscription` | `-c` | — | 구독 상태만 확인하고 종료 (도구 호출 없음) |
| `--add-account N` | `-a` | — | `userN` 등록/재로그인 (N은 2 이상). 도구를 대화형으로 선택 |
| `--list-accounts` | `-l` | — | 등록 계정, 도구, 구독 상태 표시 |
| `--remove-account N` | `-r` | — | `userN`을 백업 이름으로 이동 (복구 가능, `user1`은 불가) |
| `--count N` | `-n` | — | 계정 번호 나열 대신, 존재하는 모든 계정(account 1 포함, `--list-accounts`와 동일한 기준)을 1번부터 순서대로 실시간 확인해 유료 구독 계정만 최대 N개 자동 선택. 계정이 N개보다 적으면 있는 만큼만 선택. 명시적 계정번호나 다른 계정 관리 옵션(`-a`/`-l`/`-r`/`-c`)과 함께 쓸 수 없음 |
| `--project-loop` | — | — | 작업 루프 모드. 여러 계정을 번갈아 쓰며 `--project-dir`의 프롬프트를 반복 실행 (위 "작업 루프" 참고). `-a`/`-l`/`-r`/`-c`와 함께 쓸 수 없음 |
| `--project-dir DIR` | — | — | `--project-loop` 대상 프로젝트. git 저장소여야 함 |
| `--prompt-file FILE` | — | `prompt.md` | `--project-dir` 기준 프롬프트 파일. 매 실행마다 새로 읽음 |

계정 번호를 여러 개 지정하면 중복은 제거되고 처음 등장한 순서가 유지됩니다. `--model`/`--effort`를 명시하면 그 실행에 포함된 **모든** 계정(도구 무관)에 동일하게 적용됩니다 — claude와 codex를 같은 실행에 섞을 때 서로 다른 모델을 강제로 맞추고 싶은 게 아니라면 보통 생략하는 편이 안전합니다.

## 로그 및 상태 파일

- 실행 로그: `<계정 설정 디렉터리>/start-limit-runs/loop-<타임스탬프>.log`
- 스케줄 상태: `<프로젝트 루트>/.schedule/queue.json` (git 추적 대상 아님)
- 계정 ↔ 도구 매핑: `<프로젝트 루트>/.schedule/accounts.json` (git 추적 대상 아님)
- 작업 루프 로그: `<대상 프로젝트>/.claude-runs/` 또는 `.codex-runs/`의 `step-<타임스탬프>-user<N>.log` / `.raw.jsonl`
- 작업 루프 잠금: `<프로젝트 루트>/.schedule/locks/project-<해시>.lock` (git 추적 대상 아님)
