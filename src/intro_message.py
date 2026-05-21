"""봇 자기소개 메시지. 채널 초대 시 자동 게시 + announce_intro 스크립트가 재사용."""
from __future__ import annotations

from .config import ADMIN_JJR
from .services import admin_service


def build_channel_intro() -> str:
    try:
        primary = admin_service.get_primary_admin_id() or ADMIN_JJR
    except Exception:
        primary = ADMIN_JJR
    contact = f"<@{primary}>" if primary else "운영자"
    return f"""\
:wave: 안녕하세요! AI Engineer 주간 세미나 운영 봇 *seminar_bot* 입니다.

이 채널에 초대되어 매주 세미나 관련 공지와 알림을 드릴 예정이에요.

*자동 공지* (이 채널에)
  • 월 09:00 — 이번 주 발표자 + 토픽 미리보기
  • 목 13:30 — 오늘 발표 안내
  • 사이클 추첨 결과 / 자료 제출 / 연기 처리 등 주요 이벤트

*발표 멤버 분들* — 봇 DM 에 그냥 자연어로 보내시면 알아서 처리합니다.
  대부분 슬래시 커맨드 없이도 가능하지만, 빠른 단축키:
  • `/세미나-일정` — 다가올 5주 일정 (본인 차례 ⭐)
  • `/세미나-연기` — 연기 신청 (자연어 멀티턴)
  • `/세미나-토픽` — 토픽 등록 (DM 에 "내 토픽은 X" 도 OK)
  • `/세미나-선호도` — 평소 회피 날짜/주차
  • `/제출` — 발표 자료 PDF 제출 → VLM 분석 + RAG 인입
  • 자료 질문도 봇 DM 에 자유롭게 ("X 발표 핵심 요점")

*운영자 분*
  `/세미나-현황` `/세미나-토픽-알림` `/세미나-안내` `/세미나-공지` `/세미나-재추첨` `/어드민-*`

운영 문의: {contact} :pray:
"""
