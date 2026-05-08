"""Slack 업로드 파일 → 호스트 디스크 저장."""
from __future__ import annotations

import logging
import re
from datetime import date
from pathlib import Path

import httpx
from slack_sdk import WebClient

from ..config import DB_PATH, SLACK_BOT_TOKEN

log = logging.getLogger(__name__)

# DB와 같은 부모 디렉토리 아래 submissions/ 두기
SUBMISSIONS_ROOT: Path = Path(DB_PATH).parent / "submissions"


def _safe_name(name: str) -> str:
    # 디렉토리 명에 안전한 문자만, 공백→_, 한국어 허용
    cleaned = re.sub(r"[^\w가-힣.\-]", "_", name, flags=re.UNICODE)
    return cleaned[:120] or "file.pdf"


def download_slack_file(
    client: WebClient,
    *,
    file_id: str,
    presenter: str,
    seminar_date: date,
) -> tuple[Path, str]:
    """Slack에서 파일 다운로드 → 호스트 디스크 저장.

    Returns: (저장된 경로, 원본 파일명).
    """
    info = client.files_info(file=file_id)
    f = info["file"]
    url = f.get("url_private_download") or f.get("url_private")
    if not url:
        raise RuntimeError(f"Slack 파일 url 없음: {file_id}")
    original_name = f.get("name", "file.pdf")
    safe = _safe_name(original_name)

    seminar_dir = SUBMISSIONS_ROOT / seminar_date.isoformat() / _safe_name(presenter)
    seminar_dir.mkdir(parents=True, exist_ok=True)
    dest = seminar_dir / f"{file_id}_{safe}"

    headers = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}
    with httpx.Client(timeout=60.0, follow_redirects=True) as h:
        with h.stream("GET", url, headers=headers) as resp:
            resp.raise_for_status()
            with dest.open("wb") as fp:
                for chunk in resp.iter_bytes(chunk_size=64 * 1024):
                    fp.write(chunk)
    log.info("downloaded %s → %s (%d bytes)", file_id, dest, dest.stat().st_size)
    return dest, original_name
