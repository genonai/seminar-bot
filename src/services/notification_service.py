"""자동 발송. notification_log 테이블로 같은 (type, target_date) 중복 발송 방지."""
from __future__ import annotations

import logging
import sqlite3
from datetime import date, timedelta

from slack_sdk import WebClient

from ..config import BROADCAST_CHANNELS, CHANNEL_ID, DEFER_DEADLINE_DAYS
from . import conversation_service, member_service, schedule_service, submission_service

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# notification_log 헬퍼
# ─────────────────────────────────────────────────────────────
def _try_record(conn: sqlite3.Connection, ntype: str, target: date) -> bool:
    """이미 발송됐는지 확인 + 표시. 발송 안 됐으면 True 반환 (호출자가 발송 진행)."""
    try:
        with conn:
            conn.execute(
                "INSERT INTO notification_log (type, target_date) VALUES (?, ?)",
                (ntype, target.isoformat()),
            )
        return True
    except sqlite3.IntegrityError:
        return False


def _open_dm(client: WebClient, slack_user_id: str) -> str:
    return client.conversations_open(users=slack_user_id)["channel"]["id"]


def broadcast(client: WebClient, *, text: str, blocks: list[dict] | None = None) -> None:
    """BROADCAST_CHANNELS 모든 채널에 발송. 한 채널 실패해도 나머지 진행."""
    for ch in BROADCAST_CHANNELS:
        try:
            kwargs: dict = {"channel": ch, "text": text}
            if blocks is not None:
                kwargs["blocks"] = blocks
            client.chat_postMessage(**kwargs)
        except Exception as e:
            log.warning("broadcast → %s 실패: %s", ch, e)


# ─────────────────────────────────────────────────────────────
# (수) 자료 마감 리마인더
# ─────────────────────────────────────────────────────────────
def send_wednesday_reminder(client: WebClient, conn: sqlite3.Connection, target_thu: date) -> None:
    """수요일 발송 (퇴근 전, cron 17:00). 내일(target_thu) 발표자 중
    아직 자료 제출 안 한 사람에게 DM. /제출 슬래시 명시."""
    if not _try_record(conn, "wednesday_reminder", target_thu):
        log.info("wednesday_reminder %s 이미 발송됨, skip", target_thu)
        return
    s = schedule_service.get_by_date(conn, target_thu)
    if s is None or s.status != "예정":
        return

    submitted = {sub.presenter for sub in submission_service.get_for_seminar(conn, target_thu)}
    for name in s.presenters():
        if name in submitted:
            log.info("wednesday_reminder: %s 이미 제출, skip", name)
            continue
        m = member_service.get_by_name(conn, name)
        if m is None:
            continue
        ch = _open_dm(client, m.slack_user_id)
        _dm_with_memory(client, conn, slack_user_id=m.slack_user_id, channel=ch,
            text=(
                f":memo: 내일 *{target_thu.month}/{target_thu.day}(목) 14:00* 발표 자료 마감입니다.\n"
                f"퇴근 전에 채널에서 `/제출` 슬래시로 PDF 한 개 올려주세요 :muscle:\n"
                "_봇이 자동으로 VLM 분석 + 채널에 공유 + RAG 인입까지 처리합니다._"
            ),
        )
        log.info("wednesday_reminder DM → %s for %s", m.name, target_thu)


# ─────────────────────────────────────────────────────────────
# (목) 오늘 발표 채널 공지
# ─────────────────────────────────────────────────────────────
def send_thursday_announce(client: WebClient, conn: sqlite3.Connection, today_thu: date) -> None:
    if not _try_record(conn, "thursday_announce", today_thu):
        return
    s = schedule_service.get_by_date(conn, today_thu)
    if s is None or s.status not in {"예정"}:
        return
    slot_1 = s.slot_1 or "_미정_"
    t1 = f" — _{s.slot_1_topic}_" if s.slot_1_topic else ""
    note_line = f"\n:pushpin: {s.notes}" if s.notes else ""
    broadcast(
        client,
        text=(
            f":sparkles: *오늘 14:00 주간 세미나*\n"
            f"  • 발표: *{slot_1}*{t1}{note_line}\n"
            "관심 있으신 분 모두 환영합니다 :coffee:"
        ),
    )
    log.info("thursday_announce → broadcast for %s", today_thu)


