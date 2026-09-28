# 작업: `--project-loop` PR 리뷰 후속 조치

## 개요

- 작성일: 2026-09-28
- 상태: 미착수 (향후 작업용 백로그)
- 리뷰 대상: `6f0caa5..2bad873` (`--project-loop` 기능 10개 커밋, `master`에 병합됨)
- 리뷰 방식: `/pr-review-toolkit:review-pr` — 코드 전반, 테스트, 조용한 실패(오류 처리), 타입 설계, 주석·문서 5개 에이전트
- 결론: 기존 리셋 스케줄러 영향 없음, 정상 경로(한도 → 계정 교체 → 대기 → 재개) 정상. 다만 **며칠 무인 실행 시 루프가 죽지 않고 헛도는 경로**가 여럿 있음(대부분 가짜 실행기 모의 실행으로 재현됨).
- 치명적(Critical) 문제: 없음

관련 파일: `step_runner.py`, `project_loop.py`, `scheduler.py`(`_run_project_loop_mode`), `accounts.py`, `README.md`, `docs/superpowers/specs/2026-09-28-project-loop-design.md`

> 줄 번호는 `2bad873` 기준이다. 코드가 바뀌었으면 함수 이름으로 찾는다.

## 권장 작업 순서

1. **A1~A6** (무인 실행 중 헛돌기 / 루프 사망) — 설계 판단이 필요한 부분이 있어 짧게 설계 합의 후 진행
2. **A7** (상태 이름 단일화) — 5줄 수준, A1~A6과 함께
3. **B** (테스트 보강) — 특히 계정 오류 파킹 시간, 잠금 유지, Harness 무한 루프 방지
4. **C** (문서 정정)
5. **D** (개선 제안) — 여유 있을 때
6. 수정 후 `/pr-review-toolkit:review-pr` 재실행

## 이미 결정된 사항 (다시 논의하지 않음)

- `STEP_STATUS`는 마지막 매치를 쓴다 (단, A-관련 D3 참고)
- codex는 CLI 기본 모델/effort로 실행
- SIGTERM도 Ctrl+C와 같은 경로로 종료 코드 130
- `--interval`은 project-loop 모드에서 1 이상
- codex 인증 오류 판정 단어는 `"sign in again"`만 사용 (`"log out"`은 오탐 때문에 제거됨)

---

## A. 고쳐야 할 문제 (Important)

### A1. 재로그인이 필요한 계정이 매시간 "정상"으로 판정되어 무한 헛실행

- 위치: `project_loop.py:199-205`(account_error 재확인), `:242-249`(account_error 분기), 근본 원인 `accounts.py:214-225`(`check_paid_subscription`)
- 시나리오: 토큰 만료/폐기 → 실행이 인증 오류 → 60분 파킹 → 재확인 시 실시간 플랜 조회가 401 → `HTTPError`가 잡혀 **로컬에 저장된 플랜으로 대체 판정** → "계정 확인 완료 → 다시 사용합니다" 출력 → 사용량 조회도 401이지만 `_safe_usage`가 삼킴 → 다시 실패 → 반복. 이 경로는 3회 연속 실패 카운터를 건드리지 않아 **영원히 종료되지 않음**. claude의 `authentication_failed`도 같은 구멍.
- 재현(모의): codex 계정 2개 모두 재로그인 필요 → 4.2일 동안 헛실행 199회, 종료 안 됨
- 수정안:
  - 재확인이 "플랜 기록이 있다"가 아니라 "자격 증명이 실제로 동작한다"를 확인하도록 한다. 예: `fetch_usage_fn` 성공까지 요구하거나, `check_paid_subscription(..., require_live=True)` 추가(실시간 조회 실패 시 실패 처리)
  - 계정별 연속 계정 오류 횟수를 세고, 3회 연속이면 `재로그인 필요: python3 scheduler.py -a N` 출력
  - 모든 계정이 `account_error` 상태면 대기하지 말고 그 안내와 함께 종료 코드 1

### A2. codex 한도 초과 + 사용량 조회 실패 시 `--interval`마다 헛실행

