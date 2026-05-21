"""Slack 메시지 + Block Kit 빌더."""
from __future__ import annotations

from datetime import date
from typing import Any

from ..config import ADMIN_JJR, CHANNEL_ID
from ..models import Schedule
from ..services import admin_service


def _primary_admin() -> str:
    return admin_service.get_primary_admin_id() or ADMIN_JJR


WEEKDAY_KO = ["월", "화", "수", "목", "금", "토", "일"]


def fmt_date(d: date | str) -> str:
    if isinstance(d, str):
        d = date.fromisoformat(d)
    return f"{d.month}/{d.day} ({WEEKDAY_KO[d.weekday()]})"


# ─────────────────────────────────────────────────────────────
# /세미나-일정
# ─────────────────────────────────────────────────────────────
def upcoming_schedule(
    schedules: list[Schedule],
    viewer_user_id: str,
    name_to_slack: dict[str, str],
) -> str:
    if not schedules:
        return f":calendar: 등록된 다가올 세미나가 없습니다. 운영자(<@{_primary_admin()}>)에게 문의해주세요."

    lines: list[str] = [":calendar: *다가올 세미나 일정*", ""]
    for s in schedules:
        viewer_in_slot = bool(s.slot_1 and name_to_slack.get(s.slot_1) == viewer_user_id)
        marker = ":star:" if viewer_in_slot else "  "
        slot_1 = s.slot_1 or "_미정_"
        lines.append(f"{marker} *{fmt_date(s.date)}* 14:00 — {slot_1}")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────
# DM 시작 메시지 (defer / preference)
# ─────────────────────────────────────────────────────────────
def defer_kickoff(requester_name: str, assigned: date, deadline: date, initial_text: str) -> str:
    extra = f"\n\n> {initial_text}" if initial_text.strip() else ""
    return (
        f":wave: 안녕하세요 *{requester_name}*님. {fmt_date(assigned)} 발표 연기 신청 도와드릴게요.\n"
        f"마감은 *{fmt_date(deadline)}* 까지입니다.\n"
        f"이 DM에서 사정과 가능한 일정 등을 편하게 알려주시면 정리해서 진재님께 전달드립니다.{extra}"
    )


def preference_kickoff(member_name: str, current_summary: str) -> str:
    return (
        f":wave: 안녕하세요 *{member_name}*님. 평상시 발표 선호도 등록 도와드릴게요.\n"
        f"현재 저장된 값: {current_summary}\n"
        "회피 날짜, 회피하고 싶은 월내 주차 (예: 매달 마지막주) 등을 자유롭게 알려주세요."
    )


# ─────────────────────────────────────────────────────────────
# Preview blocks (사용자 confirm 직전)
# ─────────────────────────────────────────────────────────────
def defer_preview_blocks(*, draft_id: int, payload: dict[str, Any], assigned: date) -> list[dict[str, Any]]:
    reason = payload.get("reason", "")
    pref_dates = payload.get("preferred_replacement_dates") or []
    avoid_dates = payload.get("additional_avoid_dates") or []

    fields: list[str] = [f"*원래 날짜:*\n{fmt_date(assigned)}", f"*사유:*\n{reason}"]
    if pref_dates:
        fields.append(f"*가능한 날짜로 언급된 것:*\n{', '.join(pref_dates)}")
    if avoid_dates:
        fields.append(f"*추가 회피 날짜:*\n{', '.join(avoid_dates)}")

    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": ":mag: *연기 신청 미리보기*"}},
        {"type": "section", "fields": [{"type": "mrkdwn", "text": f} for f in fields]},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "style": "primary",
                    "text": {"type": "plain_text", "text": "이대로 신청"},
                    "action_id": "defer_confirm",
                    "value": str(draft_id),
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "수정하기"},
                    "action_id": "defer_revise",
                    "value": str(draft_id),
                },
                {
                    "type": "button",
                    "style": "danger",
                    "text": {"type": "plain_text", "text": "취소"},
                    "action_id": "defer_cancel",
                    "value": str(draft_id),
                },
            ],
        },
    ]


def preference_preview_blocks(*, draft_id: int, payload: dict[str, Any]) -> list[dict[str, Any]]:
    avoid_dates = payload.get("avoid_dates") or []
    avoid_weeks = payload.get("avoid_weeks_of_month") or []

    fields: list[str] = []
    fields.append(f"*회피 날짜:*\n{', '.join(avoid_dates) if avoid_dates else '없음'}")
    fields.append(f"*회피 주차:*\n{', '.join(map(str, avoid_weeks)) if avoid_weeks else '없음'}")

    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": ":mag: *선호도 등록 미리보기*"}},
        {"type": "section", "fields": [{"type": "mrkdwn", "text": f} for f in fields]},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "style": "primary",
                    "text": {"type": "plain_text", "text": "이대로 저장"},
                    "action_id": "pref_confirm",
                    "value": str(draft_id),
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "수정하기"},
                    "action_id": "pref_revise",
                    "value": str(draft_id),
                },
                {
                    "type": "button",
                    "style": "danger",
                    "text": {"type": "plain_text", "text": "취소"},
                    "action_id": "pref_cancel",
                    "value": str(draft_id),
                },
            ],
        },
    ]


