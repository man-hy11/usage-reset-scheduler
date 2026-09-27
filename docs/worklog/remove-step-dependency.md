# 작업: Step 의존성 제거 및 리셋 직후 호출

## 개요

- 요청일: 2026-09-27
- 상태: 완료
- 목표: 프로젝트 Step 실행기가 아니라, 사용량 창 리셋 직후 최소 호출로 다음 5시간 창을 빠르게 시작하는 독립 실행기로 단순화한다.

## 계획

1. 독립 실행 및 리셋 예약 동작을 테스트로 고정한다.
2. 두 loop 스크립트에서 `shortform-ai`, `prompt.md`, `STEP_STATUS`, `--smoke` 의존성을 제거한다.
3. 스크립트 위치를 작업·로그 기준 경로로 사용하고 최소 프롬프트를 실행한다.
4. 주간 한도가 소진되지 않았다면 사용률과 관계없이 5시간 리셋 + 1분을 다음 실행으로 선택한다.
5. 구문, 도움말, 계산 테스트와 실제 사용량 조회를 검증한다.

## 범위

- 변경 예정: `run-step-loop.sh`, `run-step-loop-claude.sh`, `collect-usage.sh`, `tests/`
- 추가: 이 작업 기록
- 제외: Codex/Claude 인증 파일과 외부 프로젝트 파일 변경

## 리스크

- API 조회 실패 또는 리셋 시각 누락 시 정확한 예약이 불가능하다. 짧은 `--interval` 폴백으로 재시도한다.
- 주간 한도 소진 중 5시간 리셋만 따르면 불필요한 호출이 발생한다. 주간 사용률이 임계치 이상이면 주간 리셋을 우선한다.
- 실제 무한 loop 검증은 장시간 대기와 모델 호출 비용이 발생한다. 계산·도움말·실제 API 조회를 검증하고 전체 loop는 실행하지 않는다.

## 테스트 전략

- `tests/test-collect-usage.sh`: 5시간 리셋 예약, 주간 소진 우선, 조회 실패 폴백
- `tests/test-loop-independence.sh`: `prompt.md`, `STEP_STATUS`, `shortform-ai`, `--smoke` 의존성 부재
- `bash -n collect-usage.sh run-step-loop.sh run-step-loop-claude.sh tests/*.sh`
- `./collect-usage.sh status codex`와 `./collect-usage.sh status claude` 실제 조회 예정

## 변경 이력

### 2026-09-27 범위 확정

- 결정: 최소 프롬프트 호출 후 5시간 리셋 + 1분을 다음 실행 시각으로 사용한다.
- 사유: 목적은 작업 Step 자동화가 아니라 새 사용량 창을 리셋 직후 시작하는 것이다.
- 영향: 기존 `prompt.md` 기반 프로젝트 작업 및 `STEP_STATUS` 복구 흐름은 제거된다.

### 2026-09-27 Claude 다중 계정 지원 시작

- 상태: 완료
- 계획: `CLAUDE_CONFIG_DIR` 기준으로 인증 파일을 선택하고 계정별 실행 로그·상태 파일을 분리한다.
- 범위: `collect-usage.sh`, `run-step-loop-claude.sh`, 관련 테스트와 fixture. Codex 동작은 변경하지 않는다.
- 리스크: CLI 계정과 사용량 조회 계정이 달라지지 않도록 동일 환경변수를 두 경로 모두에서 사용해야 한다.
- 테스트: 가짜 `HOME` 기본 계정과 별도 `CLAUDE_CONFIG_DIR` 계정을 구성하고, 모의 `curl`이 별도 계정 토큰만 허용하도록 검증할 예정이다.

### 2026-09-27 Claude 다중 계정 지원 결과

- `collect-usage.sh`가 `${CLAUDE_CONFIG_DIR:-~/.claude}/.credentials.json`을 읽도록 변경했다.
- Claude 상태, 대기시간, 호출 로그를 `<CLAUDE_CONFIG_DIR>/start-limit-runs/`에 저장해 동시 실행 계정 간 충돌을 방지했다.
- `run-step-loop-claude.sh --help`에 계정별 실행 예시를 추가했다.
- `tests/test-claude-multi-account.sh`에서 기본 계정과 선택 계정 토큰을 분리한 모의 API 검증이 통과했다.
- 기존 사용량 계산, loop 독립성, 전체 Bash 구문 검사가 통과했다.
- 실제 기본 계정도 `CLAUDE_CONFIG_DIR="$HOME/.claude" ./collect-usage.sh status claude`로 정상 조회했다.
- 변경 파일: `collect-usage.sh`, `run-step-loop-claude.sh`, `tests/test-claude-multi-account.sh`, `tests/bin/curl`, 두 인증 fixture, 이 작업 기록.
- 남은 이슈: 추가 계정은 사용자가 각 디렉터리에서 `claude auth login`을 완료해야 한다.

