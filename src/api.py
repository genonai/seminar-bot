"""Debug HTTP API — 봇 상태/로그를 외부에서 조회 (Bearer token 인증).

엔드포인트:
  GET /health
  GET /logs?keyword=&limit=         로그 파일 검색 (file logging 필요)
  GET /state/schedule?limit=         다가올 일정
  GET /state/submissions             전체 submissions
  GET /state/members                 멤버 풀
  GET /state/conversation/{user_id}  특정 사용자 대화 history
  GET /state/memos?seminar_date=&category=
  GET /state/admins

별도 thread 의 uvicorn 서버로 Bolt 와 동시 실행.
"""
from __future__ import annotations

import logging
import threading
from datetime import date
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query

from .config import API_BIND, API_PORT, API_TOKEN, DB_PATH
from .db import session
from .services import (
    admin_service,
    conversation_service,
    member_service,
    memo_service,
    schedule_service,
    submission_service,
)

log = logging.getLogger(__name__)
LOG_FILE: Path = Path(DB_PATH).parent / "bot.log"

app = FastAPI(title="seminar-bot debug API", version="1.0")


def _auth(authorization: str | None = Header(default=None)) -> None:
    if not API_TOKEN:
        raise HTTPException(503, "API_TOKEN not configured")
    if authorization != f"Bearer {API_TOKEN}":
        raise HTTPException(401, "invalid bearer token")


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "log_file_exists": LOG_FILE.exists()}


@app.get("/logs", dependencies=[Depends(_auth)])
def logs(
    keyword: str | None = Query(None, description="case-insensitive substring"),
    limit: int = Query(80, ge=1, le=1000),
) -> dict:
    if not LOG_FILE.exists():
        raise HTTPException(404, f"log file not found: {LOG_FILE}")
    try:
        all_lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as e:
        raise HTTPException(500, f"read failed: {e}")
    if keyword:
        kw = keyword.lower()
        matched = [ln for ln in all_lines if kw in ln.lower()]
    else:
        matched = all_lines
    return {
        "keyword": keyword,
        "total_lines": len(all_lines),
        "matched": len(matched),
        "returned": min(limit, len(matched)),
        "lines": matched[-limit:],
    }


@app.get("/state/schedule", dependencies=[Depends(_auth)])
def state_schedule(limit: int = 10) -> list[dict]:
    with session(DB_PATH) as conn:
        rows = schedule_service.get_upcoming(conn, today=date.today(), limit=limit)
    return [
        {
            "date": s.date.isoformat(),
            "reminder_date": s.reminder_date.isoformat(),
            "slot_1": s.slot_1,
            "slot_2": s.slot_2,
            "slot_1_topic": s.slot_1_topic,
            "slot_2_topic": s.slot_2_topic,
            "notes": s.notes,
            "status": s.status,
            "cycle_id": s.cycle_id,
        }
        for s in rows
    ]


@app.get("/state/submissions", dependencies=[Depends(_auth)])
def state_submissions() -> list[dict]:
    with session(DB_PATH) as conn:
        rows = list(conn.execute(
            "SELECT id, presenter, seminar_date, file_name, status, page_count, "
            "submitted_at, ingested_at, announce_ts FROM submissions ORDER BY id DESC"
        ))
    return [dict(r) for r in rows]


@app.get("/state/members", dependencies=[Depends(_auth)])
def state_members() -> list[dict]:
    with session(DB_PATH) as conn:
        members = member_service.get_all(conn)
    return [
        {
            "name": m.name,
            "slack_user_id": m.slack_user_id,
            "presented_count": m.presented_count,
            "defer_count": m.defer_count,
            "last_presented": m.last_presented.isoformat() if m.last_presented else None,
        }
        for m in members
    ]


@app.get("/state/conversation/{user_id}", dependencies=[Depends(_auth)])
def state_conversation(user_id: str, limit: int = 30) -> dict:
    with session(DB_PATH) as conn:
        history = conversation_service.get_history(conn, user_id, limit=limit)
    return {"user_id": user_id, "count": len(history), "history": history}


@app.get("/state/memos", dependencies=[Depends(_auth)])
def state_memos(
    seminar_date: str | None = None, category: str | None = None
) -> list[dict]:
    with session(DB_PATH) as conn:
        return memo_service.list_memos(
            conn, seminar_date=seminar_date, category=category, limit=100
        )


@app.get("/state/admins", dependencies=[Depends(_auth)])
def state_admins() -> list[dict]:
    return admin_service.list_admins()


def start_api_server() -> None:
    """uvicorn 을 별도 thread 에서 실행."""
    if not API_TOKEN:
        log.warning("API_TOKEN 미설정 → debug API 비활성화")
        return

    import uvicorn

    def _run() -> None:
        uvicorn.run(app, host=API_BIND, port=API_PORT, log_level="warning", access_log=False)

    t = threading.Thread(target=_run, daemon=True, name="api-server")
    t.start()
    log.info("debug API server thread started → %s:%d (auth required)", API_BIND, API_PORT)
