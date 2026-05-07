"""DM 메시지 이벤트 라우팅."""
from __future__ import annotations

import logging
from datetime import date

from slack_bolt import App
from slack_sdk import WebClient

from . import flows

log = logging.getLogger(__name__)


def register(app: App) -> None:
    @app.event("message")
    def handle_message_events(body: dict, client: WebClient, logger: logging.Logger) -> None:
        event = body.get("event", {})
        # DM 메시지만 처리
        if event.get("channel_type") != "im":
            return
        # bot 자신이 보낸 메시지 무시
        if event.get("bot_id") or event.get("subtype") in {"bot_message", "message_changed", "message_deleted"}:
            return
        user = event.get("user")
        text = event.get("text", "")
        channel = event.get("channel")
        if not user or not channel or not text:
            return
        log.info("DM from user=%s text=%r", user, text[:80])
        flows.handle_dm_message(
            client, slack_user_id=user, channel=channel, text=text, today=date.today(),
        )
