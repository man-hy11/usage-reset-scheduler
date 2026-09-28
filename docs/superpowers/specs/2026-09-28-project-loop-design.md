# 설계: 다중 계정 작업 루프 (`--project-loop`)

## 배경

`~/shortform-ai`의 `run-step-loop-claude.sh`(claude)와 `run-step-loop.sh`(codex)는
**계정 하나로** `prompt.md`를 반복 실행하면서, 출력 끝의 `STEP_STATUS: ...` 줄을 보고
다음 동작을 정한다. 이 계정의 한도가 차면 실행은 `STEP_STATUS` 없이 끝나고, 스크립트는
"완료 상태 문자열을 찾지 못했습니다"를 출력하고 멈춘다. 그러면 사용자가 다른 계정으로
직접 다시 실행해야 한다.

이 저장소의 스케줄러는 이미 여러 계정(claude/codex 혼합)의 플랜, 사용량, 리셋 시각을
관리한다. 다만 지금은 리셋 직후 "Reply with OK."만 보내 한도 시간창을 여는 용도로만
쓰인다.

사용자 요구: 이 계정 관리 기능 위에 **작업 루프**를 얹는다. 한 계정으로 step을 계속
진행하다가 한도가 차면 그 계정의 리셋 시각을 큐에 넣고, 쓸 수 있는 다른 계정으로 같은
step을 이어서 진행한다. 이 과정을 계속 반복한다.

## 목표

- `scheduler.py --project-loop --project-dir DIR`로 여러 계정을 번갈아 쓰며 DIR의
  `prompt.md`를 반복 실행한다.
- 계정은 **한도가 찰 때까지 계속 쓰고**, 차면 쓸 수 있는 다음 계정으로 넘어간다.
- 한도가 찬 계정은 리셋 시각 + 1분에 자동으로 다시 투입된다.
- 쓸 수 있는 계정이 없으면 가장 빠른 리셋 시각까지 기다렸다가 재개한다.
- 기존 bash 스크립트의 `STEP_STATUS` 규칙(COMPLETE / FAILURE_ANALYSIS /
  FAILURE_IMPROVEMENT → 계속, INCOMPLETE → 정지)을 그대로 따른다.
- 계정 선택은 기존 `--count/-n`과 명시적 계정 번호 방식을 그대로 재사용한다.
- **기존 기능(리셋 스케줄러, 계정 관리 CLI)의 동작은 전혀 바꾸지 않는다.**

## 비목표

- 중단된 step을 이어서 실행하는 기능. 다음 계정은 같은 step을 **처음부터** 다시
  실행한다. 진행 상태는 프로젝트의 파일(`PHASE.md`, worklog, 작업 트리)에 남아 있고,
  `prompt.md`의 inspect 단계가 이를 파악한다.
- 작업 트리 정리(stash/reset). 중단된 step이 남긴 변경은 그대로 둔다.
- 프로젝트 완료 판단. 루프는 아래 종료 조건이 생기기 전까지 계속 돈다(기존 bash
  스크립트와 동일).
- 계정 단위 잠금. 리셋 스케줄러와 작업 루프를 동시에 돌려도 된다(아래 "동시 실행" 참고).
- codex 모델/effort 지정. codex 계정은 CLI 기본 설정으로 실행한다(`run-step-loop.sh`와
  동일).
- Windows 네이티브 지원. 프로젝트 잠금에 `fcntl.flock`을 쓰므로 Linux/WSL/macOS 전용이다.
- 실행 상태를 파일로 저장하는 기능. 재시작하면 모든 상태를 처음부터 다시 계산한다
  (기존 스케줄러의 설계 원칙과 같음).

## 검증된 CLI 동작 (2026-09-28 확인)

설계의 판정 규칙은 아래 확인 결과를 근거로 한다.

### claude 2.1.283, `-p --output-format stream-json`

한도가 찬 user1(주간 100%)로 실제 호출해 확인했다.

- 종료 코드 1, stderr는 비어 있다.
- 한도 이벤트:
  `{"type":"rate_limit_event","rate_limit_info":{"status":"rejected","resetsAt":1790787600,"rateLimitType":"seven_day",...}}`
- 이어서 `{"type":"assistant","error":"rate_limit",...}`가 나오고, 문구는
  "You've hit your weekly limit · resets Oct 1, 2am (Asia/Seoul)"이다.
- 마지막으로 `{"type":"result","is_error":true,"api_error_status":429,"terminal_reason":"api_error",...}`가 나온다.
- 과거 `-p` 세션 기록에서 한도에 걸린 경우는 516건이다. 시작 직후 366건, 작업 도중 150건이며,
  **모든 경우 한도 오류 직후 종료**했다.
