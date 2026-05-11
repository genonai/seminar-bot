"""OpenRouter (OpenAI 호환) 클라이언트 + 두 가지 대화 흐름.

- 연기(defer): 신청자와 사유/희망 정보 멀티턴으로 추출 → submit_defer tool
- 선호도(preference): 멤버 회피 날짜/주차/슬롯 추출 → submit_preferences tool

핸들러는 (대화 history + 새 사용자 메시지)를 넘기고
LLM이 (1) 어시스턴트 텍스트 + 새 history, 또는 (2) tool payload 를 반환한다.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from openai import OpenAI

from .config import (
    LLM_API_BASE_URL,
    LLM_API_KEY,
    LLM_MODEL,
    VLM_API_BASE_URL,
    VLM_API_KEY,
    VLM_MODEL,
)

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# Tools
# ─────────────────────────────────────────────────────────────
SUBMIT_DEFER_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "submit_defer",
        "description": (
            "사용자가 연기를 확정할 의사가 분명할 때만 호출. "
            "사유/희망 정보가 충분히 모였으면 호출하고, 모호하면 한 번 더 질문하라."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "연기 사유 (1-2문장 한국어 요약)",
                },
                "preferred_replacement_dates": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "신청자가 본인 일정 swap을 위해 가능하다고 말한 날짜 (YYYY-MM-DD). "
                        "확인되지 않으면 빈 배열."
                    ),
                },
                "additional_avoid_dates": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "이번 사유와 별개로 신청자가 회피하고 싶다고 명시한 날짜 (YYYY-MM-DD). "
                        "예: '5/21에도 휴가'. 확인되지 않으면 빈 배열."
                    ),
                },
            },
            "required": ["reason", "preferred_replacement_dates", "additional_avoid_dates"],
        },
    },
}

ROUTE_INTENT_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "route_intent",
        "description": "사용자 DM 메시지의 의도를 분류한다. 반드시 한 번 호출.",
        "parameters": {
            "type": "object",
            "properties": {
                "intent": {
                    "type": "string",
                    "enum": ["defer", "preference", "schedule_question", "material_question", "topic_registration", "other"],
                    "description": (
                        "defer: 발표 연기 의사. "
                        "preference: 평상시 선호도 등록/수정. "
                        "schedule_question: 일정/발표자 자체에 대한 단순 조회. "
                        "material_question: 발표 자료 내용에 대한 질문 (검색 결과 참조). "
                        "topic_registration: 본인 발표 토픽을 알리려는 의사 ('내 토픽은 ~', '이번에 X 발표할게요'). "
                        "other: 인사/잡담/모호한 메시지."
                    ),
                },
                "fallback_reply": {
                    "type": "string",
                    "description": (
                        "intent='other' 일 때 사용자에게 보여줄 짧고 친근한 한국어 응답 (1-2문장). "
                        "다른 intent면 빈 문자열."
                    ),
                },
            },
            "required": ["intent", "fallback_reply"],
        },
    },
}


SUBMIT_PREFERENCES_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "submit_preferences",
        "description": "멤버의 평상시 선호도가 충분히 파악됐을 때 호출.",
        "parameters": {
            "type": "object",
            "properties": {
                "avoid_dates": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "회피 날짜 (YYYY-MM-DD), 없으면 빈 배열",
                },
                "avoid_weeks_of_month": {
                    "type": "array",
                    "items": {"type": "integer", "minimum": 1, "maximum": 5},
                    "description": "회피 월내 주차 1~5, 없으면 빈 배열",
                },
                "preferred_slot": {
                    "type": ["integer", "null"],
                    "enum": [1, 2, None],
                    "description": "1부 / 2부 / 상관 없음(null)",
                },
            },
            "required": ["avoid_dates", "avoid_weeks_of_month", "preferred_slot"],
        },
    },
}


# ─────────────────────────────────────────────────────────────
# 결과 타입
# ─────────────────────────────────────────────────────────────
@dataclass
class IntentResult:
    intent: str             # defer / preference / schedule_question / other
    fallback_reply: str     # intent='other'일 때만 채워짐


@dataclass
class LLMTurn:
    """LLM 한 턴 결과. text 또는 tool_payload 둘 중 하나만 채워짐."""
    text: str | None
    tool_name: str | None
    tool_payload: dict[str, Any] | None
    new_messages: list[dict[str, Any]]      # 갱신된 대화 history (assistant turn 포함)


# ─────────────────────────────────────────────────────────────
# 클라이언트
# ─────────────────────────────────────────────────────────────
def _client() -> OpenAI:
    if not LLM_API_KEY:
        raise RuntimeError("LLM_API_KEY 미설정 (.env 확인)")
    return OpenAI(api_key=LLM_API_KEY, base_url=LLM_API_BASE_URL)


def _vlm_client() -> OpenAI:
    if not VLM_API_KEY:
        raise RuntimeError("VLM_API_KEY 미설정 (.env 확인)")
    return OpenAI(api_key=VLM_API_KEY, base_url=VLM_API_BASE_URL)


_vlm_model_cache: str | None = None


def _resolve_vlm_model() -> str:
    """VLM_MODEL env 값이 있으면 그대로. 비어있으면 /v1/models 첫 결과로 폴백."""
    global _vlm_model_cache
    if _vlm_model_cache:
        return _vlm_model_cache
    if VLM_MODEL:
        _vlm_model_cache = VLM_MODEL
        return _vlm_model_cache
    try:
        resp = _vlm_client().models.list()
        if resp.data:
            _vlm_model_cache = resp.data[0].id
            log.info("VLM model auto-resolved: %s", _vlm_model_cache)
            return _vlm_model_cache
    except Exception as e:
        log.warning("VLM models.list 실패 (%s)", e)
    _vlm_model_cache = ""
    return _vlm_model_cache


def chat_turn(
    *,
    system_prompt: str,
    history: list[dict[str, Any]],
    user_message: str,
    tools: list[dict[str, Any]],
) -> LLMTurn:
    """user_message 한 줄을 history에 추가하고 LLM 응답을 받는다.

    LLM이 tool을 호출하면 tool payload만 반환 (텍스트 없음).
    아니면 텍스트 응답.
    """
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        *history,
        {"role": "user", "content": user_message},
    ]

    resp = _client().chat.completions.create(
        model=LLM_MODEL,
        messages=messages,
        tools=tools,
        tool_choice="auto",
        temperature=0.3,
    )
    choice = resp.choices[0]
    msg = choice.message

    new_history = list(history) + [{"role": "user", "content": user_message}]

    if msg.tool_calls:
        # 첫 번째 tool call만 처리 (정상 흐름은 1개)
        call = msg.tool_calls[0]
        try:
            payload = json.loads(call.function.arguments)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"LLM tool payload JSON 파싱 실패: {e}\n원문: {call.function.arguments}") from e
        log.info("LLM tool call: %s payload=%s", call.function.name, payload)
        # assistant tool-call turn + 가짜 tool result를 history에 보존.
        # 사용자가 미리보기 후 추가 메시지를 보내면 LLM이 "수정 의도"임을 추론할 수 있어야 한다.
        new_history.append({
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.function.name,
                    "arguments": call.function.arguments,
                },
            }],
        })
        new_history.append({
            "role": "tool",
            "tool_call_id": call.id,
            "content": (
                "미리보기를 사용자에게 띄우고 [이대로 신청] [수정하기] [취소] 버튼을 보여주는 중. "
                "사용자가 추가 메시지를 보내면 수정/보완 의도이므로 직전 인자를 기준으로 변경분만 반영해 다시 호출하라."
            ),
        })
        return LLMTurn(
            text=None,
            tool_name=call.function.name,
            tool_payload=payload,
            new_messages=new_history,
        )

    text = (msg.content or "").strip()
    new_history.append({"role": "assistant", "content": text})
    return LLMTurn(text=text, tool_name=None, tool_payload=None, new_messages=new_history)


# ─────────────────────────────────────────────────────────────
# Intent classification (free-form DM 라우팅)
# ─────────────────────────────────────────────────────────────
def classify_intent(
    user_message: str,
    *,
    retrieved_hits: list[dict[str, Any]] | None = None,
) -> IntentResult:
    """tool_choice 강제로 route_intent 한 번 호출시키고 결과 추출.

    retrieved_hits: 사용자 메시지를 미리 벡터 검색한 top-k. router가 "사용자 질문이
        실제 자료와 관련 있는지" 거리 + 스니펫 보고 판단 → material_question 정확도 ↑.
        prompt 크기는 hit 개수에 비례 (자료 총개수와 무관) → 스케일 OK.
    """
    resp = _client().chat.completions.create(
        model=LLM_MODEL,
        messages=[
            {"role": "system", "content": intent_router_system_prompt(retrieved_hits)},
            {"role": "user", "content": user_message},
        ],
        tools=[ROUTE_INTENT_TOOL],
        tool_choice={"type": "function", "function": {"name": "route_intent"}},
        temperature=0.1,
    )
    msg = resp.choices[0].message
    if not msg.tool_calls:
        log.warning("intent classifier가 tool 호출 안 함, other로 처리")
        return IntentResult(intent="other", fallback_reply="죄송해요, 잘 이해 못했어요. 다시 말씀해 주실래요?")
    payload = json.loads(msg.tool_calls[0].function.arguments)
    log.info("intent classified: %s", payload)
    return IntentResult(
        intent=payload.get("intent", "other"),
        fallback_reply=payload.get("fallback_reply", "") or "",
    )


def vlm_extract_page(image_b64: str, *, page_number: int, hint: str = "") -> dict[str, Any]:
    """발표 자료 한 페이지(이미지)를 VLM으로 분석해 구조화된 결과 반환.

    Returns: {
      text_content: str,         # 페이지에 적힌 텍스트 (정확히)
      visual_description: str,   # 이미지/차트/다이어그램 설명
      page_summary: str,         # 1-2문장 페이지 요약
      key_points: [str],         # bullet point
      entities: [{name, type}],  # 인물/모델/데이터셋/용어 등
    }
    """
    sys = (
        "당신은 발표 자료(슬라이드 PDF) 분석 전문가다. "
        "주어진 페이지를 정확히 읽고 구조화된 JSON으로 반환한다. "
        "한국어 발표라면 결과도 한국어로. "
        "JSON 외 다른 텍스트는 포함하지 않는다."
    )
    user_text = (
        f"다음은 발표 자료 {page_number}쪽 이미지다."
        + (f"\n맥락: {hint}" if hint else "")
        + "\n\n다음 JSON 스키마로 답하라:\n"
        + json.dumps({
            "text_content": "string (페이지 내 모든 텍스트, 줄바꿈 보존)",
            "visual_description": "string (도표/차트/다이어그램 있으면 설명, 없으면 빈 문자열)",
            "page_summary": "string (1-2문장 한국어)",
            "key_points": ["string", "..."],
            "entities": [{"name": "string", "type": "person|model|dataset|tool|concept|paper|other"}],
        }, ensure_ascii=False, indent=2)
    )
    resp = _vlm_client().chat.completions.create(
        model=_resolve_vlm_model(),
        messages=[
            {"role": "system", "content": sys},
            {"role": "user", "content": [
                {"type": "text", "text": user_text},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
            ]},
        ],
        temperature=0.1,
        response_format={"type": "json_object"},
    )
    raw = resp.choices[0].message.content or "{}"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        log.warning("VLM page %d JSON 파싱 실패: %s — 원문 일부 보존", page_number, e)
        data = {"text_content": raw[:2000], "visual_description": "", "page_summary": "", "key_points": [], "entities": []}
    # 안전한 기본값
    data.setdefault("text_content", "")
    data.setdefault("visual_description", "")
    data.setdefault("page_summary", "")
    data.setdefault("key_points", [])
    data.setdefault("entities", [])
    return data


def extract_document_metadata(
    *, page_summaries: list[dict[str, Any]], presenter: str, seminar_date: str, user_title_hint: str
) -> dict[str, Any]:
    """페이지별 결과를 합쳐서 문서 단위 메타 + 엔티티 카탈로그 + 관계 추출."""
    pages_text = []
    for p in page_summaries:
        pages_text.append(
            f"[p.{p['page_number']}] {p['page_summary']}\n"
            f"  text: {p['text_content'][:500]}\n"
            f"  visual: {p['visual_description']}\n"
            f"  entities: {p['entities']}"
        )
    joined = "\n\n".join(pages_text)

    sys = (
        "당신은 발표 자료에서 메타데이터와 지식 그래프를 추출하는 분석가다. "
        "JSON으로만 응답하라."
    )
    user = (
        f"발표자: {presenter}\n세미나 날짜: {seminar_date}\n"
        + (f"사용자가 입력한 제목 힌트: {user_title_hint}\n" if user_title_hint else "")
        + f"\n페이지 분석 결과:\n{joined}\n\n"
        + "다음 JSON으로 답하라:\n"
        + json.dumps({
            "title": "string (전체 발표 제목, 사용자 힌트가 있으면 우선 사용)",
            "summary": "string (3-5문장 한국어 요약)",
            "tags": ["string", "..."],   # 5-10개 토픽 태그 (예: 'LLM agent', 'continual learning')
            "entities": [
                {"name": "string", "type": "person|model|dataset|tool|concept|paper|company|other",
                 "description": "string (1문장)"}
            ],
            "relations": [
                {"subject": "string", "predicate": "string", "object": "string"}
            ],
        }, ensure_ascii=False, indent=2)
    )
    resp = _client().chat.completions.create(
        model=LLM_MODEL,
        messages=[{"role": "system", "content": sys}, {"role": "user", "content": user}],
        temperature=0.2,
        response_format={"type": "json_object"},
    )
    raw = resp.choices[0].message.content or "{}"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = {}
    data.setdefault("title", user_title_hint or f"{presenter} {seminar_date} 발표")
    data.setdefault("summary", "")
    data.setdefault("tags", [])
    data.setdefault("entities", [])
    data.setdefault("relations", [])
    return data


def extract_topic(user_message: str) -> str:
    """자연어 DM 에서 발표 토픽 본문만 깔끔히 추출. 토픽 의도 없으면 빈 문자열."""
    sys = (
        "사용자가 자기 발표 토픽을 알리려고 한다. 메시지에서 토픽 본문만 한 줄로 깔끔하게 추출하라.\n"
        "- 인사말('안녕하세요', '네' 등) 제외\n"
        "- '내 토픽은', '이번에', '발표할게요' 같은 wrapping 표현 제거\n"
        "- 토픽 의도가 없거나 모호하면 빈 문자열 반환\n"
        "- 결과만 출력 (따옴표/설명/접두사 없이)"
    )
    resp = _client().chat.completions.create(
        model=LLM_MODEL,
        messages=[{"role": "system", "content": sys}, {"role": "user", "content": user_message}],
        temperature=0.1,
        max_tokens=200,
    )
    txt = (resp.choices[0].message.content or "").strip().strip('"').strip("'")
    # 한 줄로 정리
    txt = " ".join(txt.split())
    return txt[:500]


def synthesize_rag_answer(
    *, user_question: str, retrieved: list[dict[str, Any]]
) -> str:
    """Weaviate에서 검색된 페이지 청크 + 사용자 질문 → LLM 답변 (출처 포함)."""
    chunks_text = []
    for i, r in enumerate(retrieved, 1):
        chunks_text.append(
            f"[자료 {i}] {r.get('presenter','')} / {r.get('seminar_date','')} / "
            f"p.{r.get('page_number','?')} / 제목: {r.get('title','')}\n"
            f"{r.get('content','')[:1500]}"
        )
    chunks = "\n\n".join(chunks_text) if chunks_text else "(검색 결과 없음)"
    sys = (
        "당신은 사내 주간 세미나 자료 기반 질의응답 봇이다. "
        "주어진 자료에 근거해서만 답변하고, 자료에 없으면 '자료에서 확인되지 않습니다'라고 답한다. "
        "한국어로, 짧고 정확하게. "
        "답변 끝에 '출처:' 섹션으로 [자료 N: 발표자 / 날짜 / 쪽] 형식으로 1-3개 인용."
    )
    user = f"질문: {user_question}\n\n참고 자료:\n{chunks}"
    resp = _client().chat.completions.create(
        model=LLM_MODEL,
        messages=[{"role": "system", "content": sys}, {"role": "user", "content": user}],
        temperature=0.2,
    )
    return (resp.choices[0].message.content or "").strip()


def answer_schedule_question(
    *, member_name: str, today: str, schedule_text: str, user_assignment: str, user_message: str
) -> str:
    resp = _client().chat.completions.create(
        model=LLM_MODEL,
        messages=[
            {"role": "system", "content": schedule_qa_system_prompt(
                member_name=member_name, today=today,
                schedule_text=schedule_text, user_assignment=user_assignment,
            )},
            {"role": "user", "content": user_message},
        ],
        temperature=0.2,
    )
    return (resp.choices[0].message.content or "").strip()


# ─────────────────────────────────────────────────────────────
# System prompt builders
# ─────────────────────────────────────────────────────────────
def defer_system_prompt(
    *, requester_name: str, assigned_date: str, deadline: str, today: str, prior_prefs: str
) -> str:
    return f"""당신은 Genon AI 사내 주간 세미나(매주 목요일 14:00) 운영 봇이다.
