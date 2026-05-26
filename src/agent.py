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
from datetime import date
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
    notification_service,
    schedule_service,
    vector_service,
)
from .config import ADMIN_JJR

log = logging.getLogger(__name__)


ROLE_RANK = {
    "bystander": 0,
    "member": 1,
    "admin": 2,
}


TOOL_REQUIRED_ROLE = {
    "send_message": "bystander",
    "answer_schedule_question": "bystander",
    "answer_material_question": "bystander",
    "escalate_to_admin": "bystander",
    "set_topic": "member",
    "start_defer_flow": "member",
    "start_preference_flow": "member",
    "add_memo": "member",
    "list_memos": "member",
    "set_seminar_note": "admin",
    "set_member_pool": "admin",
    "admin_help": "admin",
    "set_presenter": "admin",
    "broadcast_schedule": "admin",
    "manage_admin": "admin",
    "inspect_db_state": "admin",
    "plan_next_steps": "bystander",
    "dm_user": "admin",
}


ROLE_DENIAL_MESSAGES = {
    "admin": ":no_entry_sign: 운영자만 사용할 수 있는 기능이에요.",
    "member": ":no_entry_sign: 발표 멤버만 사용할 수 있는 기능이에요. 일정/자료 조회는 가능합니다.",
    "bystander": ":no_entry_sign: 권한이 부족해요.",
}


