"""멤버 조회/갱신 서비스."""
from __future__ import annotations

import sqlite3
from datetime import date

from ..models import Member, Preferences


def _row_to_member(row: sqlite3.Row) -> Member:
    last = row["last_presented"]
    return Member(
        name=row["name"],
        slack_user_id=row["slack_user_id"],
        preferences=Preferences.from_json(row["preferences"]),
        presented_count=row["presented_count"],
        defer_count=row["defer_count"],
        last_presented=date.fromisoformat(last) if last else None,
    )


def get_all(conn: sqlite3.Connection) -> list[Member]:
    rows = conn.execute("SELECT * FROM members ORDER BY name").fetchall()
    return [_row_to_member(r) for r in rows]


def get_by_name(conn: sqlite3.Connection, name: str) -> Member | None:
    row = conn.execute("SELECT * FROM members WHERE name = ?", (name,)).fetchone()
    return _row_to_member(row) if row else None


def get_by_slack_id(conn: sqlite3.Connection, slack_user_id: str) -> Member | None:
    row = conn.execute(
        "SELECT * FROM members WHERE slack_user_id = ?", (slack_user_id,)
    ).fetchone()
    return _row_to_member(row) if row else None


def name_to_slack_id_map(conn: sqlite3.Connection) -> dict[str, str]:
    rows = conn.execute("SELECT name, slack_user_id FROM members").fetchall()
    return {r["name"]: r["slack_user_id"] for r in rows}