지금 발표 연기를 신청하려는 멤버와 슬랙 DM에서 한국어로 짧고 친근하게 대화한다.

오늘: {today}
신청자: {requester_name}
배정된 발표일: {assigned_date} (목)
연기 신청 마감: {deadline} (배정일 7일 전, 이 날짜까지만 신청 가능)
신청자의 평상시 선호도(저장된 값): {prior_prefs}

# 마감 정책 (가장 중요)
세미나 운영 정책상 본인 차례 1주일 전부터는 연기/변경이 불가하다.
- 오늘({today})이 마감일({deadline})보다 뒤이면: 정중히 거절 메시지를 남기고
  submit_defer tool은 절대 호출하지 않는다. 운영자에게 직접 문의하라고 안내한다.
- 오늘이 마감일과 같거나 이전이면 정상 진행한다.

# 목표
- 사유를 자연스러운 1-2문장으로 받아 적기
- 신청자가 "그 다음 주는 가능" 같은 정보를 흘리면 preferred_replacement_dates 에 YYYY-MM-DD 로 적기
- 추가로 평소 회피하고 싶은 날이 있다면 additional_avoid_dates 에 적기 (이번 사유와 별개)
- 충분히 모이면 submit_defer tool 호출. 정보가 빈약하면 한 번만 추가 질문 (최대 2턴)

