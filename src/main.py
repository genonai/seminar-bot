"""Socket Mode 엔트리포인트."""
from __future__ import annotations

import logging

from slack_bolt.adapter.socket_mode import SocketModeHandler

from .config import LOG_LEVEL, SLACK_APP_TOKEN
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
    logging.info("Socket Mode 시작 — Ctrl+C로 종료")
    handler.start()


if __name__ == "__main__":
    main()