def send_monday_preview(client: WebClient, conn: sqlite3.Connection, today_mon: date) -> None:
    """매주 월요일 09:00 — 이번 주 목요일 발표자 + 토픽 미리 공지."""
    if not _try_record(conn, "monday_preview", today_mon):
        return
    # 이번 주 목요일 = today_mon + 3 (월=0, 목=3)
    target_thu = today_mon + timedelta(days=3)
    s = schedule_service.get_by_date(conn, target_thu)
    if s is None or s.status != "예정":
        log.info("monday_preview: %s 일정 없음 (또는 취소/완료), skip", target_thu)
        return

    slot_1 = s.slot_1 or "_미정_"
    t1 = f"\n     ↳ 토픽: {s.slot_1_topic}" if s.slot_1_topic else "\n     ↳ 토픽: _아직 미공유_"
    note_line = f"\n:pushpin: *안내*: {s.notes}" if s.notes else ""
    broadcast(
        client,
        text=(
            f":calendar: *이번 주 목요일 ({target_thu.month}/{target_thu.day}) 14:00 — 주간 세미나*\n"
            f"  • 발표: *{slot_1}*{t1}{note_line}\n"
            "발표자분 자료 마감은 수요일 14:00입니다 :muscle:"
        ),
    )
    log.info("monday_preview → broadcast for %s", target_thu)


def send_topic_reminders(client: WebClient, conn: sqlite3.Connection, today: date) -> None:
    """발표 7일 전 발표자에게 토픽 미등록이면 DM 알림."""
    target = today + timedelta(days=7)
    if target.weekday() != 3:
        return  # 목요일 아닌 경우 skip
    if not _try_record(conn, "topic_reminder", target):
        return
    s = schedule_service.get_by_date(conn, target)
    if s is None or s.status != "예정":
        return

    if s.slot_1 and not s.slot_1_topic:
        m = member_service.get_by_name(conn, s.slot_1)
        if m is not None:
            ch = _open_dm(client, m.slack_user_id)
            _dm_with_memory(client, conn, slack_user_id=m.slack_user_id, channel=ch,
                text=(
                    f":memo: 다음 주 {target.month}/{target.day}(목) 14:00 발표 — 아직 토픽 미공유.\n"
                    "이번에 다룰 내용 한 줄로 알려주시면 자동 저장됩니다."
                ),
            )
            log.info("topic_reminder DM → %s for %s", m.name, target)


# ─────────────────────────────────────────────────────────────
# 연기 마감 임박 안내 (8일 전 = 마감 1일 전)
# ─────────────────────────────────────────────────────────────
def send_defer_deadline_reminders(client: WebClient, conn: sqlite3.Connection, today: date) -> None:
    """오늘로부터 (DEFER_DEADLINE_DAYS + 1)일 후의 발표자에게 '내일이 연기 마감' DM."""
    target = today + timedelta(days=DEFER_DEADLINE_DAYS + 1)
    if target.weekday() != 3:
        return  # 목요일 아니면 skip
    if not _try_record(conn, "defer_deadline", target):
        return
    s = schedule_service.get_by_date(conn, target)
    if s is None or s.status != "예정":
        return
    for name in s.presenters():
        m = member_service.get_by_name(conn, name)
        if m is None:
            continue
        client.chat_postMessage(
            channel=_open_dm(client, m.slack_user_id),
            text=(
                f":alarm_clock: {target.isoformat()}(목) 발표 연기 신청 마감이 *내일* 입니다.\n"
                "변경 필요하면 채널에서 `/세미나-연기` 또는 봇 DM으로 알려주세요."
            ),
        )
        log.info("defer_deadline_reminder DM → %s for %s", m.name, target)