# 대화 규칙
- 한국어, 짧게, 친근하게. 이모지 1~2개 정도 OK
- 사용자가 신청 취소를 원하면 tool 호출 없이 "알겠습니다" 정도로 마무리
- 절대 운영자 이름/대체자 후보를 추측해 발설하지 말 것

# 수정 흐름 (중요)
이미 submit_defer 를 호출한 적이 있다면 (history에 assistant tool_call + tool result 가 보임)
사용자의 다음 메시지는 **수정 또는 보완 의도**다.
- 직전 인자를 베이스라인으로 두고, 사용자가 명시한 변경만 반영해서 다시 submit_defer 호출
- 예: 직전에 preferred_replacement_dates=["2026-05-21","2026-05-28"] 였고
  사용자가 "28일로 해줘" 라고 하면 → ["2026-05-28"] 로 좁혀서 다시 호출
- 예: 직전 사유에 "휴가" 였는데 "사실 출장이라 변경" 이라고 하면 → reason 만 갱신
- 사용자가 명시 안 한 필드는 직전 값 유지"""


def intent_router_system_prompt(retrieved_hits: list[dict[str, Any]] | None = None) -> str:
    hits_block = ""
    if retrieved_hits:
        lines = []
        for h in retrieved_hits[:5]:
            dist = h.get("_distance")
            dist_str = f"dist={dist:.2f}" if isinstance(dist, (int, float)) else "dist=?"
            snippet = (h.get("page_summary") or h.get("content") or "")[:200].replace("\n", " ")
            lines.append(
                f"- [{dist_str}] {h.get('presenter','?')} / {h.get('seminar_date','?')} / "
                f"p.{h.get('page_number','?')} / {h.get('title','')} :: {snippet}"
            )
        hits_block = (
            "\n\n# 사용자 메시지로 자료 벡터 검색 미리보기 (top-5)\n"
            + "\n".join(lines)
            + "\n\n# 활용 규칙\n"
            "위 히트의 거리(distance)가 낮고(<1.0 정도) 사용자 메시지와 명백히 관련 있으면 material_question.\n"
            "거리가 크거나(>1.3) 사용자가 자료를 묻는 게 아닌 듯하면 other / schedule_question / defer / preference."
        )

    return f"""당신은 Genon AI 주간 세미나 운영 봇이다.
