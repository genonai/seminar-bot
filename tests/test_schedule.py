"""schedule_service.set_presenter 단위 테스트 — 토픽 orphan 방지 + 슬롯 분기."""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta

import pytest

from src.db import MIGRATIONS, SCHEMA
from src.models import Schedule
from src.services import schedule_service


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:", detect_types=sqlite3.PARSE_DECLTYPES)
    c.row_factory = sqlite3.Row
    for stmt in SCHEMA:
        c.execute(stmt)
    for table, column, ddl in MIGRATIONS:
        cols = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    return c


def _make_schedule(conn, *, slot_1=None, slot_2=None, slot_1_topic=None, slot_2_topic=None):
    d = date(2026, 6, 4)
    s = Schedule(
        date=d, reminder_date=d - timedelta(days=1),
        slot_1=slot_1, slot_2=slot_2, cycle_id=1,
        slot_1_topic=slot_1_topic, slot_2_topic=slot_2_topic,
    )
    schedule_service.upsert(conn, s)
    return d


def test_set_presenter_assigns_and_clears_topic() -> None:
    """기존 슬롯에 누가 있고 토픽이 있을 때, 다른 사람으로 교체하면 토픽도 자동 클리어."""
    conn = _conn()
    d = _make_schedule(conn, slot_1="A", slot_1_topic="A의 토픽")
    schedule_service.set_presenter(conn, d, 1, "B")
    s = schedule_service.get_by_date(conn, d)
    assert s.slot_1 == "B"
    assert s.slot_1_topic is None  # orphan 방지


def test_set_presenter_with_explicit_topic() -> None:
    conn = _conn()
    d = _make_schedule(conn, slot_1="A", slot_1_topic="A의 토픽")
    schedule_service.set_presenter(conn, d, 1, "B", topic="B의 새 토픽")
    s = schedule_service.get_by_date(conn, d)
    assert s.slot_1 == "B"
    assert s.slot_1_topic == "B의 새 토픽"


def test_set_presenter_clears_slot() -> None:
    conn = _conn()
    d = _make_schedule(conn, slot_2="X", slot_2_topic="X의 토픽")
    schedule_service.set_presenter(conn, d, 2, None)
    s = schedule_service.get_by_date(conn, d)
    assert s.slot_2 is None
    assert s.slot_2_topic is None


def test_set_presenter_rejects_duplicate_within_day() -> None:
    conn = _conn()
    d = _make_schedule(conn, slot_1="A")
    with pytest.raises(ValueError):
        schedule_service.set_presenter(conn, d, 2, "A")  # 같은 사람 두 슬롯 금지


def test_set_presenter_rejects_invalid_slot() -> None:
    conn = _conn()
    d = _make_schedule(conn, slot_1="A")
    with pytest.raises(ValueError):
        schedule_service.set_presenter(conn, d, 3, "B")


def test_set_topic_dispatches_to_correct_slot() -> None:
    """slot_2 의 사람도 set_topic 으로 자기 토픽 등록 가능해야 함."""
    conn = _conn()
    d = _make_schedule(conn, slot_1="A", slot_2="B")
    ok = schedule_service.set_topic(conn, d, "B", "B의 토픽")
    assert ok is True
    s = schedule_service.get_by_date(conn, d)
    assert s.slot_1_topic is None
    assert s.slot_2_topic == "B의 토픽"