### 2026-09-27 Claude 유료 구독 게이트 시작

- 상태: 완료
- 계획: loop 진입 전에 `claude auth status --json`을 읽고 `claude.ai` 로그인 및 유료 구독 여부를 판정한다.
- 허용 플랜: Anthropic 공식 유료 플랜인 `pro`, `max`, `team`, `enterprise`만 허용한다.
- 거부 조건: 무료, 로그아웃, API 인증, 알 수 없는 플랜, 상태 명령 실패 및 JSON 파싱 실패는 모델 호출 없이 SKIP한다.
- 테스트: 모의 Claude CLI로 네 유료 플랜, 무료 플랜, 로그아웃, API 인증, 잘못된 JSON을 검증할 예정이다.
- 범위: `run-step-loop-claude.sh`, 테스트용 모의 CLI, 구독 게이트 테스트, 이 작업 기록. Codex는 변경하지 않는다.

### 2026-09-27 Claude 유료 구독 게이트 결과

- loop의 최초 대기 전에 `claude auth status --json`을 검사하도록 구현했다.
- `loggedIn=true`, `authMethod=claude.ai`, `subscriptionType`이 `pro|max|team|enterprise`인 경우만 실행한다.
- 무료·로그아웃·API 인증·알 수 없는 플랜·상태 명령 실패·JSON 파싱 실패는 `SKIP: <사유>`를 출력하고 exit 0으로 종료한다.
- `--check-subscription` 옵션으로 모델 호출 없이 판정만 확인할 수 있다.
- 모의 CLI 기반 9개 판정 시나리오와 기존 회귀 테스트가 통과했다.
- 실제 기존 계정에서 `./run-step-loop-claude.sh --check-subscription` 실행 결과 `유료 구독 확인: pro`를 확인했다.
- 변경 파일: `run-step-loop-claude.sh`, `tests/bin/claude`, `tests/test-claude-paid-gate.sh`, 이 작업 기록.
- 남은 이슈: CLI가 향후 새로운 유료 플랜명을 추가하면 안전 우선으로 SKIP하므로 허용 목록 갱신이 필요하다.

### 2026-09-27 Claude 계정 관리자 시작

- 상태: 완료
- 계획: 숫자 기반 `userN` 등록·목록·제거와 단일/복수 계정 실행 라우팅을 `run-step-loop-claude.sh`에 추가한다.
- 계정 매핑: `user1=~/.claude`, `userN=~/.claude-account-N` (`N≥2`). 미지정 실행은 user1이다.
- 복수 실행: `1 2 3`처럼 공백으로 지정한 계정을 병렬 자식 프로세스로 실행하며 각 계정이 독립적으로 유료 구독 게이트를 통과한다.
- 제거 안전성: user1 제거는 금지하고 user2 이상은 영구 삭제하지 않고 타임스탬프가 붙은 백업 경로로 이동한다.
- 테스트: 기본 선택, 단일 선택, 복수 선택과 무료 계정 SKIP, 추가, 목록, 안전 제거, user1 보호를 모의 CLI와 임시 HOME으로 검증할 예정이다.

### 2026-09-27 Claude 계정 관리자 결과

- 계정 미지정 시 user1, 숫자 하나 지정 시 해당 계정, 숫자 여러 개 지정 시 계정별 병렬 자식 loop를 실행하도록 구현했다.
- `--add-account N`, `--list-accounts`, `--remove-account N`을 추가했다.
- user1은 기존 `~/.claude`로 고정하고 추가·제거에서 보호한다. user2 이상은 `~/.claude-account-N`을 사용한다.
- 제거는 영구 삭제하지 않고 `.removed-YYYYMMDD-HHMMSS-PID` 백업명으로 이동한다.
- 복수 실행 시 각 자식이 유료 구독을 독립 판정하므로 무료·미로그인 계정만 SKIP하고 유료 계정은 계속한다.
- `tests/test-claude-account-manager.sh`에서 기본/단일/복수 선택, 유료·무료 혼합, 추가, 목록, 두 자리 번호, 안전 제거, user1 보호를 검증했다.
- 기존 구독 게이트, 다중 계정, 사용량 계산, loop 독립성 테스트와 Bash 구문 검사가 모두 통과했다.
- 실제 `--list-accounts` 및 `1 --check-subscription` 실행에서 user1의 `pro` 상태를 확인했다.
- 변경 파일: `run-step-loop-claude.sh`, `tests/bin/claude`, `tests/test-claude-account-manager.sh`, 이 작업 기록.
- 남은 이슈: 실제 추가 계정 OAuth 로그인은 각 계정 소유자가 브라우저 인증을 완료해야 하므로 모의 CLI까지만 자동 검증했다.

