"""Weaviate (vector DB) 래퍼.

181 서버 Weaviate에 vectorizer 모듈이 없어서 BYOV (Bring Your Own Vector):
  - 컬렉션은 vectorizer=none 으로 생성
  - insert 시 우리가 GenOS embed_service 로 만든 vector 를 직접 전달
  - 검색은 query.near_vector() 로 vector 기반
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any
from urllib.parse import urlparse

import weaviate
from weaviate.classes.config import Configure, DataType, Property
from weaviate.classes.query import Filter, MetadataQuery

from .. import embed_service
from ..config import WEAVIATE_URL

log = logging.getLogger(__name__)

COLLECTION_NAME = "SeminarPage"


def _connect() -> weaviate.WeaviateClient:
    parsed = urlparse(WEAVIATE_URL)
    host = parsed.hostname or "localhost"
    port = parsed.port or 8080
    secure = parsed.scheme == "https"
    return weaviate.connect_to_custom(
        http_host=host, http_port=port, http_secure=secure,
        grpc_host=host, grpc_port=50051, grpc_secure=secure,
        skip_init_checks=True,
    )


def _vectorizer_config() -> Any:
    """BYOV — vectorizer 없음. 우리가 vector 직접 제공."""
    return Configure.Vectorizer.none()


def ensure_collection() -> None:
    """SeminarPage 컬렉션 없으면 생성. 멱등."""
    client = _connect()
    try:
        if client.collections.exists(COLLECTION_NAME):
            return
        client.collections.create(
            name=COLLECTION_NAME,
            vectorizer_config=_vectorizer_config(),
            properties=[
                Property(name="submission_id", data_type=DataType.INT),
                Property(name="presenter", data_type=DataType.TEXT),
                Property(name="seminar_date", data_type=DataType.TEXT),
                Property(name="title", data_type=DataType.TEXT),
                Property(name="page_number", data_type=DataType.INT),
                Property(name="content", data_type=DataType.TEXT),                    # vectorize 대상
                Property(name="text_content", data_type=DataType.TEXT),
                Property(name="visual_description", data_type=DataType.TEXT),
                Property(name="page_summary", data_type=DataType.TEXT),
                Property(name="key_points", data_type=DataType.TEXT_ARRAY),
                Property(name="entities", data_type=DataType.TEXT_ARRAY),
                Property(name="tags", data_type=DataType.TEXT_ARRAY),
            ],
        )
        log.info("Weaviate collection created: %s", COLLECTION_NAME)
    finally:
        client.close()


def insert_pages(
    *,
    submission_id: int,
    presenter: str,
    seminar_date: date,
    title: str,
    tags: list[str],
    pages: list[dict[str, Any]],
) -> int:
    """pages: [{page_number, text_content, visual_description, page_summary, key_points, entities}, ...]
    각 페이지의 검색 가능한 통합 텍스트(content)를 만들어 insert.
    Returns: 삽입 개수.
    """
    client = _connect()
    try:
        coll = client.collections.get(COLLECTION_NAME)
        # 기존 같은 submission 데이터 있으면 삭제 (재처리 케이스)
        coll.data.delete_many(
            where=Filter.by_property("submission_id").equal(submission_id)
        )

        # content 텍스트 빌드 → 임베딩 일괄 생성 → insert 시 vector 같이 전달 (BYOV)
        contents: list[str] = []
        for p in pages:
            text_parts = [
                p.get("page_summary", ""),
                p.get("text_content", ""),
                p.get("visual_description", ""),
                " · ".join(p.get("key_points", [])),
            ]
            contents.append("\n".join(part for part in text_parts if part).strip() or " ")

        vectors = embed_service.embed_batch(contents)

        with coll.batch.dynamic() as batch:
            for p, content, vec in zip(pages, contents, vectors):
                ent_names = [e.get("name", "") for e in (p.get("entities") or []) if isinstance(e, dict)]
                batch.add_object(
                    properties={
                        "submission_id": submission_id,
                        "presenter": presenter,
                        "seminar_date": seminar_date.isoformat(),
                        "title": title,
                        "page_number": int(p.get("page_number", 0)),
                        "content": content,
                        "text_content": p.get("text_content", ""),
                        "visual_description": p.get("visual_description", ""),
                        "page_summary": p.get("page_summary", ""),
                        "key_points": list(p.get("key_points") or []),
                        "entities": ent_names,
                        "tags": list(tags),
                    },
                    vector=vec,
                )
        return len(pages)
    finally:
        client.close()


def search(query: str, *, limit: int = 5) -> list[dict[str, Any]]:
    """BYOV: query 텍스트를 임베딩 → near_vector 검색."""
    client = _connect()
    try:
        if not client.collections.exists(COLLECTION_NAME):
            return []
        query_vec = embed_service.embed(query)
        coll = client.collections.get(COLLECTION_NAME)
        result = coll.query.near_vector(
            near_vector=query_vec,
            limit=limit,
            return_metadata=MetadataQuery(distance=True),
        )
        out: list[dict[str, Any]] = []
        for obj in result.objects:
            props = dict(obj.properties)
            props["_distance"] = obj.metadata.distance if obj.metadata else None
            out.append(props)
        return out
    finally:
        client.close()


def delete_submission(submission_id: int) -> int:
    client = _connect()
    try:
        if not client.collections.exists(COLLECTION_NAME):
            return 0
        coll = client.collections.get(COLLECTION_NAME)
        result = coll.data.delete_many(
            where=Filter.by_property("submission_id").equal(submission_id)
        )
        return result.successful or 0
    finally:
        client.close()