- 위치: `project_loop.py:160-167`(`_limit_available_at`), `:34-39`(`_safe_usage`)
- 시나리오: codex는 `limit_reset_at`을 주지 않으므로(`step_runner.py:100`) 리셋 시각을 사용량 API로만 얻는다. 조회가 계속 실패하면(토큰 파일 이동, 응답 형식 변경, `fetch_codex_usage` 내부 `KeyError` 등) `now + interval`만 쉬고, 실행 전 확인도 None이라 또 실행 → 즉시 한도 → 5분 파킹 반복. 주간 한도면 약 2,000회. `limit_hit`이 `unknown_fail_streak`을 초기화하므로(`:236`) 종료도 안 됨.
- 같은 부류: 사용량 조회로 보이지 않는 반복 429(리셋 스케줄러 동시 실행, 모델별 주간 한도 — `fetch_claude_usage`는 `five_hour`/`seven_day`만 읽음)
- 추가(테스트 리뷰): 한도 시점에 사용량 API가 99.x%(반올림/지연)를 보고하면 100% 기준에 걸리지 않아 역시 `--interval`만 파킹됨
- 재현(모의): codex 계정 1개 → 0.69일에 실행 200회, 종료 안 됨
- 수정안:
  - codex의 "try again at 2:57 PM" 문구를 epoch로 파싱(날짜 없으면 오늘/내일 중 미래 시각)
  - 한도 신호는 확실한데 리셋 시각을 모르면 고정 `interval` 대신 계정별 지수 백오프(interval, ×2, … 최대 1시간)
  - CLI가 이미 한도를 확정했으므로 `_limit_available_at`의 사용량 대체 판정은 95% 기준 사용 고려
  - "한도 신호 있음, 리셋 미확인, 사용률 100% 미만"이 N회 이상 반복되면 크게 경고

### A3. 95% 교차 확인이 실제 오류를 "주간 한도"로 둔갑시킴

- 위치: `project_loop.py:251-262`, `:157`
- 시나리오: 모든 실행이 한도 신호 없이 크래시(깨진 CLI 업데이트, 잘못된 `--model`, node 크래시 등)하는데 주간 사용률이 95% 이상이면 `exhausted`/"주간 한도"로 **최대 7일** 파킹. 출력 메시지는 확정된 한도와 똑같고 종료 코드·로그 경로가 없음. `unknown_fail_streak = 0`이 되어 3회 연속 종료도 발동 안 함.
- 재현(모의): 96% 계정 2개 + 항상 크래시 → 둘 다 6일 파킹 후 반복, 로그엔 "주간 한도"만
- 수정안:
  - 이 경우 메시지를 구분: `한도 신호 없이 실패(종료 코드 1), 사용률 96% → 한도로 추정. 로그: … .raw.jsonl`
  - 사용률 100%일 때만 streak 초기화
  - 95~99%면 주간 리셋까지가 아니라 `interval`만 파킹하거나, 별도 "추정 한도" 카운터로 누적 시 에스컬레이션

### A4. 실패한 실행의 실제 오류 문구가 버려지고, 안내되는 로그 경로는 빈 파일

- 위치: `step_runner.py:84-91`(claude 인식 안 된 `assistant.error`, `result.is_error`/`result.result`), `:114-126`(codex 인식 안 된 `error`/`turn.failed`), `project_loop.py:264-268`(알 수 없는 실패 메시지)
- 시나리오: `.log`에는 추출된 어시스턴트 텍스트만 있어 실패 시 비어 있음. 실제 원인은 `.raw.jsonl`(스트리밍 조각으로 매우 시끄러움)이나 캡처되지 않는 stderr에만 있음. 예: codex `"unexpected status 401 Unauthorized: …"`, `"Not logged in"`은 한도도 계정 오류도 아닌 것으로 분류됨.
- 수정안:
  - `StepOutcome`에 `last_error: str | None` 추가. 인식 안 된 `assistant.error`, `is_error: true`인 `result`의 `result` 문구, codex의 모든 오류 메시지를 담는다
  - 이를 `.log`에도 쓰고, 알 수 없는 실패 / 95% 분기 메시지에 `.raw.jsonl` 경로와 함께 출력