- 과거 기록에 있는 한도가 아닌 계정 오류: `error: "authentication_failed"`(Login expired),
  `error: "oauth_org_not_allowed"`.
- 여러 프로세스의 토큰 갱신은 `<설정폴더>/.oauth_refresh.lock`으로 CLI가 직접 순서를
  맞춘다.

### codex 0.157.1, `exec --json`

로컬 가짜 서버가 429 `usage_limit_reached`를 주게 하고 실제 codex 바이너리로 확인했다.

- 종료 코드 1.
- `{"type":"error","message":"You’ve hit your usage limit. ... try again at 2:57 PM."}` 뒤에
  `{"type":"turn.failed","error":{"message":"..."}}`가 나온다.
- 문구의 아포스트로피는 U+2019(`’`)이다. 문구 전체가 아니라 "usage limit"이라는 단어로
  판정한다.
- 리셋 시각은 시각 문자열로만 나오므로, 정확한 값은 사용량 API로 얻는다.
- stdin이 연결돼 있으면 "Reading additional input from stdin..."을 출력하므로 stdin은
  `DEVNULL`로 연결한다.
- 토큰 갱신 잠금은 없지만, 인증이 실패하면 `auth.json`을 다시 읽는다("Reloaded auth").
  같은 순간에 두 프로세스가 갱신하면 "refresh token was already used" 오류가 드물게 날
  수 있다.

## CLI

```
python3 scheduler.py --project-loop --project-dir DIR [--prompt-file FILE]
                     [계정번호 ... | -n N] [--model M] [--effort E]
                     [-w TIME] [-d MIN] [--interval MIN]
```

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `--project-loop` | — | 작업 루프 모드로 실행 |
| `--project-dir DIR` | (필수) | 대상 프로젝트. git 저장소여야 한다 |
| `--prompt-file FILE` | `prompt.md` | DIR 기준 상대 경로. **매 실행마다 새로 읽는다** |
| 계정번호 / `-n N` | `1` | 기존 규칙과 같다 |
| `--model` / `--effort` | `claude-opus-5-5` / `high` | **claude 계정에만** 적용된다. 이 기본값은 작업 루프 모드에서만 쓰인다 |
| `-w` / `-d` | 즉시 | 최초 실행 시각. 기존과 같은 형식 |
| `--interval` | `5` | 리셋 시각을 알 수 없을 때 다시 확인하기까지 기다리는 시간(분) |

검증 (위반 시 종료 코드 1):
- `--project-dir`나 `--prompt-file`을 `--project-loop` 없이 쓰면 오류.
- `--project-loop`를 `-a/-l/-r/-c`와 함께 쓰면 오류.
- DIR이 없거나, git 저장소가 아니거나, 프롬프트 파일이 없으면 오류(bash 스크립트와 같은 검사).
- `--threshold`는 작업 루프에서 쓰이지 않는다(무시).

## 아키텍처

### 파일 구조

```
scheduler.py      # parse_args에 옵션 추가, main()에서 --project-loop일 때 분기 (기존 경로는 그대로)
project_loop.py   # 새 파일: 계정 상태, 계정 선택, 실행 결과 판정, 메인 루프, 프로젝트 잠금
step_runner.py    # 새 파일: 도구별 명령 구성, 실시간 스트리밍 실행, 이벤트 파싱 → StepOutcome
tests/test_project_loop.py
tests/test_step_runner.py
tests/fixtures/   # 위에서 확인한 실제 claude/codex 한도 초과 출력 (민감 정보 제거)
README.md         # "작업 루프" 섹션 추가
```

`step_runner.py`는 subprocess와 출력 해석을 맡고, `project_loop.py`는 판단과 스케줄링을
맡는다. 루프는 실행 함수, 사용량 조회, 플랜 확인, sleep, now를 **주입받아서**
subprocess 없이 테스트할 수 있다.

### 재사용하는 기존 코드 (수정하지 않음)

- `select_paid_account_ids`, `accounts.known_account_ids`: `-n N` 계정 선택
- `_check_plan`, `accounts.check_paid_subscription`: 유료 플랜 확인
- `fetch_usage`: 5시간/주간 사용률과 리셋 시각 조회
- `accounts.account_dir`, `registry.get_tool`: 계정 폴더와 도구
- `_share_claude_config_all`: 계정 간 설정 공유 (작업 루프에도 CLAUDE.md, skills가 필요함)
- `_parse_wait_until`: `-w` 파싱

