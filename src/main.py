"""Socket Mode 엔트리포인트."""
from __future__ import annotations

import logging

from slack_bolt.adapter.socket_mode import SocketModeHandler

from .config import ADMIN_USER_IDS, DB_PATH, LOG_LEVEL, SLACK_APP_TOKEN
from .db import init_schema, session
from .scheduler import start_scheduler
from .services import admin_service, member_service
from .slack.app import build_app


def main() -> None:
    logging.basicConfig(
        level=LOG_LEVEL,
        format="%(asctime)s %(levelname)-7s %(name)s :: %(message)s",
    )
    if not SLACK_APP_TOKEN:
        raise RuntimeError("SLACK_APP_TOKEN 미설정 (.env 확인)")

    app = build_app()
    handler = SocketModeHandler(app, SLACK_APP_TOKEN)

    # 스키마 보장 + admins 부트스트랩 (DB 비어있을 때 env로부터 1회)
    with session(DB_PATH) as conn:
        init_schema(conn)
        n = admin_service.bootstrap_if_empty(conn, ADMIN_USER_IDS)
        if n:
            logging.info("admins bootstrapped: %d명 (%s)", n, ", ".join(ADMIN_USER_IDS))

    # 시작 시 채널 멤버 1회 sync (실패해도 봇은 계속 동작)
    with session(DB_PATH) as conn:
        active, errors = member_service.sync_from_channel(app.client, conn)
        if errors:
            logging.warning("startup sync errors: %s — channels:read scope 확인 필요할 수도", errors)
        else:
            logging.info("startup sync: %d active 멤버", len(active))

    start_scheduler(app.client)            # APScheduler 백그라운드 스레드
    logging.info("Socket Mode 시작 — Ctrl+C로 종료")
    handler.start()


if __name__ == "__main__":
    main()