### A5. 실행 중 예외가 여러 날 돌던 루프를 트레이스백으로 종료

- 위치: `scheduler.py:914-916`(`run_step_fn`), `step_runner.py:178-197`(`Popen`), `accounts.py:111`
- 시나리오:
  - `claude`/`codex`가 PATH에 없음(cron/systemd에서 `~/.local/bin` 누락 흔함). claude는 시작 시 `claude auth status`를 실제 실행해 걸러지지만, **codex는 `auth.json`만 읽어서 시작 확인을 통과** → claude 계정이 소진된 며칠 뒤 codex로 넘어가는 순간 `FileNotFoundError`로 사망
  - `prompt.md` 삭제/비UTF-8, 에디터의 rename 저장 순간의 일시적 `FileNotFoundError`
  - `prompt.md`가 약 128KiB를 넘으면 argv 한 개로 넘겨 `OSError E2BIG`
  - 디스크 가득(`ENOSPC`) — `.raw.jsonl`은 모든 조각을 기록하고 회전하지 않음
  - `_decode_jwt_claims`의 `IndexError`(형식이 깨진 id_token)가 `_check_plan`에서 새어 나옴
  - `subprocess.run(["claude","auth","status","--json"])`에 timeout이 없어 매시간 재확인이 영원히 멈출 수 있음
- 수정안:
  - 시작 검증에서 선택된 계정의 도구를 `shutil.which`로 확인해 명확한 한국어 메시지로 실패
  - 루프에서 `run_step_fn`의 `OSError`를 잡아 예외 내용을 출력하고 알 수 없는 실패로 셈(또는 해당 계정을 `account_error`로). `BaseException`은 잡지 않는다
  - `check_plan_fn`을 `Exception`으로 감싸 "재확인 실패"로 처리
  - `claude auth status` 호출에 `timeout=` 추가

### A6. 5초 안에 `kill`을 두 번 보내면 에이전트가 남은 채 잠금이 풀림

- 위치: `step_runner.py:156-164`(`stop_process`), `:209-211`, `scheduler.py:886-933`
- 시나리오: 첫 SIGTERM → `stop_process`가 `proc.wait(timeout=5)` 중 → 두 번째 SIGTERM이 그 대기 중에 `KeyboardInterrupt`를 일으켜 `proc.kill()`을 건너뜀 → `Popen.__exit__`은 KeyboardInterrupt일 때 0.25초만 기다리고 죽이지 않음 → `finally`가 잠금 해제 → claude/codex가 저장소를 계속 수정하는데 새 루프가 잠금을 잡을 수 있음. (이전에 보류한 "finally 중 이중 신호"보다 발생 확률이 높은 별개 경로)
- 추가: `kill -9`도 같음(README에 경고 있음). CLI의 손자 프로세스(MCP 서버, Bash 도구 명령)는 CLI만 terminate/kill 해서는 살아남음
- 수정안:
  - `stop_process` 안에서 SIGTERM 핸들러를 `SIG_IGN`으로 두거나 핸들러를 1회용으로
  - `start_new_session=True`로 띄우고 `os.killpg`로 프로세스 그룹 전체 종료
  - `pass_fds=(lock_fd,)`로 잠금 fd를 자식에게 넘겨, 부모가 `kill -9`로 죽어도 에이전트가 끝날 때까지 flock 유지 (flock은 open file description 단위라 자식이 들고 있으면 유지됨)

### A7. step 상태 이름이 4곳에 따로 정의됨

