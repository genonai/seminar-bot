"""PDF 자료 → VLM 페이지 분석 → 문서 메타 → Weaviate 저장."""
from __future__ import annotations

import base64
import io
import logging
from pathlib import Path
from typing import Any

import pypdfium2 as pdfium

from .. import llm_service
from . import submission_service, vector_service

log = logging.getLogger(__name__)


MAX_PAGES_PER_SUBMISSION = 100   # 비용/시간 안전판
PAGE_RENDER_DPI = 144             # 144 = 2x screen, VLM이 텍스트 잘 읽음
JPEG_QUALITY = 85                 # 너무 크면 토큰 비용 ↑


def _render_page_b64(pdf: pdfium.PdfDocument, page_index: int) -> str:
    """PDF 페이지 → JPEG → base64 (data URL용)."""
    page = pdf[page_index]
    bitmap = page.render(scale=PAGE_RENDER_DPI / 72)
    pil_img = bitmap.to_pil()
    buf = io.BytesIO()
    pil_img.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _ingest_pdf(file_path: Path, *, presenter: str, seminar_date_str: str, title_hint: str) -> dict[str, Any]:
    """파일 한 개 처리. 페이지별 VLM + 문서 메타 추출. Weaviate insert는 호출자가."""
    pdf = pdfium.PdfDocument(str(file_path))
    n = min(len(pdf), MAX_PAGES_PER_SUBMISSION)
    if len(pdf) > MAX_PAGES_PER_SUBMISSION:
        log.warning("PDF %d 페이지 — 처음 %d만 처리", len(pdf), MAX_PAGES_PER_SUBMISSION)

    pages: list[dict[str, Any]] = []
    for i in range(n):
        page_no = i + 1
        log.info("VLM 페이지 %d/%d 처리 중...", page_no, n)
        try:
            b64 = _render_page_b64(pdf, i)
            extracted = llm_service.vlm_extract_page(
                b64,
                page_number=page_no,
                hint=f"발표자={presenter}, 날짜={seminar_date_str}",
            )
        except Exception as e:                  # 한 페이지 실패해도 진행
            log.exception("페이지 %d VLM 실패: %s", page_no, e)
            extracted = {
                "text_content": "",
                "visual_description": "",
                "page_summary": f"[처리 실패: {e}]",
                "key_points": [],
                "entities": [],
            }
        extracted["page_number"] = page_no
        pages.append(extracted)

    # 문서 단위 메타 추출
    doc_meta = llm_service.extract_document_metadata(
        page_summaries=pages,
        presenter=presenter,
        seminar_date=seminar_date_str,
        user_title_hint=title_hint,
    )

    return {
        "page_count": n,
        "pages": pages,
        "title": doc_meta.get("title", ""),
        "summary": doc_meta.get("summary", ""),
        "tags": list(doc_meta.get("tags", [])),
        "entities": list(doc_meta.get("entities", [])),
        "relations": list(doc_meta.get("relations", [])),
    }


def ingest_submission(conn, submission_id: int, *, title_hint: str = "") -> dict[str, Any]:
    """submission row를 처리. 결과는 호출자가 mark_ingested로 저장."""
    sub = submission_service.get(conn, submission_id)
    if sub is None:
        raise ValueError(f"submission {submission_id} 없음")

    submission_service.mark_processing(conn, submission_id)
    file_path = Path(sub.file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"PDF 없음: {file_path}")

    result = _ingest_pdf(
        file_path,
        presenter=sub.presenter,
        seminar_date_str=sub.seminar_date.isoformat(),
        title_hint=title_hint,
    )

    # Weaviate insert
    vector_service.ensure_collection()
    vector_service.insert_pages(
        submission_id=submission_id,
        presenter=sub.presenter,
        seminar_date=sub.seminar_date,
        title=result["title"],
        tags=result["tags"],
        pages=result["pages"],
    )

    submission_service.mark_ingested(
        conn, submission_id,
        page_count=result["page_count"],
        title=result["title"],
        summary=result["summary"],
        tags=result["tags"],
        entities=result["entities"],
        relations=result["relations"],
    )
    log.info("submission %d ingested: %d pages, title=%r", submission_id, result["page_count"], result["title"])
    return result
