"""DB 초기 셋업: 스키마 생성. 멤버 시드는 seed_members.py 참고."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import DB_PATH
from src.db import connect, init_schema


def main() -> None:
    print(f"[init_db] DB_PATH = {DB_PATH}")
    conn = connect(DB_PATH)
    try:
        init_schema(conn)
    finally:
        conn.close()
    print("[init_db] schema OK")


if __name__ == "__main__":
    main()
