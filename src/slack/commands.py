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
    member_service,
    notification_service,
    schedule_service,
)
from . import flows, guards, messages

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

        notification_service.announce_new_cycle(client, schedules, new_cid)
        respond(
            text=(
                f":white_check_mark: 재추첨 완료 — cycle {new_cid}, "
                f"활성 멤버 {len(active)}명 기반 {len(schedules)}주 일정. 채널에 공지됨."
            ),
            response_type="ephemeral",
        )

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