def _has_tool_role(caller_role: str, tool_name: str) -> bool:
    required = TOOL_REQUIRED_ROLE.get(tool_name, "admin")
    return ROLE_RANK.get(caller_role, 0) >= ROLE_RANK[required]


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
            "name": "admin_help",
            "description": (
                "운영자가 '도움말', '뭐 할 수 있어', '운영자 명령어', '어떻게 써', 'admin help' 같은 "
                "사용법/기능 안내를 요청할 때. 운영자만 — member/bystander 호출자는 send_message 로 일반 안내."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_presenter",
            "description": (
                "특정 회차의 slot_1 또는 slot_2 에 발표자 배정/교체/제거. 운영자만. "
                "1명/주 자동 추첨 후 ad-hoc 으로 2명 발표 만들거나, 임의 교체할 때 사용. "
                "예: '5/28 한 명 더 추가해, 김재선' → slot=2, name='김재선'. "
                "'6/4 두 번째 발표자 빼' → slot=2, name=null. "
                "'6/11 첫 발표자 임종석으로 바꿔, 토픽 RAG eval' → slot=1, name='임종석', topic='RAG eval'. "
                "토픽 명시 안 하면 기존 슬롯 토픽은 자동 클리어 (앞 사람 토픽 인계 방지)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target_date": {
                        "type": "string",
                        "description": "YYYY-MM-DD. 사용자가 '5/28' 처럼 줄여 말하면 올해 기준으로 보정.",
                    },
                    "slot": {
                        "type": "integer",
                        "enum": [1, 2],
                        "description": "1=첫 번째 발표자, 2=두 번째 발표자. '추가/한 명 더' 같으면 2.",
                    },
                    "name": {
                        "type": ["string", "null"],
                        "description": "배정할 멤버 이름 (한국어). null=해당 슬롯 비우기.",
                    },
                    "topic": {
                        "type": ["string", "null"],
                        "description": "선택. 새 배정과 동시에 토픽 등록. 미지정이면 null (기존 슬롯 토픽 자동 클리어).",
                    },
                },
                "required": ["target_date", "slot", "name", "topic"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "broadcast_schedule",
            "description": (
                "운영자가 DM 에서 세미나 일정을 등록 채널(BROADCAST_CHANNELS)에 즉시 공지하고 싶을 때. "
                "예: '채널에 일정 공유해', '일정 공지해', '이번주 발표자 채널에 알려'. 운영자만."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "scope": {
                        "type": "string",
                        "enum": ["this_week", "upcoming"],
                        "description": "this_week=가장 가까운 1회차만, upcoming=다가올 5주 일정 전체. 모호하면 upcoming.",
                    },
                },
                "required": ["scope"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "manage_admin",
            "description": (
                "운영자 권한 추가/삭제/목록 조회. 운영자만. "
                "'@사용자 운영자 추가', '운영자 목록', 'X 어드민 빼줘' 같은 요청에 사용."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["add", "remove", "list"],
                        "description": "add=운영자 추가, remove=운영자 삭제, list=운영자 목록",
                    },
                    "target_user": {
                        "type": ["string", "null"],
                        "description": "add/remove 대상 Slack mention 또는 user_id. list면 null.",
                    },
                },
                "required": ["action", "target_user"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "inspect_db_state",
            "description": (
                "DB 상태를 안전하게 조회. 운영자만. 임의 SQL 실행이 아니라 allowlist 된 상태만 읽는다. "
                "멤버/제외목록/운영자/다가올 일정/메모 요약 확인에 사용."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "scope": {
                        "type": "string",
                        "enum": ["admins", "members", "excluded_members", "upcoming_schedule", "memos"],
                        "description": "조회할 DB 상태 범위",
                    },
                },
                "required": ["scope"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "plan_next_steps",
            "description": (
                "사용자 요청이 여러 단계이거나 바로 실행하기 위험/모호할 때, DM 대화 history와 현재 상태를 바탕으로 "
                "짧은 실행 계획을 제시하고 필요한 확인/추가 정보를 요청한다. 실제 변경은 하지 않는다."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "goal": {
                        "type": "string",
                        "description": "사용자가 달성하려는 목표를 한 줄로 요약",
                    },
                    "steps": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": 5,
                        "description": "봇이 수행할 계획. 각 단계는 짧은 한국어 문장.",
                    },
                    "needs_confirmation": {
                        "type": "boolean",
                        "description": "실행 전 사용자 확인이 필요한지 여부",
                    },
                    "question": {
                        "type": ["string", "null"],
                        "description": "사용자에게 필요한 확인/추가 질문. 없으면 null.",
                    },
                },
                "required": ["goal", "steps", "needs_confirmation", "question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "dm_user",
            "description": (
                "운영자가 특정 사용자에게 DM을 보내거나 보낼 메시지를 초안으로 만들 때 사용. 운영자만. "
                "사용자가 명확히 '보내줘/전송해'라고 한 경우에만 send_now=true. 애매하면 send_now=false로 초안만 보여준다."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target_user": {
                        "type": "string",
                        "description": "수신자 Slack mention/user_id 또는 발표 멤버 이름",
                    },
                    "text": {
                        "type": "string",
                        "description": "수신자에게 보낼 DM 본문",
                    },
                    "send_now": {
                        "type": "boolean",
                        "description": "true=즉시 발송, false=운영자에게 초안만 보여주고 확인 요청",
                    },
                    "reason": {
                        "type": ["string", "null"],
                        "description": "왜 보내는지 운영자 확인용 짧은 설명",
                    },
                },
                "required": ["target_user", "text", "send_now", "reason"],
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

bystander 는 *읽기(answer_schedule_question / answer_material_question / send_message / escalate_to_admin)*만 가능.
mutation 요청 (set_topic / set_seminar_note / start_defer_flow / start_preference_flow / add_memo 등) 시도하면
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
- *채널 일정 공지* (운영자만): '채널에 일정 공유해' / '일정 공지해' / '이번주 발표자 채널에 띄워'
  → broadcast_schedule(scope='this_week' 또는 'upcoming'). 모호하면 upcoming.
- *발표자 배정/교체/제거* (운영자만): '5/28 한 명 더 추가, 김재선' / '6/4 두 번째 발표자 빼' / '6/11 첫 발표자 임종석으로 바꿔'
  → set_presenter(target_date='YYYY-MM-DD', slot=1|2, name='이름' 또는 null, topic='토픽' 또는 null)
   - '한 명 더', '추가', '두 번째' → slot=2. '교체', '바꿔', '첫', '메인' → 보통 slot=1 (또는 위 upcoming 표의 빈 슬롯).
   - 사용자 명시 안 했고 빈 슬롯이 있으면 빈 쪽을 먼저 채워라.
   - '빼/제거/취소' → name=null.
   - 토픽 같이 말하면 topic 도 채워. 안 말하면 null (앞 사람 토픽이 자동 클리어됨 — 정상 동작).
   - 사용자가 '5/28' 처럼 줄여 말하면 오늘 기준 가까운 미래 목요일로 보정해서 YYYY-MM-DD 만들어라.
- *운영자 도움말/사용법 요청* ('도움말', '뭐 할 수 있어', '명령어 알려줘', '어떻게 써' 등)
  → 호출자가 admin 이면 admin_help. member/bystander 면 send_message 로 일반 안내 (채널 자기소개 메시지 참고하라고).
- *운영자 관리* (운영자만): '@누구 운영자 추가/삭제', '운영자 목록'
  → manage_admin(action='add'/'remove'/'list', target_user='<@U...>' 또는 null)
- *DB 상태 조회* (운영자만): 'DB 상태', '제외 목록 보여줘', '멤버 풀 확인', '다가올 일정 DB 확인'
  → inspect_db_state(scope='admins'/'members'/'excluded_members'/'upcoming_schedule'/'memos')
  - 임의 SQL 실행은 지원하지 않는다. 필요한 경우 가장 가까운 scope를 고른다.
- *사용자 DM 발송/초안* (운영자만): '@누구에게 X라고 DM 보내줘', '허성환님께 토픽 알려달라고 메시지 써줘'
  → dm_user(target_user='...', text='...', send_now=true/false, reason='...')
  - "보내줘", "전송해", "DM 해줘"처럼 즉시 발송 의도가 명확하면 send_now=true.
  - "초안", "써줘", "어떻게 보낼까"처럼 검토 요청이면 send_now=false.
  - 수신자/본문이 모호하면 plan_next_steps 또는 send_message로 확인 질문.
- *권한/지식 밖* — 봇이 답할 수 없거나 운영자 판단 필요 → escalate_to_admin
   - 예: '회의실 예약 좀', '발표비 정산', '봇 기능에 없는 외부 시스템 연동 요청' 등
- 여러 단계 요청, 위험한 변경, 대상/날짜/채널이 모호한 요청 → plan_next_steps
   - 예: "다음 사이클 다시 짜고 공지도 해줘", "운영자 정리하고 풀도 업데이트해줘"
   - 계획만 제시하고 실제 변경은 하지 않는다. 사용자가 확인/세부정보를 주면 다음 턴에서 적절한 실행 tool 호출.
- 인사/잡담/그 외 → send_message

# 대화 history 활용 (중요)
직전 봇 발화가 토픽/노트를 물어봤다면 사용자 짧은 답을 그에 대한 응답으로 해석.
예) 봇이 '허성환 토픽 미등록' → 사용자 'pydanticAI' → set_topic(target=null, topic='pydanticAI')
예) 봇이 일정 조회 답함 → 사용자 '허성환은 pydanticAI한데' → admin 이면 set_topic(target='허성환', topic='pydanticAI')
사용자가 "그거", "저 사람", "위 일정", "아까 말한 날짜"처럼 지시하면 반드시 대화 history에서 참조 대상을 찾는다.
history로도 대상이 확정되지 않으면 plan_next_steps 또는 send_message로 짧게 확인 질문한다.

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
    def _upcoming_line(s) -> str:
        names = s.presenters()
        if not names:
            return f"  - {s.date.isoformat()} 14:00: 미정"
        parts: list[str] = []
        for i, n in enumerate(names, start=1):
            topic = s.topic_for(n)
            label = f"slot_{i}={n}" + (f" 토픽={topic!r}" if topic else "")
            parts.append(label)
        return f"  - {s.date.isoformat()} 14:00: " + " / ".join(parts)
    upcoming_text = "\n".join(_upcoming_line(s) for s in upcoming) or "  (없음)"

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
                caller_member=caller_member, is_admin=is_admin, caller_role=caller_role,
                hits=hits, today=today,
            )
        return

    # tool 호출 없이 텍스트만 → 그대로 발송 (안전망)
    if msg.content:
        _say(client, conn, slack_user_id, dm_channel, msg.content.strip())