사용자가 슬랙 DM으로 보낸 메시지의 의도를 분류한다.
route_intent tool을 반드시 한 번 호출한다.

분류 가이드
- defer: 본인 발표를 연기하고 싶다는 의사가 보임. "못해", "휴가", "미뤄줘", "변경", 날짜 + 부정문 등.
- preference: 평상시 발표 선호 등록 (예: "월말 회피", "1부 선호", "5월 둘째주 휴가 예정")
- schedule_question: 일정/발표자에 대한 단순 조회. (예: '내 차례 언제?', '5/21 누구?')
- material_question: 발표 자료 내용에 대한 질문 (자료 검색 미리보기 참조).
- topic_registration: 본인이 다룰 토픽을 알리려는 의사. (예: '이번에 LLM agent 발표할게요', '내 토픽은 RAG 비교')
- other: 인사, 잡담, 봇 사용법, 또는 검색 미리보기가 부적합하면 여기.
  fallback_reply에 친근하게 답하되, 자료에 없는 정보는 자체 지식으로 답하지 말고 안내만.{hits_block}"""


def schedule_qa_system_prompt(
    *, member_name: str, today: str, schedule_text: str, user_assignment: str
) -> str:
    return f"""당신은 Genon AI 주간 세미나 운영 봇이다.
