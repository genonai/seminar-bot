"""member_service.sync_from_channel — Slack client mock으로 격리 테스트."""
from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock

from slack_sdk.errors import SlackApiError

from src.db import MIGRATIONS, SCHEMA
from src.services import member_service


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:", detect_types=sqlite3.PARSE_DECLTYPES)
    c.row_factory = sqlite3.Row
    for stmt in SCHEMA:
        c.execute(stmt)
    for table, column, ddl in MIGRATIONS:
        cols = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    return c


def _mock_client(channel_members: list[str], user_profiles: dict[str, dict]) -> MagicMock:
    """conversations_members + users_info 응답을 emulate."""
    client = MagicMock()
    client.conversations_members.return_value = {
        "members": channel_members,
        "response_metadata": {"next_cursor": ""},
    }

    def users_info(user: str) -> dict:
        return {"user": user_profiles[user]}

    client.users_info.side_effect = users_info
    return client


# ─────────────────────────────────────────────────────────────
# 기본 동작
# ─────────────────────────────────────────────────────────────
def test_sync_inserts_new_members_excluding_admins() -> None:
    conn = _conn()
    profiles = {
        "U_A": {"profile": {"display_name_normalized": "이선호"}, "real_name": "이선호"},
        "U_B": {"profile": {"display_name_normalized": "임종석"}, "real_name": "임종석"},
        "U_JJR": {"profile": {"display_name_normalized": "이진재"}, "real_name": "이진재"},
        "U_KDP": {"profile": {"display_name_normalized": "박기돈"}, "real_name": "박기돈"},
    }
    client = _mock_client(["U_A", "U_B", "U_JJR", "U_KDP"], profiles)

    active, errors = member_service.sync_from_channel(
        client, conn, channel_id="C1", exclude_user_ids=("U_JJR", "U_KDP"),
    )

    assert errors == []
    assert sorted(m.name for m in active) == ["이선호", "임종석"]
    assert all(m.slack_user_id not in {"U_JJR", "U_KDP"} for m in active)


def test_sync_skips_bots_and_deleted() -> None:
    conn = _conn()
    profiles = {
        "U_A": {"profile": {"display_name_normalized": "이선호"}, "real_name": "이선호"},
        "U_BOT": {"profile": {}, "real_name": "seimar_bot", "is_bot": True},
        "U_DEL": {"profile": {}, "real_name": "old", "deleted": True},
    }
    client = _mock_client(["U_A", "U_BOT", "U_DEL"], profiles)
    active, _ = member_service.sync_from_channel(
        client, conn, channel_id="C1", exclude_user_ids=(),
    )
    assert [m.name for m in active] == ["이선호"]


def test_sync_marks_left_members_inactive() -> None:
    conn = _conn()
    # 1차 sync: A, B 둘 다 채널에
    profiles_first = {
        "U_A": {"profile": {"display_name_normalized": "A"}, "real_name": "A"},
        "U_B": {"profile": {"display_name_normalized": "B"}, "real_name": "B"},
    }
    client = _mock_client(["U_A", "U_B"], profiles_first)
    member_service.sync_from_channel(client, conn, channel_id="C1", exclude_user_ids=())

    # 2차 sync: B만 (A는 떠남)
    client2 = _mock_client(["U_B"], {"U_B": profiles_first["U_B"]})
    active, _ = member_service.sync_from_channel(client2, conn, channel_id="C1", exclude_user_ids=())

    assert [m.name for m in active] == ["B"]
    # A는 DB에 남아있지만 is_active=0
    a_row = conn.execute("SELECT is_active FROM members WHERE name = 'A'").fetchone()
    assert a_row["is_active"] == 0


def test_sync_preserves_preferences_on_rejoin() -> None:
    conn = _conn()
    # 1차: A 등록 + 선호도 저장
    profiles = {"U_A": {"profile": {"display_name_normalized": "A"}, "real_name": "A"}}
    client = _mock_client(["U_A"], profiles)
    member_service.sync_from_channel(client, conn, channel_id="C1", exclude_user_ids=())
    conn.execute(
        "UPDATE members SET preferences = ? WHERE slack_user_id = 'U_A'",
        ('{"avoid_dates": ["2026-06-04"]}',),
    )
    conn.commit()

    # A 채널 떠남
    client_empty = _mock_client([], {})
    member_service.sync_from_channel(client_empty, conn, channel_id="C1", exclude_user_ids=())

    # A 다시 합류
    member_service.sync_from_channel(client, conn, channel_id="C1", exclude_user_ids=())

    row = conn.execute("SELECT preferences, is_active FROM members WHERE slack_user_id = 'U_A'").fetchone()
    assert row["is_active"] == 1
    assert "2026-06-04" in row["preferences"]


def test_sync_handles_missing_scope_gracefully() -> None:
    conn = _conn()
    client = MagicMock()
    err_resp = {"ok": False, "error": "missing_scope"}
    err = SlackApiError(message="missing_scope", response=err_resp)
    client.conversations_members.side_effect = err
    active, errors = member_service.sync_from_channel(client, conn, channel_id="C1")
    assert active == []
    assert "missing_scope" in errors


def test_get_all_active_filters() -> None:
    conn = _conn()
    profiles = {
        "U_A": {"profile": {"display_name_normalized": "A"}, "real_name": "A"},
        "U_B": {"profile": {"display_name_normalized": "B"}, "real_name": "B"},
    }
    client = _mock_client(["U_A", "U_B"], profiles)
    member_service.sync_from_channel(client, conn, channel_id="C1", exclude_user_ids=())
    # 둘 다 active
    assert len(member_service.get_all_active(conn)) == 2
    # 한 명 비활성화
    conn.execute("UPDATE members SET is_active = 0 WHERE name = 'A'")
    conn.commit()
    active = member_service.get_all_active(conn)
    assert [m.name for m in active] == ["B"]
    # get_all 은 전부 반환
    assert len(member_service.get_all(conn)) == 2
