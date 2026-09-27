# start_limit

Claude Code(`claude` CLI)를 여러 계정에 걸쳐 최소 호출로 반복 실행해, 사용량 창(5시간 / 주간)을 리셋 직후 다시 시작시키는 스케줄러입니다.

Codex용 `run-step-loop.sh`는 별개의 독립 스크립트입니다. 이 문서는 Claude용 `scheduler.py`를 다룹니다.

## 요구 사항

- Python 3.10+
- `requests` (`pip install requests`)
- `claude` CLI가 PATH에 있고, 최소 한 번은 `claude auth login`으로 로그인되어 있을 것

## 빠른 시작

```bash
# 계정 미지정 시 user1(~/.claude)을 대상으로 실행
python3 scheduler.py

# 여러 계정을 동시에 스케줄에 등록 (순차 실행, 동시에 두 계정을 실행하지 않음)
python3 scheduler.py 1 2 3

# 특정 시각에 최초 실행 시작
python3 scheduler.py 1 -w "14:00"
python3 scheduler.py 1 -w "2026-10-01 09:00"
```

## 동작 방식 (요약)

- 지정한 각 계정의 구독 상태를 시작 시 한 번만 확인합니다. Pro/Max/Team/Enterprise만 통과하고, 무료·미로그인·조회실패 계정은 그 실행 동안 큐에서 **영구 제외**됩니다(재확인 없음).
- 남은 계정들은 우선순위 큐(다음 실행 예정 시각 기준)로 관리되며, `claude` 호출은 **한 번에 한 계정씩 순차로만** 실행됩니다.
- 호출 후 사용량을 재조회해 다음 실행 시각을 계산합니다: 주간 한도가 임계치 이상 소진됐으면 주간 리셋 + 1분, 그 외에는 5시간 리셋 + 1분.
- 사용량 조회가 실패한 계정은 `retry_pending` 상태로 남아 `--interval`분 간격으로 재시도합니다. 큐에서 어떤 계정이든 다음 차례로 뽑힐 때마다, 그 시점에 실패 이력이 있는 계정들도 먼저 재조회를 시도합니다. 실패가 반복돼도 포기하지 않고 계속 재시도합니다(5회부터는 로그 심각도만 올라감).
- 스케줄 상태는 `.schedule/queue.json`에 매 주기 저장되며, 재시작 시 이 파일을 읽어 기존 스케줄을 복원합니다(무료 제외 판정은 계정별로 매번 새로 하지 않고, 저장된 `scheduled`/`retry_pending` 상태를 그대로 이어받습니다).
- 큐를 한 바퀴 돌 때마다(계정 하나를 처리할 때마다) 전체 계정의 현재 상태를 콘솔에 출력합니다. 시작 시(재시작 포함) 각 계정의 로그인 이메일과 최신 한도를 한 번 조회해 채워 넣고, 이후 매 실행마다 갱신합니다. 시각은 날짜까지 포함합니다(주간 리셋은 오늘과 최대 7일 차이 날 수 있으므로 시:분:초만으로는 언제인지 구분할 수 없기 때문입니다):
  ```
  ==========큐상태============
  1. user1
    계정: user1@example.com
    상태: scheduled
    다음 실행: 2026-09-27 17:00:59
    5시간 한도: 42% (리셋 2026-09-27 17:00:59)
    주간 한도: 17% (리셋 2026-10-04 09:30:00)
  2. user2
    상태: free_skip
  3. user3
    계정: other@example.com
    상태: retry_pending (실패 3회)
    다음 재시도: 2026-09-27 14:17:10
  =========================
  ```
- 종료는 `Ctrl+C`(KeyboardInterrupt)로만 가능합니다 — 정상 동작 중에는 큐가 저절로 비어 루프가 끝나는 일이 없습니다.

## 계정 관리

계정 번호와 실제 설정 디렉터리 매핑:

| 계정 번호 | 설정 디렉터리 |
|---|---|
| `1` | `~/.claude` (기본 계정, 별도 등록 불필요) |
| `N` (2 이상) | `~/.claude-account-N` |

