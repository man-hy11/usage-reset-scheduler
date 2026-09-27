# 설계: Claude 다중 계정 우선순위 큐 스케줄러 (Python 전환)

## 배경

현재 `run-step-loop-claude.sh`는 계정을 여러 개 지정하면(`1 2 3`) 각 계정마다
자기 자신을 재귀 호출해 완전히 독립된 백그라운드 프로세스로 띄운다. 각 프로세스는
자기만의 무한루프를 돌며 사용량을 조회하고 다음 실행 시각을 계산해 `sleep`한다.
계정 간 조율은 없고, 중앙에서 "가장 가까운 시각의 계정을 골라 실행"하는 개념도 없다.

사용자는 이를 다음과 같은 구조로 바꾸길 원한다:

- 여러 계정의 다음 실행 시각을 하나의 우선순위 큐로 관리하는 단일 컨트롤러
- `claude` 호출 자체는 완전 순차 실행(한 번에 한 계정만)
- 무료 플랜 계정은 최초 확인 후 큐에서 영구 제외, 재확인 없음
- 사용량 조회(API) 실패는 계정별로 최대 5회까지 재시도하되, 재시도는 "큐에서 어떤
  계정이 pop될 때마다 그 시점의 실패 이력 있는 계정들을 먼저 훑어본다"는 방식으로 처리

동시에 유지보수성을 높이기 위해 bash + jq + python heredoc이 뒤섞인 현재 구현을
Python으로 통일한다.

## 목표

- 계정별 독립 프로세스 구조를 단일 컨트롤러 + 우선순위 큐 구조로 전환
- Claude 호출은 완전 순차 실행
- 무료 플랜 자동/영구 제외
- 사용량 조회 실패에 대한 계정별 재시도(최대 5회, pop 시점마다 재도전)
- bash/jq/python 혼합 구현을 순수 Python으로 통일 (표준 라이브러리 위주)
- 기존 계정 관리 CLI(`--add-account`, `--list-accounts`, `--remove-account`)와
  옵션 인터페이스(`--wait-until`, `--delay`, `--interval`, `--threshold`,
  `--model`, `--effort`, `--check-subscription`)는 그대로 유지
- 기존 계정 자격증명 위치(`~/.claude`, `~/.claude-account-N`)는 변경하지 않는다
  (이미 로그인되어 있는 계정 2, 3번을 그대로 활용)

## 비목표

- Codex용 `run-step-loop.sh`는 이번 작업에서 다루지 않는다 (별도 작업)
- Windows 네이티브 환경에서의 실제 검증은 하지 않는다 (이 세션은 WSL2이며 Windows
  실행 환경이 없다). 다만 코드는 `pathlib`/`subprocess`/`time`/`heapq` 등 표준
  라이브러리로만 작성해 이식 가능성을 열어둔다. `claude`/`codex` CLI 자체가
  Windows에서 `CLAUDE_CONFIG_DIR` 등 환경변수 규칙을 동일하게 지원하는지는 CLI
  벤더의 구현에 달려 있으며 이 스펙의 통제 범위 밖이다.

## 아키텍처

### 파일 구조

```
start_limit/
  scheduler.py          # 진입점: CLI 파싱, 우선순위 큐 메인 루프
  accounts.py            # 계정 관리(add/list/remove) + 유료 구독 게이트
  usage.py               # 사용량 API 조회 + 다음 실행 시각 계산 (collect-usage.sh 대체)
  .schedule/
    queue.json            # 영속 스케줄 상태 (프로젝트 로컬, git 비대상)
  tests/
    test_usage.py
    test_scheduler.py
    test_accounts.py
```

`run-step-loop-claude.sh`와 관련 bash 테스트 (`test-loop-independence.sh`,
`test-claude-multi-account.sh`, `test-claude-paid-gate.sh`,
`test-claude-account-manager.sh`, `test-collect-usage.sh`) 및 모의 CLI
(`tests/bin/claude`, `tests/bin/curl`)는 삭제한다. `run-step-loop.sh`(Codex)는
유지한다.

**주의**: `collect-usage.sh`는 `run-step-loop.sh`(Codex, 유지 대상)가 여전히
`COLLECT_USAGE="$SCRIPT_DIR/collect-usage.sh"`로 참조하고 있으므로 삭제하지
않고 그대로 유지한다. `usage.py`는 Claude 스케줄러 전용 신규 파일로, Codex 경로의
`collect-usage.sh`와 병존한다 (중복이지만 이번 스코프에서 Codex 쪽을 건드리지
않기로 했으므로 의도적으로 남긴다).

