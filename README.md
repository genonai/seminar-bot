# seminar-bot

Genon AI 사내 주간 세미나(매주 목 14:00) 운영 슬랙 봇.

## 기능 (Phase 1)

- `/세미나-일정` — 다가올 5주 일정 ephemeral 조회 (본인 차례 ⭐)
- `/세미나-연기` — 자연어 멀티턴 LLM 대화로 연기 신청 → 진재님 + 자동 선정 대체자 양쪽 승인 → 스케줄 갱신
- `/세미나-선호도` — 자연어 LLM 대화로 평상시 선호도 (회피 날짜/주차/슬롯) 등록
- 봇 DM에 자유 메시지 → intent router LLM이 분류해서 적절한 흐름으로 진입 (defer / preference / schedule_question / other)

## 디렉토리

```
src/
  config.py           환경변수 + cost function 가중치 + 멤버 로스터
  db.py               sqlite 연결 + 스키마
  models.py           Member, Schedule, DeferRequest, Preferences
  cost.py             대체자 선정 cost function (순수)
  llm_service.py      OpenAI 호환 클라이언트 + intent router + tools (submit_defer, submit_preferences)
  services/
    schedule_service.py   일정 조회/수정
    member_service.py     멤버 CRUD
    preference_service.py 선호도 영구 저장
    defer_service.py      연기 상태 머신 + 대체자 선정 (Tier 1 free agent → Tier 2 cycle)
    draft_service.py      DM 멀티턴 대화 상태
  slack/
    app.py              Bolt App 빌더
    commands.py         슬래시 핸들러
    dm.py               DM 메시지 라우팅
    actions.py          버튼 핸들러
    flows.py            LLM ↔ DB ↔ Slack 오케스트레이션
    messages.py         Block Kit + 텍스트 빌더
    guards.py           채널/멤버 가드
scripts/
  init_db.py            스키마 생성
  seed_members.py       9명 멤버 시드
  seed_schedule.py      5주 사이클 시드 (랜덤 또는 결정적)
  verify_slack.py       토큰 + Socket Mode + 채널 검증
  admin_test_seed.py    셀프 테스트용 (운영 시작 전 --undo)
deploy/
  seminar-bot.service   systemd unit
  install.sh            181 서버 초기 설치
  sync.sh               로컬 → 181 동기화
tests/
  test_cost.py          cost function 35 케이스
```

## 로컬 개발

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
cp .env.example .env       # 토큰/키 채우기
.venv/bin/python scripts/verify_slack.py
.venv/bin/python scripts/init_db.py
.venv/bin/python scripts/seed_members.py
.venv/bin/python scripts/seed_schedule.py --shuffle
.venv/bin/python -m src.main
```

## 환경변수 (`.env`)

| 변수 | 설명 |
|---|---|
| `SLACK_BOT_TOKEN` | `xoxb-...` Bot User OAuth Token |
| `SLACK_APP_TOKEN` | `xapp-...` App-Level Token (Socket Mode, scope `connections:write`) |
| `SLACK_SIGNING_SECRET` | App Signing Secret |
| `LLM_API_KEY` | OpenAI 호환 API 키 (OpenRouter 등) |
| `LLM_API_BASE_URL` | LLM API base URL (기본 `https://openrouter.ai/api/v1`) |
| `LLM_MODEL` | LLM 모델 ID (기본 `anthropic/claude-sonnet-4.5`) |
| `DB_PATH` | sqlite 파일 경로 (181 운영: `/var/lib/seminar-bot/seminar.db`) |
| `CHANNEL_ID` | 세미나 채널 ID |
| `ADMIN_USER_IDS` | 운영자 슬랙 ID 콤마 구분 |
| `TIMEZONE` | 기본 `Asia/Seoul` |
| `LOG_LEVEL` | 기본 `INFO` |

## 배포 (Docker)

서버에 시스템 패키지(python3.11 등) 설치 안 함. Docker 이미지 안에서 격리.

### 처음 한 번 (서버)

```bash
# repo clone (deploy key 사용)
sudo mkdir -p /opt/seminar-bot
sudo chown -R $USER:$USER /opt/seminar-bot
git clone git@github.com:genonai/seminar-bot.git /opt/seminar-bot
cd /opt/seminar-bot

# .env 채우기 (로컬에서 scp 또는 vi)
chmod 600 .env

# 이미지 빌드 + 시작
bash deploy/install.sh

# 1차 schedule 시드 (한 번만; 멤버는 entrypoint에서 자동 시드)
docker compose exec bot python scripts/seed_schedule.py --shuffle
```

### 코드 갱신

```bash
cd /opt/seminar-bot
git pull
docker compose up -d --build
```

### 운영 명령

```bash
docker compose logs -f      # 실시간 로그
docker compose ps           # 컨테이너 상태
docker compose restart      # 재시작
docker compose down         # 정지
```

DB 백업: `/opt/seminar-bot/data/seminar.db` 파일을 정기적으로 복사 (호스트 볼륨 mount).

## 테스트

```bash
.venv/bin/pytest -q
```

cost function 단위 테스트만 35 케이스. LLM/Slack 통합 테스트는 실 환경에서.
