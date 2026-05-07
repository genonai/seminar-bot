"""admin_service 단위 테스트."""
from __future__ import annotations

import sqlite3

from src.db import MIGRATIONS, SCHEMA
from src.services import admin_service


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


def test_bootstrap_first_is_primary() -> None:
    conn = _conn()
    n = admin_service.bootstrap_if_empty(conn, ["U_A", "U_B", "U_C"])
    assert n == 3
    rows = admin_service.list_admins(conn)
    primaries = [r for r in rows if r["is_primary"] == 1]
    assert len(primaries) == 1
    assert primaries[0]["slack_user_id"] == "U_A"
    assert admin_service.get_primary_admin_id(conn) == "U_A"


def test_bootstrap_idempotent() -> None:
    conn = _conn()
    admin_service.bootstrap_if_empty(conn, ["U_A"])
    n = admin_service.bootstrap_if_empty(conn, ["U_X", "U_Y"])
    assert n == 0
    assert admin_service.get_admin_ids(conn) == ("U_A",)


def test_add_admin() -> None:
    conn = _conn()
    admin_service.bootstrap_if_empty(conn, ["U_A"])
    assert admin_service.add_admin(conn, "U_B", added_by="U_A") is True
    assert admin_service.add_admin(conn, "U_B", added_by="U_A") is False  # idempotent
    ids = admin_service.get_admin_ids(conn)
    assert "U_A" in ids and "U_B" in ids


def test_remove_admin() -> None:
    conn = _conn()
    admin_service.bootstrap_if_empty(conn, ["U_A", "U_B"])
    ok, reason = admin_service.remove_admin(conn, "U_B")
    assert ok and reason == ""
    assert admin_service.get_admin_ids(conn) == ("U_A",)


def test_remove_last_admin_blocked() -> None:
    conn = _conn()
    admin_service.bootstrap_if_empty(conn, ["U_A"])
    ok, reason = admin_service.remove_admin(conn, "U_A")
    assert not ok
    assert "마지막" in reason
    assert admin_service.get_admin_ids(conn) == ("U_A",)


def test_remove_nonexistent() -> None:
    conn = _conn()
    admin_service.bootstrap_if_empty(conn, ["U_A", "U_B"])
    ok, reason = admin_service.remove_admin(conn, "U_X")
    assert not ok
    assert "없음" in reason


def test_is_admin() -> None:
    conn = _conn()
    admin_service.bootstrap_if_empty(conn, ["U_A"])
    assert admin_service.is_admin("U_A", conn)
    assert not admin_service.is_admin("U_NOT", conn)