### 큐 상태 스키마 (`.schedule/queue.json`)

```json
{
  "1": {"next_run_at": 1758999999, "status": "scheduled", "fail_count": 0},
  "2": {"next_run_at": null, "status": "free_skip", "fail_count": 0},
  "3": {"next_run_at": 1759000100, "status": "retry_pending", "fail_count": 3}
}
```

필드:

- `next_run_at`: 다음 실행 예정 unix epoch. `free_skip`이면 `null`.
- `status`: `scheduled` (정상 대기) / `retry_pending` (사용량 조회 실패, 재시도
  대상) / `free_skip` (무료·미로그인 플랜, 영구 제외)
- `fail_count`: 연속 조회 실패 횟수. 성공하면 0으로 리셋. 5 이상이면 해당 라운드는
  건너뛰지만 계속 큐에 남아 다음 라운드에 재도전한다 (영구 포기 없음, 5는 매
  pop마다 재시도할지 말지를 가르는 소프트 임계치가 아니라 "재시도 시도 자체는
  계속하되 5회 이상 실패했다는 사실을 로그에 남기기 위한" 카운터로 취급한다 — 즉
  5 도달 이후에도 재시도는 계속되며, 그때부터는 매번 실패 로그 레벨을 warning으로
  올린다).

### 메인 루프 (`scheduler.py`)

1. **초기화**
   - 지정된 각 계정에 대해 `accounts.check_paid_subscription()` 1회 호출
   - 무료/미로그인/조회실패 → `status=free_skip`, 큐에서 즉시 배제 (이후 어떤
     루프 단계에서도 다시 확인하지 않는다)
   - 유료 계정 → 초기 `next_run_at` 계산: `--wait-until`이 있으면 그 시각,
     없으면 `--delay` 반영 후 즉시(now)
   - `heapq`에 `(next_run_at, account_id)` 형태로 push

2. **루프** (힙이 빌 때까지 반복; 힙이 비면 전 계정이 `free_skip`이라는 뜻이므로
   종료):
   1. 힙에서 최소 `next_run_at`을 가진 `(t, account_id)`를 pop
   2. **재시도 우선 처리**: 큐 상태에서 `status == "retry_pending"`인 모든 계정을
      순회하며 (pop된 계정 자신 포함 가능) 사용량 조회를 즉시 재시도한다.
      - 성공: `status=scheduled`, `fail_count=0`, 새 `next_run_at` 계산 후 힙에
        push
      - 실패: `fail_count += 1`, `next_run_at = now + interval`로 갱신 후 다시
        push. (5 이상이어도 계속 시도하며 로그 레벨만 올린다.)
   3. pop된 계정이 위 재시도 처리로 이미 `scheduled`로 상태가 바뀌어 새 시각이
      미래라면, 그 계정은 이번에는 실행하지 않고 다시 힙에 맡긴다 (즉시 continue).
   4. pop된 계정의 `next_run_at`까지 `time.sleep`
   5. 그 계정으로 `claude` 프로세스를 실행 (subprocess, 순차 — 동시에 다른 계정
      프로세스를 실행하지 않는다)
   6. 실행 후 `usage.py`로 사용량 재조회
      - 성공: `status=scheduled`, `fail_count=0`, 새 `next_run_at` 계산, push
      - 실패: `status=retry_pending`, `fail_count=1`(또는 필드가 이미 있으면
        +1), `next_run_at = now + interval`, push
   7. 큐 상태를 `.schedule/queue.json`에 저장 (매 반복마다 영속화하여 중단 시
      복구 가능하게 한다)

3. **종료 처리**: `KeyboardInterrupt` 수신 시 현재 큐 상태를 저장하고 정상 종료.

### `usage.py`

`collect-usage.sh`의 역할을 그대로 이식:

- `fetch_claude_usage(config_dir: Path) -> dict`: `.credentials.json`에서 토큰을
  읽어 `https://api.anthropic.com/api/oauth/usage` 호출, 정규화된 dict 반환
  (`five_hour_used_percent`, `five_hour_reset_at`, `weekly_used_percent`,
  `weekly_reset_at`)
- `compute_next_run(status: dict, threshold: float, fallback_min: int) -> int`:
  주간 사용률이 threshold 이상이면 주간 리셋+60초, 아니면 5시간 리셋+60초. 값이
  없거나 계산 불가하면 `fallback_min * 60`.
