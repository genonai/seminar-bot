"""cost function 단위 테스트.

원칙: 한 케이스가 한 가중치만 건드리도록 격리해서 회귀 추적이 쉽게 한다.
"""
from __future__ import annotations

from datetime import date

import pytest

from src.config import DEFAULT_WEIGHTS, Weights
from src.cost import cost, pick_replacement, week_of_month
from src.models import Member, Preferences


# ─────────────────────────────────────────────────────────────
# week_of_month
# ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "d,expected",
    [
        (date(2026, 5, 1), 1),
        (date(2026, 5, 7), 1),
        (date(2026, 5, 8), 2),
        (date(2026, 5, 14), 2),
        (date(2026, 5, 15), 3),
        (date(2026, 5, 21), 3),
        (date(2026, 5, 22), 4),
        (date(2026, 5, 28), 4),
        (date(2026, 5, 29), 5),
        (date(2026, 5, 31), 5),
    ],
)
def test_week_of_month(d: date, expected: int) -> None:
    assert week_of_month(d) == expected


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────
def make(
    name: str = "이선호",
    *,
    avoid_dates: list[str] | None = None,
    avoid_weeks: list[int] | None = None,
    presented: int = 0,
    deferred: int = 0,
    last: date | None = None,
) -> Member:
    return Member(
        name=name,
        slack_user_id="U" + name,
        preferences=Preferences(
            avoid_dates=avoid_dates or [],
            avoid_weeks_of_month=avoid_weeks or [],
        ),
        presented_count=presented,
        defer_count=deferred,
        last_presented=last,
    )


TARGET = date(2026, 5, 14)            # 목요일, 5월 둘째주


# ─────────────────────────────────────────────────────────────
# 베이스라인
# ─────────────────────────────────────────────────────────────
def test_blank_member_zero_cost() -> None:
    """선호도/이력 모두 비어있고 사이클 후보도 아님 → 0점."""
    m = make()
    assert cost(m, TARGET, current_cycle_remaining=set()) == 0.0


# ─────────────────────────────────────────────────────────────
# 1. avoid_dates / avoid_weeks
# ─────────────────────────────────────────────────────────────
def test_avoid_date_hit() -> None:
    m = make(avoid_dates=["2026-05-14"])
    assert cost(m, TARGET, set()) == DEFAULT_WEIGHTS.avoid_date


def test_avoid_date_miss() -> None:
    m = make(avoid_dates=["2026-05-21"])
    assert cost(m, TARGET, set()) == 0.0


def test_avoid_week_of_month_hit() -> None:
    # 2026-05-14는 5월 둘째주
    m = make(avoid_weeks=[2])
    assert cost(m, TARGET, set()) == DEFAULT_WEIGHTS.avoid_week


def test_avoid_week_miss() -> None:
    m = make(avoid_weeks=[4])
    assert cost(m, TARGET, set()) == 0.0


def test_avoid_date_and_week_stack() -> None:
    m = make(avoid_dates=["2026-05-14"], avoid_weeks=[2])
    expected = DEFAULT_WEIGHTS.avoid_date + DEFAULT_WEIGHTS.avoid_week
    assert cost(m, TARGET, set()) == expected


# ─────────────────────────────────────────────────────────────
# 2. presented_count
# ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("n", [0, 1, 3, 7])
def test_presented_count_scales(n: int) -> None:
    m = make(presented=n)
    assert cost(m, TARGET, set()) == DEFAULT_WEIGHTS.per_presented * n


# ─────────────────────────────────────────────────────────────
# 3. 사이클 미발표 보너스
# ─────────────────────────────────────────────────────────────
def test_current_cycle_bonus_applies_when_in_set() -> None:
    m = make(name="이선호")
    assert cost(m, TARGET, {"이선호"}) == DEFAULT_WEIGHTS.current_cycle_bonus


def test_current_cycle_bonus_skipped_when_not_in_set() -> None:
    m = make(name="이선호")
    assert cost(m, TARGET, {"임종석"}) == 0.0


# ─────────────────────────────────────────────────────────────
# 4. defer_count
# ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("n", [0, 1, 4])
def test_defer_count_scales(n: int) -> None:
    m = make(deferred=n)
    assert cost(m, TARGET, set()) == DEFAULT_WEIGHTS.per_defer * n


# ─────────────────────────────────────────────────────────────
# 5. recent presentation penalty
# ─────────────────────────────────────────────────────────────
def test_recent_penalty_within_threshold() -> None:
    # 1주 전 발표 → 페널티
    m = make(last=date(2026, 5, 7))
    assert cost(m, TARGET, set()) == DEFAULT_WEIGHTS.recent_penalty


def test_no_penalty_at_threshold_boundary() -> None:
    # 정확히 2주 전 → weeks_since == 2.0, NOT < 2 → 페널티 없음
    m = make(last=date(2026, 4, 30))
    assert cost(m, TARGET, set()) == 0.0


def test_no_penalty_beyond_threshold() -> None:
    m = make(last=date(2026, 4, 16))      # 4주 전
    assert cost(m, TARGET, set()) == 0.0


def test_no_penalty_when_never_presented() -> None:
    m = make(last=None)
    assert cost(m, TARGET, set()) == 0.0


# ─────────────────────────────────────────────────────────────
# 6. 누적 시나리오
# ─────────────────────────────────────────────────────────────
def test_combined_realistic_scenario() -> None:
    # avoid_date 위반 + 3회 발표 + 1회 연기 + 사이클 후보 + 1주 전 발표
    m = make(
        name="이선호",
        avoid_dates=["2026-05-14"],
        presented=3,
        deferred=1,
        last=date(2026, 5, 7),
    )
    expected = (
        DEFAULT_WEIGHTS.avoid_date
        + DEFAULT_WEIGHTS.per_presented * 3
        + DEFAULT_WEIGHTS.current_cycle_bonus
        + DEFAULT_WEIGHTS.per_defer
        + DEFAULT_WEIGHTS.recent_penalty
    )
    assert cost(m, TARGET, {"이선호"}) == expected


# ─────────────────────────────────────────────────────────────
# 7. 가중치 커스터마이즈
# ─────────────────────────────────────────────────────────────
def test_custom_weights_override() -> None:
    custom = Weights(per_presented=10.0)
    m = make(presented=5)
    assert cost(m, TARGET, set(), weights=custom) == 50.0


# ─────────────────────────────────────────────────────────────
# pick_replacement
# ─────────────────────────────────────────────────────────────
def test_pick_replacement_chooses_lowest_cost() -> None:
    a = make(name="이선호", presented=5)
    b = make(name="임종석", presented=1)
    c = make(name="조채린", presented=3)
    chosen = pick_replacement([a, b, c], TARGET, current_cycle_remaining=set())
    assert chosen.name == "임종석"


def test_pick_replacement_breaks_ties_by_name() -> None:
    # 모두 동일 점수 → 사전순으로 먼저인 이름
    a = make(name="조채린")
    b = make(name="이선호")
    c = make(name="임종석")
    chosen = pick_replacement([a, b, c], TARGET, set())
    assert chosen.name == "이선호"  # 한글 사전순


def test_pick_replacement_respects_avoid() -> None:
    a = make(name="이선호", avoid_dates=["2026-05-14"])  # +1000
    b = make(name="임종석", presented=2)                  # +100
    chosen = pick_replacement([a, b], TARGET, set())
    assert chosen.name == "임종석"


def test_pick_replacement_empty_raises() -> None:
    with pytest.raises(ValueError):
        pick_replacement([], TARGET, set())
