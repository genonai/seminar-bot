"""Slack Modal 뷰 정의."""
from __future__ import annotations

import json
from datetime import date


def submission_modal(seminar_date: date, presenter: str) -> dict:
    """`/제출` 슬래시 응답으로 띄울 모달."""
    return {
        "type": "modal",
        "callback_id": "submit_material",
        "private_metadata": json.dumps({
            "seminar_date": seminar_date.isoformat(),
            "presenter": presenter,
        }),
        "title": {"type": "plain_text", "text": "발표 자료 제출"},
        "submit": {"type": "plain_text", "text": "제출"},
        "close": {"type": "plain_text", "text": "취소"},
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*{seminar_date.isoformat()} 발표 자료* 제출\n"
                        f"발표자: {presenter}\n"
                        "PDF 한 개 업로드해주세요. 제출 후 봇이 처리(VLM 분석 + 메타 추출)한 뒤 채널에 공유됩니다."
                    ),
                },
            },
            {
                "type": "input",
                "block_id": "file_block",
                "label": {"type": "plain_text", "text": "PDF 파일"},
                "element": {
                    "type": "file_input",
                    "action_id": "pdf_input",
                    "filetypes": ["pdf"],
                    "max_files": 1,
                },
            },
            {
                "type": "input",
                "block_id": "title_block",
                "optional": True,
                "label": {"type": "plain_text", "text": "발표 제목 (선택, 비우면 자동 추출)"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "title_input",
                    "max_length": 200,
                },
            },
        ],
    }
