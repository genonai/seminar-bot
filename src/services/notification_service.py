"""자동 발송. notification_log 테이블로 같은 (type, target_date) 중복 발송 방지."""
from __future__ import annotations

import logging
import sqlite3
from datetime import date, timedelta

from slack_sdk import WebClient

from ..config import BROADCAST_CHANNELS, CHANNEL_ID, DEFER_DEADLINE_DAYS
from . import member_service, schedule_service

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
    """수요일 14:00 발송. 다음날(target_thu) 발표자에게 자료 마감 안내 DM."""
    if not _try_record(conn, "wednesday_reminder", target_thu):
        log.info("wednesday_reminder %s 이미 발송됨, skip", target_thu)
        return
    s = schedule_service.get_by_date(conn, target_thu)
    if s is None or s.status != "예정":
        return
    for name in s.presenters():
        m = member_service.get_by_name(conn, name)
        if m is None:
            continue
        client.chat_postMessage(
            channel=_open_dm(client, m.slack_user_id),
            text=(
                f":memo: 내일({target_thu.isoformat()}, 목) 발표 자료 마감입니다.\n"
                f"오늘(수) 14:00 까지 자료 공유 부탁드립니다 — 화이팅!"
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
    slot_2 = s.slot_2 or "_미정_"
    t1 = f" — _{s.slot_1_topic}_" if s.slot_1_topic else ""
    t2 = f" — _{s.slot_2_topic}_" if s.slot_2_topic else ""
    broadcast(
        client,
        text=(
            f":sparkles: *오늘 14:00 주간 세미나*\n"
            f"  • 1부: *{slot_1}*{t1}\n"
            f"  • 2부: *{slot_2}*{t2}\n"
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
    slot_2 = s.slot_2 or "_미정_"
    t1 = f"\n     ↳ 토픽: {s.slot_1_topic}" if s.slot_1_topic else "\n     ↳ 토픽: _아직 미공유_"
    t2 = f"\n     ↳ 토픽: {s.slot_2_topic}" if s.slot_2_topic else "\n     ↳ 토픽: _아직 미공유_"
    broadcast(
        client,
        text=(
            f":calendar: *이번 주 목요일 ({target_thu.month}/{target_thu.day}) 14:00 — 주간 세미나*\n"
            f"  • 1부 (14:00-14:30): *{slot_1}*{t1}\n"
            f"  • 2부 (14:30-15:00): *{slot_2}*{t2}\n"
            "발표자분들 자료 마감은 수요일 14:00입니다 :muscle:"
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

    for slot_name, topic, slot_label in [
        (s.slot_1, s.slot_1_topic, "1부"),
        (s.slot_2, s.slot_2_topic, "2부"),
    ]:
        if not slot_name or topic:
            continue
        m = member_service.get_by_name(conn, slot_name)
        if m is None:
            continue
        client.chat_postMessage(
            channel=_open_dm(client, m.slack_user_id),
            text=(
                f":memo: 다음 주 {target.month}/{target.day}(목) *{slot_label}* 발표 — 아직 토픽 미공유.\n"
                "`/세미나-토픽` 또는 봇 DM에 \"내 토픽은 ~\" 으로 등록 부탁드립니다."
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
        slot_2 = s.slot_2 or "_미정_"
        lines.append(f"• *{fmt_date(s.date)}* — 1부: {slot_1} / 2부: {slot_2}")
    lines.append("")
    lines.append("선호도 기반 자동 배정. 변경 의견은 운영자에게 알려주세요.")
    broadcast(client, text="\n".join(lines))
