"""Slack 핸들러를 가로지르는 비즈니스 흐름.

도메인 = agent boundary 원칙으로 슬랙 어댑터에서 도메인 호출을 직접 하지 않고
이 모듈이 (1) DB, (2) LLM, (3) Slack client 를 조합한다.
"""
from __future__ import annotations

import json
import logging
from datetime import date

from slack_sdk import WebClient

from .. import llm_service
from ..config import ADMIN_JJR, CHANNEL_ID, DB_PATH
from ..db import session
from ..models import Preferences
from ..services import (
    admin_service,
    defer_service,
    draft_service,
    file_storage,
    ingestion_service,
    member_service,
    preference_service,
    schedule_service,
    submission_service,
    vector_service,
)


def _primary_admin() -> str:
    """현재 primary admin slack id (DB 우선, 없으면 env config 폴백)."""
    return admin_service.get_primary_admin_id() or ADMIN_JJR


# ─────────────────────────────────────────────────────────────
# /제출 흐름 — 모달 → 파일 다운로드 → 백그라운드 ingestion → 채널 공지
# ─────────────────────────────────────────────────────────────
def process_submission_async(
    client: WebClient,
    *,
    slack_user_id: str,
    presenter: str,
    seminar_date,                   # date
    file_id: str,
    title_hint: str,
) -> None:
    """별도 thread에서 호출됨. Slack ack는 호출자가 이미 처리.
    실패해도 raise하지 말고 사용자에게 DM으로 알림."""
    dm_channel = open_dm(client, slack_user_id)

    # 1) 파일 다운로드
    try:
        client.chat_postMessage(channel=dm_channel, text=":hourglass_flowing_sand: 자료 다운로드 중...")
        file_path, original_name = file_storage.download_slack_file(
            client, file_id=file_id, presenter=presenter, seminar_date=seminar_date,
        )
    except Exception as e:
        log.exception("submission 파일 다운로드 실패")
        client.chat_postMessage(channel=dm_channel, text=f":x: 다운로드 실패: {e}")
        return

    # 2) DB row 생성 + 처리
    submission_id: int | None = None
    try:
        with session(DB_PATH) as conn:
            submission_id = submission_service.create_pending(
                conn,
                presenter=presenter,
                seminar_date=seminar_date,
                file_path=str(file_path),
                file_name=original_name,
                slack_file_id=file_id,
            )

        client.chat_postMessage(
            channel=dm_channel,
            text=":mag: VLM 분석 + 지식 추출 시작 (페이지 수에 따라 1-3분 소요).",
        )

        with session(DB_PATH) as conn:
            result = ingestion_service.ingest_submission(conn, submission_id, title_hint=title_hint)

    except Exception as e:
        log.exception("submission %s ingestion 실패", submission_id)
        if submission_id is not None:
            with session(DB_PATH) as conn:
                submission_service.mark_failed(conn, submission_id, str(e))
        client.chat_postMessage(channel=dm_channel, text=f":x: 처리 실패: {e}")
        return

    # 3) 사용자 DM 완료
    title = result.get("title") or original_name
    summary = result.get("summary") or ""
    page_count = result.get("page_count", 0)
    tag_text = ", ".join(f"`{t}`" for t in (result.get("tags") or [])[:8])
    entity_count = len(result.get("entities") or [])

    client.chat_postMessage(
        channel=dm_channel,
        text=(
            f":white_check_mark: *처리 완료* — _{title}_ ({page_count}p)\n"
            f"태그: {tag_text or '없음'}  |  엔티티 {entity_count}개 추출\n"
            f"이제 멤버들이 봇 DM에 자료 관련 질문을 던지면 답변할 수 있습니다."
        ),
    )

    # 4) 채널 공지
    announce_channel_submission(
        client, presenter=presenter, seminar_date=seminar_date,
        title=title, summary=summary, tags=result.get("tags") or [],
        page_count=page_count, slack_file_id=file_id, submission_id=submission_id,
    )


