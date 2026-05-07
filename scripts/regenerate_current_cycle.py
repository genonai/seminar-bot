"""현재(=가장 큰 cycle_id) 사이클을 폐기하고 활성 멤버 기반으로 재추첨.

사용 시나리오: 멤버 풀에 변동이 있어서 진행 중 사이클을 새로 짜고 싶을 때.

실행:
  docker compose exec bot python scripts/regenerate_current_cycle.py

⚠️ 다음 cycle_id로 새로 생성되며, 이전 cycle_id의 모든 schedule은 삭제된다.
   진행 중 defer 신청이 있으면 먼저 처리/취소 후 실행 권장.
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import DB_PATH
from src.db import session
from src.services import cycle_service


def main() -> None:
    with session(DB_PATH) as conn:
        row = conn.execute("SELECT MAX(cycle_id) AS m FROM schedule").fetchone()
        current_cid = row["m"]
        if current_cid is not None:
            with conn:
                deleted = conn.execute(
                    "DELETE FROM schedule WHERE cycle_id = ?", (current_cid,)
                ).rowcount
            print(f"[regen] 기존 cycle {current_cid} 일정 {deleted}개 삭제")

        # 내일 기준으로 다음 목요일부터 시작 (오늘이 목요일이면 다음 주)
        anchor = date.today() + timedelta(days=1)
        new_cid, schedules = cycle_service.generate_next_cycle(conn, anchor)
        print(f"\n[regen] 새 cycle {new_cid}:")
        for s in schedules:
            slot_1 = s.slot_1 or "—"
            slot_2 = s.slot_2 or "—"
            print(f"  {s.date.isoformat()} (목)  1부: {slot_1}  2부: {slot_2}")


if __name__ == "__main__":
    main()