## 최종 결과

- `run-step-loop.sh`와 `run-step-loop-claude.sh`에서 `shortform-ai`, `prompt.md`, `STEP_STATUS`, `--smoke` 의존성을 제거했다.
- 두 스크립트는 스크립트 디렉터리에서 `Reply with OK.`를 실행하고 로컬 `.codex-runs` 또는 `.claude-runs`에 기록한다.
- 다음 실행은 주간 소진 시 주간 리셋 + 1분, 그 외에는 5시간 리셋 + 1분으로 계산한다.
- 조회·계산 실패 폴백 기본값을 5분으로 변경했다.
- 변경 파일: `collect-usage.sh`, `run-step-loop.sh`, `run-step-loop-claude.sh`, `tests/test-collect-usage.sh`, `tests/test-loop-independence.sh`, `tests/fixtures/usage-not-exhausted.json`, 이 작업 기록.
- 테스트: 두 테스트 스크립트 통과, 전체 Bash 구문 검사 통과, Codex 및 Claude 실제 사용량 API 조회와 다음 시각 계산 통과.
- 남은 이슈: 실제 무한 loop는 모델 호출 및 장시간 대기를 발생시키므로 실행하지 않았다.
- 롤백: 실행 스크립트를 이전 `prompt.md` 기반 버전으로 복원하고 `collect-usage.sh`의 대상 선택 조건을 임계치 기반으로 되돌린다.

## 2026-09-27 우선순위 큐 스케줄러로 전환 (Python)

- 상태: 완료
- 배경: 계정을 여러 개 지정하면 `run-step-loop-claude.sh`가 계정별로 완전히 독립된
  백그라운드 프로세스를 띄우는 구조였다. 이를 단일 컨트롤러가 각 계정의 다음 실행
  시각을 `heapq` 기반 우선순위 큐로 관리하고, `claude` 호출을 완전 순차로 실행하는
  구조로 바꿨다.
- 설계 문서: `docs/superpowers/specs/2026-09-27-priority-queue-scheduler-design.md`
- 구현: `usage.py`(사용량 조회 및 다음 실행 시각 계산), `accounts.py`(계정 디렉터리
  규칙, 유료 구독 게이트, 계정 관리), `scheduler.py`(우선순위 큐 메인 루프 및 CLI
  진입점)를 Python으로 신규 작성했다.
- 무료 플랜 계정은 최초 1회 확인 후 큐에서 영구 제외하며 재확인하지 않는다.
- 사용량 조회 실패 계정은 `retry_pending` 상태로 유지되며, 큐에서 어떤 계정이든
  pop될 때마다 실패 이력 있는 계정들을 먼저 재조회한다. 5회 이상 연속 실패해도
  포기하지 않고 계속 재시도하되 로그 심각도만 올린다.
- 계정 자격증명 위치(`~/.claude`, `~/.claude-account-N`)는 변경하지 않았다.
- 삭제: `run-step-loop-claude.sh`와 관련 bash 테스트·모의 CLI 일체.
- 유지: `run-step-loop.sh`(Codex)는 이번 전환 범위 밖이다. `collect-usage.sh`도
  `run-step-loop.sh:112`에서 여전히 참조하므로 삭제하지 않았다.
- Windows 네이티브 지원은 표준 라이브러리(`pathlib`, `subprocess`, `time`,
  `heapq`) 위주로 작성해 이식 가능성을 열어뒀으나, 실제 Windows 환경 검증은
  하지 않았다. `claude`/`codex` CLI 자체의 Windows `CLAUDE_CONFIG_DIR` 지원 여부는
  이 전환의 통제 범위 밖이다.
- 테스트: `tests/test_usage.py`, `tests/test_accounts.py`, `tests/test_scheduler.py`
  (pytest)로 전체 재작성. 전체 스위트 통과 (62/62).
- 롤백: git 이력에서 `run-step-loop-claude.sh`와 관련 bash 테스트를 복원하고
  `scheduler.py`/`accounts.py`/`usage.py`, `.schedule/`를 제거한다.
