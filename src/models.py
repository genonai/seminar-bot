"""도메인 모델 — DB row와 1:1로 대응."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from typing import Any


# ─────────────────────────────────────────────────────────────
# 선호도 (members.preferences JSON 컬럼 직렬화 형식)
# ─────────────────────────────────────────────────────────────
@dataclass
class Preferences:
    avoid_dates: list[str] = field(default_factory=list)        # YYYY-MM-DD
    avoid_weeks_of_month: list[int] = field(default_factory=list)  # 1~5
    preferred_slot: int | None = None                            # 1 / 2 / None

    def to_json(self) -> str:
        return json.dumps(
            {
                "avoid_dates": self.avoid_dates,
                "avoid_weeks_of_month": self.avoid_weeks_of_month,
                "preferred_slot": self.preferred_slot,
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, raw: str | None) -> "Preferences":
        if not raw:
            return cls()
        data: dict[str, Any] = json.loads(raw)
        return cls(
            avoid_dates=list(data.get("avoid_dates", [])),
            avoid_weeks_of_month=list(data.get("avoid_weeks_of_month", [])),
            preferred_slot=data.get("preferred_slot"),
        )


@dataclass
class Member:
    name: str
    slack_user_id: str
    preferences: Preferences = field(default_factory=Preferences)
    presented_count: int = 0
    defer_count: int = 0
    last_presented: date | None = None    # 직전 발표일


@dataclass
class Schedule:
    date: date                            # 세미나 (목)
    reminder_date: date                   # 자료 마감 (수)
    slot_1: str | None
    slot_2: str | None
    cycle_id: int
    status: str = "예정"                  # 예정 / 완료 / 취소
    slot_1_topic: str | None = None
    slot_2_topic: str | None = None

    def presenters(self) -> list[str]:
        return [s for s in (self.slot_1, self.slot_2) if s]

    def topic_for(self, name: str) -> str | None:
        if self.slot_1 == name:
            return self.slot_1_topic
        if self.slot_2 == name:
            return self.slot_2_topic
        return None


@dataclass
class DeferRequest:
    requester: str
    original_date: date
    reason: str
    id: int | None = None
    status: str = "pending"               # pending / approved / rejected
    approver_jjr_at: str | None = None
    approver_kdp_at: str | None = None
    replacement: str | None = None
    resolved_at: str | None = None

    def fully_approved(self) -> bool:
        return self.approver_jjr_at is not None and self.approver_kdp_at is not None
