"""Slack 이벤트 핸들러 — 채널 멤버십 변동에 즉시 반응.

DM 메시지 처리는 dm.py, 슬래시는 commands.py, 버튼은 actions.py.
이 모듈은 join/leave 같은 멤버십 이벤트만.
"""
from __future__ import annotations

import logging

from slack_bolt import App
from slack_sdk import WebClient

from ..config import CHANNEL_ID, DB_PATH
from ..db import session
from ..services import member_service

log = logging.getLogger(__name__)


def register(app: App) -> None:
    @app.event("member_joined_channel")
    def on_member_joined(event: dict, client: WebClient) -> None:
        if event.get("channel") != CHANNEL_ID:
            return
        log.info("member_joined_channel user=%s — sync 트리거", event.get("user"))
        with session(DB_PATH) as conn:
            active, errors = member_service.sync_from_channel(client, conn)
        log.info("auto-sync 완료 (joined): active=%d errors=%s", len(active), errors)

    @app.event("member_left_channel")
    def on_member_left(event: dict, client: WebClient) -> None:
        if event.get("channel") != CHANNEL_ID:
            return
        log.info("member_left_channel user=%s — sync 트리거", event.get("user"))
        with session(DB_PATH) as conn:
            active, errors = member_service.sync_from_channel(client, conn)
        log.info("auto-sync 완료 (left): active=%d errors=%s", len(active), errors)
