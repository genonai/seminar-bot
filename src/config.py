"""환경 변수 및 cost function 가중치."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


# ─────────────────────────────────────────────────────────────
# Slack / 인프라
# ─────────────────────────────────────────────────────────────
SLACK_BOT_TOKEN: str | None = os.getenv("SLACK_BOT_TOKEN")
SLACK_APP_TOKEN: str | None = os.getenv("SLACK_APP_TOKEN")
SLACK_SIGNING_SECRET: str | None = os.getenv("SLACK_SIGNING_SECRET")

# LLM / VLM (OpenAI 호환 — OpenRouter, 사내 GenOS 등 provider-agnostic)
LLM_API_KEY: str | None = os.getenv("LLM_API_KEY")
LLM_API_BASE_URL: str = os.getenv("LLM_API_BASE_URL", "https://openrouter.ai/api/v1")
LLM_MODEL: str = os.getenv("LLM_MODEL", "anthropic/claude-sonnet-4.5")

# VLM은 별도 endpoint 가능 (GenOS의 multimodal serving 등). 미설정 시 LLM_* 폴백.
VLM_API_KEY: str | None = os.getenv("VLM_API_KEY") or LLM_API_KEY
VLM_API_BASE_URL: str = os.getenv("VLM_API_BASE_URL") or LLM_API_BASE_URL
VLM_MODEL: str = os.getenv("VLM_MODEL", "anthropic/claude-sonnet-4.5")

# Weaviate (vector DB) — 181 서버에 이미 떠있는 인스턴스 사용 (BYOV: vectorizer 없음)
WEAVIATE_URL: str = os.getenv("WEAVIATE_URL", "http://localhost:8080")

# Embedding — Weaviate vectorizer 없어서 직접 임베딩 생성. 사내 GenOS (OpenAI 호환).
#   EMBEDDING_API_BASE_URL = https://<host>/api/gateway/rep/serving/{serving_id}/v1
#   EMBEDDING_MODEL = serving_rev_id (예: '559')  미설정 시 GET /v1/models 로 자동 발견
EMBEDDING_API_KEY: str | None = os.getenv("EMBEDDING_API_KEY")
EMBEDDING_API_BASE_URL: str = os.getenv(
    "EMBEDDING_API_BASE_URL",
    "https://genos.genon.ai/api/gateway/rep/serving/10/v1",
)
EMBEDDING_MODEL: str = os.getenv("EMBEDDING_MODEL", "")
EMBEDDING_DIMENSIONS: int = int(os.getenv("EMBEDDING_DIMENSIONS", "1024"))

CHANNEL_ID: str = os.getenv("CHANNEL_ID", "C0B1XSR0YNN")
DB_PATH: Path = Path(os.getenv("DB_PATH", "./seminar.db"))
TIMEZONE: str = os.getenv("TIMEZONE", "Asia/Seoul")
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")


# ─────────────────────────────────────────────────────────────
# 운영자 (.env 의 ADMIN_USER_IDS 콤마 구분 리스트로 관리)
#   첫 번째 ID = primary (연기 승인 DM, 채널 공지에서 멘션, /세미나-재추첨 등)
# ─────────────────────────────────────────────────────────────
def _parse_admin_ids(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ("U07GFTZ6LM8", "U01UPAEG4F5")
    parts = [x.strip() for x in raw.split(",") if x.strip()]
    return tuple(parts) if parts else ("U07GFTZ6LM8", "U01UPAEG4F5")


ADMIN_USER_IDS: tuple[str, ...] = _parse_admin_ids(os.getenv("ADMIN_USER_IDS"))
ADMIN_JJR: str = ADMIN_USER_IDS[0]                                    # primary
ADMIN_KDP: str = ADMIN_USER_IDS[1] if len(ADMIN_USER_IDS) > 1 else ADMIN_JJR


# ─────────────────────────────────────────────────────────────
# 발표 풀 (9명, 진재님 + 박기돈 수석 제외)
# ─────────────────────────────────────────────────────────────
MEMBER_ROSTER: dict[str, str] = {
    "이선호": "U09SVDQELUF",
    "임종석": "U09KMARPZN3",
    "조채린": "U0ATH6B5DFZ",
    "황산하": "U0877ASFARJ",
    "김재선": "U0AUBGR2H97",
    "이가은": "U09BN89MS7N",
    "이민형": "U09L6ARR3MJ",
    "김현근": "U07N3TVPW07",
    "허성환": "U086XH6QJNT",
}


# ─────────────────────────────────────────────────────────────
# Cost function 가중치 (운영하며 조정 가능)
# ─────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Weights:
    avoid_date: float = 1000.0
    avoid_week: float = 500.0
    per_presented: float = 50.0
    current_cycle_bonus: float = -30.0
    per_defer: float = 20.0
    recent_penalty: float = 100.0
    recent_threshold_weeks: int = 2


DEFAULT_WEIGHTS = Weights()


# ─────────────────────────────────────────────────────────────
# 도메인 상수
# ─────────────────────────────────────────────────────────────
SEMINAR_WEEKDAY: int = 3              # 목요일 (Mon=0)
REMINDER_WEEKDAY: int = 2             # 수요일 (자료 마감)
DEFER_DEADLINE_DAYS: int = 7          # 세미나 7일 전까지 신청 가능
CYCLE_LENGTH_WEEKS: int = 5           # 9명 / 2슬롯 → 5주에 한 번 추첨
SLOTS_PER_WEEK: int = 2

# 대체자 거절 시 차순위 시도 횟수 (초과 시 진재님 escalation)
MAX_REPLACEMENT_ATTEMPTS: int = 3
