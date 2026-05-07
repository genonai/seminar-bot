"""토큰 유효성 확인. auth.test로 bot token, AppsConnectionsOpen으로 app token 검증.

토큰 자체는 출력하지 않음. 성공 시 team/bot 이름만 보여준다.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from src.config import (
    SLACK_APP_TOKEN,
    SLACK_BOT_TOKEN,
    SLACK_SIGNING_SECRET,
    CHANNEL_ID,
)


def check_present() -> None:
    missing = [
        n for n, v in (
            ("SLACK_BOT_TOKEN", SLACK_BOT_TOKEN),
            ("SLACK_APP_TOKEN", SLACK_APP_TOKEN),
            ("SLACK_SIGNING_SECRET", SLACK_SIGNING_SECRET),
        ) if not v
    ]
    if missing:
        print(f"[FAIL] .env에 빠진 값: {', '.join(missing)}")
        sys.exit(1)
    print("[OK] .env에서 토큰 3종 로드됨")


def check_bot_token() -> None:
    if not SLACK_BOT_TOKEN or not SLACK_BOT_TOKEN.startswith("xoxb-"):
        print(f"[FAIL] SLACK_BOT_TOKEN은 'xoxb-' 로 시작해야 함 (지금: '{(SLACK_BOT_TOKEN or '')[:5]}...')")
        sys.exit(1)
    client = WebClient(token=SLACK_BOT_TOKEN)
    try:
        resp = client.auth_test()
    except SlackApiError as e:
        print(f"[FAIL] bot token auth.test 실패: {e.response['error']}")
        sys.exit(1)
    print(f"[OK] bot token 유효: team={resp['team']}, bot_user_id={resp['user_id']}, bot={resp['user']}")


def check_app_token() -> None:
    if not SLACK_APP_TOKEN or not SLACK_APP_TOKEN.startswith("xapp-"):
        print(f"[FAIL] SLACK_APP_TOKEN은 'xapp-' 로 시작해야 함 (지금: '{(SLACK_APP_TOKEN or '')[:5]}...')")
        sys.exit(1)
    client = WebClient()
    try:
        resp = client.apps_connections_open(app_token=SLACK_APP_TOKEN)
    except SlackApiError as e:
        print(f"[FAIL] app token apps.connections.open 실패: {e.response['error']}")
        print("       → connections:write scope이 있는지 확인")
        sys.exit(1)
    if not resp.get("ok"):
        print(f"[FAIL] app token 응답 ok=false: {resp}")
        sys.exit(1)
    print("[OK] app token 유효 (Socket Mode 연결 가능)")


def check_channel_membership() -> None:
    """선택 검증 — channels:read scope 없으면 skip. 봇 동작 자체엔 영향 없음."""
    client = WebClient(token=SLACK_BOT_TOKEN)
    try:
        resp = client.conversations_info(channel=CHANNEL_ID)
    except SlackApiError as e:
        err = e.response["error"]
        if err == "missing_scope":
            print("[SKIP] 채널 접근 검증 — channels:read scope 미부여 (봇 동작엔 영향 없음)")
            return
        if err == "channel_not_found":
            print(f"[WARN] 채널 {CHANNEL_ID} 못 찾음 — bot이 채널에 invite 안 됐을 수 있음")
            print("       → 채널 가서: /invite @세미나봇")
            return
        print(f"[WARN] conversations.info 실패: {err}")
        return
    channel = resp["channel"]
    print(f"[OK] 채널 접근 가능: #{channel['name']} (id={channel['id']})")


if __name__ == "__main__":
    check_present()
    check_bot_token()
    check_app_token()
    check_channel_membership()
    print("\n전부 통과. 봇 실행 준비 OK.")
