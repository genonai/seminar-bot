"""ChromaDB 기반 vector store.

embedded (별도 서비스 X). HNSW 인덱스로 native vector search.
BYOV (Bring Your Own Vector) — GenOS embed_service 로 만든 벡터 직접 주입.

저장 위치: {DB_PATH 부모}/chroma/  (호스트 볼륨에 mount되어 영속).
"""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import Any

import chromadb
from chromadb.api.types import EmbeddingFunction

from .. import embed_service
from ..config import DB_PATH

log = logging.getLogger(__name__)

COLLECTION_NAME = "seminar_pages"
CHROMA_PATH = str(Path(DB_PATH).parent / "chroma")

_client: chromadb.PersistentClient | None = None


class _BYOVEmbed(EmbeddingFunction):
    """BYOV 명시 — chromadb가 기본 ONNX 모델 로드 시도하는 걸 막음."""
    def __call__(self, input):
        raise RuntimeError("BYOV: embeddings must be provided explicitly via add(embeddings=...)")


def _get_client() -> chromadb.PersistentClient:
    global _client
    if _client is None:
        Path(CHROMA_PATH).mkdir(parents=True, exist_ok=True)
        _client = chromadb.PersistentClient(path=CHROMA_PATH)
        log.info("ChromaDB persistent client opened: %s", CHROMA_PATH)
    return _client


def _get_collection():
    return _get_client().get_or_create_collection(
        name=COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
        embedding_function=_BYOVEmbed(),
    )


def ensure_collection() -> None:
    _get_collection()


def _meta_for_page(*, submission_id: int, presenter: str, seminar_date: date,
                   title: str, page_number: int, tags: list[str],
                   page: dict[str, Any]) -> dict[str, Any]:
    """Chroma는 metadata 가 scalar (str/int/float/bool)만 허용 → list는 join."""
    ent_names = [
        e.get("name", "") for e in (page.get("entities") or [])
        if isinstance(e, dict) and e.get("name")
    ]
    key_points = list(page.get("key_points") or [])
    return {
        "submission_id": int(submission_id),
        "presenter": presenter,
        "seminar_date": seminar_date.isoformat(),
        "title": title or "",
        "page_number": int(page_number),
        "tags": ", ".join(tags)[:500],
        "entities": ", ".join(ent_names)[:500],
        "key_points": " · ".join(key_points)[:500],
        "page_summary": (page.get("page_summary") or "")[:1000],
    }


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

    coll = _get_collection()

    # 같은 submission 기존 데이터 제거 (재처리 대비)
    try:
        coll.delete(where={"submission_id": int(submission_id)})
    except Exception as e:
        log.debug("delete-before-insert no-op: %s", e)

    ids = [f"s{submission_id}_p{p.get('page_number', i+1)}" for i, p in enumerate(pages)]
    metadatas = [
        _meta_for_page(
            submission_id=submission_id, presenter=presenter,
            seminar_date=seminar_date, title=title,
            page_number=p.get("page_number", i+1),
            tags=tags, page=p,
        )
        for i, p in enumerate(pages)
    ]

    coll.add(
        ids=ids,
        embeddings=vectors,
        documents=contents,
        metadatas=metadatas,
    )
    return len(pages)


def search(query: str, *, limit: int = 5) -> list[dict[str, Any]]:
    """cosine top-k. BYOV — query 텍스트를 embed_service 로 변환."""
    coll = _get_collection()
    q_vec = embed_service.embed(query)
    res = coll.query(
        query_embeddings=[q_vec],
        n_results=limit,
        include=["documents", "metadatas", "distances"],
    )
    out: list[dict[str, Any]] = []
    if not res.get("ids") or not res["ids"][0]:
        return out

    docs = res["documents"][0] if res.get("documents") else [""] * len(res["ids"][0])
    metas = res["metadatas"][0] if res.get("metadatas") else [{}] * len(res["ids"][0])
    dists = res["distances"][0] if res.get("distances") else [None] * len(res["ids"][0])

    for doc, meta, dist in zip(docs, metas, dists):
        meta = meta or {}
        out.append({
            "submission_id": meta.get("submission_id"),
            "presenter": meta.get("presenter"),
            "seminar_date": meta.get("seminar_date"),
            "title": meta.get("title"),
            "page_number": meta.get("page_number"),
            "page_summary": meta.get("page_summary"),
            "content": doc,
            "key_points": (meta.get("key_points") or "").split(" · "),
            "entities": (meta.get("entities") or "").split(", "),
            "_distance": dist,
        })
    return out


def delete_submission(submission_id: int) -> int:
    """submission 한 개 분량 청크 전부 삭제."""
    coll = _get_collection()
    try:
        # 카운트 먼저 (반환용)
        before = coll.get(where={"submission_id": int(submission_id)}, include=[])
        n = len(before.get("ids") or [])
        coll.delete(where={"submission_id": int(submission_id)})
        return n
    except Exception as e:
        log.warning("delete_submission(%d) 실패: %s", submission_id, e)
        return 0


def count_pages() -> int:
    coll = _get_collection()
    return coll.count()


def reset_all() -> None:
    """전체 컬렉션 폐기. wipe_submissions 에서 사용."""
    client = _get_client()
    try:
        client.delete_collection(COLLECTION_NAME)
        log.info("ChromaDB collection %s deleted", COLLECTION_NAME)
    except Exception as e:
        log.debug("reset_all no-op: %s", e)