def announce_channel_submission(
    client: WebClient,
    *,
    presenter: str,
    seminar_date,
    title: str,
    summary: str,
    tags: list[str],
    page_count: int,
    slack_file_id: str,
    submission_id: int,
) -> None:
    """ingest 완료 직후 채널에 자료 공지."""
    tag_text = " ".join(f"`{t}`" for t in tags[:6]) if tags else ""
    text_lines = [
        f":books: *{presenter}*님 발표 자료 제출 — *{seminar_date.isoformat()} (목)*",
        f"*{title}* ({page_count}쪽)",
    ]
    if summary:
        text_lines.append("")
        text_lines.append(f"> {summary}")
    if tag_text:
        text_lines.append("")
        text_lines.append(tag_text)
    text_lines.append("")
    text_lines.append(":speech_balloon: 봇 DM에 자료 관련 질문하시면 답변합니다.")

    resp = client.chat_postMessage(channel=CHANNEL_ID, text="\n".join(text_lines))
    # 슬랙이 file_id 첨부를 채널에 가시화: file_remote 또는 share. 파일 자체는 발표자가 채널에 공유하면 됨.
    # 우리 봇이 첨부 재공유하려면 files.share API + private file conversion 필요. 여기선 텍스트만.

    with session(DB_PATH) as conn:
        submission_service.set_announce_ts(conn, submission_id, resp["ts"])
from . import messages

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# DM 채널 열기
# ─────────────────────────────────────────────────────────────
def open_dm(client: WebClient, slack_user_id: str) -> str:
    resp = client.conversations_open(users=slack_user_id)
    return resp["channel"]["id"]


# ─────────────────────────────────────────────────────────────
# /세미나-연기 시작
# ─────────────────────────────────────────────────────────────
def start_defer(
    client: WebClient, *, slack_user_id: str, initial_text: str, today: date
) -> str:
    """슬래시 커맨드 진입점. 검증 통과 시 DM 열고 draft 만들고 첫 메시지 보냄.
    슬래시 커맨드에 줄 ephemeral 응답을 반환."""
    with session(DB_PATH) as conn:
        member = member_service.get_by_slack_id(conn, slack_user_id)
        if member is None:
            return ":no_entry_sign: 발표 멤버가 아니어서 연기 신청을 받을 수 없습니다."

        assignment = defer_service.find_requester_assignment(conn, slack_user_id, today)
        if assignment is None:
            return f":information_source: {member.name}님께 다가올 발표 일정이 없습니다."
        assigned_date, requester_name = assignment

        ok, err = defer_service.can_request(today, assigned_date)
        if not ok:
            deadline = defer_service.deadline_for(assigned_date)
            return (
                f":no_entry_sign: 연기 신청 마감({deadline.isoformat()})이 지났습니다. "
                f"운영자(<@{_primary_admin()}>)에게 직접 문의해주세요."
            )

        # 기존 active draft 있으면 취소하고 새로 시작
        existing = draft_service.get_active(conn, slack_user_id, "defer")
        if existing is not None:
            draft_service.cancel(conn, existing.id)

        dm_channel = open_dm(client, slack_user_id)
        draft = draft_service.create(
            conn, kind="defer", slack_user_id=slack_user_id, dm_channel_id=dm_channel
        )

        # 첫 인사 DM
        deadline = defer_service.deadline_for(assigned_date)
        kickoff = messages.defer_kickoff(requester_name, assigned_date, deadline, initial_text)
        client.chat_postMessage(channel=dm_channel, text=kickoff)

        # initial_text 가 비어있지 않으면 LLM에 첫 turn 태움
        if initial_text.strip():
            _process_defer_turn(
                client, conn, draft.id, user_message=initial_text.strip(),
                requester_name=requester_name, assigned_date=assigned_date, today=today,
            )

    return ":envelope_with_arrow: DM으로 진행해드릴게요. 슬랙 좌측 'Apps' → 봇 DM 확인 부탁드립니다."


