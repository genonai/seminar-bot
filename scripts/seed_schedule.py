"""다음 사이클(5주) 일정 시딩.

실 운영에선 진재님이 별도 추첨 도구로 순서를 정해 직접 슬롯을 채울 것이므로
이 스크립트는 1차 동작 확인용이다. 옵션:

  --start YYYY-MM-DD   시작 목요일 지정 (기본: 오늘 이후 첫 목요일)
  --shuffle            멤버 순서 랜덤 (기본은 MEMBER_ROSTER 정의 순)
  --seed N             shuffle 시드 값 (재현 가능)
  --cycle-id N         cycle_id (기본: 기존 최대 + 1)
  --dry-run            DB 쓰지 않고 계획만 출력

이미 같은 날짜에 일정이 있으면 ON CONFLICT로 갱신된다.
"""
from __future__ import annotations

import argparse
import random
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import CYCLE_LENGTH_WEEKS, DB_PATH, MEMBER_ROSTER, SLOTS_PER_WEEK
from src.db import connect, init_schema
from src.models import Schedule
from src.services import schedule_service


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--start", type=date.fromisoformat)
    p.add_argument("--shuffle", action="store_true")
    p.add_argument("--seed", type=int)
    p.add_argument("--cycle-id", type=int)
    p.add_argument("--dry-run", action="store_true")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    start = args.start or schedule_service.next_thursday(date.today())
    if start.weekday() != 3:
        raise ValueError(f"--start는 목요일이어야 함 (받은 값: {start} = {start.strftime('%A')})")

    names = list(MEMBER_ROSTER.keys())
    if args.shuffle:
        rng = random.Random(args.seed) if args.seed is not None else random.Random()
        rng.shuffle(names)

    conn = connect(DB_PATH)
    try:
        init_schema(conn)
        if args.cycle_id is not None:
            cycle_id = args.cycle_id
        else:
            row = conn.execute("SELECT COALESCE(MAX(cycle_id), 0) AS m FROM schedule").fetchone()
            cycle_id = row["m"] + 1

        plan: list[Schedule] = []
        # CYCLE_LENGTH_WEEKS 주에 SLOTS_PER_WEEK 슬롯씩 배정. 멤버 부족하면 마지막 슬롯은 None.
        idx = 0
        for w in range(CYCLE_LENGTH_WEEKS):
            seminar = start + timedelta(weeks=w)
            slot_1 = names[idx] if idx < len(names) else None
            idx += 1
            slot_2 = names[idx] if idx < len(names) else None
            idx += 1
            plan.append(
                Schedule(
                    date=seminar,
                    reminder_date=seminar - timedelta(days=1),
                    slot_1=slot_1,
                    slot_2=slot_2,
                    cycle_id=cycle_id,
                )
            )

        print(f"[seed_schedule] cycle_id={cycle_id}, start={start.isoformat()}, dry_run={args.dry_run}")
        for s in plan:
            print(f"  {s.date.isoformat()} (목)  1부: {s.slot_1 or '—'}  2부: {s.slot_2 or '—'}")

        if args.dry_run:
            print("[seed_schedule] dry-run, DB 미반영")
            return

        for s in plan:
            schedule_service.upsert(conn, s)
        print(f"[seed_schedule] {len(plan)}개 일정 upsert 완료")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
