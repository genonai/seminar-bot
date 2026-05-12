"""봇 자기소개 메시지를 채널에 1회 발송 (수동).

봇이 채널에 invite 되면 자동으로 game intro 게시되지만, 메시지가 누락됐거나
재발송 필요할 때 수동으로 실행.

사용:
  docker compose exec bot python scripts/announce_intro.py              # CHANNEL_ID 로 발송
  docker compose exec bot python scripts/announce_intro.py C09P52U025S  # 특정 채널
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from slack_sdk import WebClient

from src.config import CHANNEL_ID, SLACK_BOT_TOKEN
from src.intro_message import build_channel_intro


def main() -> None:
    if not SLACK_BOT_TOKEN:
        raise RuntimeError("SLACK_BOT_TOKEN 미설정")
    target = sys.argv[1] if len(sys.argv) > 1 else CHANNEL_ID
    client = WebClient(token=SLACK_BOT_TOKEN)
    resp = client.chat_postMessage(channel=target, text=build_channel_intro())
    print(f"[announce_intro] sent ts={resp['ts']} channel={target}")


if __name__ == "__main__":
    main()
