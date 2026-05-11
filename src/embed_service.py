"""사내 GenOS 임베딩 endpoint — OpenAI 호환.

base_url 형식: https://<host>/api/gateway/rep/serving/{serving_id}/v1
GET /v1/models 로 동적으로 모델 id 발견 (없으면 EMBEDDING_MODEL env 사용).

EMBEDDING_API_BASE_URL 에 serving_id 까지 포함된 full base URL 을 넣는 게 가장 단순:
  https://genos.genon.ai/api/gateway/rep/serving/10/v1
"""
from __future__ import annotations

import logging
import threading

from openai import OpenAI

from .config import (
    EMBEDDING_API_BASE_URL,
    EMBEDDING_API_KEY,
    EMBEDDING_MODEL,
)

log = logging.getLogger(__name__)

_model_cache: str | None = None
_model_lock = threading.Lock()


def _client() -> OpenAI:
    if not EMBEDDING_API_KEY:
        raise RuntimeError("EMBEDDING_API_KEY 미설정 (.env)")
    return OpenAI(api_key=EMBEDDING_API_KEY, base_url=EMBEDDING_API_BASE_URL)


def _resolve_model() -> str:
    """모델 id 결정. env에 명시 있으면 그거 (단 'bge-m3' 같은 placeholder는 무시),
    아니면 GET /v1/models 첫 결과를 캐싱."""
    global _model_cache
    if _model_cache:
        return _model_cache
    with _model_lock:
        if _model_cache:
            return _model_cache
        # GenOS는 model id 가 serving_rev_id (e.g. '559') 형태. EMBEDDING_MODEL이
        # 그 형태(숫자)면 그대로 신뢰, 아니면 동적 발견.
        if EMBEDDING_MODEL and EMBEDDING_MODEL.isdigit():
            _model_cache = EMBEDDING_MODEL
            return _model_cache
        resp = _client().models.list()
        if not resp.data:
            raise RuntimeError("GenOS /v1/models 응답에 model 없음")
        _model_cache = resp.data[0].id
        log.info("embedding model resolved: %s", _model_cache)
        return _model_cache


def embed(text: str) -> list[float]:
    model = _resolve_model()
    resp = _client().embeddings.create(input=text, model=model)
    return list(resp.data[0].embedding)


def embed_batch(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    model = _resolve_model()
    # OpenAI 호환은 input 에 list 허용 — 배치 1회 호출
    resp = _client().embeddings.create(input=texts, model=model)
    # data 순서 보장 (index 기준 정렬)
    items = sorted(resp.data, key=lambda d: d.index)
    return [list(d.embedding) for d in items]
