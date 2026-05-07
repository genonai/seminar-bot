"""cycle_service 단위 테스트 — 인메모리 sqlite로 격리."""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest

from src.db import SCHEMA
from src.models import Preferences
from src.services import cycle_service


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:", detect_types=sqlite3.PARSE_DECLTYPES)
    c.row_factory = sqlite3.Row
    for stmt in SCHEMA:
        c.execute(stmt)
    return c


def _seed_members(conn: sqlite3.Connection, names: list[str], prefs: dict[str, Preferences] | None = None) -> None:
    prefs = prefs or {}
    with conn:
        for n in names:
            p = prefs.get(n, Preferences())
            conn.execute(
                "INSERT INTO members (name, slack_user_id, preferences) VALUES (?, ?, ?)",
                (n, "U" + n, p.to_json()),
            )


# ─────────────────────────────────────────────────────────────
# generate_next_cycle
# ─────────────────────────────────────────────────────────────
def test_generate_first_cycle_starts_next_thursday() -> None:
    conn = _conn()
    _seed_members(conn, ["A", "B", "C", "D", "E", "F", "G", "H", "I"])
    today = date(2026, 5, 5)             # Tuesday
    cid, schedules = cycle_service.generate_next_cycle(conn, today, seed=0)
    assert cid == 1
    assert len(schedules) == 5
    assert schedules[0].date == date(2026, 5, 7)   # 다음 목요일
    for i in range(1, 5):
        assert schedules[i].date == schedules[0].date + timedelta(weeks=i)


def test_generate_next_cycle_after_existing() -> None:
    conn = _conn()
    _seed_members(conn, ["A", "B", "C", "D", "E", "F", "G", "H", "I"])
    today = date(2026, 5, 5)
    cycle_service.generate_next_cycle(conn, today, seed=0)   # cycle 1: 5/7..6/4
    cid2, schedules2 = cycle_service.generate_next_cycle(conn, today, seed=0)
    assert cid2 == 2
    # 다음 사이클은 cycle 1의 마지막 날짜 다음 목요일부터
    assert schedules2[0].date == date(2026, 6, 11)


def test_generate_distinct_members_per_cycle() -> None:
    conn = _conn()
    _seed_members(conn, ["A", "B", "C", "D", "E", "F", "G", "H", "I"])
    today = date(2026, 5, 5)
    _, schedules = cycle_service.generate_next_cycle(conn, today, seed=42)
    assigned = []
    for s in schedules:
        if s.slot_1:
            assigned.append(s.slot_1)
        if s.slot_2:
            assigned.append(s.slot_2)
    # 9 members 모두 1번씩만 등장 (중복 없음)
    assert len(assigned) == 9
    assert len(set(assigned)) == 9


def test_generate_last_slot_empty() -> None:
    conn = _conn()
    _seed_members(conn, ["A", "B", "C", "D", "E", "F", "G", "H", "I"])
    today = date(2026, 5, 5)
    _, schedules = cycle_service.generate_next_cycle(conn, today, seed=0)
    # 9 멤버 / 10 슬롯 → 마지막 주 slot_2 가 None
    assert schedules[4].slot_2 is None


def test_avoid_dates_respected_in_assignment() -> None:
    conn = _conn()
    # 'A'가 5/7 회피
    prefs = {"A": Preferences(avoid_dates=["2026-05-07"])}
    _seed_members(conn, ["A", "B", "C", "D", "E", "F", "G", "H", "I"], prefs=prefs)
    today = date(2026, 5, 5)
    _, schedules = cycle_service.generate_next_cycle(conn, today, seed=0)
    # A는 5/7에 절대 안 들어가야 함 (cost +1000)
    first = schedules[0]
    assert first.slot_1 != "A"
    assert first.slot_2 != "A"


# ─────────────────────────────────────────────────────────────
# needs_new_cycle / mark_past_seminars_completed
# ─────────────────────────────────────────────────────────────
def test_needs_new_cycle_when_empty() -> None:
    conn = _conn()
    today = date(2026, 5, 5)
    assert cycle_service.needs_new_cycle(conn, today, threshold_weeks=2)


def test_needs_new_cycle_threshold() -> None:
    conn = _conn()
    _seed_members(conn, ["A", "B", "C", "D", "E", "F", "G", "H", "I"])
    today = date(2026, 5, 5)
    cycle_service.generate_next_cycle(conn, today, seed=0)  # 5주 사이클
    # 직후엔 5개 일정 — threshold 2 보다 많음
    assert not cycle_service.needs_new_cycle(conn, today, threshold_weeks=2)
    # 4주 후 시점이면 1개만 남음 → True
    later = today + timedelta(weeks=4)
    assert cycle_service.needs_new_cycle(conn, later, threshold_weeks=2)


def test_mark_past_seminars_completed() -> None:
    conn = _conn()
    _seed_members(conn, ["A", "B", "C", "D", "E", "F", "G", "H", "I"])
    today = date(2026, 5, 5)
    cycle_service.generate_next_cycle(conn, today, seed=0)
    # 시간을 1주 앞당기면 5/7 만 지난 상태
    completed = cycle_service.mark_past_seminars_completed(conn, today=date(2026, 5, 8))
    assert len(completed) == 1
    assert completed[0].date == date(2026, 5, 7)
    # 발표자 stats 갱신 확인
    rows = conn.execute(
        "SELECT name, presented_count, last_presented FROM members WHERE last_presented IS NOT NULL"
    ).fetchall()
    assert len(rows) == 2  # slot_1, slot_2 둘 다 갱신
    for r in rows:
        assert r["presented_count"] == 1
        assert r["last_presented"] == "2026-05-07"
