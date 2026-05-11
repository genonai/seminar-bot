"""SQLite + numpy 기반 벡터 스토어.

Weaviate 등 외부 서비스 의존성 제거. 우리 스케일(연 100 자료 × 50 페이지
≈ 5000 벡터)에서 cosine 검색 ~100ms 수준이라 충분.

저장: submission_pages 테이블의 BLOB 컬럼에 float32 raw bytes.
검색: 전체 벡터 메모리 로드 → numpy dot → top-k.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import date
from typing import Any

import numpy as np

from .. import embed_service
from ..config import DB_PATH, EMBEDDING_DIMENSIONS
from ..db import session

log = logging.getLogger(__name__)


def ensure_collection() -> None:
    """no-op — DB 스키마는 db.init_schema에서 보장."""
    return None


def insert_pages(
    *,
    submission_id: int,
    presenter: str,
    seminar_date: date,
    title: str,
    tags: list[str],
    pages: list[dict[str, Any]],
) -> int:
    """페이지 단위 청크 + 임베딩 일괄 저장. 같은 submission_id 의 기존 row 는 먼저 제거."""
    contents: list[str] = []
    for p in pages:
        parts = [
            p.get("page_summary", ""),
            p.get("text_content", ""),
            p.get("visual_description", ""),
            " · ".join(p.get("key_points", [])),
        ]
        contents.append("\n".join(s for s in parts if s).strip() or " ")

    vectors = embed_service.embed_batch(contents)
    if not vectors:
        return 0

    tags_json = json.dumps(list(tags), ensure_ascii=False)

    with session(DB_PATH) as conn:
        with conn:
            conn.execute(
                "DELETE FROM submission_pages WHERE submission_id = ?",
                (submission_id,),
            )
            for p, content, vec in zip(pages, contents, vectors):
                ent_names = [
                    e.get("name", "") for e in (p.get("entities") or [])
                    if isinstance(e, dict)
                ]
                vec_blob = np.asarray(vec, dtype=np.float32).tobytes()
                conn.execute(
                    """
                    INSERT INTO submission_pages
                      (submission_id, presenter, seminar_date, title, page_number, content,
                       text_content, visual_description, page_summary,
                       key_points, entities, tags, vector)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        submission_id,
                        presenter,
                        seminar_date.isoformat(),
                        title,
                        int(p.get("page_number", 0)),
                        content,
                        p.get("text_content", ""),
                        p.get("visual_description", ""),
                        p.get("page_summary", ""),
                        json.dumps(p.get("key_points") or [], ensure_ascii=False),
                        json.dumps(ent_names, ensure_ascii=False),
                        tags_json,
                        vec_blob,
                    ),
                )
    return len(pages)


def search(query: str, *, limit: int = 5) -> list[dict[str, Any]]:
    """cosine similarity top-k."""
    q = np.asarray(embed_service.embed(query), dtype=np.float32)
    qn = float(np.linalg.norm(q))
    if qn == 0:
        return []

    with session(DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT id, submission_id, presenter, seminar_date, title, page_number,
                   content, page_summary, key_points, entities, vector
            FROM submission_pages
            """
        ).fetchall()

    if not rows:
        return []

    # 벡터 매트릭스 구성 (N × dim)
    matrix = np.frombuffer(
        b"".join(r["vector"] for r in rows),
        dtype=np.float32,
    ).reshape(len(rows), -1)

    if matrix.shape[1] != EMBEDDING_DIMENSIONS:
        log.warning(
            "vector dim mismatch in DB: expected %d, got %d",
            EMBEDDING_DIMENSIONS, matrix.shape[1],
        )

    norms = np.linalg.norm(matrix, axis=1)
    norms[norms == 0] = 1.0   # 0 분모 회피
    sims = (matrix @ q) / (norms * qn)
    top_idx = np.argsort(-sims)[:limit]

    out: list[dict[str, Any]] = []
    for i in top_idx:
        r = rows[int(i)]
        out.append({
            "submission_id": r["submission_id"],
            "presenter": r["presenter"],
            "seminar_date": r["seminar_date"],
            "title": r["title"],
            "page_number": r["page_number"],
            "content": r["content"],
            "page_summary": r["page_summary"],
            "key_points": json.loads(r["key_points"]) if r["key_points"] else [],
            "entities": json.loads(r["entities"]) if r["entities"] else [],
            "_similarity": float(sims[int(i)]),
        })
    return out


def delete_submission(submission_id: int) -> int:
    with session(DB_PATH) as conn:
        with conn:
            cur = conn.execute(
                "DELETE FROM submission_pages WHERE submission_id = ?",
                (submission_id,),
            )
            return cur.rowcount or 0


def count_pages() -> int:
    with session(DB_PATH) as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM submission_pages").fetchone()
        return int(row["c"])
