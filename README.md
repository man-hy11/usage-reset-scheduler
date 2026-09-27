# usage-reset-scheduler

Claude Code(`claude` CLI)와 Codex CLI(`codex`)를 여러 계정에 걸쳐 최소 호출로 반복 실행해, 사용량 창(5시간 / 주간)을 리셋 직후 다시 시작시키는 스케줄러입니다. 도구가 섞여 있어도(Claude 계정 + Codex 계정) **하나의 우선순위 큐**로 관리합니다.

## 요구 사항

- Python 3.10+
- `requests` (`pip install requests`)
- 사용할 도구의 CLI가 PATH에 있고, 최소 한 번은 로그인되어 있을 것
  - Claude: `claude auth login`
  - Codex: `codex login`

## 빠른 시작

```bash
# 계정 미지정 시 user1(~/.claude, claude 도구)을 대상으로 실행
python3 scheduler.py

# 여러 계정을 동시에 스케줄에 등록 (순차 실행, 동시에 두 계정을 실행하지 않음)
# 각 계정은 claude일 수도 codex일 수도 있다 — 등록된 도구를 따른다
python3 scheduler.py 1 2 3 4

# 특정 시각에 최초 실행 시작
python3 scheduler.py 1 -w "14:00"
python3 scheduler.py 1 -w "2026-10-01 09:00"
```

## 동작 방식 (요약)

- 지정한 각 계정의 구독 상태를 시작 시 한 번만 확인합니다. 유료 플랜만 통과하고, 무료·미로그인·조회실패 계정은 그 실행 동안 큐에서 **영구 제외**됩니다(재확인 없음).
  - Claude: Pro/Max/Team/Enterprise
  - Codex: Plus/Pro/Team/Business/Enterprise
- 남은 계정들은 우선순위 큐(다음 실행 예정 시각 기준)로 관리되며, 도구 호출(`claude`/`codex`)은 **한 번에 한 계정씩 순차로만** 실행됩니다. claude 계정과 codex 계정이 섞여 있어도 같은 큐 안에서 순서대로 처리됩니다.
- 호출 후 사용량을 재조회해 다음 실행 시각을 계산합니다: 주간 한도가 임계치 이상 소진됐으면 주간 리셋 + 1분, 그 외에는 5시간 리셋 + 1분.
- 사용량 조회가 실패한 계정은 `retry_pending` 상태로 남아 `--interval`분 간격으로 재시도합니다. 큐에서 어떤 계정이든 다음 차례로 뽑힐 때마다, 그 시점에 실패 이력이 있는 계정들도 먼저 재조회를 시도합니다. 실패가 반복돼도 포기하지 않고 계속 재시도합니다(5회부터는 로그 심각도만 올라감).
- 스케줄 상태는 `.schedule/queue.json`에 매 주기 저장되며, 재시작 시 이 파일을 읽어 기존 스케줄을 복원합니다(무료 제외 판정은 계정별로 매번 새로 하지 않고, 저장된 `scheduled`/`retry_pending` 상태를 그대로 이어받습니다).
- 각 계정이 어느 도구(claude/codex)인지는 `.schedule/accounts.json`에 저장됩니다 — `--add-account`로 등록할 때 결정됩니다. 등록된 적 없는 계정 번호는 하위 호환을 위해 기본적으로 `claude`로 취급합니다.
- 큐를 한 바퀴 돌 때마다(계정 하나를 처리할 때마다) 전체 계정의 현재 상태를 콘솔에 출력합니다. 시작 시(재시작 포함) 각 계정의 로그인 이메일·구독 플랜·최신 한도를 한 번 조회해 채워 넣고, 이후 매 실행마다 갱신합니다. 시각은 날짜까지 포함합니다(주간 리셋은 오늘과 최대 7일 차이 날 수 있으므로 시:분:초만으로는 언제인지 구분할 수 없기 때문입니다):
  ```
  ==========큐상태============
  1. user1 [claude]
    계정: user1@example.com(pro)
    상태: scheduled
    다음 실행: 2026-09-27 17:00:59
    5시간 한도: 42% (리셋 2026-09-27 17:00:59)
    주간 한도: 17% (리셋 2026-10-04 09:30:00)
  2. user2 [claude]
    상태: free_skip
  3. user4 [codex]
    계정: user4@example.com(plus)
    상태: retry_pending (실패 3회)
    다음 재시도: 2026-09-27 14:17:10
  =========================
  ```
- 종료는 `Ctrl+C`(KeyboardInterrupt)로만 가능합니다 — 정상 동작 중에는 큐가 저절로 비어 루프가 끝나는 일이 없습니다.

## 계정 관리

계정 번호와 실제 설정 디렉터리 매핑 (도구별로 별도):

| 계정 번호 | claude 설정 디렉터리 | codex 설정 디렉터리 |
|---|---|---|
| `1` | `~/.claude` | `~/.codex` |
| `N` (2 이상) | `~/.claude-account-N` | `~/.codex-account-N` |

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
user1    [claude] pro          user1@example.com         ~/.claude
user2    [claude] pro          user2@example.com      ~/.claude-account-2
user4    [codex ] plus         user4@example.com            ~/.codex-account-4
user5    [claude] SKIP: 인증 상태 확인 실패 -                            ~/.claude-account-5
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

계정 번호를 여러 개 지정하면 중복은 제거되고 처음 등장한 순서가 유지됩니다. `--model`/`--effort`를 명시하면 그 실행에 포함된 **모든** 계정(도구 무관)에 동일하게 적용됩니다 — claude와 codex를 같은 실행에 섞을 때 서로 다른 모델을 강제로 맞추고 싶은 게 아니라면 보통 생략하는 편이 안전합니다.

## 로그 및 상태 파일

- 실행 로그: `<계정 설정 디렉터리>/start-limit-runs/loop-<타임스탬프>.log`
- 스케줄 상태: `<프로젝트 루트>/.schedule/queue.json` (git 추적 대상 아님)
- 계정 ↔ 도구 매핑: `<프로젝트 루트>/.schedule/accounts.json` (git 추적 대상 아님)