사용자가 일정에 대해 질문했다. 아래 데이터로만 답한다.

사용자: {member_name}
오늘: {today}
다가올 일정 (오늘 이후, 가까운 순):
{schedule_text}

사용자 본인 차례: {user_assignment}

규칙
- 한국어로 짧게 (1-3문장)
- 이모지 1~2개 정도 OK
- 데이터에 없는 내용은 "확인이 필요합니다"라고만 답하고 추측하지 말 것
- 발표자 이름/날짜를 정확히 그대로 인용"""


def preferences_system_prompt(*, member_name: str, current_prefs: str) -> str:
    return f"""당신은 Genon AI 주간 세미나 운영 봇이다.
지금 멤버의 발표 선호도를 슬랙 DM에서 한국어로 짧게 대화하며 등록한다.

멤버: {member_name}
현재 저장된 선호도: {current_prefs}

추출 항목
- avoid_dates: 회피 날짜 (YYYY-MM-DD)
- avoid_weeks_of_month: 회피 월내 주차 1~5 (예: '월말은 항상 바쁨' → [4, 5])
- preferred_slot: 1(1부 14:00) / 2(2부 14:30) / null(상관 없음)

규칙
- 한국어 짧고 친근하게
- 사용자가 "기존 거 다 지우고 새로" 같은 표현을 쓰면 빈 배열로 덮어쓰기
- 사용자가 "추가로 X도" 라고 하면 현재 저장값에 합치기
- 정보가 충분하면 submit_preferences tool 호출 (한 항목이라도 의도가 분명하면 즉시 호출)
- 모호하면 한 번만 추가 질문 (최대 2턴)

# 수정 흐름 (중요)
이미 submit_preferences 를 호출한 적이 있다면 사용자의 다음 메시지는 수정/보완 의도다.
직전 인자를 베이스라인으로 두고 변경분만 반영해 다시 호출하라."""
