"""대체 발표자 자동 선정용 cost function.

순수 함수 — DB/Slack/시간 부수효과 없음. 단위 테스트로 모든 분기 커버.
호출자(defer_service)는 후보군을 "이번 사이클 미발표자"로 먼저 필터링한 뒤
이 함수로 점수를 매기고 최저점을 선택한다.
"""
from __future__ import annotations

import math
from datetime import date

from .config import DEFAULT_WEIGHTS, Weights
from .models import Member


def week_of_month(d: date) -> int:
    """월내 주차 (1~5). day 1-7 → 1, 8-14 → 2, ..., 29-31 → 5."""
    return math.ceil(d.day / 7)


def cost(
    member: Member,
    target_date: date,
    current_cycle_remaining: set[str],
    weights: Weights = DEFAULT_WEIGHTS,
) -> float:
    """target_date 슬롯에 member를 배정했을 때의 비용. 낮을수록 적합."""
    score = 0.0
    iso = target_date.isoformat()
    prefs = member.preferences

    # 1. 본인 회피 요청 — 가장 강력
    if iso in prefs.avoid_dates:
        score += weights.avoid_date
    if week_of_month(target_date) in prefs.avoid_weeks_of_month:
        score += weights.avoid_week

    # 2. 발표 횟수 (많이 한 사람일수록 페널티)
    score += weights.per_presented * member.presented_count

    # 3. 이번 사이클 미발표 보너스 (필터링 이중 안전망)
    if member.name in current_cycle_remaining:
        score += weights.current_cycle_bonus

    # 4. 연기 이력
    score += weights.per_defer * member.defer_count

    # 5. 직전 발표 후 경과 (연속 방지)
    if member.last_presented is not None:
        weeks_since = (target_date - member.last_presented).days / 7
        if weeks_since < weights.recent_threshold_weeks:
            score += weights.recent_penalty

    return score


def pick_replacement(
    candidates: list[Member],
    target_date: date,
    current_cycle_remaining: set[str],
    weights: Weights = DEFAULT_WEIGHTS,
) -> Member:
    """후보 중 cost 최저 멤버 반환. 동점은 이름 사전순으로 결정 (재현 가능)."""
    if not candidates:
        raise ValueError("후보 없음 — 사이클 미발표자가 비어 있음")
    return min(
        candidates,
        key=lambda m: (cost(m, target_date, current_cycle_remaining, weights), m.name),
    )