### `step_runner.py`

```python
@dataclass
class StepOutcome:
    step_status: str | None     # "COMPLETE" | "FAILURE_ANALYSIS" | "FAILURE_IMPROVEMENT" | "INCOMPLETE" | None
    limit_hit: bool             # 한도 초과 신호가 있었는지
    limit_reset_at: int | None  # 신호에 들어 있던 리셋 시각(epoch). claude만 채워진다
    account_error: str | None   # 예: "authentication_failed", "oauth_org_not_allowed", "codex_auth"
    exit_code: int
    log_path: Path

def run_step(account_id, tool, project_dir, prompt_text, model, effort, log_dir) -> StepOutcome
```

- **명령**
  - claude: `claude --dangerously-skip-permissions --model M --effort E -p --output-format stream-json --verbose --include-partial-messages <prompt>`.
    `CLAUDE_CONFIG_DIR=<계정폴더>`, cwd는 DIR이다. `--strict-mcp-config`는 붙이지 않는다
    (작업에는 MCP가 필요할 수 있고, bash 스크립트와 같게 한다).
  - codex: `codex exec --json --sandbox danger-full-access -C DIR <prompt>`, `CODEX_HOME=<계정폴더>`.
  - 공통: `stdin=DEVNULL`. stdout을 한 줄씩 읽는다(`Popen`).
- **실시간 출력:** claude는 `text_delta`, codex는 `agent_message` 항목의 텍스트를 받는
  즉시 화면에 출력한다.
- **로그:** DIR 아래에 claude는 `.claude-runs/`, codex는 `.codex-runs/`에 남긴다.
  - `step-<YYYYmmdd-HHMMSS>-user<N>.log`: 추출한 텍스트. 기존 로그와 같은 형식이라
    `grep '^STEP_STATUS:'`가 그대로 동작한다.
  - `step-<YYYYmmdd-HHMMSS>-user<N>.raw.jsonl`: stdout 원본 전체. 한도나 오류 이벤트를
    확인하는 데 쓴다.
- **STEP_STATUS:** 추출한 텍스트에서 `^STEP_STATUS: (COMPLETE|FAILURE_ANALYSIS|FAILURE_IMPROVEMENT|INCOMPLETE)$`
  (multiline)의 **마지막** 매치를 쓴다.
- **한도 신호**
  - claude: `rate_limit_event`의 `rate_limit_info.status == "rejected"`(이때 `resetsAt`을
    `limit_reset_at`으로 씀), 또는 `assistant.error == "rate_limit"`, 또는
    `result.api_error_status == 429`.
  - codex: `error` 또는 `turn.failed` 이벤트 메시지에 "usage limit"이 들어 있음(대소문자 무시).
- **계정 오류 신호**
  - claude: `assistant.error`가 `authentication_failed` 또는 `oauth_org_not_allowed`.
  - codex: `error`/`turn.failed` 메시지에 "sign in again" 또는 "log out"이 들어 있음 →
    `"codex_auth"`.

### `project_loop.py`: 계정 상태

```python
@dataclass
class LoopAccount:
    account_id: int
    tool: str
    available_at: int          # 이 시각 이후에만 실행한다
    status: str                # "ready" | "exhausted" | "account_error"
    reason: str | None = None  # 화면 표시용 (예: "주간 한도", "Login expired")
    runs: int = 0              # 이 계정으로 연속 실행한 횟수 (화면 표시용)
```

### 시작

1. 인자를 검증하고 **프로젝트 잠금**을 잡는다(아래). 실패하면 종료한다.
2. `_share_claude_config_all()`.
3. 계정을 고른다. `-n N`이면 `select_paid_account_ids`, 아니면 지정한 번호를 쓰고 각각
   `_check_plan`으로 확인한다. 유료가 아니면 `account_error`(재확인 대상)로 둔다.
   유료 계정이 하나도 없으면 종료 코드 1로 끝난다.
4. 각 계정의 사용량을 조회해 `available_at`을 정한다(아래 "소진 판단"). `-w`/`-d`가
   있으면 모든 계정의 `available_at`을 적어도 그 시각 이후로 맞춘다.

### 메인 루프

