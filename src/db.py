"""SQLite 연결 + 스키마 초기화."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS schedule (
      date           TEXT PRIMARY KEY,
      reminder_date  TEXT NOT NULL,
      slot_1         TEXT,
      slot_2         TEXT,
      status         TEXT NOT NULL DEFAULT '예정',
      cycle_id       INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS members (
      name             TEXT PRIMARY KEY,
      slack_user_id    TEXT UNIQUE NOT NULL,
      preferences      TEXT,
      presented_count  INTEGER NOT NULL DEFAULT 0,
      defer_count      INTEGER NOT NULL DEFAULT 0,
      last_presented   TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS defer_requests (
      id                  INTEGER PRIMARY KEY AUTOINCREMENT,
      requester           TEXT NOT NULL,
      original_date       TEXT NOT NULL,
      reason              TEXT,
      hints               TEXT,             -- LLM이 추출한 부가 선호 (JSON)
      requested_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
      status              TEXT NOT NULL DEFAULT 'pending',
        -- pending / awaiting_approvals / replacement_rejected / approved / rejected / escalated / canceled
      replacement         TEXT,             -- 현재 제안된 대체자 (확정 전엔 후보, 후엔 최종)
      attempts            INTEGER NOT NULL DEFAULT 0,   -- 대체자 시도 횟수 (거절 시 +1)
      tried_replacements  TEXT NOT NULL DEFAULT '[]',   -- 거절한 후보 누적 (JSON list)
      requester_approved  DATETIME,         -- 신청자 본인 confirm 시각
      jjr_approved        DATETIME,         -- 진재님 승인 시각
      replacement_approved DATETIME,        -- 현재 제안된 대체자 승인 시각
      resolved_at         DATETIME
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS conversation_drafts (
      id              INTEGER PRIMARY KEY AUTOINCREMENT,
      kind            TEXT NOT NULL,        -- 'defer' / 'preference'
      slack_user_id   TEXT NOT NULL,
      dm_channel_id   TEXT NOT NULL,
      status          TEXT NOT NULL DEFAULT 'active',  -- active / awaiting_confirm / submitted / canceled
      messages        TEXT NOT NULL DEFAULT '[]',   -- LLM 대화 history (JSON list of {role, content})
      pending_payload TEXT,                  -- LLM이 tool로 제출한 payload (JSON), 사용자 confirm 대기
      created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
      updated_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_drafts_user_active "
    "ON conversation_drafts(slack_user_id, kind) WHERE status IN ('active', 'awaiting_confirm')",
    """
    CREATE TABLE IF NOT EXISTS notification_log (
      id              INTEGER PRIMARY KEY AUTOINCREMENT,
      type            TEXT NOT NULL,
      target_date     TEXT NOT NULL,
      sent_at         DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
      UNIQUE(type, target_date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS admins (
      slack_user_id   TEXT PRIMARY KEY,
      added_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
      added_by        TEXT,
      is_primary      INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_schedule_cycle ON schedule(cycle_id)",
    "CREATE INDEX IF NOT EXISTS idx_defer_status ON defer_requests(status)",
)


# 점진적 마이그레이션 — (table, column, full DDL after ADD COLUMN)
MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("members", "is_active", "INTEGER NOT NULL DEFAULT 1"),
)


def connect(db_path: Path | str) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    with conn:
        for stmt in SCHEMA:
            conn.execute(stmt)
        for table, column, ddl in MIGRATIONS:
            cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


@contextmanager
def session(db_path: Path | str) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        yield conn
    finally:
        conn.close()