def _dispatch(
    client: WebClient, conn,
    *,
    tool_name: str, args: dict[str, Any],
    slack_user_id: str, dm_channel: str,
    caller_member, is_admin: bool, caller_role: str,
    hits: list[dict[str, Any]], today: date,
) -> None:
    required_role = TOOL_REQUIRED_ROLE.get(tool_name)
    if required_role is None:
        log.warning("unknown tool before dispatch: %s", tool_name)
        _say(client, conn, slack_user_id, dm_channel, ":x: 알 수 없는 도구 호출. 운영자 확인 필요.")
        return

    if not _has_tool_role(caller_role, tool_name):
        _say(client, conn, slack_user_id, dm_channel, ROLE_DENIAL_MESSAGES[required_role])
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

    if tool_name == "broadcast_schedule":
        _tool_broadcast_schedule(client, conn, slack_user_id, dm_channel, args, is_admin=is_admin)
        return

    if tool_name == "set_presenter":
        _tool_set_presenter(client, conn, slack_user_id, dm_channel, args, is_admin=is_admin)
        return

    if tool_name == "admin_help":
        _tool_admin_help(client, conn, slack_user_id, dm_channel, is_admin=is_admin)
        return

    if tool_name == "manage_admin":
        _tool_manage_admin(client, conn, slack_user_id, dm_channel, args, is_admin=is_admin)
        return

    if tool_name == "inspect_db_state":
        _tool_inspect_db_state(client, conn, slack_user_id, dm_channel, args, is_admin=is_admin, today=today)
        return

    if tool_name == "plan_next_steps":
        _tool_plan_next_steps(client, conn, slack_user_id, dm_channel, args)
        return

    if tool_name == "dm_user":
        _tool_dm_user(client, conn, slack_user_id, dm_channel, args, is_admin=is_admin)
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


