"""Slash command 핸들러."""
from __future__ import annotations

import logging
from datetime import date

from slack_bolt import Ack, App, Respond
from slack_sdk import WebClient

from ..config import DB_PATH
from ..db import session
from ..services import member_service, schedule_service
from . import flows, guards, messages

log = logging.getLogger(__name__)


def register(app: App) -> None:
    @app.command("/세미나-일정")
    def handle_schedule(ack: Ack, body: dict, respond: Respond) -> None:
        ack()
        log.info("/세미나-일정 user=%s channel=%s", body.get("user_id"), body.get("channel_id"))
        if not guards.in_seminar_channel(body):
            guards.reject_wrong_channel(respond)
            return
        if not guards.is_member_or_admin(body["user_id"]):
            guards.reject_non_member(respond)
            return

        viewer = body["user_id"]
        with session(DB_PATH) as conn:
            upcoming = schedule_service.get_upcoming(conn, limit=5)
            name_to_slack = member_service.name_to_slack_id_map(conn)
        text = messages.upcoming_schedule(upcoming, viewer, name_to_slack)
        respond(text=text, response_type="ephemeral")

    @app.command("/세미나-연기")
    def handle_defer(ack: Ack, body: dict, respond: Respond, client: WebClient) -> None:
        ack()
        log.info("/세미나-연기 user=%s channel=%s text=%r",
                 body.get("user_id"), body.get("channel_id"), body.get("text"))
        if not guards.in_seminar_channel(body):
            guards.reject_wrong_channel(respond)
            return
        if not guards.is_member_or_admin(body["user_id"]):
            guards.reject_non_member(respond)
            return
        result = flows.start_defer(
            client,
            slack_user_id=body["user_id"],
            initial_text=body.get("text", "") or "",
            today=date.today(),
        )
        respond(text=result, response_type="ephemeral")

    @app.command("/세미나-선호도")
    def handle_preference(ack: Ack, body: dict, respond: Respond, client: WebClient) -> None:
        ack()
        log.info("/세미나-선호도 user=%s channel=%s", body.get("user_id"), body.get("channel_id"))
        if not guards.in_seminar_channel(body):
            guards.reject_wrong_channel(respond)
            return
        if not guards.is_member_or_admin(body["user_id"]):
            guards.reject_non_member(respond)
            return
        result = flows.start_preference(
            client,
            slack_user_id=body["user_id"],
            initial_text=body.get("text", "") or "",
        )
        respond(text=result, response_type="ephemeral")