# ─────────────────────────────────────────────────────────────
# /세미나-선호도 시작
# ─────────────────────────────────────────────────────────────
def start_preference(
    client: WebClient, *, slack_user_id: str, initial_text: str
) -> str:
    with session(DB_PATH) as conn:
        member = member_service.get_by_slack_id(conn, slack_user_id)
        if member is None:
            return ":no_entry_sign: 발표 멤버가 아니어서 선호도 등록 대상이 아닙니다."

        existing = draft_service.get_active(conn, slack_user_id, "preference")
        if existing is not None:
            draft_service.cancel(conn, existing.id)

        dm_channel = open_dm(client, slack_user_id)
        draft = draft_service.create(
            conn, kind="preference", slack_user_id=slack_user_id, dm_channel_id=dm_channel
        )
        prefs = preference_service.get(conn, member.name)
        client.chat_postMessage(
            channel=dm_channel,
            text=messages.preference_kickoff(member.name, _summarize_prefs(prefs)),
        )
        if initial_text.strip():
            _process_preference_turn(
                client, conn, draft.id, user_message=initial_text.strip(),
                member_name=member.name,
            )
    return ":envelope_with_arrow: DM으로 진행해드릴게요."


# ─────────────────────────────────────────────────────────────
# DM 메시지 이벤트 핸들링
# ─────────────────────────────────────────────────────────────
def handle_dm_message(
    client: WebClient, *, slack_user_id: str, channel: str, text: str, today: date
) -> None:
    text = text.strip()
    if not text:
        return
    with session(DB_PATH) as conn:
        draft = draft_service.get_active_any_kind(conn, slack_user_id)
        if draft is not None:
            if draft.status == "awaiting_confirm":
                # 확인 대기 상태에서 다시 메시지 → 자동으로 수정 모드로 돌림
                draft_service.reset_to_active(conn, draft.id)
                draft = draft_service.get_by_id(conn, draft.id)  # type: ignore[assignment]

            if draft.kind == "defer":
                assignment = defer_service.find_requester_assignment(conn, slack_user_id, today)
                if assignment is None:
                    client.chat_postMessage(channel=channel, text="배정된 발표 일정을 찾지 못했습니다.")
                    return
                assigned_date, requester_name = assignment
                _process_defer_turn(
                    client, conn, draft.id, user_message=text,
                    requester_name=requester_name, assigned_date=assigned_date, today=today,
                )
            elif draft.kind == "preference":
                member = member_service.get_by_slack_id(conn, slack_user_id)
                if member is None:
                    return
                _process_preference_turn(
                    client, conn, draft.id, user_message=text, member_name=member.name,
                )
            return

        # ─── active draft 없음 → 의도 라우팅 ───
        member = member_service.get_by_slack_id(conn, slack_user_id)
        if member is None:
            client.chat_postMessage(
                channel=channel,
                text="발표 멤버가 아니어서 응답이 어렵습니다. 운영자에게 문의해주세요.",
            )
            return
        intent = llm_service.classify_intent(text)
        log.info("DM router → intent=%s for user=%s text=%r", intent.intent, slack_user_id, text[:80])

        if intent.intent == "defer":
            _begin_defer_in_dm(client, conn, slack_user_id, channel, text, today)
        elif intent.intent == "preference":
            _begin_preference_in_dm(client, conn, slack_user_id, channel, text)
        elif intent.intent == "schedule_question":
            _answer_schedule(client, conn, slack_user_id, channel, text, today)
        elif intent.intent == "material_question":
            _answer_material(client, channel, text)
        else:
            reply = intent.fallback_reply or (
                "어떤 도움이 필요하신가요? 발표 연기, 선호도 등록, 일정 조회, 자료 질문 같은 걸 도와드려요."
            )
            client.chat_postMessage(channel=channel, text=reply)


