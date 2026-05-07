"""사이클 자동 생성.

cost function + 멤버 선호도를 사용해 다음 5주 슬롯을 그리디로 채운다.
9 멤버 → 10 슬롯이라 마지막 슬롯 1개는 비워둔다 (운영자 수동 보충 가능).
"""
from __future__ import annotations

import random
import sqlite3
from datetime import date, timedelta

from ..config import CYCLE_LENGTH_WEEKS
from ..cost import cost
from ..models import Member, Schedule
from . import member_service, schedule_service


def _next_cycle_id(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COALESCE(MAX(cycle_id), 0) AS m FROM schedule").fetchone()
    return int(row["m"]) + 1


def _last_seminar_date(conn: sqlite3.Connection) -> date | None:
    row = conn.execute("SELECT MAX(date) AS d FROM schedule").fetchone()
    return date.fromisoformat(row["d"]) if row and row["d"] else None


def upcoming_count(conn: sqlite3.Connection, today: date) -> int:
    """오늘 이후의 일정 개수 (status NOT IN 취소/완료)."""
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM schedule WHERE date > ? AND status NOT IN ('취소', '완료')",
        (today.isoformat(),),
    ).fetchone()
    return int(row["c"])


def needs_new_cycle(conn: sqlite3.Connection, today: date, threshold_weeks: int = 2) -> bool:
    """다가올 일정이 threshold_weeks 이하만 남았으면 새 사이클 필요."""
    return upcoming_count(conn, today) <= threshold_weeks


def _greedy_assign(
    members: list[Member],
    dates: list[date],
    *,
    rng: random.Random,
) -> dict[date, dict[int, str | None]]:
    """date 순서대로 (slot_1, slot_2) 슬롯에 cost 최저 멤버를 할당.

    멤버 9 → 슬롯 10 이라 마지막 슬롯은 None.
    동점일 때 비결정적 결과를 위해 cost 동률 후보 중에서 rng로 뽑는다.
    """
    assignment: dict[date, dict[int, str | None]] = {d: {1: None, 2: None} for d in dates}
    remaining = list(members)

    slot_order = []
    for d in dates:
        slot_order.append((d, 1))
        slot_order.append((d, 2))

    for d, slot_num in slot_order:
        if not remaining:
            break
        cycle_remaining_names = {m.name for m in remaining}
        scored = [(cost(m, d, cycle_remaining_names), m) for m in remaining]
        min_score = min(s for s, _ in scored)
        ties = [m for s, m in scored if s == min_score]
        chosen = rng.choice(ties)
        assignment[d][slot_num] = chosen.name
        remaining = [m for m in remaining if m.name != chosen.name]

    return assignment


def generate_next_cycle(
    conn: sqlite3.Connection,
    today: date | None = None,
    *,
    seed: int | None = None,
) -> tuple[int, list[Schedule]]:
    """다음 5주 사이클을 생성하고 DB에 INSERT.

    시작 목요일 = (현재 마지막 일정의 다음 목요일) 또는 (오늘 다음 목요일).
    """
    today = today or date.today()
    last = _last_seminar_date(conn)
    anchor = (last + timedelta(days=1)) if last else today
    start = schedule_service.next_thursday(anchor)
    # 만약 anchor 자체가 목요일이면 next_thursday가 anchor를 반환 — 우리는 다음 주를 원하므로 7일 더.
    if last is not None and start <= last:
        start = start + timedelta(days=7)

    cycle_id = _next_cycle_id(conn)
    members = member_service.get_all_active(conn)
    if not members:
        raise ValueError("active 멤버 0명 — 채널 sync 가 동작하지 않거나 채널이 비었음")
    dates = [start + timedelta(weeks=w) for w in range(CYCLE_LENGTH_WEEKS)]

    rng = random.Random(seed if seed is not None else cycle_id)
    assignment = _greedy_assign(members, dates, rng=rng)

    schedules: list[Schedule] = []
    for d in dates:
        s = Schedule(
            date=d,
            reminder_date=d - timedelta(days=1),
            slot_1=assignment[d][1],
            slot_2=assignment[d][2],
            cycle_id=cycle_id,
        )
        schedule_service.upsert(conn, s)
        schedules.append(s)

    return cycle_id, schedules


# ─────────────────────────────────────────────────────────────
# Past seminar 자동 마감
# ─────────────────────────────────────────────────────────────
def mark_past_seminars_completed(conn: sqlite3.Connection, today: date | None = None) -> list[Schedule]:
    """date <= today AND status='예정' 인 일정을 '완료'로 마킹.
    동시에 발표자의 presented_count++, last_presented = 그 날짜로 갱신.

    다운타임 후 catch-up 보장 — 여러 날 한 번에 처리 가능."""
    today = today or date.today()
    rows = conn.execute(
        "SELECT * FROM schedule WHERE date <= ? AND status = '예정' ORDER BY date ASC",
        (today.isoformat(),),
    ).fetchall()

    completed: list[Schedule] = []
    with conn:
        for row in rows:
            d = date.fromisoformat(row["date"])
            for name in (row["slot_1"], row["slot_2"]):
                if not name:
                    continue
                # 자동 완료 시점에 presented_count++ 와 last_presented 갱신.
                # last_presented는 더 최근 날짜만 반영 (옛 자료 catch-up 시 역행 방지).
                conn.execute(
                    """
                    UPDATE members
                    SET presented_count = presented_count + 1,
                        last_presented = CASE
                            WHEN last_presented IS NULL OR last_presented < ?
                            THEN ?
                            ELSE last_presented
                        END
                    WHERE name = ?
                    """,
                    (d.isoformat(), d.isoformat(), name),
                )
            conn.execute("UPDATE schedule SET status = '완료' WHERE date = ?", (d.isoformat(),))
            completed.append(
                Schedule(
                    date=d,
                    reminder_date=date.fromisoformat(row["reminder_date"]),
                    slot_1=row["slot_1"],
                    slot_2=row["slot_2"],
                    cycle_id=row["cycle_id"],
                    status="완료",
                )
            )
    return completed
