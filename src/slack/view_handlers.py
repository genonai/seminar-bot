"""Slack view submission (modal 제출) 핸들러.

`/제출` 모달 → 사용자 제출 → ack → 백그라운드 thread에서 ingestion 진행.
`/세미나-토픽` 모달 → schedule 테이블의 토픽 컬럼 업데이트.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import date

from slack_bolt import Ack, App
from slack_sdk import WebClient

from ..config import BROADCAST_CHANNELS, DB_PATH
from ..db import session
from ..services import admin_service, notification_service, schedule_service
from . import flows

log = logging.getLogger(__name__)


def register(app: App) -> None:
    @app.view("broadcast_announce")
    def on_broadcast_announce(ack: Ack, body: dict, view: dict, client: WebClient) -> None:
        slack_user_id = body["user"]["id"]
        # 보안: 운영자만 발송 가능
        if not admin_service.is_admin(slack_user_id):
            ack({"response_action": "errors", "errors": {"msg_block": "운영자만 발송 가능합니다."}})
            return

        text = (
            view.get("state", {}).get("values", {})
                .get("msg_block", {}).get("msg_input", {}).get("value")
            or ""
        ).strip()
        if not text:
            ack({"response_action": "errors", "errors": {"msg_block": "메시지를 입력해주세요."}})
            return
        ack()

        notification_service.broadcast(client, text=text)
        # 발송한 운영자에게 DM 확인
        try:
            dm = client.conversations_open(users=slack_user_id)["channel"]["id"]
            client.chat_postMessage(
                channel=dm,
                text=(
                    f":mega: 공지 발송 완료 — {len(BROADCAST_CHANNELS)}개 채널에 게시.\n"
                    f"> {text[:300]}{'...' if len(text) > 300 else ''}"
                ),
            )
        except Exception as e:
            log.warning("발송 확인 DM 실패: %s", e)
        log.info("/세미나-공지 by %s → %d 채널 (text len=%d)", slack_user_id, len(BROADCAST_CHANNELS), len(text))

    @app.view("submit_seminar_note")
    def on_submit_seminar_note(ack: Ack, body: dict, view: dict, client: WebClient) -> None:
        slack_user_id = body["user"]["id"]
        if not admin_service.is_admin(slack_user_id):
            ack({"response_action": "errors", "errors": {"date_block": "운영자만 등록 가능합니다."}})
            return
        state = view.get("state", {}).get("values", {})
        date_str = (
            state.get("date_block", {})
                 .get("date_select", {})
                 .get("selected_option", {})
                 .get("value")
            or ""
        )
        note = (
            state.get("note_block", {})
                 .get("note_input", {})
                 .get("value")
            or ""
        ).strip()

        if not date_str:
            ack({"response_action": "errors", "errors": {"date_block": "회차를 선택해주세요."}})
            return
        ack()

        try:
            target = date.fromisoformat(date_str)
        except Exception:
            log.warning("invalid date: %s", date_str)
            return

        with session(DB_PATH) as conn:
            ok = schedule_service.set_notes(conn, target, note if note else None)

        dm = client.conversations_open(users=slack_user_id)["channel"]["id"]
        if ok:
            if note:
                client.chat_postMessage(
                    channel=dm,
                    text=f":pushpin: 안내사항 저장됨 — *{target.isoformat()}*\n> {note}",
                )
            else:
                client.chat_postMessage(
                    channel=dm,
                    text=f":wastebasket: 안내사항 삭제됨 — *{target.isoformat()}*",
                )
            log.info("seminar_note via modal: %s ← %r", target, note[:120])
        else:
            client.chat_postMessage(channel=dm, text=":x: 해당 일정 못 찾음.")

    @app.view("submit_topic")
    def on_submit_topic(ack: Ack, body: dict, view: dict, client: WebClient) -> None:
        state = view.get("state", {}).get("values", {})
        topic = (
            state.get("topic_block", {})
                 .get("topic_input", {})
                 .get("value")
            or ""
        ).strip()
        if not topic:
            ack({"response_action": "errors", "errors": {"topic_block": "토픽을 입력해주세요."}})
            return
        ack()

        try:
            metadata = json.loads(view.get("private_metadata", "{}"))
            seminar_date = date.fromisoformat(metadata["seminar_date"])
            presenter = metadata["presenter"]
        except Exception:
            log.exception("topic private_metadata 파싱 실패")
            return

        with session(DB_PATH) as conn:
            ok = schedule_service.set_topic(conn, seminar_date, presenter, topic)

        slack_user_id = body["user"]["id"]
        dm_channel = client.conversations_open(users=slack_user_id)["channel"]["id"]
        if ok:
            client.chat_postMessage(
                channel=dm_channel,
                text=f":white_check_mark: 토픽 저장됨 — *{seminar_date.isoformat()}*: _{topic}_",
            )
            log.info("topic saved: %s / %s → %r", presenter, seminar_date, topic[:80])
        else:
            client.chat_postMessage(
                channel=dm_channel,
                text=":x: 일정을 찾지 못해 토픽 저장 실패. 운영자에게 문의해주세요.",
            )

    @app.view("submit_material")
    def on_submit_material(ack: Ack, body: dict, view: dict, client: WebClient) -> None:
        # 빈 파일 케이스 등 입력 검증 — errors response action으로 모달 유지 가능
        state = view.get("state", {}).get("values", {})
        files = (
            state.get("file_block", {})
                 .get("pdf_input", {})
                 .get("files", [])
        )
        if not files:
            ack({
                "response_action": "errors",
                "errors": {"file_block": "PDF 파일 1개를 업로드해주세요."},
            })
            return

        # 정상 — 모달 닫기
        ack()

        try:
            metadata = json.loads(view.get("private_metadata", "{}"))
            seminar_date = date.fromisoformat(metadata["seminar_date"])
            presenter = metadata["presenter"]
            is_test = bool(metadata.get("is_test", False))
        except Exception:
            log.exception("private_metadata 파싱 실패")
            return

        file_id = files[0]["id"]
        title_input = (
            state.get("title_block", {})
                 .get("title_input", {})
                 .get("value")
            or ""
        ).strip()
        slack_user_id = body["user"]["id"]
        log.info(
            "submit_material 수신: user=%s presenter=%s seminar_date=%s file_id=%s title_hint=%r",
            slack_user_id, presenter, seminar_date, file_id, title_input,
        )

        # 백그라운드 처리 (PDF 다운로드 + VLM + Weaviate)
        threading.Thread(
            target=flows.process_submission_async,
            kwargs={
                "client": client,
                "slack_user_id": slack_user_id,
                "presenter": presenter,
                "seminar_date": seminar_date,
                "file_id": file_id,
                "title_hint": title_input,
                "is_test": is_test,
            },
            daemon=True,
            name=f"submission-{file_id}",
        ).start()