- 위치: `step_runner.py`의 `_STEP_STATUS_RE` 정규식, `CONTINUE_STATUSES`(step_runner.py:15), `project_loop.py`의 `CONTINUE_MESSAGES`(:138) 키, `run_project_loop`의 `"INCOMPLETE"` 리터럴
- 위험: `CONTINUE_STATUSES`에만 추가하면 step **성공 직후** `CONTINUE_MESSAGES[...]`에서 `KeyError`로 루프 사망(`project_loop.py:224`). 정규식에 빠지면 해당 상태를 영영 인식 못 해 알 수 없는 실패 3회로 종료.
- 수정안 (약 5줄):
  ```python
  # step_runner.py
  CONTINUE_STATUSES = ("COMPLETE", "FAILURE_ANALYSIS", "FAILURE_IMPROVEMENT")
  STOP_STATUS = "INCOMPLETE"
  _STEP_STATUS_RE = re.compile(
      rf"^STEP_STATUS: ({'|'.join(CONTINUE_STATUSES + (STOP_STATUS,))})$", re.MULTILINE)
  ```
  - `project_loop.py`: 리터럴 대신 `STOP_STATUS` 비교, `CONTINUE_MESSAGES.get(status, f"STEP_STATUS: {status} → 같은 계정으로 계속합니다.")`
  - 테스트: `assert set(CONTINUE_MESSAGES) == set(step_runner.CONTINUE_STATUSES)`

---

## B. 테스트 보강

변이 테스트(코드 한 줄을 일부러 틀리게 바꿔 보기) 22건 중 **18건을 기존 테스트가 잡지 못함**. 살아남은 변이:

| 변이 | 결과 |
|---|---|
| account_error 파킹 시간 60분 → 0초 | 통과(못 잡음) |
| 한도 도달 시 unknown_fail_streak 초기화 제거 | 통과 |
| 95% 추정 소진 시 streak 초기화 제거 | 통과 |
| `build_loop_accounts`의 `max(start_at, …)` 제거(소진/계정 오류) | 통과(둘 다) |
| `CONTINUE_MESSAGES`에서 `FAILURE_IMPROVEMENT` 제거(실제론 KeyError) | 통과 |
| 정규식 오타 `FAILURE_IMPROVMENT` | 통과 |
| `AMBIGUOUS_LIMIT_PERCENT` 95 → 96 | 통과 |
| `-w` 결과 무시 / `-d` 두 번 적용 | 통과(둘 다) |
| `run_project_loop` 전에 `flock(LOCK_UN)` | 통과 |
| `write_lock_info` / `_share_claude_config_all` 제거 | 통과(둘 다) |
| 운영 코드에 `sleep_fn=lambda s: None` 주입 | 통과 |
| INCOMPLETE와 limit_hit 우선순위 뒤바꿈 | 통과 |
| account_error 재확인이 `available_at = now` | **테스트가 멈춤**(timeout 플러그인 없음) |

추가할 테스트:

1. **계정 오류 파킹 시간 (우선순위 7)** — `Harness.run_step`이 `(account_id, self.now)`를 기록하게 하고, 계정 1의 두 번째 실행이 `NOW + 600 + ACCOUNT_RECHECK_SEC` 이후인지 확인. 단일 계정 버전 `Harness([1], [acct_err(), ok("INCOMPLETE")])` → `sum(h.sleeps) >= 3600`
2. **루프 도는 동안 잠금 유지 (6)** — 가짜 `run_project_loop` 안에서 `acquire_project_lock(lock_path_for(project, locks))`가 `ProjectLockError`를 내고, 메시지에 `f"PID {os.getpid()}"`와 `"user1"`이 있는지 확인
3. **모든 상태 이름 (6)** — `find_step_status`를 4가지 상태로 parametrize, `CONTINUE_STATUSES` 각각으로 Harness 실행 시 `ran == [1, 1]`·sleep 없음 확인, A7의 키 일치 테스트
4. **streak 초기화 (5)** — `Harness([1,2], [unknown(), unknown(), limit(NOW+50_000), unknown(), unknown(), ok("INCOMPLETE")])` → `ran == [1,1,1,2,2,2]`, "알 수 없는 실패가 연속되어" 미출력. 세 번째를 `unknown()` + 97% 사용량 `sequence`로 바꾼 버전도. (실행 전 소진·account_error 시 streak을 초기화할지 정책 결정 후 고정)
5. **status와 limit_hit 우선순위 (6)** — `StepOutcome("INCOMPLETE", limit_hit=True)`의 동작을 정하고 고정(`limit_hit` 우선 고려). `StepOutcome("COMPLETE", limit_hit=True)` → 계속, 다음 실행 전 확인에서 소진 처리
6. **codex 한도 + 99% 사용량 (5)** — `limit(None)` + `sequence(usage(), usage(five=99, five_reset=NOW+5000))` → `available_at` 고정(현재 `NOW+900`, A2 수정 후 `NOW+5060` 기대)
7. **`-w`/`-d` 배선과 start_at 하한 (5)** — `scheduler._time.time` 고정 후 `-w HH:MM -d 5` → `available_at == parsed + 300` 정확히. 잘못된 `-w` → 1. `build_loop_accounts(start_at=NOW+50_000)`에서 소진 계정·무료 계정 모두 `available_at == NOW+50_000`
8. **주입 함수 예외 (5)** — 가짜 루프가 `RuntimeError` → 잠금 해제·SIGTERM 핸들러 복원 확인. Harness에서 `run_step`이 `FileNotFoundError` → A5 정책대로 고정
9. **운영 배선 (5)** — `captured_loop["sleep_fn"] is time.sleep`, `abs(captured_loop["now_fn"]() - time.time()) < 5`, `accounts.share_claude_config_all` 호출 여부
10. **Harness 무한 루프 방지 (5)** — `fetch_usage`/`check_plan`/`sleep` 호출 합계가 200을 넘으면 `AssertionError`. 회귀 시 테스트가 멈추지 않고 실패하도록
11. **약한 단언 보강** — `test_account_error_is_parked_and_rejoins_after_plan_recheck`(1번으로 보강), `test_next_account_already_exhausted_is_skipped_before_running`·`test_limit_reset_falls_back_to_usage_then_interval`에 반환 코드 확인 추가
12. **실제 성공 출력 fixture (5)** — 지금 fixture는 한도 초과만 실제 출력. 성공 경로는 합성 이벤트뿐. claude stream-json(부분 메시지, tool_use 블록, 여러 텍스트 블록), codex `--json`(여러 agent_message) 실제 성공 출력을 잘라 추가
13. 우선순위 낮음: 94.9/95 경계, 플랜 확인 실패 → 성공 복귀, `--prompt-file` 단독 사용 배선

---

## C. 문서 정정 (코드와 다름)

### 설계서 `docs/superpowers/specs/2026-09-28-project-loop-design.md`

1. 171행: codex 인증 판정이 `"sign in again" 또는 "log out"` → `error`/`turn.failed` 메시지에 "sign in again"이 들어 있음 → `"codex_auth"`
2. 144행: `log_path: Path` → `Path | None = None`. 140-143행에 기본값(`limit_hit=False`, `limit_reset_at=None`, `account_error=None`, `exit_code=0`) 누락
3. 146행: `run_step(..., log_dir)` → 실제는 `log_dir` 인자 없음(`run_log_dir()`로 계산), 키워드 전용 `out=None` 있음
4. 228행: "100%가 찬 창(… 100% 먼저 확인 후 95%)이 있으면" 자기모순 → "주간 또는 5시간 사용률이 95% 이상인 창이 있으면 소진으로 처리한다(100% 찬 창이 있으면 그 창의 리셋 시각을 우선 사용)". 이 분기가 `unknown_fail_streak = 0` 한다는 점도 누락
5. 227행: 상수 이름 `PLAN_RECHECK_MIN` → 실제 `ACCOUNT_RECHECK_SEC = 60 * 60`
6. 183행: `reason` 예시 `"Login expired"`는 어디서도 설정 안 됨 → 실제 값: `"주간 한도"`, `"5시간 한도"`, `"한도 초과"`, `"한도 초과(리셋 시각 미확인)"`, `"유료 구독 아님"`, 원본 오류 코드(`authentication_failed`, `oauth_org_not_allowed`, `codex_auth`)
7. 189-193행 시작 순서: 실제는 잠금 → 계정 선택 → 유료 계정 없으면 종료 1 → `write_lock_info` → `_share_claude_config_all()` → `build_loop_accounts`. 245행 "성공하면 파일에 PID… 기록"도 계정 선택 동안 잠금 파일이 비어 있다는 점(두 번째 실행이 "(실행 정보 없음)" 표시) 누락
8. 274-276행 "(중단 후 재개 시 지연을 줄이기 위해)" → 실제 이유는 호스트 절전: "(호스트 절전 후 깨어났을 때 대기가 절전 시간만큼 늘어나지 않도록)". 60초 분할은 계정 없음 대기에만 해당(알 수 없는 실패 뒤 대기는 한 번에 `interval_min * 60`)
9. 282-283행 "(시작 단계 포함)": SIGTERM 핸들러는 잠금 획득 후에 설치됨. 인자 검증·잠금 획득 중 `kill`은 기본 동작(셸 상태 143)
10. 104행 "bash 스크립트와 같은 검사": 스크립트는 `-d .git`, 코드는 `.git` 존재 여부라 worktree/submodule(파일인 `.git`)도 허용
11. 115-116행 파일 목록에 `tests/test_scheduler_project_loop.py` 누락
12. 16행 "리셋 시각을 큐에 넣고"가 259행 "queue.json을 읽지도 쓰지도 않는다"와 충돌 → "대기 목록" 등으로
13. 248행 근처: `kill -9` 시 잠금이 즉시 풀린다는 점 추가

