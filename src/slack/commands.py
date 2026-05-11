"""Slash command 핸들러."""
from __future__ import annotations

import logging
from datetime import date, timedelta

from slack_bolt import Ack, App, Respond
from slack_sdk import WebClient

from ..config import DB_PATH
from ..db import session
from ..services import (
    admin_service,
    cycle_service,
    defer_service,
    member_service,
    notification_service,
    schedule_service,
)
from . import flows, guards, messages, views

log = logging.getLogger(__name__)


def _parse_slack_user(text: str) -> str | None:
    """슬래시 인자에서 슬랙 user ID 추출.
    형식: '<@U07GFTZ6LM8|jinjae>' 또는 '<@U07GFTZ6LM8>' 또는 'U07GFTZ6LM8'."""
    text = text.strip()
    if not text:
        return None
    if text.startswith("<@") and ">" in text:
        inner = text[2:text.index(">")]
        return inner.split("|")[0]
    first = text.split()[0]
    if first.startswith("U") and len(first) >= 9:
        return first
    return None


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

    @app.command("/세미나-재추첨")
    def handle_regenerate(ack: Ack, body: dict, respond: Respond, client: WebClient) -> None:
        """운영자 한정. 현재 사이클 폐기 + 채널 멤버 sync + 새로 추첨 + 채널 공지."""
        ack()
        user_id = body["user_id"]
        log.info("/세미나-재추첨 user=%s channel=%s", user_id, body.get("channel_id"))
        if not admin_service.is_admin(user_id):
            guards.reject_non_admin(respond)
            return
        if not guards.in_seminar_channel(body):
            guards.reject_wrong_channel(respond)
            return

        with session(DB_PATH) as conn:
            active, errors = member_service.sync_from_channel(client, conn)
            if errors and not active:
                respond(text=f":x: 멤버 sync 실패: {errors}. `channels:read` scope 확인.", response_type="ephemeral")
                return

            row = conn.execute("SELECT MAX(cycle_id) AS m FROM schedule").fetchone()
            current_cid = row["m"]
            if current_cid is not None:
                with conn:
                    conn.execute("DELETE FROM schedule WHERE cycle_id = ?", (current_cid,))

            try:
                new_cid, schedules = cycle_service.generate_next_cycle(
                    conn, date.today() + timedelta(days=1)
                )
            except ValueError as e:
                respond(text=f":x: 추첨 실패: {e}", response_type="ephemeral")
                return
            notification_service.ask_for_topics(client, conn, schedules)

        notification_service.announce_new_cycle(client, schedules, new_cid)
        respond(
            text=(
                f":white_check_mark: 재추첨 완료 — cycle {new_cid}, "
                f"활성 멤버 {len(active)}명 기반 {len(schedules)}주 일정. 채널에 공지됨."
            ),
            response_type="ephemeral",
        )

    @app.command("/세미나-토픽")
    def handle_topic(ack: Ack, body: dict, respond: Respond, client: WebClient) -> None:
        ack()
        user_id = body["user_id"]
        log.info("/세미나-토픽 user=%s channel=%s", user_id, body.get("channel_id"))
        if not guards.in_seminar_channel(body):
            guards.reject_wrong_channel(respond)
            return
        if not guards.is_member_or_admin(user_id):
            guards.reject_non_member(respond)
            return

        today = date.today()
        with session(DB_PATH) as conn:
            assignment = defer_service.find_requester_assignment(conn, user_id, today)
            if assignment is None:
                respond(
                    text=":information_source: 다가올 발표 일정이 없어 토픽 등록 대상이 아닙니다.",
                    response_type="ephemeral",
                )
                return
            seminar_date, presenter = assignment
            s = schedule_service.get_by_date(conn, seminar_date)
            current_topic = (s.topic_for(presenter) if s else "") or ""

        try:
            client.views_open(
                trigger_id=body["trigger_id"],
                view=views.topic_modal(seminar_date, presenter, current_topic),
            )
        except Exception as e:
            log.exception("/세미나-토픽 modal 열기 실패")
            respond(text=f":x: 모달 열기 실패: {e}", response_type="ephemeral")

    @app.command("/제출")
    def handle_submit(ack: Ack, body: dict, respond: Respond, client: WebClient) -> None:
        ack()
        user_id = body["user_id"]
        log.info("/제출 user=%s channel=%s", user_id, body.get("channel_id"))
        if not guards.in_seminar_channel(body):
            guards.reject_wrong_channel(respond)
            return
        if not guards.is_member_or_admin(user_id):
            guards.reject_non_member(respond)
            return

        today = date.today()
        is_test = False
        with session(DB_PATH) as conn:
            assignment = defer_service.find_requester_assignment(conn, user_id, today)

        if assignment is None:
            # 운영자는 테스트 모드 허용: 다음 목요일 placeholder, 채널 공지 skip
            if admin_service.is_admin(user_id):
                from datetime import timedelta
                days = (3 - today.weekday()) % 7 or 7
                seminar_date = today + timedelta(days=days)
                try:
                    info = client.users_info(user=user_id)
                    real_name = info["user"].get("real_name") or info["user"].get("name") or user_id
                except Exception:
                    real_name = user_id
                presenter = f"[TEST] {real_name}"
                is_test = True
            else:
                respond(
                    text=":information_source: 다가올 발표 일정이 없어 자료 제출 대상이 아닙니다.",
                    response_type="ephemeral",
                )
                return
        else:
            seminar_date, presenter = assignment

        try:
            client.views_open(
                trigger_id=body["trigger_id"],
                view=views.submission_modal(seminar_date, presenter, is_test=is_test),
            )
        except Exception as e:
            log.exception("/제출 modal 열기 실패")
            respond(text=f":x: 모달 열기 실패: {e}", response_type="ephemeral")

    @app.command("/세미나-안내")
    def handle_seminar_note(ack: Ack, body: dict, respond: Respond, client: WebClient) -> None:
        """운영자 한정. 다가올 회차에 운영 안내 노트 등록/수정."""
        ack()
        user_id = body["user_id"]
        log.info("/세미나-안내 user=%s channel=%s", user_id, body.get("channel_id"))
        if not admin_service.is_admin(user_id):
            guards.reject_non_admin(respond)
            return
        if not guards.in_seminar_channel(body):
            guards.reject_wrong_channel(respond)
            return

        today = date.today()
        with session(DB_PATH) as conn:
            upcoming = schedule_service.get_upcoming(conn, today=today, limit=10)
        if not upcoming:
            respond(text=":information_source: 다가올 일정이 없습니다.", response_type="ephemeral")
            return
        try:
            client.views_open(
                trigger_id=body["trigger_id"],
                view=views.seminar_note_modal(upcoming),
            )
        except Exception as e:
            log.exception("/세미나-안내 modal 열기 실패")
            respond(text=f":x: 모달 열기 실패: {e}", response_type="ephemeral")

    @app.command("/세미나-공지")
    def handle_announce(ack: Ack, body: dict, respond: Respond, client: WebClient) -> None:
        """운영자 한정. 모든 BROADCAST_CHANNELS 에 임의 메시지 발송."""
        ack()
        user_id = body["user_id"]
        log.info("/세미나-공지 user=%s channel=%s", user_id, body.get("channel_id"))
        if not admin_service.is_admin(user_id):
            guards.reject_non_admin(respond)
            return
        if not guards.in_seminar_channel(body):
            guards.reject_wrong_channel(respond)
            return

        initial = (body.get("text") or "").strip()
        try:
            client.views_open(
                trigger_id=body["trigger_id"],
                view=views.announce_modal(initial),
            )
        except Exception as e:
            log.exception("/세미나-공지 modal 열기 실패")
            respond(text=f":x: 모달 열기 실패: {e}", response_type="ephemeral")

    @app.command("/세미나-토픽-알림")
    def handle_topic_remind(ack: Ack, body: dict, respond: Respond, client: WebClient) -> None:
        """운영자 한정. 다가올 첫 세미나의 토픽 미등록 발표자에게 DM 발송."""
        ack()
        user_id = body["user_id"]
        log.info("/세미나-토픽-알림 user=%s channel=%s", user_id, body.get("channel_id"))
        if not admin_service.is_admin(user_id):
            guards.reject_non_admin(respond)
            return
        if not guards.in_seminar_channel(body):
            guards.reject_wrong_channel(respond)
            return

        today = date.today()
        with session(DB_PATH) as conn:
            next_s = schedule_service.get_next_seminar(conn, today)
            if next_s is None:
                respond(text=":information_source: 다가올 일정이 없습니다.", response_type="ephemeral")
                return

            sent: list[str] = []
            already: list[str] = []
            for slot_name, topic, slot_label in [
                (next_s.slot_1, next_s.slot_1_topic, "1부"),
                (next_s.slot_2, next_s.slot_2_topic, "2부"),
            ]:
                if not slot_name:
                    continue
                if topic:
                    already.append(f"{slot_label} {slot_name} (이미 등록)")
                    continue
                m = member_service.get_by_name(conn, slot_name)
                if m is None:
                    continue
                try:
                    dm = client.conversations_open(users=m.slack_user_id)["channel"]["id"]
                    client.chat_postMessage(
                        channel=dm,
                        text=(
                            f":memo: {next_s.date.month}/{next_s.date.day}(목) *{slot_label}* 발표 토픽이 아직 등록 안 됐어요.\n"
                            "이번에 다룰 내용을 한 줄로 봇 DM에 보내주시면 자동 저장됩니다.\n"
                            "예: _\"LLM agent ReAct vs Reflexion 비교\"_"
                        ),
                    )
                    sent.append(f"{slot_label} {slot_name}")
                    log.info("topic remind DM → %s for %s", m.name, next_s.date)
                except Exception as e:
                    log.warning("topic remind DM → %s 실패: %s", m.name, e)

        msg_parts = [f":mailbox: 다가올 세미나 *{next_s.date.isoformat()}* 기준 토픽 알림 발송 결과:"]
        if sent:
            msg_parts.append(":bell: DM 발송: " + ", ".join(sent))
        if already:
            msg_parts.append(":white_check_mark: 등록 완료: " + ", ".join(already))
        if not sent and not already:
            msg_parts.append("발표자 배정 없음")
        respond(text="\n".join(msg_parts), response_type="ephemeral")

    # ─── 운영자 관리 ──────────────────────────────────────────
    @app.command("/어드민-추가")
    def handle_admin_add(ack: Ack, body: dict, respond: Respond) -> None:
        ack()
        if not admin_service.is_admin(body["user_id"]):
            guards.reject_non_admin(respond)
            return
        target = _parse_slack_user(body.get("text", "") or "")
        if target is None:
            respond(
                text="형식: `/어드민-추가 @사용자` (Slack 멘션 자동완성 사용)",
                response_type="ephemeral",
            )
            return
        with session(DB_PATH) as conn:
            added = admin_service.add_admin(conn, target, added_by=body["user_id"])
        if added:
            respond(text=f":white_check_mark: <@{target}> 운영자로 추가됨.", response_type="ephemeral")
        else:
            respond(text=f":information_source: <@{target}> 이미 운영자입니다.", response_type="ephemeral")

    @app.command("/어드민-삭제")
    def handle_admin_remove(ack: Ack, body: dict, respond: Respond) -> None:
        ack()
        if not admin_service.is_admin(body["user_id"]):
            guards.reject_non_admin(respond)
            return
        target = _parse_slack_user(body.get("text", "") or "")
        if target is None:
            respond(
                text="형식: `/어드민-삭제 @사용자`",
                response_type="ephemeral",
            )
            return
        with session(DB_PATH) as conn:
            ok, reason = admin_service.remove_admin(conn, target)
        if ok:
            respond(text=f":wastebasket: <@{target}> 운영자 권한 해제됨.", response_type="ephemeral")
        else:
            respond(text=f":no_entry_sign: 제거 실패: {reason}", response_type="ephemeral")

    @app.command("/어드민-목록")
    def handle_admin_list(ack: Ack, body: dict, respond: Respond) -> None:
        ack()
        if not admin_service.is_admin(body["user_id"]):
            guards.reject_non_admin(respond)
            return
        rows = admin_service.list_admins()
        if not rows:
            respond(text=":busts_in_silhouette: 운영자 없음 (DB 부트스트랩 실패?).", response_type="ephemeral")
            return
        lines = [":busts_in_silhouette: *현재 운영자 목록*"]
        for r in rows:
            tag = " :star: (primary)" if r["is_primary"] else ""
            lines.append(f"• <@{r['slack_user_id']}>{tag}  _added {r['added_at']}_")
        respond(text="\n".join(lines), response_type="ephemeral")