```
current = None
unknown_fail_streak = 0
loop:
  if current가 없거나 current.available_at > now:
      current = 사용 가능한 계정 중 (available_at, account_id)가 가장 작은 계정
      없으면(모두 미래 시각) → 가장 빠른 available_at까지 sleep (재개 시각 출력) → continue
  if current.status == "account_error":      # 재확인 시각이 된 계정
      _check_plan 통과하면 ready로 전환 / 실패하면 available_at = now + 60분 → current = None → continue
  실행 직전 확인: fetch_usage → 소진 상태면 exhausted로 표시하고 current = None → continue
                  (조회가 실패하면 그대로 실행한다. 실행이 한도 신호로 판정해 준다)
  상태 요약 출력
  prompt 파일을 새로 읽고 outcome = run_step(...)
  outcome을 판정한다(아래 표).
```

- **계속 같은 계정 사용:** 판정이 "계속"이면 `current`를 유지해서, 다음 실행도 같은
  계정으로 한다.
- **계정 교체:** 판정이 "소진" 또는 "계정 오류"이면 `current = None`. 다음 차례에
  `(available_at, account_id)`가 가장 작은, 사용 가능한 계정이 선택된다.

### 실행 결과 판정 (위에서부터 먼저 맞는 규칙 적용)

| 조건 | 판정 | 조치 |
|---|---|---|
| `step_status`가 COMPLETE / FAILURE_ANALYSIS / FAILURE_IMPROVEMENT | 계속 | 같은 계정으로 바로 다음 실행. `unknown_fail_streak = 0` |
| `step_status == INCOMPLETE` | 정지 | 사유와 로그 경로를 출력하고 종료 코드 1 (계정을 바꿔도 해결되지 않는 권한/승인 문제) |
| `limit_hit` | 소진 | `available_at` = 리셋 시각 + 60초. 리셋 시각은 `limit_reset_at`, 없으면 사용량 조회로 얻고, 그것도 없으면 now + `--interval`분. 계정 교체. `unknown_fail_streak = 0` |
| `account_error` | 계정 오류 | `status = account_error`, `available_at` = now + 60분(`PLAN_RECHECK_MIN`). 계정 교체 |
| 그 외 (STEP_STATUS도 신호도 없음) | 사용량으로 교차 확인 | `fetch_usage` 결과 5시간 또는 주간 사용률이 **95% 이상**이면 "소진"으로 처리한다. 아니면 알 수 없는 실패: `unknown_fail_streak += 1`, **3회 연속이면 종료 코드 1**, 아니면 같은 계정으로 재시도 |

### 소진 판단 (시작 시와 실행 직전 확인에서 사용)

`fetch_usage` 결과에서:
- 주간 사용률 ≥ 100 → `available_at = weekly_reset_at + 60`, 사유 "주간 한도"
- 그렇지 않고 5시간 사용률 ≥ 100 → `available_at = five_hour_reset_at + 60`, 사유 "5시간 한도"
- 소진인데 리셋 시각이 없으면 now + `--interval`분 뒤에 다시 확인한다.
- 소진이 아니면 사용 가능하다.

기존 `usage.compute_next_run`은 "다음 OK 전송 시각" 계산용(8일 상한, threshold 기반)이라
여기에는 쓰지 않는다.

### 프로젝트 잠금

- 경로: `<저장소>/.schedule/locks/project-<sha1(realpath(DIR))[:12]>.lock`. `.schedule/`은
  이미 gitignore에 있고, 대상 프로젝트 안에는 파일을 만들지 않는다.
- 시작할 때 `fcntl.flock(fd, LOCK_EX | LOCK_NB)`로 잠근다. 성공하면 파일에 PID, 시작 시각,
  계정 목록을 기록하고 프로세스가 끝날 때까지 fd를 열어 둔다.
- 실패하면 파일에 적힌 기존 실행 정보를 보여 주고 종료 코드 1로 끝난다.
- 어떤 방식으로 종료하든(Ctrl+C, 예외, kill -9) OS가 잠금을 푼다. 잠금 파일이 남아도
  다음 실행을 막지 않는다.
- 이 잠금으로 막지 못하는 것: 같은 DIR에서 기존 bash 루프를 돌리거나 직접 편집하는 경우.
  README에 적어 둔다.

### 동시 실행

- 리셋 스케줄러와 작업 루프는 동시에 돌려도 된다.
  - claude의 토큰 갱신은 CLI가 잠금으로 순서를 맞춘다.
  - codex는 잠금이 없지만 인증이 실패하면 `auth.json`을 다시 읽는다.
  - 작업 중인 계정에 리셋 스케줄러가 "OK"를 보내도 사용량이 조금 더 들 뿐이다.