### `scheduler.py` argparse 도움말

- 478-479행 `--model`/`--effort`: "생략 시 도구별 기본값" → project-loop에서는 `claude-opus-5-5`/`high`, claude 계정에만 적용됨을 추가

### `README.md`

- 180-181행 옵션 표 `--model`/`--effort`: "(`--project-loop`: `claude-opus-5-5` / `high`, claude 계정만)" 추가 (156행 본문은 맞음)
- 184행 옵션 표 `--interval`: project-loop에서는 리셋 시각을 모를 때 재확인 대기 + 알 수 없는 실패 뒤 재시도 대기이며 1 이상 (154행 본문은 맞음)
- 34, 191행, `scheduler.py:494`: "번갈아 쓰며"는 라운드로빈으로 읽힘 → "한도가 찰 때까지 한 계정을 쓰고 다음 계정으로 넘기며". `-n 4`는 "최대" 4개
- 153행 "종료:" 항목: 종료 코드 1이 빠짐 → "종료 코드 1: `STEP_STATUS: INCOMPLETE`, 또는 원인을 알 수 없는 실패가 3회 연속(각 실패 뒤 `--interval`분 대기 후 같은 계정으로 재시도). 종료 코드 130: `Ctrl+C` 또는 `kill`"
- 149행 한도 신호: codex는 `turn.failed`뿐 아니라 `error` 이벤트도 봄, claude는 `assistant.error == "rate_limit"`도 봄 → "codex: `error`/`turn.failed` 메시지의 'usage limit'"
- 167, 203행 잠금 위치: `<프로젝트 루트>`가 대상 프로젝트로 읽힘 → `<스케줄러 저장소 루트>/.schedule/locks/`, "대상 프로젝트 안에는 만들지 않음". 잠금 루트가 스케줄러 파일 위치 기준이라 다른 체크아웃(예: `.worktrees/`)에서 띄운 루프는 서로 막지 못함
- 167행 "다른 프로젝트끼리는 동시에 돌릴 수 있습니다": 계정 단위 잠금이 없으므로 "계정 번호가 겹치지 않게 지정하세요" 추가
- 164행 `kill -9`: 잠금은 즉시 풀리므로 "남은 에이전트를 직접 종료하기 전에 새 루프를 띄우지 마세요" 추가
- 158행 "기존 스크립트 로그와 같은 형식": 정확히는 다름(claude는 텍스트 블록 사이 줄바꿈 추가, codex는 agent_message 기록) → "`grep '^STEP_STATUS:'`가 그대로 동작하는 형식"
- 168행 claude 토큰 갱신 잠금은 claude 2.1.283에서 확인한 내용 → "(claude 2.1.283 기준)" 추가, 문장 다듬기
- 152행 "가장 빠른 리셋 시각까지": 계정 오류 파킹은 리셋이 아니라 재확인 시각 → "가장 빠른 재투입/재확인 시각까지"
- 149행과 `scheduler.py:880` 잠금 오류 문구의 `PHASE.md`/worklog: 특정 대상 프로젝트 관례 → "대상 프로젝트의 파일/작업 트리"