# ─────────────────────────────────────────────────────────────
# DM 안에서 직접 시작 (slash command 거치지 않음)
# ─────────────────────────────────────────────────────────────
def _begin_defer_in_dm(
    client: WebClient, conn, slack_user_id: str, dm_channel: str, text: str, today: date
) -> None:
    assignment = defer_service.find_requester_assignment(conn, slack_user_id, today)
    if assignment is None:
        client.chat_postMessage(channel=dm_channel, text=":information_source: 다가올 발표 일정이 없어 연기 신청 대상이 아닙니다.")
        return
    assigned_date, requester_name = assignment
    ok, _ = defer_service.can_request(today, assigned_date)
    if not ok:
        deadline = defer_service.deadline_for(assigned_date)
        client.chat_postMessage(
            channel=dm_channel,
            text=f":no_entry_sign: 연기 신청 마감({deadline.isoformat()})이 지났습니다. 운영자(<@{ADMIN_JJR}>)에게 직접 문의해주세요.",
        )
        return

    existing = draft_service.get_active(conn, slack_user_id, "defer")
    if existing is not None:
        draft_service.cancel(conn, existing.id)
    draft = draft_service.create(conn, kind="defer", slack_user_id=slack_user_id, dm_channel_id=dm_channel)
    _process_defer_turn(
        client, conn, draft.id, user_message=text,
        requester_name=requester_name, assigned_date=assigned_date, today=today,
    )


def _begin_preference_in_dm(
    client: WebClient, conn, slack_user_id: str, dm_channel: str, text: str
) -> None:
    member = member_service.get_by_slack_id(conn, slack_user_id)
    if member is None:
        return
    existing = draft_service.get_active(conn, slack_user_id, "preference")
    if existing is not None:
        draft_service.cancel(conn, existing.id)
    draft = draft_service.create(conn, kind="preference", slack_user_id=slack_user_id, dm_channel_id=dm_channel)
    _process_preference_turn(
        client, conn, draft.id, user_message=text, member_name=member.name,
    )


def _answer_schedule(
    client: WebClient, conn, slack_user_id: str, dm_channel: str, text: str, today: date
) -> None:
    member = member_service.get_by_slack_id(conn, slack_user_id)
    upcoming = schedule_service.get_upcoming(conn, today=today, limit=10)

    schedule_text = "\n".join([
        f"- {s.date.isoformat()} ({messages.WEEKDAY_KO[s.date.weekday()]}): "
        f"1부 {s.slot_1 or '미정'} / 2부 {s.slot_2 or '미정'}"
        for s in upcoming
    ]) or "(일정 없음)"

    user_assignment = "없음"
    if member is not None:
        for s in upcoming:
            if s.slot_1 == member.name:
                user_assignment = f"{s.date.isoformat()} 1부"
                break
            if s.slot_2 == member.name:
                user_assignment = f"{s.date.isoformat()} 2부"
                break

    answer = llm_service.answer_schedule_question(
        member_name=member.name if member else "비멤버",
        today=today.isoformat(),
        schedule_text=schedule_text,
        user_assignment=user_assignment,
        user_message=text,
    )
    if answer:
        client.chat_postMessage(channel=dm_channel, text=answer)


def _answer_material(client: WebClient, dm_channel: str, text: str) -> None:
    """RAG: Weaviate 검색 → LLM 합성 → DM 회신."""
    try:
        retrieved = vector_service.search(text, limit=5)
    except Exception as e:
        log.exception("vector search 실패")
        client.chat_postMessage(
            channel=dm_channel,
            text=f":warning: 자료 검색 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요. ({e})",
        )
        return

    if not retrieved:
        client.chat_postMessage(
            channel=dm_channel,
            text=":books: 아직 인입된 발표 자료가 없거나 관련된 자료를 찾지 못했습니다.",
        )
        return

    answer = llm_service.synthesize_rag_answer(user_question=text, retrieved=retrieved)
    if answer:
        client.chat_postMessage(channel=dm_channel, text=answer)


# ─────────────────────────────────────────────────────────────
# LLM turn dispatch
# ─────────────────────────────────────────────────────────────
def _summarize_prefs(p: Preferences) -> str:
    parts: list[str] = []
    if p.avoid_dates:
        parts.append(f"회피 날짜 {','.join(p.avoid_dates)}")
    if p.avoid_weeks_of_month:
        parts.append(f"회피 주차 {','.join(map(str, p.avoid_weeks_of_month))}")
    if p.preferred_slot:
        parts.append(f"선호 {p.preferred_slot}부")
    return "없음" if not parts else " / ".join(parts)


