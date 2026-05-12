"""사용자 DM 에이전트 — 도구 노출하고 LLM이 직접 선택해서 호출.

handle_dm_message 가 active draft 없는 케이스에 이걸 호출. agent.run() 은:
  1) 사용자 메시지로 벡터 검색 (자료 컨텍스트)
  2) 시스템 프롬프트 + 대화 history + 사용자 메시지 + tools 로 LLM 1회
  3) LLM이 tool 호출하면 dispatcher 가 실행 (각 tool은 권한 검사 + 액션 + DM 응답)
  4) 텍스트만 반환하면 그대로 DM 으로 발송

새 기능 추가 = TOOLS 에 한 줄 + _dispatch 에 한 분기. 명시적 의도 분류 X.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime
from typing import Any

from openai import OpenAI
from slack_sdk import WebClient

from .config import LLM_API_BASE_URL, LLM_API_KEY, LLM_MODEL
from .services import (
    admin_service,
    conversation_service,
    defer_service,
    member_service,
    schedule_service,
    vector_service,
)
from . import llm_service

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# Tool 스키마
# ─────────────────────────────────────────────────────────────
TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "set_topic",
            "description": (
                "다가올 세미나 발표자의 토픽 등록/수정. "
                "target_presenter 가 호출자 본인이거나 null이면 본인 토픽. "
                "다른 사람 이름이면 운영자만 가능 (호출자 권한은 시스템 프롬프트에 명시됨)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target_presenter": {
                        "type": ["string", "null"],
                        "description": "한국어 이름 (예: '허성환'). 본인이면 null.",
                    },
                    "topic": {
                        "type": "string",
                        "description": "토픽 본문 한 줄 (예: 'LLM agent ReAct vs Reflexion 비교')",
                    },
                },
                "required": ["target_presenter", "topic"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_seminar_note",
            "description": "특정 세미나 회차의 운영 안내 (장소/시간 변경, 회식, 특이사항). 운영자만 가능.",
            "parameters": {
                "type": "object",
                "properties": {
                    "target_date": {
                        "type": ["string", "null"],
                        "description": "YYYY-MM-DD 형식. 명시 안 됐으면 null (가장 가까운 회차 사용)",
                    },
                    "notes": {
                        "type": "string",
                        "description": "안내 본문. 빈 문자열이면 기존 안내 삭제.",
                    },
                },
                "required": ["target_date", "notes"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "answer_schedule_question",
            "description": "다가올 일정 / 발표자 자체에 대한 단순 조회 (예: '내 차례 언제?', '5/21 누구야?').",
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "사용자 원본 질문"},
                },
                "required": ["question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "answer_material_question",
            "description": "발표 자료(인입된 PDF) 내용에 대한 질문 — RAG로 답변.",
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "사용자 원본 질문"},
                },
                "required": ["question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "start_defer_flow",
            "description": "사용자가 자기 발표를 연기하고 싶을 때. 멀티턴 대화 시작 (draft 생성).",
            "parameters": {
                "type": "object",
                "properties": {
                    "initial_text": {"type": "string", "description": "사용자 메시지 그대로"},
                },
                "required": ["initial_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "start_preference_flow",
            "description": "사용자가 평상시 발표 선호도(회피 날짜/주차/슬롯) 등록 원할 때. 멀티턴 시작.",
            "parameters": {
                "type": "object",
                "properties": {
                    "initial_text": {"type": "string", "description": "사용자 메시지 그대로"},
                },
                "required": ["initial_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_message",
            "description": "위 도구 어디에도 해당 안 되는 경우 사용자에게 일반 텍스트 응답 (인사, 모호한 질문 안내, 거절, 잡담 등).",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "사용자에게 보낼 메시지 (한국어, 짧고 친근하게)"},
                },
                "required": ["text"],
            },
        },
    },
]


# ─────────────────────────────────────────────────────────────
# System prompt
# ─────────────────────────────────────────────────────────────
def _system_prompt(
    *,
    caller_name: str,
    caller_role: str,           # 'admin' / 'member' / 'unknown'
    today_iso: str,
    upcoming_text: str,
    hits_text: str,
) -> str:
    return f"""당신은 Genon AI 사내 주간 세미나(매주 목 14:00) 운영 봇이다.
슬랙 DM 으로 한 메시지마다 *반드시 정확히 한 개의 tool* 을 호출해 처리한다.

