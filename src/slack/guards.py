"""슬래시 커맨드 가드. 채널/멤버십 검증.

운영자 / 활성 멤버는 DB가 source of truth — 매번 조회 (저빈도 호출이라 성능 무관).
"""
from __future__ import annotations

import logging

from slack_bolt import Respond

from ..config import CHANNEL_ID, DB_PATH
from ..db import session
from ..services import admin_service

log = logging.getLogger(__name__)


def in_seminar_channel(body: dict) -> bool:
    return body.get("channel_id") == CHANNEL_ID


def is_admin(user_id: str) -> bool:
    return admin_service.is_admin(user_id)


def is_member_or_admin(user_id: str) -> bool:
    if admin_service.is_admin(user_id):
        return True
    with session(DB_PATH) as conn:
        row = conn.execute(
            "SELECT 1 FROM members WHERE slack_user_id = ? AND is_active = 1",
            (user_id,),
        ).fetchone()
    return row is not None


def reject_wrong_channel(respond: Respond) -> None:
    respond(
        text=f":no_entry_sign: 이 명령은 <#{CHANNEL_ID}>에서만 사용할 수 있습니다.",
        response_type="ephemeral",
    )


def reject_non_member(respond: Respond) -> None:
    respond(
        text=":no_entry_sign: 발표 멤버 또는 운영자만 사용할 수 있습니다.",
        response_type="ephemeral",
    )


def reject_non_admin(respond: Respond) -> None:
    respond(
        text=":no_entry_sign: 운영자만 사용할 수 있습니다.",
        response_type="ephemeral",
    )
