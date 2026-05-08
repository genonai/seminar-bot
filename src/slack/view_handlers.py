"""Slack view submission (modal 제출) 핸들러.

`/제출` 모달 → 사용자 제출 → ack → 백그라운드 thread에서 ingestion 진행.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import date

from slack_bolt import Ack, App
from slack_sdk import WebClient

from . import flows

log = logging.getLogger(__name__)


def register(app: App) -> None:
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
            },
            daemon=True,
            name=f"submission-{file_id}",
        ).start()
