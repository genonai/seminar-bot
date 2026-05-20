"""멤버 조회/갱신 서비스."""
from __future__ import annotations

import logging
import sqlite3
from datetime import date

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from ..config import CHANNEL_ID
from ..models import Member, Preferences

log = logging.getLogger(__name__)


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
    """모든 멤버 (active + inactive). 로그/historical 용도."""
    rows = conn.execute("SELECT * FROM members ORDER BY name").fetchall()
    return [_row_to_member(r) for r in rows]


def get_all_active(conn: sqlite3.Connection) -> list[Member]:
    """추첨/대체자 선정 풀. 채널 멤버(is_active=1) + 운영자 제외 안 함(excluded=0)."""
    rows = conn.execute(
        "SELECT * FROM members WHERE is_active = 1 AND COALESCE(excluded, 0) = 0 ORDER BY name"
    ).fetchall()
    return [_row_to_member(r) for r in rows]


def list_excluded(conn: sqlite3.Connection) -> list[dict]:
    """운영자가 발표 풀에서 명시적으로 제외한 멤버들."""
    rows = conn.execute(
        "SELECT name, slack_user_id, is_active FROM members "
        "WHERE COALESCE(excluded, 0) = 1 ORDER BY name"
    ).fetchall()
    return [dict(r) for r in rows]


def set_excluded(conn: sqlite3.Connection, slack_user_id: str, excluded: bool) -> tuple[bool, str]:
    """발표 풀에서 제외/포함 toggle. (성공여부, 이름 또는 이유)."""
    row = conn.execute("SELECT name FROM members WHERE slack_user_id = ?", (slack_user_id,)).fetchone()
    if row is None:
        return False, "members 에 없음 — 채널 멤버 sync 후 다시 시도"
    with conn:
        conn.execute(
            "UPDATE members SET excluded = ? WHERE slack_user_id = ?",
            (1 if excluded else 0, slack_user_id),
        )
    return True, row["name"]


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


# ─────────────────────────────────────────────────────────────
# 채널 동기화 (source of truth: Slack 채널 membership)
# ─────────────────────────────────────────────────────────────
def sync_from_channel(
    client: WebClient,
    conn: sqlite3.Connection,
    *,
    channel_id: str = CHANNEL_ID,
    exclude_user_ids: tuple[str, ...] | None = None,
) -> tuple[list[Member], list[str]]:
    """채널 멤버 fetch → 운영자/봇 제외 → DB upsert + 떠난 멤버 is_active=0.

    반환: (active_members, errors)
    `channels:read` scope 없거나 채널 접근 불가면 빈 리스트 + 에러 메시지 반환 (no-op).
    exclude_user_ids 미지정 시 admin_service에서 현재 운영자 list 조회.
    """
    if exclude_user_ids is None:
        from . import admin_service
        exclude_user_ids = admin_service.get_admin_ids(conn)
    errors: list[str] = []
    member_ids: list[str] = []
    cursor: str | None = None
    try:
        while True:
            kwargs = {"channel": channel_id, "limit": 200}
            if cursor:
                kwargs["cursor"] = cursor
            resp = client.conversations_members(**kwargs)
            member_ids.extend(resp["members"])
            cursor = resp.get("response_metadata", {}).get("next_cursor") or None
            if not cursor:
                break
    except SlackApiError as e:
        err = e.response["error"]
        log.warning("sync_from_channel: conversations.members 실패 (%s) — 동기화 skip", err)
        errors.append(err)
        return [], errors

    excluded = set(exclude_user_ids)
    eligible: list[tuple[str, str]] = []   # (slack_id, real_name)
    for uid in member_ids:
        if uid in excluded:
            continue
        try:
            info = client.users_info(user=uid)
        except SlackApiError as e:
            log.warning("users.info(%s) 실패: %s", uid, e.response["error"])
            errors.append(f"users.info {uid}: {e.response['error']}")
            continue
        u = info["user"]
        if u.get("is_bot") or u.get("deleted"):
            continue
        profile = u.get("profile", {})
        name = (
            profile.get("display_name_normalized")
            or profile.get("real_name_normalized")
            or u.get("real_name")
            or u.get("name")
        )
        if not name:
            continue
        eligible.append((uid, name))

    eligible_ids = {uid for uid, _ in eligible}
    empty_prefs = Preferences().to_json()

    with conn:
        # 일단 모두 비활성화 — 채널에 있는 멤버만 다시 활성화
        conn.execute("UPDATE members SET is_active = 0")
        for uid, name in eligible:
            existing = conn.execute(
                "SELECT name FROM members WHERE slack_user_id = ?", (uid,)
            ).fetchone()
            if existing is None:
                # 신규 — 이름 충돌 가능성: 같은 이름이 다른 slack_id로 있으면 suffix 붙임
                final_name = _resolve_name_collision(conn, name, uid)
                conn.execute(
                    """
                    INSERT INTO members (name, slack_user_id, preferences, is_active)
                    VALUES (?, ?, ?, 1)
                    """,
                    (final_name, uid, empty_prefs),
                )
                log.info("sync: 신규 멤버 %s (%s)", final_name, uid)
            else:
                conn.execute(
                    "UPDATE members SET is_active = 1 WHERE slack_user_id = ?",
                    (uid,),
                )

    active = get_all_active(conn)
    log.info("sync_from_channel: %d active (channel=%s, excluded=%d)",
             len(active), channel_id, len(excluded))
    return active, errors


def _resolve_name_collision(conn: sqlite3.Connection, name: str, slack_user_id: str) -> str:
    """name이 이미 다른 slack_user_id 로 존재하면 (2), (3) 같은 suffix 부여."""
    row = conn.execute("SELECT slack_user_id FROM members WHERE name = ?", (name,)).fetchone()
    if row is None or row["slack_user_id"] == slack_user_id:
        return name
    n = 2
    while True:
        candidate = f"{name} ({n})"
        if conn.execute("SELECT 1 FROM members WHERE name = ?", (candidate,)).fetchone() is None:
            return candidate
        n += 1
