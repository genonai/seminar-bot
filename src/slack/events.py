"""Slack 이벤트 핸들러 — 채널 멤버십 변동에 즉시 반응.

DM 메시지 처리는 dm.py, 슬래시는 commands.py, 버튼은 actions.py.
이 모듈은 join/leave 같은 멤버십 이벤트만.
"""
from __future__ import annotations

import logging
import threading

from slack_bolt import App
from slack_sdk import WebClient

from ..config import CHANNEL_ID, DB_PATH
from ..db import session
from ..intro_message import build_channel_intro
from ..services import member_service

log = logging.getLogger(__name__)

_bot_user_id: str | None = None
_bot_id_lock = threading.Lock()


def _get_bot_user_id(client: WebClient) -> str | None:
    global _bot_user_id
    if _bot_user_id is not None:
        return _bot_user_id
    with _bot_id_lock:
        if _bot_user_id is not None:
            return _bot_user_id
        try:
            _bot_user_id = client.auth_test()["user_id"]
            log.info("bot user_id resolved: %s", _bot_user_id)
        except Exception as e:
            log.warning("auth_test 실패: %s", e)
            return None
    return _bot_user_id


def register(app: App) -> None:
    @app.event("member_joined_channel")
    def on_member_joined(event: dict, client: WebClient) -> None:
        user_id = event.get("user")
        channel_id = event.get("channel")
        if not user_id or not channel_id:
            return

        # 1) 봇 자신이 새 채널에 초대됐다면 자기소개 자동 게시
        bot_id = _get_bot_user_id(client)
        if bot_id and user_id == bot_id:
            log.info("bot self-joined channel %s — 자기소개 게시", channel_id)
            try:
                client.chat_postMessage(channel=channel_id, text=build_channel_intro())
            except Exception as e:
                log.warning("intro post → %s 실패: %s", channel_id, e)
            # 자기소개 후에도 멤버 sync 는 별개로 처리 (아래로 흐름)

        # 2) 세미나 채널 멤버십 변동이면 sync
        if channel_id == CHANNEL_ID:
            log.info("member_joined_channel user=%s — sync 트리거", user_id)
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
