"""Block Kit 버튼 핸들러."""
from __future__ import annotations

import logging
from datetime import date

from slack_bolt import Ack, App
from slack_sdk import WebClient

from ..config import ADMIN_JJR
from . import flows

log = logging.getLogger(__name__)


def register(app: App) -> None:
    # ── 신청자 confirm/revise/cancel — defer ─────────────────
    @app.action("defer_confirm")
    def on_defer_confirm(ack: Ack, body: dict, client: WebClient) -> None:
        ack()
        flows.confirm_defer(
            client,
            draft_id=int(body["actions"][0]["value"]),
            slack_user_id=body["user"]["id"],
            today=date.today(),
        )

    @app.action("defer_revise")
    def on_defer_revise(ack: Ack, body: dict, client: WebClient) -> None:
        ack()
        flows.revise_defer(
            client,
            draft_id=int(body["actions"][0]["value"]),
            slack_user_id=body["user"]["id"],
        )

    @app.action("defer_cancel")
    def on_defer_cancel(ack: Ack, body: dict, client: WebClient) -> None:
        ack()
        flows.cancel_defer(
            client,
            draft_id=int(body["actions"][0]["value"]),
            slack_user_id=body["user"]["id"],
        )

    # ── 신청자 confirm/revise/cancel — preference ────────────
    @app.action("pref_confirm")
    def on_pref_confirm(ack: Ack, body: dict, client: WebClient) -> None:
        ack()
        flows.confirm_preference(
            client,
            draft_id=int(body["actions"][0]["value"]),
            slack_user_id=body["user"]["id"],
        )

    @app.action("pref_revise")
    def on_pref_revise(ack: Ack, body: dict, client: WebClient) -> None:
        ack()
        flows.revise_preference(
            client,
            draft_id=int(body["actions"][0]["value"]),
            slack_user_id=body["user"]["id"],
        )

    @app.action("pref_cancel")
    def on_pref_cancel(ack: Ack, body: dict, client: WebClient) -> None:
        ack()
        flows.cancel_preference(
            client,
            draft_id=int(body["actions"][0]["value"]),
            slack_user_id=body["user"]["id"],
        )

    # ── 진재님 승인/거절 ─────────────────────────────────────
    @app.action("jjr_approve")
    def on_jjr_approve(ack: Ack, body: dict, client: WebClient, respond) -> None:
        ack()
        if body["user"]["id"] != ADMIN_JJR:
            respond(text=":no_entry_sign: 진재님 본인만 승인 가능.", response_type="ephemeral")
            return
        flows.on_jjr_approve(client, defer_id=int(body["actions"][0]["value"]))

    @app.action("jjr_reject")
    def on_jjr_reject(ack: Ack, body: dict, client: WebClient, respond) -> None:
        ack()
        if body["user"]["id"] != ADMIN_JJR:
            respond(text=":no_entry_sign: 진재님 본인만 거절 가능.", response_type="ephemeral")
            return
        flows.on_jjr_reject(client, defer_id=int(body["actions"][0]["value"]))

    # ── 대체자 수락/거절 ─────────────────────────────────────
    @app.action("replacement_accept")
    def on_replacement_accept(ack: Ack, body: dict, client: WebClient) -> None:
        ack()
        flows.on_replacement_accept(client, defer_id=int(body["actions"][0]["value"]))

    @app.action("replacement_decline")
    def on_replacement_decline(ack: Ack, body: dict, client: WebClient) -> None:
        ack()
        flows.on_replacement_decline(client, defer_id=int(body["actions"][0]["value"]))
