"""셀프 테스트용 — 진재님을 임시로 멤버 풀에 추가하고 5/14 slot_2에 배정.

사용:
  python scripts/admin_test_seed.py        # 적용
  python scripts/admin_test_seed.py --undo # 원상복귀

⚠️ 운영 시작 전에 반드시 --undo 로 정리할 것.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import ADMIN_JJR, DB_PATH
from src.db import connect, init_schema
from src.models import Preferences

ADMIN_NAME = "이진재"
TEST_DATE = "2026-05-14"            # 5/14 slot_2 사용
ORIGINAL_SLOT_2 = "김재선"            # seed_schedule.py --shuffle --seed 42 결과 기준


def apply(conn) -> None:
    init_schema(conn)
    with conn:
        # 진재 멤버 추가 (이미 있으면 slack_id만 갱신)
        conn.execute(
            """
            INSERT INTO members (name, slack_user_id, preferences)
            VALUES (?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET slack_user_id = excluded.slack_user_id
            """,
            (ADMIN_NAME, ADMIN_JJR, Preferences().to_json()),
        )
        # 5/14 slot_2 → 진재 (백업: 원래 이름은 ORIGINAL_SLOT_2 상수에 있음)
        conn.execute(
            "UPDATE schedule SET slot_2 = ? WHERE date = ?",
            (ADMIN_NAME, TEST_DATE),
        )
    print(f"[admin_test_seed] {ADMIN_NAME}({ADMIN_JJR}) 멤버 추가됨")
    print(f"[admin_test_seed] {TEST_DATE} slot_2: {ORIGINAL_SLOT_2} → {ADMIN_NAME}")
    print(f"[admin_test_seed] 셀프 테스트 후 --undo 로 정리 필수")


def undo(conn) -> None:
    with conn:
        conn.execute(
            "UPDATE schedule SET slot_2 = ? WHERE date = ?",
            (ORIGINAL_SLOT_2, TEST_DATE),
        )
        conn.execute("DELETE FROM members WHERE name = ?", (ADMIN_NAME,))
    print(f"[admin_test_seed] {TEST_DATE} slot_2: {ADMIN_NAME} → {ORIGINAL_SLOT_2}")
    print(f"[admin_test_seed] {ADMIN_NAME} 멤버 삭제됨")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--undo", action="store_true")
    args = p.parse_args()

    conn = connect(DB_PATH)
    try:
        if args.undo:
            undo(conn)
        else:
            apply(conn)
        rows = conn.execute("SELECT date, slot_1, slot_2 FROM schedule ORDER BY date").fetchall()
        print()
        for r in rows:
            print(f"  {r['date']}  1부: {r['slot_1'] or '—':6}  2부: {r['slot_2'] or '—':6}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