# ─────────────────────────────────────────────────────────────
# 승인 DM 블록 (진재 + 대체자)
# ─────────────────────────────────────────────────────────────
def jjr_approval_blocks(
    *, defer_id: int, requester: str, original_date: date, reason: str,
    proposed_replacement: str, hints: dict[str, Any], attempts: int,
) -> list[dict[str, Any]]:
    pref_dates = hints.get("preferred_replacement_dates") or []
    avoid_dates = hints.get("additional_avoid_dates") or []

    extras: list[str] = []
    if pref_dates:
        extras.append(f"*신청자가 가능하다고 한 날짜:* {', '.join(pref_dates)}")
    if avoid_dates:
        extras.append(f"*신청자 추가 회피 날짜:* {', '.join(avoid_dates)}")
    if attempts > 1:
        extras.append(f":arrows_counterclockwise: *재시도 {attempts}번째* (이전 후보 거절)")

    fields = [
        f"*신청자:*\n{requester}",
        f"*원래 날짜:*\n{fmt_date(original_date)}",
        f"*사유:*\n{reason}",
        f"*제안 대체자:*\n{proposed_replacement}",
    ]

    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": ":warning: *연기 신청 — 운영자 승인 필요*"}},
        {"type": "section", "fields": [{"type": "mrkdwn", "text": f} for f in fields]},
    ]
    if extras:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(extras)}})
    blocks.append({
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": f"대체자에게도 DM 발송됨. 양쪽 승인 시 자동 반영. (id={defer_id})"}],
    })
    blocks.append({
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "style": "primary",
                "text": {"type": "plain_text", "text": "승인"},
                "action_id": "jjr_approve",
                "value": str(defer_id),
            },
            {
                "type": "button",
                "style": "danger",
                "text": {"type": "plain_text", "text": "거절"},
                "action_id": "jjr_reject",
                "value": str(defer_id),
            },
        ],
    })
    return blocks


def replacement_request_blocks(
    *, defer_id: int, requester: str, original_date: date, reason: str,
) -> list[dict[str, Any]]:
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f":raising_hand: *대체 발표 부탁*\n"
                    f"*{requester}*님이 *{fmt_date(original_date)}* 발표를 연기 신청했습니다.\n"
                    f"사유: {reason}\n\n"
                    f"같은 슬롯을 맡아주실 수 있을까요? 양쪽(진재님 + 본인) 모두 승인 시 확정됩니다."
                ),
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "style": "primary",
                    "text": {"type": "plain_text", "text": "수락"},
                    "action_id": "replacement_accept",
                    "value": str(defer_id),
                },
                {
                    "type": "button",
                    "style": "danger",
                    "text": {"type": "plain_text", "text": "거절"},
                    "action_id": "replacement_decline",
                    "value": str(defer_id),
                },
            ],
        },
    ]


# ─────────────────────────────────────────────────────────────
# 최종 알림
# ─────────────────────────────────────────────────────────────
def channel_announcement(*, requester: str, replacement: str, original_date: date) -> str:
    return (
        f":bell: *연기 처리 완료*\n"
        f"{fmt_date(original_date)} 발표가 *{requester} → {replacement}*로 변경되었습니다."
    )


def requester_done_dm(*, replacement: str, original_date: date) -> str:
    return (
        f":white_check_mark: 연기 신청이 처리됐습니다.\n"
        f"{fmt_date(original_date)} 발표는 *{replacement}*님이 대신 맡아주십니다. 감사합니다!"
    )


def replacement_thanks_dm(*, original_date: date, requester: str) -> str:
    return (
        f":raised_hands: 수락 감사합니다. *{fmt_date(original_date)}* 발표를 부탁드립니다 "
        f"({requester}님 자리)."
    )


def escalation_dm() -> str:
    return (
        f":sos: 대체자 후보 모두 거절했습니다. 운영자(<@{_primary_admin()}>)가 수동으로 처리해 주세요. "
        f"필요 시 채널(<#{CHANNEL_ID}>)에 직접 공지 부탁드립니다."
    )


def jjr_rejection_dm(*, requester: str, original_date: date) -> str:
    return (
        f":x: {requester}님 {fmt_date(original_date)} 연기 신청이 운영자(진재)에 의해 거절되었습니다."
    )


# ─────────────────────────────────────────────────────────────
# 버튼 클릭 후 메시지 freeze (actions 제거 + status 추가)
# ─────────────────────────────────────────────────────────────
def freeze_with_status(original_blocks: list[dict], status_text: str) -> list[dict]:
    """기존 메시지 블록에서 actions 블록을 제거하고 status context를 footer에 추가."""
    kept = [b for b in original_blocks if b.get("type") != "actions"]
    kept.append({
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": status_text}],
    })
    return kept
