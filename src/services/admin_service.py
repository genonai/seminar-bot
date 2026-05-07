"""운영자 관리. DB 가 source of truth.

config.ADMIN_USER_IDS (env) 는 첫 시작 시 부트스트랩 용도로만 사용.
이후 추가/삭제는 슬래시(`/어드민-*`) 또는 직접 DB로.
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Iterable

from ..config import DB_PATH
from ..db import session

log = logging.getLogger(__name__)


def list_admins(conn: sqlite3.Connection | None = None) -> list[dict]:
    def _q(c: sqlite3.Connection) -> list[dict]:
        return [dict(r) for r in c.execute(
            "SELECT slack_user_id, added_at, added_by, is_primary "
            "FROM admins ORDER BY is_primary DESC, added_at ASC"
        )]
    if conn is None:
        with session(DB_PATH) as c:
            return _q(c)
    return _q(conn)


def get_admin_ids(conn: sqlite3.Connection | None = None) -> tuple[str, ...]:
    return tuple(a["slack_user_id"] for a in list_admins(conn))


def is_admin(user_id: str, conn: sqlite3.Connection | None = None) -> bool:
    return user_id in get_admin_ids(conn)


def get_primary_admin_id(conn: sqlite3.Connection | None = None) -> str | None:
    ids = get_admin_ids(conn)
    return ids[0] if ids else None


def add_admin(conn: sqlite3.Connection, slack_user_id: str, added_by: str) -> bool:
    """True if newly added, False if already admin."""
    try:
        with conn:
            conn.execute(
                "INSERT INTO admins (slack_user_id, added_by) VALUES (?, ?)",
                (slack_user_id, added_by),
            )
        log.info("admin added: %s (by %s)", slack_user_id, added_by)
        return True
    except sqlite3.IntegrityError:
        return False


def remove_admin(conn: sqlite3.Connection, slack_user_id: str) -> tuple[bool, str]:
    """(success, reason). 마지막 1명은 안전상 제거 거부."""
    rows = list_admins(conn)
    target_in_list = any(r["slack_user_id"] == slack_user_id for r in rows)
    if not target_in_list:
        return False, "운영자 목록에 없음"
    if len(rows) <= 1:
        return False, "마지막 운영자는 제거할 수 없습니다 (최소 1명 유지)"
    with conn:
        cur = conn.execute("DELETE FROM admins WHERE slack_user_id = ?", (slack_user_id,))
    log.info("admin removed: %s", slack_user_id)
    return cur.rowcount > 0, ""


def bootstrap_if_empty(conn: sqlite3.Connection, default_ids: Iterable[str]) -> int:
    """admins 테이블 비어있으면 default_ids로 채움. 첫 ID는 is_primary=1."""
    n = conn.execute("SELECT COUNT(*) AS c FROM admins").fetchone()["c"]
    if n > 0:
        return 0
    ids_list = list(default_ids)
    with conn:
        for i, uid in enumerate(ids_list):
            conn.execute(
                "INSERT INTO admins (slack_user_id, added_by, is_primary) VALUES (?, ?, ?)",
                (uid, "bootstrap", 1 if i == 0 else 0),
            )
    log.info("admins bootstrapped: %s", ids_list)
    return len(ids_list)
