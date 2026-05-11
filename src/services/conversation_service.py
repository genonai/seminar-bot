"""사용자별 DM 대화 메모리.

각 slack_user_id 마다 봇과의 DM 메시지 rolling history 유지.
- 사용자 메시지 / 봇 메시지 둘 다 기록
- intent classifier / LLM 호출 시 컨텍스트로 주입
- 마지막 N개 (MAX_HISTORY) 만 유지 → prompt 크기 일정
"""
from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any

log = logging.getLogger(__name__)

MAX_HISTORY = 30          # 보관 최대 메시지 개수 (user+bot 합산)
DEFAULT_LIMIT = 16        # 기본 조회 (LLM 컨텍스트 주입용)
MAX_CONTENT_LEN = 2000    # 한 메시지 잘림 한도


def get_history(
    conn: sqlite3.Connection, slack_user_id: str, *, limit: int = DEFAULT_LIMIT
) -> list[dict[str, Any]]:
    row = conn.execute(
        "SELECT messages FROM user_conversations WHERE slack_user_id = ?",
        (slack_user_id,),
    ).fetchone()
    if row is None or not row["messages"]:
        return []
    try:
        msgs = json.loads(row["messages"])
    except json.JSONDecodeError:
        return []
    return msgs[-limit:] if limit > 0 else msgs


def append(conn: sqlite3.Connection, slack_user_id: str, role: str, content: str) -> None:
    """role = 'user' | 'assistant'. content 빈 문자열이면 무시."""
    content = (content or "").strip()
    if not content:
        return
    content = content[:MAX_CONTENT_LEN]
    history = get_history(conn, slack_user_id, limit=MAX_HISTORY)
    history.append({"role": role, "content": content})
    history = history[-MAX_HISTORY:]
    payload = json.dumps(history, ensure_ascii=False)
    with conn:
        conn.execute(
            """
            INSERT INTO user_conversations (slack_user_id, messages, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(slack_user_id) DO UPDATE SET
              messages = excluded.messages,
              updated_at = CURRENT_TIMESTAMP
            """,
            (slack_user_id, payload),
        )


def clear(conn: sqlite3.Connection, slack_user_id: str) -> None:
    with conn:
        conn.execute(
            "DELETE FROM user_conversations WHERE slack_user_id = ?",
            (slack_user_id,),
        )