def _process_defer_turn(
    client: WebClient, conn, draft_id: int, *,
    user_message: str, requester_name: str, assigned_date: date, today: date,
) -> None:
    draft = draft_service.get_by_id(conn, draft_id)
    if draft is None:
        return
    member = member_service.get_by_name(conn, requester_name)
    prefs_summary = _summarize_prefs(member.preferences) if member else "없음"
    deadline = defer_service.deadline_for(assigned_date)
    sysprompt = llm_service.defer_system_prompt(
        requester_name=requester_name,
        assigned_date=assigned_date.isoformat(),
        deadline=deadline.isoformat(),
        today=today.isoformat(),
        prior_prefs=prefs_summary,
    )
    turn = llm_service.chat_turn(
        system_prompt=sysprompt,
        history=draft.messages,
        user_message=user_message,
        tools=[llm_service.SUBMIT_DEFER_TOOL],
    )
    draft_service.update_messages(conn, draft.id, turn.new_messages)

    if turn.tool_name == "submit_defer":
        draft_service.set_pending(conn, draft.id, turn.tool_payload or {})
        blocks = messages.defer_preview_blocks(
            draft_id=draft.id, payload=turn.tool_payload or {}, assigned=assigned_date,
        )
        client.chat_postMessage(
            channel=draft.dm_channel_id, blocks=blocks, text="연기 신청 미리보기",
        )
    elif turn.text:
        client.chat_postMessage(channel=draft.dm_channel_id, text=turn.text)


def _process_preference_turn(
    client: WebClient, conn, draft_id: int, *, user_message: str, member_name: str,
) -> None:
    draft = draft_service.get_by_id(conn, draft_id)
    if draft is None:
        return
    current = preference_service.get(conn, member_name)
    sysprompt = llm_service.preferences_system_prompt(
        member_name=member_name, current_prefs=_summarize_prefs(current),
    )
    turn = llm_service.chat_turn(
        system_prompt=sysprompt,
        history=draft.messages,
        user_message=user_message,
        tools=[llm_service.SUBMIT_PREFERENCES_TOOL],
    )
    draft_service.update_messages(conn, draft.id, turn.new_messages)

    if turn.tool_name == "submit_preferences":
        draft_service.set_pending(conn, draft.id, turn.tool_payload or {})
        blocks = messages.preference_preview_blocks(
            draft_id=draft.id, payload=turn.tool_payload or {},
        )
        client.chat_postMessage(
            channel=draft.dm_channel_id, blocks=blocks, text="선호도 미리보기",
        )
    elif turn.text:
        client.chat_postMessage(channel=draft.dm_channel_id, text=turn.text)


# ─────────────────────────────────────────────────────────────
# 사용자 confirm — defer
# ─────────────────────────────────────────────────────────────
def confirm_defer(client: WebClient, *, draft_id: int, slack_user_id: str, today: date) -> None:
    with session(DB_PATH) as conn:
        draft = draft_service.get_by_id(conn, draft_id)
        if draft is None or draft.slack_user_id != slack_user_id:
            return
        if draft.status != "awaiting_confirm" or draft.kind != "defer":
            return
        if draft.pending_payload is None:
            return

        assignment = defer_service.find_requester_assignment(conn, slack_user_id, today)
        if assignment is None:
            client.chat_postMessage(channel=draft.dm_channel_id, text="배정된 일정을 못 찾았습니다.")
            return
        assigned_date, requester_name = assignment

        defer_id = defer_service.create(
            conn,
            requester=requester_name,
            original_date=assigned_date,
            reason=draft.pending_payload.get("reason", ""),
            hints={
                "preferred_replacement_dates": draft.pending_payload.get("preferred_replacement_dates") or [],
                "additional_avoid_dates": draft.pending_payload.get("additional_avoid_dates") or [],
            },
        )
        draft_service.mark_submitted(conn, draft.id)

        # 후보 픽 + 양쪽에 DM
        _send_approval_dms(client, conn, defer_id)

    client.chat_postMessage(
        channel=draft.dm_channel_id,
        text=":white_check_mark: 신청 완료. 진재님 + 대체자 후보에게 승인 요청을 보냈습니다.",
    )


