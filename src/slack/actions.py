"""Block Kit 버튼 핸들러."""
from __future__ import annotations

import logging
from datetime import date

from slack_bolt import Ack, App, Respond
from slack_sdk import WebClient

from ..config import ADMIN_JJR
from . import flows, messages

log = logging.getLogger(__name__)


def _freeze(respond: Respond, body: dict, status_text: str) -> None:
    """버튼이 달린 원본 메시지를 status로 잠금 (actions 제거 + footer 추가)."""
    blocks = body.get("message", {}).get("blocks", [])
    new_blocks = messages.freeze_with_status(blocks, status_text)
    respond(replace_original=True, blocks=new_blocks, text=status_text)


def register(app: App) -> None:
    # ── 신청자 confirm/revise/cancel — defer ─────────────────
    @app.action("defer_confirm")
    def on_defer_confirm(ack: Ack, body: dict, client: WebClient, respond: Respond) -> None:
        ack()
        _freeze(respond, body, ":white_check_mark: 신청 완료 — 운영자 + 대체자 후보에게 승인 요청 발송됨")
        flows.confirm_defer(
            client,
            draft_id=int(body["actions"][0]["value"]),
            slack_user_id=body["user"]["id"],
            today=date.today(),
        )

    @app.action("defer_revise")
    def on_defer_revise(ack: Ack, body: dict, client: WebClient, respond: Respond) -> None:
        ack()
        _freeze(respond, body, ":pencil2: 수정 모드 — 다음 메시지로 변경할 내용을 알려주세요")
        flows.revise_defer(
            client,
            draft_id=int(body["actions"][0]["value"]),
            slack_user_id=body["user"]["id"],
        )

    @app.action("defer_cancel")
    def on_defer_cancel(ack: Ack, body: dict, client: WebClient, respond: Respond) -> None:
        ack()
        _freeze(respond, body, ":wave: 취소됨")
        flows.cancel_defer(
            client,
            draft_id=int(body["actions"][0]["value"]),
            slack_user_id=body["user"]["id"],
        )

    # ── 신청자 confirm/revise/cancel — preference ────────────
    @app.action("pref_confirm")
    def on_pref_confirm(ack: Ack, body: dict, client: WebClient, respond: Respond) -> None:
        ack()
        _freeze(respond, body, ":white_check_mark: 선호도 저장됨")
        flows.confirm_preference(
            client,
            draft_id=int(body["actions"][0]["value"]),
            slack_user_id=body["user"]["id"],
        )

    @app.action("pref_revise")
    def on_pref_revise(ack: Ack, body: dict, client: WebClient, respond: Respond) -> None:
        ack()
        _freeze(respond, body, ":pencil2: 수정 모드 — 다음 메시지로 변경할 내용을 알려주세요")
        flows.revise_preference(
            client,
            draft_id=int(body["actions"][0]["value"]),
            slack_user_id=body["user"]["id"],
        )

    @app.action("pref_cancel")
    def on_pref_cancel(ack: Ack, body: dict, client: WebClient, respond: Respond) -> None:
        ack()
        _freeze(respond, body, ":wave: 취소됨")
        flows.cancel_preference(
            client,
            draft_id=int(body["actions"][0]["value"]),
            slack_user_id=body["user"]["id"],
        )

    # ── 진재님 승인/거절 ─────────────────────────────────────
    @app.action("jjr_approve")
    def on_jjr_approve(ack: Ack, body: dict, client: WebClient, respond: Respond) -> None:
        ack()
        if body["user"]["id"] != ADMIN_JJR:
            respond(text=":no_entry_sign: 진재님 본인만 승인 가능.", response_type="ephemeral")
            return
        _freeze(respond, body, f":white_check_mark: 승인됨 (by <@{ADMIN_JJR}>)")
        flows.on_jjr_approve(client, defer_id=int(body["actions"][0]["value"]))

    @app.action("jjr_reject")
    def on_jjr_reject(ack: Ack, body: dict, client: WebClient, respond: Respond) -> None:
        ack()
        if body["user"]["id"] != ADMIN_JJR:
            respond(text=":no_entry_sign: 진재님 본인만 거절 가능.", response_type="ephemeral")
            return
        _freeze(respond, body, f":x: 거절됨 (by <@{ADMIN_JJR}>)")
        flows.on_jjr_reject(client, defer_id=int(body["actions"][0]["value"]))

    # ── 대체자 수락/거절 ─────────────────────────────────────
    @app.action("replacement_accept")
    def on_replacement_accept(ack: Ack, body: dict, client: WebClient, respond: Respond) -> None:
        ack()
        _freeze(respond, body, ":white_check_mark: 수락 — 양쪽 승인 시 자동 반영")
        flows.on_replacement_accept(client, defer_id=int(body["actions"][0]["value"]))

    @app.action("replacement_decline")
    def on_replacement_decline(ack: Ack, body: dict, client: WebClient, respond: Respond) -> None:
        ack()
        _freeze(respond, body, ":x: 거절 — 차순위 후보에게 자동 재요청")
        flows.on_replacement_decline(client, defer_id=int(body["actions"][0]["value"]))