```bash
# 계정 추가/재로그인
python3 scheduler.py --add-account 2      # 또는: python3 scheduler.py -a 2

# 등록된 계정, 구독 상태, 로그인 이메일 목록 확인
python3 scheduler.py --list-accounts      # 또는: python3 scheduler.py -l

# 계정 제거 (완전 삭제가 아니라 타임스탬프 붙은 이름으로 백업 이동)
python3 scheduler.py --remove-account 2   # 또는: python3 scheduler.py -r 2
```

**`--add-account`에 이미 등록된 번호를 다시 넘기면?**
디렉터리를 삭제하고 새로 만드는 것이 아니라, 기존 디렉터리에 그대로 `claude auth login`을 다시 실행합니다. 즉 로그인 자격증명만 갱신되고, 그 외 캐시나 설정 파일은 유지됩니다. 완전히 초기화하려면 `--remove-account`로 백업 이동시킨 뒤 `--add-account`로 새로 만드세요.

**`user1`은 `--remove-account`로 제거할 수 없습니다** — 기본 계정 보호를 위해 항상 거부됩니다.

`--list-accounts` 출력 예:
```
user1    pro          user1@example.com         ~/.claude
user2    pro          user2@example.com      ~/.claude-account-2
user4    SKIP: 인증 상태 확인 실패 -                            ~/.claude-account-4
```
이메일을 확인할 수 없는 경우(미로그인, 조회 실패 등)에는 `-`로 표시됩니다.

## 구독 상태 및 한도 확인하기

`claude` 호출 없이 구독 상태와 현재 5시간/주간 한도 사용률·리셋 시각을 보고 싶을 때:

```bash
python3 scheduler.py 1 2 3 --check-subscription   # 또는: python3 scheduler.py 1 2 3 -c
```

출력 예:
```
[user1] 유료 구독 확인: pro (user1@example.com)
[user1]   5시간 한도: 42% 사용, 리셋 2026-09-27 17:00:59
[user1]   주간 한도: 17% 사용, 리셋 2026-10-03 09:30:00
[user2] SKIP: 유료 Claude 구독이 아님 (subscriptionType=free)
[user3] 유료 구독 확인: max (other@example.com)
[user3]   5시간 한도: 5% 사용, 리셋 2026-09-27 12:10:00
[user3]   주간 한도: 8% 사용, 리셋 2026-10-01 00:00:00
```

무료/미로그인 계정(`SKIP`)은 한도 조회를 건너뜁니다. 유료 구독이 확인됐지만 한도 API 조회 자체가 실패하면(`사용량 조회 실패: ...`) 그 줄만 건너뛰고 다음 계정으로 진행합니다.

## 전체 옵션

```
python3 scheduler.py [계정번호 ...] [옵션]
```

| 옵션 | 짧은 형태 | 기본값 | 설명 |
|---|---|---|---|
| `--model MODEL` | — | `claude-haiku-4-5` | 호출할 Claude 모델 |
| `--effort EFFORT` | — | `low` | Claude effort |
| `--wait-until TIME` | `-w` | (즉시) | 최초 실행 시각. `HH:MM` 또는 `YYYY-MM-DD HH:MM`. 과거 시각은 에러로 거부됩니다 |
| `--delay N` | `-d` | `0` | 최초 실행 전 추가 대기 시간(분) |
| `--interval N` | — | `5` | 사용량 조회 실패 시 재시도 간격(분) |
| `--threshold N` | — | `100` | 주간 한도 소진 판정 기준(%) |
| `--check-subscription` | `-c` | — | 구독 상태만 확인하고 종료 (`claude` 호출 없음) |
| `--add-account N` | `-a` | — | `userN` 로그인 등록/재로그인 (N은 2 이상) |
| `--list-accounts` | `-l` | — | 등록 계정과 구독 상태 표시 |
| `--remove-account N` | `-r` | — | `userN`을 백업 이름으로 이동 (복구 가능, `user1`은 불가) |

계정 번호를 여러 개 지정하면 중복은 제거되고 처음 등장한 순서가 유지됩니다.

## 로그 및 상태 파일

- 실행 로그: `<계정 설정 디렉터리>/start-limit-runs/loop-<타임스탬프>.log`
- 스케줄 상태: `<프로젝트 루트>/.schedule/queue.json` (git 추적 대상 아님)