def revise_defer(client: WebClient, *, draft_id: int, slack_user_id: str) -> None:
    with session(DB_PATH) as conn:
        draft = draft_service.get_by_id(conn, draft_id)
        if draft is None or draft.slack_user_id != slack_user_id:
            return
        draft_service.reset_to_active(conn, draft.id)
        client.chat_postMessage(channel=draft.dm_channel_id, text="수정하실 내용을 알려주세요.")


def cancel_defer(client: WebClient, *, draft_id: int, slack_user_id: str) -> None:
    with session(DB_PATH) as conn:
        draft = draft_service.get_by_id(conn, draft_id)
        if draft is None or draft.slack_user_id != slack_user_id:
            return
        draft_service.cancel(conn, draft.id)
        client.chat_postMessage(channel=draft.dm_channel_id, text=":wave: 신청 취소했습니다.")


# ─────────────────────────────────────────────────────────────
# 사용자 confirm — preference
# ─────────────────────────────────────────────────────────────
def confirm_preference(client: WebClient, *, draft_id: int, slack_user_id: str) -> None:
    with session(DB_PATH) as conn:
        draft = draft_service.get_by_id(conn, draft_id)
        if draft is None or draft.slack_user_id != slack_user_id:
            return
        if draft.status != "awaiting_confirm" or draft.kind != "preference":
            return
        member = member_service.get_by_slack_id(conn, slack_user_id)
        if member is None or draft.pending_payload is None:
            return
        new_prefs = Preferences(
            avoid_dates=list(draft.pending_payload.get("avoid_dates") or []),
            avoid_weeks_of_month=list(draft.pending_payload.get("avoid_weeks_of_month") or []),
            preferred_slot=draft.pending_payload.get("preferred_slot"),
        )
        preference_service.save(conn, member.name, new_prefs)
        draft_service.mark_submitted(conn, draft.id)
    client.chat_postMessage(
        channel=draft.dm_channel_id,
        text=":white_check_mark: 선호도 저장 완료. 다음 추첨/대체자 선정 때 자동 반영됩니다.",
    )


def revise_preference(client: WebClient, *, draft_id: int, slack_user_id: str) -> None:
    with session(DB_PATH) as conn:
        draft = draft_service.get_by_id(conn, draft_id)
        if draft is None or draft.slack_user_id != slack_user_id:
            return
        draft_service.reset_to_active(conn, draft.id)
        client.chat_postMessage(channel=draft.dm_channel_id, text="수정하실 내용을 알려주세요.")


def cancel_preference(client: WebClient, *, draft_id: int, slack_user_id: str) -> None:
    with session(DB_PATH) as conn:
        draft = draft_service.get_by_id(conn, draft_id)
        if draft is None or draft.slack_user_id != slack_user_id:
            return
        draft_service.cancel(conn, draft.id)
        client.chat_postMessage(channel=draft.dm_channel_id, text=":wave: 등록 취소했습니다.")


# ─────────────────────────────────────────────────────────────
# 승인 DM 발송 (진재 + 대체자)
# ─────────────────────────────────────────────────────────────
def _send_approval_dms(client: WebClient, conn, defer_id: int) -> None:
    candidate = defer_service.select_next_replacement(conn, defer_id)
    if candidate is None:
        defer_service.mark_escalated(conn, defer_id)
        d = defer_service.get(conn, defer_id)
        client.chat_postMessage(channel=open_dm(client, _primary_admin()), text=messages.escalation_dm())
        # 신청자에게도 안내
        requester_member = member_service.get_by_name(conn, d.requester)
        if requester_member:
            client.chat_postMessage(
                channel=open_dm(client, requester_member.slack_user_id),
                text=":sos: 가능한 대체자가 없어 진재님 수동 처리로 넘어갔습니다.",
            )
        return

    defer_service.assign_replacement(conn, defer_id, candidate.name)
    d = defer_service.get(conn, defer_id)

    # 진재님 DM
    jjr_dm = open_dm(client, _primary_admin())
    client.chat_postMessage(
        channel=jjr_dm,
        text=f"연기 신청 검토 — {d.requester} → {candidate.name}",
        blocks=messages.jjr_approval_blocks(
            defer_id=defer_id,
            requester=d.requester,
            original_date=d.original_date,
            reason=d.reason,
            proposed_replacement=candidate.name,
            hints=d.hints,
            attempts=d.attempts,
        ),
    )

    # 대체자 DM
    rep_dm = open_dm(client, candidate.slack_user_id)
    client.chat_postMessage(
        channel=rep_dm,
        text=f"대체 발표 부탁 — {d.original_date.isoformat()}",
        blocks=messages.replacement_request_blocks(
            defer_id=defer_id,
            requester=d.requester,
            original_date=d.original_date,
            reason=d.reason,
        ),
    )