### 코드 주석

- 추가(이유가 드러나지 않는 코드):
  - `scheduler.py:858-860` `reconfigure(line_buffering=True)`: "nohup으로 파일에 리디렉트하면 블록 버퍼링되므로"
  - `project_loop.py:17` `MAX_SLEEP_SLICE_SEC`: "절전 후 깨어나면 monotonic sleep이 절전 시간만큼 길어지므로 잘게 나눈다"
  - `project_loop.py:255-257` 100% → 95% 순서: "100% 찬 창을 먼저 봐야 실제로 막힌 창의 리셋 시각을 쓴다"
  - `step_runner.py:65-68` 텍스트 블록 사이 `"\n"`: "텍스트 블록 사이를 끊어야 STEP_STATUS 줄이 ^…$에 걸린다"
  - `step_runner.py:48` `ClaudeEventReader` docstring: `--include-partial-messages`에 의존(없으면 텍스트가 추출되지 않아 모든 실행이 알 수 없는 실패), 반환값은 표시·기록할 텍스트(없으면 `""`)
  - `step_runner.py:100` codex 리셋 시각 주석 마무리: "…시각만 알려 줘서(날짜 없음) 파싱하지 않고 사용량 조회로 얻는다" (A2에서 파싱하게 되면 그에 맞게)
- 수정: `project_loop.py:207` "큐에서는 사용 가능해 보여도…" → 이 모듈엔 큐가 없고 `queue.json`과 혼동됨 → "대기 목록상 사용 가능해도 다른 곳(직접 사용, 리셋 스케줄러)에서 써서 한도가 찼을 수 있다"
- 정리: `step_runner.py:132` "(run-step-loop.sh와 동일)" — 다른 저장소 파일 참조라 썩기 쉬움, 설계서에만 남기기

---

## D. 개선 제안 (여유 있을 때)

1. **일시 장애와 진짜 실패 구분** (`project_loop.py:264-274`) — 네트워크/API 장애가 10분(3회 × 5분)을 넘으면 며칠짜리 실행이 끝남. 알 수 없는 실패 분기에서 `status is None`(사용량 조회도 실패)이면 일시 장애로 보고 세지 않거나, 별도의 더 큰 예산 + 지수 백오프
2. **"sign in again" 없는 인증 실패** (`step_runner.py:125`) — codex 401이 그 문구 없이 오면 알 수 없는 실패 → 사용량 조회도 401 → 약 10분 뒤 루프 전체 종료(다른 계정은 멀쩡한데). 실행과 사용량 조회가 모두 401/403이면 계정 오류로 처리. `_safe_usage`가 None 대신 HTTP 상태를 알려 주게
3. **STEP_STATUS 인식** (`step_runner.py:16-19`, `project_loop.py:222-233`)
   - 모델이 앞에서 "막히면 `STEP_STATUS: INCOMPLETE`를 출력하겠다"처럼 인용한 뒤 한도에 걸리면 루프가 종료 1로 끝남(모의 재현). `limit_hit`/`account_error`가 있고 종료 코드가 0이 아니면 마지막 텍스트 블록 외의 상태 줄보다 신호를 우선
   - 줄 끝 공백, `\r`, `**…**`, 백틱, 끝의 `.`이 붙으면 인식 못 해 성공한 step이 알 수 없는 실패가 됨 → 공백·`\r`·마크다운 감싸기 제거 후 매칭, 엄격 매칭은 실패했는데 `STEP_STATUS`가 들어 있으면 "STEP_STATUS 형식 불일치: <줄>" 출력
