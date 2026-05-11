"""Slack Modal 뷰 정의."""
from __future__ import annotations

import json
from datetime import date


def announce_modal(initial_text: str = "") -> dict:
    """`/세미나-공지` 운영자 한정 — 모든 BROADCAST_CHANNELS 에 발송할 메시지 미리보기."""
    return {
        "type": "modal",
        "callback_id": "broadcast_announce",
        "title": {"type": "plain_text", "text": "채널 공지"},
        "submit": {"type": "plain_text", "text": "발송"},
        "close": {"type": "plain_text", "text": "취소"},
        "blocks": [
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": "이 메시지는 *모든 공지 채널* 에 봇 명의로 게시됩니다."}],
            },
            {
                "type": "input",
                "block_id": "msg_block",
                "label": {"type": "plain_text", "text": "메시지 (마크다운 지원)"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "msg_input",
                    "multiline": True,
                    "initial_value": initial_text or "",
                    "max_length": 3000,
                    "placeholder": {"type": "plain_text", "text": "예: 다음 주 세미나 시간이 14:30으로 변경되었습니다."},
                },
            },
        ],
    }


def topic_modal(seminar_date: date, presenter: str, current_topic: str = "") -> dict:
    """`/세미나-토픽` 슬래시 응답으로 띄울 모달."""
    return {
        "type": "modal",
        "callback_id": "submit_topic",
        "private_metadata": json.dumps({
            "seminar_date": seminar_date.isoformat(),
            "presenter": presenter,
        }),
        "title": {"type": "plain_text", "text": "발표 토픽 등록"},
        "submit": {"type": "plain_text", "text": "저장"},
        "close": {"type": "plain_text", "text": "취소"},
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*{seminar_date.isoformat()} 발표*\n발표자: {presenter}\n"
                        "이번에 다룰 토픽을 한두 줄로 적어주세요. 월요일 채널 공지에 함께 표시됩니다."
                    ),
                },
            },
            {
                "type": "input",
                "block_id": "topic_block",
                "label": {"type": "plain_text", "text": "토픽"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "topic_input",
                    "multiline": True,
                    "initial_value": current_topic or "",
                    "max_length": 500,
                    "placeholder": {"type": "plain_text", "text": "예: LLM agent 자율 도구 사용 — ReAct vs Reflexion 비교"},
                },
            },
        ],
    }


def submission_modal(seminar_date: date, presenter: str, *, is_test: bool = False) -> dict:
    """`/제출` 슬래시 응답으로 띄울 모달.

    is_test=True 면 운영자 테스트 모드 — 채널 공지 skip, DB는 정상 기록.
    wipe_submissions.py 로 정리.
    """
    return {
        "type": "modal",
        "callback_id": "submit_material",
        "private_metadata": json.dumps({
            "seminar_date": seminar_date.isoformat(),
            "presenter": presenter,
            "is_test": is_test,
        }),
        "title": {"type": "plain_text", "text": "[테스트] 자료 제출" if is_test else "발표 자료 제출"},
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
