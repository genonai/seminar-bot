"""Bolt App 빌더."""
from __future__ import annotations

from slack_bolt import App

from ..config import SLACK_BOT_TOKEN, SLACK_SIGNING_SECRET
from . import actions, commands, dm, events


def build_app() -> App:
    if not SLACK_BOT_TOKEN:
        raise RuntimeError("SLACK_BOT_TOKEN 미설정 (.env 확인)")
    if not SLACK_SIGNING_SECRET:
        raise RuntimeError("SLACK_SIGNING_SECRET 미설정 (.env 확인)")
    app = App(token=SLACK_BOT_TOKEN, signing_secret=SLACK_SIGNING_SECRET)
    commands.register(app)
    dm.register(app)
    actions.register(app)
    events.register(app)
    return app
