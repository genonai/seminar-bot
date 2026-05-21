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
    conn: sqlite3.Connection, today: date | None = None, limit: int = 5
) -> list[Schedule]:
    """오늘 이후(strictly >)의 일정. status='취소'/'완료' 제외.
    오늘 자 세미나는 제외된다 (당일 시점에는 이미 진행 중이거나 끝남)."""
    today = today or date.today()
    rows = conn.execute(
        """
        SELECT * FROM schedule
        WHERE date > ? AND status NOT IN ('취소', '완료')
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
    """target_date의 slot_1이 presenter_name 이면 topic 갱신. Returns True if updated."""
    s = get_by_date(conn, target_date)
    if s is None or s.slot_1 != presenter_name:
        return False
    with conn:
        conn.execute(
            "UPDATE schedule SET slot_1_topic = ? WHERE date = ?",
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


def replace_presenter(
    conn: sqlite3.Connection, target_date: date, old_name: str, new_name: str
) -> None:
    """해당 날짜 schedule에서 old_name을 new_name으로 교체."""
    s = get_by_date(conn, target_date)
    if s is None:
        raise ValueError(f"{target_date} 일정 없음")
    if s.slot_1 != old_name:
        raise ValueError(f"{target_date} 일정에 {old_name} 없음 (slot_1={s.slot_1})")
    s.slot_1 = new_name
    upsert(conn, s)
