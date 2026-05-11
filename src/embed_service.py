"""사내 GenOS 임베딩 endpoint 래퍼.

호출 포맷 (OpenAI 호환 아님):
  POST {base}/api/serving/{serving_id}/{serving_rev_id}
  Headers: Authorization: Bearer {key}
  Body: {"message": "텍스트", "serving_id": int, "serving_rev_id": int}

응답 shape는 명시되지 않아 여러 형태 대응 (data[0].embedding / embedding / embeddings / result).
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from .config import (
    EMBEDDING_API_BASE_URL,
    EMBEDDING_API_KEY,
    EMBEDDING_SERVING_ID,
    EMBEDDING_SERVING_REV_ID,
)

log = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(60.0, connect=10.0)


def _endpoint() -> str:
    return (
        f"{EMBEDDING_API_BASE_URL.rstrip('/')}"
        f"/api/serving/{EMBEDDING_SERVING_ID}/{EMBEDDING_SERVING_REV_ID}"
    )


def _extract_vector(data: Any) -> list[float]:
    """다양한 GenOS 응답 shape에서 임베딩 벡터 추출."""
    if isinstance(data, list) and data and isinstance(data[0], (int, float)):
        return [float(x) for x in data]
    if not isinstance(data, dict):
        raise ValueError(f"임베딩 응답 type 미지원: {type(data).__name__}")

    # 1) OpenAI 호환: {data: [{embedding: [...]}]}
    if "data" in data and data["data"]:
        d0 = data["data"][0]
        if isinstance(d0, dict) and "embedding" in d0:
            return [float(x) for x in d0["embedding"]]

    # 2) {embedding: [...]}
    if "embedding" in data and isinstance(data["embedding"], list):
        return [float(x) for x in data["embedding"]]

    # 3) {embeddings: [[...]]}
    if "embeddings" in data and data["embeddings"]:
        first = data["embeddings"][0]
        if isinstance(first, list):
            return [float(x) for x in first]

    # 4) {result: [...]} 또는 {result: {embedding: [...]}}
    if "result" in data:
        r = data["result"]
        if isinstance(r, list) and r and isinstance(r[0], (int, float)):
            return [float(x) for x in r]
        if isinstance(r, dict) and "embedding" in r:
            return [float(x) for x in r["embedding"]]
        if isinstance(r, list) and r and isinstance(r[0], list):
            return [float(x) for x in r[0]]

    # 5) {response: {...}} 으로 한 번 wrapping된 경우
    if "response" in data:
        return _extract_vector(data["response"])

    raise ValueError(
        f"임베딩 응답 shape 미지원: keys={list(data.keys())}"
    )


def embed(text: str) -> list[float]:
    if not EMBEDDING_API_KEY:
        raise RuntimeError("EMBEDDING_API_KEY 미설정 (.env)")
    payload = {
        "message": text,
        "serving_id": EMBEDDING_SERVING_ID,
        "serving_rev_id": EMBEDDING_SERVING_REV_ID,
    }
    headers = {
        "Authorization": f"Bearer {EMBEDDING_API_KEY}",
        "Content-Type": "application/json",
    }
    url = _endpoint()
    with httpx.Client(timeout=_TIMEOUT, verify=True) as h:
        r = h.post(url, json=payload, headers=headers)
        if r.status_code >= 400:
            raise RuntimeError(
                f"embed HTTP {r.status_code} {url}: {r.text[:500]}"
            )
        try:
            data = r.json()
        except Exception as e:
            raise RuntimeError(f"embed 응답 JSON 파싱 실패: {e} body={r.text[:500]}") from e
    return _extract_vector(data)


def embed_batch(texts: list[str]) -> list[list[float]]:
    """직렬 호출. GenOS가 배치 지원하면 추후 단일 요청으로 최적화."""
    out: list[list[float]] = []
    for i, t in enumerate(texts):
        try:
            out.append(embed(t))
        except Exception as e:
            log.error("embed batch %d/%d 실패: %s — 0 벡터로 채움", i + 1, len(texts), e)
            from .config import EMBEDDING_DIMENSIONS
            out.append([0.0] * EMBEDDING_DIMENSIONS)
    return out