# 호출자 정보
이름: {caller_name}
역할: {caller_role}    (admin=운영자, member=발표 풀 멤버, bystander=외부 청중)
오늘: {today_iso}

bystander 는 *읽기(answer_schedule_question / answer_material_question / send_message)*만 가능.
mutation 요청 (set_topic / set_seminar_note / start_defer_flow / start_preference_flow) 시도하면
send_message 로 정중히 거절 (예: "발표 멤버만 가능한 기능이에요").

# 도구 선택 가이드
- 본인 발표 토픽 알려옴 → set_topic(target_presenter=null, topic=…)
- 다른 발표자의 토픽 지정 ('허성환은 X', 'X님 토픽 Y' 등) → set_topic(target_presenter='이름', topic=…)
   - 단, 호출자가 admin 이 아니면 send_message 로 정중히 거절
- 회차 운영 안내 ('5/14 회의실 B', '이번주 휴무' 등) → set_seminar_note (admin 만)
   - admin 아니면 send_message 로 거절
- 일정/발표자 조회 → answer_schedule_question
- 발표 자료 내용 질문 (검색 결과와 관련) → answer_material_question
- 본인 발표 연기 의사 → start_defer_flow
- 본인 평상시 선호도 → start_preference_flow
- 인사/잡담/그 외 → send_message

# 대화 history 활용 (중요)
직전 봇 발화가 토픽/노트를 물어봤다면 사용자 짧은 답을 그에 대한 응답으로 해석.
예) 봇이 '허성환 토픽 미등록' → 사용자 'pydanticAI' → set_topic(target=null, topic='pydanticAI')
예) 봇이 일정 조회 답함 → 사용자 '허성환은 pydanticAI한데' → admin 이면 set_topic(target='허성환', topic='pydanticAI')

# 다가올 일정
{upcoming_text}

# 사용자 메시지 기반 자료 검색 미리보기
{hits_text}

