"""봇 자기소개 메시지를 채널에 1회 발송.

사용:
  docker compose exec bot python scripts/announce_intro.py

운영 시작 시점, 채널에 봇 들여놓고 한 번만 돌리면 된다.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from slack_sdk import WebClient

from src.config import CHANNEL_ID, SLACK_BOT_TOKEN

MESSAGE = """\
:wave: 안녕하세요. 주간 세미나 운영 봇 *seminar_bot* 인사드립니다.

이제 슬랙 채널 내에서 다음을 실행해 보세요:

• `/세미나-일정` — 다가올 일정 보기 (본인 차례에 :star: 표시)
• `/세미나-연기` — 못할 사정 생기면 자연어로 말씀해주세요. (예: _"5/21 휴가라 못합니다"_)
• `/세미나-선호도` — 평소 회피하고 싶은 날짜/주차 미리 등록 (예: _"월말은 항상 출장이라 빼주세요"_)

명령 외에 봇 DM에 그냥 자유롭게 메시지 보내셔도 알아서 분류해 처리합니다.

연기 신청은 운영자 + 자동 선정된 대체자 양쪽 승인 후 반영됩니다. 본인 발표 1주일 전부터는 변경 불가하니 그 전에 알려주세요 :pray:
"""


def main() -> None:
    if not SLACK_BOT_TOKEN:
        raise RuntimeError("SLACK_BOT_TOKEN 미설정")
    client = WebClient(token=SLACK_BOT_TOKEN)
    resp = client.chat_postMessage(channel=CHANNEL_ID, text=MESSAGE)
    print(f"[announce_intro] sent ts={resp['ts']} channel={resp['channel']}")


if __name__ == "__main__":
    main()
