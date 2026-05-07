"""DM 멀티턴 대화 상태 관리. defer / preference 두 흐름 공통."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any


@dataclass
class Draft:
    id: int
    kind: str                  # 'defer' / 'preference'
    slack_user_id: str
    dm_channel_id: str
    status: str                # active / awaiting_confirm / submitted / canceled
    messages: list[dict[str, Any]]
    pending_payload: dict[str, Any] | None


def _row_to_draft(row: sqlite3.Row) -> Draft:
    return Draft(
        id=row["id"],
        kind=row["kind"],
        slack_user_id=row["slack_user_id"],
        dm_channel_id=row["dm_channel_id"],
        status=row["status"],
        messages=json.loads(row["messages"]),
        pending_payload=json.loads(row["pending_payload"]) if row["pending_payload"] else None,
    )


def get_active(conn: sqlite3.Connection, slack_user_id: str, kind: str) -> Draft | None:
    row = conn.execute(
        """
        SELECT * FROM conversation_drafts
        WHERE slack_user_id = ? AND kind = ? AND status IN ('active', 'awaiting_confirm')
        ORDER BY id DESC LIMIT 1
        """,
        (slack_user_id, kind),
    ).fetchone()
    return _row_to_draft(row) if row else None


def get_active_any_kind(conn: sqlite3.Connection, slack_user_id: str) -> Draft | None:
    row = conn.execute(
        """
        SELECT * FROM conversation_drafts
        WHERE slack_user_id = ? AND status IN ('active', 'awaiting_confirm')
        ORDER BY id DESC LIMIT 1
        """,
        (slack_user_id,),
    ).fetchone()
    return _row_to_draft(row) if row else None


def get_by_id(conn: sqlite3.Connection, draft_id: int) -> Draft | None:
    row = conn.execute(
        "SELECT * FROM conversation_drafts WHERE id = ?", (draft_id,)
    ).fetchone()
    return _row_to_draft(row) if row else None


def create(conn: sqlite3.Connection, *, kind: str, slack_user_id: str, dm_channel_id: str) -> Draft:
    with conn:
        cur = conn.execute(
            """
            INSERT INTO conversation_drafts (kind, slack_user_id, dm_channel_id, messages)
            VALUES (?, ?, ?, '[]')
            """,
            (kind, slack_user_id, dm_channel_id),
        )
        new_id = cur.lastrowid
    return get_by_id(conn, new_id)  # type: ignore[return-value]


def update_messages(conn: sqlite3.Connection, draft_id: int, messages: list[dict[str, Any]]) -> None:
    with conn:
        conn.execute(
            "UPDATE conversation_drafts SET messages = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (json.dumps(messages, ensure_ascii=False), draft_id),
        )


def set_pending(conn: sqlite3.Connection, draft_id: int, payload: dict[str, Any]) -> None:
    """LLM이 tool 호출했을 때 사용자 confirm 대기 상태로 전환."""
    with conn:
        conn.execute(
            """
            UPDATE conversation_drafts
            SET status = 'awaiting_confirm',
                pending_payload = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (json.dumps(payload, ensure_ascii=False), draft_id),
        )


def mark_submitted(conn: sqlite3.Connection, draft_id: int) -> None:
    with conn:
        conn.execute(
            "UPDATE conversation_drafts SET status = 'submitted', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (draft_id,),
        )


def cancel(conn: sqlite3.Connection, draft_id: int) -> None:
    with conn:
        conn.execute(
            "UPDATE conversation_drafts SET status = 'canceled', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (draft_id,),
        )


def reset_to_active(conn: sqlite3.Connection, draft_id: int) -> None:
    """awaiting_confirm 상태에서 사용자가 '수정' 누르면 다시 대화 모드로."""
    with conn:
        conn.execute(
            """
            UPDATE conversation_drafts
            SET status = 'active', pending_payload = NULL, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (draft_id,),
        )
