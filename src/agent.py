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
    memo_service,
    schedule_service,
    vector_service,
)
from .config import ADMIN_JJR
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
            "name": "add_memo",
            "description": (
                "임의의 메모를 영구 저장. 특정 세미나 회차에 묶거나 전역으로.\n"
                "사용 예:\n"
                "  - 사용자 '나 참가하고 싶어' → add_memo(seminar_date=다음회차, category='offline_attendee', content='<@user_id>')\n"
                "  - '자료 인쇄 부탁' → add_memo(seminar_date='YYYY-MM-DD', category='todo', content='자료 인쇄')\n"
                "  - 일반 메모 → category='note', content=...\n"
                "본인 신청 의사를 등록할 땐 content 에 '<@호출자_user_id>' 형태로 멘션 넣어라."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "seminar_date": {
                        "type": ["string", "null"],
                        "description": "YYYY-MM-DD 회차 묶음. 전역 메모면 null.",
                    },
                    "category": {
                        "type": "string",
                        "description": "자유 카테고리 (예: 'offline_attendee', 'todo', 'note')",
                    },
                    "content": {
                        "type": "string",
                        "description": "메모 본문 (한국어, 간결)",
                    },
                },
                "required": ["seminar_date", "category", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_memos",
            "description": (
                "저장된 메모 조회. seminar_date/category 로 필터 가능. 결과는 사용자에게 자연어로 정리해 응답."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "seminar_date": {
                        "type": ["string", "null"],
                        "description": "특정 회차 (YYYY-MM-DD) 또는 null (전체)",
                    },
                    "category": {
                        "type": ["string", "null"],
                        "description": "특정 카테고리 또는 null (모든 카테고리)",
                    },
                },
                "required": ["seminar_date", "category"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "escalate_to_admin",
            "description": (
                "사용자 요청이 봇 권한/지식 밖이거나 운영자 판단이 필요할 때. "
                "운영자에게 DM 으로 사용자 메시지 + 봇의 판단 이유를 전달. "
                "사용자에게도 'escalate 했습니다' 안내 응답."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "왜 escalate 하는지 한 줄 (한국어)",
                    },
                    "user_message_summary": {
                        "type": "string",
                        "description": "사용자 요청 핵심 한국어 요약",
                    },
                },
                "required": ["reason", "user_message_summary"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_member_pool",
            "description": (
                "발표 풀 멤버 제외/포함 toggle. 운영자만. 채널엔 있지만 발표 안 하는 멤버(관찰자/휴직/게스트)를 풀에서 빼는 용도. "
                "사용자 메시지가 '노거현은 발표 안 함', 'X는 풀에서 빼줘', 'Y 다시 포함' 같으면 호출."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target_name": {
                        "type": "string",
                        "description": "발표 풀에서 제외/포함할 멤버 이름 (한국어). 'X는 풀에서 빼' 의 X.",
                    },
                    "excluded": {
                        "type": "boolean",
                        "description": "true=제외 (풀에서 뺌), false=포함 (다시 풀에 넣음)",
                    },
                },
                "required": ["target_name", "excluded"],
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
- 본인 발표 토픽 → set_topic(*target_presenter=null*, topic=…)
   - 자기 자신 가리키는 모든 표현 ('내 토픽은', '제 토픽', '저는', 자기 이름, 자기 mention '<@U...>') → target_presenter=null
   - 절대 호출자 본인 이름을 target_presenter 에 넣지 말 것
- 다른 발표자의 토픽 지정 ('허성환은 X', 'X님 토픽 Y' 등) → set_topic(target_presenter='이름', topic=…)
   - 단, 호출자가 admin 이 아니면 send_message 로 정중히 거절
- 회차 운영 안내 ('5/14 회의실 B', '이번주 휴무' 등) → set_seminar_note (admin 만)
   - admin 아니면 send_message 로 거절
- 일정/발표자 조회 → answer_schedule_question
- 발표 자료 내용 질문 (검색 결과와 관련) → answer_material_question
- 본인 발표 연기 의사 → start_defer_flow
- 본인 평상시 선호도 → start_preference_flow
- *기록할 메모/명단* (참가 신청, 운영 todo, 임의 메모 등) → add_memo
   - 예: '나 이번 세션 참가하고 싶어' → category='offline_attendee', seminar_date=가장 가까운 회차, content='<@호출자>'
   - 본인 신청은 호출자 user_id 를 '<@U...>' 멘션 형태로 content 에 넣어라 (사용자 이름 정보가 있다면 'name (<@U...>)' 도 OK)
- *메모 조회* ('이번 세션 누가 참가?', '오프라인 신청자 명단', '운영 todo' 등) → list_memos
   - 결과를 사용자에게 자연어로 정리해 응답
- *발표 풀 멤버십 변경* (운영자만): 'X는 풀에서 빼줘' / 'Y는 발표 안 함' / 'Z 다시 포함'
  → set_member_pool(target_name='X', excluded=true/false)
- *권한/지식 밖* — 봇이 답할 수 없거나 운영자 판단 필요 → escalate_to_admin
   - 예: '회의실 예약 좀', '발표비 정산', '봇 기능에 없는 외부 시스템 연동 요청' 등
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
        # tool_choice="required": LLM 이 무조건 tool 하나 호출하게 강제.
        # history 오염으로 LLM 이 rejection 텍스트만 뱉는 패턴 차단.
        resp = oai.chat.completions.create(
            model=LLM_MODEL, messages=msgs, tools=TOOLS, tool_choice="required",
            temperature=0.2,
        )
    except Exception as e:
        log.warning("tool_choice=required 실패, auto 로 fallback: %s", e)
        try:
            resp = oai.chat.completions.create(
                model=LLM_MODEL, messages=msgs, tools=TOOLS, tool_choice="auto",
                temperature=0.2,
            )
        except Exception as e2:
            log.exception("agent LLM 호출 실패")
            _say(client, conn, slack_user_id, dm_channel, f":x: 처리 중 오류 ({e2})")
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
MUTATION_TOOLS = {"set_topic", "set_seminar_note", "start_defer_flow", "start_preference_flow", "set_member_pool"}


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

    if tool_name == "add_memo":
        _tool_add_memo(client, conn, slack_user_id, dm_channel, args)
        return

    if tool_name == "list_memos":
        _tool_list_memos(client, conn, slack_user_id, dm_channel, args)
        return

    if tool_name == "escalate_to_admin":
        _tool_escalate(client, conn, slack_user_id, dm_channel, args)
        return

    if tool_name == "set_member_pool":
        _tool_set_member_pool(client, conn, slack_user_id, dm_channel, args, is_admin=is_admin)
        return

    log.warning("unknown tool: %s", tool_name)
    _say(client, conn, slack_user_id, dm_channel, ":x: 알 수 없는 도구 호출. 운영자 확인 필요.")


# ─────────────────────────────────────────────────────────────
# 개별 tool 실행
# ─────────────────────────────────────────────────────────────
def _norm_name(s: str | None) -> str:
    """이름 비교용 정규화 — 공백, 호칭(님/씨), 대소문자 제거."""
    if not s:
        return ""
    out = s.strip()
    # slack mention 형식 처리
    if out.startswith("<@") and ">" in out:
        out = out[2:out.index(">")].split("|")[0]
    # 한국어 호칭 제거
    for suffix in ("님", "씨"):
        if out.endswith(suffix):
            out = out[: -len(suffix)]
    return out.strip().lower()


_SELF_PRONOUNS = {
    "내", "제", "저", "본인", "나",
    "self", "me", "myself", "i",
}


def _topics_overlap(a: str, b: str) -> bool:
    """case-insensitive + whitespace normalize 후 동일 여부."""
    na = " ".join((a or "").lower().split())
    nb = " ".join((b or "").lower().split())
    return bool(na) and na == nb


def _is_self_target(target: str, *, caller_member, slack_user_id: str) -> bool:
    """target_presenter 가 호출자 자기 자신을 가리키는지."""
    norm = _norm_name(target)
    if not norm:
        return True
    if norm in _SELF_PRONOUNS:
        return True
    if caller_member and norm == _norm_name(caller_member.name):
        return True
    # slack user id 직접 매칭
    if norm == slack_user_id.lower():
        return True
    return False


def _tool_set_topic(
    client: WebClient, conn, slack_user_id: str, dm_channel: str, args: dict[str, Any],
    *, caller_member, is_admin: bool, today: date,
) -> None:
    target = args.get("target_presenter")
    topic = (args.get("topic") or "").strip()
    log.info("set_topic args: target=%r topic=%r caller=%s admin=%s",
             target, topic, caller_member.name if caller_member else None, is_admin)
    if not topic:
        _say(client, conn, slack_user_id, dm_channel,
             "토픽이 명확하지 않아요. 한 줄로 알려주세요.")
        return

    # 핵심 규칙: 멤버는 본인 토픽만 등록 가능. admin 만 다른 발표자 대신 등록 가능.
    # 따라서 비-admin 호출자는 target 을 무조건 무시 (self 강제).
    # admin 일 때만 target 의미를 살리되, 호출자 자기 자신 가리키면 self.
    if not is_admin:
        if target:
            log.info("set_topic: 비-admin 호출자 → target=%r 무시 self 강제", target)
        target = None
    elif target and _is_self_target(target, caller_member=caller_member, slack_user_id=slack_user_id):
        log.info("set_topic: target=%r → self-normalized to null", target)
        target = None

    if target:
        # admin 의 on-behalf-of: slack mention 형식이면 member 조회로 정식 이름 정규화
        if target.startswith("<@") and ">" in target:
            uid = target[2:target.index(">")].split("|")[0]
            m = member_service.get_by_slack_id(conn, uid)
            target = m.name if m else target

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

    # 같은 날 다른 슬롯이 같은 토픽 이미 등록했는지 검사
    schedule = schedule_service.get_by_date(conn, seminar_date)
    if schedule:
        if schedule.slot_1 == presenter:
            other_slot_presenter, other_slot_topic = schedule.slot_2, schedule.slot_2_topic
        elif schedule.slot_2 == presenter:
            other_slot_presenter, other_slot_topic = schedule.slot_1, schedule.slot_1_topic
        else:
            other_slot_presenter, other_slot_topic = None, None
        if other_slot_topic and _topics_overlap(topic, other_slot_topic):
            _say(
                client, conn, slack_user_id, dm_channel,
                f":no_entry_sign: 같은 날 *{other_slot_presenter}*님이 이미 같은 토픽으로 등록했어요.\n"
                f"> _{other_slot_topic}_\n"
                "다른 각도/제목으로 차별화 부탁드립니다.",
            )
            log.info("topic conflict: %s/%s same as %s/%s on %s",
                     presenter, topic[:60], other_slot_presenter, other_slot_topic[:60], seminar_date)
            return

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


def _tool_add_memo(
    client: WebClient, conn, slack_user_id: str, dm_channel: str, args: dict[str, Any],
) -> None:
    seminar_date = args.get("seminar_date")
    category = (args.get("category") or "").strip()
    content = (args.get("content") or "").strip()
    if not category or not content:
        _say(client, conn, slack_user_id, dm_channel, ":x: 메모 카테고리/본문이 비어있어요.")
        return
    try:
        memo_id = memo_service.add(
            conn,
            seminar_date=seminar_date if seminar_date else None,
            category=category, content=content, created_by=slack_user_id,
        )
    except Exception as e:
        log.exception("memo add 실패")
        _say(client, conn, slack_user_id, dm_channel, f":x: 메모 저장 실패: {e}")
        return

    where = f" ({seminar_date})" if seminar_date else ""
    _say(client, conn, slack_user_id, dm_channel,
         f":memo: 메모 저장됨 — *{category}*{where}\n> {content}")
    log.info("memo added: id=%d cat=%s sem=%s content=%r",
             memo_id, category, seminar_date, content[:80])


def _tool_list_memos(
    client: WebClient, conn, slack_user_id: str, dm_channel: str, args: dict[str, Any],
) -> None:
    seminar_date = args.get("seminar_date")
    category = args.get("category")
    rows = memo_service.list_memos(
        conn,
        seminar_date=seminar_date if seminar_date else None,
        category=category if category else None,
        limit=50,
    )
    if not rows:
        scope_label = []
        if seminar_date: scope_label.append(seminar_date)
        if category: scope_label.append(f"#{category}")
        scope = f" ({', '.join(scope_label)})" if scope_label else ""
        _say(client, conn, slack_user_id, dm_channel, f":notebook: 메모 없음{scope}.")
        return

    # 카테고리별 그룹핑
    by_cat: dict[str, list[dict]] = {}
    for r in rows:
        by_cat.setdefault(r["category"], []).append(r)

    lines: list[str] = [":notebook: *메모*"]
    for cat, items in by_cat.items():
        scope = f" — {seminar_date}" if seminar_date else ""
        lines.append(f"\n*{cat}*{scope} ({len(items)}개)")
        for r in items[:20]:
            sem = f" [{r['seminar_date']}]" if (r.get("seminar_date") and not seminar_date) else ""
            lines.append(f"  • {r['content']}{sem}")

    _say(client, conn, slack_user_id, dm_channel, "\n".join(lines))


def _tool_set_member_pool(
    client: WebClient, conn, slack_user_id: str, dm_channel: str, args: dict[str, Any],
    *, is_admin: bool,
) -> None:
    if not is_admin:
        _say(client, conn, slack_user_id, dm_channel,
             ":no_entry_sign: 발표 풀 관리는 운영자만 가능합니다.")
        return
    target_name = (args.get("target_name") or "").strip()
    excluded = bool(args.get("excluded"))
    if not target_name:
        _say(client, conn, slack_user_id, dm_channel,
             "어떤 멤버를 제외/포함할지 이름을 알려주세요.")
        return
    m = member_service.get_by_name(conn, target_name)
    if m is None:
        _say(client, conn, slack_user_id, dm_channel,
             f":x: '{target_name}' 멤버를 찾지 못했어요. 정확한 이름 다시 확인 부탁드립니다.")
        return
    ok, info = member_service.set_excluded(conn, m.slack_user_id, excluded)
    if not ok:
        _say(client, conn, slack_user_id, dm_channel, f":x: 실패: {info}")
        return
    if excluded:
        _say(client, conn, slack_user_id, dm_channel,
             f":mute: *{m.name}* 발표 풀에서 제외됨. 추첨/대체자 후보에 안 들어갑니다.")
    else:
        _say(client, conn, slack_user_id, dm_channel,
             f":speaker: *{m.name}* 발표 풀에 다시 포함됨.")
    log.info("set_member_pool: %s excluded=%s by %s", m.name, excluded, slack_user_id)


def _tool_escalate(
    client: WebClient, conn, slack_user_id: str, dm_channel: str, args: dict[str, Any],
) -> None:
    reason = (args.get("reason") or "").strip() or "(이유 미지정)"
    summary = (args.get("user_message_summary") or "").strip() or "(요약 없음)"
    primary = admin_service.get_primary_admin_id() or ADMIN_JJR
    text_admin = (
        f":bell: *봇 권한 밖 요청 escalation*\n"
        f"From: <@{slack_user_id}>\n"
        f"요청 요약: {summary}\n"
        f"봇 판단: {reason}"
    )
    try:
        admin_dm = client.conversations_open(users=primary)["channel"]["id"]
        client.chat_postMessage(channel=admin_dm, text=text_admin)
    except Exception as e:
        log.warning("escalate → admin DM 실패: %s", e)
        _say(client, conn, slack_user_id, dm_channel,
             ":x: 운영자에게 전달 실패. 잠시 후 다시 시도하거나 직접 연락 부탁드립니다.")
        return

    _say(client, conn, slack_user_id, dm_channel,
         f"제 권한/판단 밖이라 운영자(<@{primary}>) 에게 메시지 전달드렸어요. 곧 응답 드릴 거예요. :pray:")
    log.info("escalate by %s reason=%r summary=%r", slack_user_id, reason[:80], summary[:80])


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