- 작업 루프는 `.schedule/queue.json`을 읽지도 쓰지도 않는다(리셋 스케줄러의 상태를
  덮어쓰지 않기 위해). 작업 루프의 로그는 계정 폴더의 `start-limit-runs/`에 남지 않으므로,
  리셋 스케줄러의 "최근 실행" 표시에는 반영되지 않는다.

### 화면 출력

실행마다:
```
==========작업루프 상태============
▶ 실행: user2 [claude]  (연속 3회차, 2026-09-28 12:05:10 시작)
  user3 [claude]  대기
  user4 [codex ]  대기
  user1 [claude]  소진(주간 한도) → 2026-10-01 02:00:59 재투입
===================================
```
모든 계정이 기다리는 중일 때: `[시각] 사용 가능한 계정 없음 → user2 재개 2026-09-28 13:10:59까지 대기`.

### 종료

- 종료 코드 1: `STEP_STATUS: INCOMPLETE`, 알 수 없는 실패 3회 연속, 시작 시 검증 실패,
  잠금 실패, 유료 계정 없음.
- `Ctrl+C`: 실행 중인 하위 프로세스를 종료(`terminate`, 5초 뒤 `kill`)하고 종료 코드 130.
- 그 외에는 끝나지 않는다.

## 기존 기능에 미치는 영향

- `scheduler.py`는 `parse_args`에 옵션 세 개를 추가하고, `main()` 초반에 `--project-loop`
  분기를 추가하는 것만 바뀐다. 이 옵션 없이 실행하면 동작은 완전히 같다.
- 재사용하는 기존 함수는 수정하지 않는다.
- **완료 조건:** 기존 `tests/`는 수정 없이 모두 통과해야 한다.

## 테스트

- `test_step_runner.py`
  - 확인한 실제 출력을 fixture로 사용한다: claude 한도 초과(rate_limit_event rejected + 429),
    codex 한도 초과(turn.failed, `’` 포함).
  - 정상 출력(STEP_STATUS 각각, 여러 개가 있으면 마지막 것), 계정 오류(authentication_failed,
    codex "sign in again"), JSON이 아닌 줄이 섞인 경우.
  - 명령 구성: stdin DEVNULL, 환경 변수, cwd, claude에만 모델/effort 적용.
- `test_project_loop.py` (실행/조회/sleep/now 주입)
  - 한도가 찰 때까지 같은 계정을 계속 쓰고, 소진되면 다음 계정으로 넘어간다.
  - 다음 계정도 소진된 상태(실행 직전 확인)면 건너뛴다.
  - 모든 계정이 소진되면 가장 빠른 리셋까지 기다린다.
  - INCOMPLETE → 종료 코드 1.
  - 알 수 없는 실패: 2회는 같은 계정으로 재시도, 3회 연속이면 종료. 사이에 성공이 있으면 카운터 초기화.
  - 교차 확인: STEP_STATUS가 없고 사용률이 95% 이상이면 소진으로 처리.
  - 계정 오류 → 60분 뒤 재확인, 플랜 확인을 통과하면 복귀.
  - 리셋 시각 우선순위: 신호에 있는 값 → 사용량 조회 → `--interval`.
  - `-w`/`-d`가 최초 `available_at`에 반영되는지.
- 잠금: 같은 DIR로 두 번 잠그면 두 번째가 실패하고, 첫 번째가 풀리면 다시 잠글 수 있다.
  심볼릭 링크 경로도 같은 잠금으로 처리된다.
- 인자 검증: 조합 오류, DIR/git/프롬프트 파일 누락.
- 실제 CLI를 쓰는 E2E 테스트는 자동화하지 않는다. 구현 후 수동으로 확인한다:
  한도가 찬 계정 하나와 쓸 수 있는 계정 하나로, 임시 git 저장소와
  "Reply with exactly: STEP_STATUS: COMPLETE" 프롬프트를 써서 계정 전환과 계속 실행을 확인한다.

## 위험 요소

- **codex 한도 응답 형식:** 실제 서버 응답이 아니라, codex 파서가 받아들이는 형식에 맞춰
  재현한 것이다. 형식이 다르면 "usage limit" 신호를 놓칠 수 있다. 이 경우에도 교차 확인
  (사용률 95% 이상)에서 잡힌다.
- **CLI 출력 형식 변경:** 이벤트 이름이 바뀌면 신호를 놓친다. 이 경우에도 교차 확인이
  대비책이 되고, fixture 테스트가 판정 기준을 문서처럼 남긴다.
- **step 재시작 비용:** 중단된 step을 처음부터 다시 실행하므로, 작업 도중 소진이 잦으면
  같은 작업을 반복하게 된다. 사용자가 받아들인 동작이다.
