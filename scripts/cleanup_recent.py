"""최근 N시간 내 봇이 게시한 파일/메시지 일괄 삭제 (배포 실수 정리용).

기본 dry-run. 실제 삭제하려면 --confirm.

사용:
  docker compose exec bot python scripts/cleanup_recent.py             # dry run, 최근 24h
  docker compose exec bot python scripts/cleanup_recent.py --confirm   # 실 삭제
  docker compose exec bot python scripts/cleanup_recent.py --hours 6 --confirm

필요한 scope:
  - files:write (이미 있어야 함 — 자료 배포에 사용)
  - chat:write (이미 있음)
  - channels:history (메시지 삭제 시 필요. 없으면 메시지는 skip 되고 파일만 삭제됨)
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from src.config import BROADCAST_CHANNELS, SLACK_BOT_TOKEN


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--hours", type=int, default=24)
    p.add_argument("--confirm", action="store_true")
    args = p.parse_args()

    if not SLACK_BOT_TOKEN:
        raise RuntimeError("SLACK_BOT_TOKEN 미설정")

    client = WebClient(token=SLACK_BOT_TOKEN)
    bot_user_id = client.auth_test()["user_id"]
    since = int(time.time()) - args.hours * 3600
    mode = "REAL DELETE" if args.confirm else "DRY-RUN"
    print(f"[{mode}] bot={bot_user_id}  hours={args.hours}  since={since}")
    print(f"  채널: {BROADCAST_CHANNELS}\n")

    # 1) 봇이 업로드한 파일 (files.delete = 모든 shared 위치에서 제거)
    print("--- 봇 업로드 파일 (files.list) ---")
    try:
        resp = client.files_list(user=bot_user_id, count=100)
        files = resp.get("files", [])
    except SlackApiError as e:
        print(f"  files.list 실패: {e.response['error']}")
        files = []

    target_files = [f for f in files if (f.get("created", 0) or 0) >= since]
    print(f"  대상 파일: {len(target_files)}개")
    for f in target_files:
        print(f"    {f.get('id')}  {f.get('name')}  created={f.get('created')}")
        if args.confirm:
            try:
                client.files_delete(file=f["id"])
                print("      → deleted")
            except SlackApiError as e:
                print(f"      → 실패: {e.response['error']}")

    # 2) 채널 메시지 — 봇이 보낸 텍스트 메시지 (파일 없음)
    print("\n--- 봇 채널 메시지 (conversations.history) ---")
    for ch in BROADCAST_CHANNELS:
        print(f"  채널 {ch}:")
        try:
            hist = client.conversations_history(channel=ch, limit=30)
        except SlackApiError as e:
            print(f"    history 실패: {e.response['error']} (channels:history scope 필요할 수 있음)")
            continue

        for m in hist.get("messages", []):
            ts = float(m.get("ts", "0"))
            if ts < since:
                continue
            if not (m.get("user") == bot_user_id or m.get("bot_id")):
                continue
            preview = (m.get("text") or "")[:60].replace("\n", " ")
            print(f"    ts={m.get('ts')}  text={preview!r}")
            if args.confirm:
                try:
                    client.chat_delete(channel=ch, ts=m["ts"])
                    print("      → deleted")
                except SlackApiError as e:
                    print(f"      → 실패: {e.response['error']}")

    print(f"\n[{mode}] 완료. --confirm 추가하지 않으면 dry run.")


if __name__ == "__main__":
    main()
