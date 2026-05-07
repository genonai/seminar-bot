"""9명 멤버 시딩. 멱등 (이미 있으면 slack_user_id만 갱신)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import DB_PATH, MEMBER_ROSTER
from src.db import connect, init_schema
from src.models import Preferences


def main() -> None:
    print(f"[seed_members] DB_PATH = {DB_PATH}")
    conn = connect(DB_PATH)
    try:
        init_schema(conn)
        empty_prefs = Preferences().to_json()
        with conn:
            for name, slack_id in MEMBER_ROSTER.items():
                conn.execute(
                    """
                    INSERT INTO members (name, slack_user_id, preferences)
                    VALUES (?, ?, ?)
                    ON CONFLICT(name) DO UPDATE SET
                      slack_user_id = excluded.slack_user_id
                    """,
                    (name, slack_id, empty_prefs),
                )
        rows = conn.execute(
            "SELECT name, slack_user_id, presented_count, defer_count FROM members ORDER BY name"
        ).fetchall()
    finally:
        conn.close()

    print(f"[seed_members] {len(rows)} members:")
    for r in rows:
        print(f"  {r['name']:6}  {r['slack_user_id']:14}  presented={r['presented_count']}  deferred={r['defer_count']}")


if __name__ == "__main__":
    main()
