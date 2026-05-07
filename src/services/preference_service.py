"""멤버 선호도 영구 저장."""
from __future__ import annotations

import sqlite3

from ..models import Preferences


def get(conn: sqlite3.Connection, name: str) -> Preferences:
    row = conn.execute("SELECT preferences FROM members WHERE name = ?", (name,)).fetchone()
    if row is None:
        raise ValueError(f"멤버 없음: {name}")
    return Preferences.from_json(row["preferences"])


def save(conn: sqlite3.Connection, name: str, prefs: Preferences) -> None:
    with conn:
        conn.execute(
            "UPDATE members SET preferences = ? WHERE name = ?",
            (prefs.to_json(), name),
        )