# 응답 규칙
- 매번 *정확히 하나*의 tool 호출. content 텍스트만 반환 금지.
- send_message 의 text 는 한국어로 1-3문장, 친근하게.
- 데이터에 없는 내용은 모른다고 답하고 추측 금지.
"""


# ─────────────────────────────────────────────────────────────
# 엔트리포인트
# ─────────────────────────────────────────────────────────────
def run(
    client: WebClient,
    conn,
    *,
    slack_user_id: str,
    dm_channel: str,
    user_message: str,
    history: list[dict[str, Any]],
    today: date,
) -> None:
    caller_member = member_service.get_by_slack_id(conn, slack_user_id)
    is_admin = admin_service.is_admin(slack_user_id)

    caller_name = caller_member.name if caller_member else f"<@{slack_user_id}>"
    # bystander = 발표 멤버도 운영자도 아닌 외부 청중 (다른 채널에서 봇 만난 사람)
    # 읽기(일정/자료 조회) 허용, 쓰기(토픽/노트/연기 등) 차단
    if is_admin:
        caller_role = "admin"
    elif caller_member:
        caller_role = "member"
    else:
        caller_role = "bystander"

    # 사용자 메시지로 사전 검색 → 자료 컨텍스트
    try:
        hits = vector_service.search(user_message, limit=5)
    except Exception as e:
        log.warning("agent: vector search 실패 무시 (%s)", e)
        hits = []
    hits_text = _format_hits(hits)

    upcoming = schedule_service.get_upcoming(conn, today=today, limit=5)
    upcoming_text = "\n".join(
        f"  - {s.date.isoformat()}: 1부 {s.slot_1 or '미정'} / 2부 {s.slot_2 or '미정'}"
        + (f" / 토픽 1부 {s.slot_1_topic!r}" if s.slot_1_topic else "")
        + (f" / 토픽 2부 {s.slot_2_topic!r}" if s.slot_2_topic else "")
        for s in upcoming
    ) or "  (없음)"

    sys = _system_prompt(
        caller_name=caller_name, caller_role=caller_role,
        today_iso=today.isoformat(),
        upcoming_text=upcoming_text, hits_text=hits_text,
    )

    msgs: list[dict[str, Any]] = [{"role": "system", "content": sys}, *history,
                                   {"role": "user", "content": user_message}]

    if not LLM_API_KEY:
        log.error("LLM_API_KEY 미설정")
        _say(client, conn, slack_user_id, dm_channel, ":x: LLM 설정 오류 (운영자 확인 필요).")
        return

    oai = OpenAI(api_key=LLM_API_KEY, base_url=LLM_API_BASE_URL)
    try:
        resp = oai.chat.completions.create(
            model=LLM_MODEL, messages=msgs, tools=TOOLS, tool_choice="auto",
            temperature=0.2,
        )
    except Exception as e:
        log.exception("agent LLM 호출 실패")
        _say(client, conn, slack_user_id, dm_channel, f":x: 처리 중 오류 ({e})")
        return

    msg = resp.choices[0].message
    log.info("agent → user=%s tool_calls=%s text=%r",
             slack_user_id,
             [tc.function.name for tc in (msg.tool_calls or [])],
             (msg.content or "")[:80])

    if msg.tool_calls:
        for call in msg.tool_calls:
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                log.warning("tool args JSON 파싱 실패: %s", call.function.arguments)
                args = {}
            _dispatch(
                client, conn,
                tool_name=call.function.name, args=args,
                slack_user_id=slack_user_id, dm_channel=dm_channel,
                caller_member=caller_member, is_admin=is_admin,
                hits=hits, today=today,
            )
        return

    # tool 호출 없이 텍스트만 → 그대로 발송 (안전망)
    if msg.content:
        _say(client, conn, slack_user_id, dm_channel, msg.content.strip())


# ─────────────────────────────────────────────────────────────
# Dispatcher
# ─────────────────────────────────────────────────────────────
MUTATION_TOOLS = {"set_topic", "set_seminar_note", "start_defer_flow", "start_preference_flow"}


def _dispatch(
    client: WebClient, conn,
    *,
    tool_name: str, args: dict[str, Any],
    slack_user_id: str, dm_channel: str,
    caller_member, is_admin: bool,
    hits: list[dict[str, Any]], today: date,
) -> None:
    # bystander 가드: 읽기만 허용
    if (caller_member is None and not is_admin and tool_name in MUTATION_TOOLS):
        _say(client, conn, slack_user_id, dm_channel,
             ":no_entry_sign: 발표 멤버만 사용 가능한 기능이에요. 일정/자료 조회는 가능합니다.")
        return

    if tool_name == "send_message":
        _say(client, conn, slack_user_id, dm_channel, args.get("text", "").strip() or ":wave:")
        return

    if tool_name == "set_topic":
        _tool_set_topic(client, conn, slack_user_id, dm_channel, args,
                         caller_member=caller_member, is_admin=is_admin, today=today)
        return

    if tool_name == "set_seminar_note":
        _tool_set_note(client, conn, slack_user_id, dm_channel, args,
                        is_admin=is_admin, today=today)
        return

    if tool_name == "answer_schedule_question":
        from .slack import flows
        flows._answer_schedule(client, conn, slack_user_id, dm_channel,
                                args.get("question") or "", today)
        return

    if tool_name == "answer_material_question":
        from .slack import flows
        flows._answer_material(client, dm_channel, args.get("question") or "",
                                prefetched_hits=hits)
        return

    if tool_name == "start_defer_flow":
        from .slack import flows
        flows._begin_defer_in_dm(client, conn, slack_user_id, dm_channel,
                                  args.get("initial_text") or "", today)
        return

    if tool_name == "start_preference_flow":
        from .slack import flows
        flows._begin_preference_in_dm(client, conn, slack_user_id, dm_channel,
                                       args.get("initial_text") or "")
        return

    log.warning("unknown tool: %s", tool_name)
    _say(client, conn, slack_user_id, dm_channel, ":x: 알 수 없는 도구 호출. 운영자 확인 필요.")


# ─────────────────────────────────────────────────────────────
# 개별 tool 실행
# ─────────────────────────────────────────────────────────────
def _tool_set_topic(
    client: WebClient, conn, slack_user_id: str, dm_channel: str, args: dict[str, Any],
    *, caller_member, is_admin: bool, today: date,
) -> None:
    target = args.get("target_presenter")
    topic = (args.get("topic") or "").strip()
    if not topic:
        _say(client, conn, slack_user_id, dm_channel,
             "토픽이 명확하지 않아요. 한 줄로 알려주세요.")
        return

    if target:
        target = target.strip()
        # 본인 이름과 같으면 self 모드로
        if caller_member and target == caller_member.name:
            target = None

    if target:
        # On-behalf-of: admin only
        if not is_admin:
            _say(client, conn, slack_user_id, dm_channel,
                 ":no_entry_sign: 본인 외 발표자의 토픽은 운영자만 등록할 수 있어요.")
            return
        m = member_service.get_by_name(conn, target)
        if m is None:
            _say(client, conn, slack_user_id, dm_channel,
                 f":x: '{target}' 발표 멤버를 찾지 못했어요.")
            return
        assignment = defer_service.find_requester_assignment(conn, m.slack_user_id, today)
        if assignment is None:
            _say(client, conn, slack_user_id, dm_channel,
                 f":x: {target}님이 다가올 일정에 없어요.")
            return
        seminar_date, presenter = assignment
    else:
        # Self mode
        if caller_member is None:
            _say(client, conn, slack_user_id, dm_channel,
                 ":information_source: 운영자는 발표 풀에서 빠져있어요. 다른 발표자 이름을 같이 알려주세요.")
            return
        assignment = defer_service.find_requester_assignment(conn, slack_user_id, today)
        if assignment is None:
            _say(client, conn, slack_user_id, dm_channel,
                 ":information_source: 다가올 발표 일정이 없어 토픽 등록 대상이 아닙니다.")
            return
        seminar_date, presenter = assignment

    ok = schedule_service.set_topic(conn, seminar_date, presenter, topic)
    if ok:
        _say(client, conn, slack_user_id, dm_channel,
             f":white_check_mark: 토픽 저장됨 — *{seminar_date.isoformat()} ({presenter})*\n"
             f"> _{topic}_")
        log.info("topic set via agent: %s / %s ← %r", presenter, seminar_date, topic[:80])
    else:
        _say(client, conn, slack_user_id, dm_channel,
             ":x: 토픽 저장 실패. 일정을 못 찾았어요.")


def _tool_set_note(
    client: WebClient, conn, slack_user_id: str, dm_channel: str, args: dict[str, Any],
    *, is_admin: bool, today: date,
) -> None:
    if not is_admin:
        _say(client, conn, slack_user_id, dm_channel,
             ":no_entry_sign: 운영 안내는 운영자만 등록할 수 있어요.")
        return

    target_str = args.get("target_date")
    notes = (args.get("notes") or "").strip()

    target_date: date | None = None
    if target_str:
        try:
            target_date = date.fromisoformat(target_str)
        except Exception:
            target_date = None
    if target_date is None:
        upcoming = schedule_service.get_upcoming(conn, today=today, limit=1)
        if upcoming:
            target_date = upcoming[0].date
    if target_date is None:
        _say(client, conn, slack_user_id, dm_channel,
             ":x: 다가올 일정이 없어 안내 저장 불가.")
        return

    ok = schedule_service.set_notes(conn, target_date, notes or None)
    if ok:
        if notes:
            _say(client, conn, slack_user_id, dm_channel,
                 f":pushpin: 운영 안내 저장됨 — *{target_date.isoformat()}*\n> {notes}")
        else:
            _say(client, conn, slack_user_id, dm_channel,
                 f":wastebasket: 운영 안내 삭제됨 — *{target_date.isoformat()}*")
    else:
        _say(client, conn, slack_user_id, dm_channel,
             f":x: {target_date.isoformat()} 일정 찾지 못함.")


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────
def _say(client: WebClient, conn, slack_user_id: str, channel: str, text: str) -> None:
    client.chat_postMessage(channel=channel, text=text)
    try:
        conversation_service.append(conn, slack_user_id, "assistant", text)
    except Exception as e:
        log.warning("conversation append (assistant) 실패: %s", e)


def _format_hits(hits: list[dict[str, Any]]) -> str:
    if not hits:
        return "  (검색 결과 없음)"
    lines = []
    for h in hits[:5]:
        dist = h.get("_distance")
        dist_str = f"{dist:.2f}" if isinstance(dist, (int, float)) else "?"
        snippet = (h.get("page_summary") or h.get("content") or "")[:200].replace("\n", " ")
        lines.append(
            f"  - [dist={dist_str}] {h.get('presenter','?')} / {h.get('seminar_date','?')} "
            f"/ p.{h.get('page_number','?')} / {h.get('title','')} :: {snippet}"
        )
    return "\n".join(lines)