- HTTP 호출은 표준 라이브러리(`urllib.request`) 또는 이미 프로젝트에 있다면
  `requests`를 사용한다 (구현 단계에서 확인).

### `accounts.py`

기존 bash 로직을 그대로 이식:

- `account_dir(account_id: int) -> Path`: `1`이면 `Path.home() / ".claude"`,
  그 외는 `Path.home() / f".claude-account-{account_id}"`
- `add_account(n)`, `list_accounts()`, `remove_account(n)`: 기존 동작(등록,
  목록+구독상태 표시, 타임스탬프 백업 이동, user1 보호) 동일하게 유지
- `check_paid_subscription(config_dir: Path) -> tuple[bool, str]`: `claude auth
  status --json` 실행 후 `loggedIn`, `authMethod`, `subscriptionType` 검사
  (pro/max/team/enterprise만 통과)

### CLI 인터페이스 (`scheduler.py`)

기존 `run-step-loop-claude.sh`와 동일한 플래그를 유지한다:

```
scheduler.py [계정번호 ...] [--model M] [--effort E] [-w/--wait-until T]
             [-d/--delay N] [--interval N] [--threshold N]
             [--check-subscription]
             [--add-account N | --list-accounts | --remove-account N]
```

계정번호를 여러 개 주면(`1 2 3`) 기존처럼 여러 계정을 대상으로 하되, 이제는
백그라운드 프로세스를 여러 개 띄우는 대신 단일 프로세스 안의 우선순위 큐로
처리한다.

## 에러 처리

- `claude` 호출 자체 실패(비정상 종료 코드): 로그만 남기고 사용량 재조회 단계로
  진행 (기존과 동일한 관용적 처리 — 사용량 조회 결과에 따라 다음 시각이 결정됨)
- 사용량 조회 실패: 위 재시도 로직(`retry_pending`, 최대 소프트 임계치 5회)
- 큐 파일이 손상되었거나 없으면 빈 상태로 새로 시작 (초기화 단계 재실행)

## 테스트 전략

- `tests/test_usage.py`: API 응답 mock으로 `compute_next_run`의 주간/5시간 우선
  순위, 폴백 케이스 검증 (`test-collect-usage.sh` 대체)
- `tests/test_accounts.py`: `claude auth status --json` mock으로 유료/무료/
  미로그인/API 인증 판정 검증 (`test-claude-paid-gate.sh`,
  `test-claude-multi-account.sh`, `test-claude-account-manager.sh` 대체)
- `tests/test_scheduler.py`:
  - 여러 계정 중 무료 플랜이 최초 1회 확인 후 큐에서 배제되고 이후 재확인되지
    않는지
  - 가장 이른 `next_run_at`을 가진 계정이 먼저 pop되는지 (우선순위 큐 정합성)
  - 사용량 조회 실패 계정이 `retry_pending`으로 전환되고, 이후 다른 계정이
    pop되는 시점마다 재시도되는지
  - `fail_count`가 5를 넘어도 재시도가 계속되는지(포기하지 않음)
  - `claude` 호출은 항상 한 번에 하나씩만 실행되는지(순차성 검증 — mock으로
    동시 호출 여부 감지)
- pytest 사용, mock으로 실제 API 호출/서브프로세스 대체

## 마이그레이션

- 삭제: `run-step-loop-claude.sh`,
  `tests/test-loop-independence.sh`, `tests/test-claude-multi-account.sh`,
  `tests/test-claude-paid-gate.sh`, `tests/test-claude-account-manager.sh`,
  `tests/test-collect-usage.sh`, `tests/bin/claude`, `tests/bin/curl`,
  `tests/fixtures/usage*.json` (pytest fixture로 대체)
- 유지 (삭제하지 않음): `collect-usage.sh` — `run-step-loop.sh`(Codex)가 계속
  참조하므로 남긴다.
- 유지: `run-step-loop.sh` (Codex, 별도 작업)
- 신규: `scheduler.py`, `accounts.py`, `usage.py`, `tests/test_*.py`,
  `.schedule/`를 `.gitignore`에 추가 (큐 파일은 런타임 상태이므로 커밋 대상 아님)

## 롤백

`run-step-loop-claude.sh`와 관련 bash 테스트를 git 이력에서 복원하고
`scheduler.py`/`accounts.py`/`usage.py`와 `.schedule/`를 제거한다.
`collect-usage.sh`는 애초에 삭제하지 않으므로 롤백 대상이 아니다.
