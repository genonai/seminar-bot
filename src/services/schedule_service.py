"""세미나 일정 조회/수정."""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from ..config import REMINDER_WEEKDAY, SEMINAR_WEEKDAY
from ..models import Schedule


def _row_to_schedule(row: sqlite3.Row) -> Schedule:
    keys = set(row.keys())
    return Schedule(
        date=date.fromisoformat(row["date"]),
        reminder_date=date.fromisoformat(row["reminder_date"]),
        slot_1=row["slot_1"],
        slot_2=row["slot_2"],
        cycle_id=row["cycle_id"],
        status=row["status"],
        slot_1_topic=row["slot_1_topic"] if "slot_1_topic" in keys else None,
        slot_2_topic=row["slot_2_topic"] if "slot_2_topic" in keys else None,
        notes=row["notes"] if "notes" in keys else None,
    )


def next_thursday(today: date) -> date:
    """today 포함, 다음 목요일."""
    delta = (SEMINAR_WEEKDAY - today.weekday()) % 7
    return today + timedelta(days=delta)


def reminder_date_for(seminar: date) -> date:
    """세미나 날짜 → 자료 마감일(전날 수요일)."""
    delta = (seminar.weekday() - REMINDER_WEEKDAY) % 7
    return seminar - timedelta(days=delta if delta > 0 else 7)


def get_upcoming(
    conn: sqlite3.Connection, today: date | None = None, limit: int = 5,
) -> list[Schedule]:
    """다가올 일정. status='취소'/'완료' 제외. 당일 회차도 포함 (date >= today).
    당일 cron(16:00) 이 status='완료' 로 마킹하면 그 시점부터 자동으로 빠진다."""
    today = today or date.today()
    rows = conn.execute(
        """
        SELECT * FROM schedule
        WHERE date >= ? AND status NOT IN ('취소', '완료')
        ORDER BY date ASC
        LIMIT ?
        """,
        (today.isoformat(), limit),
    ).fetchall()
    return [_row_to_schedule(r) for r in rows]


def get_by_date(conn: sqlite3.Connection, d: date) -> Schedule | None:
    row = conn.execute(
        "SELECT * FROM schedule WHERE date = ?", (d.isoformat(),)
    ).fetchone()
    return _row_to_schedule(row) if row else None


def upsert(conn: sqlite3.Connection, s: Schedule) -> None:
    """schedule 삽입 또는 갱신 (date PK 기준)."""
    with conn:
        conn.execute(
            """
            INSERT INTO schedule (date, reminder_date, slot_1, slot_2, status, cycle_id)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
              reminder_date = excluded.reminder_date,
              slot_1 = excluded.slot_1,
              slot_2 = excluded.slot_2,
              status = excluded.status,
              cycle_id = excluded.cycle_id
            """,
            (
                s.date.isoformat(),
                s.reminder_date.isoformat(),
                s.slot_1,
                s.slot_2,
                s.status,
                s.cycle_id,
            ),
        )


def set_topic(
    conn: sqlite3.Connection, target_date: date, presenter_name: str, topic: str
) -> bool:
    """target_date의 slot_1 또는 slot_2가 presenter_name 이면 해당 슬롯의 topic 갱신.
    Returns True if updated."""
    s = get_by_date(conn, target_date)
    if s is None:
        return False
    slot = s.slot_of(presenter_name)
    if slot is None:
        return False
    column = "slot_1_topic" if slot == 1 else "slot_2_topic"
    with conn:
        conn.execute(
            f"UPDATE schedule SET {column} = ? WHERE date = ?",
            (topic, target_date.isoformat()),
        )
    return True


def set_notes(conn: sqlite3.Connection, target_date: date, notes: str | None) -> bool:
    """schedule.notes 갱신. 빈 문자열/None 이면 NULL로 클리어."""
    s = get_by_date(conn, target_date)
    if s is None:
        return False
    payload = notes.strip() if notes else None
    with conn:
        conn.execute(
            "UPDATE schedule SET notes = ? WHERE date = ?",
            (payload, target_date.isoformat()),
        )
    return True


def get_next_seminar(
    conn: sqlite3.Connection, today: date | None = None
) -> Schedule | None:
    """오늘 이후 가장 가까운 1개 일정 (월요일 알림용)."""
    today = today or date.today()
    row = conn.execute(
        """
        SELECT * FROM schedule
        WHERE date >= ? AND status NOT IN ('취소', '완료')
        ORDER BY date ASC LIMIT 1
        """,
        (today.isoformat(),),
    ).fetchone()
    return _row_to_schedule(row) if row else None


def set_presenter(
    conn: sqlite3.Connection,
    target_date: date,
    slot: int,
    name: str | None,
    *,
    topic: str | None = None,
) -> Schedule:
    """target_date의 slot_N(=1|2)에 name 배정. name=None이면 해당 슬롯 비움 (토픽도 같이 클리어).
    topic 주어지면 같은 트랜잭션에 갱신. name 채우는데 topic=None이면 기존 슬롯 토픽 클리어 (orphan 방지).

    raise ValueError: 일정 없음 / slot 잘못됨 / 같은 사람이 다른 슬롯에 이미 있음.
    """
    if slot not in (1, 2):
        raise ValueError(f"slot은 1 또는 2 (got {slot})")
    s = get_by_date(conn, target_date)
    if s is None:
        raise ValueError(f"{target_date} 일정 없음")
    other_slot_name = s.slot_2 if slot == 1 else s.slot_1
    if name is not None and other_slot_name == name:
        raise ValueError(f"{name}님이 같은 날 다른 슬롯에 이미 배정됨")

    name_col = f"slot_{slot}"
    topic_col = f"slot_{slot}_topic"
    # 이름이 바뀌면 (또는 비우면) 그 슬롯의 토픽은 새 사람 인계 방지 위해 항상 함께 리셋.
    # 호출자가 topic 을 명시했으면 그것으로 덮어씀.
    new_topic = topic if name is not None else None
    with conn:
        conn.execute(
            f"UPDATE schedule SET {name_col} = ?, {topic_col} = ? WHERE date = ?",
            (name, new_topic, target_date.isoformat()),
        )
    return get_by_date(conn, target_date)  # type: ignore[return-value]


def replace_presenter(
    conn: sqlite3.Connection, target_date: date, old_name: str, new_name: str
) -> None:
    """해당 날짜 schedule에서 old_name이 들어있는 슬롯을 new_name으로 교체.
    내부적으로 set_presenter 호출 — 토픽은 자동 클리어."""
    s = get_by_date(conn, target_date)
    if s is None:
        raise ValueError(f"{target_date} 일정 없음")
    slot = s.slot_of(old_name)
    if slot is None:
        raise ValueError(f"{target_date} 일정에 {old_name} 없음 (slot_1={s.slot_1}, slot_2={s.slot_2})")
    set_presenter(conn, target_date, slot, new_name)
