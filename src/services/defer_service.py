"""연기 신청 도메인.

상태 머신:
  pending                — DB에 막 들어왔지만 후보 산정 전
  awaiting_approvals     — 진재 + 대체자 둘에게 승인 요청 보낸 상태
  replacement_rejected   — 대체자가 거절. 차순위로 재시도 진행 중
  approved               — 양쪽 다 승인 → 스케줄 갱신 + 채널 공지 끝
  escalated              — 후보 다 거절. 진재님 수동 처리 필요
  canceled               — 신청자 또는 진재 취소
  rejected               — 진재 거절
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from ..config import DEFER_DEADLINE_DAYS, MAX_REPLACEMENT_ATTEMPTS
from ..cost import pick_replacement
from ..models import Member
from . import member_service, schedule_service


# ─────────────────────────────────────────────────────────────
# Domain types
# ─────────────────────────────────────────────────────────────
@dataclass
class DeferRow:
    id: int
    requester: str
    original_date: date
    reason: str
    hints: dict[str, Any]
    status: str
    replacement: str | None
    attempts: int
    tried_replacements: list[str]
    requester_approved: str | None
    jjr_approved: str | None
    replacement_approved: str | None


def _row_to_defer(row: sqlite3.Row) -> DeferRow:
    return DeferRow(
        id=row["id"],
        requester=row["requester"],
        original_date=date.fromisoformat(row["original_date"]),
        reason=row["reason"] or "",
        hints=json.loads(row["hints"]) if row["hints"] else {},
        status=row["status"],
        replacement=row["replacement"],
        attempts=row["attempts"],
        tried_replacements=json.loads(row["tried_replacements"]),
        requester_approved=row["requester_approved"],
        jjr_approved=row["jjr_approved"],
        replacement_approved=row["replacement_approved"],
    )


def get(conn: sqlite3.Connection, defer_id: int) -> DeferRow:
    row = conn.execute("SELECT * FROM defer_requests WHERE id = ?", (defer_id,)).fetchone()
    if row is None:
        raise ValueError(f"defer_requests {defer_id} 없음")
    return _row_to_defer(row)


# ─────────────────────────────────────────────────────────────
# Validation
# ─────────────────────────────────────────────────────────────
def deadline_for(seminar_date: date) -> date:
    return seminar_date - timedelta(days=DEFER_DEADLINE_DAYS)


def can_request(today: date, seminar_date: date) -> tuple[bool, str]:
    """오늘이 마감 이내인지. (True, "") 또는 (False, 이유)."""
    deadline = deadline_for(seminar_date)
    if today > deadline:
        return False, f"마감({deadline.isoformat()}) 지남"
    return True, ""


def find_requester_assignment(
    conn: sqlite3.Connection, slack_user_id: str, today: date
) -> tuple[date, str] | None:
    """slack 사용자가 다가오는 일정에 배정된 첫 슬롯의 (날짜, 이름) 반환. 없으면 None."""
    member = member_service.get_by_slack_id(conn, slack_user_id)
    if member is None:
        return None
    # include_today=True — 당일 발표자도 자기 일정으로 찾을 수 있게 (자료 제출/연기 용도)
    upcoming = schedule_service.get_upcoming(conn, today=today, limit=10, include_today=True)
    for s in upcoming:
        if member.name in s.presenters():
            return s.date, member.name
    return None


# ─────────────────────────────────────────────────────────────
# Lifecycle
# ─────────────────────────────────────────────────────────────
def create(
    conn: sqlite3.Connection, *, requester: str, original_date: date, reason: str, hints: dict[str, Any]
) -> int:
    """신청자 confirm 직후 호출. status='pending', 후보 산정 전."""
    with conn:
        cur = conn.execute(
            """
            INSERT INTO defer_requests (requester, original_date, reason, hints, status, requester_approved)
            VALUES (?, ?, ?, ?, 'pending', CURRENT_TIMESTAMP)
            """,
            (requester, original_date.isoformat(), reason, json.dumps(hints, ensure_ascii=False)),
        )
        return cur.lastrowid  # type: ignore[return-value]


def select_next_replacement(
    conn: sqlite3.Connection, defer_id: int
) -> Member | None:
    """대체자 선정.

    제외:
      - 신청자
      - 이미 거절한 후보들 (tried_replacements)
      - 같은 날 다른 슬롯에 이미 들어있는 사람 (중복 방지)

    우선순위:
      Tier 1) 현 cycle에 미배정된 free agent (가장 자연스러운 대체)
      Tier 2) 현 cycle 안인데 다른 날짜에 배정된 사람 (이중 배정 발생, 운영자 후속 swap 필요)
    """
    d = get(conn, defer_id)
    excluded = set(d.tried_replacements) | {d.requester}
    # 같은 날 다른 슬롯에 이미 있는 사람도 제외 — 한 사람이 양 슬롯 동시에 채우는 것 방지.
    same_day = schedule_service.get_by_date(conn, d.original_date)
    if same_day is not None:
        for name in same_day.presenters():
            if name != d.requester:
                excluded.add(name)

    # 현 cycle 안에 있는 모든 멤버 이름
    in_cycle = _members_in_cycle(conn, d.original_date)

    all_members = member_service.get_all_active(conn)

    # Tier 1: cycle 밖 (free agent)
    free_agents = [m for m in all_members if m.name not in excluded and m.name not in in_cycle]
    if free_agents:
        return pick_replacement(free_agents, d.original_date, current_cycle_remaining=set())

    # Tier 2: cycle 안 다른 날짜 (이중 배정 발생, 사용자에게 알림 필요)
    cycle_others = [m for m in all_members if m.name not in excluded and m.name in in_cycle]
    if cycle_others:
        return pick_replacement(cycle_others, d.original_date, current_cycle_remaining=set())

    return None


def _members_in_cycle(conn: sqlite3.Connection, target_date: date) -> set[str]:
    """target_date가 속한 cycle의 모든 슬롯(slot_1, slot_2)에 들어있는 사람 이름 set."""
    row = conn.execute(
        "SELECT cycle_id FROM schedule WHERE date = ?", (target_date.isoformat(),)
    ).fetchone()
    if row is None:
        return set()
    cycle_id = row["cycle_id"]
    rows = conn.execute(
        "SELECT slot_1, slot_2 FROM schedule WHERE cycle_id = ? AND status != '취소'",
        (cycle_id,),
    ).fetchall()
    names: set[str] = set()
    for r in rows:
        if r["slot_1"]:
            names.add(r["slot_1"])
        if r["slot_2"]:
            names.add(r["slot_2"])
    return names


def assign_replacement(conn: sqlite3.Connection, defer_id: int, replacement_name: str) -> None:
    """후보 픽 후 DM 보낼 때 호출. status → awaiting_approvals, attempts++."""
    with conn:
        conn.execute(
            """
            UPDATE defer_requests
            SET replacement = ?,
                attempts = attempts + 1,
                status = 'awaiting_approvals',
                replacement_approved = NULL
            WHERE id = ?
            """,
            (replacement_name, defer_id),
        )


def record_jjr_approval(conn: sqlite3.Connection, defer_id: int) -> None:
    with conn:
        conn.execute(
            "UPDATE defer_requests SET jjr_approved = CURRENT_TIMESTAMP WHERE id = ?",
            (defer_id,),
        )


def record_replacement_approval(conn: sqlite3.Connection, defer_id: int) -> None:
    with conn:
        conn.execute(
            "UPDATE defer_requests SET replacement_approved = CURRENT_TIMESTAMP WHERE id = ?",
            (defer_id,),
        )


def record_replacement_rejection(conn: sqlite3.Connection, defer_id: int) -> None:
    """대체자가 거절. tried_replacements에 누적, status → replacement_rejected."""
    d = get(conn, defer_id)
    if d.replacement is None:
        raise ValueError(f"defer {defer_id}: 거절 처리 시점인데 replacement가 None")
    new_tried = list(d.tried_replacements) + [d.replacement]
    with conn:
        conn.execute(
            """
            UPDATE defer_requests
            SET status = 'replacement_rejected',
                tried_replacements = ?,
                replacement = NULL,
                replacement_approved = NULL
            WHERE id = ?
            """,
            (json.dumps(new_tried, ensure_ascii=False), defer_id),
        )


def is_fully_approved(d: DeferRow) -> bool:
    return d.jjr_approved is not None and d.replacement_approved is not None


def is_escalation_needed(d: DeferRow) -> bool:
    return d.attempts >= MAX_REPLACEMENT_ATTEMPTS


def mark_escalated(conn: sqlite3.Connection, defer_id: int) -> None:
    with conn:
        conn.execute(
            "UPDATE defer_requests SET status = 'escalated' WHERE id = ?",
            (defer_id,),
        )


def mark_canceled(conn: sqlite3.Connection, defer_id: int) -> None:
    with conn:
        conn.execute(
            """
            UPDATE defer_requests
            SET status = 'canceled', resolved_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (defer_id,),
        )


def mark_rejected_by_jjr(conn: sqlite3.Connection, defer_id: int) -> None:
    with conn:
        conn.execute(
            """
            UPDATE defer_requests
            SET status = 'rejected', resolved_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (defer_id,),
        )


def finalize_approval(conn: sqlite3.Connection, defer_id: int) -> DeferRow:
    """양쪽 승인 완료 → schedule 갱신 + defer_count 증가 + status='approved'."""
    d = get(conn, defer_id)
    if not is_fully_approved(d):
        raise ValueError(f"defer {defer_id}: 양쪽 승인 안 됨")
    if d.replacement is None:
        raise ValueError(f"defer {defer_id}: replacement None")

    schedule_service.replace_presenter(
        conn, d.original_date, old_name=d.requester, new_name=d.replacement
    )
    with conn:
        conn.execute(
            "UPDATE members SET defer_count = defer_count + 1 WHERE name = ?",
            (d.requester,),
        )
        conn.execute(
            """
            UPDATE defer_requests
            SET status = 'approved', resolved_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (defer_id,),
        )
    return get(conn, defer_id)
