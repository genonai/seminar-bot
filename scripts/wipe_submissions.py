"""테스트용 자료 ingest 데이터 전체 초기화.

지우는 것:
  1) submissions 테이블의 모든 row
  2) ChromaDB 'seminar_pages' 컬렉션 전체 (벡터 청크)
  3) data/submissions/ 디렉토리의 모든 PDF 파일

옵션:
  --confirm    실행. (이 플래그 없으면 dry-run, 영향 없이 카운트만 출력)
  --id N       특정 submission_id 하나만 삭제 (전체 wipe 대신)

사용:
  docker compose exec bot python scripts/wipe_submissions.py --confirm
  docker compose exec bot python scripts/wipe_submissions.py --id 3 --confirm
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import DB_PATH
from src.db import session
from src.services import file_storage, vector_service


def _wipe_one(sid: int, confirm: bool) -> None:
    with session(DB_PATH) as conn:
        row = conn.execute("SELECT * FROM submissions WHERE id = ?", (sid,)).fetchone()
        if row is None:
            print(f"[wipe] submission {sid} 없음")
            return
        print(f"[wipe] submission {sid}: presenter={row['presenter']} file={row['file_path']}")

        if not confirm:
            print("[wipe] DRY-RUN. --confirm 추가하면 실 삭제.")
            return

        # 파일 삭제
        try:
            Path(row["file_path"]).unlink(missing_ok=True)
        except Exception as e:
            print(f"  파일 삭제 실패 (무시): {e}")

        # DB row
        conn.execute("DELETE FROM submissions WHERE id = ?", (sid,))
        conn.commit()

    # ChromaDB 청크
    deleted = vector_service.delete_submission(sid)
    print(f"  ChromaDB 청크 {deleted}개 삭제")
    print(f"[wipe] submission {sid} 정리 완료")


def _wipe_all(confirm: bool) -> None:
    with session(DB_PATH) as conn:
        rows = conn.execute("SELECT id, presenter, file_path FROM submissions").fetchall()
    print(f"[wipe] 대상 submissions: {len(rows)}개")
    for r in rows:
        print(f"  id={r['id']}  presenter={r['presenter']}  file={r['file_path']}")

    subs_root = file_storage.SUBMISSIONS_ROOT
    print(f"[wipe] 파일 디렉토리: {subs_root} (존재: {subs_root.exists()})")

    if not confirm:
        print("[wipe] DRY-RUN. --confirm 추가하면 실 삭제.")
        return

    # 1) DB
    with session(DB_PATH) as conn:
        with conn:
            conn.execute("DELETE FROM submissions")
    print("[wipe] submissions 테이블 비움")

    # 2) ChromaDB
    vector_service.reset_all()
    print("[wipe] ChromaDB 컬렉션 reset")

    # 3) 파일
    if subs_root.exists():
        shutil.rmtree(subs_root)
        print(f"[wipe] {subs_root} 삭제")

    print("[wipe] 전체 초기화 완료")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--confirm", action="store_true")
    p.add_argument("--id", type=int, help="특정 submission만 삭제")
    args = p.parse_args()

    if args.id is not None:
        _wipe_one(args.id, args.confirm)
    else:
        _wipe_all(args.confirm)


if __name__ == "__main__":
    main()
