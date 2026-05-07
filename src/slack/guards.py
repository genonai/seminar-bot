"""슬래시 커맨드 가드.

Slack 워크스페이스 전체에서 autocomplete에 노출되는 건 막을 수 없다 (플랫폼 설계).
대신 핸들러 진입 시점에 채널/사용자를 검증해 권한 없으면 ephemeral로 거절한다.
"""
from __future__ import annotations

from slack_bolt import Respond

from ..config import ADMIN_USER_IDS, CHANNEL_ID, MEMBER_ROSTER


_MEMBER_SLACK_IDS: frozenset[str] = frozenset(MEMBER_ROSTER.values())
_ALLOWED_USERS: frozenset[str] = _MEMBER_SLACK_IDS | frozenset(ADMIN_USER_IDS)


def in_seminar_channel(body: dict) -> bool:
    """슬래시 커맨드가 #ai-engineer-주간세미나 채널에서 호출됐는지."""
    return body.get("channel_id") == CHANNEL_ID


def is_member_or_admin(user_id: str) -> bool:
    return user_id in _ALLOWED_USERS


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
