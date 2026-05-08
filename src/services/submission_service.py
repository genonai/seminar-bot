"""발표 자료 제출 메타 + 처리 상태."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date


@dataclass
class Submission:
    id: int
    presenter: str
    seminar_date: date
    file_path: str
    file_name: str
    slack_file_id: str | None
    page_count: int | None
    title: str | None
    summary: str | None
    tags: list[str]
    entities: list[dict]           # [{name, type, description}, ...]
    relations: list[dict]          # [{subject, predicate, object}, ...]
    status: str                    # pending / processing / ingested / failed
    error_message: str | None
    submitted_at: str
    ingested_at: str | None
    announce_ts: str | None


def _row(r: sqlite3.Row) -> Submission:
    return Submission(
        id=r["id"],
        presenter=r["presenter"],
        seminar_date=date.fromisoformat(r["seminar_date"]),
        file_path=r["file_path"],
        file_name=r["file_name"],
        slack_file_id=r["slack_file_id"],
        page_count=r["page_count"],
        title=r["title"],
        summary=r["summary"],
        tags=json.loads(r["tags"]) if r["tags"] else [],
        entities=json.loads(r["entities"]) if r["entities"] else [],
        relations=json.loads(r["relations"]) if r["relations"] else [],
        status=r["status"],
        error_message=r["error_message"],
        submitted_at=r["submitted_at"],
        ingested_at=r["ingested_at"],
        announce_ts=r["announce_ts"],
    )


def create_pending(
    conn: sqlite3.Connection,
    *,
    presenter: str,
    seminar_date: date,
    file_path: str,
    file_name: str,
    slack_file_id: str | None,
) -> int:
    with conn:
        cur = conn.execute(
            """
            INSERT INTO submissions (presenter, seminar_date, file_path, file_name, slack_file_id, status)
            VALUES (?, ?, ?, ?, ?, 'pending')
            """,
            (presenter, seminar_date.isoformat(), file_path, file_name, slack_file_id),
        )
        return cur.lastrowid                                  # type: ignore[return-value]


def get(conn: sqlite3.Connection, sid: int) -> Submission | None:
    r = conn.execute("SELECT * FROM submissions WHERE id = ?", (sid,)).fetchone()
    return _row(r) if r else None


def get_for_seminar(conn: sqlite3.Connection, seminar_date: date) -> list[Submission]:
    rows = conn.execute(
        "SELECT * FROM submissions WHERE seminar_date = ? AND status = 'ingested' ORDER BY id DESC",
        (seminar_date.isoformat(),),
    ).fetchall()
    return [_row(r) for r in rows]


def list_ingested(conn: sqlite3.Connection, limit: int = 50) -> list[Submission]:
    rows = conn.execute(
        "SELECT * FROM submissions WHERE status = 'ingested' ORDER BY ingested_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [_row(r) for r in rows]


def mark_processing(conn: sqlite3.Connection, sid: int) -> None:
    with conn:
        conn.execute("UPDATE submissions SET status = 'processing' WHERE id = ?", (sid,))


def mark_ingested(
    conn: sqlite3.Connection,
    sid: int,
    *,
    page_count: int,
    title: str | None,
    summary: str | None,
    tags: list[str],
    entities: list[dict],
    relations: list[dict],
) -> None:
    with conn:
        conn.execute(
            """
            UPDATE submissions
            SET status = 'ingested',
                page_count = ?,
                title = ?,
                summary = ?,
                tags = ?,
                entities = ?,
                relations = ?,
                ingested_at = CURRENT_TIMESTAMP,
                error_message = NULL
            WHERE id = ?
            """,
            (
                page_count,
                title,
                summary,
                json.dumps(tags, ensure_ascii=False),
                json.dumps(entities, ensure_ascii=False),
                json.dumps(relations, ensure_ascii=False),
                sid,
            ),
        )


def mark_failed(conn: sqlite3.Connection, sid: int, error_message: str) -> None:
    with conn:
        conn.execute(
            "UPDATE submissions SET status = 'failed', error_message = ? WHERE id = ?",
            (error_message, sid),
        )


def set_announce_ts(conn: sqlite3.Connection, sid: int, ts: str) -> None:
    with conn:
        conn.execute("UPDATE submissions SET announce_ts = ? WHERE id = ?", (ts, sid))