4. **로그 가독성** — 결과 줄(`project_loop.py:38, 157, 202, 205, 224, 247, 266, 270`)에 시각이 없음. `_run_project_loop_mode`에서 `print_fn`을 시각 접두 래퍼로 한 번에 처리. 며칠짜리 대기 중엔 대기 줄이 한 번만 찍히므로 몇 시간마다 살아 있음 표시
5. **자식 stderr 캡처** (`step_runner.py:187-197`) — CLI 시작 오류(알 수 없는 옵션, 미로그인, node 크래시)는 stderr로만 나옴. 스레드로 읽어 터미널과 `<stem>.stderr.log`에 같이 쓰고, 알 수 없는 실패 메시지에 마지막 몇 줄 포함
6. **`LoopAccount` 상태 변경 모으기** — 6곳에서 필드를 직접 수정 중(account_error 분기는 `_set_exhausted`를 손으로 복제). 상수 `READY, EXHAUSTED, ACCOUNT_ERROR`와 메서드 추가:
   ```python
   def park(self, status: str, available_at: int, reason: str) -> None:
       self.status, self.available_at, self.reason, self.runs = status, available_at, reason, 0

   def start_run(self) -> None:
       self.status, self.reason = READY, None
       self.runs += 1
   ```
   `status`에 "마지막으로 파킹된 이유이며 사용 가능 여부는 `available_at`이 결정" 한 줄 주석. `Literal`/`Enum`은 타입 체커가 없어 불필요
7. **`StepOutcome`** — `@dataclass(frozen=True)`(현재 아무도 필드를 바꾸지 않음). 선택: `__post_init__`에서 `step_status`가 허용값인지, `limit_reset_at is None or limit_hit`인지 확인
8. **claude `resetsAt` 여러 번** — 5시간 → 7일처럼 `rejected`가 여러 번 오면 마지막 값이 이김. 실제로 막는 건 더 늦은 시각이므로 `self.limit_reset_at = max(self.limit_reset_at or 0, int(resets_at))`
9. **리더 인터페이스** — 선택: 두 리더에 `signals()`를 두고 `run_step`이 한 곳에서 `StepOutcome`을 만들게
10. **잠금 fd 누수** (`project_loop.py:117-118`) — `os.open`/`mkdir` 권한 오류는 `ProjectLockError` 처리를 우회해 트레이스백. `os.pread`/`os.ftruncate` 실패 시 fd 누수 → flock 이후 코드를 `try/except: os.close(fd); raise`로
11. **잠금 정보 공백** (`scheduler.py:900`) — 계정 선택 동안 잠금 파일이 비어 있음. 잠금 직후 PID·시작 시각을 먼저 쓰고 계정 목록은 나중에 추가
12. **종료 코드 구분** — 처리되지 않은 예외도 종료 코드 1이라 INCOMPLETE·3회 실패와 구분 안 됨. SIGTERM은 관례상 143(현재는 결정대로 130 유지 중)
13. **`-n` 안내** — project-loop에서 `-n`이 요청 개수보다 적게 찾아도 경고 없음(기존 모드는 경고함). 명시한 계정 번호 중 무료 계정도 잠금 정보에 나열됨
14. **비JSON 줄 개수** — 무시한 비JSON 줄 수를 세어 알 수 없는 실패 메시지에 포함

## 참고: 리뷰에서 확인된 정상 동작

- 새 분기는 새 옵션 3개일 때만 진입, argparse 약어 충돌 없음, 기존 테스트 파일 무변경
- 모든 계정 교체 경로가 `available_at`을 미래로 두어 쉬지 않고 도는 루프 없음(interval ≥ 1 전제)
- 잠금 fd는 `os.open`(CLOEXEC)이라 자식에게 상속 안 됨, Python 수준 종료 경로에서 모두 해제
- SIGTERM 1회는 `stop_process` + `Popen` 컨텍스트 매니저로 자식 종료·파이프 정리 정상
- `~/shortform-ai/.gitignore`가 `.claude-runs/`·`.codex-runs/`를 무시하므로 `.raw.jsonl`이 커밋되지 않음
- claude 텍스트 추출은 bash `jq` 필터와 같고(블록 사이 줄바꿈만 추가), STEP_STATUS 정규식은 bash `grep`과 동일
- 실제 CLI로 한도 감지 → 파킹 → 대기 → 재개 → INCOMPLETE 종료까지 확인됨(2026-09-28)
