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

# LLM (OpenAI 호환 — OpenRouter 등 provider-agnostic)
LLM_API_KEY: str | None = os.getenv("LLM_API_KEY")
LLM_API_BASE_URL: str = os.getenv("LLM_API_BASE_URL", "https://openrouter.ai/api/v1")
LLM_MODEL: str = os.getenv("LLM_MODEL", "anthropic/claude-sonnet-4.5")

CHANNEL_ID: str = os.getenv("CHANNEL_ID", "C0B1XSR0YNN")
DB_PATH: Path = Path(os.getenv("DB_PATH", "./seminar.db"))
TIMEZONE: str = os.getenv("TIMEZONE", "Asia/Seoul")
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")


# ─────────────────────────────────────────────────────────────
# 운영자 (양쪽 승인 필요)
# ─────────────────────────────────────────────────────────────
ADMIN_JJR: str = "U07GFTZ6LM8"
ADMIN_KDP: str = "U01UPAEG4F5"
ADMIN_USER_IDS: tuple[str, str] = (ADMIN_JJR, ADMIN_KDP)


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