def _parse_slack_user_id(text: str | None) -> str | None:
    """Slack mention('<@U...|name>'), raw user id, or empty text → user id."""
    if not text:
        return None
    text = text.strip()
    if text.startswith("<@") and ">" in text:
        inner = text[2:text.index(">")]
        return inner.split("|")[0]
    first = text.split()[0] if text.split() else ""
    if first.startswith("U") and len(first) >= 9:
        return first
    return None


def _member_from_target(conn, target: str | None):
    if not target:
        return None
    uid = _parse_slack_user_id(target)
    if uid:
        return member_service.get_by_slack_id(conn, uid)
    return member_service.get_by_name(conn, target.strip())


def _resolve_target_user_id(conn, target: str | None) -> str | None:
    uid = _parse_slack_user_id(target)
    if uid:
        return uid
    member = _member_from_target(conn, target)
    return member.slack_user_id if member else None


_SELF_PRONOUNS = {
    "내", "제", "저", "본인", "나",
    "self", "me", "myself", "i",
}


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
        if seminar_date:
            scope_label.append(seminar_date)
        if category:
            scope_label.append(f"#{category}")
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
    m = _member_from_target(conn, target_name)
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


def _tool_admin_help(
    client: WebClient, conn, slack_user_id: str, dm_channel: str, *, is_admin: bool,
) -> None:
    if not is_admin:
        _say(client, conn, slack_user_id, dm_channel,
             ":information_source: 일반 사용자용 안내는 채널 자기소개 메시지를 참고해주세요.")
        return
    from . import intro_message
    _say(client, conn, slack_user_id, dm_channel, intro_message.build_admin_help())


