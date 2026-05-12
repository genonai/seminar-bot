"""에이전트가 자유롭게 적어두는 메모.

설계:
  - 카테고리(자유 텍스트) + 본문 + 선택적 seminar_date
  - 동일 (seminar_date, category, content) 중복은 add() 가 자동 회피
  - 운영자가 list 로 조회 / delete 로 정리

예시 사용:
  - 'offline_attendee' 카테고리: 다음 세션 오프라인 참가 신청자 명단
  - 'todo' 카테고리: 운영자가 해야 할 잡일 (자료 인쇄, 회의실 예약 등)
  - 'note' 카테고리: 기타 메모
"""
from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any

log = logging.getLogger(__name__)


def add(
    conn: sqlite3.Connection,
    *,
    seminar_date: str | None,
    category: str,
    content: str,
    created_by: str | None = None,
    metadata: dict | None = None,
) -> int:
    """동일 (seminar_date, category, content) 가 이미 있으면 기존 id 반환, 아니면 신규 insert."""
    category = (category or "").strip()
    content = (content or "").strip()
    if not category or not content:
        raise ValueError("category, content 필수")

    existing = conn.execute(
        """
        SELECT id FROM memo_pad
        WHERE category = ?
          AND content = ?
          AND COALESCE(seminar_date, '') = COALESCE(?, '')
        """,
        (category, content, seminar_date),
    ).fetchone()
    if existing:
        return int(existing["id"])

    with conn:
        cur = conn.execute(
            """
            INSERT INTO memo_pad (seminar_date, category, content, metadata, created_by)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                seminar_date,
                category,
                content,
                json.dumps(metadata, ensure_ascii=False) if metadata else None,
                created_by,
            ),
        )
        return int(cur.lastrowid)


def list_memos(
    conn: sqlite3.Connection,
    *,
    seminar_date: str | None = None,
    category: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    where: list[str] = []
    params: list[Any] = []
    if seminar_date is not None:
        where.append("seminar_date = ?")
        params.append(seminar_date)
    if category is not None:
        where.append("category = ?")
        params.append(category)
    sql = "SELECT * FROM memo_pad"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def delete(conn: sqlite3.Connection, memo_id: int) -> bool:
    with conn:
        cur = conn.execute("DELETE FROM memo_pad WHERE id = ?", (memo_id,))
    return cur.rowcount > 0


def list_categories(conn: sqlite3.Connection, seminar_date: str | None = None) -> list[str]:
    if seminar_date:
        rows = conn.execute(
            "SELECT DISTINCT category FROM memo_pad WHERE seminar_date = ? ORDER BY category",
            (seminar_date,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT DISTINCT category FROM memo_pad ORDER BY category"
        ).fetchall()
    return [r["category"] for r in rows]