# ─────────────────────────────────────────────────────────────
# 새 사이클 자동 추첨 결과 채널 공지
# ─────────────────────────────────────────────────────────────
def announce_new_cycle(client: WebClient, schedules: list, cycle_id: int) -> None:
    if not schedules:
        return
    from ..slack.messages import fmt_date
    lines = [f":dart: *다음 사이클(cycle {cycle_id}) 자동 추첨 결과*", ""]
    for s in schedules:
        slot_1 = s.slot_1 or "_미정_"
        lines.append(f"• *{fmt_date(s.date)}* 14:00 — {slot_1}")
    lines.append("")
    lines.append("선호도 기반 자동 배정. 변경 의견은 운영자에게 알려주세요.")
    broadcast(client, text="\n".join(lines))


def broadcast_schedule_summary(
    client: WebClient, conn: sqlite3.Connection, *, scope: str
) -> tuple[bool, str]:
    """운영자 ad-hoc 일정 broadcast. scope='this_week' | 'upcoming'."""
    from ..slack import messages
    upcoming = schedule_service.get_upcoming(conn, limit=5)
    if not upcoming:
        return False, "다가올 일정이 없습니다."

    if scope == "this_week":
        s = upcoming[0]
        slot_1 = s.slot_1 or "_미정_"
        topic_line = (
            f"\n     ↳ 토픽: {s.slot_1_topic}" if s.slot_1_topic
            else "\n     ↳ 토픽: _아직 미공유_"
        )
        note_line = f"\n:pushpin: *안내*: {s.notes}" if s.notes else ""
        text = (
            f":calendar: *{messages.fmt_date(s.date)} 14:00 — 주간 세미나*\n"
            f"  • 발표: *{slot_1}*{topic_line}{note_line}"
        )
    else:  # upcoming
        lines = [":calendar: *다가올 세미나 일정*", ""]
        for s in upcoming:
            slot_1 = s.slot_1 or "_미정_"
            topic = f" — _{s.slot_1_topic}_" if s.slot_1_topic else ""
            lines.append(f"• *{messages.fmt_date(s.date)}* 14:00 — {slot_1}{topic}")
        text = "\n".join(lines)

    broadcast(client, text=text)
    return True, ""


def _dm_with_memory(client: WebClient, conn: sqlite3.Connection, *, slack_user_id: str, channel: str, text: str) -> None:
    """사용자 DM 발송 + conversation_service 에 봇 발화로 기록."""
    client.chat_postMessage(channel=channel, text=text)
    try:
        conversation_service.append(conn, slack_user_id, "assistant", text)
    except Exception as e:
        log.warning("conversation log (bot DM) 실패: %s", e)


def ask_for_topics(client: WebClient, conn: sqlite3.Connection, schedules: list) -> None:
    """새 사이클 추첨 직후, 토픽 없는 각 발표자에게 DM으로 토픽 요청."""
    from ..slack.messages import fmt_date

    for s in schedules:
        if not s.slot_1 or s.slot_1_topic:
            continue
        m = member_service.get_by_name(conn, s.slot_1)
        if m is None:
            continue
        try:
            ch = _open_dm(client, m.slack_user_id)
            _dm_with_memory(client, conn, slack_user_id=m.slack_user_id, channel=ch,
                text=(
                    f":wave: 안녕하세요 *{m.name}*님! *{fmt_date(s.date)} 14:00* 발표가 배정됐어요 :tada:\n\n"
                    "이번에 다룰 토픽 한 줄로 알려주시면 자동 저장됩니다.\n"
                    "예: _\"LLM agent ReAct vs Reflexion 비교\"_\n"
                    "수정도 새 메시지 보내시면 됩니다."
                ),
            )
            log.info("topic ask DM → %s for %s", m.name, s.date)
        except Exception as e:
            log.warning("topic ask DM → %s 실패: %s", m.name, e)