# ─────────────────────────────────────────────────────────────
# 운영자 / 대체자 액션
# ─────────────────────────────────────────────────────────────
def on_jjr_approve(client: WebClient, *, defer_id: int) -> None:
    with session(DB_PATH) as conn:
        defer_service.record_jjr_approval(conn, defer_id)
        d = defer_service.get(conn, defer_id)
        if defer_service.is_fully_approved(d):
            _finalize(client, conn, defer_id)


def on_jjr_reject(client: WebClient, *, defer_id: int) -> None:
    with session(DB_PATH) as conn:
        d = defer_service.get(conn, defer_id)
        defer_service.mark_rejected_by_jjr(conn, defer_id)
        # 신청자에게 통지
        requester_member = member_service.get_by_name(conn, d.requester)
        if requester_member:
            client.chat_postMessage(
                channel=open_dm(client, requester_member.slack_user_id),
                text=messages.jjr_rejection_dm(requester=d.requester, original_date=d.original_date),
            )


def on_replacement_accept(client: WebClient, *, defer_id: int) -> None:
    with session(DB_PATH) as conn:
        defer_service.record_replacement_approval(conn, defer_id)
        d = defer_service.get(conn, defer_id)
        if defer_service.is_fully_approved(d):
            _finalize(client, conn, defer_id)


def on_replacement_decline(client: WebClient, *, defer_id: int) -> None:
    with session(DB_PATH) as conn:
        defer_service.record_replacement_rejection(conn, defer_id)
        d = defer_service.get(conn, defer_id)
        if defer_service.is_escalation_needed(d):
            defer_service.mark_escalated(conn, defer_id)
            client.chat_postMessage(channel=open_dm(client, _primary_admin()), text=messages.escalation_dm())
            return
        # 진재 승인은 이미 받았더라도 대체자가 바뀌었으므로 진재 승인 무효화
        with conn:
            conn.execute(
                "UPDATE defer_requests SET jjr_approved = NULL WHERE id = ?",
                (defer_id,),
            )
        _send_approval_dms(client, conn, defer_id)


# ─────────────────────────────────────────────────────────────
# Finalize
# ─────────────────────────────────────────────────────────────
def _finalize(client: WebClient, conn, defer_id: int) -> None:
    d = defer_service.finalize_approval(conn, defer_id)
    requester_member = member_service.get_by_name(conn, d.requester)
    rep_member = member_service.get_by_name(conn, d.replacement) if d.replacement else None

    # 채널 공지
    client.chat_postMessage(
        channel=CHANNEL_ID,
        text=messages.channel_announcement(
            requester=d.requester, replacement=d.replacement or "",
            original_date=d.original_date,
        ),
    )
    if requester_member:
        client.chat_postMessage(
            channel=open_dm(client, requester_member.slack_user_id),
            text=messages.requester_done_dm(replacement=d.replacement or "", original_date=d.original_date),
        )
    if rep_member:
        client.chat_postMessage(
            channel=open_dm(client, rep_member.slack_user_id),
            text=messages.replacement_thanks_dm(original_date=d.original_date, requester=d.requester),
        )
    log.info("defer %d finalized: %s -> %s on %s", defer_id, d.requester, d.replacement, d.original_date)