def _tool_set_presenter(
    client: WebClient, conn, slack_user_id: str, dm_channel: str, args: dict[str, Any],
    *, is_admin: bool,
) -> None:
    if not is_admin:
        _say(client, conn, slack_user_id, dm_channel,
             ":no_entry_sign: 발표자 변경은 운영자만 가능합니다.")
        return
    target_str = (args.get("target_date") or "").strip()
    slot_raw = args.get("slot")
    name_arg = args.get("name")
    topic_arg = args.get("topic")
    name = (name_arg or "").strip() if isinstance(name_arg, str) else None
    if name == "":
        name = None
    topic = (topic_arg or "").strip() if isinstance(topic_arg, str) else None
    if topic == "":
        topic = None

    try:
        target_date = date.fromisoformat(target_str)
    except Exception:
        _say(client, conn, slack_user_id, dm_channel,
             f":x: 날짜 형식이 잘못됐어요 (받은 값: {target_str!r}). YYYY-MM-DD 로 알려주세요.")
        return
    try:
        slot = int(slot_raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        _say(client, conn, slack_user_id, dm_channel,
             ":x: slot은 1 또는 2 여야 해요.")
        return
    if slot not in (1, 2):
        _say(client, conn, slack_user_id, dm_channel,
             ":x: slot은 1 또는 2 여야 해요.")
        return

    # 이름 정규화: slack mention 형식이면 member 조회로 이름 변환.
    if name and name.startswith("<@") and ">" in name:
        uid = name[2:name.index(">")].split("|")[0]
        m = member_service.get_by_slack_id(conn, uid)
        if m is not None:
            name = m.name

    if name is not None:
        m = member_service.get_by_name(conn, name)
        if m is None:
            _say(client, conn, slack_user_id, dm_channel,
                 f":x: '{name}' 발표 멤버를 찾지 못했어요. 정확한 이름 다시 확인 부탁드립니다.")
            return

    try:
        new_s = schedule_service.set_presenter(
            conn, target_date, slot, name, topic=topic,
        )
    except ValueError as e:
        _say(client, conn, slack_user_id, dm_channel, f":x: {e}")
        return

    if name is None:
        _say(client, conn, slack_user_id, dm_channel,
             f":wastebasket: *{target_date.isoformat()}* slot_{slot} 비움.")
    else:
        topic_line = f"\n> _{topic}_" if topic else ""
        _say(client, conn, slack_user_id, dm_channel,
             f":white_check_mark: *{target_date.isoformat()}* slot_{slot} ← *{name}*{topic_line}")
    log.info("set_presenter by %s: date=%s slot=%s name=%r topic=%r → %s",
             slack_user_id, target_date, slot, name, topic, new_s)


def _tool_broadcast_schedule(
    client: WebClient, conn, slack_user_id: str, dm_channel: str, args: dict[str, Any],
    *, is_admin: bool,
) -> None:
    if not is_admin:
        _say(client, conn, slack_user_id, dm_channel,
             ":no_entry_sign: 채널 공지는 운영자만 가능합니다.")
        return
    scope = (args.get("scope") or "upcoming").strip()
    if scope not in {"this_week", "upcoming"}:
        scope = "upcoming"
    ok, info = notification_service.broadcast_schedule_summary(client, conn, scope=scope)
    if ok:
        label = "이번 주 회차" if scope == "this_week" else "다가올 5주 일정"
        _say(client, conn, slack_user_id, dm_channel,
             f":mega: 등록 채널에 *{label}* 공지 발송했습니다.")
        log.info("broadcast_schedule by %s scope=%s", slack_user_id, scope)
    else:
        _say(client, conn, slack_user_id, dm_channel, f":x: 공지 실패: {info}")


def _tool_manage_admin(
    client: WebClient, conn, slack_user_id: str, dm_channel: str, args: dict[str, Any],
    *, is_admin: bool,
) -> None:
    if not is_admin:
        _say(client, conn, slack_user_id, dm_channel,
             ":no_entry_sign: 운영자 관리는 운영자만 가능합니다.")
        return

    action = (args.get("action") or "").strip()
    target = args.get("target_user")

    if action == "list":
        rows = admin_service.list_admins(conn)
        if not rows:
            _say(client, conn, slack_user_id, dm_channel,
                 ":busts_in_silhouette: 운영자 없음 (DB 부트스트랩 실패 가능).")
            return
        lines = [":busts_in_silhouette: *현재 운영자 목록*"]
        for r in rows:
            tag = " :star: (primary)" if r["is_primary"] else ""
            lines.append(f"• <@{r['slack_user_id']}>{tag}  _added {r['added_at']}_")
        _say(client, conn, slack_user_id, dm_channel, "\n".join(lines))
        return

    target_uid = _parse_slack_user_id(target)
    if target_uid is None:
        _say(client, conn, slack_user_id, dm_channel,
             "대상 사용자를 Slack 멘션(`<@U...>`)이나 user ID로 알려주세요.")
        return

    if action == "add":
        added = admin_service.add_admin(conn, target_uid, added_by=slack_user_id)
        if added:
            _say(client, conn, slack_user_id, dm_channel,
                 f":white_check_mark: <@{target_uid}> 운영자로 추가됨.")
        else:
            _say(client, conn, slack_user_id, dm_channel,
                 f":information_source: <@{target_uid}> 이미 운영자입니다.")
        return

    if action == "remove":
        ok, reason = admin_service.remove_admin(conn, target_uid)
        if ok:
            _say(client, conn, slack_user_id, dm_channel,
                 f":wastebasket: <@{target_uid}> 운영자 권한 해제됨.")
        else:
            _say(client, conn, slack_user_id, dm_channel,
                 f":no_entry_sign: 제거 실패: {reason}")
        return

    _say(client, conn, slack_user_id, dm_channel,
         ":x: 운영자 관리 action은 add/remove/list 중 하나여야 합니다.")


def _tool_inspect_db_state(
    client: WebClient, conn, slack_user_id: str, dm_channel: str, args: dict[str, Any],
    *, is_admin: bool, today: date,
) -> None:
    if not is_admin:
        _say(client, conn, slack_user_id, dm_channel,
             ":no_entry_sign: DB 상태 조회는 운영자만 가능합니다.")
        return

    scope = (args.get("scope") or "").strip()
    if scope == "admins":
        rows = admin_service.list_admins(conn)
        lines = [":busts_in_silhouette: *admins*"]
        lines.extend(
            f"• <@{r['slack_user_id']}> primary={bool(r['is_primary'])} added={r['added_at']}"
            for r in rows
        )
        _say(client, conn, slack_user_id, dm_channel, "\n".join(lines) if len(lines) > 1 else "admins: 없음")
        return

    if scope == "members":
        members = member_service.get_all(conn)
        active_ids = {m.slack_user_id for m in member_service.get_all_active(conn)}
        lines = [f":card_index: *members* ({len(members)} total, {len(active_ids)} active pool)"]
        for m in members[:50]:
            status = "pool" if m.slack_user_id in active_ids else "inactive/excluded"
            lines.append(f"• {m.name} <@{m.slack_user_id}> — {status}")
        _say(client, conn, slack_user_id, dm_channel, "\n".join(lines))
        return

    if scope == "excluded_members":
        rows = member_service.list_excluded(conn)
        if not rows:
            _say(client, conn, slack_user_id, dm_channel, ":speaker: 제외된 멤버 없음.")
            return
        lines = [":mute: *발표 풀 제외 멤버*"]
        for r in rows:
            in_ch = "" if r["is_active"] else " _(채널 떠남)_"
            lines.append(f"• <@{r['slack_user_id']}> ({r['name']}){in_ch}")
        _say(client, conn, slack_user_id, dm_channel, "\n".join(lines))
        return

    if scope == "upcoming_schedule":
        upcoming = schedule_service.get_upcoming(conn, today=today, limit=10)
        if not upcoming:
            _say(client, conn, slack_user_id, dm_channel, ":calendar: 다가올 일정 없음.")
            return
        lines = [":calendar: *다가올 일정 DB 상태*"]
        for s in upcoming:
            topic = f" — _{s.slot_1_topic}_" if s.slot_1_topic else ""
            note = f" / note={s.notes}" if s.notes else ""
            lines.append(f"• {s.date.isoformat()} 14:00 — {s.slot_1 or '미정'}{topic}{note}")
        _say(client, conn, slack_user_id, dm_channel, "\n".join(lines))
        return

    if scope == "memos":
        rows = memo_service.list_memos(conn, limit=20)
        if not rows:
            _say(client, conn, slack_user_id, dm_channel, ":notebook: 메모 없음.")
            return
        lines = [":notebook: *최근 메모*"]
        for r in rows:
            sem = f" [{r['seminar_date']}]" if r.get("seminar_date") else ""
            lines.append(f"• #{r['id']} {r['category']}{sem}: {r['content']}")
        _say(client, conn, slack_user_id, dm_channel, "\n".join(lines))
        return

    _say(client, conn, slack_user_id, dm_channel,
         ":x: DB 조회 scope는 admins/members/excluded_members/upcoming_schedule/memos 중 하나여야 합니다.")


def _tool_plan_next_steps(
    client: WebClient, conn, slack_user_id: str, dm_channel: str, args: dict[str, Any],
) -> None:
    goal = (args.get("goal") or "").strip() or "요청 처리"
    raw_steps = args.get("steps") or []
    steps = [str(s).strip() for s in raw_steps if str(s).strip()][:5]
    if not steps:
        steps = ["요청 내용을 확인한다.", "필요한 도구를 선택해 처리한다."]
    needs_confirmation = bool(args.get("needs_confirmation"))
    question = (args.get("question") or "").strip()

    lines = [f":clipboard: *처리 계획*: {goal}"]
    for i, step in enumerate(steps, start=1):
        lines.append(f"{i}. {step}")
    if needs_confirmation:
        lines.append("")
        lines.append(question or "이대로 진행해도 될까요?")
    elif question:
        lines.append("")
        lines.append(question)
    _say(client, conn, slack_user_id, dm_channel, "\n".join(lines))


def _tool_dm_user(
    client: WebClient, conn, slack_user_id: str, dm_channel: str, args: dict[str, Any],
    *, is_admin: bool,
) -> None:
    if not is_admin:
        _say(client, conn, slack_user_id, dm_channel,
             ":no_entry_sign: 사용자 DM 발송은 운영자만 가능합니다.")
        return

    target = (args.get("target_user") or "").strip()
    text = (args.get("text") or "").strip()
    reason = (args.get("reason") or "").strip()
    send_now = bool(args.get("send_now"))

    target_uid = _resolve_target_user_id(conn, target)
    if target_uid is None:
        _say(client, conn, slack_user_id, dm_channel,
             f":x: '{target}' 사용자를 찾지 못했어요. Slack 멘션이나 정확한 이름으로 알려주세요.")
        return
    if not text:
        _say(client, conn, slack_user_id, dm_channel,
             ":x: 보낼 DM 본문이 비어있어요.")
        return
    if target_uid == slack_user_id:
        _say(client, conn, slack_user_id, dm_channel,
             ":information_source: 자기 자신에게 보내는 DM이라 초안으로만 보여드릴게요.")
        send_now = False

    if not send_now:
        lines = [
            ":memo: *DM 초안*",
            f"To: <@{target_uid}>",
        ]
        if reason:
            lines.append(f"Reason: {reason}")
        lines.extend(["", text, "", "보내려면 `이대로 보내줘`처럼 다시 말씀해주세요."])
        _say(client, conn, slack_user_id, dm_channel, "\n".join(lines))
        return

    try:
        target_dm = client.conversations_open(users=target_uid)["channel"]["id"]
        client.chat_postMessage(channel=target_dm, text=text)
    except Exception as e:
        log.exception("dm_user 실패 target=%s", target_uid)
        _say(client, conn, slack_user_id, dm_channel, f":x: <@{target_uid}> DM 발송 실패: {e}")
        return

    _say(client, conn, slack_user_id, dm_channel,
         f":envelope_with_arrow: <@{target_uid}>에게 DM 보냈습니다.")
    log.info("dm_user sent: from_admin=%s target=%s reason=%r", slack_user_id, target_uid, reason[:80])


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
